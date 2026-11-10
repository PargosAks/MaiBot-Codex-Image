"""pytest suite 选择、依赖分类和外部契约完成检查。"""

from collections import Counter
from dataclasses import dataclass, field

import pytest


SUITES = {
    "unit": "unit",
    "local": "local_integration",
    "external": "external_contract",
}
# 这里只声明真正持有外部资源的 fixture。依赖它们的间接 fixture 自动继承分类。
EXTERNAL_FIXTURES = frozenset({
    "actual_mcp", "dashboard_source", "host_site_packages", "host_source", "public_host", "public_transport",
})
LOCAL_FIXTURES = frozenset({"mcp_protocol_fixture", "reference_protocol"})
RANK = {marker: rank for rank, marker in enumerate(SUITES.values())}
CASE_SUITE = pytest.StashKey[str]()


@dataclass
class Selection:
    counts: Counter[str] = field(default_factory=Counter)
    skipped_external: set[str] = field(default_factory=set)
    error: str | None = None


SELECTION = pytest.StashKey[Selection]()


def pytest_addoption(parser):
    group = parser.getgroup("codex-image")
    group.addoption("--suite", choices=("all", *SUITES), default="all",
                    help="Select all, unit, local integration, or external contract tests.")
    group.addoption("--require-external", action="store_true",
                    help="Fail when no external contract is selected or a selected external case is skipped.")


def pytest_configure(config):
    if config.getoption("require_external") and config.getoption("collectonly"):
        raise pytest.UsageError("--require-external requires execution; omit --collect-only")
    config.stash[SELECTION] = Selection()


def _classify(item):
    explicit = {mark.name for mark in item.iter_markers() if mark.name in RANK}
    if len(explicit) > 1:
        raise pytest.UsageError(f"{item.nodeid}: conflicting suite markers: {', '.join(sorted(explicit))}")
    dependencies = set(getattr(item, "fixturenames", ()))
    inferred = ("external_contract" if dependencies & EXTERNAL_FIXTURES else
                "local_integration" if dependencies & LOCAL_FIXTURES else "unit")
    if not explicit:
        item.add_marker(inferred)
        return inferred
    declared = next(iter(explicit))
    if RANK[declared] < RANK[inferred]:
        raise pytest.UsageError(f"{item.nodeid}: {declared} conflicts with required {inferred} fixture")
    return declared


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(config, items):
    # 在 pytest 原生 -m 筛选前添加标记，筛选完成后再取本次 suite 的交集。
    for item in items:
        item.stash[CASE_SUITE] = _classify(item)
    result = yield
    selected = config.getoption("suite")
    if selected != "all":
        keep, deselect = [], []
        for item in items:
            (keep if item.stash[CASE_SUITE] == SUITES[selected] else deselect).append(item)
        if deselect:
            config.hook.pytest_deselected(items=deselect)
            items[:] = keep
    return result


def pytest_collection_finish(session):
    session.config.stash[SELECTION].counts.update(
        marker for item in session.items if (marker := item.stash.get(CASE_SUITE, None)) is not None
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    # 读最终报告，覆盖标记、fixture 和用例内跳过；xfail 也不构成外部契约通过。
    if (report.skipped and item.config.getoption("require_external")
            and item.stash[CASE_SUITE] == "external_contract"):
        item.config.stash[SELECTION].skipped_external.add(item.nodeid)
    return report


def pytest_sessionfinish(session, exitstatus):
    if not session.config.getoption("require_external"):
        return
    selection = session.config.stash[SELECTION]
    if selection.skipped_external:
        selection.error = f"{len(selection.skipped_external)} selected external contract case(s) skipped"
    elif not selection.counts["external_contract"]:
        selection.error = "no external contract cases selected"
    if selection.error and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, config):
    selection = config.stash[SELECTION]
    counts = ", ".join(f"{suite}={selection.counts[mark]}" for suite, mark in SUITES.items())
    terminalreporter.write_line(f"suite selection: {counts}")
    if selection.error:
        terminalreporter.write_line(f"--require-external: {selection.error}", red=True)
