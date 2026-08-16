"""Evidence packet statistics computation and markdown rendering.

Computes summary statistics from evidence packets and renders them
as a markdown dashboard.

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
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from mondo_ai.datamodel import EvidencePacket


@dataclass
class PacketStats:
    """Summary statistics for a collection of evidence packets.

    Attributes:
        total_packets: Total number of packets.
        total_evidence_lines: Total evidence lines across all packets.
        total_evidence_items: Total evidence items across all lines.
        unique_subjects: Number of unique Mondo subject IDs.
        by_predicate: Packet counts by statement predicate label.
        by_source: Packet counts by evidence source ID.
        by_status: Packet counts by curation status.
        by_direction: Evidence line counts by direction.
        by_strength: Evidence line counts by strength.
        by_evidence_item_type: Evidence item counts by type.
        score_distribution: Evidence line counts by score bucket.
        by_evidence_count: Packet counts by number of evidence lines.
    """

    total_packets: int = 0
    total_evidence_lines: int = 0
    total_evidence_items: int = 0
    unique_subjects: int = 0
    by_predicate: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
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

        lines.extend(_render_table("By Statement Predicate", "Predicate", self.by_predicate))
        lines.extend(_render_table("By Source", "Source", self.by_source))
        lines.extend(_render_table("By Curation Status", "Status", self.by_status))
        lines.extend(_render_table("By Evidence Direction", "Direction", self.by_direction))
        lines.extend(_render_table("By Evidence Strength", "Strength", self.by_strength))
        lines.extend(_render_table("By Evidence Item Type", "Type", self.by_evidence_item_type))
        lines.extend(_render_table("Score Distribution", "Bucket", self.score_distribution))
        lines.extend(_render_table("Evidence Lines per Packet", "Evidence Lines", self.by_evidence_count))

        return "\n".join(lines)


def _render_table(title: str, col_name: str, data: dict[str, int]) -> list[str]:
    """Render a single stats table section."""
    if not data:
        return []
    total = sum(data.values())
    rows = [
        f"## {title}",
        "",
        f"| {col_name} | Count | % |",
        "|--------|------:|---:|",
    ]
    for key in sorted(data, key=data.get, reverse=True):  # type: ignore[arg-type]
        count = data[key]
        pct = (count / total * 100) if total > 0 else 0
        rows.append(f"| {key} | {count:,} | {pct:.1f}% |")
    rows.append("")
    return rows


def _score_bucket(score: float) -> str:
    """Assign a score to a named bucket."""
    if score >= 0.8:
        return "high (>= 0.8)"
    if score >= 0.5:
        return "medium (0.5 - 0.8)"
    return "low (< 0.5)"


def compute_stats(packets: Iterable["EvidencePacket"]) -> PacketStats:
    """Compute summary statistics from evidence packets.

    Args:
        packets: Iterable of EvidencePacket objects (list or store iterator).

    Returns:
        PacketStats with computed counts and distributions.
    """
    predicate_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    direction_counter: Counter[str] = Counter()
    strength_counter: Counter[str] = Counter()
    item_type_counter: Counter[str] = Counter()
    score_counter: Counter[str] = Counter()
    evidence_count_counter: Counter[str] = Counter()
    subjects: set[str] = set()

    total_packets = 0
    total_lines = 0
    total_items = 0

    for packet in packets:
        total_packets += 1

        # Evidence lines per packet
        n_lines = len(packet.has_evidence_lines or [])
        evidence_count_counter[str(n_lines)] += 1

        # Subject
        if packet.statement and packet.statement.subject:
            subjects.add(packet.statement.subject)

        # Predicate
        pred = packet.statement.predicate if packet.statement else None
        if pred:
            pred_label = pred.label or pred.code or "unknown"
            predicate_counter[pred_label] += 1

        # Status
        status = packet.status
        if hasattr(status, "value"):
            status_str = status.value
        else:
            status_str = str(status) if status else "UNREVIEWED"
        status_counter[status_str] += 1

        # Evidence lines
        for el in packet.has_evidence_lines or []:
            total_lines += 1

            # Direction
            if el.direction_of_evidence_provided:
                direction_counter[el.direction_of_evidence_provided] += 1

            # Strength
            if el.strength_of_evidence_provided:
                strength_counter[el.strength_of_evidence_provided] += 1

            # Score bucket
            if el.score_of_evidence_provided is not None:
                score_counter[_score_bucket(el.score_of_evidence_provided)] += 1

            # Evidence items
            for item in el.has_evidence_items or []:
                total_items += 1

                # Source (from ConcordanceItem)
                source_id = getattr(item, "source_id", None)
                if source_id:
                    source_counter[source_id] += 1

                # Item type
                item_type = getattr(item, "evidence_item_type", None)
                if item_type:
                    type_str = item_type.value if hasattr(item_type, "value") else str(item_type)
                    item_type_counter[type_str] += 1

    return PacketStats(
        total_packets=total_packets,
        total_evidence_lines=total_lines,
        total_evidence_items=total_items,
        unique_subjects=len(subjects),
        by_predicate=dict(predicate_counter),
        by_source=dict(source_counter),
        by_status=dict(status_counter),
        by_direction=dict(direction_counter),
        by_strength=dict(strength_counter),
        by_evidence_item_type=dict(item_type_counter),
        score_distribution=dict(score_counter),
        by_evidence_count=dict(evidence_count_counter),
    )
