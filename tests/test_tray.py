"""Tests for the cross-platform system tray host."""
from types import SimpleNamespace

import pytest
from crossweave import cli, tray, web


class _FakeWorkspace:
    next_port = 18765

    def __init__(self, address, initial_home=''):
        host, port = address
        if port == 0:
            port = _FakeWorkspace.next_port
            _FakeWorkspace.next_port += 1
        self.server_address = (host, port)
        self.server_port = port
        self.initial_home = initial_home
        self.serving = False
        self.shutdown_called = False
        self.closed = False

    def serve_forever(self):
        self.serving = True

    def shutdown(self):
        self.shutdown_called = True

    def server_close(self):
        self.closed = True


def test_tray_controller_falls_back_from_busy_default_port(monkeypatch):
    def fake_workspace(address, initial_home=''):
        if address[1] == 8765:
            raise OSError('port in use')
        return _FakeWorkspace(address, initial_home=initial_home)

    monkeypatch.setattr(web, '_load_gui_env', lambda: None)
    monkeypatch.setattr(web, 'Workspace', fake_workspace)

    ctrl = tray.TrayController()
    url = ctrl.start_server()
    assert url.startswith('http://127.0.0.1:')
    assert not url.endswith(':8765')

    new_url = ctrl.restart_server()
    assert new_url != url
    assert ctrl.url == new_url

    ctrl.stop_server()
    assert ctrl.url == ''


def test_tray_controller_explicit_port_propagates_oserror(monkeypatch):
    def busy_workspace(address, initial_home=''):
        raise OSError('port in use')

    monkeypatch.setattr(web, '_load_gui_env', lambda: None)
    monkeypatch.setattr(web, 'Workspace', busy_workspace)

    ctrl = tray.TrayController(port=9000)
    with pytest.raises(OSError):
        ctrl.start_server()


def test_tray_controller_open_workspace_and_quit(monkeypatch):
    monkeypatch.setattr(web, '_load_gui_env', lambda: None)
    monkeypatch.setattr(web, 'Workspace', _FakeWorkspace)

    app_calls = []
    browser_calls = []
    monkeypatch.setattr(web, 'open_app_window', lambda url: app_calls.append(url))
    monkeypatch.setattr(tray.webbrowser, 'open', lambda url: browser_calls.append(url))

    fake_icon = SimpleNamespace(title='', stopped=False)
    fake_icon.stop = lambda: setattr(fake_icon, 'stopped', True)

    ctrl = tray.TrayController(port=9123)
    ctrl.attach_icon(fake_icon)
    assert fake_icon.title == 'CrossWeave (已停止)'

    url = ctrl.start_server()
    assert url == 'http://127.0.0.1:9123'
    assert fake_icon.title == 'CrossWeave (http://127.0.0.1:9123)'

    ctrl.open_workspace(True)
    ctrl.open_workspace(False)
    assert app_calls == ['http://127.0.0.1:9123']
    assert browser_calls == ['http://127.0.0.1:9123']

    ctrl.quit()
    assert fake_icon.stopped is True
    assert ctrl.url == ''
    assert fake_icon.title == 'CrossWeave (已停止)'


def test_cmd_tray_reports_missing_dependencies(monkeypatch, capsys):
    def missing():
        raise RuntimeError('cw tray requires optional dependencies (pystray, Pillow).')

    monkeypatch.setattr(tray, '_import_tray_deps', missing)

    assert tray.cmd_tray([]) == 1
    assert 'pystray' in capsys.readouterr().err


def test_cmd_tray_starts_icon_and_opens_when_requested(monkeypatch):
    created = []
    opened = []

    class _FakeIcon:
        def __init__(self, name, image, title='', menu=None):
            self.name = name
            self.image = image
            self.title = title
            self.menu = menu
            self.visible = False
            created.append(self)

        def run(self, setup=None):
            if setup is not None:
                setup(self)

        def stop(self):
            pass

    fake_pystray = SimpleNamespace(
        Icon=_FakeIcon,
        Menu=type('Menu', (), {'SEPARATOR': object(), '__init__': lambda self, *items: None}),
        MenuItem=lambda *args, **kwargs: (args, kwargs),
    )

    monkeypatch.setattr(tray, '_import_tray_deps', lambda: (fake_pystray, object(), object()))
    monkeypatch.setattr(tray, '_make_icon_image', lambda *a, **kw: 'fake-image')
    monkeypatch.setattr(web, '_load_gui_env', lambda: None)
    monkeypatch.setattr(web, 'Workspace', _FakeWorkspace)
    monkeypatch.setattr(web, 'open_app_window', lambda url: opened.append(url))

    assert tray.cmd_tray(['--port', '9234', '--open']) == 0
    assert len(created) == 1
    assert created[0].visible is True
    assert opened == ['http://127.0.0.1:9234']


def test_cli_dispatches_tray_subcommand(monkeypatch):
    called = []
    monkeypatch.setattr(tray, 'cmd_tray', lambda args: called.append(args) or 0)
    monkeypatch.setattr(tray.sys, 'argv', ['cw', 'tray', '--open'])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0
    assert called == [['--open']]


def test_make_icon_image_renders_rgba():
    PIL_Image = pytest.importorskip('PIL.Image')
    PIL_ImageDraw = pytest.importorskip('PIL.ImageDraw')
    img = tray._make_icon_image(PIL_Image, PIL_ImageDraw, size=64)
    assert img.mode == 'RGBA'
    assert img.size == (64, 64)


def test_handler_send_bytes_ignores_connection_aborted():
    class _BrokenWFile:
        def write(self, _data):
            raise ConnectionAbortedError(10053, 'connection aborted')

    handler = web.Handler.__new__(web.Handler)
    handler.headers_for = lambda status, kind: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    handler.wfile = _BrokenWFile()

    handler.send_bytes(b'{}', 'application/json; charset=utf-8')


def test_install_and_uninstall_windows_shortcut(monkeypatch, tmp_path, capsys):
    pytest.importorskip('PIL.Image')
    appdata = tmp_path / 'AppData' / 'Roaming'
    home = tmp_path / 'home'
    monkeypatch.setattr(tray.os, 'name', 'nt')
    monkeypatch.setenv('APPDATA', str(appdata))
    monkeypatch.setattr(tray.common, 'HOME', str(home))

    written = []

    def fake_write(link_path, target_path, arguments, icon_path):
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.write_bytes(b'lnk')
        written.append((link_path, target_path, arguments, icon_path))

    monkeypatch.setattr(tray, '_write_windows_shortcut', fake_write)

    assert tray.cmd_tray(['--install-shortcut', '--autostart']) == 0
    out = capsys.readouterr().out
    assert 'Installed shortcut:' in out
    assert len(written) == 2
    assert written[0][0].name == 'CrossWeave.lnk'
    assert written[1][0].parent.name == 'Startup'
    assert written[0][3].is_file()

    assert tray.cmd_tray(['--uninstall-shortcut']) == 0
    out = capsys.readouterr().out
    assert 'Removed:' in out
    assert not written[0][0].exists()
    assert not written[1][0].exists()
    assert not written[0][3].exists()
