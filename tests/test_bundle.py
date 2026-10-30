"""The only release artifact is a whitelist-built plugin without private data."""

import hashlib
import importlib.util
import json
import shutil
import zipfile

from image_plugin_under_test import Config
from conftest import PLUGIN

spec = importlib.util.spec_from_file_location("plugin_bundle_under_test", PLUGIN / "scripts/build_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def inspect_plugin_bundle(destination):
    outputs = bundle.build(destination)
    assert len(outputs) == 1
    output, digest = outputs[0]
    assert output.name == "MaiBot-Codex-Image-plugin-v0.7.0.zip"
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert {
            "plugin.py", "bridge.py", "delivery.py", "policy.py", "store.py", "artist.py", "thinking.py",
            "recovery.py", "docs/implementation-stage-3.md",
            "config.example.toml", "_manifest.json", "docs/artist-design.md",
            "docs/artist-stage-0.md", "docs/implementation-stage-1.md", "docs/implementation-stage-2.md",
        } <= names
        assert not any(name.startswith(("service/", "tests/", ".venv/", "state/")) for name in names)
        assert not names & {
            "embedded.py",
            "client.py",
            "login.py",
            "deploy.py",
            "install.sh",
            "install.ps1",
            "setup.sh",
        }
        manifest = json.loads(archive.read("_manifest.json"))
        assert manifest["version"] == "0.7.0"
        assert {x["name"] for x in manifest["dependencies"]} == {"httpx", "mcp"}
    assert (destination / "SHA256SUMS").read_text(encoding="ascii") == f"{digest}  {output.name}\n"
    return output, names


def test_plugin_only_config_and_single_bundle(tmp_path):
    assert set(Config.model_fields) == {"plugin", "connection"}
    inspect_plugin_bundle(tmp_path / "dist")


def test_bundle_whitelist_excludes_private_and_runtime_files(tmp_path, monkeypatch):
    # Only an isolated synthetic source tree is mutated; real credentials/state are never read.
    source = tmp_path / "synthetic-source"
    source.mkdir()
    for name in bundle.PLUGIN_FILES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN / name, target)
    (source / "docs").mkdir(exist_ok=True)
    (source / "docs/development.md").write_text("Synthetic development documentation", encoding="utf-8")
    private_files = [
        "config.toml",
        ".env",
        "auth.json",
        "secret.txt",
        "delivery.sqlite3",
        "artist.sqlite3",
        ".codex/auth.json",
        "state/jobs.sqlite3",
        "state/output.png",
        "service/config.toml",
        ".venv/pyvenv.cfg",
        "__pycache__/plugin.pyc",
        "tests/private-note.txt",
        "logs/private.log",
        "docs/private.key",
        "docs/auth.json",
        "docs/.env",
        "docs/private.md",
        "scripts/setup_host.py",
        "dist/validation/baseline/service/src/example.py",
    ]
    for relative in private_files:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-private-marker")
    monkeypatch.setattr(bundle, "ROOT", source)
    output, names = inspect_plugin_bundle(tmp_path / "dist")
    assert names.isdisjoint(private_files)
    with zipfile.ZipFile(output) as archive:
        assert all(b"synthetic-private-marker" not in archive.read(name) for name in names)
