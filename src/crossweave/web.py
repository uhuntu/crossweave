"""Loopback-only browser workspace over the existing session readers."""
import argparse
import codecs
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from . import chat, sessions, ui

ASSETS = Path(__file__).with_name('web_assets')


def list_sessions():
    return [dict(tool=row[0], id=row[1], age=row[2], cwd=row[4], title=row[5])
            for row, _ in ui.gather()]


def conversation(tool, sid):
    if tool not in sessions.ID_RESOLVERS or sid not in sessions.ID_RESOLVERS[tool](sid):
        raise ValueError('会话不存在')
    details = sessions.session_handoff_details(tool, sid)
    if details is None:
        raise ValueError('无法读取会话原文')
    cwd, markdown = details
    markdown = markdown.replace(f"# Crossweave handoff from {tool}",
                                f"# {tool} conversation", 1)
    messages = None
    if tool == 'zcode':
        messages = [dict(role=role, text=text)
                    for role, text in sessions.zcode_handoff_messages(sid)]
    return dict(tool=tool, id=sid, cwd=cwd, markdown=markdown,
                messages=messages, can_chat=chat.available(tool))


def stop_process(proc):
    if proc.poll() is None:
        try:
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait()
        except ProcessLookupError:
            pass


class Workspace(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        # SQLite readers cache connections: every read stays on one thread.
        self.reader = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.active = set()
        self.processes = set()
        self.closing = False

    def read(self, fn, *args):
        return self.reader.submit(fn, *args).result()

    def server_close(self):
        with self.lock:
            self.closing = True
            processes = list(self.processes)
        for proc in processes:
            stop_process(proc)
        super().server_close()
        self.reader.shutdown(wait=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # conversation IDs and prompts do not belong in access logs

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

    def headers_for(self, status, kind):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def send_bytes(self, body, kind, status=200):
        self.headers_for(status, kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(),
                        'application/json; charset=utf-8', status)

    def do_GET(self):
        if not self.valid_host():
            return self.reply(dict(error='Invalid host'), 403)
        url = urlsplit(self.path)
        try:
            if url.path in ('/', '/app.js', '/style.css'):
                name, kind = {'/': ('index.html', 'text/html'),
                              '/app.js': ('app.js', 'text/javascript'),
                              '/style.css': ('style.css', 'text/css')}[url.path]
                body = (ASSETS / name).read_bytes()
                if name == 'index.html':
                    body = body.replace(b'__CW_TOKEN__', self.server.token.encode())
                return self.send_bytes(body, kind + '; charset=utf-8')
            if url.path == '/api/sessions':
                return self.reply(self.server.read(list_sessions))
            if url.path == '/api/conversation':
                query = parse_qs(url.query)
                return self.reply(self.server.read(conversation, query.get('tool', [''])[0],
                                                   query.get('id', [''])[0]))
            self.reply(dict(error='Not found'), 404)
        except ValueError as exc:
            self.reply(dict(error=str(exc)), 404)
        except Exception:
            self.reply(dict(error='读取失败，请刷新重试'), 500)

    def do_POST(self):
        origin = self.headers.get('Origin')
        allowed = (f'http://127.0.0.1:{self.server.server_port}',
                   f'http://localhost:{self.server.server_port}')
        if (not self.valid_host() or origin not in allowed
                or not secrets.compare_digest(self.headers.get('X-CW-Token', ''), self.server.token)):
            return self.reply(dict(error='Invalid request origin or token'), 403)
        if self.path != '/api/chat':
            return self.reply(dict(error='Not found'), 404)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 128_000:
                raise ValueError('消息长度不合法')
            data = json.loads(self.rfile.read(size))
            tool = data.get('tool', 'zcode')
            sid, text = data.get('id'), data.get('text')
            if (not isinstance(tool, str) or tool not in chat.CHAT_TOOLS
                    or not isinstance(sid, str) or not isinstance(text, str) or not text.strip()):
                raise ValueError('请输入消息')
            info = self.server.read(conversation, tool, sid)
            if not info['can_chat']:
                raise ValueError(f'本机未安装 {tool} CLI')
            cwd = info['cwd']
            if not cwd or not os.path.isdir(cwd):
                raise ValueError('会话目录不可用')
        except (ValueError, AttributeError, TypeError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        key = (tool, sid)
        with self.server.lock:
            if self.server.closing or key in self.server.active:
                return self.reply(dict(error='该会话正在运行，请等待完成'), 409)
            self.server.active.add(key)
        proc = None
        try:
            with self.server.lock:
                if self.server.closing:
                    return self.reply(dict(error='服务正在关闭'), 503)
                proc = subprocess.Popen(sessions.spawnable(chat.turn_command(tool, sid, text)),
                                        cwd=cwd, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=os.name == 'posix')
                self.server.processes.add(proc)
            self.headers_for(200, 'application/x-ndjson; charset=utf-8')
            self.end_headers()
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                self.event(dict(output=decoder.decode(chunk)))
            tail = decoder.decode(b'', final=True)
            if tail:
                self.event(dict(output=tail))
            self.event(dict(done=True, code=proc.wait()))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as exc:
            if proc is None:
                self.reply(dict(error=f'无法启动 {tool}: {exc}'), 500)
        finally:
            if proc is not None:
                stop_process(proc)
                proc.stdout.close()
            with self.server.lock:
                self.server.active.discard(key)
                self.server.processes.discard(proc)

    def event(self, data):
        self.wfile.write((json.dumps(data, ensure_ascii=False) + '\n').encode())
        self.wfile.flush()


def cmd_web(args):
    parser = argparse.ArgumentParser(prog='cw web', description='本地会话工作台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    opts = parser.parse_args(args)
    if not 0 <= opts.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    try:
        server = Workspace(('127.0.0.1', opts.port))
    except OSError as exc:
        print(f'cw web: {exc}', file=sys.stderr)
        return 1
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Crossweave: {url}\nCtrl-C stops the local workspace.', flush=True)
    if not opts.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0
