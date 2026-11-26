"""The SQLite packet index summarises a JSONL store of profile packets for fast queries."""

import time
from copy import deepcopy
from pathlib import Path
from typing import Iterator

import pytest

from mondo_ai.storage.index import PacketIndex, PacketSummary, summarize_packet
from mondo_ai.storage.jsonl_store import JSONLPacketStore
from tests.test_packet_builder import _find, built  # noqa: F401  (fixture)


@pytest.fixture
def store(built, tmp_path: Path) -> JSONLPacketStore:  # noqa: F811
    s = JSONLPacketStore(tmp_path / "store")
    s.add_packets(built["merged_list"])
    return s


@pytest.fixture
def index(store: JSONLPacketStore, tmp_path: Path) -> Iterator[PacketIndex]:
    idx = PacketIndex(tmp_path / "index.db")
    idx.build_from_store(store)
    yield idx
    idx.close()


def test_summarize_packet_reads_the_profile_shape(built):  # noqa: F811
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    st = packet["statement"]
    row = summarize_packet(packet)
    assert row["packet_id"] == packet["id"]
    assert row["claim_type"] == "SynonymStatement"
    assert (row["subject"], row["predicate"], row["object"]) == ("MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    assert row["predicate_label"] == "has exact synonym"
    assert row["statement_text"] == st["statement_text"]
    assert row["review_state"] == "PENDING_REVIEW"
    assert row["score"] == st["score"]
    assert row["strength"] == st["strength"]
    assert row["direction"] == "supports"
    assert row["line_count"] == 3
    assert row["sources"] == ["infores:disease-ontology", "infores:omim", "infores:orphanet"]
    assert row["version"] == packet["version"]


def test_summarize_packet_takes_sources_from_records_too(built):  # noqa: F811
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "OMIM:219700")
    assert "infores:omim" in summarize_packet(packet)["sources"]


def test_build_from_store_indexes_every_packet(index: PacketIndex, built):  # noqa: F811
    assert index.get_stats()["total_packets"] == len(built["merged_list"])
    assert len(list(index.iter_all())) == len(built["merged_list"])


def test_query_by_claim_type_and_subject(index: PacketIndex):
    rows = index.query(claim_type="SubclassStatement", subject="MONDO:1")
    assert rows and all(isinstance(r, PacketSummary) for r in rows)
    assert {(r.claim_type, r.subject) for r in rows} == {("SubclassStatement", "MONDO:1")}
    assert rows[0].object == "MONDO:2"


def test_query_by_review_state_score_and_source(index: PacketIndex):
    pending = index.query(review_state="PENDING_REVIEW", limit=1000)
    assert pending and {r.review_state for r in pending} == {"PENDING_REVIEW"}

    negative = index.query(max_score=0, limit=1000)
    assert {(r.subject, r.object) for r in negative} == {("MONDO:4", "DOID:9"), ("MONDO:1", "ICD10CM:E84")}
    assert all(r.score is not None and r.score >= 0.5 for r in index.query(min_score=0.5, limit=1000))

    orphanet = index.query(source="infores:orphanet", limit=1000)
    assert orphanet and all("infores:orphanet" in (r.sources or "").split(",") for r in orphanet)


def test_query_by_predicate_object_text_strength_and_lines(index: PacketIndex):
    (row,) = index.query(predicate="skos:exactMatch", object="ICD10CM:E84.0")
    assert row.claim_type == "MappingStatement"
    assert all("CF" in (r.statement_text or "") for r in index.query(text="CF"))
    strong = index.query(strength="strong", limit=1000)
    assert {r.strength for r in strong} <= {"strong"}
    assert all(r.line_count >= 3 for r in index.query(min_lines=3, limit=1000))
    assert {r.object for r in index.query(direction="disputes")} == {"DOID:9", "ICD10CM:E84"}


def test_results_are_ordered_by_score_descending(index: PacketIndex):
    scores = [r.score for r in index.query(limit=1000) if r.score is not None]
    assert scores == sorted(scores, reverse=True)


def test_get_stats_counts_profile_fields(index: PacketIndex, built):  # noqa: F811
    stats = index.get_stats()
    assert stats["by_claim_type"]["MappingStatement"] > 0
    assert set(stats["by_review_state"]) == {"PENDING_REVIEW", "NOT_VERIFIED"}
    assert stats["by_source"]["infores:omim"] > 0
    assert sum(stats["by_strength"].values()) == stats["total_packets"]
    assert stats["unique_subjects"] == len({p["statement"]["subject"] for p in built["merged_list"]})


def test_rebuild_clears_and_append_keeps(store: JSONLPacketStore, tmp_path: Path):
    idx = PacketIndex(tmp_path / "i.db")
    n = idx.build_from_store(store)
    assert idx.build_from_store(store) == n
    assert idx.get_stats()["total_packets"] == n
    idx.build_from_store(store, clear=False)
    assert idx.get_stats()["total_packets"] == n  # same ids replace, never duplicate
    idx.close()


def test_bulk_indexing_is_fast(built, tmp_path: Path):  # noqa: F811
    """20k packets index well under a few seconds (the merged store holds ~200k)."""
    template = built["merged_list"][0]
    packets = []
    for i in range(20_000):
        p = deepcopy(template)
        p["id"] = f"mondoai:pkt/{i:012x}"
        packets.append(p)
    idx = PacketIndex(tmp_path / "big.db")
    start = time.perf_counter()
    assert idx.index_packets(packets) == 20_000
    assert time.perf_counter() - start < 5
    assert idx.get_stats()["total_packets"] == 20_000
    idx.close()
