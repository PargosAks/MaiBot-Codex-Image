"""Pair a host service and plugin from one checkout; never log in or generate images."""

from pathlib import Path, PurePosixPath
import argparse
import ipaddress
import json
import os
import platform
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service" / "src"))
from deploy import locked, private_write, users_list  # noqa: E402
from codex_image_mcp.client import auth_home, find_codex  # noqa: E402
from codex_image_mcp.lease import StateLease  # noqa: E402

PLUGIN_ID = "pargosaks.maibot-codex-image"
UNIT_NAME = "maibot-codex-image.service"
UNIT_MARKER = "# Managed by MaiBot-Codex-Image scripts/setup_host.py"
PLUGIN_FILES = (
    "plugin.py", "bridge.py", "delivery.py", "_manifest.json", "requirements.txt",
    "config.example.toml", "README.md", "LICENSE", ".gitignore",
)


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=60).stdout


def inspect_container(name):
    if platform.system() != "Linux":
        raise ValueError("Docker 自动配置仅适用于本机 Linux Docker；其他环境请使用手动指南。")
    endpoint = json.loads(command(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])).strip()
    if not endpoint.startswith("unix://") or (
        os.environ.get("DOCKER_HOST") and not os.environ["DOCKER_HOST"].startswith("unix://")
    ):
        raise ValueError("自动配置只支持本机 Unix socket Docker；远程 Docker 请使用手动部署。")
    security = json.loads(command(["docker", "info", "--format", "{{json .SecurityOptions}}"])) or []
    if any("rootless" in option or "userns" in option for option in security):
        raise ValueError("rootless 或 user namespace 映射 Docker 请使用手动部署，不能假设宿主机 UID 映射。")
    data = json.loads(command(["docker", "inspect", "--type=container", name]))
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("无法确定唯一的 MaiBot 容器。")
    return data[0]


def container_owner(name):
    # Read IDs in the container's configured exec user context. No files or image layers change.
    values = [command(["docker", "exec", name, "id", option]).strip() for option in ("-u", "-g")]
    if not all(value.isdecimal() for value in values):
        raise ValueError("无法取得容器运行用户的 UID/GID，请核对容器状态或使用手动部署。")
    return tuple(int(value) for value in values)


def gateway(info, network=""):
    if info.get("HostConfig", {}).get("NetworkMode") == "host":
        return "127.0.0.1"
    networks = info.get("NetworkSettings", {}).get("Networks", {})
    if network:
        selected = networks.get(network)
    elif len(networks) == 1:
        selected = next(iter(networks.values()))
    else:
        raise ValueError("容器有多个或没有网络，请用 --network 指定实际网络名称。")
    value = selected.get("Gateway", "") if selected else ""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("所选 Docker 网络没有可用的宿主机 IPv4 Gateway。") from exc
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError("自动配置需要有效的宿主机 IPv4 Gateway。")
    return str(address)


def container_plugin_path(info, plugin_dir):
    # Validate against actual mounts; an arbitrary host clone is not a container plugin install.
    matches = []
    for mount in info.get("Mounts", []):
        if not mount.get("RW") or not mount.get("Source") or not mount.get("Destination"):
            continue
        source = Path(mount["Source"]).resolve()
        if plugin_dir.is_relative_to(source):
            target = PurePosixPath(mount["Destination"]) / plugin_dir.relative_to(source).as_posix()
            visible = []
            child_mount = False
            for other in info.get("Mounts", []):
                if not other.get("Destination"):
                    continue
                destination = PurePosixPath(other["Destination"])
                if destination != target and destination.is_relative_to(target):
                    # A separately mounted config/source file would not receive our
                    # host-side writes. Leave mixed mount layouts to manual setup.
                    child_mount = True
                if target.is_relative_to(destination):
                    path = None
                    if other.get("Source"):
                        path = (Path(other["Source"]) / target.relative_to(destination).as_posix()).resolve()
                    visible.append((len(destination.parts), path, other.get("RW", False)))
            if visible and not child_mount:
                _, actual_source, writable = max(visible, key=lambda item: item[0])
                if writable and actual_source == plugin_dir:
                    matches.append((len(source.parts), str(target)))
    if not matches:
        raise ValueError("插件目录不在容器实际可见的可写挂载中，或已被嵌套挂载遮蔽；请核对 Mounts。")
    return max(matches)[1]


