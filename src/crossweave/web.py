"""Loopback-only browser workspace over the existing session readers."""
import argparse
import codecs
from concurrent.futures import ThreadPoolExecutor
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from . import chat, common, delete, exporter, llmwiki, rename, sessions, ui

ASSETS = Path(__file__).with_name('web_assets')
APP_PROFILE_DIR = os.path.join(common.HOME, '.cache', 'crossweave', 'gui-profile')
DESKTOP_FILE = os.path.join(common.HOME, '.local', 'share', 'applications', 'crossweave.desktop')
ICON_FILE = os.path.join(common.HOME, '.local', 'share', 'icons', 'hicolor', 'scalable', 'apps', 'crossweave.svg')

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="28" fill="#1f6956"/>
  <g stroke="#f8faf8" stroke-width="10" stroke-linecap="round">
    <line x1="64" y1="26" x2="64" y2="102"/>
    <line x1="26" y1="64" x2="102" y2="64"/>
    <line x1="37" y1="37" x2="91" y2="91"/>
    <line x1="91" y1="37" x2="37" y2="91"/>
  </g>
</svg>
"""


def _session_messages(tool, sid, record):
    if tool == 'claude':
        return sessions.claude_handoff_messages(record['path']) if record and record.get('path') else []
    if tool == 'codex':
        path = (record or {}).get('path') or sessions.codex_rollout_path(sid)
        return sessions.codex_handoff_messages(path) if path else []
    if tool in ('step', 'pi'):
        return sessions.step_handoff_messages(record['path']) if record and record.get('path') else []
    if tool == 'kimi':
        return sessions.kimi_handoff_messages(record['dir']) if record and record.get('dir') else []
    if tool == 'zcode':
        return sessions.zcode_handoff_messages(sid)
    if tool == 'opencode':
        return sessions.opencode_handoff_messages(sid)
    if tool == 'agy':
        return sessions.agy_handoff_messages(sid)
    return []


def list_sessions():
    return [dict(tool=row[0], id=row[1], age=row[2], cwd=row[4], title=row[5],
                 extra_home=rec.get('extra_home') or '')
            for row, rec in ui.gather()]


def conversation(tool, sid):
    if tool not in sessions.ID_RESOLVERS or sid not in sessions.ID_RESOLVERS[tool](sid):
        raise ValueError('会话不存在')
    record = sessions._find_record(tool, sid)
    details = sessions.session_handoff_details(tool, sid)
    if details is None:
        raise ValueError('无法读取会话原文')
    cwd, markdown = details
    markdown = markdown.replace(f"# Crossweave handoff from {tool}",
                                f"# {tool} conversation", 1)
    messages = [dict(role=role, text=text)
                for role, text in _session_messages(tool, sid, record)]
    extra_home = (record or {}).get('extra_home') or ''
    return dict(
        tool=tool, id=sid, cwd=cwd, markdown=markdown,
        messages=messages,
        extra_home=extra_home,
        can_chat=(not extra_home) and chat.available(tool),
        can_rename=(not extra_home) and (tool in rename._RENAMABLE),
        can_delete=tool in sessions.TOOLS,
    )


def rename_session(tool, sid, title):
    if tool not in sessions.TOOLS:
        raise ValueError('不支持的工具')
    title = ' '.join((title or '').split()).strip()
    if not title:
        raise ValueError('请输入新标题')
    record = sessions._find_record(tool, sid)
    if record is None:
        raise ValueError('会话不存在')
    if record.get('extra_home'):
        raise ValueError('外部目录会话为只读，暂不支持重命名')
    if tool not in rename._RENAMABLE:
        raise ValueError(f'{tool} 暂不支持重命名')
    if not rename._apply(record, title):
        raise ValueError('重命名失败')
    return dict(ok=True, title=title)


def delete_session(tool, sid):
    if tool not in sessions.TOOLS:
        raise ValueError('不支持的工具')
    record = sessions._find_record(tool, sid)
    if record is None:
        raise ValueError('会话不存在')
    backup = delete.delete_record(record)
    return dict(ok=True, backup=backup)


_WIKILINK_RE = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')


def _wiki_client():
    api_url = os.environ.get(llmwiki.API_ENV, '').strip() or exporter.API_URL
    token = os.environ.get(exporter.TOKEN_ENV, '')
    return exporter.WikiClient(api_url, token)


def wiki_status():
    project = llmwiki.wiki_dir()
    if project is None:
        return dict(configured=False, path='', name='', online=False, pages=0)
    wiki_root = project / 'wiki'
    pages = 0
    try:
        for p in wiki_root.rglob('*.md'):
            rel = p.relative_to(wiki_root).as_posix()
            if any(part in llmwiki.SKIP_DIRS for part in rel.split('/')):
                continue
            pages += 1
    except OSError:
        pass
    online = False
    try:
        client = _wiki_client()
        client.request('GET', '/health', timeout=1.5)
        online = True
    except Exception:
        online = False
    return dict(configured=True, path=str(project), name=project.name, online=online, pages=pages)


def list_wiki_pages():
    project = llmwiki.wiki_dir()
    if project is None:
        return []
    wiki_root = project / 'wiki'
    items = []
    try:
        paths = sorted(wiki_root.rglob('*.md'))
    except OSError:
        return []
    for p in paths:
        if not p.is_file():
            continue
        rel = p.relative_to(wiki_root).as_posix()
        parts = rel.split('/')
        if any(part in llmwiki.SKIP_DIRS for part in parts):
            continue
        try:
            st = p.stat()
            head = p.read_bytes()[:4096].decode('utf-8', errors='replace')
        except OSError:
            continue
        title = llmwiki._title(head, p.stem)
        category = parts[0] if len(parts) > 1 else ''
        items.append(dict(rel=rel, stem=p.stem, title=title, category=category, mtime=int(st.st_mtime)))
    items.sort(key=lambda x: (0 if x['rel'] in ('index.md', 'overview.md') else 1, -x['mtime'], x['rel']))
    return items


def read_wiki_page(rel):
    project = llmwiki.wiki_dir()
    if project is None:
        raise ValueError('未配置 CW_LLM_WIKI')
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError('页面路径为空')
    wiki_root = (project / 'wiki').resolve()
    target = (wiki_root / rel).resolve()
    try:
        rel_clean = target.relative_to(wiki_root).as_posix()
    except ValueError:
        raise ValueError('非法页面路径')
    if not target.is_file():
        raise ValueError('页面不存在')
    raw = target.read_bytes()[:llmwiki.READ_BYTES].decode('utf-8', errors='replace')
    fm_title, body = llmwiki._split(raw)
    title = fm_title or llmwiki._title(raw, target.stem)
    links = []
    seen = set()
    for m in _WIKILINK_RE.finditer(body):
        t = m.group(1).strip()
        if t and t not in seen:
            seen.add(t)
            links.append(dict(target=t, label=(m.group(2) or t).strip()))
    return dict(rel=rel_clean, stem=target.stem, title=title, body=body, markdown=raw,
                path=str(target), links=links)


def export_to_wiki(tool, sid):
    project = llmwiki.wiki_dir()
    if project is None:
        raise ValueError('未配置 CW_LLM_WIKI 环境变量')
    if tool not in sessions.TOOLS:
        raise ValueError('不支持的工具')
    record = sessions._find_record(tool, sid)
    if record is None:
        raise ValueError('会话不存在')
    target, written = exporter.snapshot(project, tool, sid, record=record)
    rescanned = False
    note = ''
    try:
        _wiki_client().rescan(project)
        rescanned = True
    except exporter.ExportError as exc:
        note = str(exc)
    return dict(ok=True, path=str(target), written=written, rescanned=rescanned, note=note)


def rescan_wiki():
    project = llmwiki.wiki_dir()
    if project is None:
        raise ValueError('未配置 CW_LLM_WIKI 环境变量')
    _wiki_client().rescan(project)
    return dict(ok=True)


def wiki_graph():
    project = llmwiki.wiki_dir()
    if project is None:
        return dict(nodes=[], edges=[])
    wiki_root = project / 'wiki'
    pages = []
    by_key = {}
    try:
        paths = sorted(wiki_root.rglob('*.md'))
    except OSError:
        return dict(nodes=[], edges=[])
    for p in paths:
        if not p.is_file():
            continue
        rel = p.relative_to(wiki_root).as_posix()
        parts = rel.split('/')
        if any(part in llmwiki.SKIP_DIRS for part in parts):
            continue
        try:
            raw = p.read_bytes()[:llmwiki.READ_BYTES].decode('utf-8', errors='replace')
        except OSError:
            continue
        fm_title, body = llmwiki._split(raw)
        title = fm_title or llmwiki._title(raw, p.stem)
        category = parts[0] if len(parts) > 1 else ''
        pages.append((rel, p.stem, title, category, body))
        for k in (rel.lower(), rel[:-3].lower() if rel.lower().endswith('.md') else rel.lower(),
                  p.stem.lower(), title.lower()):
            by_key.setdefault(k, rel)
    edge_set = set()
    degree = {rel: 0 for rel, *_ in pages}
    for rel, _stem, _title, _cat, body in pages:
        for m in _WIKILINK_RE.finditer(body):
            raw_t = m.group(1).strip().lower()
            if raw_t.endswith('.md'):
                raw_t = raw_t[:-3]
            dst = by_key.get(raw_t)
            if not dst or dst == rel:
                continue
            pair = (rel, dst) if rel < dst else (dst, rel)
            if pair not in edge_set:
                edge_set.add(pair)
                degree[rel] += 1
                degree[dst] += 1
    nodes = [dict(id=rel, stem=stem, title=title, category=cat, degree=degree[rel])
             for rel, stem, title, cat, _ in pages]
    edges = [dict(source=a, target=b) for a, b in sorted(edge_set)]
    return dict(nodes=nodes, edges=edges)


def _find_app_browser():
    names = (
        'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
        'microsoft-edge', 'microsoft-edge-stable', 'brave-browser',
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    extra = []
    if sys.platform == 'darwin':
        extra = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
            '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
        ]
    elif os.name == 'nt':
        roots = [os.environ.get('PROGRAMFILES', ''), os.environ.get('PROGRAMFILES(X86)', ''),
                 os.environ.get('LOCALAPPDATA', '')]
        rels = (
            os.path.join('Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join('Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
        )
        extra = [os.path.join(r, rel) for r in roots if r for rel in rels]
    for path in extra:
        if os.path.isfile(path):
            return path
    return None


def open_app_window(url):
    browser = _find_app_browser()
    if browser:
        argv = [
            browser,
            f'--app={url}',
            f'--user-data-dir={APP_PROFILE_DIR}',
            '--window-size=1280,840',
            '--disable-features=Translate',
            '--no-first-run',
            '--no-default-browser-check',
        ]
        if os.name == 'posix':
            argv.append('--class=crossweave')
        try:
            os.makedirs(APP_PROFILE_DIR, exist_ok=True)
            return subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == 'posix',
            )
        except OSError:
            pass
    webbrowser.open(url)
    return None


def _cw_exec_prefix():
    found = shutil.which('cw')
    if found:
        return [found]
    local_bin = os.path.join(common.HOME, '.local', 'bin', 'cw')
    if os.path.isfile(local_bin):
        return [local_bin]
    return [sys.executable, '-m', 'crossweave.cli']


_DESKTOP_ENV_VARS = (
    'PATH',
    'CW_LLM_WIKI',
    'CW_LLM_WIKI_API',
    'LLM_WIKI_API_TOKEN',
    'CW_VAULT',
    'CROSSWEAVE_EXTRA_HOMES',
)


def _desktop_quote(arg):
    """Quote an Exec= argument per the FreeDesktop Desktop Entry spec
    (double quotes, not POSIX single quotes)."""
    if not arg or any(ch in arg for ch in ' \t\n"\'\\><~|&;$*?#()`'):
        escaped = arg.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
        return f'"{escaped}"'
    return arg


def install_desktop(home=None):
    if os.name != 'posix' or sys.platform == 'darwin':
        raise ValueError('.desktop 仅适用于 Linux 桌面环境')
    os.makedirs(os.path.dirname(ICON_FILE), exist_ok=True)
    with open(ICON_FILE, 'w', encoding='utf-8') as fh:
        fh.write(_ICON_SVG)
    env_pairs = [f'{k}={os.environ[k]}' for k in _DESKTOP_ENV_VARS if os.environ.get(k)]
    cmd = (['env', *env_pairs] if env_pairs else []) + [*_cw_exec_prefix(), 'gui']
    if home:
        cmd.extend(['--home', home])
    exec_line = ' '.join(_desktop_quote(p) for p in cmd)
    desktop = (
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Name=CrossWeave\n'
        'Comment=Local AI coding-agent session workspace\n'
        f'Exec={exec_line}\n'
        f'Icon={ICON_FILE}\n'
        'Terminal=false\n'
        'Categories=Development;Utility;\n'
        'StartupWMClass=crossweave\n'
    )
    os.makedirs(os.path.dirname(DESKTOP_FILE), exist_ok=True)
    with open(DESKTOP_FILE, 'w', encoding='utf-8') as fh:
        fh.write(desktop)
    updater = shutil.which('update-desktop-database')
    if updater:
        try:
            subprocess.run([updater, os.path.dirname(DESKTOP_FILE)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
    return DESKTOP_FILE, ICON_FILE


def uninstall_desktop():
    removed = []
    for path in (DESKTOP_FILE, ICON_FILE):
        if os.path.exists(path):
            os.unlink(path)
            removed.append(path)
    return removed


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

    def __init__(self, address, initial_home=''):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.initial_home = initial_home or ''
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
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

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
            if url.path == '/favicon.svg':
                return self.send_bytes(_ICON_SVG.encode('utf-8'), 'image/svg+xml; charset=utf-8')
            if url.path in ('/', '/app.js', '/style.css'):
                name, kind = {'/': ('index.html', 'text/html'),
                              '/app.js': ('app.js', 'text/javascript'),
                              '/style.css': ('style.css', 'text/css')}[url.path]
                body = (ASSETS / name).read_bytes()
                if name == 'index.html':
                    body = body.replace(b'__CW_TOKEN__', self.server.token.encode())
                    body = body.replace(b'__CW_HOME__', html.escape(self.server.initial_home, quote=True).encode())
                return self.send_bytes(body, kind + '; charset=utf-8')
            if url.path == '/api/sessions':
                return self.reply(self.server.read(list_sessions))
            if url.path == '/api/conversation':
                query = parse_qs(url.query)
                return self.reply(self.server.read(conversation, query.get('tool', [''])[0],
                                                   query.get('id', [''])[0]))
            if url.path == '/api/wiki/status':
                return self.reply(self.server.read(wiki_status))
            if url.path == '/api/wiki/pages':
                return self.reply(self.server.read(list_wiki_pages))
            if url.path == '/api/wiki/page':
                query = parse_qs(url.query)
                return self.reply(self.server.read(read_wiki_page, query.get('rel', [''])[0]))
            if url.path == '/api/wiki/graph':
                return self.reply(self.server.read(wiki_graph))
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
        if self.path == '/api/rename':
            return self._handle_rename()
        if self.path == '/api/delete':
            return self._handle_delete()
        if self.path == '/api/export-wiki':
            return self._handle_export_wiki()
        if self.path == '/api/wiki/rescan':
            return self._handle_wiki_rescan()
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

    def _read_json_body(self, max_size=16_000):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= max_size:
            raise ValueError('请求体长度不合法')
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict):
            raise ValueError('请求格式不合法')
        return data

    def _handle_rename(self):
        try:
            data = self._read_json_body()
            tool, sid, title = data.get('tool'), data.get('id'), data.get('title')
            if not isinstance(tool, str) or not isinstance(sid, str) or not isinstance(title, str):
                raise ValueError('参数不完整')
        except (ValueError, TypeError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        with self.server.lock:
            if (tool, sid) in self.server.active:
                return self.reply(dict(error='该会话正在运行，请等待完成后再重命名'), 409)
        try:
            result = self.server.read(rename_session, tool, sid, title)
            return self.reply(result)
        except ValueError as exc:
            return self.reply(dict(error=str(exc)), 400)
        except Exception as exc:
            return self.reply(dict(error=f'重命名失败: {exc}'), 500)

    def _handle_delete(self):
        try:
            data = self._read_json_body()
            tool, sid = data.get('tool'), data.get('id')
            if not isinstance(tool, str) or not isinstance(sid, str):
                raise ValueError('参数不完整')
        except (ValueError, TypeError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        with self.server.lock:
            if (tool, sid) in self.server.active:
                return self.reply(dict(error='该会话正在运行，请等待完成后再删除'), 409)
        try:
            result = self.server.read(delete_session, tool, sid)
            return self.reply(result)
        except ValueError as exc:
            return self.reply(dict(error=str(exc)), 400)
        except Exception as exc:
            return self.reply(dict(error=f'删除失败: {exc}'), 500)

    def _handle_export_wiki(self):
        try:
            data = self._read_json_body()
            tool, sid = data.get('tool'), data.get('id')
            if not isinstance(tool, str) or not isinstance(sid, str):
                raise ValueError('参数不完整')
        except (ValueError, TypeError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        try:
            result = self.server.read(export_to_wiki, tool, sid)
            return self.reply(result)
        except (ValueError, exporter.ExportError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        except Exception as exc:
            return self.reply(dict(error=f'导出失败: {exc}'), 500)

    def _handle_wiki_rescan(self):
        try:
            result = self.server.read(rescan_wiki)
            return self.reply(result)
        except (ValueError, exporter.ExportError) as exc:
            return self.reply(dict(error=str(exc)), 400)
        except Exception as exc:
            return self.reply(dict(error=f'刷新失败: {exc}'), 500)

    def event(self, data):
        self.wfile.write((json.dumps(data, ensure_ascii=False) + '\n').encode())
        self.wfile.flush()


def cmd_web(args):
    parser = argparse.ArgumentParser(prog='cw web', description='本地会话工作台')
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--app', action='store_true', help='以独立应用窗口模式打开')
    parser.add_argument('--home', default=None, help='临时包含并筛选指定目录（如 /mnt/win）')
    parser.add_argument('--install-desktop', action='store_true', help='安装 Linux .desktop 启动图标')
    parser.add_argument('--uninstall-desktop', action='store_true', help='卸载 Linux .desktop 启动图标')
    opts = parser.parse_args(args)
    if opts.install_desktop:
        try:
            desktop_path, icon_path = install_desktop(home=opts.home)
        except (OSError, ValueError) as exc:
            print(f'cw gui: {exc}', file=sys.stderr)
            return 1
        print(f'Installed desktop entry: {desktop_path}\nIcon: {icon_path}')
        return 0
    if opts.uninstall_desktop:
        removed = uninstall_desktop()
        if removed:
            for p in removed:
                print(f'Removed: {p}')
        else:
            print('No desktop entry installed.')
        return 0
    if opts.port is not None and not 0 <= opts.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    with sessions._with_extra_home(opts.home):
        port = 8765 if opts.port is None else opts.port
        try:
            server = Workspace(('127.0.0.1', port), initial_home=opts.home or '')
        except OSError as exc:
            if opts.port is None:
                # Default port is busy (e.g. another cw web/gui window is open);
                # pick an ephemeral free port so launching from .desktop never fails.
                try:
                    server = Workspace(('127.0.0.1', 0), initial_home=opts.home or '')
                except OSError as exc2:
                    print(f'cw web: {exc2}', file=sys.stderr)
                    return 1
            else:
                print(f'cw web: {exc}', file=sys.stderr)
                return 1
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'CrossWeave: {url}\nCtrl-C stops the local workspace.', flush=True)
        app_proc = None
        if not opts.no_open:
            if opts.app:
                app_proc = open_app_window(url)
            else:
                webbrowser.open(url)
        try:
            if app_proc is not None:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                started = time.monotonic()
                app_proc.wait()
                # If the browser handed off to an already-running profile and
                # exited immediately, stay alive until Ctrl-C rather than
                # killing the freshly opened tab.
                if time.monotonic() - started < 1.5:
                    thread.join()
                else:
                    server.shutdown()
                    thread.join()
            else:
                server.serve_forever()
        except KeyboardInterrupt:
            print()
            server.shutdown()
        finally:
            server.server_close()
    return 0
