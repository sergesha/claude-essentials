"""Knowledge Atlas command entry point."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .contracts import RuntimeConfig, AtlasError
from .runtime import RuntimeMismatch, prepare_runtime, validate_runtime


def main() -> int:
    parser = argparse.ArgumentParser(prog='knowledge-atlas')
    commands = parser.add_subparsers(dest='command', required=True)
    runtime = commands.add_parser('runtime')
    runtime.add_argument('operation', choices=('prepare', 'check'))
    runtime.add_argument('--config', type=Path, required=True)
    for name in ('serve','mcp','status','stop'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path)
        if name == 'mcp':
            command.add_argument('--channel', choices=('mcp','viewer'), default='mcp')
    view = commands.add_parser('view')
    view.add_argument('--atlas',required=True)
    view.add_argument('--config',type=Path)
    view.add_argument('--docker-project')
    view.add_argument('--port',type=int,default=0)
    args = parser.parse_args()
    try:
        if args.command == 'view':
            import os
            from .viewer.backend import run_viewer
            run_viewer(args.atlas,args.config,args.docker_project or os.getenv('KNOWLEDGE_ATLAS_DOCKER_PROJECT'),args.port)
            return 0
        if args.command != 'runtime':
            from .launcher import load_config, serve_service, service_context
            from .client import AtlasClient
            if args.command == 'mcp':
                import asyncio
                from .mcp_bridge import SessionClient, default_project, serve
                project = default_project() if args.config is None else None
                asyncio.run(serve(SessionClient(args.config, args.channel, project)))
                return 0
            config = load_config(args.config)
            if args.command == 'serve':
                serve_service(config)
            else:
                client = AtlasClient(service_context(config).service_root)
                print(json.dumps(client.stop() if args.command == 'stop' else client.request('/v1/status')))
            return 0
        value = json.loads(args.config.read_text())
        if not isinstance(value, dict) or set(value) - {'runtime_root', 'manifest'}:
            raise ValueError('configuration accepts runtime_root and optional manifest')
        root = Path(value['runtime_root']).expanduser()
        if not root.is_absolute():
            root = args.config.resolve().parent / root
        manifest = value.get('manifest')
        if manifest is not None:
            manifest = Path(manifest).expanduser()
            if not manifest.is_absolute():
                manifest = args.config.resolve().parent / manifest
        config = RuntimeConfig(root, manifest)
        function = prepare_runtime if args.operation == 'prepare' else validate_runtime
        print(json.dumps(asdict(function(config)), default=str))
        return 0
    except AtlasError as error:
        print(json.dumps({'code':error.code,'message':str(error),'status':error.status}),file=sys.stderr)
    except RuntimeMismatch as error:
        print(json.dumps({'code': error.code, 'component': error.component, 'message': str(error)}), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'code': 'invalid_configuration', 'message': str(error)}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
