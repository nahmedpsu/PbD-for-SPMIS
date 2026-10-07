"""Command-line interface.

pbd-spmis serve all|<service>     run the all-in-one app or a single service with uvicorn
pbd-spmis demo                    run the end-to-end privacy walkthrough in-process
pbd-spmis catalog validate        validate catalog/*.yaml
pbd-spmis catalog bundle          build policy/bundle/data.json for OPA
pbd-spmis token ...               issue a development token
pbd-spmis contracts export        regenerate contracts/openapi/*.yaml
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(
    help="PbD-SPMIS: Privacy-by-Design control plane and reference SP-MIS.", no_args_is_help=True
)
catalog_app = typer.Typer(help="Privacy catalogue tools.", no_args_is_help=True)
contracts_app = typer.Typer(help="API contract tools.", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")
app.add_typer(contracts_app, name="contracts")

ROOT = Path(__file__).resolve().parents[2]


@app.command()
def serve(
    target: str = typer.Argument("all", help="'all' or one of the service names"),
    host: str = "0.0.0.0",  # noqa: S104 - container default
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Run the all-in-one application or a single service."""
    import uvicorn

    from .config import SERVICES

    if target == "all":
        uvicorn.run("pbd_spmis.app:app", host=host, port=port, reload=reload)
        return
    if target not in SERVICES:
        raise typer.BadParameter(f"unknown service '{target}'; choose from {', '.join(SERVICES)}")
    from .app import service_app

    uvicorn.run(service_app(target), host=host, port=port)


@app.command()
def demo(json_output: bool = typer.Option(False, "--json", help="machine-readable output")) -> None:
    """Run the end-to-end privacy walkthrough against fresh in-memory databases."""
    from .demo import run_demo

    run_demo(json_output=json_output)


@catalog_app.command("validate")
def catalog_validate(directory: Path = typer.Option(ROOT / "catalog", "--dir")) -> None:
    from .catalog.loader import merge_catalog_files, validate_catalog

    data = merge_catalog_files(directory)
    problems = validate_catalog(data)
    if problems:
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"catalogue {data['version']} is valid: {len(data['attributes'])} attributes, "
        f"{len(data['purposes'])} purposes, {len(data['programs'])} programs"
    )


@catalog_app.command("bundle")
def catalog_bundle(check: bool = False) -> None:
    rc = subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "build_bundle.py"), *(["--check"] if check else [])]
    )  # noqa: S603
    raise typer.Exit(code=rc)


@contracts_app.command("export")
def contracts_export(check: bool = False) -> None:
    rc = subprocess.call(
        [sys.executable, str(ROOT / "scripts" / "export_contracts.py"), *(["--check"] if check else [])]
    )  # noqa: S603
    raise typer.Exit(code=rc)


@app.command()
def gateway(
    config: Path = typer.Option(
        ..., "--config", help="gateway YAML (see integrations/gateway/gateway.example.yaml)"
    ),
    host: str = "0.0.0.0",  # noqa: S104 - container default
    port: int = 8080,
) -> None:
    """Run the privacy gateway in front of an existing MIS API (openIMIS, CORE-MIS, FHIR)."""
    import uvicorn

    from .gateway import GatewayConfig, create_gateway

    uvicorn.run(create_gateway(GatewayConfig.from_file(config)), host=host, port=port)


@app.command()
def token(
    sub: str = typer.Option(..., help="actor id"),
    role: str = typer.Option(..., help="e.g. CASE_WORKER, ANALYST, PAYMENT_SERVICE"),
    agency: str = "SOCIAL_PROTECTION_AGENCY",
    programs: str = typer.Option("", help="comma-separated program assignments ('*' for services)"),
    cases: str = typer.Option("", help="comma-separated case ids"),
    mfa: bool = False,
    service: bool = typer.Option(False, "--service", help="issue a machine identity"),
    ttl: int = 3600,
) -> None:
    """Issue a development bearer token signed with PBD_AUTH_SIGNING_KEY."""
    from .common.auth import issue_token
    from .config import get_settings

    s = get_settings()
    typer.echo(
        issue_token(
            s.auth_signing_key,
            sub=sub,
            role=role,
            agency=agency,
            programs=[p for p in programs.split(",") if p],
            cases=[c for c in cases.split(",") if c],
            amr=["pwd", "mfa"] if mfa else ["pwd"],
            svc=service,
            issuer=s.auth_issuer,
            ttl_seconds=ttl,
        )
    )


@app.command()
def decide(input_file: Path = typer.Argument(..., help="JSON policy input")) -> None:
    """Evaluate a policy input with the embedded engine (no services required)."""
    from .catalog.loader import get_catalog
    from .pdp import engine

    inp = json.loads(input_file.read_text(encoding="utf-8"))
    typer.echo(json.dumps(engine.evaluate(get_catalog().data, inp), indent=2))


if __name__ == "__main__":
    app()
