"""SQLite index over a JSONL packet store, for fast querying of packet summaries.

Each SIEVE profile packet (a dict, one claim per packet) becomes one row holding the
statement's claim type, subject, predicate, object, text, review state, inclusion score
S with its strength band and direction, the number of evidence lines, the resources the
evidence comes from, and the packet version. Rows are written in bulk, so a store of
~200k packets indexes in seconds.

Example:
    >>> from mondo_ai.storage.index import PacketIndex
    >>> from mondo_ai.storage.jsonl_store import JSONLPacketStore
    >>> index = PacketIndex("packet_index.db")  # doctest: +SKIP
    >>> index.build_from_store(JSONLPacketStore("packet_store_merged"))  # doctest: +SKIP
    >>> rows = index.query(min_score=0.8, review_state="PENDING_REVIEW")  # doctest: +SKIP
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

from mondo_ai.stats import score_bucket
from mondo_ai.storage.jsonl_store import JSONLPacketStore

COLUMNS = (
    "packet_id",
    "claim_type",
    "subject",
    "predicate",
    "predicate_label",
    "object",
    "statement_text",
    "review_state",
    "score",
    "strength",
    "direction",
    "line_count",
    "sources",
    "version",
)

BATCH_SIZE = 10_000


@dataclass
class PacketSummary:
    """One indexed packet.

    ``predicate`` is the predicate CURIE and ``predicate_label`` its label; ``sources``
    is the comma-separated list of resources (infores ids) the evidence comes from.
    """

    packet_id: str
    claim_type: str
    subject: str
    predicate: str
    predicate_label: str | None
    object: str
    statement_text: str | None
    review_state: str | None
    score: float | None
    strength: str | None
    direction: str | None
    line_count: int
    sources: str
    version: str | None


def packet_sources(packet: dict[str, Any]) -> list[str]:
    """The resources the packet's evidence comes from, sorted.

    Takes the ``resource`` of every item held by an evidence line and of the statement's
    ``has_evidence``. Items of a higher-order line are copies of other lines, so their
    own items are already counted.

    >>> packet_sources({"statement": {"has_evidence_lines": [
    ...     {"has_evidence_items": [{"resource": "infores:omim"}, {"type": "ComputationalScore"}]},
    ...     {"has_evidence_items": [{"type": "SieveEvidenceLine", "has_evidence_items": []}]}],
    ...     "has_evidence": [{"resource": "infores:pubmed"}]}})
    ['infores:omim', 'infores:pubmed']
    """
    st = packet["statement"]
    found: set[str] = set()
    for line in st.get("has_evidence_lines") or []:
        for item in line.get("has_evidence_items") or []:
            if item.get("resource"):
                found.add(item["resource"])
    for item in st.get("has_evidence") or []:
        if item.get("resource"):
            found.add(item["resource"])
    return sorted(found)


def summarize_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """The indexed fields of one packet, keyed by column name (``sources`` as a list).

    >>> row = summarize_packet({"id": "mondoai:pkt/1", "version": "v", "statement": {
    ...     "type": "SynonymStatement", "subject": "MONDO:1", "object": "CF",
    ...     "predicate": {"code": "oboInOwl:hasExactSynonym", "label": "has exact synonym"},
    ...     "score": 0.5, "strength": "moderate", "has_evidence_lines": [{}]}})
    >>> row["claim_type"], row["predicate_label"], row["line_count"], row["sources"]
    ('SynonymStatement', 'has exact synonym', 1, [])
    """
    st = packet["statement"]
    predicate = st.get("predicate") or {}
    return {
        "packet_id": packet["id"],
        "claim_type": st["type"],
        "subject": st["subject"],
        "predicate": predicate.get("code") or "",
        "predicate_label": predicate.get("label"),
        "object": st["object"],
        "statement_text": st.get("statement_text"),
        "review_state": st.get("review_state"),
        "score": st.get("score"),
        "strength": st.get("strength"),
        "direction": st.get("direction"),
        "line_count": len(st.get("has_evidence_lines") or []),
        "sources": packet_sources(packet),
        "version": packet.get("version"),
    }


def _row(packet: dict[str, Any]) -> tuple[Any, ...]:
    """A packet as a tuple in COLUMNS order, sources joined by commas."""
    summary = summarize_packet(packet)
    summary["sources"] = ",".join(summary["sources"])
    return tuple(summary[c] for c in COLUMNS)


class PacketIndex:
    """SQLite index of packet summaries.

    >>> idx = PacketIndex(":memory:")
    >>> idx.index_packets([{"id": "mondoai:pkt/1", "statement": {"type": "SynonymStatement",
    ...     "subject": "MONDO:1", "object": "CF", "score": 0.4}}])
    1
    >>> [r.packet_id for r in idx.query(subject="MONDO:1")]
    ['mondoai:pkt/1']
    """

    def __init__(self, db_path: str | Path = "packet_index.db") -> None:
        """Open (or create) the index database.

        Args:
            db_path: Path to the SQLite database file (``":memory:"`` for a scratch index).
        """
        self.db_path = db_path if db_path == ":memory:" else Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        """Create the table and indexes if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS packets (
                packet_id TEXT PRIMARY KEY,
                claim_type TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                predicate_label TEXT,
                object TEXT NOT NULL,
                statement_text TEXT,
                review_state TEXT,
                score REAL,
                strength TEXT,
                direction TEXT,
                line_count INTEGER NOT NULL DEFAULT 0,
                sources TEXT NOT NULL DEFAULT '',
                version TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_claim_type ON packets(claim_type);
            CREATE INDEX IF NOT EXISTS idx_subject ON packets(subject);
            CREATE INDEX IF NOT EXISTS idx_object ON packets(object);
            CREATE INDEX IF NOT EXISTS idx_review_state ON packets(review_state);
            CREATE INDEX IF NOT EXISTS idx_score ON packets(score);
        """)
        conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def clear(self) -> None:
        """Remove every row."""
        conn = self._get_conn()
        conn.execute("DELETE FROM packets")
        conn.commit()

    def index_packets(self, packets: Iterable[dict[str, Any]]) -> int:
        """Insert or replace packets in bulk.

        Args:
            packets: Packet dicts (a list or a store iterator).

        Returns:
            Number of packets written.
        """
        conn = self._get_conn()
        sql = f"INSERT OR REPLACE INTO packets ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
        rows = (_row(p) for p in packets)
        count = 0
        while batch := list(islice(rows, BATCH_SIZE)):
            conn.executemany(sql, batch)
            count += len(batch)
        conn.commit()
        return count

    def build_from_store(self, store: JSONLPacketStore, clear: bool = True) -> int:
        """Index every packet of a JSONL store.

        Args:
            store: The store to index (latest version of each packet).
            clear: If True, empty the index first.

        Returns:
            Number of packets indexed.
        """
        if clear:
            self.clear()
        return self.index_packets(store.iter_packets())

    def query(
        self,
        claim_type: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        review_state: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        strength: str | None = None,
        direction: str | None = None,
        source: str | None = None,
        min_lines: int | None = None,
        text: str | None = None,
        version: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PacketSummary]:
        """Query the index; results are ordered by score, highest first.

        Args:
            claim_type: Statement class (SynonymStatement, SubclassStatement, MappingStatement).
            subject: Mondo id (exact).
            predicate: Predicate CURIE or label (exact).
            object: Statement object (exact).
            review_state: e.g. PENDING_REVIEW or NOT_VERIFIED.
            min_score: Minimum inclusion score S.
            max_score: Maximum inclusion score S.
            strength: Strength band (strong, moderate, weak).
            direction: supports or disputes.
            source: A resource (infores id) among the packet's sources.
            min_lines: Minimum number of evidence lines.
            text: Substring of the statement text (case-insensitive).
            version: Packet version.
            limit: Maximum results.
            offset: Results to skip.

        Returns:
            Matching PacketSummary rows.
        """
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("claim_type", claim_type),
            ("subject", subject),
            ("object", object),
            ("review_state", review_state),
            ("strength", strength),
            ("direction", direction),
            ("version", version),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if predicate is not None:
            conditions.append("(predicate = ? OR predicate_label = ?)")
            params.extend([predicate, predicate])
        if min_score is not None:
            conditions.append("score >= ?")
            params.append(min_score)
        if max_score is not None:
            conditions.append("score <= ?")
            params.append(max_score)
        if source is not None:
            conditions.append("(',' || sources || ',') LIKE ?")
            params.append(f"%,{source},%")
        if min_lines is not None:
            conditions.append("line_count >= ?")
            params.append(min_lines)
        if text is not None:
            conditions.append("statement_text LIKE ?")
            params.append(f"%{text}%")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM packets {where} ORDER BY score DESC NULLS LAST, packet_id LIMIT ? OFFSET ?"
        cursor = self._get_conn().execute(sql, [*params, limit, offset])
        return [PacketSummary(**dict(row)) for row in cursor]

    def _counts(self, column: str) -> dict[str, int]:
        """Row counts by the values of one column."""
        cursor = self._get_conn().execute(
            f"SELECT {column}, COUNT(*) FROM packets GROUP BY {column} ORDER BY COUNT(*) DESC"
        )
        return {str(row[0]): row[1] for row in cursor}

    def get_stats(self) -> dict[str, Any]:
        """Summary counts: totals and packets by claim type, predicate, review state,
        strength, score bucket and source.
        """
        conn = self._get_conn()
        stats: dict[str, Any] = {
            "total_packets": conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0],
            "unique_subjects": conn.execute("SELECT COUNT(DISTINCT subject) FROM packets").fetchone()[0],
            "by_claim_type": self._counts("claim_type"),
            "by_predicate": self._counts("COALESCE(predicate_label, predicate)"),
            "by_review_state": self._counts("review_state"),
            "by_strength": self._counts("strength"),
        }
        conn.create_function("score_bucket", 1, lambda s: "unscored" if s is None else score_bucket(s))
        cursor = conn.execute("SELECT score_bucket(score) AS bucket, COUNT(*) FROM packets GROUP BY bucket")
        stats["score_buckets"] = {row[0]: row[1] for row in cursor}
        by_source: dict[str, int] = {}
        for (sources,) in conn.execute("SELECT sources FROM packets WHERE sources != ''"):
            for src in sources.split(","):
                by_source[src] = by_source.get(src, 0) + 1
        stats["by_source"] = by_source
        return stats

    def iter_all(self) -> Iterator[PacketSummary]:
        """Every indexed packet, ordered by id."""
        for row in self._get_conn().execute("SELECT * FROM packets ORDER BY packet_id"):
            yield PacketSummary(**dict(row))
