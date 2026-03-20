"""End-to-end integration tests for mondo-ai.

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
"""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from mondo_ai.datamodel import (
    ConcordanceItem,
    CurationStatus,
    EvidenceItemType,
    EvidencePacket,
    RecommendedAction,
    SieveDocument,
    SieveEvidenceLine,
    SieveStatement,
    StatementPredicate,
    predicate_to_coding,
)
from mondo_ai.enrichment import PacketEnricher, PubMedClient
from mondo_ai.agents import SynthesisAgent


def create_test_packet() -> EvidencePacket:
    """Create a realistic test packet for integration testing.

    Uses a real Mondo disease (asthma) with a known synonym.
    The packet starts with concordance evidence from OMIM.
    """
    item = ConcordanceItem(
        id="ev_test_001",
        type="ConcordanceItem",
        evidenceItemType=EvidenceItemType.ConcordanceItem,
        sourceName="OMIM",
        sourceSubject="OMIM:600807",
        sourceSubjectLabel="Asthma",
        description="Cross-reference from OMIM supports this synonym",
    )
    line = SieveEvidenceLine(
        id="el_test_001",
        type="SieveEvidenceLine",
        hasEvidenceItems=[item],
        directionOfEvidenceProvided="supports",
        strengthOfEvidenceProvided="strong",
        scoreOfEvidenceProvided=0.9,
    )
    stmt = SieveStatement(
        id="stmt_integration_test",
        type="SieveStatement",
        subject="MONDO:0004979",
        subjectLabel="asthma",
        predicate=predicate_to_coding(StatementPredicate.EXACT_SYNONYM),
        object="bronchial asthma",
        objectLabel="bronchial asthma",
    )
    return EvidencePacket(
        id="integration_test_pkt",
        statement=stmt,
        status=CurationStatus.UNREVIEWED,
        hasEvidenceLines=[line],
    )


@pytest.mark.integration
def test_pubmed_search():
    """Test that PubMed search returns results for a disease query."""
    client = PubMedClient(cache_dir=None)

    pmids = client.search("asthma bronchial", max_results=3)

    assert len(pmids) > 0, "PubMed search should return results"
    assert all(pmid.isdigit() for pmid in pmids), "PMIDs should be numeric"
    print(f"\nFound {len(pmids)} PMIDs: {pmids}")


@pytest.mark.integration
def test_pubmed_fetch():
    """Test that PubMed fetch retrieves paper details."""
    client = PubMedClient(cache_dir=None)

    paper = client.fetch("12576356")

    assert paper is not None, "Should fetch paper details"
    assert paper.pmid == "12576356"
    assert paper.title, "Paper should have a title"
    assert paper.reference_id == "PMID:12576356"
    print(f"\nFetched paper: {paper.title}")
    print(f"Authors: {', '.join(paper.authors[:3])}...")
    print(f"Journal: {paper.journal} ({paper.year})")


@pytest.mark.integration
def test_pubmed_enrichment():
    """Test enriching a packet with PubMed literature.

    This demonstrates evidence MERGING - the enricher adds new
    SieveDocument items to the existing packet's evidence lines.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PubMedClient(cache_dir=tmpdir)
        enricher = PacketEnricher(pubmed_client=client, max_papers=2)

        packet = create_test_packet()
        original_line_count = len(packet.hasEvidenceLines)
        print(f"\nOriginal evidence line count: {original_line_count}")

        # Enrich - this MERGES new evidence into the packet
        enriched = enricher.enrich(packet)

        assert len(enriched.hasEvidenceLines) > original_line_count, \
            "Enrichment should add literature evidence lines"

        print(f"\nAfter enrichment:")
        print(f"  Total evidence lines: {len(enriched.hasEvidenceLines)}")

        for line in enriched.hasEvidenceLines:
            print(f"  - Line {line.id}: {len(line.hasEvidenceItems or [])} items")
            for item in line.hasEvidenceItems or []:
                if isinstance(item, SieveDocument):
                    print(f"      PMID:{item.pmid}: {item.title[:50] if item.title else 'No title'}...")


@pytest.mark.integration
def test_ai_synthesis():
    """Test AI synthesis on an evidence packet.

    This demonstrates that synthesis updates the packet.evidence_synthesis
    field directly (the evidence_synthesis lives IN the packet).
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not anthropic_key and not openai_key:
        pytest.skip("No AI API key found (need ANTHROPIC_API_KEY or OPENAI_API_KEY)")

    provider = "anthropic" if anthropic_key else "openai"

    agent = SynthesisAgent(provider=provider)
    packet = create_test_packet()

    # Verify packet has no evidence_synthesis initially
    assert packet.evidence_synthesis is None, "Packet should start without evidence_synthesis"

    # Synthesize - this updates packet.evidence_synthesis IN PLACE
    synthesis = agent.synthesize(packet)

    # Verify the evidence_synthesis is now IN the packet
    assert packet.evidence_synthesis is not None, \
        "Synthesis should update packet.evidence_synthesis"
    assert packet.evidence_synthesis is synthesis, \
        "Packet evidence_synthesis should be the returned synthesis"

    # Check the synthesis has required fields
    assert synthesis.summary, "Should have summary"
    assert synthesis.score is not None, "Should have score"

    print(f"\nAI Synthesis (stored in packet.evidence_synthesis):")
    print(f"  Score: {packet.evidence_synthesis.score}")
    print(f"  Summary: {packet.evidence_synthesis.summary[:200]}...")
    if packet.evidence_synthesis.cited_evidence:
        print(f"  Cited evidence: {packet.evidence_synthesis.cited_evidence}")


