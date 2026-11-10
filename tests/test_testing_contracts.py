"""测试框架的来源约束，使用合成文件，不加载 Host。"""

import pytest

from support.host_source import HOST_REPO_ENV, HOST_SOURCE_ENV, host_file


def test_explicit_host_snapshot_drift_fails_even_with_a_git_repo_configured(tmp_path, monkeypatch):
    relative = "plugin_runtime/runner/runner_main.py"
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("class PluginRunner: pass\n", encoding="utf-8")
    monkeypatch.setenv(HOST_SOURCE_ENV, str(tmp_path))
    monkeypatch.setenv(HOST_REPO_ENV, str(tmp_path))
    # 错误必须指出目录快照的指纹差异，不能降级为 Git 读取或跳过测试。
    with pytest.raises(AssertionError, match=r"CODEX_IMAGE_TEST_HOST_SOURCE/.*does not match Host"):
        host_file(relative)



@pytest.fixture
def isolated_suites(pytester):
    pytester.makeini("""
        [pytest]
        addopts = --strict-markers -p no:asyncio
        markers =
            unit: isolated unit
            local_integration: local integration
            external_contract: external contract
    """)
    pytester.makeconftest('pytest_plugins = ("support.testing",)')
    return pytester


def test_suite_selection_partitions_cases_and_composes_with_native_mark_filter(isolated_suites):
    isolated_suites.makepyfile(test_cases="""
        import pytest

        @pytest.fixture
        def mcp_protocol_fixture():
            return object()

        @pytest.fixture
        def actual_mcp():
            return object()

        @pytest.fixture
        def prepared_host(mcp_protocol_fixture, actual_mcp):
            return object()

        def test_plain():
            pass

        @pytest.mark.parametrize("sdk", ["installed", pytest.param("snapshot", marks=pytest.mark.external_contract)])
        def test_sdk(sdk):
            pass

        def test_local(mcp_protocol_fixture):
            pass

        def test_host(prepared_host):
            pass
    """)
    expected = {
        "unit": {"test_plain", "test_sdk[installed]"},
        "local": {"test_local"},
        "external": {"test_host", "test_sdk[snapshot]"},
    }

    def selected(counts, *options):
        result = isolated_suites.runpytest_inprocess("--collect-only", "-q", *options)
        assert result.ret == pytest.ExitCode.OK
        result.stdout.fnmatch_lines([f"suite selection: {counts}"])
        return {line.split("::", 1)[1] for line in result.outlines if line.startswith("test_cases.py::")}

    assert selected("unit=2, local=1, external=2") == set().union(*expected.values())
    assert selected("unit=2, local=0, external=0", "--suite=unit") == expected["unit"]
    assert selected("unit=0, local=1, external=0", "--suite=local") == expected["local"]
    assert selected("unit=0, local=0, external=2", "--suite=external") == expected["external"]
    assert selected("unit=0, local=0, external=2", "-m", "external_contract") == expected["external"]


def test_require_external_checks_only_selected_external_execution(isolated_suites):
    isolated_suites.makepyfile(test_cases="""
        import pytest

        @pytest.fixture
        def actual_mcp():
            pytest.skip("public source absent")

        @pytest.mark.external_contract
        def test_external_ok():
            pass

        def test_external_setup_skip(actual_mcp):
            pytest.fail("setup should skip first")

        @pytest.mark.external_contract
        def test_external_call_skip():
            pytest.skip("runtime dependency absent")

        @pytest.mark.external_contract
        @pytest.mark.xfail(reason="contract still fails")
        def test_external_xfail():
            assert False

        def test_unit_skip():
            pytest.skip("optional local feature")
    """)
    optional = isolated_suites.runpytest_inprocess("-q", "--suite=external")
    assert optional.ret == pytest.ExitCode.OK
    optional.assert_outcomes(passed=1, skipped=2, xfailed=1, deselected=1)
    optional.stdout.fnmatch_lines(["suite selection: unit=0, local=0, external=4"])

    required = isolated_suites.runpytest_inprocess("-q", "--require-external")
    assert required.ret == pytest.ExitCode.TESTS_FAILED
    required.assert_outcomes(passed=1, skipped=3, xfailed=1)
    required.stdout.fnmatch_lines(["--require-external: 3 selected external contract case(s) skipped"])

    subset = isolated_suites.runpytest_inprocess("-q", "--require-external", "-k", "external_ok or unit_skip")
    assert subset.ret == pytest.ExitCode.OK
    subset.assert_outcomes(passed=1, skipped=1, deselected=3)

    empty = isolated_suites.runpytest_inprocess("-q", "--require-external", "-k", "unit_skip")
    assert empty.ret == pytest.ExitCode.TESTS_FAILED
    empty.stdout.fnmatch_lines(["--require-external: no external contract cases selected"])


def test_conflicting_explicit_suites_rejected_during_collection(isolated_suites):
    isolated_suites.makepyfile(test_cases="""
        import pytest

        @pytest.mark.unit
        @pytest.mark.external_contract
        def test_ambiguous():
            pytest.fail("classification must fail before execution")
    """)
    result = isolated_suites.runpytest_inprocess("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*conflicting suite markers: external_contract, unit*"])
