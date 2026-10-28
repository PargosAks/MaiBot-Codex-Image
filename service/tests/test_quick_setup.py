"""First-install configuration with fake binaries and Docker/systemd fixtures only."""

from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import shutil
import subprocess
import tomllib

import pytest

from codex_image_mcp.lease import StateLease

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("quick_setup_under_test", ROOT / "scripts/setup_host.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    repo = tmp_path / "host repo"
    repo.mkdir()
    for name in setup.PLUGIN_FILES:
        (repo / name).write_bytes((ROOT / name).read_bytes())
    (repo / "docs").mkdir()
    (repo / "docs/quickstart.md").write_text("test documentation", encoding="utf-8")
    parent = tmp_path / "MaiBot/plugins"
    parent.mkdir(parents=True)
    (parent.parent / "core.py").write_bytes(b"never modify core")
    binary = tmp_path / "native-codex.exe"
    binary.write_bytes(b"fake; never execute")
    home = tmp_path / "existing-login"
    home.mkdir()
    (home / "config.toml").write_bytes(b'keep existing Codex settings\n')
    monkeypatch.setattr(setup, "find_codex", lambda value: str(binary))
    monkeypatch.setattr(setup, "auth_home", lambda value: home)
    return repo, parent / "codex_image", home


def configure(installation, **kwargs):
    repo, plugin, _ = installation
    return setup.configure(repo, plugin, ["qq:123"], **kwargs)


def test_new_setup_pairs_plugin_only_allowlist_and_thin_files(installation):
    repo, plugin, home = installation
    result = configure(installation, host="172.18.0.1")
    service = tomllib.loads((repo / "service.toml").read_text())
    cfg = tomllib.loads((plugin / "config.toml").read_text())
    assert "allowed_users" not in service
    assert cfg["connection"]["allowed_users"] == ["qq:123"]
    assert cfg["connection"]["secret"] == (repo / "secret.txt").read_text().strip()
    assert cfg["connection"]["url"] == result["url"] == "http://172.18.0.1:8765/mcp"
    assert not (plugin / "service").exists() and not (plugin / "scripts").exists()
    assert (plugin / "docs/quickstart.md").is_file()
    assert "config.toml" in (plugin / ".gitignore").read_text(encoding="utf-8")
    assert (home / "config.toml").read_bytes() == b'keep existing Codex settings\n'
    assert not (home / "auth.json").exists()
    assert (plugin.parent.parent / "core.py").read_bytes() == b"never modify core"


def test_repeat_preserves_secret_state_custom_settings_and_disabled_plugin(installation):
    repo, plugin, _ = installation
    configure(installation)
    secret = (repo / "secret.txt").read_bytes()
    important = repo / "runtime/state/jobs.sqlite3"
    important.write_bytes(b"do not open or replay the database")
    service = repo / "service.toml"
    service.write_text(service.read_text() + '\nmodel = "custom"\ntimeout = 777\nallowed_users = ["legacy"]\n')
    config = plugin / "config.toml"
    config.write_text(config.read_text().replace("enabled = true", "enabled = false"))
    (plugin / "plugin.py").write_text("# user modification must survive", encoding="utf-8")
    (plugin / ".git").mkdir()
    configure(installation)
    assert (repo / "secret.txt").read_bytes() == secret
    assert important.read_bytes() == b"do not open or replay the database"
    updated = tomllib.loads(service.read_text())
    assert updated["model"] == "custom" and updated["timeout"] == 777
    assert "allowed_users" not in updated
    assert tomllib.loads(config.read_text())["plugin"]["enabled"] is False
    assert (plugin / "plugin.py").read_text() == "# user modification must survive"
    assert list((repo / "runtime/backups").glob("*/plugin-config.toml"))


@pytest.mark.parametrize("change", ["host", "users", "key", "missing-key", "corrupt-key"])
def test_existing_conflicts_never_rotate_or_overwrite(installation, change):
    repo, plugin, _ = installation
    configure(installation)
    secret = repo / "secret.txt"
    cfg = plugin / "config.toml"
    if change == "key":
        cfg.write_text(cfg.read_text().replace(secret.read_text().strip(), "other-secret-" + "x" * 40))
    if change == "missing-key":
        secret.unlink()
    if change == "corrupt-key":
        secret.write_text("short")
    before = cfg.read_bytes(), (repo / "service.toml").read_bytes()
    args = ["qq:other"] if change == "users" else ["qq:123"]
    with pytest.raises(ValueError):
        setup.configure(repo, plugin, args, host="172.18.0.1" if change == "host" else "127.0.0.1")
    assert (cfg.read_bytes(), (repo / "service.toml").read_bytes()) == before
    if change == "missing-key":
        assert not secret.exists()


def test_running_server_allows_plugin_only_configuration_without_touching_service(installation):
    repo, _, _ = installation
    configure(installation)
    original = (repo / "service.toml").read_bytes()
    secret = (repo / "secret.txt").read_bytes()
    lease = StateLease(repo / "runtime/state")
    try:
        configure(installation)
        assert (repo / "service.toml").read_bytes() == original
        assert (repo / "secret.txt").read_bytes() == secret
    finally:
        lease.close()


def test_running_server_still_blocks_service_configuration_changes(installation):
    repo, _, _ = installation
    configure(installation)
    path = repo / "service.toml"
    path.write_text(path.read_text() + '\nallowed_users = ["old-policy"]\n')
    original = path.read_bytes()
    lease = StateLease(repo / "runtime/state")
    try:
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            configure(installation)
        assert path.read_bytes() == original
    finally:
        lease.close()


@pytest.mark.parametrize("condition", ["foreign", "old-version", "non-plugin", "wrong-leaf"])
def test_unknown_destination_is_not_overwritten(installation, condition):
    repo, plugin, _ = installation
    if condition == "wrong-leaf":
        plugin = plugin.parent / "another-directory"
    else:
        plugin.mkdir()
        if condition == "non-plugin":
            (plugin / "notes.txt").write_text("preserve")
        else:
            (plugin / "_manifest.json").write_text(json.dumps({
                "id": "foreign.plugin" if condition == "foreign" else setup.PLUGIN_ID,
                "version": "0.1.0",
            }))
    with pytest.raises(ValueError):
        setup.configure(repo, plugin, ["qq:123"])
    assert not (repo / "secret.txt").exists()
    assert not (plugin / "plugin.py").exists()


def docker_info(parent):
    return {"HostConfig": {"NetworkMode": "bridge"},
            "NetworkSettings": {"Networks": {"mai": {"Gateway": "172.18.0.1"}}},
            "Mounts": [{"Source": str(parent), "Destination": "/app/plugins", "RW": True}]}


def test_docker_mount_mapping_and_gateway(installation):
    _, plugin, _ = installation
    info = docker_info(plugin.parent)
    assert setup.gateway(info) == "172.18.0.1"
    assert setup.container_plugin_path(info, plugin) == "/app/plugins/codex_image"
    info["Mounts"][0]["RW"] = False
    with pytest.raises(ValueError, match="可写挂载"):
        setup.container_plugin_path(info, plugin)
    info["HostConfig"]["NetworkMode"] = "host"
    assert setup.gateway(info) == "127.0.0.1"


def test_multiple_or_invalid_network_requires_explicit_choice(installation):
    _, plugin, _ = installation
    info = docker_info(plugin.parent)
    info["NetworkSettings"]["Networks"]["other"] = {"Gateway": "172.19.0.1"}
    with pytest.raises(ValueError, match="--network"):
        setup.gateway(info)
    assert setup.gateway(info, "other") == "172.19.0.1"
    with pytest.raises(ValueError, match="Gateway"):
        setup.gateway(info, "missing")


def test_cli_docker_only_inspects_and_does_not_print_secret(installation, monkeypatch, capsys):
    repo, plugin, _ = installation
    calls = []

    def read_only(argv):
        calls.append(argv)
        if argv[:3] == ["docker", "context", "inspect"]:
            return json.dumps("unix:///var/run/docker.sock")
        if argv[:2] == ["docker", "info"]:
            return json.dumps(["name=seccomp", "name=cgroupns"])
        if argv == ["docker", "inspect", "--type=container", "maibot"]:
            return json.dumps([docker_info(plugin.parent)])
        if argv[:6] == ["docker", "exec", "maibot", "sh", "-c", 'test -x "$1"']:
            assert argv[-1] in ("/app/plugins", "/app", "/")
            return ""
        assert argv in (["docker", "exec", "maibot", "id", "-u"], ["docker", "exec", "maibot", "id", "-g"])
        return "1000\n"

    monkeypatch.setattr(setup, "ROOT", repo)
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup, "command", read_only)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/local/bin/uv")
    owners = []
    monkeypatch.setattr(setup, "set_owner", lambda path, owner: owners.append((path, owner)))
    assert setup.main(["--container", "maibot", "--plugin-dir", str(plugin), "--allowed-user", "qq:123"]) == 0
    assert len(calls) == 8
    assert (plugin / "config.toml", (1000, 1000)) in owners
    assert (repo / "secret.txt").read_text().strip() not in capsys.readouterr().out


