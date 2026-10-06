from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pbd_spmis.testing import Harness

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def vectors() -> list[dict]:
    return json.loads((ROOT / "policy" / "conformance" / "vectors.json").read_text())["vectors"]


@pytest.fixture()
def harness(tmp_path: Path):
    h = Harness(tmp_dir=str(tmp_path))
    yield h
    h.close()


@pytest.fixture(scope="session")
def opa_binary() -> str | None:
    return shutil.which("opa")


def assert_vector(decision: dict, vector: dict) -> None:
    e = vector["expected"]
    name = vector["name"]
    assert decision["allow"] == e["allow"], f"{name}: allow"
    assert decision["release"] == e["release"], f"{name}: release {decision['release']}"
    assert decision["reason_codes"] == e["reason_codes"], f"{name}: reasons {decision['reason_codes']}"
    for o in e["obligations_include"]:
        assert o in decision["obligations"], f"{name}: missing obligation {o}"
    for o in e.get("obligations_exclude", []):
        assert o not in decision["obligations"], f"{name}: unexpected obligation {o}"
