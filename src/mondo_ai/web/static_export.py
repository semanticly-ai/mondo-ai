"""Export the packet store to a static JSON payload for the docs Evidence Browser.

GitHub Pages is static, so the browser can't run the FastAPI app. Instead we dump the
store to a compact ``data.json`` that the mkdocs ``evidence/`` page
(``docs/evidence/index.md``) loads client-side. The payload is::

    {"stats": {...}, "fields": FIELDS, "packets": [[value per field], ...]}

One array per packet with a single column header keeps ~200k packets small; the page
zips each row with ``fields``. Evidence detail is not exported (the FastAPI app shows it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from mondo_ai.stats import compute_stats
from mondo_ai.storage.index import summarize_packet
from mondo_ai.storage.jsonl_store import JSONLPacketStore

FIELDS = [
    "id",
    "claim_type",
    "subject",
    "predicate",
    "object",
    "statement_text",
    "review_state",
    "score",
    "strength",
    "line_count",
    "sources",
]


def packet_row(packet: dict[str, Any]) -> list[Any]:
    """One packet as a list of values in FIELDS order (predicate as its label).

    >>> packet_row({"id": "mondoai:pkt/1", "statement": {"type": "SynonymStatement",
    ...     "subject": "MONDO:1", "object": "CF", "statement_text": "x has exact synonym 'CF'",
    ...     "predicate": {"code": "oboInOwl:hasExactSynonym", "label": "has exact synonym"},
    ...     "review_state": "PENDING_REVIEW", "score": 0.5, "strength": "moderate",
    ...     "has_evidence_lines": [{"has_evidence_items": [{"resource": "infores:omim"}]}]}})[3:]
    ['has exact synonym', 'CF', "x has exact synonym 'CF'", 'PENDING_REVIEW', 0.5, 'moderate', 1, ['infores:omim']]
    """
    s = summarize_packet(packet)
    return [
        s["packet_id"],
        s["claim_type"],
        s["subject"],
        s["predicate_label"] or s["predicate"],
        s["object"],
        s["statement_text"],
        s["review_state"],
        s["score"],
        s["strength"],
        s["line_count"],
        s["sources"],
    ]


def _collect(packets: Iterable[dict[str, Any]], rows: list[list[Any]]) -> Iterator[dict[str, Any]]:
    """Pass packets through while appending each one's row, so stats and rows take one pass."""
    for packet in packets:
        rows.append(packet_row(packet))
        yield packet


def export_web_data(store_dir: Path | str, out_path: Path | str) -> int:
    """Write the ``{stats, fields, packets}`` JSON for the docs Evidence Browser.

    Args:
        store_dir: Directory of the JSONL packet store to export.
        out_path: Destination JSON file (parent dirs are created).

    Returns:
        Number of packets exported.
    """
    store = JSONLPacketStore(Path(store_dir))
    rows: list[list[Any]] = []
    stats = compute_stats(_collect(store.iter_packets(), rows))
    payload = {
        "stats": {
            "total_packets": stats.total_packets,
            "total_evidence_lines": stats.total_evidence_lines,
            "unique_subjects": stats.unique_subjects,
            "by_claim_type": stats.by_claim_type,
            "by_predicate": stats.by_predicate,
            "by_review_state": stats.by_review_state,
            "by_source": stats.by_source,
            "by_strength": stats.by_strength,
            "score_distribution": stats.score_distribution,
        },
        "fields": FIELDS,
        "packets": rows,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return len(rows)
