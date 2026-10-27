from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


@dataclass(frozen=True)
class Settings:
    binary: str
    home: Path
    state: Path
    secret: str
    allowed_users: tuple[str, ...]
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
        cfg = cls(**data, secret=os.environ.get("CODEX_IMAGE_SECRET", ""))
        return cfg.validated()

    def validated(self) -> "Settings":
        if len(self.secret) < 32 or not self.allowed_users:
            raise ValueError(
                "Configure a secret of at least 32 characters and explicit allowed_users"
            )
        if self.max_queue < 1 or self.timeout <= 0 or self.rpc_timeout <= 0:
            raise ValueError("Invalid queue or timeout settings")
        object.__setattr__(self, "home", Path(self.home).expanduser().resolve())
        object.__setattr__(self, "state", Path(self.state).resolve())
        object.__setattr__(self, "allowed_users", tuple(self.allowed_users))
        return self
