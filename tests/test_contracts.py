from __future__ import annotations

import subprocess
import sys

import yaml
from openapi_spec_validator import validate

from .conftest import ROOT


def test_contracts_are_valid_openapi():
    files = sorted((ROOT / "contracts" / "openapi").glob("*.yaml"))
    assert len(files) == 10
    for f in files:
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        validate(spec)
        assert "X-Purpose" in spec["info"]["x-privacy-headers"]


def test_contracts_are_in_sync_with_code():
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "export_contracts.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_appendix_b_contract_shape():
    spec = yaml.safe_load((ROOT / "contracts/openapi/eligibility.yaml").read_text())
    assert "/v1/eligibility/verify" in spec["paths"]
    schema = spec["components"]["schemas"]["VerifyIn"]
    assert set(schema["properties"]) == {"person_token", "checks"}
