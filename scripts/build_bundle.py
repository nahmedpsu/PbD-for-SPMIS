#!/usr/bin/env python3
"""Build the OPA data bundle from the catalogue and the conformance vectors.

Writes policy/bundle/data.json containing:
  data.catalog      the merged, validated privacy catalogue
  data.conformance  the conformance vectors

Run after any change to catalog/*.yaml or policy/conformance/vectors.json. CI fails when the
committed bundle is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pbd_spmis.catalog.loader import load_catalog  # noqa: E402


def build() -> dict:
    catalog = load_catalog(ROOT / "catalog")
    vectors = json.loads((ROOT / "policy" / "conformance" / "vectors.json").read_text(encoding="utf-8"))
    return {"catalog": catalog.to_bundle(), "conformance": {"vectors": vectors["vectors"]}}


def main(argv: list[str]) -> int:
    out = ROOT / "policy" / "bundle" / "data.json"
    bundle = build()
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    if "--check" in argv:
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            print(f"{out} is stale; run scripts/build_bundle.py", file=sys.stderr)
            return 1
        print("bundle is up to date")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    n = len(bundle["conformance"]["vectors"])
    print(f"wrote {out} ({n} vectors, catalogue {bundle['catalog']['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
