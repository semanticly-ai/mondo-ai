"""Evidence packet statistics computation and markdown rendering.

Computes summary statistics from SIEVE profile packets (as dicts) and renders them as a
markdown dashboard.

Example:
    >>> from mondo_ai.stats import compute_stats
    >>> from mondo_ai.storage.jsonl_store import JSONLPacketStore
    >>> store = JSONLPacketStore("packet_store/")  # doctest: +SKIP
    >>> stats = compute_stats(store.iter_packets())  # doctest: +SKIP
    >>> print(stats.to_markdown())  # doctest: +SKIP
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass
class PacketStats:
    """Summary statistics for a collection of evidence packets.

    Attributes:
        total_packets: Total number of packets.
        total_evidence_lines: Total evidence lines across all packets.
        total_evidence_items: Total items held directly by those lines.
        unique_subjects: Number of unique Mondo subject IDs.
        by_claim_type: Packet counts by claim type (statement class).
        by_predicate: Packet counts by statement predicate label.
        by_review_state: Packet counts by review state.
        by_source: Evidence line counts by the resource of their items.
        by_direction: Evidence line counts by direction.
        by_strength: Evidence line counts by strength band.
        by_evidence_item_type: Evidence item counts by type.
        score_distribution: Packet counts by inclusion score S bucket.
        by_evidence_count: Packet counts by number of evidence lines.
    """

    total_packets: int = 0
    total_evidence_lines: int = 0
    total_evidence_items: int = 0
    unique_subjects: int = 0
    by_claim_type: dict[str, int] = field(default_factory=dict)
    by_predicate: dict[str, int] = field(default_factory=dict)
    by_review_state: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    by_direction: dict[str, int] = field(default_factory=dict)
    by_strength: dict[str, int] = field(default_factory=dict)
    by_evidence_item_type: dict[str, int] = field(default_factory=dict)
    score_distribution: dict[str, int] = field(default_factory=dict)
    by_evidence_count: dict[str, int] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """Render statistics as a markdown dashboard.

        Returns:
            Markdown-formatted string with tables and summary counts.
        """
        lines = [
            "# Evidence Packet Statistics",
            "",
            f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
            "",
            "## Summary",
            "",
            "| Metric | Count |",
            "|--------|------:|",
            f"| Total packets | {self.total_packets:,} |",
            f"| Total evidence lines | {self.total_evidence_lines:,} |",
            f"| Total evidence items | {self.total_evidence_items:,} |",
            f"| Unique Mondo subjects | {self.unique_subjects:,} |",
            "",
        ]
        lines.extend(_render_table("By Claim Type", "Claim type", self.by_claim_type))
        lines.extend(_render_table("By Statement Predicate", "Predicate", self.by_predicate))
        lines.extend(_render_table("By Review State", "Review state", self.by_review_state))
        lines.extend(_render_table("Inclusion Score", "S", self.score_distribution))
        lines.extend(_render_table("Evidence Lines by Source", "Source", self.by_source))
        lines.extend(_render_table("Evidence Lines by Direction", "Direction", self.by_direction))
        lines.extend(_render_table("Evidence Lines by Strength", "Strength", self.by_strength))
        lines.extend(_render_table("By Evidence Item Type", "Type", self.by_evidence_item_type))
        lines.extend(_render_table("Evidence Lines per Packet", "Evidence Lines", self.by_evidence_count))
        return "\n".join(lines)


def _render_table(title: str, col_name: str, data: dict[str, int]) -> list[str]:
    """Render a single stats table section."""
    if not data:
        return []
    total = sum(data.values())
    rows = [f"## {title}", "", f"| {col_name} | Count | % |", "|--------|------:|---:|"]
    for key in sorted(data, key=data.get, reverse=True):  # type: ignore[arg-type]
        count = data[key]
        pct = (count / total * 100) if total > 0 else 0
        rows.append(f"| {key} | {count:,} | {pct:.1f}% |")
    rows.append("")
    return rows


def score_bucket(score: float) -> str:
    """Assign an inclusion score S to a named bucket.

    >>> score_bucket(0.85), score_bucket(-0.1)
    ('S >= 0.8', 'S <= 0')
    """
    if score >= 0.8:
        return "S >= 0.8"
    if score >= 0.4:
        return "0.4 <= S < 0.8"
    if score > 0:
        return "0 < S < 0.4"
    return "S <= 0"


def line_source(line: dict[str, Any]) -> str:
    """The resource a line's evidence comes from: its first item that names one."""
    for item in line.get("has_evidence_items") or []:
        if item.get("resource"):
            return str(item["resource"])
    return str(line.get("independence_cluster") or "unknown")


def compute_stats(packets: Iterable[dict[str, Any]]) -> PacketStats:
    """Compute summary statistics from evidence packets.

    Args:
        packets: Iterable of packet dicts (list or store iterator).

    Returns:
        PacketStats with computed counts and distributions.
    """
    claim_types: Counter[str] = Counter()
    predicates: Counter[str] = Counter()
    review_states: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    directions: Counter[str] = Counter()
    strengths: Counter[str] = Counter()
    item_types: Counter[str] = Counter()
    scores: Counter[str] = Counter()
    line_counts: Counter[str] = Counter()
    subjects: set[str] = set()
    total_packets = total_lines = total_items = 0

    for packet in packets:
        total_packets += 1
        st = packet["statement"]
        lines = st.get("has_evidence_lines") or []
        line_counts[str(len(lines))] += 1
        subjects.add(st["subject"])
        claim_types[st["type"]] += 1
        pred = st.get("predicate") or {}
        predicates[pred.get("label") or pred.get("code") or "unknown"] += 1
        review_states[st.get("review_state") or "unknown"] += 1
        if st.get("score") is not None:
            scores[score_bucket(st["score"])] += 1
        for line in lines:
            total_lines += 1
            sources[line_source(line)] += 1
            if line.get("direction_of_evidence_provided"):
                directions[line["direction_of_evidence_provided"]] += 1
            if line.get("strength_of_evidence_provided"):
                strengths[line["strength_of_evidence_provided"]] += 1
            for item in line.get("has_evidence_items") or []:
                total_items += 1
                item_types[item["type"]] += 1

    return PacketStats(
        total_packets=total_packets,
        total_evidence_lines=total_lines,
        total_evidence_items=total_items,
        unique_subjects=len(subjects),
        by_claim_type=dict(claim_types),
        by_predicate=dict(predicates),
        by_review_state=dict(review_states),
        by_source=dict(sources),
        by_direction=dict(directions),
        by_strength=dict(strengths),
        by_evidence_item_type=dict(item_types),
        score_distribution=dict(scores),
        by_evidence_count=dict(line_counts),
    )
