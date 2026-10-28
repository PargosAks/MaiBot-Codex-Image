"""First-install configuration with fake binaries and Docker/systemd fixtures only."""

from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import shutil
import stat
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


@pytest.mark.parametrize("change", ["host", "key", "missing-key", "corrupt-key"])
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
    with pytest.raises(ValueError):
        setup.configure(repo, plugin, ["qq:123"], host="172.18.0.1" if change == "host" else "127.0.0.1")
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
    assert cfg["plugin"] == {"enabled": False, "config_version": "0.6.3"}
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
    cfg.write_text(cfg.read_text().replace('config_version = "0.6.3"', 'config_version = "0.5.0"'))
    lease = StateLease(data / "runtime/state")
    try:
        setup.configure(plugin, plugin, ["qq:123"], data_dir=data)
        assert tomllib.loads(cfg.read_text())["plugin"]["config_version"] == "0.6.3"
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


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_installs_without_user_argument_or_user_prompt(installation, monkeypatch, capsys, interactive):
    plugin, data, _ = single_checkout(installation)
    prompts = []

    def answer_container_only(prompt):
        prompts.append(prompt)
        assert prompt.startswith("MaiBot 容器名"), prompt
        return ""

    monkeypatch.setattr(setup, "ROOT", plugin)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", answer_container_only)
    assert setup.main(["--data-dir", str(data), "--foreground"]) == 0
    assert len(prompts) == int(interactive)
    cfg = tomllib.loads((plugin / "config.toml").read_text())
    assert cfg["connection"]["allowed_users"] == []
    assert "allowed_users" not in tomllib.loads((data / "service.toml").read_text())
    assert "MaiBot 插件面板配置 connection.allowed_users" in capsys.readouterr().out


@pytest.mark.parametrize("users", [[], ["qq:123"], ["qq：123", "not configured correctly"]])
def test_existing_webui_allowlist_is_preserved_without_validation(installation, users):
    repo, plugin, _ = installation
    configure(installation)
    config = setup.tomlkit.parse((plugin / "config.toml").read_text())
    config["connection"]["allowed_users"] = users
    (plugin / "config.toml").write_text(setup.tomlkit.dumps(config), encoding="utf-8")
    protected = [repo / "secret.txt", repo / "service.toml", plugin / "config.toml"]
    before = [path.read_bytes() for path in protected]
    lease = StateLease(repo / "runtime/state")
    try:
        # Even old automation supplying a different/invalid user must not take
        # ownership of the list that the operator manages in the WebUI.
        setup.configure(repo, plugin, ["qq：ignored-legacy-option"])
        assert [path.read_bytes() for path in protected] == before
    finally:
        lease.close()


