"""Runtime configuration.

Every setting is an environment variable so that the same code runs in the all-in-one
development mode, in Docker Compose and in a distributed Kubernetes deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICES = (
    "pdp",
    "audit",
    "vault",
    "registry",
    "program",
    "broker",
    "eligibility",
    "breakglass",
    "payments",
    "retention",
)


def _repo_root() -> Path:
    # src/pbd_spmis/config.py -> repo root
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Process-wide settings, read once from the environment."""

    # "allinone": every service is mounted in one ASGI app and inter-service calls are
    # dispatched in-process. "distributed": inter-service calls go to <SERVICE>_URL.
    service_mode: str = field(default_factory=lambda: os.getenv("PBD_SERVICE_MODE", "allinone"))
    catalog_dir: Path = field(
        default_factory=lambda: Path(os.getenv("PBD_CATALOG_DIR", str(_repo_root() / "catalog")))
    )
    # SQLAlchemy URL template. "{service}" is replaced by the service name so that each service
    # keeps its own database (or schema) as the architecture requires.
    database_url: str = field(
        default_factory=lambda: os.getenv("PBD_DATABASE_URL", "sqlite:///./data/{service}.sqlite3")
    )
    # Policy engine for the PDP: "embedded" (pure Python reference evaluator) or "opa"
    # (delegates to an Open Policy Agent instance at PBD_OPA_URL running policy/rego).
    policy_engine: str = field(default_factory=lambda: os.getenv("PBD_POLICY_ENGINE", "embedded"))
    opa_url: str = field(default_factory=lambda: os.getenv("PBD_OPA_URL", "http://localhost:8181"))
    # HS256 signing key for development tokens. Production deployments should use the OIDC
    # provider configured through PBD_OIDC_* (see docs/deployment.md).
    auth_signing_key: str = field(
        default_factory=lambda: os.getenv("PBD_AUTH_SIGNING_KEY", "dev-only-change-me")
    )
    auth_issuer: str = field(default_factory=lambda: os.getenv("PBD_AUTH_ISSUER", "pbd-spmis-dev"))
    # Base64 32-byte master key used by the vault and payment stores to wrap per-record keys.
    vault_master_key: str = field(default_factory=lambda: os.getenv("PBD_VAULT_MASTER_KEY", ""))
    # HMAC key for deterministic identifier hashes used for deduplication lookups.
    index_hmac_key: str = field(default_factory=lambda: os.getenv("PBD_INDEX_HMAC_KEY", ""))
    fail_closed: bool = field(default_factory=lambda: os.getenv("PBD_FAIL_CLOSED", "true") == "true")
    log_level: str = field(default_factory=lambda: os.getenv("PBD_LOG_LEVEL", "INFO"))

    def service_url(self, service: str) -> str | None:
        return os.getenv(f"PBD_{service.upper()}_URL")

    def database_url_for(self, service: str) -> str:
        return self.database_url.replace("{service}", service)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Forget cached settings (used by tests that change environment variables)."""
    global _settings
    _settings = None
