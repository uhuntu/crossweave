"""Cross-platform system tray host for the local CrossWeave workspace."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from . import common, sessions, web


def _import_tray_deps():
    """Load optional pystray and Pillow dependencies on demand."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError(
            'cw tray requires optional dependencies (pystray, Pillow). '
            'Install them with: pip install "crossweave[tray]"'
        ) from exc
    return pystray, Image, ImageDraw


def _make_icon_image(image_cls, draw_cls, size=64):
    """Render the CrossWeave mark (matching web._ICON_SVG) as an RGBA icon."""
    scale = 2
    canvas = size * scale
    img = image_cls.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    draw = draw_cls.Draw(img)

    radius = round(28 * canvas / 128)
    draw.rounded_rectangle([(0, 0), (canvas - 1, canvas - 1)], radius=radius, fill='#1f6956')

    stroke = max(2, round(10 * canvas / 128))
    cap = stroke / 2

    def pt(val):
        return val * canvas / 128

    lines = (
        ((64, 26), (64, 102)),
        ((26, 64), (102, 64)),
        ((37, 37), (91, 91)),
        ((91, 37), (37, 91)),
    )
    for (x1, y1), (x2, y2) in lines:
        p1 = (pt(x1), pt(y1))
        p2 = (pt(x2), pt(y2))
        draw.line([p1, p2], fill='#f8faf8', width=stroke)
        for px, py in (p1, p2):
            draw.ellipse([px - cap, py - cap, px + cap, py + cap], fill='#f8faf8')

    resample = getattr(image_cls, 'Resampling', image_cls).LANCZOS
    return img.resize((size, size), resample)


class TrayController:
    """Manage the background Workspace HTTP server and tray actions."""

    def __init__(self, port=None, home=None, app_mode=True):
        self.requested_port = port
        self.home = home or ''
        self.app_mode = app_mode
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._icon = None
        self._dirs_cache = []
        self._dirs_cache_at = 0.0

    @property
    def url(self):
        with self._lock:
            server = self._server
        if server is None:
            return ''
        return f'http://127.0.0.1:{server.server_port}'

    def attach_icon(self, icon):
        self._icon = icon
        self._sync_icon_title()

    def _sync_icon_title(self):
        if self._icon is None:
            return
        url = self.url
        self._icon.title = f'CrossWeave ({url})' if url else 'CrossWeave (已停止)'

    def start_server(self):
        """Start the local Workspace server in a daemon thread and return its URL."""
        web._load_gui_env()
        with self._lock:
            if self._server is not None:
                return f'http://127.0.0.1:{self._server.server_port}'
            port = 8765 if self.requested_port is None else self.requested_port
            try:
                server = web.Workspace(('127.0.0.1', port), initial_home=self.home)
            except OSError:
                if self.requested_port is not None:
                    raise
                server = web.Workspace(('127.0.0.1', 0), initial_home=self.home)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._server = server
            self._thread = thread
        self._sync_icon_title()
        return self.url

    def stop_server(self):
        """Shut down the running Workspace server if active."""
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._sync_icon_title()

    def restart_server(self):
        """Restart the Workspace server and update the tray tooltip."""
        self.stop_server()
        return self.start_server()

    def open_workspace(self, app_mode=None):
        """Open the workspace in app-window mode or the default browser."""
        url = self.url or self.start_server()
        use_app = self.app_mode if app_mode is None else app_mode
        if use_app:
            web.open_app_window(url)
        else:
            webbrowser.open(url)

    def quit(self):
        """Stop the Workspace server and exit the tray event loop."""
        self.stop_server()
        if self._icon is not None:
            self._icon.stop()

    def recent_dirs(self, limit=6, ttl=30.0):
        """Return recently used existing directories, cached briefly for responsive menus."""
        now = time.monotonic()
        if self._dirs_cache and (now - self._dirs_cache_at) < ttl:
            return list(self._dirs_cache)
        with self._lock:
            server = self._server
        dirs = server.read(web.recent_directories, limit) if server is not None else web.recent_directories(limit)
        self._dirs_cache = list(dirs)
        self._dirs_cache_at = now
        return list(dirs)

    def launch_agent(self, tool, cwd=None):
        """Launch `cw <tool>` in an interactive terminal."""
        web.launch_agent_terminal(tool, cwd=cwd)


