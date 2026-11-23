"""The static exporter dumps the packet store to a JSON payload for the docs browser.

The mkdocs "Evidence Browser" page (docs/evidence/index.md) loads this JSON client-side,
so the browser ships as static files on GitHub Pages. With ~200k packets the payload is a
column header plus one array per packet rather than a list of keyed objects.
"""

import json
from pathlib import Path

from mondo_ai.storage.jsonl_store import JSONLPacketStore
from mondo_ai.web.static_export import FIELDS, export_web_data
from tests.test_packet_builder import _find, built  # noqa: F401  (fixture)

PAGE = Path(__file__).parent.parent / "docs" / "evidence" / "index.md"


def _export(built, tmp_path: Path) -> dict:  # noqa: F811
    store_dir = tmp_path / "store"
    JSONLPacketStore(store_dir).add_packets(built["merged_list"])
    out = tmp_path / "docs" / "evidence" / "data.json"
    n = export_web_data(store_dir, out)
    assert n == len(built["merged_list"])
    return json.loads(out.read_text())


def test_export_stats_block(built, tmp_path: Path):  # noqa: F811
    stats = _export(built, tmp_path)["stats"]
    assert stats["total_packets"] == len(built["merged_list"])
    assert stats["by_claim_type"]["SynonymStatement"] > 0
    assert set(stats["by_review_state"]) == {"PENDING_REVIEW", "NOT_VERIFIED"}
    assert stats["by_source"]["infores:omim"] > 0
    assert sum(stats["score_distribution"].values()) == stats["total_packets"]
    assert stats["unique_subjects"] == 4


def test_export_packet_rows(built, tmp_path: Path):  # noqa: F811
    payload = _export(built, tmp_path)
    assert payload["fields"] == FIELDS
    rows = [dict(zip(payload["fields"], row)) for row in payload["packets"]]
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    st = packet["statement"]
    (row,) = [r for r in rows if r["id"] == packet["id"]]
    assert row == {
        "id": packet["id"],
        "claim_type": "SynonymStatement",
        "subject": "MONDO:1",
        "predicate": "has exact synonym",
        "object": "CF",
        "statement_text": st["statement_text"],
        "review_state": "PENDING_REVIEW",
        "score": st["score"],
        "strength": st["strength"],
        "line_count": 3,
        "sources": ["infores:disease-ontology", "infores:omim", "infores:orphanet"],
    }


def test_export_is_compact(built, tmp_path: Path):  # noqa: F811
    store_dir = tmp_path / "store"
    JSONLPacketStore(store_dir).add_packets(built["merged_list"])
    out = tmp_path / "data.json"
    export_web_data(store_dir, out)
    text = out.read_text()
    assert "\n" not in text and ", " not in text.split('"packets"')[0]
    # no per-packet keys and no evidence detail: well under 1 kB per packet here
    assert len(text) / len(built["merged_list"]) < 1000


def test_docs_page_reads_the_exported_shape():
    """The browser page indexes rows by the exported field names."""
    page = PAGE.read_text()
    assert "data.fields" in page
    for name in FIELDS:
        assert f"'{name}'" in page, name
    for stat in ("by_claim_type", "by_review_state", "by_source", "score_distribution"):
        assert stat in page
