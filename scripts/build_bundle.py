"""Build only the plugin package; runtime data and service files are never included."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"]
PLUGIN_FILES = (
    "domain/__init__.py", "creation/__init__.py", "host/__init__.py", "inspiration/__init__.py", "mcp_client/__init__.py", "workspace/__init__.py",
    "plugin.py", "mcp_client/bridge.py", "domain/delivery.py", "domain/policy.py", "domain/store.py", "creation/artist.py", "creation/thinking.py", "creation/recovery.py", "creation/editing.py", "creation/rendering.py",
    # 职责拆分后的模块必须随完整插件包发布，不能依赖开发目录中的残留源码。
    "creation/contracts.py", "creation/receipts.py", "creation/reviewing.py", "creation/sharing.py",
    "domain/async_work.py", "domain/errors.py", "domain/image_validation.py", "mcp_client/contracts.py",
    "workspace/images.py",
    "domain/schema.py", "domain/sqlite_uow.py", "domain/ledger_repository.py",
    "domain/review_repository.py", "domain/wake_repository.py",
    "host/runtime_generation.py", "host/delivery_runtime.py",
    "host/host_runtime.py", "host/host_adapters.py", "workspace/studio.py", "host/studio_chat.py", "webui.json", "assets/studio.html",
    "domain/profiles.py", "domain/assets.py", "creation/references.py", "host/host_media.py",
    "inspiration/group_notebook.py", "inspiration/group_ingress.py", "inspiration/group_collection.py", "docs/group-inspiration.md",
    "docs/group-inspiration-validation.md", "docs/validation/group-inspiration-20261104.json",
    "inspiration/group_thoughts.py", "host/group_thought_runtime.py",
    "_manifest.json", "config.example.toml",
    "requirements.txt", "README.md", "LICENSE", "CHANGELOG.md",
    "docs/quickstart.md", "docs/advanced.md", "docs/troubleshooting.md",
    "docs/development.md", "docs/validation.md", "docs/studio.md", "docs/retained-plan-recovery.md",
    "docs/architecture.md", "docs/plugin-audit-20261105.md",
    "docs/refactor-20261110.md",
    "docs/local-validation-20261110.md",
    "docs/artist-design.md", "docs/artist-stage-0.md", "docs/implementation-stage-1.md",
    "docs/implementation-stage-2.md",
    "docs/implementation-stage-3.md",
    "docs/implementation-stage-4.md",
    "docs/implementation-stage-5.md",
    "docs/implementation-stage-6.md",
    "docs/artist-iteration-plan.md",
    "docs/implementation-i1.md",
    "docs/implementation-i2.md",
    "docs/implementation-i3.md",
    "docs/implementation-i5.md",
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
