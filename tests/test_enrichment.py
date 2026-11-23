"""Tests for PubMed enrichment of SIEVE profile packets.

Enrichment attaches PubMed hits to the packet's claim as direct evidence
(``statement.has_evidence``): Publications with no interpretation and no strength, so the
score does not move. Every enriched packet must still pass the profile.
"""

import copy
from functools import lru_cache
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mondo_ai.config import get_default_config
from mondo_ai.datamodel import (
    EntityMapping,
    EntitySnapshot,
    ResourceSnapshot,
    Synonym,
    SynonymScope,
)
from mondo_ai.enrichment.enricher import (
    PacketEnricher,
    claim_terms,
    has_literature_evidence,
    paper_to_publication,
)
from mondo_ai.enrichment.pubmed import (
    PubMedPaper,
    ReferenceCache,
    build_search_query,
    parse_article,
)
from mondo_ai.mapping.sssom_reader import SSSOMReader
from mondo_ai.packets.builder import PacketBuilder, Source
from mondo_ai.packets.registry import PUBMED
from mondo_ai.packets.validate import validate_packet
from mondo_ai.scoring.scorer import explain

DATE = "2026-11-20"

# --- a tiny build: Mondo plus DOID, normalized through a two-row SSSOM file ------------

_SSSOM = [
    ("MONDO:1", "cystic fibrosis", "skos:exactMatch", "DOID:1485"),
    ("MONDO:2", "lung disease", "skos:exactMatch", "DOID:850"),
]


def _snap(rid: str, version: str, entities: list[EntitySnapshot]) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource_id=rid, version=version, entity_count=len(entities), entities=entities
    )


@lru_cache(maxsize=1)
def _built(tmp: str) -> tuple[dict, ...]:
    """Merged packets of the tiny build (built once per session)."""
    lines = [
        "# mapping_set_id: http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv"
    ]
    lines.append(
        "subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification"
    )
    lines += ["\t".join([*row, "", "semapv:ManualMappingCuration"]) for row in _SSSOM]
    path = Path(tmp) / "mondo.sssom.tsv"
    path.write_text("\n".join(lines) + "\n")
    mondo = _snap(
        "mondo",
        "http://purl.obolibrary.org/obo/mondo/releases/2026-11-03/mondo.owl",
        [
            EntitySnapshot(id="MONDO:1", label="cystic fibrosis"),
            EntitySnapshot(id="MONDO:2", label="lung disease"),
        ],
    )
    doid = _snap(
        "doid",
        "http://purl.obolibrary.org/obo/doid/releases/2026-10-29/doid.owl",
        [
            EntitySnapshot(
                id="DOID:1485",
                label="cystic fibrosis",
                synonyms=[
                    Synonym(value="mucoviscidosis", scope=SynonymScope.EXACT),
                    Synonym(
                        value="pancreatic fibrosis",
                        scope=SynonymScope.RELATED,
                        pmids=["123"],
                    ),
                ],
                parents=["DOID:850"],
                mappings=[
                    EntityMapping(
                        object_id="MONDO:1", predicate_id="oboInOwl:hasDbXref"
                    )
                ],
            ),
            EntitySnapshot(id="DOID:850", label="lung disease"),
        ],
    )
    config = get_default_config()
    resources = {r.id: r for r in config.resources}
    builder = PacketBuilder(
        sources=[Source(resources["doid"], doid)],
        mondo=mondo,
        sssom=SSSOMReader(path),
        config=config,
        date=DATE,
    )
    _, merged = builder.build(["synonyms", "subclass", "mappings"])
    return tuple(merged)