@pytest.mark.parametrize("writable", [False, True])
def test_nested_mount_hiding_parent_is_rejected(installation, writable):
    repo, plugin, _ = installation
    info = docker_info(plugin.parent)
    info["Mounts"].append({"Source": str(repo / "other"), "Destination": "/app/plugins/codex_image", "RW": writable})
    with pytest.raises(ValueError, match="遮蔽"):
        setup.container_plugin_path(info, plugin)


@pytest.mark.parametrize("child,writable", [("config.toml", False), ("config.toml", True),
                                         ("plugin.py", False), ("plugin.py", True)])
def test_child_file_mounts_are_not_written_through_parent(installation, child, writable):
    repo, plugin, _ = installation
    info = docker_info(plugin.parent)
    info["Mounts"].append({"Source": str(repo / child),
                           "Destination": "/app/plugins/codex_image/" + child, "RW": writable})
    with pytest.raises(ValueError, match="遮蔽"):
        setup.container_plugin_path(info, plugin)


@pytest.mark.parametrize("source", [None, ""])
def test_opaque_tmpfs_mount_over_plugin_is_rejected(installation, source):
    _, plugin, _ = installation
    info = docker_info(plugin.parent)
    mount = {"Type": "tmpfs", "Destination": "/app/plugins/codex_image", "RW": True}
    if source is not None:
        mount["Source"] = source
    info["Mounts"].append(mount)
    with pytest.raises(ValueError, match="遮蔽"):
        setup.container_plugin_path(info, plugin)


