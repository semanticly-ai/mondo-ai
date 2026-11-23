"""Tests for attaching Mondo's curator and publication provenance to packets."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mondo_ai.config import get_default_config
from mondo_ai.extraction.mondo_extractor import AxiomProvenance
from mondo_ai.packets.mondo_provenance import PUBMED_RESOURCE, apply_mondo_provenance, provenance_key
from mondo_ai.packets.registry import PUBMED, build_registry
from mondo_ai.packets.validate import validate_packet
from mondo_ai.scoring.scorer import score_packet

EXAMPLES = Path(__file__).parent / "data" / "sieve_example_packets.json"
CURATOR_A = "0000-0001-5208-3432"
CURATOR_B = "0000-0002-6601-2165"


def _example(claim_type: str, review_state: str = "NOT_VERIFIED") -> dict[str, Any]:
    """The example packet of a claim type, with the given review state."""
    packet = next(p for p in json.loads(EXAMPLES.read_text()) if p["statement"]["type"] == claim_type)
    packet["statement"]["review_state"] = review_state
    return packet


@pytest.fixture
def synonym() -> dict[str, Any]:
    """A synonym claim already in Mondo (object 'SYNGAP1-related intellectual disability')."""
    return _example("SynonymStatement")


def _key(packet: dict[str, Any]) -> tuple[str, str, str]:
    return provenance_key(packet["statement"])


class TestProvenanceKey:
    def test_synonym_object_is_casefolded(self, synonym):
        assert _key(synonym) == ("MONDO:0012960", "oboInOwl:hasExactSynonym", "syngap1-related intellectual disability")

    def test_other_objects_are_kept(self):
        assert _key(_example("MappingStatement")) == ("MONDO:0008482", "skos:exactMatch", "OMIM:184400")
        assert _key(_example("SubclassStatement")) == ("MONDO:0018770", "rdfs:subClassOf", "MONDO:0022410")


class TestApplyMondoProvenance:
    def test_curators_become_contributions_and_agents(self, synonym):
        out = apply_mondo_provenance(synonym, {_key(synonym): AxiomProvenance(orcids=(CURATOR_A, CURATOR_B))})
        statement = out["statement"]
        assert statement["contributions"] == [
            {
                "id": f"{statement['id']}#curator-{CURATOR_A}",
                "type": "SieveContribution",
                "contributor": f"orcid:{CURATOR_A}",
                "role": "CURATOR",
            },
            {
                "id": f"{statement['id']}#curator-{CURATOR_B}",
                "type": "SieveContribution",
                "contributor": f"orcid:{CURATOR_B}",
                "role": "CURATOR",
            },
        ]
        agents = out["reference_snapshot"]["agents"]
        assert {"id": f"orcid:{CURATOR_A}", "type": "SieveAgent", "subtype": "person"} in agents
        assert {"id": f"orcid:{CURATOR_B}", "type": "SieveAgent", "subtype": "person"} in agents
        assert validate_packet(out) == []

    def test_pmids_become_publications(self, synonym):
        out = apply_mondo_provenance(synonym, {_key(synonym): AxiomProvenance(pmids=("123", "456"))})
        assert out["statement"]["has_evidence"] == [
            {"id": "PMID:123", "type": "Publication", "resource": PUBMED},
            {"id": "PMID:456", "type": "Publication", "resource": PUBMED},
        ]
        assert PUBMED_RESOURCE in out["reference_snapshot"]["resources"]
        assert "contributions" not in out["statement"]
        assert validate_packet(out) == []

    def test_score_does_not_change(self, synonym):
        before = {k: synonym["statement"][k] for k in ("score", "strength", "direction")}
        out = apply_mondo_provenance(synonym, {_key(synonym): AxiomProvenance(orcids=(CURATOR_A,), pmids=("123",))})
        rescored = score_packet(copy.deepcopy(out))
        assert {k: rescored["statement"][k] for k in before} == before

    def test_subclass_claim(self):
        packet = _example("SubclassStatement")
        out = apply_mondo_provenance(packet, {_key(packet): AxiomProvenance(orcids=(CURATOR_A,), pmids=("29804726",))})
        assert len(out["statement"]["contributions"]) == 1
        assert out["statement"]["has_evidence"][0]["id"] == "PMID:29804726"
        assert validate_packet(out) == []

    def test_mapping_claim_takes_curators_but_no_publications(self):
        """A publication is not evidence for a mapping (the claim-evidence matrix)."""
        packet = _example("MappingStatement")
        out = apply_mondo_provenance(packet, {_key(packet): AxiomProvenance(orcids=(CURATOR_A,), pmids=("1",))})
        assert len(out["statement"]["contributions"]) == 1
        assert "has_evidence" not in out["statement"]
        assert PUBMED_RESOURCE not in out["reference_snapshot"]["resources"]
        assert validate_packet(out) == []

    def test_idempotent(self, synonym):
        provenance = {_key(synonym): AxiomProvenance(orcids=(CURATOR_A,), pmids=("123",))}
        once = apply_mondo_provenance(synonym, provenance)
        twice = apply_mondo_provenance(once, provenance)
        assert twice == once
        assert [a["id"] for a in twice["reference_snapshot"]["agents"]].count(f"orcid:{CURATOR_A}") == 1
        assert [r["id"] for r in twice["reference_snapshot"]["resources"]].count(PUBMED) == 1

    def test_does_not_mutate_its_input(self, synonym):
        original = copy.deepcopy(synonym)
        apply_mondo_provenance(synonym, {_key(synonym): AxiomProvenance(orcids=(CURATOR_A,), pmids=("123",))})
        assert synonym == original

    def test_candidate_claim_is_untouched(self):
        packet = _example("SynonymStatement", review_state="PENDING_REVIEW")
        assert apply_mondo_provenance(packet, {_key(packet): AxiomProvenance(orcids=(CURATOR_A,))}) is packet

    def test_no_provenance_is_untouched(self, synonym):
        assert apply_mondo_provenance(synonym, {}) is synonym

    def test_pubmed_resource_matches_the_registry(self):
        registry = build_registry(get_default_config(), {}, "2026-11-20")
        assert PUBMED_RESOURCE in registry["resources"]

    def test_mapping_with_only_publications_is_untouched(self):
        packet = _example("MappingStatement")
        assert apply_mondo_provenance(packet, {_key(packet): AxiomProvenance(pmids=("1",))}) is packet
