"""Run manually in a terminal; never called by plugin startup."""

import argparse
import os
import subprocess
import sys

from client import auth_home, find_codex


def main():
    parser = argparse.ArgumentParser(description="检查或手动登录 Codex；默认复用当前 CODEX_HOME 或 ~/.codex")
    parser.add_argument("--binary", default="", help="Codex 原生程序路径")
    parser.add_argument("--home", default="", help="可选专用登录目录；需与插件 codex.home 一致")
    parser.add_argument("--device-auth", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    home = auth_home(args.home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ, CODEX_HOME=str(home))
    for key in ("OPENAI_API_KEY", "OPENAI_ACCESS_TOKEN", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    # Preserve the existing credential store (including keyring). Do not force a different login.
    argv = [find_codex(args.binary), "login"]
    if args.status:
        argv.append("status")
    elif args.device_auth:
        argv.append("--device-auth")
    print("Codex 登录目录：" + str(home), flush=True)
    return subprocess.call(argv, env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