@pytest.mark.parametrize("private", ["directory", "source-file"])
def test_existing_private_checkout_is_rejected_before_configuration_write(installation, monkeypatch, private):
    repo, plugin, _ = installation
    configure(installation)
    before = (plugin / "config.toml").read_bytes()
    protected = plugin if private == "directory" else plugin / "plugin.py"
    original = Path.stat
    fake = SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o40700 if private == "directory" else 0o100600)
    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: fake if path == protected else original(path, *a, **kw))
    with pytest.raises(ValueError, match="容器用户无法"):
        setup.configure(repo, plugin, ["qq:123"], plugin_owner=(1000, 1000))
    assert (plugin / "config.toml").read_bytes() == before


@pytest.mark.parametrize("unsafe_uid,unsafe_mode", [(1000, 0o100644), (0, 0o100666)])
def test_root_service_rejects_untrusted_source_tree(installation, monkeypatch, unsafe_uid, unsafe_mode):
    repo, _, _ = installation
    original = Path.lstat
    monkeypatch.setattr(setup, "require_root_path", lambda path: None)
    monkeypatch.setattr(setup.os, "walk", lambda *a, **kw: [(str(repo), [], ["plugin.py"])])
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_uid=unsafe_uid, st_mode=unsafe_mode)
                        if path == repo / "plugin.py" else original(path))
    with pytest.raises(ValueError, match="非 root 所有或其他用户可写"):
        setup.validate_root_install(repo, "/usr/bin/uv", "/usr/bin/codex")


def test_root_service_rejects_replaceable_parent_directory(installation, monkeypatch):
    repo, _, _ = installation
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, **kw: SimpleNamespace(st_uid=1000, st_mode=0o40755)
                        if path == repo.parent else original(path, **kw))
    with pytest.raises(ValueError, match="root 所有"):
        setup.require_root_path(repo)


@pytest.mark.parametrize("security", [["name=rootless"], ["name=userns"]])
def test_unmapped_container_uid_modes_are_rejected(monkeypatch, security):
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(setup, "command", lambda argv: json.dumps("unix:///var/run/docker.sock")
                        if argv[1] == "context" else json.dumps(security))
    with pytest.raises(ValueError, match="映射"):
        setup.inspect_container("maibot")


def test_remote_docker_is_not_a_local_host_install(monkeypatch):
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup, "command", lambda argv: json.dumps("ssh://another-machine"))
    with pytest.raises(ValueError, match="远程 Docker"):
        setup.inspect_container("maibot")


def test_systemd_generation_and_install_order(tmp_path, monkeypatch):
    root = tmp_path / "host repo"
    root.mkdir()
    unit = setup.systemd_unit(root, "/usr/local/bin/uv", "/root/.codex", "/opt/codex/bin/codex", True)
    assert 'UnsetEnvironment=CODEX_IMAGE_SECRET' in unit
    assert 'Environment="CODEX_HOME=/root/.codex"' in unit
    assert "Wants=network-online.target docker.service" in unit
    assert "User=root" in unit and "KillMode=mixed" in unit
    source, target = root / setup.UNIT_NAME, tmp_path / "units" / setup.UNIT_NAME
    source.write_text(unit)
    target.parent.mkdir()
    calls = []

    def execute(argv):
        calls.append(argv)
        if argv[:2] == ["systemd-analyze", "verify"]:
            assert not target.exists()
        return "active\n" if "is-active" in argv else ""

    monkeypatch.setattr(setup, "command", execute)
    setup.install_systemd(source, target)
    assert target.read_text() == unit
    assert calls == [["systemd-analyze", "verify", str(source)], ["systemctl", "daemon-reload"],
                     ["systemctl", "enable", "--now", setup.UNIT_NAME], ["systemctl", "is-active", setup.UNIT_NAME]]