def _dir_label(path):
    name = os.path.basename(path.rstrip('\\/'))
    return f'{name} ({path})' if name and name != path else path


def _make_launch_cb(controller, tool, cwd):
    return lambda _icon, _item: controller.launch_agent(tool, cwd)


def _launch_menu_items(pystray, controller):
    item = pystray.MenuItem
    installed = [t['tool'] for t in web.available_tools() if t['installed']]
    tools = installed or list(web.LAUNCH_TOOLS)
    dirs = controller.recent_dirs()
    entries = []
    for tool in tools:
        if dirs:
            sub_items = [item(_dir_label(d), _make_launch_cb(controller, tool, d)) for d in dirs]
            sub_items.append(pystray.Menu.SEPARATOR)
            sub_items.append(item('主目录 (~)', _make_launch_cb(controller, tool, None)))
            entries.append(item(tool, pystray.Menu(*sub_items)))
        else:
            entries.append(item(tool, _make_launch_cb(controller, tool, None)))
    return entries


def _build_menu(pystray, controller):
    item = pystray.MenuItem
    return pystray.Menu(
        item('打开工作台', lambda _icon, _item: controller.open_workspace(True), default=True),
        item('在浏览器中打开', lambda _icon, _item: controller.open_workspace(False)),
        item('快速启动 Agent', pystray.Menu(lambda: _launch_menu_items(pystray, controller))),
        pystray.Menu.SEPARATOR,
        item(lambda _item: f'服务地址: {controller.url or "已停止"}', None, enabled=False),
        item('重启服务', lambda _icon, _item: controller.restart_server()),
        pystray.Menu.SEPARATOR,
        item('退出', lambda _icon, _item: controller.quit()),
    )


def _tray_target():
    """Return (target_path, arguments) for launching the tray without a console."""
    exe_dir = Path(sys.executable).parent
    for candidate in (exe_dir / 'cw-tray.exe', exe_dir / 'cw-tray'):
        if candidate.is_file():
            return str(candidate), ''
    found = shutil.which('cw-tray')
    if found:
        return found, ''
    pythonw = exe_dir / 'pythonw.exe'
    target = str(pythonw if pythonw.is_file() else sys.executable)
    return target, '-m crossweave.tray'


def _shortcut_paths():
    appdata = os.environ.get('APPDATA', '').strip()
    if not appdata:
        raise ValueError('APPDATA environment variable is not set')
    programs = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
    return {
        'start_menu': programs / 'CrossWeave.lnk',
        'startup': programs / 'Startup' / 'CrossWeave.lnk',
        'icon': Path(common.HOME) / '.local' / 'share' / 'crossweave' / 'crossweave.ico',
    }


def _write_windows_shortcut(link_path, target_path, arguments, icon_path):
    link_path.parent.mkdir(parents=True, exist_ok=True)
    ps_script = (
        '$ws = New-Object -ComObject WScript.Shell; '
        '$sc = $ws.CreateShortcut($env:CW_LNK_PATH); '
        '$sc.TargetPath = $env:CW_LNK_TARGET; '
        'if ($env:CW_LNK_ARGS) { $sc.Arguments = $env:CW_LNK_ARGS }; '
        'if ($env:CW_LNK_ICON) { $sc.IconLocation = $env:CW_LNK_ICON }; '
        '$sc.Description = "CrossWeave"; '
        '$sc.Save()'
    )
    env = os.environ.copy()
    env['CW_LNK_PATH'] = str(link_path)
    env['CW_LNK_TARGET'] = target_path
    env['CW_LNK_ARGS'] = arguments
    env['CW_LNK_ICON'] = str(icon_path)
    subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps_script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        **common.no_window_kwargs(),
    )


