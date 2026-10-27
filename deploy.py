"""Installer shared by Windows and Unix entry points. Codex login is always manual."""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent
PLUGIN_ID = "pargosaks.maibot-codex-image"
COMMAND_ID = PLUGIN_ID + ".codex_image_command"
PLUGIN_FILES = ("plugin.py", "bridge.py", "delivery.py", "client.py", "embedded.py", "login.py", "_manifest.json", "requirements.txt", "README.md")


def run(argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another installer or service is using this directory") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.replace(temporary, path)


def users_list(values):
    users = []
    for value in values:
        protocol, separator, identity = value.strip().partition(":")
        if not separator or not protocol or not identity or any(c.isspace() for c in value):
            raise ValueError("Allowed user must look like qq:123456 or webui:actual_user_id")
        users.append(protocol.lower() + ":" + identity)
    if not users:
        raise ValueError("At least one explicit allowed user is required")
    return list(dict.fromkeys(users))


def preflight(instance, runtime):
    instance, runtime = Path(instance).resolve(), Path(runtime).resolve()
    if instance == runtime or runtime.is_relative_to(instance) or instance.is_relative_to(runtime):
        raise ValueError("MaiBot instance and image service directory must be separate, non-nested directories")
    bot = instance / "config" / "bot_config.toml"
    if not bot.is_file() or not (instance / "config" / "model_config.toml").is_file():
        raise ValueError("MaiBot instance is not initialized; use --create-instance for a new instance")
    tomllib.loads(bot.read_text(encoding="utf-8"))
    target = instance / "plugins" / "codex_image"
    if target.is_symlink() or not target.resolve().is_relative_to(instance):
        raise ValueError("Plugin destination escapes instance")
    if target.resolve() == ROOT or (target / ".git").exists():
        raise ValueError("Do not install over a source checkout; choose a separate runtime instance")
    if target.exists():
        manifest = target / "_manifest.json"
        if not manifest.exists() or json.loads(manifest.read_text(encoding="utf-8")).get("id") != PLUGIN_ID:
            raise ValueError("Destination contains a different or unrecognized plugin")
    state_path = runtime / "deployment.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if previous and Path(previous["instance"]).resolve() != instance:
        raise ValueError("This service directory is paired with another MaiBot instance")
    return instance, runtime, target, previous


def asset_name(system=None, machine=None):
    system, machine = system or platform.system(), (machine or platform.machine()).lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    suffix = {"Windows": "pc-windows-msvc", "Linux": "unknown-linux-musl", "Darwin": "apple-darwin"}.get(system)
    if not arch or not suffix:
        raise ValueError("Automatic Codex installation supports Windows/Linux/macOS x64 and ARM64")
    return f"codex-package-{arch}-{suffix}.tar.gz"


def extract_package(archive, destination):
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.name or ":" in member.name:
                raise ValueError("Unsafe archive member")
            if not (destination / member.name).resolve().is_relative_to(destination):
                raise ValueError("Archive path escapes destination")
            stream.extract(member, destination, filter="data")


def codex_binary(runtime, supplied=None):
    if supplied:
        binary = Path(supplied).expanduser().resolve()
        if not binary.is_file() or binary.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("CodexBinary must name a native executable, not a shell shim")
        return binary
    candidate = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if candidate and Path(candidate).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return Path(candidate).resolve()
    manifest = json.loads((ROOT / "scripts/codex-assets.json").read_text(encoding="utf-8"))
    entry = manifest["assets"][asset_name()]
    package_root = runtime / "codex" / manifest["version"]
    marker = package_root / "installed.json"
    if marker.exists():
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        binary = package_root / recorded["binary"]
        if binary.is_file():
            return binary
    archive = runtime / "downloads" / asset_name()
    archive.parent.mkdir(parents=True, exist_ok=True)
    digest = None
    if archive.exists():
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != entry["sha256"]:
        print("Downloading official Codex package " + manifest["version"], flush=True)
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "MaiBot-Codex-Image/0.1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != entry["sha256"]:
            raise ValueError("Codex package checksum mismatch")
    extract_package(archive, package_root)
    name = "codex.exe" if os.name == "nt" else "codex"
    choices = [p for p in package_root.rglob(name) if p.is_file() and not p.is_symlink()]
    # Official packages can put the executable at the root or under bin/.
    if not choices:
        raise ValueError("Official Codex package does not contain the expected executable")
    choices.sort(key=lambda p: (len(p.relative_to(package_root).parts), str(p)))
    binary = choices[0]
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | 0o100)
    private_write(marker, json.dumps({"binary": str(binary.relative_to(package_root)), "sha256": entry["sha256"]}))
    return binary


