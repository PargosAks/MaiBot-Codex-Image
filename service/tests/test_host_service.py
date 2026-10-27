"""Host configuration and state ownership; fake binaries and temporary state only."""

from pathlib import Path
import asyncio
import os
import sqlite3
import subprocess
import sys

import pytest

from codex_image_mcp.client import find_codex
from codex_image_mcp.config import Settings
from codex_image_mcp.server import create_app
from codex_image_mcp.service import Service
import codex_image_mcp.client as client_module
import codex_image_mcp.config as config_module
import codex_image_mcp.server as server_module
import codex_image_mcp.service as service_module
from conftest import SECRET


@pytest.fixture
def host_config(tmp_path, monkeypatch):
    for key in ("CODEX_HOME", "CODEX_IMAGE_SECRET", "CODEX_IMAGE_SECRET_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "os-user"))
    monkeypatch.setenv("HOME", str(tmp_path / "os-user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "os-user"))
    monkeypatch.setenv("CODEX_IMAGE_SECRET", SECRET)
    native = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    native.write_bytes(b"fake native binary; never execute")
    monkeypatch.setattr(
        client_module.shutil, "which", lambda name: str(native) if name == native.name else None
    )
    path = tmp_path / "service.toml"

    def write(extra=""):
        path.write_text(
            f'state = "{(tmp_path / "state").as_posix()}"\n' + extra,
            encoding="utf-8",
        )
        return path

    return write, native


@pytest.mark.parametrize("extra", ["", 'binary = ""\nhome = ""\n'])
def test_settings_load_discovers_native_and_reuses_default_home(host_config, tmp_path, extra):
    write, native = host_config
    cfg = Settings.load(write(extra))
    assert cfg.binary == str(native.resolve())
    assert cfg.home == (tmp_path / "os-user" / ".codex").resolve()
    assert not hasattr(cfg, "allowed_users")
    assert not cfg.home.exists()
    assert not cfg.state.exists()


@pytest.mark.parametrize("legacy_users", ['["qq:123"]', "[]"])
def test_settings_warns_and_ignores_legacy_allowed_users(host_config, legacy_users):
    write, _ = host_config
    with pytest.warns(UserWarning, match=r"allowed_users.*ignored.*plugin.*connection.allowed_users"):
        cfg = Settings.load(write(f"allowed_users = {legacy_users}\n"))
    assert not hasattr(cfg, "allowed_users")
    assert cfg.validated() is cfg


def test_settings_without_legacy_allowed_users_does_not_warn(host_config, recwarn):
    write, _ = host_config
    Settings.load(write())
    assert not recwarn


@pytest.mark.parametrize("extra", ["", 'home = ""\n'])
def test_settings_load_reuses_environment_home(host_config, tmp_path, monkeypatch, extra):
    write, _ = host_config
    existing = tmp_path / "existing-login"
    monkeypatch.setenv("CODEX_HOME", str(existing))
    assert Settings.load(write(extra)).home == existing.resolve()
    assert not existing.exists()


def test_settings_load_explicit_binary_and_home_win(host_config, tmp_path, monkeypatch):
    write, _ = host_config
    explicit = tmp_path / "selected-codex.exe"
    explicit.write_bytes(b"fake native binary; never execute")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "environment-login"))
    monkeypatch.setattr(client_module.shutil, "which", lambda name: pytest.fail("Explicit binary must win"))
    cfg = Settings.load(write(f'binary = "{explicit.as_posix()}"\nhome = "~/selected-login"\n'))
    assert cfg.binary == str(explicit.resolve())
    assert cfg.home == (Path.home() / "selected-login").resolve()


def test_direct_settings_validation_never_discovers_binary(settings, monkeypatch):
    monkeypatch.setattr(config_module, "find_codex", lambda *args: pytest.fail("Unexpected native discovery"))
    assert settings.validated().binary == "unused-codex"


def test_native_discovery_resolves_npm_vendor_binary(tmp_path, monkeypatch):
    shim = tmp_path / "bin" / "codex.js"
    shim.parent.mkdir()
    shim.write_text("fake npm launcher", encoding="utf-8")
    name = "codex.exe" if os.name == "nt" else "codex"
    native = tmp_path / "vendor" / "x86_64-unknown-linux-musl" / "codex" / name
    native.parent.mkdir(parents=True)
    native.write_bytes(b"fake native binary; never execute")
    monkeypatch.setattr(client_module.shutil, "which", lambda value: str(shim) if value == "codex" else None)
    monkeypatch.setattr(client_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(client_module.platform, "system", lambda: "Linux")
    assert find_codex() == str(native.resolve())


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1", ".js"])
def test_explicit_script_is_not_a_native_binary(tmp_path, suffix):
    shim = tmp_path / ("codex" + suffix)
    shim.write_text("never run", encoding="utf-8")
    with pytest.raises(ValueError, match="原生程序"):
        find_codex(str(shim))


def test_missing_native_binary_has_clear_error(monkeypatch):
    monkeypatch.setattr(client_module.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="未找到 Codex 原生客户端"):
        find_codex()


def test_settings_secret_file_and_environment_precedence(host_config, tmp_path, monkeypatch):
    write, _ = host_config
    secret = tmp_path / "secret"
    secret.write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.delenv("CODEX_IMAGE_SECRET")
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(secret))
    assert Settings.load(write()).secret == SECRET
    monkeypatch.setenv("CODEX_IMAGE_SECRET", "environment-wins-" + "x" * 32)
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(tmp_path / "does-not-exist"))
    assert Settings.load(write()).secret == "environment-wins-" + "x" * 32