def quote_unit(value):
    value = str(value)
    if any(c in value for c in '\r\n%$"'):
        raise ValueError("systemd 自动配置路径不能包含换行、百分号、美元符号或双引号。")
    return '"' + value.replace("\\", "\\\\") + '"'


def systemd_unit(root, uv, home, binary, docker=False, data_dir=None):
    data_dir = Path(data_dir) if data_dir else root
    executable_path = f"{Path(binary).parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    dependencies = "network-online.target" + (" docker.service" if docker else "")
    return f"""{UNIT_MARKER}
[Unit]
Description=MaiBot Codex Image MCP Service
Wants={dependencies}
After={dependencies}

[Service]
Type=simple
User=root
Group=root
WorkingDirectory={quote_unit(root)}
Environment={quote_unit('HOME=' + str(Path.home()))}
Environment={quote_unit('PATH=' + executable_path)}
Environment={quote_unit('CODEX_HOME=' + str(home))}
Environment={quote_unit('CODEX_IMAGE_SECRET_FILE=' + str(data_dir / 'secret.txt'))}
Environment={quote_unit('UV_PROJECT_ENVIRONMENT=' + str(data_dir / 'venv'))}
Environment="PYTHONUNBUFFERED=1"
UnsetEnvironment=CODEX_IMAGE_SECRET
ExecStart={quote_unit(uv)} run --project {quote_unit(root / 'service')} --locked --no-dev codex-image-mcp --config {quote_unit(data_dir / 'service.toml')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=90
KillMode=mixed
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def require_root_path(path):
    path = Path(path).resolve(strict=True)
    for entry in (path, *path.parents):
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"root 后台服务要求路径由 root 所有且组/其他用户不可写：{entry}")


def validate_root_install(root, uv, binary):
    # A privileged boot service must not execute a source tree that another user can replace.
    for executable in (uv, binary, sys.executable):
        require_root_path(executable)
    require_root_path(root)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            # Runner owns this TOML file. It is not executable service source;
            # its parent and every executable/importable path remain protected.
            if path == root / "config.toml" and path.is_file() and not path.is_symlink():
                continue
            info = path.lstat()
            if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                raise ValueError(f"root 后台服务源码/环境含非 root 所有或其他用户可写路径：{path}")
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if resolved.is_dir() and not resolved.is_relative_to(root):
                    raise ValueError("root 后台服务源码不能包含指向外部目录的符号链接。")
                require_root_path(resolved)


def set_owner(path, owner):
    if os.name != "nt":
        os.chown(path, *owner)


def readable_by(info, owner, mask):
    uid, gid = owner
    if uid == 0:
        return True
    shift = 6 if uid == info.st_uid else (3 if gid == info.st_gid else 0)
    return ((info.st_mode >> shift) & mask) == mask


def validate_existing_plugin_access(plugin_dir, owner):
    # Do not loosen permissions on an existing private checkout or expose its other files.
    if not readable_by(plugin_dir.stat(), owner, 1):
        raise ValueError("容器用户无法进入现有插件目录；请手动核对属主/访问权限，不会自动放开私有目录。")
    for name in PLUGIN_FILES[:5]:
        if not readable_by((plugin_dir / name).stat(), owner, 4):
            raise ValueError("容器用户无法读取现有插件源码；请手动核对该插件的属主/权限后再配置。")


def systemd_preflight(unit, root, uv, binary, directory=Path("/etc/systemd/system"), data_dir=None):
    data_dir = Path(data_dir) if data_dir else root
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise ValueError("--systemd 需要本机 Linux root 用户；请使用已手动登录 Codex 的同一用户。")
    for name in ("systemctl", "systemd-analyze"):
        if not shutil.which(name):
            raise ValueError(f"缺少 {name}，请使用前台启动或手动部署。")
    validate_root_install(root, uv, binary)
    if data_dir != root:
        validate_root_install(data_dir, uv, binary)
    target = directory / UNIT_NAME
    if target.is_symlink():
        raise ValueError("现有 systemd 服务文件是符号链接，请按手动指南处理。")
    if target.exists():
        previous = target.read_text(encoding="utf-8")
        if previous != unit and (
            UNIT_MARKER not in previous or f"--config {quote_unit(data_dir / 'service.toml')}" not in previous
        ):
            raise ValueError("已有同名手动或其他部署的 systemd 服务；不会覆盖，请先按手动指南核对。")
    return target


def install_systemd(source, target, start=True):
    # Verify before updating the unit. On a command failure, report it; never blindly retry a start.
    command(["systemd-analyze", "verify", str(source)])
    if target.exists() and target.read_bytes() != source.read_bytes():
        private_write(source.parent / "backups" / f"unit-{time.time_ns()}.bak", target.read_bytes())
    private_write(target, source.read_bytes())
    target.chmod(0o644)
    command(["systemctl", "daemon-reload"])
    if start:
        start_systemd()


def start_systemd():
    command(["systemctl", "enable", "--now", UNIT_NAME])
    if command(["systemctl", "is-active", UNIT_NAME]).strip() != "active":
        raise ValueError("配置已保存，但服务尚未运行；请查看 journalctl，不要重复提交绘图。")


def default_data_dir(root, plugin_dir):
    # Existing separate installs keep every original path. A fresh single
    # checkout never puts host credentials, artifacts or its venv in Docker.
    if root != plugin_dir or (root / "service.toml").exists():
        return root
    if platform.system() == "Linux" and os.geteuid() == 0:
        return Path("/var/lib/maibot-codex-image")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "maibot-codex-image"


def pristine_plugin_config(config):
    if not config or set(config) - {"plugin", "connection"}:
        return not config
    plugin, connection = config.get("plugin", {}), config.get("connection", {})
    defaults = {"url": "http://127.0.0.1:8765/mcp", "secret": "", "allowed_users": [],
                "poll_seconds": 2, "watch_seconds": 360}
    return (not set(plugin) - {"enabled", "config_version"}
            and all(key in defaults and value == defaults[key] for key, value in connection.items()))


def sync_environment(root, data_dir, uv):
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(data_dir / "venv"))
    subprocess.run([uv, "sync", "--project", str(root / "service"), "--locked", "--no-dev", "--python", "3.13"],
                   env=env, check=True)


def validate_host_privacy(info, paths):
    for mount in info.get("Mounts", []):
        if mount.get("Source"):
            source = Path(mount["Source"]).resolve()
            if any(Path(path).resolve().is_relative_to(source) or source.is_relative_to(Path(path).resolve())
                   for path in paths):
                raise ValueError("宿主机服务配置、任务或 Codex 登录目录位于容器可见挂载内，请选用宿主机私有目录。")


def validate_container_access(container, plugin_path, existing):
    target = PurePosixPath(plugin_path)
    checks = [("-x", path) for path in target.parents]
    if existing:
        checks.extend([("-x", target), *(("-r", target / name) for name in PLUGIN_FILES[:5])])
    for mode, path in checks:
        try:
            # Fixed shell program, path passed as an argument; checks use the same
            # configured exec UID as id above, and do not change any container file.
            command(["docker", "exec", container, "sh", "-c", 'test ' + mode + ' "$1"', "access-check", str(path)])
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"容器运行用户无法访问插件或其父路径：{path}；请核对现有挂载和权限。") from exc


def configure(root, plugin_dir, allowed, host="127.0.0.1", port=8765, uv="uv", use_systemd=False, docker=False,
              plugin_owner=None, data_dir=None, prepare_environment=False, container="", network=""):
    if Path(plugin_dir).expanduser().is_symlink():
        raise ValueError("插件目录不能是符号链接。")
    root, plugin_dir = Path(root).resolve(), Path(plugin_dir).expanduser().resolve()
    if plugin_dir.name != "codex_image" or not plugin_dir.parent.is_dir():
        raise ValueError("请指定已有插件根目录下的 codex_image 文件夹，例如 /opt/maibot/plugins/codex_image。")
    if root != plugin_dir and (root.is_relative_to(plugin_dir) or plugin_dir.is_relative_to(root)):
        raise ValueError("服务源码和插件目录可以相同，或使用互不包含的旧分离布局。")
    if not 1024 <= port <= 65535:
        raise ValueError("端口应在 1024 到 65535 之间。")
    allowed = users_list(allowed)
    if plugin_owner and os.name != "nt" and os.geteuid() not in (0, plugin_owner[0]):
        raise ValueError("当前用户不能为容器运行用户设置私有插件配置，请用合适的用户或手动部署。")
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    selected_data = Path(data_dir).expanduser() if data_dir is not None else default_data_dir(root, plugin_dir)
    if selected_data.is_symlink():
        raise ValueError("宿主机数据目录不能是符号链接。")
    data_dir = selected_data.resolve()
    if data_dir.is_relative_to(plugin_dir) and not (data_dir == root and (root / "service.toml").exists()):
        raise ValueError("新安装的宿主机配置和任务必须保存在插件目录之外。")
    runtime, service_path, secret_path = data_dir / "runtime", data_dir / "service.toml", data_dir / "secret.txt"
    metadata_path, plugin_path = runtime / "setup-host.json", plugin_dir / "config.toml"
    for path in (runtime, service_path, secret_path, plugin_path, plugin_dir):
        if path.is_symlink():
            raise ValueError("自动配置目标不能是符号链接，请核对实际目录后使用手动配置。")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.mkdir(exist_ok=True, mode=0o700)
    with locked(runtime / "setup.lock"):
        prior = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        if prior and prior.get("plugin_dir") != str(plugin_dir):
            raise ValueError("这个服务仓库已配对其他插件目录，不能自动改配。")
        old_service = tomllib.loads(service_path.read_text(encoding="utf-8")) if service_path.exists() else {}
        old_plugin = tomllib.loads(plugin_path.read_text(encoding="utf-8")) if plugin_path.exists() else {}
        if plugin_dir.exists() and any(plugin_dir.iterdir()):
            target_manifest = plugin_dir / "_manifest.json"
            if not target_manifest.is_file():
                raise ValueError("目标目录非空且不是已识别的插件，不会覆盖。")
            existing = json.loads(target_manifest.read_text(encoding="utf-8"))
            if existing.get("id") != PLUGIN_ID or existing.get("version") != manifest["version"]:
                raise ValueError("目标插件版本或身份不匹配，请先备份并更新插件，或使用新的空目录。")
            if not all((plugin_dir / name).is_file() for name in PLUGIN_FILES[:5]):
                raise ValueError("目标插件文件不完整，请先恢复；不会覆盖未知改动。")
            if plugin_owner:
                validate_existing_plugin_access(plugin_dir, plugin_owner)
            copy_plugin = False
        else:
            copy_plugin = True
        pristine = pristine_plugin_config(old_plugin)
        if not secret_path.exists() and (
            service_path.exists() or (old_plugin and not pristine) or prior or (runtime / "state").exists()
        ):
            raise ValueError("已有配置或状态但缺少 secret.txt，请恢复原密钥；不会自动换新。")
        secret = secret_path.read_text(encoding="utf-8").strip() if secret_path.exists() else secrets.token_urlsafe(40)
        if len(secret) < 32 or not secret.strip():
            raise ValueError("secret.txt 无效，请恢复原密钥。")
        url = f"http://{host}:{port}/mcp"
        if old_service and (old_service.get("host", "127.0.0.1") != host or old_service.get("port", 8765) != port):
            raise ValueError("现有服务监听地址或端口不同，请手动核对；不会改写现有部署。")
        if old_plugin and not pristine:
            connection = old_plugin.get("connection", {})
            if (connection.get("url") != url or connection.get("secret") != secret
                    or set(connection.get("allowed_users", [])) != set(allowed)):
                raise ValueError("现有插件连接、密钥或用户配置不同，请手动核对；不会覆盖。")
        binary = find_codex(old_service.get("binary", ""))
        home = auth_home(old_service.get("home", ""))
        state = Path(old_service.get("state", runtime / "state")).expanduser().resolve()
        if state.is_relative_to(plugin_dir) or state.is_relative_to(home) or home.is_relative_to(plugin_dir):
            raise ValueError("服务状态目录不能位于插件目录内或直接使用 Codex 登录目录。")
        settings = tomlkit.parse(service_path.read_text(encoding="utf-8")) if old_service else tomlkit.document()
        settings.update(binary=binary, home=str(home), state=str(state), host=host, port=port)
        settings.pop("allowed_users", None)
        plugin = tomlkit.parse(plugin_path.read_text(encoding="utf-8")) if old_plugin else tomlkit.parse(
            f'[plugin]\nconfig_version = {json.dumps(manifest["version"])}\nenabled = true\n'
        )
        plugin["plugin"]["config_version"] = manifest["version"]
        if not old_plugin or pristine:
            plugin["connection"] = {"url": url, "secret": secret, "allowed_users": allowed,
                                    "poll_seconds": 2, "watch_seconds": 360}
        unit = systemd_unit(root, uv, home, binary, docker, data_dir) if use_systemd else None
        unit_target = systemd_preflight(unit, root, uv, binary, data_dir=data_dir) if use_systemd else None
        service_text = tomlkit.dumps(settings)
        service_changed = not service_path.exists() or service_path.read_text(encoding="utf-8") != service_text
        unit_changed = use_systemd and (not unit_target.exists() or unit_target.read_text(encoding="utf-8") != unit)
        python_name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        needs_environment = prepare_environment and not (data_dir / "venv" / python_name).is_file()
        # A plugin-only reconfiguration can run while the independent MCP owns
        # its database. Only service/config/environment changes need its lease.
        lease = StateLease(state) if service_changed or unit_changed or needs_environment else None
        try:
            if needs_environment:
                sync_environment(root, data_dir, uv)
                if use_systemd:
                    validate_root_install(data_dir, uv, binary)
            backup = runtime / "backups" / str(time.time_ns())
            for path, label in ((service_path, "service.toml"), (plugin_path, "plugin-config.toml"),
                                (metadata_path, "setup-host.json")):
                if path.exists():
                    private_write(backup / label, path.read_bytes())
            if copy_plugin:
                owner = plugin_dir.parent.stat()
                plugin_dir.mkdir(exist_ok=True)
                for name in PLUGIN_FILES:
                    private_write(plugin_dir / name, (root / name).read_bytes())
                    (plugin_dir / name).chmod(0o644)
                for source in (root / "docs").glob("*.md"):
                    private_write(plugin_dir / "docs" / source.name, source.read_bytes())
                    (plugin_dir / "docs" / source.name).chmod(0o644)
                if plugin_owner or (os.name != "nt" and os.geteuid() == 0):
                    set_owner(plugin_dir, plugin_owner or (owner.st_uid, owner.st_gid))
            if not secret_path.exists():
                private_write(secret_path, secret + "\n")
            if service_changed:
                private_write(service_path, service_text)
            plugin_text = tomlkit.dumps(plugin)
            if not plugin_path.exists() or plugin_path.read_text(encoding="utf-8") != plugin_text:
                private_write(plugin_path, plugin_text)
            if plugin_owner or (os.name != "nt" and os.geteuid() == 0):
                owner = plugin_dir.stat()
                set_owner(plugin_path, plugin_owner or (owner.st_uid, owner.st_gid))
            private_write(metadata_path, json.dumps({"plugin_dir": str(plugin_dir), "source_dir": str(root),
                                                     "container": container, "network": network}, indent=2))
            if use_systemd:
                unit_source = runtime / UNIT_NAME
                private_write(unit_source, unit)
                unit_source.chmod(0o644)
                if unit_changed:
                    # Publish and reload the new unit while no server can claim
                    # this state. Release only immediately before starting it.
                    install_systemd(unit_source, unit_target, start=False)
        finally:
            if lease is not None:
                lease.close()
        if use_systemd:
            start_systemd()
    return {"url": url, "service_config": str(service_path), "plugin_config": str(plugin_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="一键配对宿主机 MCP 和 MaiBot 插件；Codex 登录仍由用户手动完成")
    parser.add_argument("--container", help="本机 Linux MaiBot 容器名；不填可交互选择直接部署")
    parser.add_argument("--network", default="", help="Docker 网络名；多网络时必须指定")
    parser.add_argument("--plugin-dir", type=Path, default=ROOT, help="默认使用当前源码目录；旧分离布局可指定插件路径")
    parser.add_argument("--data-dir", type=Path, help="宿主机私有配置目录；已有安装可指定原 service.toml 所在目录")
    parser.add_argument("--allowed-user", action="append", default=[], help="仅写入插件白名单，如 qq:123456")
    parser.add_argument("--port", type=int, help="默认保留已有端口，新安装使用 8765")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--systemd", action="store_true", help="Linux root：安装并启动 systemd，启用开机自启")
    mode.add_argument("--foreground", action="store_true", help="只配置并打印前台启动命令")
    parser.add_argument("--prepare-environment", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.plugin_dir.is_symlink():
        raise ValueError("插件目录不能是符号链接。")
    plugin_dir = args.plugin_dir.expanduser().resolve()
    selected_data = args.data_dir or default_data_dir(ROOT, plugin_dir)
    if selected_data.is_symlink():
        raise ValueError("宿主机数据目录不能是符号链接。")
    data_dir = selected_data.expanduser().resolve()
    metadata_path = data_dir / "runtime/setup-host.json"
    prior = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    old_plugin = tomllib.loads((plugin_dir / "config.toml").read_text(encoding="utf-8")) if (plugin_dir / "config.toml").exists() else {}
    allowed = args.allowed_user or old_plugin.get("connection", {}).get("allowed_users", [])
    interactive = sys.stdin.isatty()
    if not allowed and not interactive:
        parser.error("首次非交互配置请提供 --allowed-user。")
    container = args.container
    if container is None and prior:
        container = prior.get("container", "")
    elif container is None and interactive:
        container = input("MaiBot 容器名（直接部署留空）：").strip()
    args.network = args.network or prior.get("network", "")
    info = inspect_container(container) if container else None
    if info:
        print("容器现有挂载：")
        for mount in info.get("Mounts", []):
            print(f"  {mount.get('Source', '')} -> {mount.get('Destination', '')}")
        networks = info.get("NetworkSettings", {}).get("Networks", {})
        if len(networks) > 1 and not args.network and interactive:
            print("网络：" + ", ".join(networks))
            args.network = input("选择网络名称：").strip()
    if info:
        visible_plugin = container_plugin_path(info, plugin_dir)
        print("容器中的插件路径：" + visible_plugin)
        service_path = data_dir / "service.toml"
        old_service = tomllib.loads(service_path.read_text(encoding="utf-8")) if service_path.exists() else {}
        validate_host_privacy(info, [data_dir, auth_home(old_service.get("home", "")),
                                    old_service.get("state", data_dir / "runtime/state")])
    plugin_owner = container_owner(container) if info else None
    if info:
        validate_container_access(container, visible_plugin, plugin_dir.exists())
    allowed = allowed or [input("允许绘图的用户（例如 qq:123456）：").strip()]
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("未找到 uv，请先安装并将它加入 PATH。")
    service_path = data_dir / "service.toml"
    old_service = tomllib.loads(service_path.read_text(encoding="utf-8")) if service_path.exists() else {}
    port = args.port if args.port is not None else old_service.get("port", 8765)
    result = configure(ROOT, plugin_dir, allowed, gateway(info, args.network) if info else "127.0.0.1",
                       port, str(Path(uv).resolve()), args.systemd, bool(container), plugin_owner,
                       data_dir, args.prepare_environment, container or "", args.network)
    print("配置已保存，两端密钥已配对；用户白名单仅位于插件。")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("请在 MaiBot WebUI 授权绘图命令及文字/图片发送，启用并重新加载插件。")
    if args.systemd:
        print("systemd 已启用；日志：journalctl -u " + UNIT_NAME + " -f")
    elif os.name == "nt":
        print("启动前清除 CODEX_IMAGE_SECRET，并设置 CODEX_IMAGE_SECRET_FILE 为 " + str(data_dir / "secret.txt"))
        print("设置 UV_PROJECT_ENVIRONMENT 为 " + str(data_dir / "venv"))
        print(subprocess.list2cmdline([uv, "run", "--project", str(ROOT / "service"), "--locked", "--no-dev",
                                     "codex-image-mcp", "--config", str(data_dir / "service.toml")]))
    else:
        print("前台启动：")
        print("unset CODEX_IMAGE_SECRET")
        print("export CODEX_IMAGE_SECRET_FILE=" + shlex.quote(str(data_dir / "secret.txt")))
        print("export UV_PROJECT_ENVIRONMENT=" + shlex.quote(str(data_dir / "venv")))
        print(shlex.join([uv, "run", "--project", str(ROOT / "service"), "--locked", "--no-dev",
                          "codex-image-mcp", "--config", str(data_dir / "service.toml")]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Do not expose captured command output or configuration contents (which may contain secrets).
        print("自动配置未完成：" + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__),
              file=sys.stderr)
        print("已保存的文件请保留；检查原配置或 systemd 日志后再操作，不会自动重新登录或绘图。", file=sys.stderr)
        raise SystemExit(1) from None