@pytest.mark.integration
def test_full_pipeline():
    """Test the full pipeline: create packet -> enrich -> synthesize.

    This is the complete end-to-end test demonstrating:
    1. Evidence packet creation with initial concordance evidence
    2. Evidence MERGING via PubMed enrichment (adds literature evidence)
    3. AI synthesis stored directly IN the evidence packet
    4. Final packet contains all evidence + evidence_synthesis

    The final EvidencePacket is a complete unit with:
    - statement: the assertion being evaluated
    - hasEvidenceLines: merged list of all evidence lines
    - evidence_synthesis: AI-generated synthesis with summary
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if not anthropic_key and not openai_key:
        pytest.skip("No AI API key found (need ANTHROPIC_API_KEY or OPENAI_API_KEY)")

    provider = "anthropic" if anthropic_key else "openai"

    print("\n" + "="*60)
    print("FULL PIPELINE INTEGRATION TEST")
    print("="*60)

    # Step 1: Create evidence packet with initial evidence
    print("\n[1] Creating evidence packet...")
    packet = create_test_packet()
    predicate = packet.statement.predicate
    pred_label = predicate.label if predicate else "unknown"
    print(f"    Statement: {packet.statement.subjectLabel} {pred_label} '{packet.statement.object}'")
    print(f"    Initial evidence lines: {len(packet.hasEvidenceLines)}")
    print(f"    Evidence synthesis: {packet.evidence_synthesis}")

    # Step 2: Enrich - MERGE literature evidence into packet
    print("\n[2] Enriching with PubMed literature (MERGING evidence)...")
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PubMedClient(cache_dir=tmpdir)
        enricher = PacketEnricher(pubmed_client=client, max_papers=2)
        packet = enricher.enrich(packet)

    concordance_count = 0
    literature_count = 0
    for line in packet.hasEvidenceLines or []:
        for item in line.hasEvidenceItems or []:
            if isinstance(item, ConcordanceItem):
                concordance_count += 1
            elif isinstance(item, SieveDocument):
                literature_count += 1
    print(f"    Concordance items: {concordance_count}")
    print(f"    Literature items: {literature_count}")
    print(f"    Total evidence lines: {len(packet.hasEvidenceLines or [])}")

    # Step 3: Synthesize - add evidence_synthesis INTO the packet
    print(f"\n[3] Running AI synthesis ({provider})...")
    print("    Synthesis will be stored in packet.evidence_synthesis")
    agent = SynthesisAgent(provider=provider)
    agent.synthesize(packet)  # Updates packet.evidence_synthesis in place

    print(f"    Score: {packet.evidence_synthesis.score}")
    print(f"    Summary: {packet.evidence_synthesis.summary[:100]}...")

    # Step 4: Verify final packet structure
    print("\n[4] Final EvidencePacket structure:")
    print(f"    packet.id: {packet.id}")
    print(f"    packet.statement: {packet.statement.subjectLabel} -> {packet.statement.object}")
    print(f"    packet.hasEvidenceLines: {len(packet.hasEvidenceLines or [])} lines")
    print(f"    packet.evidence_synthesis: present (score: {packet.evidence_synthesis.score})")

    # Step 5: Export complete packet to YAML
    print("\n[5] Exporting complete EvidencePacket to YAML...")
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        packet_dict = packet.model_dump(mode='json', exclude_none=True)
        yaml.dump(packet_dict, f, default_flow_style=False, sort_keys=False)
        print(f"    Exported to: {f.name}")

        # Show snippet of the YAML
        f.flush()
        with open(f.name) as rf:
            content = rf.read()
            print(f"\n    YAML preview (first 50 lines):")
            for i, line in enumerate(content.split('\n')[:50]):
                print(f"    {line}")

    print("\n" + "="*60)
    print("PIPELINE TEST COMPLETED SUCCESSFULLY")
    print("="*60)

    # Final assertions
    assert packet.evidence_synthesis is not None, "Packet must have evidence_synthesis"
    assert len(packet.hasEvidenceLines or []) >= 1, "Packet must have evidence lines"


@pytest.mark.integration
def test_reference_cache():
    """Test that the reference cache works correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        client = PubMedClient(cache_dir=tmpdir)

        paper1 = client.fetch("12576356")
        assert paper1 is not None

        cache_file = Path(tmpdir) / "PMID_12576356.md"
        assert cache_file.exists(), "Cache file should be created"

        paper2 = client.fetch("12576356")
        assert paper2 is not None
        assert paper2.title == paper1.title

        content = cache_file.read_text()
        assert "---" in content, "Should have YAML frontmatter"
        assert paper1.title in content
        print(f"\nCache file created: {cache_file}")
        print(f"Cache size: {cache_file.stat().st_size} bytes")