@pytest.mark.parametrize("value", [None, "", "short", " " * 40])
def test_settings_rejects_missing_or_invalid_secret(host_config, monkeypatch, value):
    write, _ = host_config
    if value is None:
        monkeypatch.delenv("CODEX_IMAGE_SECRET")
    else:
        monkeypatch.setenv("CODEX_IMAGE_SECRET", value)
    with pytest.raises(ValueError, match="CODEX_IMAGE_SECRET.*at least 32"):
        Settings.load(write())


def test_empty_environment_secret_does_not_fall_back_to_file(host_config, tmp_path, monkeypatch):
    write, _ = host_config
    secret = tmp_path / "secret"
    secret.write_text(SECRET, encoding="utf-8")
    monkeypatch.setenv("CODEX_IMAGE_SECRET", "")
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(secret))
    with pytest.raises(ValueError, match="non-empty secret"):
        Settings.load(write())


def test_secret_validation_precedes_binary_discovery(host_config, monkeypatch):
    write, _ = host_config
    monkeypatch.delenv("CODEX_IMAGE_SECRET")
    monkeypatch.setattr(
        config_module, "find_codex", lambda *args: pytest.fail("Secret must be checked first")
    )
    with pytest.raises(ValueError, match="non-empty secret"):
        Settings.load(write())


@pytest.mark.parametrize("content", [b"", b"\n \n", b"short", b"\xff"])
def test_settings_rejects_invalid_secret_file(host_config, tmp_path, monkeypatch, content):
    write, _ = host_config
    secret = tmp_path / "secret"
    secret.write_bytes(content)
    monkeypatch.delenv("CODEX_IMAGE_SECRET")
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(secret))
    with pytest.raises(ValueError, match="CODEX_IMAGE_SECRET_FILE"):
        Settings.load(write())


def test_settings_reports_unreadable_secret_file(host_config, tmp_path, monkeypatch):
    write, _ = host_config
    monkeypatch.delenv("CODEX_IMAGE_SECRET")
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="Cannot read CODEX_IMAGE_SECRET_FILE"):
        Settings.load(write())


