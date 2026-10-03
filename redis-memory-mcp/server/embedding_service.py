"""One shared CPU Gemma Q4 model; no model weights in MCP processes."""
import hashlib
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
from embedding import MODEL, REVISION, TOKENIZER_SHA256, endpoint_info

FILES = {
    'tokenizer.json': TOKENIZER_SHA256,
    'onnx/model_q4.onnx': 'ad1dfee81a70f7944b9b9d1cc6e48075b832881cf33fab2f2b248be78f3f0043',
    'onnx/model_q4.onnx_data': '599962c3143b040de2dd05e5975be3e9091dd067cacc6a8f7186e3203bab9e02',
}

class Runtime:
    def __init__(self):
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        import onnxruntime as ort
        paths = {}
        for name, digest in FILES.items():
            directory = os.getenv('EMBED_MODEL_PATH')
            path = Path(directory) / name if directory else Path(hf_hub_download(MODEL, name, revision=REVISION))
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('Embedding model artifact checksum mismatch')
            paths[name] = path
        self.tokenizer = Tokenizer.from_file(str(paths['tokenizer.json']))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(str(paths['onnx/model_q4.onnx']), sess_options=options,
                                           providers=['CPUExecutionProvider'])

    def encode(self, inputs):
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 4 or any(not isinstance(s, str) for s in inputs):
            raise ValueError('inputs must contain 1-4 strings')
        encoded = [self.tokenizer.encode(s).ids for s in inputs]
        if any(not ids or len(ids) > 2048 for ids in encoded):
            raise ValueError('Input exceeds the 2048-token model limit; truncation is disabled')
        result = []
        for ids in encoded:
            array = np.asarray([ids], dtype=np.int64)
            vector = self.session.run(['sentence_embedding'], {
                'input_ids': array, 'attention_mask': np.ones_like(array),
            })[0][0]
            norm = np.linalg.norm(vector)
            if vector.shape != (768,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm == 0:
                raise ValueError('Invalid embedding output')
            result.append((vector / norm).tolist())
        return result


def main():
    runtime = Runtime()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            data = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/info':self.reply(200, endpoint_info())
            elif self.path == '/health':self.reply(200, {'status': 'ok'})
            else:self.reply(404, {'error': 'Not found'})

        def do_POST(self):
            if self.path != '/embed':
                self.reply(404, {'error': 'Not found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('Request must be 1 byte to 1 MiB')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or body.get('truncate', False):
                    raise ValueError('Truncation is not supported')
                self.reply(200, runtime.encode(body.get('inputs')))
            except (ValueError, TypeError, UnicodeError):
                self.reply(400, {'error': 'Invalid input: use 1-4 strings, at most 2048 tokens each; no truncation'})
            except Exception:
                self.reply(500, {'error': 'Embedding failed'})

        def log_message(self, *_):
            pass  # Never log documents, queries or caller-supplied paths.

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

    # Serialize requests and inference to bound peak memory across all clients.
    HTTPServer((os.getenv('EMBED_HOST', '127.0.0.1'), int(os.getenv('EMBED_PORT', '8081'))), Handler).serve_forever()

if __name__ == '__main__':
    main()
