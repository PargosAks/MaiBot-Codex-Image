"""Login reuse and drawing isolation; fake credentials only, no paid calls."""

from pathlib import Path

import pytest

from codex_image_mcp.client import auth_home
from codex_image_mcp.codex import Codex
from codex_image_mcp.service import Service
from conftest import envelope
from test_service import wait_job


def test_home_prefers_explicit_then_environment_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert auth_home() == Path.home() / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "existing-login"))
    assert auth_home() == (tmp_path / "existing-login").resolve()
    assert auth_home(str(tmp_path / "optional-login")) == (tmp_path / "optional-login").resolve()


def test_environment_excludes_provider_auth_and_process_overrides(settings, monkeypatch):
    for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "CODEX_AUTH_BASE_URL",
                "CODEX_CONFIG", "CODEX_HOME", "NODE_OPTIONS"):
        monkeypatch.setenv(key, "not-inherited")
    driver = Codex(settings)
    env = driver.environment(settings.state / "isolated")
    assert env["CODEX_HOME"] == str(settings.state / "isolated")
    assert not set(env) & {"OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "CODEX_AUTH_BASE_URL",
                           "CODEX_CONFIG", "NODE_OPTIONS"}


async def test_login_accepts_existing_config_without_changing_it(settings, driver_factory):
    settings.home.mkdir()
    config = settings.home / "config.toml"
    original = b'model_provider="daily-provider"\n[mcp_servers.daily]\ncommand="never-run-this"\n'
    config.write_bytes(original)
    await driver_factory().check_login()
    assert config.read_bytes() == original
    assert not (settings.home / "auth.json").exists()


@pytest.mark.parametrize("mode,expected", [("refresh", "completed"), ("account-change", "outcome_unknown")])
async def test_refresh_uses_original_login_and_rejects_account_switch(
    settings, driver_factory, mode, expected
):
    service = Service(settings, driver_factory(mode))
    await service.start()
    try:
        request = envelope("submit", {"request_id": "refresh-test", "prompt": "circle"})
        job = await service.submit(request)
        final = await wait_job(service, job["job_id"], {expected})
        assert final["status"] == expected
        assert (await service.submit(request))["job_id"] == job["job_id"]
        assert service.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert not list(settings.state.rglob("auth.json"))
        assert not list(settings.state.rglob("config.toml"))
        if mode == "refresh":
            path = service.store.db.execute("SELECT path FROM artifacts").fetchone()[0]
            assert Path(path).is_relative_to(settings.state)
            # A worker writes image outputs only to its own home, not the account's home.
            assert not (settings.home / "generated_images").exists()
    finally:
        await service.close()


async def test_runtime_home_rejects_unknown_config_before_any_inference(settings, driver_factory):
    job = {"id": "img_" + "1" * 32, "prompt": "circle"}
    runtime = settings.state / "codex-runtime" / job["id"]
    runtime.mkdir(parents=True)
    (runtime / "config.toml").write_text('model_provider="unexpected"', encoding="utf-8")

    async def no_output(*args, **kwargs):
        raise AssertionError("No thread or image is allowed")

    with pytest.raises(ValueError, match="未知 config.toml"):
        await driver_factory().generate(job, no_output, no_output)