def test_webui_users_set_before_first_pairing_survive(installation):
    plugin, data, _ = single_checkout(installation)
    config = setup.tomlkit.parse((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    config["connection"]["allowed_users"] = ["qq:123"]
    (plugin / "config.toml").write_text(setup.tomlkit.dumps(config), encoding="utf-8")
    setup.configure(plugin, plugin, data_dir=data)
    paired = tomllib.loads((plugin / "config.toml").read_text())
    assert paired["connection"]["allowed_users"] == ["qq:123"]
    assert paired["connection"]["secret"] == (data / "secret.txt").read_text().strip()


def test_legacy_first_prefill_adds_missing_list_to_paired_config(installation):
    repo, plugin, _ = installation
    configure(installation)
    config = setup.tomlkit.parse((plugin / "config.toml").read_text())
    del config["connection"]["allowed_users"]
    (plugin / "config.toml").write_text(setup.tomlkit.dumps(config), encoding="utf-8")
    protected = [repo / "secret.txt", repo / "service.toml"]
    before = [path.read_bytes() for path in protected]
    lease = StateLease(repo / "runtime/state")
    try:
        setup.configure(repo, plugin, ["qq:789"])
        assert tomllib.loads((plugin / "config.toml").read_text())["connection"]["allowed_users"] == ["qq:789"]
        assert [path.read_bytes() for path in protected] == before
    finally:
        lease.close()


@pytest.fixture
def uv_lock_tree(tmp_path, monkeypatch):
    root = tmp_path / "host-data"
    environment = root / "venv"
    environment.mkdir(parents=True)
    lock = environment / ".lock"
    lock.write_bytes(b"")
    original = Path.lstat
    overrides = {lock: SimpleNamespace(st_uid=0, st_mode=0o100666)}
    monkeypatch.setattr(setup, "require_root_path", lambda path: None)
    def posix_lstat(path, *args, **kwargs):
        if path in overrides:
            return overrides[path]
        info = original(path, *args, **kwargs)
        mode = stat.S_IFMT(info.st_mode) | (0o755 if stat.S_ISDIR(info.st_mode) else 0o644)
        return SimpleNamespace(st_uid=0, st_mode=mode, st_size=info.st_size)

    monkeypatch.setattr(Path, "lstat", posix_lstat)
    return root, environment, lock, overrides


def test_exact_registered_uv_lock_accepts_normal_0666_mode_without_chmod(uv_lock_tree):
    root, environment, lock, _ = uv_lock_tree
    before = lock.read_bytes()
    setup.validate_root_install(root, "/usr/bin/uv", "/usr/bin/codex", environment=environment)
    assert lock.read_bytes() == before
    with pytest.raises(ValueError, match="其他用户可写"):
        setup.validate_root_install(root, "/usr/bin/uv", "/usr/bin/codex")


@pytest.mark.parametrize("uid,mode", [(1001, 0o100666), (0, 0o120777), (0, 0o40755),
                                    (0, 0o010666), (0, 0o100777)])
def test_uv_lock_exception_rejects_nonroot_link_directory_fifo_and_executable(uv_lock_tree, uid, mode):
    root, environment, lock, overrides = uv_lock_tree
    overrides[lock] = SimpleNamespace(st_uid=uid, st_mode=mode)
    with pytest.raises(ValueError, match="普通非可执行文件"):
        setup.validate_root_install(root, "/usr/bin/uv", "/usr/bin/codex", environment=environment)


@pytest.mark.parametrize("relative", ["runtime/.lock", "venv/lib/.lock", "venv/lib/imported.py", "venv/bin/python"])
def test_uv_lock_exception_does_not_allow_other_writable_files(uv_lock_tree, relative):
    root, environment, _, overrides = uv_lock_tree
    unsafe = root / relative
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.write_bytes(b"do not execute")
    overrides[unsafe] = SimpleNamespace(st_uid=0, st_mode=0o100666)
    with pytest.raises(ValueError, match="其他用户可写"):
        setup.validate_root_install(root, "/usr/bin/uv", "/usr/bin/codex", environment=environment)


def test_uv_lock_exception_still_rejects_replaceable_environment_directory(uv_lock_tree):
    root, environment, _, overrides = uv_lock_tree
    overrides[environment] = SimpleNamespace(st_uid=0, st_mode=0o40777)
    with pytest.raises(ValueError, match="其他用户可写"):
        setup.validate_root_install(root, "/usr/bin/uv", "/usr/bin/codex", environment=environment)


def test_root_permission_error_identifies_actual_uid_and_mode(installation, monkeypatch):
    repo, _, _ = installation
    executable = repo / "native-codex"
    executable.write_bytes(b"never execute")
    original = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: SimpleNamespace(st_uid=1001, st_mode=0o100755)
                        if path == executable else original(path, *a, **kw))
    with pytest.raises(ValueError, match=r"UID=1001，权限=0755"):
        setup.require_root_path(executable)


def test_service_sync_does_not_inherit_setup_tool_virtual_environment(installation, monkeypatch):
    repo, _, _ = installation
    data = repo.parent / "private-data"
    monkeypatch.setenv("VIRTUAL_ENV", "/temporary/setup-environment")
    calls = []
    monkeypatch.setattr(setup.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    setup.sync_environment(repo, data, "/usr/bin/uv")
    argv, kwargs = calls[0]
    assert kwargs["check"] is True and "--active" not in argv
    assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(data / "venv")
    assert "VIRTUAL_ENV" not in kwargs["env"]
    assert setup.os.environ["VIRTUAL_ENV"] == "/temporary/setup-environment"


def test_first_install_can_resume_after_post_uv_validation_failed(installation, monkeypatch):
    repo, plugin, home = installation
    target = repo.parent / "units" / setup.UNIT_NAME
    target.parent.mkdir()
    monkeypatch.setattr(setup, "systemd_preflight", lambda *a, **kw: target)
    monkeypatch.setattr(setup, "install_systemd", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "start_systemd", lambda: None)
    syncs, validations = [], []

    def synced(*args):
        syncs.append(args)
        executable = repo / "venv" / ("Scripts/python.exe" if setup.os.name == "nt" else "bin/python")
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"synthetic environment")

    def validated(*args, **kwargs):
        validations.append(kwargs)
        if len(validations) == 1:
            raise ValueError("synthetic old uv lock false positive")

    monkeypatch.setattr(setup, "sync_environment", synced)
    monkeypatch.setattr(setup, "validate_root_install", validated)
    before_home = (home / "config.toml").read_bytes()
    with pytest.raises(ValueError, match="false positive"):
        setup.configure(repo, plugin, use_systemd=True, prepare_environment=True)
    state = repo / "runtime/state"
    assert {path.name for path in state.iterdir()} == {".service.lock"}
    assert not (repo / "secret.txt").exists() and not (repo / "service.toml").exists()
    setup.configure(repo, plugin, use_systemd=True, prepare_environment=True)
    assert len(syncs) == 2  # An incomplete first install rechecks its cached environment.
    assert validations[-1]["environment"] == repo / "venv"
    assert (repo / "secret.txt").exists() and (repo / "service.toml").exists()
    assert tomllib.loads((repo / "service.toml").read_text())["state"] == str(state)
    assert (home / "config.toml").read_bytes() == before_home


@pytest.mark.parametrize("content", ["jobs.sqlite3", "artifacts", "codex-runtime", "unknown-file", "nonempty-lock"])
def test_missing_key_with_real_or_unknown_state_still_refuses_generation(installation, content):
    repo, plugin, _ = installation
    state = repo / "runtime/state"
    state.mkdir(parents=True)
    if content in {"artifacts", "codex-runtime"}:
        (state / content).mkdir()
    else:
        (state / (".service.lock" if content == "nonempty-lock" else content)).write_bytes(b"existing state")
    with pytest.raises(ValueError, match="原密钥"):
        setup.configure(repo, plugin)
    assert not (repo / "secret.txt").exists()


def test_missing_key_with_dangling_state_link_still_refuses_generation(installation, monkeypatch):
    repo, plugin, _ = installation
    state = repo / "runtime/state"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == state or original(path))
    with pytest.raises(ValueError, match="原密钥"):
        setup.configure(repo, plugin)
    assert not (repo / "secret.txt").exists()


