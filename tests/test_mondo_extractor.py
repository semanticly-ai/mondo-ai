"""Tests for the Mondo axiom-provenance extractor (extraction/mondo_extractor.py)."""

from pathlib import Path

import pytest

from mondo_ai.extraction.mondo_extractor import (
    AxiomProvenance,
    MondoProvenanceExtractor,
    extract_mondo_provenance,
    provenance_from_annotations,
)

MINI_MONDO = Path(__file__).parent / "data" / "mini_mondo.owl"

CURATOR_A = "0000-0001-5208-3432"
CURATOR_B = "0000-0002-6601-2165"

EXPECTED = {
    ("MONDO:0000002", "rdfs:subClassOf", "MONDO:0000001"): AxiomProvenance(orcids=(CURATOR_B,)),
    ("MONDO:0000003", "rdfs:subClassOf", "MONDO:0000002"): AxiomProvenance(pmids=("29804726",)),
    ("MONDO:0000003", "oboInOwl:hasExactSynonym", "cf"): AxiomProvenance(orcids=(CURATOR_A,), pmids=("12345",)),
    ("MONDO:0000003", "oboInOwl:hasExactSynonym", "mucoviscidosis"): AxiomProvenance(pmids=("27745641",)),
    ("MONDO:0000003", "oboInOwl:hasNarrowSynonym", "cf, classic"): AxiomProvenance(pmids=("333",)),
    ("MONDO:0000003", "skos:exactMatch", "OMIM:219700"): AxiomProvenance(orcids=(CURATOR_B,)),
    ("MONDO:0000003", "skos:broadMatch", "NCIT:C2975"): AxiomProvenance(orcids=(CURATOR_A,)),
}


@pytest.fixture(scope="module")
def provenance() -> dict[tuple[str, str, str], AxiomProvenance]:
    """The provenance map of the mini Mondo fixture."""
    return MondoProvenanceExtractor().extract(MINI_MONDO)


class TestProvenanceFromAnnotations:
    def test_orcid_urls(self):
        assert provenance_from_annotations([f"https://orcid.org/{CURATOR_A}"]) == AxiomProvenance(orcids=(CURATOR_A,))

    def test_orcid_curie_and_http_url(self):
        prov = provenance_from_annotations([f"ORCID:{CURATOR_A}", f"http://orcid.org/{CURATOR_B}"])
        assert prov.orcids == (CURATOR_A, CURATOR_B)

    def test_pmid_forms(self):
        prov = provenance_from_annotations(
            ["PMID:1", "PMID: 2", "https://pubmed.ncbi.nlm.nih.gov/3", "https://www.ncbi.nlm.nih.gov/pubmed/4"]
        )
        assert prov.pmids == ("1", "2", "3", "4")

    def test_deduplicates_in_order(self):
        prov = provenance_from_annotations(["PMID:2", "PMID:1", "PMID:2"])
        assert prov.pmids == ("2", "1")

    def test_other_sources_give_nothing(self):
        prov = provenance_from_annotations(["MONDO:equivalentTo", "DOID:4", "https://www.malacards.org/x"])
        assert not prov


class TestMondoProvenanceExtractor:
    def test_exact_mapping(self, provenance):
        assert provenance == EXPECTED

    def test_synonym_keys_are_casefolded(self, provenance):
        assert ("MONDO:0000003", "oboInOwl:hasExactSynonym", "cf") in provenance
        assert ("MONDO:0000003", "oboInOwl:hasExactSynonym", "CF") not in provenance

    def test_axioms_without_orcid_or_pmid_are_left_out(self, provenance):
        assert ("MONDO:0000003", "rdfs:subClassOf", "MONDO:0000001") not in provenance  # MONDO:Entailed only
        assert ("MONDO:0000003", "oboInOwl:hasRelatedSynonym", "fibrocystic disease") not in provenance
        assert ("MONDO:0000003", "skos:exactMatch", "Orphanet:586") not in provenance

    def test_only_mondo_subjects(self, provenance):
        assert all(subject.startswith("MONDO:") for subject, _, _ in provenance)

    def test_excluded_subclass_is_not_read(self, provenance):
        assert all(predicate != "mondo:excluded_subClassOf" for _, predicate, _ in provenance)

    def test_convenience_function(self):
        assert extract_mondo_provenance(MINI_MONDO) == EXPECTED

    def test_counts_by_kind(self, provenance):
        assert MondoProvenanceExtractor.counts(provenance) == {
            "synonym": {"axioms": 3, "with_orcid": 1, "with_pmid": 3},
            "subclass": {"axioms": 2, "with_orcid": 1, "with_pmid": 1},
            "mapping": {"axioms": 2, "with_orcid": 2, "with_pmid": 0},
        }
