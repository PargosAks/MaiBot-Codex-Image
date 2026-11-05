"""Exercise the release, not conftest's source package or its import cache.

The loader belongs to Host, not maibot-plugin-sdk. Set
CODEX_IMAGE_TEST_HOST_SITE_PACKAGES to a public installed Host package when it
is not available in this workspace's local test tools. Missing Host is an
explicit skip; there is deliberately no replacement plugin loader.

Host logging, instance paths and local-SDK discovery are isolated. Registration
executes three unchanged methods extracted from Host's Runner AST, with its
real protocol models and an in-memory RPC sink. This covers declaration and
registration payloads, not a running Host, permissions, browser or IPC delivery.
"""

# ruff: noqa: I001
# Parent AGENTS.md requires from-imports before plain imports.
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
import ast
import asyncio
import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import pkgutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest


PLUGIN = Path(__file__).resolve().parents[1]
PACKAGES = ("domain", "creation", "host", "inspiration", "mcp_client", "workspace")
HOST_ENV = "CODEX_IMAGE_TEST_HOST_SITE_PACKAGES"


@pytest.fixture
def release(tmp_path):
    spec = importlib.util.spec_from_file_location("layout_bundle_builder", PLUGIN / "scripts/build_bundle.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    [(archive_path, digest)] = builder.build(tmp_path / "dist")
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == digest
    root = tmp_path / "plugins" / "arbitrary-install-directory"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {f"{name}/__init__.py" for name in PACKAGES} <= names
        assert {"plugin.py", "webui.json", "assets/studio.html"} <= names
        # Old flat files must not accidentally make incomplete nested imports work.
        assert {name for name in names if "/" not in name and name.endswith(".py")} == {"plugin.py"}
        archive.extractall(root)
    return root


@pytest.fixture
def host_site_packages():
    configured = os.environ.get(HOST_ENV)
    if configured:
        root = Path(configured).resolve()
        assert (root / "src/plugin_runtime/runner/plugin_loader.py").is_file(), HOST_ENV
        return root
    candidates = [PLUGIN.parents[2] / "local-maibot/tools/maibot-py314/Lib/site-packages"]
    try:
        candidates.append(Path(importlib.metadata.distribution("maibot").locate_file("")))
    except importlib.metadata.PackageNotFoundError:
        pass
    for root in candidates:
        if (root / "src/plugin_runtime/runner/plugin_loader.py").is_file():
            return root.resolve()
    pytest.skip(f"Real public Host PluginLoader unavailable; set {HOST_ENV} (SDK alone has no Loader)")


def run_isolated(root, host_root, mode="reload"):
    # -I discards PYTHONPATH, user site and cwd; conftest has already imported the
    # checkout in the parent process, which must never satisfy a release import.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()),
         str(root), str(host_root), mode],
        cwd=root.parent.parent, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["forbidden_calls"] == []
    (root.parent.parent / "layout-report.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8",
    )
    return report


@pytest.mark.parametrize("alternate_id", [False, True], ids=["manifest-id", "arbitrary-package-name"])
def test_release_real_host_registration_and_fresh_reload(release, host_site_packages, alternate_id):
    if alternate_id:
        manifest_path = release / "_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Valid manifest ID with leading digit, dots and hyphens exercises Host's
        # generated package name, independently of both checkout and install name.
        manifest["id"] = "9-layout.other-plugin"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = run_isolated(release, host_site_packages)
    assert report["cycles"] == 2
    assert report["webui_entry"] == "assets/studio.html"
    assert report["purged_module_count"] > len(PACKAGES)


def test_missing_release_dependency_cannot_fall_back_to_checkout(release, host_site_packages):
    (release / "domain/policy.py").unlink()
    report = run_isolated(release, host_site_packages, "missing-dependency")
    assert report["rejected_missing_dependency"] is True