def test_empty_state_lock_link_is_not_treated_as_pristine(installation, monkeypatch):
    repo, plugin, _ = installation
    state = repo / "runtime/state"
    state.mkdir(parents=True)
    lock = state / ".service.lock"
    lock.write_bytes(b"")
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: SimpleNamespace(st_mode=0o120777, st_size=0)
                        if path == lock else original(path, *a, **kw))
    with pytest.raises(ValueError, match="原密钥"):
        setup.configure(repo, plugin)
    assert not (repo / "secret.txt").exists()


@pytest.mark.parametrize("keep_metadata", [False, True])
def test_missing_paired_service_config_refuses_resync_or_replacement(installation, monkeypatch, keep_metadata):
    repo, plugin, _ = installation
    configure(installation)
    (repo / "service.toml").unlink()
    if not keep_metadata:
        (repo / "runtime/setup-host.json").unlink()
    key, config = (repo / "secret.txt").read_bytes(), (plugin / "config.toml").read_bytes()

    def forbidden(*args):
        raise AssertionError("Do not resync an environment whose original service paths are missing")

    monkeypatch.setattr(setup, "sync_environment", forbidden)
    with pytest.raises(ValueError, match="恢复原服务配置"):
        setup.configure(repo, plugin, prepare_environment=True)
    assert not (repo / "service.toml").exists()
    assert (repo / "secret.txt").read_bytes() == key and (plugin / "config.toml").read_bytes() == config


