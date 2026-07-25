"""Tests for evidence packet statistics generation."""

from datetime import date
from pathlib import Path


from mondo_ai.datamodel import (
    ConcordanceItem,
    CurationStatus,
    EvidenceItemType,
    EvidencePacket,
    InformationEntity,
    SieveEvidenceLine,
    SieveStatement,
)
from mondo_ai.datamodel.sieve_models import Coding
from mondo_ai.stats import compute_stats


def _make_packet(
    packet_id: str,
    predicate_code: str = "oio:hasExactSynonym",
    predicate_label: str = "exact synonym",
    source_id: str = "omim",
    source_name: str = "OMIM",
    direction: str = "supports",
    strength: str = "strong",
    score: float = 0.95,
    status: CurationStatus = CurationStatus.UNREVIEWED,
    num_evidence_lines: int = 1,
    num_items_per_line: int = 1,
    subject: str = "MONDO:0000001",
    subject_label: str = "test disease",
    object_value: str = "test synonym",
) -> EvidencePacket:
    """Create a test packet with configurable properties."""
    evidence_lines = []
    for i in range(num_evidence_lines):
        items: list[InformationEntity] = []
        for j in range(num_items_per_line):
            items.append(
                ConcordanceItem(
                    id=f"ev_{packet_id}_{i}_{j}",
                    type="ConcordanceItem",
                    evidenceItemType=EvidenceItemType.ConcordanceItem,
                    sourceName=source_name,
                    sourceId=source_id,
                    description=f"Evidence item {j}",
                )
            )
        evidence_lines.append(
            SieveEvidenceLine(
                id=f"el_{packet_id}_{i}",
                type="SieveEvidenceLine",
                hasEvidenceItems=items,
                directionOfEvidenceProvided=direction,
                strengthOfEvidenceProvided=strength,
                scoreOfEvidenceProvided=score,
            )
        )

    return EvidencePacket(
        id=packet_id,
        statement=SieveStatement(
            id=f"stmt_{packet_id}",
            type="SieveStatement",
            subject=subject,
            subjectLabel=subject_label,
            predicate=Coding(code=predicate_code, label=predicate_label),
            object=object_value,
            objectLabel=object_value,
            statementText=f"{subject_label} has {predicate_label} '{object_value}'",
        ),
        status=status,
        hasEvidenceLines=evidence_lines,
        created=date(2026, 3, 18),
    )


