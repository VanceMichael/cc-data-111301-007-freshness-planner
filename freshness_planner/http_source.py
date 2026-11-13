from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


@contextmanager
def source_server(directory):
    root = Path(directory)
    routes = json.loads((root / 'routes.json').read_text(encoding='utf-8'))
    state = {'revision': 0, 'requests': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['requests'].append(self.path)
            route = routes.get(self.path)
            if route is None:
                self.send_error(404)
                return
            versions = route if isinstance(route, list) else [route]
            item = versions[min(state['revision'], len(versions)-1)]
            status = item.get('status', 200)
            body = (root / item['file']).read_bytes() if 'file' in item else b''
            if self.headers.get('If-None-Match') == item.get('etag') and item.get('etag'):
                status, body = 304, b''
            self.send_response(status)
            self.send_header('Content-Type', item.get('type', 'application/json; charset=utf-8'))
            if item.get('etag'):
                self.send_header('ETag', item['etag'])
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            self.rfile.read(length)
            if self.path != '/control/advance':
                self.send_error(404)
                return
            state['revision'] += 1
            body = json.dumps({'revision': state['revision']}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    worker.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