async def test_app_construction_is_exclusive_before_start_and_reopens(settings):
    first = create_app(settings)
    try:
        assert first.service.worker is None
        job = first.service.store.submit("owner", "message", "circle", 10)
        first.service.store.claim()
        first.service.store.update(job["job_id"], status="running", thread_id="original-thread")
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            create_app(settings)
        current = first.service.store.get("owner", job["job_id"])
        assert current["status"] == "running"
        assert current["error_code"] is None
        assert first.service.store.db.execute("SELECT thread_id FROM jobs").fetchone()[0] == "original-thread"
    finally:
        await asyncio.gather(first.service.close(), first.service.close())
        await first.service.close()
    restored = create_app(settings)
    try:
        current = restored.service.store.get("owner", job["job_id"])
        assert current["status"] == "outcome_unknown"
        assert current["error_code"] == "SERVICE_RESTARTED"
    finally:
        await restored.service.close()


async def test_state_lock_excludes_another_process(settings):
    first = Service(settings)
    source = """
from pathlib import Path
import sys
from codex_image_mcp.lease import StateLease
try:
    lease = StateLease(Path(sys.argv[1]))
except RuntimeError as exc:
    assert 'STATE_DIRECTORY_IN_USE' in str(exc)
    print('locked')
else:
    lease.close()
    print('acquired')
"""

    def probe():
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run(
            [sys.executable, "-c", source, str(settings.state)],
            env=env, capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()

    try:
        assert await asyncio.to_thread(probe) == "locked"
    finally:
        await first.close()
    assert await asyncio.to_thread(probe) == "acquired"


async def test_store_open_failure_releases_state_lock(settings, monkeypatch):
    real_store = service_module.Store

    def broken_store(path):
        raise OSError("simulated database open failure")

    monkeypatch.setattr(service_module, "Store", broken_store)
    with pytest.raises(OSError, match="simulated database open failure"):
        Service(settings)
    monkeypatch.setattr(service_module, "Store", real_store)
    restored = Service(settings)
    await restored.close()


async def test_driver_construction_failure_closes_store_and_releases_lock(settings, monkeypatch):
    real_driver = service_module.Codex
    opened = []
    real_store = service_module.Store

    def track_store(path):
        store = real_store(path)
        opened.append(store)
        return store

    def broken_driver(cfg):
        raise RuntimeError("simulated driver construction failure")

    monkeypatch.setattr(service_module, "Store", track_store)
    monkeypatch.setattr(service_module, "Codex", broken_driver)
    with pytest.raises(RuntimeError, match="simulated driver construction failure"):
        Service(settings)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].db.execute("SELECT 1")
    monkeypatch.setattr(service_module, "Codex", real_driver)
    restored = Service(settings)
    await restored.close()


async def test_transport_construction_failure_releases_state_lock(settings, monkeypatch):
    real_mcp = server_module.FastMCP

    def broken_transport(*args, **kwargs):
        raise RuntimeError("simulated transport construction failure")

    monkeypatch.setattr(server_module, "FastMCP", broken_transport)
    with pytest.raises(RuntimeError, match="simulated transport construction failure"):
        create_app(settings)
    monkeypatch.setattr(server_module, "FastMCP", real_mcp)
    restored = create_app(settings)
    await restored.service.close()


async def test_start_does_not_repeat_initialization_or_create_duplicate_workers(settings, monkeypatch):
    service = Service(settings)
    monkeypatch.setattr(service_module, "Store", lambda *args: pytest.fail("Store reopened during start"))
    try:
        await service.start()
        worker = service.worker
        await service.start()
        assert service.worker is worker
    finally:
        await service.close()
    with pytest.raises(RuntimeError, match="SERVICE_CLOSED"):
        await service.start()


async def test_cancelled_close_retains_lock_until_worker_stops(settings):
    entered, stopping, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Driver:
        async def generate(self, job, on_ids, on_image):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopping.set()
                await finish.wait()
                raise

    service = Service(settings, Driver())
    service.store.submit("owner", "message", "circle", 10)
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(service.close())
        await asyncio.wait_for(stopping.wait(), 5)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            Service(settings)
    finally:
        finish.set()
        await service.close()
    restored = Service(settings)
    await restored.close()
