"""Whitelist-only source packages. Runtime secrets are never eligible for packaging."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"]
PLUGIN_FILES = ("plugin.py", "bridge.py", "delivery.py", "client.py", "embedded.py", "login.py", "_manifest.json", "config.example.toml",
                "requirements.txt", "README.md", "LICENSE", "CHANGELOG.md")
ROOT_FILES = (*PLUGIN_FILES, "pyproject.toml", "deploy.py", "install.ps1", "install.sh", ".gitignore")


def entries():
    files = [ROOT / name for name in ROOT_FILES]
    for pattern in ("docs/*.md", "scripts/*.py", "scripts/codex-assets.json", "service/src/**/*.py",
                    "service/tests/*.py"):
        files.extend(ROOT.glob(pattern))
    files.extend(ROOT / "service" / name for name in ("pyproject.toml", "uv.lock", "requirements.txt", "config.example.toml"))
    return sorted({p for p in files if p.is_file() and "__pycache__" not in p.parts})


def build(destination=None):
    destination = Path(destination or ROOT / "dist")
    destination.mkdir(parents=True, exist_ok=True)
    all_files = entries()
    results = []
    plugin_files = [ROOT / name for name in PLUGIN_FILES] + list((ROOT / "docs").glob("*.md"))
    plugin_files += list((ROOT / "service/src").rglob("*.py"))
    for suffix, files in (("", all_files), ("-plugin", plugin_files)):
        output = destination / f"MaiBot-Codex-Image{suffix}-v{VERSION}.zip"
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(set(files)):
                relative = path.relative_to(ROOT).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(2026, 10, 27, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
                archive.writestr(info, path.read_bytes())
        with output.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        results.append((output, digest))
    (destination / "SHA256SUMS").write_text("".join(f"{digest}  {path.name}\n" for path, digest in results), encoding="ascii")
    return results


if __name__ == "__main__":
    for output, digest in build():
        print(output.name + "  " + digest)