def built_packets(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    """Fresh deep copies of the merged packets of the tiny build (shared with other test modules)."""
    return copy.deepcopy(list(_built(str(tmp_path_factory.getbasetemp()))))


@pytest.fixture
def packets(tmp_path_factory) -> list[dict]:
    """The packets of the tiny build."""
    return built_packets(tmp_path_factory)


def find(packets: list[dict], claim_type: str, obj: str) -> dict:
    """The packet of the given claim type and object."""
    for p in packets:
        if p["statement"]["type"] == claim_type and p["statement"]["object"] == obj:
            return p
    raise AssertionError(f"no {claim_type} packet with object {obj}")


def make_paper(
    pmid: str = "12345678", title: str = "Mucoviscidosis in adults", **kw
) -> PubMedPaper:
    """A PubMed paper with a full publication date and one publication type."""
    fields = dict(
        pmid=pmid,
        title=title,
        abstract="A study of mucoviscidosis.",
        authors=["Smith J"],
        journal="Test Journal",
        year="2023",
        doi=None,
        publication_date="2023-05-17",
        publication_types=[("D016428", "Journal Article")],
    )
    fields.update(kw)
    return PubMedPaper(**fields)  # type: ignore[arg-type]


def assert_valid(packet: dict) -> None:
    """The packet passes the profile and its statement score is the reference score."""
    assert validate_packet(packet) == []
    st = packet["statement"]
    assert st["score"] == pytest.approx(explain(packet).s, abs=1e-4)


# --- query building --------------------------------------------------------------------


class TestBuildSearchQuery:
    def test_basic_query(self):
        query = build_search_query("asthma", "bronchial asthma")
        assert '"asthma"[Title/Abstract]' in query
        assert '"bronchial asthma"[Title/Abstract]' in query

    def test_disease_only(self):
        assert build_search_query("asthma", "") == '("asthma"[Title/Abstract])'

    def test_same_term_once(self):
        assert build_search_query("asthma", "Asthma") == '("asthma"[Title/Abstract])'


class TestClaimTerms:
    def test_synonym_terms_from_statement_text(self, packets):
        st = find(packets, "SynonymStatement", "mucoviscidosis")["statement"]
        assert claim_terms(st) == ("cystic fibrosis", "mucoviscidosis")

    def test_subclass_terms_from_statement_text(self, packets):
        st = find(packets, "SubclassStatement", "MONDO:2")["statement"]
        assert claim_terms(st) == ("cystic fibrosis", "lung disease")

    def test_labels_passed_in_win(self, packets):
        st = find(packets, "SubclassStatement", "MONDO:2")["statement"]
        labels = {"MONDO:1": "CF", "MONDO:2": "pulmonary disease"}
        assert claim_terms(st, labels) == ("CF", "pulmonary disease")


# --- PubMed papers ---------------------------------------------------------------------

ARTICLE_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><Article>
<Journal><Title>Test Journal</Title><JournalIssue><PubDate><Year>2021</Year><Month>Mar</Month></PubDate></JournalIssue></Journal>
<ArticleTitle>Cystic fibrosis today</ArticleTitle>
<Abstract><AbstractText>Background.</AbstractText><AbstractText>Results.</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>
<PublicationTypeList><PublicationType UI="D016428">Journal Article</PublicationType>
<PublicationType UI="D016454">Review</PublicationType></PublicationTypeList>
<ArticleDate DateType="Electronic"><Year>2021</Year><Month>02</Month><Day>09</Day></ArticleDate>
</Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/x</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


class TestPubMedPaper:
    def test_reference_id(self):
        assert make_paper("99999999").reference_id == "PMID:99999999"

    def test_parse_article(self):
        paper = parse_article("42", ARTICLE_XML)
        assert paper is not None
        assert paper.title == "Cystic fibrosis today"
        assert paper.abstract == "Background.\nResults."
        assert paper.authors == ["Jane Doe"]
        assert paper.year == "2021"
        assert paper.doi == "10.1/x"
        assert paper.publication_date == "2021-02-09"
        assert paper.publication_types == [
            ("D016428", "Journal Article"),
            ("D016454", "Review"),
        ]

    def test_parse_article_without_full_date(self):
        xml = ARTICLE_XML.replace(
            b'<ArticleDate DateType="Electronic"><Year>2021</Year><Month>02</Month><Day>09</Day></ArticleDate>',
            b"",
        )
        paper = parse_article("42", xml)
        assert paper is not None
        assert paper.publication_date is None
        assert paper.year == "2021"

    def test_parse_empty_set(self):
        assert parse_article("42", b"<PubmedArticleSet/>") is None


class TestReferenceCache:
    def test_cache_path(self, tmp_path):
        path = ReferenceCache(cache_dir=tmp_path)._get_cache_path("12345678")
        assert path == tmp_path / "PMID_12345678.md"

    def test_markdown_has_frontmatter(self):
        markdown = make_paper().to_markdown()
        assert markdown.startswith("---")
        assert "title: Mucoviscidosis in adults" in markdown
        assert "A study of mucoviscidosis." in markdown

    def test_put_and_get_round_trip(self, tmp_path):
        cache = ReferenceCache(cache_dir=tmp_path)
        paper = make_paper()
        cache.put(paper)
        assert cache.get(paper.pmid) == paper

    def test_get_missing(self, tmp_path):
        assert ReferenceCache(cache_dir=tmp_path).get("nonexistent") is None


# --- Publication items -----------------------------------------------------------------


class TestPaperToPublication:
    def test_publication_shape(self):
        pub = paper_to_publication(make_paper(), DATE)
        assert pub == {
            "id": "PMID:12345678",
            "type": "Publication",
            "resource": PUBMED,
            "label": "Mucoviscidosis in adults",
            "date_authored": "2023-05-17",
            "record_metadata": {"retrieved_from": PUBMED, "retrieval_date": DATE},
            "generation_context": [
                {"code": "MESH:D016428", "label": "Journal Article"}
            ],
        }

    def test_unknown_basis_and_date(self):
        pub = paper_to_publication(
            make_paper(publication_date=None, publication_types=[]), DATE
        )
        assert "date_authored" not in pub
        assert pub["generation_context"] == [{"code": "sieve:basis_unknown"}]


# --- literature detection --------------------------------------------------------------


class TestHasLiteratureEvidence:
    def test_built_packet_has_none(self, packets):
        assert (
            has_literature_evidence(find(packets, "SynonymStatement", "mucoviscidosis"))
            is False
        )

    def test_source_citation_inside_a_prior_claim_does_not_count(self, packets):
        # DOID cites PMID:123 on its related synonym; that is the source's claim, not literature attached to ours.
        assert (
            has_literature_evidence(
                find(packets, "SynonymStatement", "pancreatic fibrosis")
            )
            is False
        )

    def test_direct_evidence_counts(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        packet["statement"]["has_evidence"] = [paper_to_publication(make_paper(), DATE)]
        assert has_literature_evidence(packet) is True

    def test_publication_in_a_line_counts(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        packet["statement"]["has_evidence_lines"][0]["has_evidence_items"].append(
            paper_to_publication(make_paper(), DATE)
        )
        assert has_literature_evidence(packet) is True


# --- the enricher ----------------------------------------------------------------------


def _enricher(papers: list[PubMedPaper], **kw) -> tuple[PacketEnricher, MagicMock]:
    client = MagicMock()
    client.search_and_fetch.return_value = papers
    return PacketEnricher(pubmed_client=client, date=DATE, **kw), client


class TestPacketEnricher:
    def test_attaches_publications_as_direct_evidence(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        lines_before = copy.deepcopy(packet["statement"]["has_evidence_lines"])
        enricher, client = _enricher(
            [make_paper("1"), make_paper("2", "Second")], max_papers=2
        )

        enriched = enricher.enrich(packet)

        assert [p["id"] for p in enriched["statement"]["has_evidence"]] == [
            "PMID:1",
            "PMID:2",
        ]
        assert enriched["statement"]["has_evidence_lines"] == lines_before
        client.search_and_fetch.assert_called_once_with(
            build_search_query("cystic fibrosis", "mucoviscidosis"), max_results=2
        )
        assert_valid(enriched)

    def test_score_does_not_change(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        score = packet["statement"]["score"]
        enricher, _ = _enricher([make_paper()])
        assert enricher.enrich(packet)["statement"]["score"] == score

    def test_pubmed_lands_in_the_reference_snapshot(self, packets):
        packet = find(packets, "SubclassStatement", "MONDO:2")
        assert PUBMED not in {
            r["id"] for r in packet["reference_snapshot"].get("resources") or []
        }
        enricher, _ = _enricher([make_paper()])
        enriched = enricher.enrich(packet)
        entries = [
            r for r in enriched["reference_snapshot"]["resources"] if r["id"] == PUBMED
        ]
        assert entries == [
            {
                "id": PUBMED,
                "type": "InformationResource",
                "label": "PubMed",
                "curie_prefixes": ["PMID"],
                "default_role": "PRIMARY",
            }
        ]
        assert_valid(enriched)

    def test_no_duplicate_ids(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        enricher, _ = _enricher([make_paper("1")], skip_enriched=False)
        enricher.enrich(packet)
        enricher.enrich(packet)
        assert [p["id"] for p in packet["statement"]["has_evidence"]] == ["PMID:1"]
        assert_valid(packet)

    def test_skips_a_pmid_the_packet_already_carries(self, packets):
        # PMID:123 is nested in DOID's prior claim as a bare Publication; a richer copy would diverge.
        packet = find(packets, "SynonymStatement", "pancreatic fibrosis")
        enricher, _ = _enricher([make_paper("123"), make_paper("7")])
        enriched = enricher.enrich(packet)
        assert [p["id"] for p in enriched["statement"]["has_evidence"]] == ["PMID:7"]
        assert_valid(enriched)

    def test_skip_enriched_by_default(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        packet["statement"]["has_evidence"] = [
            paper_to_publication(make_paper("9"), DATE)
        ]
        enricher, client = _enricher([make_paper("1")])
        assert enricher.should_enrich(packet) is False
        enricher.enrich(packet)
        client.search_and_fetch.assert_not_called()
        assert [p["id"] for p in packet["statement"]["has_evidence"]] == ["PMID:9"]

    def test_force_with_skip_enriched_false(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        packet["statement"]["has_evidence"] = [
            paper_to_publication(make_paper("9"), DATE)
        ]
        enricher, _ = _enricher([make_paper("1")], skip_enriched=False)
        enricher.enrich(packet)
        assert [p["id"] for p in packet["statement"]["has_evidence"]] == [
            "PMID:9",
            "PMID:1",
        ]

    def test_score_threshold(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        score = packet["statement"]["score"]
        assert PacketEnricher(
            pubmed_client=MagicMock(), min_score_threshold=score + 0.01
        ).should_enrich(packet)
        assert not PacketEnricher(
            pubmed_client=MagicMock(), min_score_threshold=score
        ).should_enrich(packet)

    def test_mapping_claims_are_not_enriched(self, packets):
        # Publications are not evidence for a MappingStatement (claim-evidence matrix).
        packet = find(packets, "MappingStatement", "DOID:1485")
        enricher, client = _enricher([make_paper()])
        assert enricher.should_enrich(packet) is False
        enricher.enrich(packet)
        client.search_and_fetch.assert_not_called()
        assert "has_evidence" not in packet["statement"]

    def test_no_hits_leaves_packet_unchanged(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        before = copy.deepcopy(packet)
        enricher, _ = _enricher([])
        assert enricher.enrich(packet) == before

    def test_enrich_packets_batch(self, packets):
        enricher, _ = _enricher([make_paper("11111111")])
        enriched = enricher.enrich_packets(packets)
        assert len(enriched) == len(packets)
        for packet in enriched:
            if packet["statement"]["type"] != "MappingStatement":
                assert has_literature_evidence(packet)
            assert_valid(packet)
