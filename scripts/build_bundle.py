"""Build only the plugin package; runtime data and service files are never included."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"]
PLUGIN_FILES = (
    "plugin.py", "bridge.py", "delivery.py", "policy.py", "store.py", "artist.py", "thinking.py", "recovery.py", "editing.py", "rendering.py",
    "host_runtime.py", "host_adapters.py",
    "_manifest.json", "config.example.toml",
    "requirements.txt", "README.md", "LICENSE", "CHANGELOG.md",
    "docs/quickstart.md", "docs/advanced.md", "docs/troubleshooting.md",
    "docs/development.md", "docs/validation.md",
    "docs/artist-design.md", "docs/artist-stage-0.md", "docs/implementation-stage-1.md",
    "docs/implementation-stage-2.md",
    "docs/implementation-stage-3.md",
    "docs/implementation-stage-4.md",
    "docs/implementation-stage-5.md",
    "docs/implementation-stage-6.md",
)


def entries():
    # An explicit list prevents local configs, task data or extra docs entering releases.
    # Missing required files fail the build rather than silently producing a partial package.
    return [ROOT / name for name in PLUGIN_FILES]


def build(destination=None):
    destination = Path(destination or ROOT / "dist")
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"MaiBot-Codex-Image-plugin-v{VERSION}.zip"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(entries()):
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 10, 30, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    results = [(output, digest)]
    (destination / "SHA256SUMS").write_text("".join(f"{digest}  {path.name}\n" for path, digest in results), encoding="ascii")
    return results


if __name__ == "__main__":
    for output, digest in build():
        print(output.name + "  " + digest)