@pytest.mark.parametrize("argv,stage", [
    (["systemd-analyze", "verify", "/private/maibot-codex-image.service"], "服务单元校验"),
    (["systemctl", "daemon-reload"], "配置重载"),
    (["systemctl", "enable", "--now", setup.UNIT_NAME], "服务启用与启动"),
    (["systemctl", "is-active", setup.UNIT_NAME], "运行状态检查"),
    (["/usr/local/bin/uv", "sync", "--project", "/private/service"], "服务依赖同步"),
    (["docker", "exec", "private-container", "id", "-u"], "容器用户或访问检查"),
])
def test_cli_reports_failed_stage_and_exit_code_without_captured_secrets(monkeypatch, capsys, argv, stage):
    private = "synthetic-private-secret-never-print"

    def failed(argv=None):
        raise subprocess.CalledProcessError(7, command, output=private, stderr=private)

    command = argv
    monkeypatch.setattr(setup, "main", failed)
    assert setup.cli([]) == 1
    output = capsys.readouterr()
    assert stage in output.err and "退出码 7" in output.err
    assert "CalledProcessError" not in output.err and private not in output.err + output.out
    assert "private-container" not in output.err


def test_unknown_command_and_timeout_do_not_echo_arbitrary_arguments_or_output():
    private = "synthetic-secret-in-command-output"
    unknown = subprocess.CalledProcessError(3, [private, private], output=private, stderr=private)
    assert private not in setup.describe_setup_error(unknown)
    timeout = subprocess.TimeoutExpired(["systemd-analyze", "verify", private], 60, output=private, stderr=private)
    description = setup.describe_setup_error(timeout)
    assert "执行超时" in description and private not in description


def test_unit_verify_failure_offers_shell_quoted_read_only_check():
    path = Path("/private folder/maibot-codex-image.service")
    error = subprocess.CalledProcessError(1, ["systemd-analyze", "verify", str(path)], stderr="do not echo raw stderr")
    description = setup.describe_setup_error(error)
    assert setup.shlex.join(["systemd-analyze", "verify", str(path)]) in description
    assert "只读检查" in description and "do not echo raw stderr" not in description


def test_unit_verify_failure_never_publishes_or_starts_after_failure(installation, monkeypatch, capsys):
    repo, plugin, _ = installation
    target = repo.parent / "units" / setup.UNIT_NAME
    target.parent.mkdir()
    monkeypatch.setattr(setup, "systemd_preflight", lambda *a, **kw: target)
    calls = []

    def reject_unit(argv):
        calls.append(argv)
        assert argv[:2] == ["systemd-analyze", "verify"]
        raise subprocess.CalledProcessError(1, argv, stderr="synthetic hidden diagnostic")

    monkeypatch.setattr(setup, "command", reject_unit)
    monkeypatch.setattr(setup, "main", lambda argv: setup.configure(repo, plugin, use_systemd=True))
    assert setup.cli([]) == 1
    output = capsys.readouterr()
    assert "正在校验 systemd" in output.out and "服务单元校验失败" in output.err
    assert len(calls) == 1 and not target.exists()
    assert (repo / "service.toml").exists() and (repo / "secret.txt").exists()
    assert (repo / "runtime" / setup.UNIT_NAME).exists()
    lease = StateLease(repo / "runtime/state")
    lease.close()


def test_working_directory_is_a_bare_path_while_exec_and_environment_stay_quoted():
    root = Path("/opt/a directory/plugins/codex_image")
    unit = setup.systemd_unit(root, "/usr/bin/uv", "/root/.codex", "/usr/bin/codex")
    line = next(line for line in unit.splitlines() if line.startswith("WorkingDirectory="))
    assert line == "WorkingDirectory=" + root.as_posix()
    assert 'Environment="CODEX_HOME=/root/.codex"' in unit
    assert 'ExecStart="/usr/bin/uv" run --project ' + setup.quote_unit(root / "service") in unit


@pytest.mark.parametrize("suffix", ["trailing ", "tab\tname", "line\nname", "escape\x1bname", "percent%name"])
def test_working_directory_rejects_unsupported_path_control_or_expansion(suffix):
    with pytest.raises(ValueError):
        setup.unit_directory(Path("/opt") / suffix)
