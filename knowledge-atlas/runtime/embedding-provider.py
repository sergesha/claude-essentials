#!/usr/bin/env python3
"""Local HTTP bridge for native VectorLink's GenericHttp embedding interface."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
from knowledge_atlas.core.embedding_provider import EmbeddingProvider

p = argparse.ArgumentParser()
p.add_argument('--profile', type=Path, required=True)
p.add_argument('--cache', type=Path, required=True)
p.add_argument('--endpoint', required=True)
p.add_argument('--port', type=int, default=18099)
args = p.parse_args()
profile = json.loads(args.profile.read_text())
provider = None


def get_provider():
    global provider
    if provider is None:
        provider = EmbeddingProvider(args.profile, args.cache, args.endpoint)
    return provider


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/stats':
            self.reply(200, provider.stats if provider else {'inference_calls':0,'cache_hits':0,'inference_seconds':0.0})
        elif self.path == '/v1/models':
            self.reply(200, {'data': [{'id': profile['model_namespace']}]})
        else:
            self.reply(404, {'error': 'unknown route'})

    def do_POST(self):
        if self.path != '/v1/embeddings':
            self.reply(404, {'error': 'unknown route'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.reply(200, get_provider().embed(body))
        except (ValueError, KeyError, TypeError) as error:
            self.reply(422, {'error': str(error)})
        except Exception as error:
            self.reply(502, {'error': str(error)})


HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