class TestComputeStats:
    """Tests for compute_stats function."""

    def test_empty_packets(self) -> None:
        """Stats from empty list should have zero counts."""
        stats = compute_stats([])
        assert stats.total_packets == 0
        assert stats.total_evidence_lines == 0
        assert stats.total_evidence_items == 0

    def test_single_packet_counts(self) -> None:
        """Single packet should count correctly."""
        packets = [_make_packet("pkt_001")]
        stats = compute_stats(packets)
        assert stats.total_packets == 1
        assert stats.total_evidence_lines == 1
        assert stats.total_evidence_items == 1

    def test_multiple_evidence_lines(self) -> None:
        """Multiple evidence lines and items should be counted."""
        packets = [
            _make_packet("pkt_001", num_evidence_lines=3, num_items_per_line=2),
        ]
        stats = compute_stats(packets)
        assert stats.total_evidence_lines == 3
        assert stats.total_evidence_items == 6

    def test_by_predicate(self) -> None:
        """Packets should be grouped by predicate type."""
        packets = [
            _make_packet("pkt_001", predicate_label="exact synonym"),
            _make_packet("pkt_002", predicate_label="exact synonym"),
            _make_packet("pkt_003", predicate_label="parent"),
        ]
        stats = compute_stats(packets)
        assert stats.by_predicate["exact synonym"] == 2
        assert stats.by_predicate["parent"] == 1

    def test_by_source(self) -> None:
        """Packets should be grouped by source."""
        packets = [
            _make_packet("pkt_001", source_id="omim"),
            _make_packet("pkt_002", source_id="ordo"),
            _make_packet("pkt_003", source_id="omim"),
        ]
        stats = compute_stats(packets)
        assert stats.by_source["omim"] == 2
        assert stats.by_source["ordo"] == 1

    def test_by_status(self) -> None:
        """Packets should be grouped by curation status."""
        packets = [
            _make_packet("pkt_001", status=CurationStatus.UNREVIEWED),
            _make_packet("pkt_002", status=CurationStatus.ACCEPTED),
        ]
        stats = compute_stats(packets)
        assert stats.by_status["UNREVIEWED"] == 1
        assert stats.by_status["ACCEPTED"] == 1

    def test_by_direction(self) -> None:
        """Evidence lines should be grouped by direction."""
        packets = [
            _make_packet("pkt_001", direction="supports"),
            _make_packet("pkt_002", direction="disputes"),
            _make_packet("pkt_003", direction="supports"),
        ]
        stats = compute_stats(packets)
        assert stats.by_direction["supports"] == 2
        assert stats.by_direction["disputes"] == 1

    def test_by_strength(self) -> None:
        """Evidence lines should be grouped by strength."""
        packets = [
            _make_packet("pkt_001", strength="strong"),
            _make_packet("pkt_002", strength="weak"),
        ]
        stats = compute_stats(packets)
        assert stats.by_strength["strong"] == 1
        assert stats.by_strength["weak"] == 1

    def test_score_distribution(self) -> None:
        """Scores should be bucketed into distribution ranges."""
        packets = [
            _make_packet("pkt_001", score=0.95),
            _make_packet("pkt_002", score=0.60),
            _make_packet("pkt_003", score=0.30),
        ]
        stats = compute_stats(packets)
        assert stats.score_distribution["high (>= 0.8)"] == 1
        assert stats.score_distribution["medium (0.5 - 0.8)"] == 1
        assert stats.score_distribution["low (< 0.5)"] == 1

    def test_unique_mondo_subjects(self) -> None:
        """Should count unique Mondo subjects."""
        packets = [
            _make_packet("pkt_001", subject="MONDO:0000001"),
            _make_packet("pkt_002", subject="MONDO:0000001"),
            _make_packet("pkt_003", subject="MONDO:0000002"),
        ]
        stats = compute_stats(packets)
        assert stats.unique_subjects == 2

    def test_by_evidence_item_type(self) -> None:
        """Evidence items should be grouped by type."""
        packets = [_make_packet("pkt_001")]
        stats = compute_stats(packets)
        assert stats.by_evidence_item_type["ConcordanceItem"] == 1

    def test_by_evidence_count(self) -> None:
        """Packets should be grouped by number of evidence lines."""
        packets = [
            _make_packet("pkt_001", num_evidence_lines=1),
            _make_packet("pkt_002", num_evidence_lines=2),
            _make_packet("pkt_003", num_evidence_lines=2),
            _make_packet("pkt_004", num_evidence_lines=3),
        ]
        stats = compute_stats(packets)
        assert stats.by_evidence_count["1"] == 1
        assert stats.by_evidence_count["2"] == 2
        assert stats.by_evidence_count["3"] == 1


class TestPacketStatsMarkdown:
    """Tests for markdown rendering."""

    def test_to_markdown_contains_header(self) -> None:
        """Markdown output should contain a title."""
        stats = compute_stats([_make_packet("pkt_001")])
        md = stats.to_markdown()
        assert "# Evidence Packet Statistics" in md

    def test_to_markdown_contains_summary(self) -> None:
        """Markdown should contain summary counts."""
        stats = compute_stats([_make_packet("pkt_001")])
        md = stats.to_markdown()
        assert "Total packets" in md
        assert "1" in md

    def test_to_markdown_contains_tables(self) -> None:
        """Markdown should contain distribution tables."""
        packets = [
            _make_packet("pkt_001", source_id="omim"),
            _make_packet("pkt_002", source_id="ordo"),
        ]
        stats = compute_stats(packets)
        md = stats.to_markdown()
        assert "omim" in md
        assert "ordo" in md
        assert "Source" in md


class TestStatsFromJSONLStore:
    """Tests for computing stats from a JSONL store."""

    def test_stats_from_store(self, tmp_path: Path) -> None:
        """Should compute stats by iterating over a JSONL store."""
        from mondo_ai.storage.jsonl_store import JSONLPacketStore

        store = JSONLPacketStore(tmp_path / "store")
        packets = [
            _make_packet("pkt_001", source_id="omim"),
            _make_packet("pkt_002", source_id="ordo"),
            _make_packet("pkt_003", source_id="doid"),
        ]
        store.add_packets(packets)

        stats = compute_stats(store.iter_packets())
        assert stats.total_packets == 3
        assert stats.by_source["omim"] == 1
        assert stats.by_source["ordo"] == 1
        assert stats.by_source["doid"] == 1
