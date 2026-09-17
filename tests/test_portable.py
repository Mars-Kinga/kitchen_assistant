from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from urllib.request import urlopen
import zipfile

import pytest

import runtime_core.kitchen_console as console
from scripts import build_portable, portable


def test_occupied_port_falls_back_and_serves_status() -> None:
    service = SimpleNamespace(status=lambda: {"kitchen": {"state": "IDLE"}})
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        if port == 65535:
            pytest.skip("No higher port is available")
        server = console.KitchenConsoleServer(service, host="127.0.0.1", port=port)
        server.start()
        try:
            assert port < server.port <= min(65535, port + 19)
            with urlopen(f"http://127.0.0.1:{server.port}/api/status", timeout=5) as response:
                assert json.load(response)["kitchen"]["state"] == "IDLE"
        finally:
            server.stop()


def test_exhausted_port_does_not_disrupt_existing_listener() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(OSError):
            console.KitchenConsoleServer(object(), host="127.0.0.1", port=port, port_attempts=1)
        assert holder.getsockname()[1] == port


def test_two_console_instances_do_not_share_a_port() -> None:
    first = console.KitchenConsoleServer(object(), host="127.0.0.1", port=0)
    first.start()
    second = None
    try:
        if first.port == 65535:
            pytest.skip("No higher port is available")
        second = console.KitchenConsoleServer(object(), host="127.0.0.1", port=first.port)
        second.start()
        assert second.port != first.port
    finally:
        if second is not None:
            second.stop()
        first.stop()


def test_system_assigned_port_is_reported_in_urls() -> None:
    server = console.KitchenConsoleServer(object(), host="127.0.0.1", port=0)
    server.start()
    try:
        assert server.port > 0
        assert f"http://127.0.0.1:{server.port}" in server.access_urls()
    finally:
        server.stop()


def test_non_conflict_errors_do_not_retry(monkeypatch) -> None:
    calls = []

    def fail(address, handler):
        calls.append(address)
        raise OSError(errno.EADDRNOTAVAIL, "invalid local address")

    monkeypatch.setattr(console, "_ConsoleHTTPServer", fail)
    with pytest.raises(OSError):
        console.KitchenConsoleServer(object(), port=18765)
    assert calls == [("0.0.0.0", 18765)]


@pytest.mark.parametrize("port", [0, 65535])
def test_port_boundaries_do_not_wrap_or_hide_bind_failure(monkeypatch, port) -> None:
    calls = []

    def fail(address, handler):
        calls.append(address[1])
        raise OSError(errno.EADDRINUSE, "busy")

    monkeypatch.setattr(console, "_ConsoleHTTPServer", fail)
    with pytest.raises(OSError):
        console.KitchenConsoleServer(object(), port=port)
    assert calls == [port]


@pytest.mark.parametrize("error", [OSError(errno.EADDRINUSE, "busy"), OSError(errno.EACCES, "reserved")])
def test_windows_busy_and_reserved_ports_are_skipped(monkeypatch, error) -> None:
    calls = []

    def bind(address, handler):
        calls.append(address[1])
        if address[1] == 18765:
            raise error
        return SimpleNamespace(server_address=address)

    with monkeypatch.context() as patch:
        patch.setattr(console.sys, "platform", "win32")
        patch.setattr(console, "_ConsoleHTTPServer", bind)
        server = console.KitchenConsoleServer(object(), port=18765)
        actual_port = server.port
    assert calls == [18765, 18766]
    assert actual_port == 18766


@pytest.mark.parametrize("config", [{"port": -1}, {"port": True}, {"port": 65536},
                                    {"port_attempts": 0}, {"host": ""}, []])
def test_bad_portable_configuration_is_rejected(tmp_path, monkeypatch, config) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "console.json").write_text(json.dumps(config), encoding="utf-8-sig")
    monkeypatch.setattr(portable, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        portable.read_console_config()


def test_portable_configuration_reads_notepad_bom(tmp_path, monkeypatch) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "console.json").write_text('{"port": 19876}', encoding="utf-8-sig")
    monkeypatch.setattr(portable, "ROOT", tmp_path)
    assert portable.read_console_config()["port"] == 19876


def make_package_source(root: Path) -> None:
    for name in build_portable.ROOT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("example\n", encoding="utf-8")
    for name in ("runtime_core/module.py", "skills/kitchen_assistant/recipes/catalog/fixed.json",
                 "skills/kitchen_assistant/recipes/imported/personal.json",
                 "skills/kitchen_assistant/recipes/generated/cache.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    for name in ("config/qwen.env", "config/console.json", ".venv/secret.py",
                 "runtime_core/__pycache__/private.py", "runtime_core/.env",
                 "skills/kitchen_assistant/private.env", "runtime_logs/events.jsonl"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SECRET_MUST_NOT_BE_PACKAGED", encoding="utf-8")


@pytest.mark.parametrize("include_recipes", [False, True])
def test_package_excludes_secrets_and_hashes_every_file(tmp_path, include_recipes) -> None:
    root = tmp_path / "source"
    make_package_source(root)
    output = tmp_path / "delivery.zip"
    build_portable.build(output, include_local_recipes=include_recipes, root=root)
    prefix = build_portable.PACKAGE_NAME + "/"
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read(prefix + "PACKAGE_MANIFEST.json"))
        names = set(manifest["files"])
        assert ("skills/kitchen_assistant/recipes/imported/personal.json" in names) == include_recipes
        assert ("skills/kitchen_assistant/recipes/generated/cache.json" in names) == include_recipes
        assert "skills/kitchen_assistant/recipes/catalog/fixed.json" in names
        for name, digest in manifest["files"].items():
            data = archive.read(prefix + name)
            assert b"SECRET_MUST_NOT_BE_PACKAGED" not in data
            assert hashlib.sha256(data).hexdigest() == digest
        assert b"\r\n" in archive.read(prefix + "start.cmd")
    assert output.with_suffix(".zip.sha256").read_text().startswith(hashlib.sha256(output.read_bytes()).hexdigest())


def test_package_rejects_symlink_to_external_content(tmp_path) -> None:
    root = tmp_path / "source"
    make_package_source(root)
    external = tmp_path / "private.json"
    external.write_text("SECRET", encoding="utf-8")
    link = root / "runtime_core" / "private.json"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("Symlinks are unavailable for this user")
    with pytest.raises(ValueError):
        build_portable.package_files(root)
