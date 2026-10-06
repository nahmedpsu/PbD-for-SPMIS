"""The conformance suite: every vector must pass on the embedded engine, on the Rego policy
(when an ``opa`` binary is available) and through the PDP HTTP contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbd_spmis.catalog.loader import load_catalog
from pbd_spmis.pdp import engine

from .conftest import ROOT, assert_vector


def _vector_ids() -> list[str]:
    return [v["name"] for v in json.loads((ROOT / "policy/conformance/vectors.json").read_text())["vectors"]]


@pytest.mark.parametrize("index", range(len(_vector_ids())), ids=_vector_ids())
def test_embedded_engine_vector(vectors, index):
    v = vectors[index]
    decision = engine.evaluate(load_catalog(ROOT / "catalog").data, v["input"])
    assert_vector(decision, v)


@pytest.mark.parametrize("index", range(len(_vector_ids())), ids=_vector_ids())
def test_rego_policy_vector(vectors, index, opa_binary, tmp_path: Path):
    if opa_binary is None:
        pytest.skip("opa binary not on PATH")
    v = vectors[index]
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(v["input"]))
    cmd = [
        opa_binary,
        "eval",
        "--v1-compatible",
        "--format",
        "json",
        "-d",
        str(ROOT / "policy/rego/spmis/authz.rego"),
        "-d",
        str(ROOT / "policy/bundle/data.json"),
        "-i",
        str(inp),
        "data.spmis.authz.decision",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    result = json.loads(out.stdout)["result"][0]["expressions"][0]["value"]
    assert_vector(result, v)


def test_bundle_is_fresh():
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/build_bundle.py"), "--check"], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("index", range(len(_vector_ids())), ids=_vector_ids())
def test_pdp_http_contract_vector(harness, vectors, index):
    v = vectors[index]
    payload = dict(v["input"])
    payload.setdefault("subject", {"person_token": None, "program_relationship": []})
    resp = harness.client("pdp").post(
        "/v1/decisions", headers=harness.headers(sub="svc:test", role="PDP_SERVICE", svc=True), json=payload
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_id"].startswith("PD-")
    assert body["policy_version"] == "2026.10.0"
    assert_vector(body, v)


def test_pdp_decisions_are_audited(harness, vectors):
    v = vectors[0]
    resp = harness.client("pdp").post(
        "/v1/decisions",
        headers=harness.headers(sub="svc:test", role="PDP_SERVICE", svc=True),
        json=v["input"],
    )
    did = resp.json()["decision_id"]
    events = (
        harness.client("audit")
        .get("/v1/events", headers=harness.headers(sub="aud-1", role="AUDITOR"), params={"decision_id": did})
        .json()["events"]
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "policy_decision"
    assert events[0]["details"]["policy_version"] == "2026.10.0"
    assert events[0]["reason_codes"] == ["ALLOW"]
