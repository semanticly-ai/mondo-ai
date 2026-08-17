"""Export the packet store to a static JSON payload for the docs Evidence Browser.

GitHub Pages is static, so the browser can't run the FastAPI app. Instead we dump
the committed store to a compact ``data.json`` that the mkdocs ``evidence/`` page
loads client-side. The docs build regenerates it from the current store, so every
push resets the deployed browser to whatever is committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mondo_ai.stats import compute_stats
from mondo_ai.storage.jsonl_store import JSONLPacketStore


def _val(x: Any) -> Any:
    """Return an enum's value, or the object unchanged (works for plain strings)."""
    return getattr(x, "value", x)


def _packet_record(packet: Any) -> dict[str, Any]:
    """Compact per-packet record: enough for the list and expandable detail."""
    stmt = packet.statement
    predicate = ""
    if stmt and stmt.predicate:
        predicate = stmt.predicate.label or stmt.predicate.code or ""

    sources: list[str] = []
    evidence: list[dict[str, Any]] = []
    for line in packet.has_evidence_lines or []:
        source_name = None
        source_subject = None
        justification = None
        for item in line.has_evidence_items or []:
            source_name = getattr(item, "source_name", None) or source_name
            sid = getattr(item, "source_id", None)
            if sid and sid not in sources:
                sources.append(sid)
            source_subject = getattr(item, "source_subject", None) or source_subject
            justification = getattr(item, "mapping_justification", None) or justification
        evidence.append(
            {
                "source": source_name,
                "direction": _val(line.direction_of_evidence_provided),
                "strength": _val(line.strength_of_evidence_provided),
                "score": line.score_of_evidence_provided,
                "source_subject": source_subject,
                "justification": justification,
            }
        )

    score = None
    if packet.evidence_synthesis and packet.evidence_synthesis.score:
        score = packet.evidence_synthesis.score.value
    if score is None:
        # Fall back to the first evidence line's score (as the FastAPI app does).
        for line in packet.has_evidence_lines or []:
            if line.score_of_evidence_provided is not None:
                score = line.score_of_evidence_provided
                break

    return {
        "id": packet.id,
        "subject": stmt.subject if stmt else None,
        "subject_label": stmt.subject_label if stmt else None,
        "predicate": predicate,
        "object": stmt.object if stmt else None,
        "object_label": stmt.object_label if stmt else None,
        "status": _val(packet.status) if packet.status else "UNREVIEWED",
        "score": score,
        "sources": sources,
        "n_evidence": len(packet.has_evidence_lines or []),
        "evidence": evidence,
    }


def export_web_data(store_dir: Path | str, out_path: Path | str) -> int:
    """Write ``{stats, packets}`` JSON for the docs Evidence Browser.

    Args:
        store_dir: Directory of the JSONL packet store to export.
        out_path: Destination JSON file (parent dirs are created).

    Returns:
        Number of packets exported.
    """
    store = JSONLPacketStore(Path(store_dir))
    packets = list(store.iter_packets())
    stats = compute_stats(packets)

    payload = {
        "stats": {
            "total_packets": stats.total_packets,
            "total_evidence_lines": stats.total_evidence_lines,
            "total_evidence_items": stats.total_evidence_items,
            "unique_subjects": stats.unique_subjects,
            "by_source": stats.by_source,
            "by_predicate": stats.by_predicate,
            "by_status": stats.by_status,
            "by_direction": stats.by_direction,
        },
        "packets": [_packet_record(p) for p in packets],
    }

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    return len(packets)
