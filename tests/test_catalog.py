from __future__ import annotations

import copy

import pytest

from pbd_spmis.catalog.loader import CatalogError, load_catalog, merge_catalog_files, validate_catalog

from .conftest import ROOT


def test_catalog_loads_and_validates():
    cat = load_catalog(ROOT / "catalog")
    assert cat.version == "2026.10.0"
    assert "eligibility_verification" in cat.purposes
    assert cat.attribute_class("national_id") == "C4"
    assert cat.program_code("cash_assistance") == "CASH"
    assert cat.program_by_code("DISA") == "disability_allowance"


def test_every_purpose_release_references_known_attributes_and_modes():
    cat = load_catalog(ROOT / "catalog")
    for name, p in cat.purposes.items():
        for attr, mode in p["release"].items():
            assert attr in cat.attributes, (name, attr)
            assert mode in cat.attributes[attr]["disclosures"], (name, attr, mode)


def test_validation_catches_cross_reference_errors():
    data = merge_catalog_files(ROOT / "catalog")
    broken = copy.deepcopy(data)
    broken["purposes"]["eligibility_verification"]["release"]["shoe_size"] = "exact"
    broken["purposes"]["eligibility_verification"]["release"]["income"] = "token"
    broken["programs"]["cash_assistance"]["purposes"].append("marketing")
    problems = validate_catalog(broken)
    assert any("unknown attribute 'shoe_size'" in p for p in problems)
    assert any("mode 'token' is not a permitted disclosure for 'income'" in p for p in problems)
    assert any("unknown purpose 'marketing'" in p for p in problems)


def test_validation_rejects_exact_c4_export_purposes():
    data = merge_catalog_files(ROOT / "catalog")
    broken = copy.deepcopy(data)
    broken["purposes"]["analytics_reporting"]["release"]["income"] = "exact"
    problems = validate_catalog(broken)
    assert any("export purposes must not release C4 attributes exactly" in p for p in problems)


def test_schema_rejects_unknown_release_mode():
    data = merge_catalog_files(ROOT / "catalog")
    broken = copy.deepcopy(data)
    broken["purposes"]["registration"]["release"]["name"] = "plaintext"
    problems = validate_catalog(broken)
    assert any(p.startswith("schema:") for p in problems)


def test_missing_file_raises(tmp_path):
    with pytest.raises(CatalogError):
        load_catalog(tmp_path)


def test_bands_and_assertions():
    cat = load_catalog(ROOT / "catalog")
    assert cat.band_for("income", 180) == "100-249"
    assert cat.band_for("income", 5000) == "500+"
    assert cat.assert_for("income", "below_threshold", 180, program="cash_assistance") is True
    assert cat.assert_for("income", "below_threshold", 300, program="cash_assistance") is False
    assert (
        cat.assert_for("disability_status", "eligible", "certified_severe", program="disability_allowance")
        is True
    )
    assert (
        cat.assert_for("disability_status", "eligible", "not_certified", program="disability_allowance")
        is False
    )
