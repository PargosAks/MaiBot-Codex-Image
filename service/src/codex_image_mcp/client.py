"""Native Codex discovery and existing login selection for the host service."""

from pathlib import Path
import os
import platform
import shutil


def auth_home(value: str = "") -> Path:
    selected = value or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(selected).expanduser().resolve()


def find_codex(value: str = "") -> str:
    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            candidate = Path(shutil.which(value) or value)
        candidate = candidate.resolve()
        if not candidate.is_file() or candidate.suffix.lower() in {".cmd", ".bat", ".ps1", ".js"}:
            raise ValueError("binary 必须指向 Codex 原生程序；不能填写 .cmd 或 .ps1。")
        return str(candidate)

    name = "codex.exe" if os.name == "nt" else "codex"
    candidate = shutil.which(name)
    if candidate and Path(candidate).is_file():
        # npm's Unix launcher is JavaScript with a shebang. Locate its native vendor binary.
        resolved = Path(candidate).resolve()
        if resolved.suffix.lower() not in {".js", ".cmd", ".ps1", ".bat"}:
            return str(resolved)

    shim = shutil.which("codex")
    if shim:
        resolved = Path(shim).resolve()
        roots = [resolved.parent / "node_modules/@openai/codex"]
        if resolved.parent.name == "bin":
            roots.append(resolved.parent.parent)
        arch = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64"
        suffix = {"Windows": "pc-windows-msvc", "Darwin": "apple-darwin", "Linux": "unknown-linux-musl"}.get(
            platform.system()
        )
        target = f"{arch}-{suffix}"
        for root in roots:
            for vendor in [root / "vendor", *root.glob("node_modules/@openai/codex-*/vendor"),
                           *root.parent.glob("codex-*/vendor")]:
                native = vendor / target / "codex" / name
                if native.is_file():
                    return str(native.resolve())
    raise ValueError("未找到 Codex 原生客户端。请安装官方 Codex CLI，或设置 binary 后重新启动服务。")
