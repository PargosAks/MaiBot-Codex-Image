from dataclasses import dataclass
from pathlib import Path
import os
import tomllib
import warnings

from .client import auth_home, find_codex


def _secret() -> str:
    # An explicitly set environment secret wins, including an invalid empty value.
    if "CODEX_IMAGE_SECRET" in os.environ:
        return os.environ["CODEX_IMAGE_SECRET"]
    secret_file = os.environ.get("CODEX_IMAGE_SECRET_FILE", "")
    if not secret_file:
        return ""
    try:
        return Path(secret_file).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Cannot read CODEX_IMAGE_SECRET_FILE as a UTF-8 secret file") from exc


@dataclass(frozen=True)
class Settings:
    binary: str
    home: Path
    state: Path
    secret: str
    model: str = ""
    timeout: float = 300
    rpc_timeout: float = 30
    max_queue: int = 10
    max_image_bytes: int = 20 * 1024 * 1024
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def load(cls, path: Path) -> "Settings":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "allowed_users" in data:
            data.pop("allowed_users")
            warnings.warn(
                "Service setting 'allowed_users' is ignored; configure user authorization "
                "in the plugin's connection.allowed_users instead.",
                UserWarning,
                stacklevel=2,
            )
        # Discovery belongs to configuration loading, not direct Settings construction.
        data.setdefault("binary", "")
        data["home"] = auth_home(data.get("home", ""))
        cfg = cls(**data, secret=_secret()).validated()
        object.__setattr__(cfg, "binary", find_codex(cfg.binary))
        return cfg

    def validated(self) -> "Settings":
        if len(self.secret) < 32 or not self.secret.strip():
            raise ValueError(
                "Configure CODEX_IMAGE_SECRET or CODEX_IMAGE_SECRET_FILE with a non-empty secret "
                "of at least 32 characters"
            )
        if self.max_queue < 1 or self.timeout <= 0 or self.rpc_timeout <= 0:
            raise ValueError("Invalid queue or timeout settings")
        object.__setattr__(self, "home", Path(self.home).expanduser().resolve())
        object.__setattr__(self, "state", Path(self.state).expanduser().resolve())
        return self