def test_systemd_validation_failure_does_not_write_unit(tmp_path, monkeypatch):
    source, target = tmp_path / "source.service", tmp_path / "target.service"
    source.write_text("invalid unit")
    target.write_text("keep existing unit")

    def fail(argv):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(setup, "command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        setup.install_systemd(source, target)
    assert target.read_text() == "keep existing unit"


def single_checkout(installation):
    repo, plugin, home = installation
    shutil.copytree(repo, plugin)
    return plugin, plugin.parent.parent.parent / "host-private", home


def test_single_checkout_keeps_host_data_outside_plugin_and_accepts_runner_defaults(installation):
    plugin, data, home = single_checkout(installation)
    (plugin / "config.toml").write_text(
        '[plugin]\nconfig_version = "0.5.0"\nenabled = false\n'
        '[connection]\nurl = "http://127.0.0.1:8765/mcp"\nsecret = ""\n'
        'allowed_users = []\npoll_seconds = 2\nwatch_seconds = 360\n', encoding="utf-8")
    before_source = (plugin / "plugin.py").read_bytes()
    setup.configure(plugin, plugin, ["qq:123"], data_dir=data)
    cfg = tomllib.loads((plugin / "config.toml").read_text())
    service = tomllib.loads((data / "service.toml").read_text())
    assert cfg["plugin"] == {"enabled": False, "config_version": "0.6.0"}
    assert cfg["connection"]["secret"] == (data / "secret.txt").read_text().strip()
    assert service["state"] == str((data / "runtime/state").resolve())
    assert service["home"] == str(home)
    assert not any((plugin / name).exists() for name in ("service.toml", "secret.txt", "runtime", "venv"))
    assert (plugin / "plugin.py").read_bytes() == before_source


def test_single_checkout_repeat_while_mcp_running_preserves_login_key_and_jobs(installation):
    plugin, data, home = single_checkout(installation)
    setup.configure(plugin, plugin, ["qq:123"], data_dir=data)
    database = data / "runtime/state/jobs.sqlite3"
    database.write_bytes(b"existing tasks must not be opened or migrated")
    auth = home / "auth.json"
    auth.write_bytes(b"fake login marker - never read by setup")
    protected = [data / "secret.txt", data / "service.toml", database, auth, home / "config.toml"]
    original = [path.read_bytes() for path in protected]
    cfg = plugin / "config.toml"
    cfg.write_text(cfg.read_text().replace('config_version = "0.6.0"', 'config_version = "0.5.0"'))
    lease = StateLease(data / "runtime/state")
    try:
        setup.configure(plugin, plugin, ["qq:123"], data_dir=data)
        assert tomllib.loads(cfg.read_text())["plugin"]["config_version"] == "0.6.0"
        assert [path.read_bytes() for path in protected] == original
    finally:
        lease.close()


def test_unpaired_existing_plugin_connection_never_creates_replacement_secret(installation):
    repo, plugin, _ = installation
    configure(installation)
    alternate = repo.parent / "new-empty-data"
    previous = (plugin / "config.toml").read_bytes()
    with pytest.raises(ValueError, match="原密钥"):
        setup.configure(repo, plugin, ["qq:123"], data_dir=alternate)
    assert not (alternate / "secret.txt").exists()
    assert (plugin / "config.toml").read_bytes() == previous


def test_reusing_old_data_directory_preserves_absolute_state_and_settings(installation):
    repo, plugin, home = installation
    configure(installation)
    old_service = (repo / "service.toml").read_bytes()
    secret = (repo / "secret.txt").read_bytes()
    job = repo / "runtime/state/jobs.sqlite3"
    job.write_bytes(b"original database")
    setup.configure(plugin, plugin, ["qq:123"], data_dir=repo)
    assert (repo / "service.toml").read_bytes() == old_service
    assert (repo / "secret.txt").read_bytes() == secret
    assert job.read_bytes() == b"original database"
    assert tomllib.loads(old_service.decode())["home"] == str(home)


def test_host_private_files_must_not_be_visible_in_any_container_mount(installation):
    _, plugin, home = installation
    info = docker_info(plugin.parent)
    setup.validate_host_privacy(info, [home])
    with pytest.raises(ValueError, match="容器可见"):
        setup.validate_host_privacy(info, [plugin / "private"])
    info["Mounts"].append({"Source": str(home), "Destination": "/credentials", "RW": False})
    with pytest.raises(ValueError, match="容器可见"):
        setup.validate_host_privacy(info, [home])


@pytest.mark.parametrize("private,child", [("home", "auth.json"), ("data", "secret.txt"),
                                         ("state", "artifacts")])
def test_nested_private_file_or_directory_mount_is_rejected(tmp_path, private, child):
    protected = tmp_path / private
    info = {"Mounts": [{"Source": str(protected / child), "Destination": "/leak", "RW": False}]}
    with pytest.raises(ValueError, match="容器可见"):
        setup.validate_host_privacy(info, [protected])


def test_container_parent_access_is_checked_in_actual_exec_user_context(monkeypatch):
    calls = []

    def inaccessible(argv):
        calls.append(argv)
        if argv[-1] == "/app/plugins":
            raise subprocess.CalledProcessError(1, argv)
        return ""

    monkeypatch.setattr(setup, "command", inaccessible)
    with pytest.raises(ValueError, match="父路径"):
        setup.validate_container_access("mai", "/app/plugins/codex_image", existing=True)
    assert calls[0] == ["docker", "exec", "mai", "sh", "-c", 'test -x "$1"', "access-check", "/app/plugins"]


def test_systemd_publication_holds_lease_and_releases_before_start(installation, monkeypatch):
    repo, plugin, _ = installation
    unit_target = repo.parent / "units" / setup.UNIT_NAME
    unit_target.parent.mkdir()
    monkeypatch.setattr(setup, "systemd_preflight", lambda *a, **kw: unit_target)
    observed = []

    def publish(source, target, start=True):
        assert not start
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            StateLease(repo / "runtime/state")
        observed.append("published-under-lease")

    def start():
        lease = StateLease(repo / "runtime/state")
        lease.close()
        observed.append("started-after-release")

    monkeypatch.setattr(setup, "install_systemd", publish)
    monkeypatch.setattr(setup, "start_systemd", start)
    setup.configure(repo, plugin, ["qq:123"], use_systemd=True)
    assert observed == ["published-under-lease", "started-after-release"]


def test_default_data_dir_new_single_source_and_old_layout(installation, monkeypatch):
    repo, plugin, _ = installation
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0, raising=False)
    assert setup.default_data_dir(plugin, plugin) == Path("/var/lib/maibot-codex-image")
    assert setup.default_data_dir(repo, plugin) == repo
    (plugin / "service.toml").parent.mkdir()
    (plugin / "service.toml").write_text("# existing layout")
    assert setup.default_data_dir(plugin, plugin) == plugin


