from pathlib import Path
import sys
import importlib.util

import pytest

from codex_image_mcp.config import Settings
from codex_image_mcp.codex import Codex, Rpc
import codex_image_mcp.codex as driver_module

PLUGIN = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "image_plugin_under_test",
    PLUGIN / "plugin.py",
    submodule_search_locations=[str(PLUGIN)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from image_plugin_under_test.bridge import sign  # noqa: E402

SCOPE = {"platform": "qq", "stream_id": "real-stream", "user_id": "123"}
SECRET = "local-test-secret-" + "s" * 32


@pytest.fixture
def settings(tmp_path):
    return Settings(
        binary="unused-codex",
        home=tmp_path / "home",
        state=tmp_path / "state",
        secret=SECRET,
        allowed_users=("qq:123",),
        timeout=2,
        rpc_timeout=1,
    ).validated()


def envelope(op, args, scope=None):
    return sign(SECRET, scope or SCOPE, op, args)


@pytest.fixture
def driver_factory(monkeypatch, settings):
    def factory(mode="ok"):
        def rpc(argv, env, timeout):
            role = "worker" if 'cli_auth_credentials_store="ephemeral"' in argv else "broker"
            return Rpc(
                [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode, role],
                env,
                timeout,
            )

        monkeypatch.setattr(driver_module, "Rpc", rpc)
        return Codex(settings)

    return factory