@pytest.mark.integration
def test_pipeline_runner_multi_source():
    """Test PipelineRunner with multiple OWL sources (OMIM, ORDO, DOID).

    This integration test requires actual OWL files to be present.
    It tests the full extraction pipeline from multiple disease sources.

    Run with:
        uv run pytest tests/test_integration.py::test_pipeline_runner_multi_source -v -s
    """
    from mondo_ai.pipeline import PipelineRunner, export_packets_yaml, export_packets_summary

    # Standard paths from mondo-ingest
    base_path = Path("/Users/matentzn/ws/mondo-ingest/src/ontology/tmp")
    sssom_path = base_path / "mondo.sssom.tsv"
    omim_path = base_path / "mirror-omim.owl"
    ordo_path = base_path / "mirror-ordo.owl"
    doid_path = base_path / "mirror-doid.owl"

    # Check if files exist
    if not sssom_path.exists():
        pytest.skip(f"SSSOM file not found at {sssom_path}")
    if not omim_path.exists():
        pytest.skip(f"OMIM OWL file not found at {omim_path}")

    print("\n" + "="*60)
    print("PIPELINE RUNNER MULTI-SOURCE TEST")
    print("="*60)

    # Initialize runner with available sources
    runner_kwargs = {
        "sssom_path": sssom_path,
        "omim_owl_path": omim_path,
    }
    if ordo_path.exists():
        runner_kwargs["ordo_owl_path"] = ordo_path
        print(f"[✓] ORDO available: {ordo_path}")
    else:
        print(f"[✗] ORDO not available (skipping)")

    if doid_path.exists():
        runner_kwargs["doid_owl_path"] = doid_path
        print(f"[✓] DOID available: {doid_path}")
    else:
        print(f"[✗] DOID not available (skipping)")

    runner = PipelineRunner(**runner_kwargs)

    # Run pipeline with small counts for testing
    print("\n[1] Running extraction pipeline...")
    packets = runner.run(
        omim_synonym_count=3,
        ordo_synonym_count=3 if ordo_path.exists() else 0,
        doid_synonym_count=3 if doid_path.exists() else 0,
        doid_hierarchy_count=3 if doid_path.exists() else 0,
    )

    print(f"    Generated {len(packets)} packets")
    assert len(packets) > 0, "Should generate at least some packets"

    # Analyze packets by source
    sources = {}
    predicates = {}
    for packet in packets:
        # SIEVE: evidence is in hasEvidenceLines
        if packet.hasEvidenceLines:
            for line in packet.hasEvidenceLines:
                for item in line.hasEvidenceItems or []:
                    if hasattr(item, 'sourceName') and item.sourceName:
                        source = item.sourceName
                        sources[source] = sources.get(source, 0) + 1
        # Get predicate label
        pred = packet.statement.predicate
        if pred:
            pred_str = pred.label if hasattr(pred, 'label') and pred.label else str(pred)
        else:
            pred_str = "unknown"
        predicates[pred_str] = predicates.get(pred_str, 0) + 1

    print(f"\n[2] Packets by source:")
    for source, count in sorted(sources.items()):
        print(f"    - {source}: {count}")

    print(f"\n[3] Packets by predicate:")
    for pred, count in sorted(predicates.items()):
        print(f"    - {pred}: {count}")

    # Verify packet structure
    print(f"\n[4] Verifying packet structure...")
    for packet in packets[:3]:
        assert packet.id, "Packet should have ID"
        assert packet.statement, "Packet should have statement"
        assert packet.statement.subject, "Statement should have subject (Mondo ID)"
        assert packet.hasEvidenceLines, "Packet should have evidence lines"
        print(f"    {packet.id}: {packet.statement.subjectLabel} -> {packet.statement.object}")

    # Export to temp directory
    print(f"\n[5] Exporting packets...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        export_packets_yaml(packets, tmppath)
        export_packets_summary(packets, tmppath / "summary.yaml")

        yaml_files = list(tmppath.glob("pkt_*.yaml"))
        print(f"    Exported {len(yaml_files)} packet files")
        assert len(yaml_files) == len(packets), "Should export all packets"

        summary_path = tmppath / "summary.yaml"
        assert summary_path.exists(), "Should create summary"
        with open(summary_path) as f:
            summary = yaml.safe_load(f)
        print(f"    Summary: {summary['total_packets']} total packets")

    print("\n" + "="*60)
    print("MULTI-SOURCE TEST COMPLETED SUCCESSFULLY")
    print("="*60)
