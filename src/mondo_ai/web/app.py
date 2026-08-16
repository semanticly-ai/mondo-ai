"""FastAPI web application for browsing evidence packets.

Provides a dashboard with statistics and a browsable list of evidence packets
with filtering by source, predicate, and status.

Run with: just serve-web
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mondo_ai.stats import compute_stats, PacketStats
from mondo_ai.storage.jsonl_store import JSONLPacketStore

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

STORE_DIR = Path("packet_store_merged")

app = FastAPI(title="MondoAI Evidence Packet Browser")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Lazy-loaded singletons
_store: JSONLPacketStore | None = None
_stats: PacketStats | None = None
_index_cache: dict[str, Any] | None = None


def _get_store() -> JSONLPacketStore:
    """Get or create the packet store singleton."""
    global _store
    if _store is None:
        _store = JSONLPacketStore(STORE_DIR)
    return _store


def _get_stats() -> PacketStats:
    """Get or compute cached statistics."""
    global _stats
    if _stats is None:
        store = _get_store()
        _stats = compute_stats(store.iter_packets())
    return _stats


def _get_index() -> dict[str, Any]:
    """Load the raw index for fast listing/filtering."""
    global _index_cache
    if _index_cache is None:
        index_path = STORE_DIR / "index.json"
        with open(index_path) as f:
            _index_cache = json.load(f)
    return _index_cache


def _packet_summary(packet: Any) -> dict[str, Any]:
    """Extract a summary dict from an EvidencePacket for list display."""
    stmt = packet.statement
    predicate_label = ""
    if stmt and stmt.predicate:
        predicate_label = stmt.predicate.label or stmt.predicate.code or ""

    # Collect source IDs from evidence items
    sources: list[str] = []
    n_evidence = 0
    for el in packet.has_evidence_lines or []:
        n_evidence += 1
        for item in el.has_evidence_items or []:
            src = getattr(item, "source_id", None)
            if src and src not in sources:
                sources.append(src)

    score = None
    direction = None
    if packet.evidence_synthesis:
        if packet.evidence_synthesis.score:
            score = packet.evidence_synthesis.score.value
        direction = packet.evidence_synthesis.direction

    # Get score from first evidence line as fallback
    if score is None and packet.has_evidence_lines:
        first_line = (packet.has_evidence_lines or [None])[0]
        if first_line and first_line.score_of_evidence_provided is not None:
            score = first_line.score_of_evidence_provided

    return {
        "id": packet.id,
        "subject": stmt.subject if stmt else "",
        "subject_label": stmt.subject_label if stmt else "",
        "predicate": predicate_label,
        "object": stmt.object if stmt else "",
        "object_label": stmt.object_label if stmt else "",
        "status": str(packet.status) if packet.status else "UNREVIEWED",
        "sources": sources,
        "n_evidence": n_evidence,
        "score": score,
        "direction": direction,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard with summary statistics."""
    stats = _get_stats()
    store = _get_store()
    store_stats = store.get_stats()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "stats": stats, "store_stats": store_stats},
    )


@app.get("/packets", response_class=HTMLResponse)
async def packet_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    source: str | None = Query(None),
    predicate: str | None = Query(None),
    subject: str | None = Query(None),
    q: str | None = Query(None),
    min_evidence: int | None = Query(None, ge=1),
) -> HTMLResponse:
    """Paginated, filterable list of evidence packets."""
    store = _get_store()

    # Collect all packet summaries (cached after first load)
    summaries = _get_packet_summaries(store)

    # Apply filters
    filtered = summaries
    if source:
        filtered = [s for s in filtered if source in s["sources"]]
    if predicate:
        filtered = [s for s in filtered if predicate == s["predicate"]]
    if subject:
        filtered = [
            s for s in filtered
            if subject.lower() in (s["subject"] or "").lower()
            or subject.lower() in (s["subject_label"] or "").lower()
        ]
    if q:
        q_lower = q.lower()
        filtered = [
            s for s in filtered
            if q_lower in (s["subject"] or "").lower()
            or q_lower in (s["subject_label"] or "").lower()
            or q_lower in (s["object"] or "").lower()
            or q_lower in (s["object_label"] or "").lower()
            or q_lower in s["id"].lower()
        ]
    if min_evidence is not None:
        filtered = [s for s in filtered if s["n_evidence"] >= min_evidence]

    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    page_items = filtered[start : start + per_page]

    # Get available filter values
    stats = _get_stats()

    return templates.TemplateResponse(
        "packets.html",
        {
            "request": request,
            "packets": page_items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "source": source or "",
            "predicate": predicate or "",
            "subject": subject or "",
            "q": q or "",
            "min_evidence": min_evidence or "",
            "available_sources": sorted(stats.by_source.keys()),
            "available_predicates": sorted(stats.by_predicate.keys()),
            "available_evidence_counts": sorted(
                int(k) for k in stats.by_evidence_count.keys()
            ),
        },
    )


_summaries_cache: list[dict[str, Any]] | None = None


def _get_packet_summaries(store: JSONLPacketStore) -> list[dict[str, Any]]:
    """Get cached list of all packet summaries."""
    global _summaries_cache
    if _summaries_cache is None:
        _summaries_cache = [_packet_summary(p) for p in store.iter_packets()]
    return _summaries_cache


@app.get("/packets/{packet_id}", response_class=HTMLResponse)
async def packet_detail(request: Request, packet_id: str) -> HTMLResponse:
    """Detail view for a single evidence packet."""
    store = _get_store()
    packet = store.get_packet(packet_id)
    if packet is None:
        return templates.TemplateResponse(
            "not_found.html",
            {"request": request, "packet_id": packet_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        "packet_detail.html",
        {"request": request, "packet": packet},
    )


@app.get("/api/stats")
async def api_stats() -> dict:
    """JSON API endpoint for statistics."""
    stats = _get_stats()
    return {
        "total_packets": stats.total_packets,
        "total_evidence_lines": stats.total_evidence_lines,
        "total_evidence_items": stats.total_evidence_items,
        "unique_subjects": stats.unique_subjects,
        "by_predicate": stats.by_predicate,
        "by_source": stats.by_source,
        "by_status": stats.by_status,
    }
