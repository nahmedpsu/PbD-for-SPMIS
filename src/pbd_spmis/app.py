"""All-in-one composition of every service into one ASGI application.

Each service keeps its own app, database and service identity; this module only mounts them under
``/<service>`` and registers them for in-process dispatch. The same services run as separate
processes in a distributed deployment with no code change.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from . import __version__
from .common.clients import clear_apps, register_app
from .config import SERVICES, get_settings

_FACTORIES = {
    "pdp": "pbd_spmis.pdp.app",
    "audit": "pbd_spmis.audit.app",
    "vault": "pbd_spmis.vault.app",
    "registry": "pbd_spmis.registry.app",
    "program": "pbd_spmis.program.app",
    "broker": "pbd_spmis.broker.app",
    "eligibility": "pbd_spmis.eligibility.app",
    "breakglass": "pbd_spmis.breakglass.app",
    "payments": "pbd_spmis.payments.app",
    "retention": "pbd_spmis.retention.app",
}


def service_app(service: str) -> FastAPI:
    import importlib

    module = importlib.import_module(_FACTORIES[service])
    return module.create_app()


def create_app() -> FastAPI:
    clear_apps()
    root = FastAPI(
        title="PbD-SPMIS (all-in-one)",
        version=__version__,
        description="Privacy-by-Design control plane and reference Social Protection MIS. "
        "Every service is mounted under its own prefix; see /<service>/docs.",
    )
    apps: dict[str, FastAPI] = {}
    for service in SERVICES:
        app = service_app(service)
        apps[service] = app
        register_app(service, app)
        root.mount(f"/{service}", app)

    @root.get("/")
    def index() -> dict[str, Any]:
        return {
            "name": "pbd-spmis",
            "version": __version__,
            "mode": get_settings().service_mode,
            "policy_engine": get_settings().policy_engine,
            "services": {s: {"docs": f"/{s}/docs", "openapi": f"/{s}/openapi.json"} for s in SERVICES},
        }

    @root.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "services": list(SERVICES)}

    return root


app = create_app()
