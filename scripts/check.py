"""统一开发检查入口；沿用当前解释器和 pytest 的选择、报告选项。"""

from pathlib import Path
import argparse
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "unit", "local", "external"), default="all")
    parser.add_argument("--no-lint", action="store_true", help="只运行测试，适合已完成静态检查的定向回归")
    options, pytest_args = parser.parse_known_args()
    if not options.no_lint:
        result = subprocess.run([
            sys.executable, "-m", "ruff", "check", "--select", "E4,E7,E9,F",
            "plugin.py", "domain", "creation", "host", "inspiration", "mcp_client", "workspace", "scripts", "tests",
        ], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    # 参数列表直接交给 pytest，不通过 shell 拼接；-k、路径及 JUnit 选项均可透传。
    return subprocess.run([
        sys.executable, "-m", "pytest", "-q", f"--suite={options.suite}", *pytest_args,
    ], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