def install_shortcut(autostart=False, home=None):
    """Install a Windows Start Menu shortcut (and optional Startup entry) for cw-tray."""
    if os.name != 'nt':
        raise ValueError('Windows Start Menu shortcuts are only supported on Windows (on Linux use `cw web --install-desktop`)')
    _, image_cls, draw_cls = _import_tray_deps()
    paths = _shortcut_paths()
    paths['icon'].parent.mkdir(parents=True, exist_ok=True)
    icon_img = _make_icon_image(image_cls, draw_cls, size=128)
    icon_img.save(
        str(paths['icon']),
        format='ICO',
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)],
    )
    target, args = _tray_target()
    if home:
        quoted = f'"{home}"' if ' ' in home else home
        args = f'{args} --home {quoted}'.strip()
    installed = [paths['start_menu']]
    _write_windows_shortcut(paths['start_menu'], target, args, paths['icon'])
    if autostart:
        _write_windows_shortcut(paths['startup'], target, args, paths['icon'])
        installed.append(paths['startup'])
    return installed, paths['icon']


def uninstall_shortcut():
    """Remove installed Windows Start Menu / Startup shortcuts and icon."""
    if os.name != 'nt':
        return []
    paths = _shortcut_paths()
    removed = []
    for path in (paths['start_menu'], paths['startup'], paths['icon']):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def _parse_args(args):
    parser = argparse.ArgumentParser(prog='cw tray', description='CrossWeave 系统托盘常驻工作台')
    parser.add_argument('--port', type=int, default=None, help='指定本地服务端口（默认 8765，冲突时自动分配）')
    parser.add_argument('--home', default=None, help='临时包含并筛选指定目录（如 /mnt/win）')
    parser.add_argument('--open', action='store_true', help='启动托盘后立即打开工作台')
    parser.add_argument('--browser', action='store_true', help='默认使用系统浏览器而非独立应用窗口打开')
    parser.add_argument('--install-shortcut', action='store_true', help='安装 Windows 开始菜单快捷方式')
    parser.add_argument('--uninstall-shortcut', action='store_true', help='卸载 Windows 开始菜单快捷方式')
    parser.add_argument('--autostart', action='store_true', help='配合 --install-shortcut 同时添加到开机自启')
    opts = parser.parse_args(args)
    if opts.port is not None and not 0 <= opts.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    return opts


def cmd_tray(args=None):
    """Run CrossWeave in the system tray with a persistent local Workspace server."""
    opts = _parse_args(sys.argv[1:] if args is None else args)
    if opts.install_shortcut:
        try:
            installed, icon_path = install_shortcut(autostart=opts.autostart, home=opts.home)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f'cw tray: {exc}', file=sys.stderr)
            return 1
        for path in installed:
            print(f'Installed shortcut: {path}')
        print(f'Icon: {icon_path}')
        return 0
    if opts.uninstall_shortcut:
        try:
            removed = uninstall_shortcut()
        except (OSError, ValueError) as exc:
            print(f'cw tray: {exc}', file=sys.stderr)
            return 1
        if removed:
            for path in removed:
                print(f'Removed: {path}')
        else:
            print('No shortcut installed.')
        return 0
    try:
        pystray, image_cls, draw_cls = _import_tray_deps()
    except RuntimeError as exc:
        print(f'cw tray: {exc}', file=sys.stderr)
        return 1

    controller = TrayController(port=opts.port, home=opts.home, app_mode=not opts.browser)
    with sessions._with_extra_home(opts.home):
        try:
            url = controller.start_server()
        except OSError as exc:
            print(f'cw tray: {exc}', file=sys.stderr)
            return 1

        icon = pystray.Icon(
            'crossweave',
            _make_icon_image(image_cls, draw_cls),
            title=f'CrossWeave ({url})',
            menu=_build_menu(pystray, controller),
        )
        controller.attach_icon(icon)

        def _on_ready(tray_icon):
            tray_icon.visible = True
            if opts.open:
                controller.open_workspace()

        try:
            icon.run(setup=_on_ready)
        except KeyboardInterrupt:
            pass
        finally:
            controller.stop_server()
    return 0


def main():
    sys.exit(cmd_tray())
