"""A single-atlas browser boundary; backend credentials never reach the browser."""
import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from markdown_it import MarkdownIt
from ..api_models import parse_request
from ..contracts import AtlasError

READS={'search','atlas.status','wiki.pages','wiki.read','question.ask','history','diff','evidence.read','candidate.inspect','operation.read'}
MUTATIONS={'knowledge.revise','candidate.revise','candidate.decide','type.decide'}
ASSETS=Path(__file__).with_name('static')


def render_markdown(content):
    renderer=MarkdownIt('commonmark',{'html':False}).enable('table').disable('image')
    # Presentation only: canonical Markdown and provenance remain unchanged.
    body=re.sub(r'\A---[ \t]*\r?\n(?=[A-Za-z_][\w-]*:).*?\r?\n---[ \t]*(?:\r?\n|\Z)', '', content, count=1, flags=re.S)
    return renderer.render(body)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=16

    def __init__(self,backend,atlas_id,port=0):
        self.backend=backend;self.atlas_id=atlas_id
        self.token=secrets.token_urlsafe(32)
        self.slots=threading.BoundedSemaphore(4)
        self.operation_ids=set()
        super().__init__(('127.0.0.1',port),Handler)
        self.origin='http://127.0.0.1:'+str(self.server_address[1])

    @property
    def launch_url(self):
        return self.origin+'/#'+self.token


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(5)

    def log_message(self,*_):
        pass  # No URLs, browser secrets or imported content in request logs.

    def reply(self,status,body,kind='application/json'):
        raw=json.dumps(body,ensure_ascii=False).encode() if kind=='application/json' else body
        self.send_response(status)
        for key,value in {'Content-Type':kind,'Content-Length':str(len(raw)),
            'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}.items():
            self.send_header(key,value)
        self.end_headers();self.wfile.write(raw)

    def boundary(self,authenticated=False):
        if self.headers.get('Host')!=self.server.origin.removeprefix('http://'):
            raise AtlasError('forbidden','Host must match the localhost viewer',403)
        origin=self.headers.get('Origin')
        if origin is not None and origin!=self.server.origin:
            raise AtlasError('forbidden','Foreign Origin rejected',403)
        if authenticated and not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+self.server.token):
            raise AtlasError('unauthorized','Use the launch URL printed by the viewer',401)

    def do_GET(self):
        try:
            self.boundary()
            names={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),
                   '/style.css':('style.css','text/css; charset=utf-8'),'/requests.mjs':('requests.mjs','text/javascript; charset=utf-8'),
                   '/graph.mjs':('graph.mjs','text/javascript; charset=utf-8'),
                   '/graph-model.mjs':('graph-model.mjs','text/javascript; charset=utf-8'),
                   '/vendor/cytoscape.esm.min.mjs':('vendor/cytoscape.esm.min.mjs','text/javascript; charset=utf-8')}
            if self.path not in names:raise AtlasError('not_found','Unknown page',404)
            name,kind=names[self.path]
            self.reply(200,(ASSETS/name).read_bytes(),kind)
        except AtlasError as error:self.reply(error.status,{'error':{'code':error.code,'message':str(error)}})

    def do_POST(self):
        acquired=False
        try:
            self.boundary(True)
            action=self.path.removeprefix('/api/')
            if self.path!='/api/'+action or action not in READS|MUTATIONS:
                raise AtlasError('not_found','Action not available in viewer',404)
            if self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                raise AtlasError('invalid_request','Bounded JSON body required',400)
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=1024*1024:raise AtlasError('invalid_request','Body exceeds viewer limit',413)
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict) or set(body)-{'payload','publication','request_id','expected_publication'}:
                raise AtlasError('invalid_request','Unexpected viewer request fields',400)
            if action in MUTATIONS and body.get('publication'):
                if body['publication']!=body.get('expected_publication'):
                    raise AtlasError('stale_publication','Historical views are read-only',409)
            request={**body,'atlas_id':self.server.atlas_id}
            parse_request(action,request)
            if action=='operation.read' and body['payload'].get('operation_id') not in self.server.operation_ids:
                raise AtlasError('operation_not_found','Operation does not belong to this viewer session',404)
            acquired=self.server.slots.acquire(blocking=False)
            if not acquired:raise AtlasError('busy','Viewer is processing other requests',409)
            result=self.server.backend.call(action,request)
            if isinstance(result,dict) and result.get('error'):
                error=result['error'];raise AtlasError(error['code'],error['message'],error.get('status',503))
            if action in MUTATIONS and result.get('operation_id'):
                self.server.operation_ids.add(result['operation_id'])
            if action=='atlas.status':
                self.server.operation_ids.update(row['operation_id'] for row in result.get('data',{}).get('active_operations',[]))
            if action=='wiki.read':
                page=result['data']['page'];page['html']=render_markdown(page['content'])
            self.reply(200,result)
        except AtlasError as error:self.reply(error.status,{'error':{'code':error.code,'message':str(error)}})
        except (ValueError,TypeError,KeyError) as error:self.reply(400,{'error':{'code':'invalid_request','message':str(error)}})
        except Exception as error:
            self.reply(503,{'error':{'code':'backend_unavailable','message':str(error)}})
        finally:
            if acquired:self.server.slots.release()
