"""Tests for the bulk OWL extractor that turns a source ontology into a ResourceSnapshot."""

from pathlib import Path

import pytest

from mondo_ai.datamodel import SynonymScope
from mondo_ai.extraction.owl_extractor import OWLExtractor, normalize_xref

MINI = Path(__file__).parent / "data" / "mini_source.owl"


@pytest.fixture(scope="module")
def snapshot():
    return OWLExtractor().extract(MINI, "doid", "Disease Ontology", iri_filter="DOID_", skip_deprecated=False)


@pytest.fixture(scope="module")
def by_id(snapshot):
    return {e.id: e for e in snapshot.entities}


def test_snapshot_metadata(snapshot):
    assert snapshot.resource_id == "doid"
    assert snapshot.version == "http://purl.obolibrary.org/obo/doid/releases/2026-10-29/doid.owl"
    assert snapshot.entity_count == 4
    assert len(snapshot.checksum) == 64


def test_iri_filter_excludes_other_namespaces(by_id):
    assert "GO:0000001" not in by_id


def test_deprecated_kept_and_flagged(by_id):
    assert by_id["DOID:3"].deprecated is True
    assert by_id["DOID:2"].deprecated is False


def test_skip_deprecated():
    snap = OWLExtractor().extract(MINI, "doid", iri_filter="DOID_")
    assert "DOID:3" not in {e.id for e in snap.entities}


def test_label_and_definition(by_id):
    cf = by_id["DOID:2"]
    assert cf.label == "cystic fibrosis"
    assert cf.definition == "A genetic disease of the lungs."
    assert cf.definition_pmids == ["999"]


def test_synonyms_with_scope_type_and_pmids(by_id):
    syns = {s.value: s for s in by_id["DOID:2"].synonyms}
    assert syns["CF"].scope == SynonymScope.EXACT
    assert syns["CF"].type == "OMO:0003000"
    assert syns["CF"].pmids == ["12345"]
    assert syns["mucoviscidosis"].scope == SynonymScope.RELATED
    assert syns["Cystic fibrosis of pancreas"].scope == SynonymScope.EXACT


def test_named_and_classification_parents(by_id):
    cf = by_id["DOID:2"]
    assert cf.parents == ["DOID:1"]
    assert cf.classification_parents == ["DOID:4"]


def test_xrefs_normalised_with_precision(by_id):
    maps = {(m.object_id, m.predicate_id): m for m in by_id["DOID:2"].mappings}
    assert maps[("OMIM:219700", "oboInOwl:hasDbXref")].mapping_source == "E"
    assert ("Orphanet:586", "oboInOwl:hasDbXref") in maps
    assert ("Orphanet:586", "skos:exactMatch") in maps


def test_snapshot_round_trip(snapshot, tmp_path):
    from mondo_ai.extraction.snapshots import load_snapshot, save_snapshot

    path = save_snapshot(snapshot, tmp_path / "doid.yaml")
    assert load_snapshot(path) == snapshot


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MIM:219700", "OMIM:219700"),
        ("ORDO:586", "Orphanet:586"),
        ("ICD10CM:Q90.0", "ICD10CM:Q90.0"),
        ("ICD-10:E84", "ICD10WHO:E84"),
        ("MESH:D003550", "MESH:D003550"),
        ("MeSH:D003550", "MESH:D003550"),
    ],
)
def test_normalize_xref(raw, expected):
    assert normalize_xref(raw) == expected
