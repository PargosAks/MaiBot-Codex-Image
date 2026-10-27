"""Management commands generated into a deployment; secrets stay in local files."""
from pathlib import Path
import argparse
import asyncio
import json
import os
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["login", "login-status", "start", "doctor", "maibot"])
    parser.add_argument("--device-auth", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    state = json.loads((root / "deployment.json").read_text(encoding="utf-8"))
    env = dict(os.environ)
    env["CODEX_HOME"] = str(root / "codex-home")
    env["PYTHONUTF8"] = "1"
    for key in ("OPENAI_API_KEY", "OPENAI_ACCESS_TOKEN", "CODEX_ACCESS_TOKEN"):
        env.pop(key, None)
    if args.command in {"login", "login-status"}:
        command = [state["binary"], "login", "-c", 'cli_auth_credentials_store="file"']
        if args.command == "login-status":
            command.append("status")
        elif args.device_auth:
            command.append("--device-auth")
        return subprocess.call(command, env=env)
    if args.command == "maibot":
        if not state.get("maibot_exe"):
            print("Start your existing MaiBot instance normally; this installer did not own its runtime.")
            return 1
        return subprocess.call([state["maibot_exe"], "run", "--directory", state["instance"]], env=env)
    secret = (root / "secret.txt").read_text(encoding="utf-8").strip()
    env["CODEX_IMAGE_SECRET"] = secret
    from codex_image_mcp.codex import Codex, Rpc
    from codex_image_mcp.config import Settings
    os.environ["CODEX_IMAGE_SECRET"] = secret
    settings = Settings.load(root / "service.toml")

    async def check():
        if (settings.home / "config.toml").exists():
            raise ValueError("Dedicated CODEX_HOME contains config.toml; use an auth-only home")
        driver = Codex(settings)
        rpc = Rpc([settings.binary, "app-server", "--listen", "stdio://"], driver.environment(), settings.rpc_timeout)
        try:
            await rpc.start()
            await rpc.call("initialize", {"clientInfo": {"name": "maibot_image_setup_check", "version": "0.1.0"}})
            await rpc.send({"method": "initialized"})
            account = await rpc.call("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ValueError("ChatGPT login required. Run Login-Codex first; no API key is accepted.")
        finally:
            await rpc.close()
    try:
        asyncio.run(check())
    except Exception as exc:
        print("Not ready: " + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        return 2
    print("Ready: official app-server responds and ChatGPT login is present. No image was generated.")
    if args.command == "doctor":
        return 0
    # Keep one serving process per deployment without terminating unrelated processes.
    lock = (root / "service.lock").open("a+b")
    lock.seek(0, 2)
    if lock.tell() == 0:
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("The image service is already running for this deployment.")
        return 1
    from codex_image_mcp.server import create_app
    import uvicorn
    try:
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
