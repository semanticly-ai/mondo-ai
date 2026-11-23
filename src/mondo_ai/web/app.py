"""FastAPI web application for browsing SIEVE profile evidence packets.

Reads packet dicts from a :class:`~mondo_ai.storage.jsonl_store.JSONLPacketStore`
(default ``packet_store_merged``) and serves a dashboard, a filterable packet list and a
packet detail page.

Packet ids contain ``:`` and ``/`` (``mondoai:pkt/abc123``), so pages route by the part
after ``mondoai:pkt/``: ``/packets/abc123``. A URL-encoded full id
(``/packets/mondoai%3Apkt%2Fabc123``) resolves too.

Run with: just serve-web
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mondo_ai.stats import PacketStats, compute_stats
from mondo_ai.storage.index import summarize_packet
from mondo_ai.storage.jsonl_store import JSONLPacketStore

STORE_DIR = Path("packet_store_merged")
PACKET_PREFIX = "mondoai:pkt/"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{}/"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---------------------------------------------------------------------------
# Packet ids and routes
# ---------------------------------------------------------------------------


def packet_path(pid: str) -> str:
    """The detail page path of a packet id.

    >>> packet_path("mondoai:pkt/00011edc897e-icd10cm")
    '/packets/00011edc897e-icd10cm'
    >>> packet_path("x:y/z")
    '/packets/x%3Ay%2Fz'
    """
    if pid.startswith(PACKET_PREFIX):
        return "/packets/" + quote(pid[len(PACKET_PREFIX) :], safe="")
    return "/packets/" + quote(pid, safe="")


def packet_id_from_key(key: str) -> str:
    """The packet id a ``/packets/{key}`` path names: a full id, or the part after the prefix.

    >>> packet_id_from_key("abc123"), packet_id_from_key("mondoai:pkt/abc123")
    ('mondoai:pkt/abc123', 'mondoai:pkt/abc123')
    """
    return key if ":" in key else PACKET_PREFIX + key


def line_anchor(line_id: str) -> str:
    """An HTML id for an evidence line: the last path segment of its id.

    >>> line_anchor("mondoai:el/00011edc897e-icd10cm")
    'line-00011edc897e-icd10cm'
    """
    return "line-" + line_id.rsplit("/", 1)[-1].replace("#", "-")


def pubmed_url(curie: str) -> str | None:
    """The PubMed page of a ``PMID:`` CURIE, else None.

    >>> pubmed_url("PMID:123"), pubmed_url("DOI:1")
    ('https://pubmed.ncbi.nlm.nih.gov/123/', None)
    """
    if curie.startswith("PMID:"):
        return PUBMED_URL.format(curie.split(":", 1)[1])
    return None


# ---------------------------------------------------------------------------
# View models: plain dicts the templates render
# ---------------------------------------------------------------------------


def _publication_view(item: dict[str, Any]) -> dict[str, Any]:
    """A Publication as id, PubMed link and title."""
    return {"kind": "publication", "id": item["id"], "url": pubmed_url(item["id"]), "label": item.get("label")}


def _record_view(item: dict[str, Any]) -> dict[str, Any]:
    """An ExternalRecord as id, label, resource, release and obsolescence."""
    meta = item.get("record_metadata") or {}
    return {
        "kind": "record",
        "id": item["id"],
        "label": item.get("label") or meta.get("source_label"),
        "resource": item.get("resource"),
        "release": meta.get("source_release"),
        "obsolete": bool(item.get("obsolete_in_source")),
    }


def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    """One evidence item as a template-ready dict, by its kind.

    >>> _item_view({"id": "mondoai:cs/1", "type": "ComputationalScore", "value": "1.0",
    ...             "parameters": "match=label_label", "specified_by": ["m"]})["kind"]
    'score'
    >>> _item_view({"id": "mondoai:el/x-omim", "type": "SieveEvidenceLine"})["anchor"]
    'line-x-omim'
    """
    kind = item.get("type")
    if kind == "SieveEvidenceLine":
        return {
            "kind": "line",
            "id": item["id"],
            "anchor": line_anchor(item["id"]),
            "direction": item.get("direction_of_evidence_provided"),
            "strength": item.get("strength_of_evidence_provided"),
        }
    if kind == "Publication":
        return _publication_view(item)
    if kind == "ExternalRecord":
        return _record_view(item)
    if kind == "ComputationalScore":
        return {
            "kind": "score",
            "id": item["id"],
            "value": item.get("value"),
            "parameters": item.get("parameters"),
            "specified_by": item.get("specified_by") or [],
            "software_version": item.get("software_version"),
        }
    if item.get("subject") is not None and item.get("predicate") is not None:
        records = [r for r in item.get("reported_in") or [] if r.get("type") != "Publication"]
        record = _record_view(records[0]) if records else None
        predicate = item["predicate"]
        return {
            "kind": "claim",
            "id": item["id"],
            "type": kind,
            "record_id": record["id"] if record else item["subject"],
            "subject": item["subject"],
            "predicate": predicate.get("label") or predicate.get("code"),
            "predicate_code": predicate.get("code"),
            "object": item.get("object"),
            "record": record,
            "resource": item.get("resource"),
            "context": [c.get("label") or c.get("code") for c in item.get("generation_context") or []],
            "publications": [_publication_view(r) for r in item.get("reported_in") or [] if r.get("type") == "Publication"],
            "resolution_chains": [
                f"{step.get('input_value')} → {step.get('output_value')} ({g.get('entity_slot')})"
                for g in item.get("resolution_chains") or []
                for step in g.get("steps") or []
            ],
            "obsolete": bool(item.get("obsolete_in_source")),
        }
    return {"kind": "other", "id": item.get("id"), "type": kind}


def _line_view(line: dict[str, Any]) -> dict[str, Any]:
    """One evidence line: its interpretation and its items."""
    contributions = line.get("contributions") or []
    act = contributions[0] if contributions else {}
    return {
        "id": line["id"],
        "anchor": line_anchor(line["id"]),
        "description": line.get("description"),
        "direction": line.get("direction_of_evidence_provided"),
        "strength": line.get("strength_of_evidence_provided"),
        "score": line.get("score_of_evidence_provided"),
        "cluster": line.get("independence_cluster"),
        "rule": line.get("computed_by_rule"),
        "confidence": line.get("confidence"),
        "date_attached": line.get("date_attached"),
        "last_confirmed_against": line.get("last_confirmed_against"),
        "contributor": act.get("contributor"),
        "method_type": act.get("method_type"),
        "specified_by": act.get("specified_by") or [],
        "read_coverage": act.get("read_coverage"),
        "rationale": act.get("rationale"),
        "items": [_item_view(i) for i in line.get("has_evidence_items") or []],
    }


def _snapshot_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The frozen reference snapshot: resources with their latest quality, rules, agents, methods."""
    resources = []
    for r in snapshot.get("resources") or []:
        ratings = r.get("quality_ratings") or []
        resources.append({"id": r["id"], "label": r.get("label"), "quality": ratings[-1]["level"] if ratings else None})
    return {
        "resources": resources,
        "releases": snapshot.get("releases") or [],
        "agents": snapshot.get("agents") or [],
        "methods": snapshot.get("methods") or [],
        "rules": snapshot.get("aggregation_rules") or [],
    }


