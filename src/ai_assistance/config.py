from dataclasses import dataclass
import ipaddress
from pathlib import Path
import os


def validate_loopback_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("host must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("host must be a loopback IP address")
    return host


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    worker_limit: int = 2
    max_workers: int = 4

    def __post_init__(self) -> None:
        validate_loopback_host(self.host)
        if not 1 <= self.port <= 65535:
            raise ValueError("AI_ASSISTANCE_PORT must be between 1 and 65535")

    @classmethod
    def from_environment(cls, project_root: Path | None = None) -> "Settings":
        root = project_root or Path.cwd()
        database_path = Path(os.environ.get("AI_ASSISTANCE_DB_PATH", root / "data" / "price_checker.sqlite3"))
        worker_limit = int(os.environ.get("AI_ASSISTANCE_WORKERS", "2"))
        if not 1 <= worker_limit <= 4:
            raise ValueError("AI_ASSISTANCE_WORKERS must be between 1 and 4")
        return cls(
            database_path=database_path,
            host=os.environ.get("AI_ASSISTANCE_HOST", "127.0.0.1"),
            port=int(os.environ.get("AI_ASSISTANCE_PORT", "8765")),
            worker_limit=worker_limit,
        )