def _host_modules(host_root, sandbox):
    """Load unchanged public Host modules without starting its application."""
    host_version = next(dist.version for dist in importlib.metadata.distributions(path=[str(host_root)])
                        if dist.metadata["Name"].lower() == "maibot")
    for name in ("src", "src.common", "src.plugin_runtime", "src.plugin_runtime.runner",
                 "src.plugin_runtime.protocol"):
        module = ModuleType(name)
        module.__path__ = [str(host_root.joinpath(*name.split(".")))]
        sys.modules[name] = module
    sys.modules["src.plugin_runtime"].detect_host_application_version = lambda *_: host_version

    def stub(name, **attributes):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        sys.modules[name] = module

    stub("src.common.logger", get_logger=logging.getLogger)
    stub("src.common.startup_diagnostics", diagnostic_stage=lambda *a, **kw: contextlib.nullcontext())
    stub("src.common.paths", get_instance_root=lambda: sandbox,
         project_metadata_path=lambda *_: sandbox / "pyproject.toml")
    stub("src.plugin_runtime.local_sdk", read_local_sdk_version=lambda **kw: None)
    loader = importlib.import_module("src.plugin_runtime.runner.plugin_loader")
    webui = importlib.import_module("src.plugin_runtime.webui_schema")
    protocol = importlib.import_module("src.plugin_runtime.protocol.envelope")
    assert Path(loader.__file__).resolve() == host_root / "src/plugin_runtime/runner/plugin_loader.py"
    return loader, webui, protocol, host_version


def _registration_runner(host_root, webui, protocol, sink):
    """Execute real Runner registration; omit process startup/IPC/config access.

    AST selection preserves method bodies and decorators verbatim. An explicit
    method-set check makes Host API drift fail instead of silently weakening the
    test. PluginLoader itself is imported and executed normally, never extracted.
    """
    path = host_root / "src/plugin_runtime/runner/runner_main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"_register_plugin", "_get_plugin_default_config", "_get_plugin_config_schema"}
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and wanted <= {item.name for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}]
    assert len(owners) == 1, "Host Runner registration API changed"
    methods = [node for node in owners[0].body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    harness = ast.ClassDef(name="RegistrationRunner", bases=[], keywords=[], body=methods, decorator_list=[])
    selected = ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[]))
    namespace = {
        "Any": Any, "Dict": dict, "List": list, "cast": cast, "_ConfigAwarePlugin": object,
        "PluginMeta": object, "asyncio": asyncio, "logger": logging.getLogger("layout-registration"),
        "load_webui_extension": webui.load_webui_extension,
        "ComponentDeclaration": protocol.ComponentDeclaration,
        "LLMProviderDeclaration": protocol.LLMProviderDeclaration,
        "RegisterPluginPayload": protocol.RegisterPluginPayload,
    }
    exec(compile(selected, str(path), "exec"), namespace)  # noqa: S102 - unchanged local Host AST
    runner = namespace["RegistrationRunner"]()
    runner._rpc_client = sink
    return runner


def _plugin_modules(prefix):
    return {name: module for name, module in sys.modules.items()
            if name == prefix or name.startswith(prefix + ".")}


def _import_release_packages(prefix):
    # Include deferred adapters that disabled autonomy does not instantiate.
    # This is an import smoke test, without prescribing individual import edges.
    for name in PACKAGES:
        package = importlib.import_module(f"{prefix}.{name}")
        for entry in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(entry.name)