def test_systemd_single_checkout_uses_external_secret_and_environment(tmp_path):
    source, data = tmp_path / "plugins/codex_image", tmp_path / "private"
    unit = setup.systemd_unit(source, "/usr/bin/uv", "/root/.codex", "/usr/bin/codex", data_dir=data)
    assert 'Environment=' + setup.quote_unit('CODEX_IMAGE_SECRET_FILE=' + str(data / "secret.txt")) in unit
    assert 'Environment=' + setup.quote_unit('UV_PROJECT_ENVIRONMENT=' + str(data / "venv")) in unit
    assert setup.quote_unit(data / "service.toml") in unit
    assert setup.quote_unit(source / "service") in unit


def test_single_source_cli_uses_checkout_without_prompting_for_plugin_path(installation, monkeypatch):
    plugin, data, _ = single_checkout(installation)
    monkeypatch.setattr(setup, "ROOT", plugin)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    assert setup.main(["--data-dir", str(data), "--allowed-user", "qq:123", "--foreground"]) == 0
    assert (data / "service.toml").exists()
    assert (plugin / "config.toml").exists()


def test_root_guard_ignores_only_runner_toml_not_executable_files(installation, monkeypatch):
    repo, _, _ = installation
    config = repo / "config.toml"
    config.write_text("# container-owned configuration")
    original = Path.lstat
    monkeypatch.setattr(setup, "require_root_path", lambda path: None)
    monkeypatch.setattr(setup.os, "walk", lambda *a, **kw: [(str(repo), [], ["config.toml"])])
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_uid=1000, st_mode=0o100600)
                        if path == config else original(path))
    setup.validate_root_install(repo, "/usr/bin/uv", "/usr/bin/codex")
