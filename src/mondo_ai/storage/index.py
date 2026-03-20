"""SQLite-based packet index for fast querying.

This module provides indexing and querying capabilities for evidence packets
stored as YAML files. The index stores metadata for fast filtering without
loading full packet files.

Example:
    >>> from mondo_ai.storage.index import PacketIndex
    >>> index = PacketIndex("packet_index.db")
    >>> index.build_from_directory("evidence_packets")
    >>> results = index.query(min_score=0.8, status="UNREVIEWED")
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

from mondo_ai.datamodel import EvidencePacket, load_packet


@dataclass
class PacketSummary:
    """Summary of a packet for index results."""

    packet_id: str
    file_path: str
    subject_id: str
    subject_label: str | None
    predicate: str
    object_value: str
    status: str
    score: float | None
    evidence_count: int
    source_ids: str  # Comma-separated
    created: str | None
    updated: str | None


class PacketIndex:
    """SQLite-based index for evidence packets."""

    def __init__(self, db_path: str | Path = "packet_index.db") -> None:
        """Initialize the packet index.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        """Create database schema if it doesn't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS packets (
                packet_id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_label TEXT,
                predicate TEXT NOT NULL,
                object_value TEXT NOT NULL,
                status TEXT NOT NULL,
                score REAL,
                evidence_count INTEGER DEFAULT 0,
                source_ids TEXT,
                created TEXT,
                updated TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_subject_id ON packets(subject_id);
            CREATE INDEX IF NOT EXISTS idx_status ON packets(status);
            CREATE INDEX IF NOT EXISTS idx_score ON packets(score);
            CREATE INDEX IF NOT EXISTS idx_predicate ON packets(predicate);
        """)
        conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def _extract_metadata(self, packet: EvidencePacket, file_path: Path) -> dict:
        """Extract indexable metadata from a packet.

        Args:
            packet: The evidence packet.
            file_path: Path to the packet file.

        Returns:
            Dict of metadata for indexing.
        """
        # Get score from synthesis if available
        score = None
        if packet.evidence_synthesis and packet.evidence_synthesis.score:
            score = packet.evidence_synthesis.score.value

        # Get predicate label
        predicate = ""
        if packet.statement.predicate:
            predicate = packet.statement.predicate.label or packet.statement.predicate.code or ""

        # Count evidence lines and extract source IDs
        evidence_count = 0
        source_ids = set()
        if packet.hasEvidenceLines:
            evidence_count = len(packet.hasEvidenceLines)
            for line in packet.hasEvidenceLines:
                if line.hasEvidenceItems:
                    for item in line.hasEvidenceItems:
                        # Try to get sourceId from ConcordanceItem
                        if hasattr(item, "sourceId") and item.sourceId:
                            source_ids.add(item.sourceId)

        return {
            "packet_id": packet.id,
            "file_path": str(file_path),
            "subject_id": packet.statement.subject or "",
            "subject_label": packet.statement.subjectLabel,
            "predicate": predicate,
            "object_value": packet.statement.object or "",
            "status": packet.status.value if hasattr(packet.status, "value") else (packet.status or "UNREVIEWED"),
            "score": score,
            "evidence_count": evidence_count,
            "source_ids": ",".join(sorted(source_ids)),
            "created": str(packet.created) if packet.created else None,
            "updated": str(packet.updated) if packet.updated else None,
        }

    def index_packet(self, packet: EvidencePacket, file_path: Path) -> None:
        """Index a single packet.

        Args:
            packet: The evidence packet.
            file_path: Path to the packet file.
        """
        conn = self._get_conn()
        meta = self._extract_metadata(packet, file_path)

        conn.execute("""
            INSERT OR REPLACE INTO packets
            (packet_id, file_path, subject_id, subject_label, predicate,
             object_value, status, score, evidence_count, source_ids, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            meta["packet_id"],
            meta["file_path"],
            meta["subject_id"],
            meta["subject_label"],
            meta["predicate"],
            meta["object_value"],
            meta["status"],
            meta["score"],
            meta["evidence_count"],
            meta["source_ids"],
            meta["created"],
            meta["updated"],
        ))
        conn.commit()

    def build_from_directory(
        self,
        directory: Path | str,
        pattern: str = "pkt_*.yaml",
        clear: bool = True,
    ) -> int:
        """Build index from a directory of packet files.

        Args:
            directory: Directory containing packet YAML files.
            pattern: Glob pattern for packet files.
            clear: If True, clear existing index first.

        Returns:
            Number of packets indexed.
        """
        directory = Path(directory)
        conn = self._get_conn()

        if clear:
            conn.execute("DELETE FROM packets")
            conn.commit()

        count = 0
        for file_path in sorted(directory.glob(pattern)):
            try:
                packet = load_packet(file_path)
                self.index_packet(packet, file_path)
                count += 1
            except Exception as e:
                print(f"Warning: Failed to index {file_path}: {e}")

        return count

    def query(
        self,
        subject_id: str | None = None,
        predicate: str | None = None,
        status: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PacketSummary]:
        """Query the index with filters.

        Args:
            subject_id: Filter by Mondo ID (exact match).
            predicate: Filter by predicate (contains match).
            status: Filter by status (UNREVIEWED, APPROVED, REJECTED).
            min_score: Minimum NER score.
            max_score: Maximum NER score.
            source_id: Filter by source (doid, ordo, omim).
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            List of PacketSummary objects.
        """
        conn = self._get_conn()

        conditions = []
        params = []

        if subject_id:
            conditions.append("subject_id = ?")
            params.append(subject_id)

        if predicate:
            conditions.append("predicate LIKE ?")
            params.append(f"%{predicate}%")

        if status:
            conditions.append("status = ?")
            params.append(status)

        if min_score is not None:
            conditions.append("score >= ?")
            params.append(min_score)

        if max_score is not None:
            conditions.append("score <= ?")
            params.append(max_score)

        if source_id:
            conditions.append("source_ids LIKE ?")
            params.append(f"%{source_id}%")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT * FROM packets
            {where_clause}
            ORDER BY score DESC NULLS LAST, packet_id
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = conn.execute(query, params)
        results = []
        for row in cursor:
            results.append(PacketSummary(
                packet_id=row["packet_id"],
                file_path=row["file_path"],
                subject_id=row["subject_id"],
                subject_label=row["subject_label"],
                predicate=row["predicate"],
                object_value=row["object_value"],
                status=row["status"],
                score=row["score"],
                evidence_count=row["evidence_count"],
                source_ids=row["source_ids"],
                created=row["created"],
                updated=row["updated"],
            ))

        return results

    def get_stats(self) -> dict:
        """Get summary statistics from the index.

        Returns:
            Dict with counts and statistics.
        """
        conn = self._get_conn()

        stats = {}

        # Total count
        stats["total_packets"] = conn.execute(
            "SELECT COUNT(*) FROM packets"
        ).fetchone()[0]

        # By status
        cursor = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM packets GROUP BY status"
        )
        stats["by_status"] = {row[0]: row[1] for row in cursor}

        # By predicate
        cursor = conn.execute(
            "SELECT predicate, COUNT(*) as cnt FROM packets GROUP BY predicate ORDER BY cnt DESC LIMIT 10"
        )
        stats["by_predicate"] = {row[0]: row[1] for row in cursor}

        # Score distribution
        cursor = conn.execute("""
            SELECT
                CASE
                    WHEN score >= 0.7 THEN 'accept'
                    WHEN score <= -0.3 THEN 'reject'
                    WHEN score IS NULL THEN 'unscored'
                    ELSE 'review'
                END as bucket,
                COUNT(*) as cnt
            FROM packets
            GROUP BY bucket
        """)
        stats["score_buckets"] = {row[0]: row[1] for row in cursor}

        # Evidence source counts
        cursor = conn.execute("SELECT source_ids FROM packets WHERE source_ids != ''")
        source_counts: dict[str, int] = {}
        for row in cursor:
            for src in row[0].split(","):
                if src:
                    source_counts[src] = source_counts.get(src, 0) + 1
        stats["by_source"] = source_counts

        # Unique subjects
        stats["unique_subjects"] = conn.execute(
            "SELECT COUNT(DISTINCT subject_id) FROM packets"
        ).fetchone()[0]

        return stats

    def iter_all(self) -> Iterator[PacketSummary]:
        """Iterate over all indexed packets.

        Yields:
            PacketSummary for each packet.
        """
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM packets ORDER BY packet_id")

        for row in cursor:
            yield PacketSummary(
                packet_id=row["packet_id"],
                file_path=row["file_path"],
                subject_id=row["subject_id"],
                subject_label=row["subject_label"],
                predicate=row["predicate"],
                object_value=row["object_value"],
                status=row["status"],
                score=row["score"],
                evidence_count=row["evidence_count"],
                source_ids=row["source_ids"],
                created=row["created"],
                updated=row["updated"],
            )
