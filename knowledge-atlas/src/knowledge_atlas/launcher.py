"""Start or attach to one compatible local service per external data root."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from .contracts import AtlasError,RuntimeConfig,ServiceContext
from .locks import FileLock,is_locked
from .http_api import create_app,write_private
from .client import AtlasClient


def load_config(path=None):
    path=path or os.environ.get('KNOWLEDGE_ATLAS_CONFIG')
    if path:return json.loads(Path(path).expanduser().read_text())
    root=Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share'))/'knowledge-atlas'
    return {'service_root':str(root/'service')}


def service_context(config):
    if not isinstance(config,dict) or set(config)-{'service_root','runtime','configuration'}:
        raise AtlasError('invalid_configuration','service_root, runtime and configuration are supported',422)
    root=Path(config['service_root']).expanduser().resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise AtlasError('invalid_location','service data must live outside the plugin',422)
    runtime=config.get('runtime')
    if runtime:
        runtime=RuntimeConfig(Path(runtime['runtime_root']).expanduser().resolve(),
                              Path(runtime['manifest']).expanduser().resolve() if runtime.get('manifest') else None)
    return ServiceContext(root,runtime,config.get('configuration',{}))


def ensure_service(config):
    context=service_context(config)
    root=context.service_root
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    deadline=time.monotonic()+20
    while True:
        try:
            with FileLock(root/'startup.lock'):
                if is_locked(root/'service.lock'):
                    client=AtlasClient(root)
                    client.request('/v1/status')
                    return json.loads((root/'discovery.json').read_text())
                path=root/'launch-config.json'
                write_private(path,json.dumps(config))
                with (root/'service.log').open('ab') as log:
                    process=subprocess.Popen([sys.executable,'-m','knowledge_atlas.cli','serve','--config',str(path)],
                        stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parent.parent)})
                while time.monotonic()<deadline:
                    if process.poll() is not None:raise AtlasError('service_unavailable','service exited; inspect service.log',503)
                    try:
                        client=AtlasClient(root);client.request('/v1/status')
                        return json.loads((root/'discovery.json').read_text())
                    except (OSError,ValueError,AtlasError):time.sleep(.05)
                raise AtlasError('service_unavailable','service startup timed out; inspect service.log',503)
        except AtlasError as error:
            if error.code!='busy' or time.monotonic()>=deadline:raise
            time.sleep(.05)


def serve_service(config):
    import signal
    import threading
    app=create_app(service_context(config)).start()
    stopping=threading.Event()
    def stop_signal(*_):stopping.set()
    signal.signal(signal.SIGTERM,stop_signal)
    signal.signal(signal.SIGINT,stop_signal)
    while app.server is not None and not stopping.wait(.1):pass
    if app.server is not None:app.stop(wait=True)
