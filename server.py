#!/usr/bin/env python3
"""Run: python3 server.py --port 8765 [--mode local|official|auto] [--json]."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

from dashboard import ROOT, DashboardError, build_summary, configuration


class SummaryCache:
    def __init__(self, config):
        self.config, self.value, self.updated = config, None, 0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.monotonic() - self.updated > 30:
                self.value = build_summary(self.config)
                self.updated = time.monotonic()
            return self.value


def handler_for(config):
    cache = SummaryCache(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CreditDashboard/1.0'

        def log_message(self, format, *args):
            pass  # Never log requests, headers, keys, or upstream response bodies.

        def send_data(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json_data(self, status, data):
            self.send_data(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode(),
                           'application/json; charset=utf-8')

        def do_GET(self):
            # Reject cross-origin browser requests and DNS rebinding hostnames.
            if self.headers.get('Host') not in ('127.0.0.1:%d' % config['port'], 'localhost:%d' % config['port']):
                self.json_data(403, {'error': 'Use the dashboard at 127.0.0.1.'})
                return
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.json_data(403, {'error': 'Cross-site requests are disabled.'})
                return
            path = urlsplit(self.path).path
            if path == '/api/health':
                self.json_data(200, {'app': 'openai-credit-dashboard'})
            elif path == '/api/summary':
                try:
                    self.json_data(200, cache.get())
                except DashboardError as exc:
                    self.json_data(502, {'error': str(exc)})
                except Exception:
                    self.json_data(500, {'error': 'Could not build summary; check configuration and local data.'})
            elif path in ('/', '/style.css', '/app.js'):
                name = 'index.html' if path == '/' else path[1:]
                mime = {'index.html': 'text/html', 'style.css': 'text/css', 'app.js': 'text/javascript'}[name]
                self.send_data(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
            else:
                self.json_data(404, {'error': 'Not found'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'config.json'))
    parser.add_argument('--mode', choices=['local', 'official', 'auto'])
    parser.add_argument('--port', type=int)
    parser.add_argument('--grant-total', type=float)
    parser.add_argument('--grant-start')
    parser.add_argument('--expiry')
    parser.add_argument('--sessions-dir')
    parser.add_argument('--log-file')
    parser.add_argument('--pricing-file')
    parser.add_argument('--json', action='store_true', help='Print summary and exit')
    args = parser.parse_args()
    try:
        config = configuration(args)
        if args.json:
            print(json.dumps(build_summary(config), ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        server = ThreadingHTTPServer(('127.0.0.1', config['port']), handler_for(config))
    except DashboardError as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}))
        else:
            print('Error: ' + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('Cannot bind 127.0.0.1; choose another --port or check the existing service.', file=sys.stderr)
        return 1
    print('OpenAI Credit Dashboard: http://127.0.0.1:%d/' % config['port'], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