def runtime_environment(runtime):
    env = dict(os.environ)
    env.update(UV_CACHE_DIR=str(runtime / "cache"), UV_PYTHON_INSTALL_DIR=str(runtime / "python"),
               UV_TOOL_DIR=str(runtime / "tools"), UV_TOOL_BIN_DIR=str(runtime / "bin"),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def service_source(runtime):
    destination = runtime / "service"
    for name in ("pyproject.toml", "uv.lock", "requirements.txt"):
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "service" / name, destination / name)
    for source in (ROOT / "service/src").rglob("*.py"):
        if "__pycache__" in source.parts:
            continue
        target = destination / "src" / source.relative_to(ROOT / "service/src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def configure(instance, runtime, binary, allowed, port, grant_command, maibot_exe=None):
    import tomlkit
    instance, runtime, target, previous = preflight(instance, runtime)
    secret_path = runtime / "secret.txt"
    if previous and not secret_path.is_file():
        raise ValueError("Existing deployment lost secret.txt; restore it instead of silently rotating credentials")
    secret = secret_path.read_text(encoding="utf-8").strip() if secret_path.exists() else secrets.token_urlsafe(40)
    if len(secret) < 32:
        raise ValueError("Existing secret.txt is invalid")
    if (target / "config.toml").exists() and not previous:
        raise ValueError("An existing configured plugin needs its original service directory; refusing to replace its key")
    service_config = runtime / "service.toml"
    old_settings = tomllib.loads(service_config.read_text(encoding="utf-8")) if service_config.exists() else {}
    home = old_settings.get("home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    settings = {"binary": str(binary), "home": home, "state": str(runtime / "state"),
                "allowed_users": allowed, "host": "127.0.0.1", "port": port, "model": "",
                "timeout": 300, "rpc_timeout": 30}
    if service_config.exists():
        settings = tomllib.loads(service_config.read_text(encoding="utf-8")) | settings
    plugin = {"plugin": {"config_version": "0.3.0", "enabled": True}, "service": {"mode": "external"},
              "connection": {"url": f"http://127.0.0.1:{port}/mcp", "secret": secret,
                             "allowed_users": allowed, "poll_seconds": 2, "watch_seconds": 360}}
    botpath = instance / "config/bot_config.toml"
    bot = tomlkit.parse(botpath.read_text(encoding="utf-8"))
    if grant_command:
        plugin_section = bot.setdefault("plugin", tomlkit.table())
        rules = plugin_section.setdefault("command_permissions", tomlkit.table())
        rule = rules.setdefault(COMMAND_ID, tomlkit.table())
        rule["allow_users"] = list(dict.fromkeys(list(rule.get("allow_users", [])) + allowed))
        rule.setdefault("allow_chats", [])
    # Back up only managed files. Preserve unrelated files and all runtime data.
    backup = runtime / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    for name in (*PLUGIN_FILES, "config.toml"):
        old = target / name
        if old.exists():
            private_write(backup / "plugin" / name, old.read_bytes())
    if grant_command:
        private_write(backup / "bot_config.toml", botpath.read_bytes())
    for old in (service_config, runtime / "deployment.json"):
        if old.exists():
            private_write(backup / old.name, old.read_bytes())
    target.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        private_write(target / name, (ROOT / name).read_bytes())
    for source in (ROOT / "service/src").rglob("*.py"):
        private_write(target / source.relative_to(ROOT), source.read_bytes())
    private_write(secret_path, secret + "\n")
    private_write(service_config, tomlkit.dumps(settings))
    private_write(target / "config.toml", tomlkit.dumps(plugin))
    if grant_command:
        private_write(botpath, tomlkit.dumps(bot))
    private_write(runtime / "manage.py", (ROOT / "scripts/manage.py").read_bytes())
    metadata = {"instance": str(instance), "runtime": str(runtime), "binary": str(binary),
                "port": port, "allowed_users": allowed, "auth_home": home,
                "maibot_exe": maibot_exe or previous.get("maibot_exe")}
    private_write(runtime / "deployment.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    python = runtime / "service/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    for stem, command in (("Login-Codex", "login"), ("Start-Service", "start"), ("Check-Installation", "doctor"),
                          ("Start-MaiBot", "maibot")):
        ps = '$ErrorActionPreference = "Stop"\n& ' + "'" + str(python).replace("'", "''") + "' '" + str(runtime / "manage.py").replace("'", "''") + f"' {command} @args\nexit $LASTEXITCODE\n"
        private_write(runtime / (stem + ".ps1"), b"\xef\xbb\xbf" + ps.encode("utf-8"))
        shell = "#!/bin/sh\nexec " + shlex.join([str(python), str(runtime / "manage.py"), command]) + ' "$@"\n'
        path = runtime / (stem.lower() + ".sh")
        private_write(path, shell)
        path.chmod(0o700)
    print("Installation configured. No Codex login was attempted.")
    print("1. Manually run: " + str(runtime / ("Login-Codex.ps1" if os.name == "nt" else "login-codex.sh")))
    print("2. Run Check-Installation, then Start-Service; restart MaiBot to load the plugin.")
    print("Only plugin files and the requested command permission entry were changed in the MaiBot instance.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the Codex image service and MaiBot plugin")
    parser.add_argument("--maibot-directory", type=Path)
    parser.add_argument("--install-directory", type=Path, required=True)
    parser.add_argument("--allowed-user", action="append", default=[])
    parser.add_argument("--codex-binary", type=Path)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--create-instance", action="store_true")
    parser.add_argument("--no-grant-command", action="store_true")
    parser.add_argument("--configure-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    instance = args.maibot_directory or Path(input("MaiBot instance directory: ").strip().strip('"'))
    users = users_list(args.allowed_user or [input("Allowed user (e.g. qq:123456): ").strip()])
    runtime = args.install_directory.expanduser().resolve()
    instance = instance.expanduser().resolve()
    if instance == runtime or runtime.is_relative_to(instance) or instance.is_relative_to(runtime):
        raise ValueError("Use separate, non-nested directories for the service and MaiBot")
    if not 1024 <= args.port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    env = runtime_environment(runtime)
    if args.configure_only:
        configure(instance, runtime, args.codex_binary, users, args.port, not args.no_grant_command,
                  os.environ.get("MAIBOT_IMAGE_NEW_MAIBOT"))
        return
    with locked(runtime / "install.lock"):
        if args.create_instance and not (instance / "config/bot_config.toml").exists():
            run([args.uv, "tool", "install", "--python", "3.13", "maibot==1.3.15", "--with", "maibot-plugin-sdk==2.17.0"], env=env)
            exe = runtime / "bin" / ("maibot.exe" if os.name == "nt" else "maibot")
            run([exe, "init", "--directory", instance], env=env)
            env["MAIBOT_IMAGE_NEW_MAIBOT"] = str(exe)
        preflight(instance, runtime)
        binary = codex_binary(runtime, args.codex_binary)
        run([binary, "--version"], env=env)
        service_source(runtime)
        run([args.uv, "sync", "--project", runtime / "service", "--no-dev", "--locked", "--python", "3.13"], env=env)
        python = runtime / "service/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command = [python, ROOT / "deploy.py", "--configure-only", "--maibot-directory", instance,
                   "--install-directory", runtime, "--codex-binary", binary, "--port", str(args.port)]
        for user in users:
            command.extend(["--allowed-user", user])
        if args.no_grant_command:
            command.append("--no-grant-command")
        run(command, env=env)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("Installation stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
