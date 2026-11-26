"""Tests for evidence packet statistics generation."""

from pathlib import Path

from mondo_ai.stats import compute_stats


def _make_packet(
    packet_id: str,
    claim_type: str = "SynonymStatement",
    predicate_code: str = "oboInOwl:hasExactSynonym",
    predicate_label: str = "has exact synonym",
    resource: str = "infores:omim",
    direction: str = "supports",
    strength: str = "strong",
    line_score: float = 0.95,
    score: float = 0.9,
    review_state: str = "PENDING_REVIEW",
    num_evidence_lines: int = 1,
    num_items_per_line: int = 1,
    subject: str = "MONDO:0000001",
) -> dict:
    """A profile packet dict with configurable properties."""
    lines = []
    for i in range(num_evidence_lines):
        items = [
            {
                "id": f"pc_{packet_id}_{i}_{j}",
                "type": claim_type,
                "subject": "OMIM:1",
                "predicate": {"code": predicate_code},
                "object": "x",
                "resource": resource,
            }
            for j in range(num_items_per_line)
        ]
        lines.append(
            {
                "id": f"el_{packet_id}_{i}",
                "type": "SieveEvidenceLine",
                "has_evidence_items": items,
                "direction_of_evidence_provided": direction,
                "strength_of_evidence_provided": strength,
                "score_of_evidence_provided": line_score,
                "independence_cluster": resource,
            }
        )
    return {
        "id": packet_id,
        "type": "EvidencePacket",
        "version": "2026-11-20",
        "statement": {
            "id": f"st_{packet_id}",
            "type": claim_type,
            "subject": subject,
            "predicate": {"code": predicate_code, "label": predicate_label},
            "object": "test synonym",
            "review_state": review_state,
            "score": score,
            "has_evidence_lines": lines,
        },
        "reference_snapshot": {"id": f"{packet_id}#references", "type": "SieveRegistry"},
    }


class TestComputeStats:
    """Tests for compute_stats function."""

    def test_empty_packets(self) -> None:
        stats = compute_stats([])
        assert stats.total_packets == 0
        assert stats.total_evidence_lines == 0
        assert stats.total_evidence_items == 0

    def test_single_packet_counts(self) -> None:
        stats = compute_stats([_make_packet("pkt_001")])
        assert stats.total_packets == 1
        assert stats.total_evidence_lines == 1
        assert stats.total_evidence_items == 1

    def test_multiple_evidence_lines(self) -> None:
        stats = compute_stats([_make_packet("pkt_001", num_evidence_lines=3, num_items_per_line=2)])
        assert stats.total_evidence_lines == 3
        assert stats.total_evidence_items == 6

    def test_by_claim_type_and_predicate(self) -> None:
        packets = [
            _make_packet("pkt_001"),
            _make_packet("pkt_002"),
            _make_packet("pkt_003", claim_type="SubclassStatement", predicate_code="rdfs:subClassOf", predicate_label="subclass of"),
        ]
        stats = compute_stats(packets)
        assert stats.by_claim_type == {"SynonymStatement": 2, "SubclassStatement": 1}
        assert stats.by_predicate["has exact synonym"] == 2
        assert stats.by_predicate["subclass of"] == 1

    def test_by_source_counts_lines_by_item_resource(self) -> None:
        packets = [
            _make_packet("pkt_001", resource="infores:omim"),
            _make_packet("pkt_002", resource="infores:orphanet"),
            _make_packet("pkt_003", resource="infores:omim"),
        ]
        stats = compute_stats(packets)
        assert stats.by_source["infores:omim"] == 2
        assert stats.by_source["infores:orphanet"] == 1

    def test_by_review_state(self) -> None:
        packets = [_make_packet("pkt_001"), _make_packet("pkt_002", review_state="NOT_VERIFIED")]
        stats = compute_stats(packets)
        assert stats.by_review_state == {"PENDING_REVIEW": 1, "NOT_VERIFIED": 1}

    def test_by_direction(self) -> None:
        packets = [
            _make_packet("pkt_001", direction="supports"),
            _make_packet("pkt_002", direction="disputes"),
            _make_packet("pkt_003", direction="supports"),
        ]
        stats = compute_stats(packets)
        assert stats.by_direction["supports"] == 2
        assert stats.by_direction["disputes"] == 1

    def test_by_strength(self) -> None:
        stats = compute_stats([_make_packet("pkt_001", strength="strong"), _make_packet("pkt_002", strength="weak")])
        assert stats.by_strength["strong"] == 1
        assert stats.by_strength["weak"] == 1

    def test_score_distribution_buckets_the_inclusion_score(self) -> None:
        packets = [
            _make_packet("pkt_001", score=0.95),
            _make_packet("pkt_002", score=0.60),
            _make_packet("pkt_003", score=0.10),
            _make_packet("pkt_004", score=-0.3),
        ]
        stats = compute_stats(packets)
        assert stats.score_distribution == {
            "S >= 0.8": 1,
            "0.4 <= S < 0.8": 1,
            "0 < S < 0.4": 1,
            "S <= 0": 1,
        }

    def test_unique_mondo_subjects(self) -> None:
        packets = [
            _make_packet("pkt_001", subject="MONDO:0000001"),
            _make_packet("pkt_002", subject="MONDO:0000001"),
            _make_packet("pkt_003", subject="MONDO:0000002"),
        ]
        assert compute_stats(packets).unique_subjects == 2

    def test_by_evidence_item_type(self) -> None:
        stats = compute_stats([_make_packet("pkt_001")])
        assert stats.by_evidence_item_type["SynonymStatement"] == 1

    def test_by_evidence_count(self) -> None:
        packets = [
            _make_packet("pkt_001", num_evidence_lines=1),
            _make_packet("pkt_002", num_evidence_lines=2),
            _make_packet("pkt_003", num_evidence_lines=2),
            _make_packet("pkt_004", num_evidence_lines=3),
        ]
        stats = compute_stats(packets)
        assert stats.by_evidence_count == {"1": 1, "2": 2, "3": 1}


class TestPacketStatsMarkdown:
    """Tests for markdown rendering."""

    def test_to_markdown_contains_header_and_summary(self) -> None:
        md = compute_stats([_make_packet("pkt_001")]).to_markdown()
        assert "# Evidence Packet Statistics" in md
        assert "| Total packets | 1 |" in md

    def test_to_markdown_contains_tables(self) -> None:
        packets = [_make_packet("pkt_001", resource="infores:omim"), _make_packet("pkt_002", resource="infores:orphanet")]
        md = compute_stats(packets).to_markdown()
        assert "infores:omim" in md
        assert "infores:orphanet" in md
        assert "## By Claim Type" in md


class TestStatsFromJSONLStore:
    """Tests for computing stats from a JSONL store."""

    def test_stats_from_store(self, tmp_path: Path) -> None:
        from mondo_ai.storage.jsonl_store import JSONLPacketStore

        store = JSONLPacketStore(tmp_path / "store")
        store.add_packets(
            [
                _make_packet("pkt_001", resource="infores:omim"),
                _make_packet("pkt_002", resource="infores:orphanet"),
                _make_packet("pkt_003", resource="infores:disease-ontology"),
            ]
        )
        stats = compute_stats(store.iter_packets())
        assert stats.total_packets == 3
        assert stats.by_source == {"infores:omim": 1, "infores:orphanet": 1, "infores:disease-ontology": 1}
