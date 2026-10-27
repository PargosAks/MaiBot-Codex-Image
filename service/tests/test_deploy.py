from pathlib import Path
import importlib.util
import io
import json
import tarfile
import tomllib
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bundle_deploy", ROOT / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
build_spec = importlib.util.spec_from_file_location("bundle_build", ROOT / "scripts/build_bundle.py")
bundle = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(bundle)


@pytest.fixture
def instance(tmp_path):
    path = tmp_path / "麦麦 instance"
    (path / "config").mkdir(parents=True)
    (path / "config/bot_config.toml").write_text(
        '# preserve my comment\n[plugin]\npermission = ["qq:owner"]\n'
        '[plugin.command_permissions."other.command"]\nallow_users=["qq:other"]\n', encoding="utf-8")
    (path / "config/model_config.toml").write_text("# preserve model settings\n", encoding="utf-8")
    (path / "core.py").write_bytes(b"UNMODIFIED CORE")
    return path


def test_configuration_is_scoped_and_repeatable(instance, tmp_path):
    runtime = tmp_path / "image runtime"
    runtime.mkdir()
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"unused")
    deploy.configure(instance, runtime, binary, ["qq:123"], 18765, True)
    first = (runtime / "secret.txt").read_bytes()
    (runtime / "codex-home/auth.json").write_text('{"test":"credential-preserved"}', encoding="utf-8")
    (runtime / "state").mkdir()
    (runtime / "state/important-job").write_bytes(b"never replay or delete")
    deploy.configure(instance, runtime, binary, ["qq:123"], 18765, True)
    assert (runtime / "secret.txt").read_bytes() == first
    assert (runtime / "codex-home/auth.json").read_text() == '{"test":"credential-preserved"}'
    assert (runtime / "state/important-job").read_bytes() == b"never replay or delete"
    assert (instance / "core.py").read_bytes() == b"UNMODIFIED CORE"
    assert (instance / "config/model_config.toml").read_text() == "# preserve model settings\n"
    text = (instance / "config/bot_config.toml").read_text(encoding="utf-8")
    assert "# preserve my comment" in text
    bot = tomllib.loads(text)
    assert bot["plugin"]["permission"] == ["qq:owner"]
    assert bot["plugin"]["command_permissions"]["other.command"]["allow_users"] == ["qq:other"]
    assert bot["plugin"]["command_permissions"][deploy.COMMAND_ID]["allow_users"] == ["qq:123"]
    service = tomllib.loads((runtime / "service.toml").read_text(encoding="utf-8"))
    plugin = tomllib.loads((instance / "plugins/codex_image/config.toml").read_text(encoding="utf-8"))
    assert service["allowed_users"] == plugin["connection"]["allowed_users"] == ["qq:123"]
    assert plugin["connection"]["secret"].encode() == first.strip()
    assert (runtime / "backups").is_dir()


def test_no_grant_preserves_bot_bytes(instance, tmp_path):
    original = (instance / "config/bot_config.toml").read_bytes()
    runtime = tmp_path / "image"
    runtime.mkdir()
    deploy.configure(instance, runtime, Path("codex.exe"), ["qq:123"], 8765, False)
    assert (instance / "config/bot_config.toml").read_bytes() == original


def test_destination_and_secret_conflicts_are_rejected(instance, tmp_path):
    with pytest.raises(ValueError, match="non-nested"):
        deploy.preflight(instance, instance / "image")
    target = instance / "plugins/codex_image"
    target.mkdir(parents=True)
    (target / "_manifest.json").write_text(json.dumps({"id": "somebody.else"}))
    with pytest.raises(ValueError, match="different"):
        deploy.preflight(instance, tmp_path / "runtime")
    (target / "_manifest.json").write_text(json.dumps({"id": deploy.PLUGIN_ID}))
    (target / "config.toml").write_text('secret="do not replace"')
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    with pytest.raises(ValueError, match="original service"):
        deploy.configure(instance, runtime, Path("codex.exe"), ["qq:123"], 8765, True)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "dir/../../escape", "C:/escape", "dir\\escape"])
def test_archive_escape_rejected(tmp_path, name):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo(name)
        info.size = 1
        stream.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        deploy.extract_package(archive, tmp_path / "safe")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Windows", "ARM64"),
    ("Linux", "x86_64"), ("Linux", "aarch64"), ("Darwin", "arm64"), ("Darwin", "x86_64")])
def test_all_supported_platforms_have_pinned_packages(system, machine):
    manifest = json.loads((ROOT / "scripts/codex-assets.json").read_text())
    entry = manifest["assets"][deploy.asset_name(system, machine)]
    assert entry["url"].startswith("https://github.com/openai/codex/releases/download/")
    assert len(entry["sha256"]) == 64


def test_bundle_does_not_include_runtime_secrets(tmp_path):
    results = bundle.build(tmp_path / "dist")
    with zipfile.ZipFile(results[0][0]) as archive:
        names = set(archive.namelist())
        assert {"plugin.py", "_manifest.json", "install.ps1", "install.sh", "service/uv.lock"} <= names
        assert not any(".git/" in name or ".venv/" in name or "__pycache__" in name for name in names)
        assert not names.intersection({"auth.json", "secret.txt", "config.toml", "deployment.json"})
    with zipfile.ZipFile(results[1][0]) as archive:
        assert "plugin.py" in archive.namelist() and "install.ps1" not in archive.namelist()
