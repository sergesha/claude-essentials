"""Loopback-only HTTP boundary around the shared operation handlers."""
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import re
import threading
from . import __version__
from .api_models import parse_request, schema
from .contracts import ActionContext, AtlasError
from .operations import initialize_service_storage, Dispatcher, inspect_operation, resume, submit
from .locks import FileLock
from .providers.base import ProviderError
from .atomic import write_private


class LocalHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128


def invoke(context,request):
    from .actions import register_handlers
    from .actions.atlas import list_atlases, open_atlas, atlas_status
    from .actions.decisions import inspect_candidate
    from .actions.packages import inspect_packages
    from .actions.reads import read_action
    if request.action == 'atlas.list':
        return 200,list_atlases(context,request)
    if request.action == 'atlas.open' and request.payload:
        raise AtlasError('invalid_request', 'atlas.open takes an atlas_id UUID and an empty or omitted payload')
    if request.action=='operation.read':
        return 200,inspect_operation(context,request.payload['operation_id'])
    if request.action=='operation.resume':
        return 202,resume(context,request.payload['operation_id'],request.request_id)
    if request.action in register_handlers(context):return 202,submit(context,request)
    reads={'atlas.open':open_atlas,'atlas.status':atlas_status,'candidate.inspect':inspect_candidate,
           'packages.inspect':inspect_packages,**{name:read_action for name in ('search','evidence.read','knowledge.apply','history','diff','wiki.pages','wiki.read','question.ask')}}
    if request.action not in reads:raise AtlasError('unsupported_action',request.action,422)
    return 200,reads[request.action](ActionContext.for_read(context,request))


class LocalApp:
    def __init__(self,context):
        self.context=context
        self.dispatcher=Dispatcher(context)
        self.thread=None
        self.server=None
        self.lock=FileLock(context.service_root/'service.lock')
        self.token=None
        self.endpoint=None
        self.admission=threading.Condition()
        self.submissions=0
        self.stopping=False
        self.shutdown_lock=threading.Lock()

    def start(self):
        initialize_service_storage(self.context)
        os.chmod(self.context.service_root,0o700)
        self.lock.__enter__()
        try:
            token_path=self.context.service_root/'token'
            self.token = secrets.token_urlsafe(32)
            write_private(token_path,self.token)
            app=self
            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self,*args):pass
                def setup(self):
                    super().setup();self.connection.settimeout(30)
                def reply(self,status,value):
                    data=json.dumps(value,default=str,allow_nan=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)))
                    if self.close_connection:
                        self.send_header('Connection', 'close')
                    self.end_headers();self.wfile.write(data)
                def check_origin(self):
                    expected=app.endpoint.removeprefix('http://')
                    if self.headers.get_all('Host')!=[expected] or self.headers.get('Origin') is not None:
                        raise AtlasError('invalid_request','unsupported Host or Origin',400)
                def authorized(self):
                    self.check_origin()
                    supplied=self.headers.get('Authorization','')
                    if not hmac.compare_digest(supplied,'Bearer '+app.token):
                        raise AtlasError('unauthenticated','local bearer token required',401)
                def handle_request(self):
                    try:
                        if self.command == 'GET' and self.path == '/v1/identity':
                            self.check_origin()
                            nonce = self.headers.get('Atlas-Challenge', '')
                            if not re.fullmatch(r'[0-9a-f]{64}', nonce):
                                raise AtlasError('invalid_request', 'invalid service challenge')
                            proof = hmac.digest(app.token.encode(), (app.endpoint + '/' + nonce).encode(), 'sha256').hex().encode()
                            self.send_response(200)
                            self.send_header('Content-Length', str(len(proof)))
                            self.end_headers()
                            self.wfile.write(proof)
                            return
                        self.close_connection = True
                        self.authorized()
                        if self.command=='GET' and self.path=='/v1/schema':return self.reply(200,schema())
                        if self.command=='GET' and self.path=='/v1/status':
                            return self.reply(200,{'version':__version__,'endpoint':app.endpoint})
                        if self.command=='GET' and self.path.startswith('/v1/operations/'):
                            return self.reply(200,asdict(inspect_operation(app.context,self.path[len('/v1/operations/'):])) )
                        if self.command!='POST':raise AtlasError('not_found','unknown route',404)
                        size=int(self.headers.get('Content-Length','0'))
                        if not 0<size<=16*1024*1024:raise AtlasError('invalid_request','JSON body must be 1 byte to 16 MiB')
                        body=json.loads(self.rfile.read(size))
                        if self.path=='/v1/stop':
                            app.begin_stop(wait=False)
                            self.reply(200,{'status':'stopping'})
                            threading.Thread(target=app.stop,daemon=True).start();return
                        if not self.path.startswith('/v1/actions/'):raise AtlasError('not_found','unknown route',404)
                        action=self.path[len('/v1/actions/'):]
                        request=parse_request(action,body)
                        from .actions import register_handlers
                        if request.action in register_handlers(app.context) or request.action=='operation.resume':
                            with app.admission:
                                if app.stopping:raise AtlasError('busy','service is stopping',409)
                                app.submissions += 1
                            try:
                                channel = self.headers.get('Atlas-Channel', 'http')
                                if channel not in ('http', 'mcp', 'viewer', 'python'):
                                    raise AtlasError('invalid_request', 'unknown attribution channel')
                                service = replace(app.context, configuration={
                                    **app.context.configuration, 'attribution_channel': channel})
                                status,result=invoke(service,request)
                            finally:
                                with app.admission:
                                    app.submissions -= 1
                                    app.admission.notify_all()
                        else:status,result=invoke(app.context,request)
                        self.reply(status,asdict(result))
                    except ProviderError:
                        self.reply(503, {'error': {'code': 'provider_failed',
                            'message': 'Configured provider failed; inspect private read evidence', 'status': 503}})
                    except AtlasError as error:self.reply(error.status,{'error':{'code':error.code,'message':str(error),'status':error.status}})
                    except (ValueError,TypeError,KeyError,UnicodeError) as error:self.reply(400,{'error':{'code':'invalid_request','message':str(error),'status':400}})
                    except Exception:self.reply(503,{'error':{'code':'service_unavailable','message':'inspect service evidence','status':503}})
                do_GET=handle_request
                do_POST=handle_request
            self.server=LocalHTTPServer(('127.0.0.1',0),Handler)
            self.endpoint='http://127.0.0.1:'+str(self.server.server_port)
            write_private(self.context.service_root/'discovery.json',json.dumps({'version':__version__,'endpoint':self.endpoint,'pid':os.getpid()}))
            self.dispatcher.start()
            self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
            self.thread.start()
            return self
        except BaseException:
            if self.server:
                self.server.server_close()
            (self.context.service_root / 'discovery.json').unlink(missing_ok=True)
            self.lock.__exit__()
            raise

    def begin_stop(self,*,wait=False):
        with self.admission:
            if self.stopping:return
            if self.submissions and not wait:
                raise AtlasError('busy', 'submissions are in progress', 409)
            self.stopping=True
            try:
                self.admission.wait_for(lambda: self.submissions == 0)
                self.dispatcher.stop(wait=wait)
            except BaseException:
                self.stopping=False
                raise

    def stop(self,*,wait=False):
        self.begin_stop(wait=wait)
        with self.shutdown_lock:
            if self.server:
                self.server.shutdown();self.server.server_close()
                if self.thread:self.thread.join()
                self.server=None
                (self.context.service_root / 'discovery.json').unlink(missing_ok=True)
            self.lock.__exit__()


def create_app(context):return LocalApp(context)