async def _check_release(root, host_root, mode, forbidden):
    from maibot_sdk import MaiBotPlugin
    from maibot_sdk.context import PluginContext, PluginPaths

    sandbox = root.parent.parent / "isolated-runtime"
    sandbox.mkdir()
    loader_module, webui, protocol, host_version = _host_modules(host_root, sandbox)
    loader = loader_module.PluginLoader(host_version=host_version)
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    plugin_id = manifest["id"]
    report = {"sdk_version": importlib.metadata.version("maibot-plugin-sdk"),
              "host_version": host_version, "loader": loader_module.__file__,
              "loader_sha256": hashlib.sha256(Path(loader_module.__file__).read_bytes()).hexdigest(),
              "plugin_version": manifest["version"], "forbidden_calls": forbidden}
    report["host_source_sha256"] = {
        name: hashlib.sha256((host_root / "src/plugin_runtime" / name).read_bytes()).hexdigest()
        for name in ("runner/runner_main.py", "runner/manifest_validator.py", "webui_schema.py",
                     "protocol/envelope.py")
    }
    # The subprocess starts without any checkout package, even though conftest
    # imported it in the parent. Also reject accidental source imports by path.
    assert not any(name.startswith("image_plugin_under_test") for name in sys.modules)
    assert all(Path(path).resolve() != PLUGIN for path in sys.path)
    if mode == "missing-dependency":
        assert loader.discover_and_load([str(root.parent)]) == []
        assert "domain.policy" in loader.failed_plugins[plugin_id]
        report["rejected_missing_dependency"] = True
        return report

    class RegistrationSink:
        def __init__(self):
            self.payloads = []

        async def send_request(self, method, *, plugin_id, payload, timeout_ms):
            assert method == "plugin.register_components"
            assert plugin_id == manifest["id"] and timeout_ms == 10000
            self.payloads.append(protocol.RegisterPluginPayload.model_validate(payload))
            return SimpleNamespace(error=None, payload={"accepted": True})

    async def deny_host(*args, **kwargs):
        forbidden.append("host RPC")
        raise AssertionError("No Host/model/render/send capabilities are permitted")

    sink = RegistrationSink()
    registration = _registration_runner(host_root, webui, protocol, sink)
    data_dir, runtime_dir = sandbox / "data", sandbox / "runtime"
    data_dir.mkdir()
    runtime_dir.mkdir()
    previous_modules = None
    previous_class = previous_config_class = previous_scope_class = None
    baseline_tasks = asyncio.all_tasks()
    for cycle in range(2):
        [meta] = loader.discover_and_load([str(root.parent)])
        assert loader.failed_plugins == {}
        plugin, prefix = meta.instance, meta.module_name
        assert isinstance(plugin, MaiBotPlugin)
        assert type(plugin).__module__ == prefix
        _import_release_packages(prefix)
        modules = _plugin_modules(prefix)
        assert all(Path(module.__file__).resolve().is_relative_to(root) for module in modules.values())
        if previous_modules is not None:
            assert modules.keys() == previous_modules.keys()
            assert all(modules[name] is not old for name, old in previous_modules.items())
            assert type(plugin) is not previous_class
            assert plugin.config_model is not previous_config_class
            assert sys.modules[prefix + ".domain.policy"].Scope is not previous_scope_class

        defaults = plugin.get_default_config()
        assert set(defaults) == {"plugin", "connection", "autonomy"}
        assert defaults["plugin"]["config_version"] == "0.11.0"
        assert defaults["autonomy"]["enabled"] is False
        assert defaults["autonomy"]["spaces"] == []
        assert defaults["connection"]["allowed_users"] == []
        plugin.set_plugin_config(defaults)
        plugin._set_context(PluginContext(plugin_id, rpc_call=deny_host,
                                         paths=PluginPaths(data_dir=data_dir, runtime_dir=runtime_dir)))
        assert await registration._register_plugin(meta)
        payload = sink.payloads[-1]
        assert payload.plugin_version == manifest["version"]
        assert payload.default_config == defaults
        assert set(payload.config_schema["sections"]) == {"plugin", "connection", "autonomy"}
        assert payload.config_schema["sections"]["plugin"]["fields"]["config_version"]["default"] == "0.11.0"
        autonomy = payload.config_schema["sections"]["autonomy"]["fields"]
        assert autonomy["enabled"]["default"] is False
        assert autonomy["persona"]["hidden"] is True
        assert {"profiles", "series"}.isdisjoint(autonomy["spaces"]["item_fields"])
        components = {component.name: component for component in payload.components}
        assert len(components) == len(payload.components)
        assert components["codex_image_command"].component_type == "COMMAND"
        assert components["codex_image_command"].metadata["permission"] == "operator"
        assert {"codex_image_artist_chat", "codex_image_studio_chat", "codex_image_studio_replyer",
                "codex_image_thought_offer", "codex_image_thought_record"} <= components.keys()
        for name, handler in meta.component_handlers.items():
            assert callable(getattr(plugin, handler)), name
        [page] = payload.webui.pages
        assert page.entry == "assets/studio.html"
        assert page.document == (root / page.entry).read_text(encoding="utf-8")
        assert page.document_revision == hashlib.sha256(page.document.encode("utf-8")).hexdigest()
        for query in page.queries.values():
            component = components[query.api]
            assert component.component_type == "API"
            assert component.metadata["public"] is False
            assert component.metadata["version"] == query.version

        loader.set_loaded_plugin(meta)
        journal = None
        try:
            await plugin.on_load()
            assert plugin.startup_error == ""
            assert plugin.bridge is None
            journal = plugin.journal
            assert journal is not None
            assert journal.db.execute("SELECT COUNT(*) FROM delivery").fetchone()[0] == 0
            assert plugin.artist_runtime.store is None
            assert plugin.artist_runtime.tasks == {}
            assert plugin.watchers == {}
            result = await plugin.studio_spaces()
            assert result["items"] == []
            assert asyncio.all_tasks() == baseline_tasks
        finally:
            await plugin.on_unload()
        assert plugin.journal is plugin.bridge is plugin.artist_runtime is None
        assert plugin.group_thought_runtime is plugin.studio_chat is None
        assert plugin.watchers == {}
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            journal.db.execute("SELECT 1")
        assert asyncio.all_tasks() == baseline_tasks
        assert loader.remove_loaded_plugin(plugin_id) is meta
        removed = loader.purge_plugin_modules(plugin_id, str(root))
        assert set(modules) <= set(removed)
        assert _plugin_modules(prefix) == {}
        assert loader.get_plugin(plugin_id) is None
        previous_modules = modules
        previous_class, previous_config_class = type(plugin), plugin.config_model
        previous_scope_class = modules[prefix + ".domain.policy"].Scope
        report.update(cycles=cycle + 1, purged_module_count=len(removed), webui_entry=page.entry,
                      module_name=prefix, config_version=defaults["plugin"]["config_version"],
                      registered_components=sorted(components))

    # The real registration path must reject an omitted nested resource instead
    # of finding the old root HTML in the checkout. No RPC payload may be emitted.
    html = root / "assets/studio.html"
    content = html.read_bytes()
    try:
        html.unlink()
        before = len(sink.payloads)
        assert await registration._register_plugin(meta) is False
        assert len(sink.payloads) == before
        report["rejected_missing_nested_html"] = True
    finally:
        html.write_bytes(content)
    assert not (data_dir / "artist.sqlite3").exists()
    assert not forbidden
    return report


def _worker():
    root, host_root, mode = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3]
    forbidden = []
    sandbox = root.parent.parent

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
                     "socket.sendto", "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            forbidden.append(event)
            raise AssertionError(f"External operation prohibited: {event}")
        if event == "sqlite3.connect":
            assert Path(args[0]).resolve().is_relative_to(sandbox), "SQLite outside isolated runtime"

    # Windows creates a local socketpair while constructing its event loop.
    # Install the network barrier after that, before Host or plugin imports.
    with asyncio.Runner() as runner:
        runner.get_loop()
        sys.addaudithook(audit)
        report = runner.run(_check_release(root, host_root, mode, forbidden))
    print(json.dumps(report))


if __name__ == "__main__":
    _worker()