def packet_view(packet: dict[str, Any]) -> dict[str, Any]:
    """Everything the detail page shows, from one packet dict.

    The heading is the statement text (it names the subject), or the subject CURIE.

    >>> view = packet_view({"id": "mondoai:pkt/1", "statement": {"type": "SynonymStatement",
    ...     "subject": "MONDO:1", "object": "CF", "predicate": {"code": "c"}}})
    >>> view["heading"], view["lines"], view["has_evidence"]
    ('MONDO:1', [], [])
    """
    st = packet["statement"]
    predicate = st.get("predicate") or {}
    return {
        "id": packet["id"],
        "version": packet.get("version"),
        "heading": st.get("statement_text") or st["subject"],
        "statement": {
            "id": st.get("id"),
            "type": st["type"],
            "subject": st["subject"],
            "predicate": predicate.get("label") or predicate.get("code"),
            "predicate_code": predicate.get("code"),
            "object": st["object"],
            "text": st.get("statement_text"),
            "review_state": st.get("review_state"),
            "proposing_channel": st.get("proposing_channel"),
            "score": st.get("score"),
            "strength": st.get("strength"),
            "direction": st.get("direction"),
        },
        "lines": [_line_view(li) for li in st.get("has_evidence_lines") or []],
        "has_evidence": [_item_view(i) for i in st.get("has_evidence") or []],
        "snapshot": _snapshot_view(packet.get("reference_snapshot") or {}),
    }


# ---------------------------------------------------------------------------
# Store state
# ---------------------------------------------------------------------------


