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
    search_provider: str | None = None
    search_base_url: str | None = None
    search_timeout_seconds: float = 5.0
    search_max_results: int = 10
    search_language: str = "uk-UA"
    search_max_query_length: int = 200
    search_max_title_length: int = 300
    search_max_snippet_length: int = 500

    def __post_init__(self) -> None:
        validate_loopback_host(self.host)
        if not 1 <= self.port <= 65535:
            raise ValueError("AI_ASSISTANCE_PORT must be between 1 and 65535")
        if not 0 < self.search_timeout_seconds <= 60:
            raise ValueError("AI_ASSISTANCE_SEARCH_TIMEOUT must be between 0 and 60 seconds")
        if not 1 <= self.search_max_results <= 100:
            raise ValueError("AI_ASSISTANCE_SEARCH_MAX_RESULTS must be between 1 and 100")
        if not isinstance(self.search_language, str) or not self.search_language.strip():
            raise ValueError("AI_ASSISTANCE_SEARCH_LANGUAGE must not be empty")
        if not 1 <= self.search_max_query_length <= 1000:
            raise ValueError("AI_ASSISTANCE_SEARCH_MAX_QUERY_LENGTH must be between 1 and 1000")
        if not 1 <= self.search_max_title_length <= 1000:
            raise ValueError("AI_ASSISTANCE_SEARCH_MAX_TITLE_LENGTH must be between 1 and 1000")
        if not 1 <= self.search_max_snippet_length <= 5000:
            raise ValueError("AI_ASSISTANCE_SEARCH_MAX_SNIPPET_LENGTH must be between 1 and 5000")
        if self.search_provider is not None:
            if not isinstance(self.search_provider, str) or not self.search_provider.strip():
                raise ValueError("AI_ASSISTANCE_SEARCH_PROVIDER must not be empty")
            if self.search_provider != "searxng":
                raise ValueError("unsupported search provider")
            if not self.search_base_url or not self.search_base_url.strip():
                raise ValueError("AI_ASSISTANCE_SEARCH_BASE_URL is required when a provider is configured")

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
            search_provider=os.environ.get("AI_ASSISTANCE_SEARCH_PROVIDER") or None,
            search_base_url=os.environ.get("AI_ASSISTANCE_SEARCH_BASE_URL") or None,
            search_timeout_seconds=float(os.environ.get("AI_ASSISTANCE_SEARCH_TIMEOUT", "5")),
            search_max_results=int(os.environ.get("AI_ASSISTANCE_SEARCH_MAX_RESULTS", "10")),
            search_language=os.environ.get("AI_ASSISTANCE_SEARCH_LANGUAGE", "uk-UA"),
            search_max_query_length=int(os.environ.get("AI_ASSISTANCE_SEARCH_MAX_QUERY_LENGTH", "200")),
            search_max_title_length=int(os.environ.get("AI_ASSISTANCE_SEARCH_MAX_TITLE_LENGTH", "300")),
            search_max_snippet_length=int(os.environ.get("AI_ASSISTANCE_SEARCH_MAX_SNIPPET_LENGTH", "500")),
        )

