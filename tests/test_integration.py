"""End-to-end integration tests for enrichment and synthesis.

These tests make real API calls to PubMed and AI providers.
They require:
- NCBI_API_KEY (optional, for faster PubMed access)
- ANTHROPIC_API_KEY or OPENAI_API_KEY (required for synthesis)

Run with:
    uv run --env-file .env pytest tests/test_integration.py -v -s

To run only specific tests:
    uv run pytest tests/test_integration.py::test_pubmed_enrichment -v -s
    uv run --env-file .env pytest tests/test_integration.py::test_ai_synthesis -v -s
    uv run --env-file .env pytest tests/test_integration.py::test_full_pipeline -v -s

The packet runner itself is covered by tests/test_pipeline.py.
"""

import os
from pathlib import Path

import pytest
import yaml

from mondo_ai.agents import SYNTHESIS_AGENT, SynthesisAgent
from mondo_ai.enrichment import PacketEnricher, PubMedClient, has_literature_evidence
from tests.test_enrichment import assert_valid, built_packets, find


@pytest.fixture
def packets(tmp_path_factory) -> list[dict]:
    """The packets of the tiny build in tests/test_enrichment.py."""
    return built_packets(tmp_path_factory)


def _provider() -> str:
    """The provider to use, or skip when no API key is set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    pytest.skip("No AI API key found (need ANTHROPIC_API_KEY or OPENAI_API_KEY)")


def _synthesis_lines(packet: dict) -> list[dict]:
    return [
        line
        for line in packet["statement"]["has_evidence_lines"]
        if line["contributions"][0]["contributor"] == SYNTHESIS_AGENT
    ]


@pytest.mark.integration
def test_pubmed_search():
    """PubMed search returns PMIDs for a disease query."""
    client = PubMedClient(cache_dir=None)
    pmids = client.search("asthma bronchial", max_results=3)
    assert len(pmids) > 0, "PubMed search should return results"
    assert all(pmid.isdigit() for pmid in pmids), "PMIDs should be numeric"


@pytest.mark.integration
def test_pubmed_fetch():
    """PubMed fetch retrieves paper details, publication types included."""
    client = PubMedClient(cache_dir=None)
    paper = client.fetch("12576356")
    assert paper is not None, "Should fetch paper details"
    assert paper.pmid == "12576356"
    assert paper.title, "Paper should have a title"
    assert paper.publication_types, "PubMed gives at least one publication type"


@pytest.mark.integration
def test_pubmed_enrichment(packets, tmp_path):
    """Enrichment attaches PubMed hits as direct evidence; the packet stays valid."""
    enricher = PacketEnricher(
        pubmed_client=PubMedClient(cache_dir=tmp_path), max_papers=2
    )
    packet = find(packets, "SynonymStatement", "mucoviscidosis")
    score = packet["statement"]["score"]

    enriched = enricher.enrich(packet)

    assert has_literature_evidence(enriched), (
        "cystic fibrosis / mucoviscidosis has literature"
    )
    assert all(
        p["type"] == "Publication" for p in enriched["statement"]["has_evidence"]
    )
    assert enriched["statement"]["score"] == score
    assert_valid(enriched)


@pytest.mark.integration
def test_ai_synthesis(packets):
    """The model's judgement becomes one synthesis line; the packet stays valid."""
    agent = SynthesisAgent(provider=_provider())
    packet = find(packets, "SynonymStatement", "mucoviscidosis")

    agent.synthesize(packet)

    lines = _synthesis_lines(packet)
    assert len(lines) == 1
    assert lines[0]["contributions"][0]["rationale"]
    assert_valid(packet)


@pytest.mark.integration
def test_full_pipeline(packets, tmp_path):
    """Build -> enrich -> synthesize -> export, with a valid packet at every step."""
    provider = _provider()
    cache = tmp_path / "refs"
    packet = find(packets, "SynonymStatement", "mucoviscidosis")
    assert_valid(packet)

    packet = PacketEnricher(
        pubmed_client=PubMedClient(cache_dir=cache), max_papers=2
    ).enrich(packet)
    assert_valid(packet)

    from mondo_ai.enrichment import ReferenceCache

    SynthesisAgent(provider=provider, reference_cache=ReferenceCache(cache)).synthesize(
        packet
    )
    assert_valid(packet)
    line = _synthesis_lines(packet)[0]
    publications = packet["statement"].get("has_evidence") or []
    assert {p["id"] for p in publications} <= {
        i["id"] for i in line["has_evidence_items"]
    }

    out = tmp_path / "packet.yaml"
    out.write_text(yaml.safe_dump(packet, sort_keys=False))
    assert yaml.safe_load(out.read_text()) == packet


@pytest.mark.integration
def test_reference_cache(tmp_path: Path):
    """The reference cache stores a fetched paper and serves it again."""
    client = PubMedClient(cache_dir=tmp_path)
    paper1 = client.fetch("12576356")
    assert paper1 is not None
    cache_file = tmp_path / "PMID_12576356.md"
    assert cache_file.exists(), "Cache file should be created"

    paper2 = client.fetch("12576356")
    assert paper2 == paper1
    assert cache_file.read_text().startswith("---")