class _StoreState:
    """The store with its statistics and packet summaries, computed on first use in one pass."""

    def __init__(self, store_dir: Path | str) -> None:
        self.store_dir = Path(store_dir)
        self._store: JSONLPacketStore | None = None
        self._stats: PacketStats | None = None
        self._summaries: list[dict[str, Any]] = []

    @property
    def store(self) -> JSONLPacketStore:
        """The packet store, opened on first use (opening creates the directory)."""
        if self._store is None:
            self._store = JSONLPacketStore(self.store_dir)
        return self._store

    def _load(self) -> None:
        summaries: list[dict[str, Any]] = []

        def collect(packets: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
            for p in packets:
                summaries.append(summarize_packet(p))
                yield p

        self._stats = compute_stats(collect(self.store.iter_packets()))
        self._summaries = summaries

    @property
    def stats(self) -> PacketStats:
        """Statistics over the whole store."""
        if self._stats is None:
            self._load()
        assert self._stats is not None
        return self._stats

    @property
    def summaries(self) -> list[dict[str, Any]]:
        """One summary per packet (see :func:`mondo_ai.storage.index.summarize_packet`)."""
        if self._stats is None:
            self._load()
        return self._summaries


def _bound(value: str | None) -> float | None:
    """A score bound from a query parameter; empty means none.

    >>> _bound(""), _bound(None), _bound("0.5")
    (None, None, 0.5)
    """
    return float(value) if value else None


def _filter(
    summaries: list[dict[str, Any]],
    claim_type: str | None,
    review_state: str | None,
    source: str | None,
    min_score: float | None,
    max_score: float | None,
    q: str | None,
) -> list[dict[str, Any]]:
    """The summaries matching every given filter."""
    out = summaries
    if claim_type:
        out = [s for s in out if s["claim_type"] == claim_type]
    if review_state:
        out = [s for s in out if s["review_state"] == review_state]
    if source:
        out = [s for s in out if source in s["sources"]]
    if min_score is not None:
        out = [s for s in out if s["score"] is not None and s["score"] >= min_score]
    if max_score is not None:
        out = [s for s in out if s["score"] is not None and s["score"] <= max_score]
    if q:
        ql = q.lower()
        out = [
            s
            for s in out
            if ql in (s["statement_text"] or "").lower()
            or ql in s["subject"].lower()
            or ql in s["object"].lower()
            or ql in s["packet_id"].lower()
        ]
    return out


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(store_dir: Path | str = STORE_DIR) -> FastAPI:
    """A browser app over the JSONL store in ``store_dir`` (read lazily, on first request).

    >>> create_app("packet_store_merged").title
    'MondoAI Evidence Packet Browser'
    """
    web = FastAPI(title="MondoAI Evidence Packet Browser")
    state = _StoreState(store_dir)
    templates.env.globals["packet_path"] = packet_path

    @web.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Dashboard with summary statistics."""
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"stats": state.stats, "store_stats": state.store.get_stats()},
        )

    @web.get("/packets", response_class=HTMLResponse)
    async def packet_list(
        request: Request,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=10, le=200),
        claim_type: str | None = Query(None),
        review_state: str | None = Query(None),
        source: str | None = Query(None),
        min_score: str | None = Query(None),
        max_score: str | None = Query(None),
        q: str | None = Query(None),
    ) -> HTMLResponse:
        """Paginated, filterable list of packets (an empty score box means no bound)."""
        low, high = _bound(min_score), _bound(max_score)
        filtered = _filter(state.summaries, claim_type, review_state, source, low, high, q)
        total = len(filtered)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        filters = {
            "claim_type": claim_type or "",
            "review_state": review_state or "",
            "source": source or "",
            "min_score": "" if low is None else low,
            "max_score": "" if high is None else high,
            "q": q or "",
        }
        stats = state.stats
        return templates.TemplateResponse(
            request,
            "packets.html",
            {
                "packets": filtered[start : start + per_page],
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "filters": filters,
                "query_string": urlencode({k: v for k, v in filters.items() if v != ""}),
                "active": any(v != "" for v in filters.values()),
                "available_claim_types": sorted(stats.by_claim_type),
                "available_review_states": sorted(stats.by_review_state),
                "available_sources": sorted({src for s in state.summaries for src in s["sources"]}),
            },
        )

    @web.get("/packets/{key:path}", response_class=HTMLResponse)
    async def packet_detail(request: Request, key: str) -> HTMLResponse:
        """Detail view of one packet."""
        pid = packet_id_from_key(key)
        packet = state.store.get_packet(pid)
        if packet is None:
            return templates.TemplateResponse(request, "not_found.html", {"packet_id": pid}, status_code=404)
        return templates.TemplateResponse(request, "packet_detail.html", {"p": packet_view(packet)})

    @web.get("/api/stats")
    async def api_stats() -> dict[str, Any]:
        """Statistics as JSON."""
        stats = state.stats
        return {
            "total_packets": stats.total_packets,
            "total_evidence_lines": stats.total_evidence_lines,
            "total_evidence_items": stats.total_evidence_items,
            "unique_subjects": stats.unique_subjects,
            "by_claim_type": stats.by_claim_type,
            "by_predicate": stats.by_predicate,
            "by_review_state": stats.by_review_state,
            "by_source": stats.by_source,
            "by_strength": stats.by_strength,
            "score_distribution": stats.score_distribution,
        }

    return web


app = create_app()
