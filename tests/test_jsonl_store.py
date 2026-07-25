"""Tests for JSONL packet storage system."""

import tempfile
from pathlib import Path

import pytest

from mondo_ai.datamodel import (
    Coding,
    CurationStatus,
    EvidencePacket,
    SieveStatement,
)
from mondo_ai.storage.jsonl_store import (
    FileMetadata,
    JSONLPacketStore,
    Manifest,
    PacketLocation,
)


def _create_test_packet(packet_id: str, subject: str = "MONDO:0000001") -> EvidencePacket:
    """Create a test packet with given ID."""
    statement = SieveStatement(
        id=f"stmt_{packet_id}",
        type="SieveStatement",
        subject=subject,
        predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
        object="test synonym",
        statementText=f"Test statement for {packet_id}",
    )
    return EvidencePacket(
        id=packet_id,
        statement=statement,
        status=CurationStatus.UNREVIEWED,
    )


class TestFileMetadata:
    """Tests for FileMetadata dataclass."""

    def test_to_dict(self) -> None:
        """Test serialization to dict."""
        meta = FileMetadata(
            filename="packets_000.jsonl",
            status="open",
            size_bytes=1000,
            packet_count=10,
        )
        d = meta.to_dict()
        assert d["filename"] == "packets_000.jsonl"
        assert d["status"] == "open"
        assert d["size_bytes"] == 1000
        assert d["packet_count"] == 10

    def test_from_dict(self) -> None:
        """Test deserialization from dict."""
        d = {
            "filename": "packets_001.jsonl",
            "status": "sealed",
            "size_bytes": 50000000,
            "packet_count": 25000,
            "created": "2026-01-30T10:00:00",
            "sealed_at": "2026-01-30T11:00:00",
        }
        meta = FileMetadata.from_dict(d)
        assert meta.filename == "packets_001.jsonl"
        assert meta.status == "sealed"
        assert meta.sealed_at == "2026-01-30T11:00:00"


class TestPacketLocation:
    """Tests for PacketLocation dataclass."""

    def test_to_dict(self) -> None:
        """Test serialization."""
        loc = PacketLocation(file="packets_000.jsonl", offset=1234, length=500)
        d = loc.to_dict()
        assert d["file"] == "packets_000.jsonl"
        assert d["offset"] == 1234
        assert d["length"] == 500

    def test_from_dict(self) -> None:
        """Test deserialization."""
        d = {"file": "packets_001.jsonl", "offset": 5000, "length": 800}
        loc = PacketLocation.from_dict(d)
        assert loc.file == "packets_001.jsonl"
        assert loc.offset == 5000


class TestManifest:
    """Tests for Manifest dataclass."""

    def test_get_open_file(self) -> None:
        """Test finding open file."""
        manifest = Manifest(
            files=[
                FileMetadata(filename="packets_000.jsonl", status="sealed"),
                FileMetadata(filename="packets_001.jsonl", status="open"),
            ]
        )
        open_file = manifest.get_open_file()
        assert open_file is not None
        assert open_file.filename == "packets_001.jsonl"

    def test_get_open_file_none(self) -> None:
        """Test when no open file exists."""
        manifest = Manifest(
            files=[
                FileMetadata(filename="packets_000.jsonl", status="sealed"),
            ]
        )
        assert manifest.get_open_file() is None

    def test_get_sealed_files(self) -> None:
        """Test getting sealed files."""
        manifest = Manifest(
            files=[
                FileMetadata(filename="packets_000.jsonl", status="sealed"),
                FileMetadata(filename="packets_001.jsonl", status="sealed"),
                FileMetadata(filename="packets_002.jsonl", status="open"),
            ]
        )
        sealed = manifest.get_sealed_files()
        assert len(sealed) == 2
        assert all(f.status == "sealed" for f in sealed)


class TestJSONLPacketStore:
    """Tests for JSONLPacketStore."""

    def test_init_creates_directory(self) -> None:
        """Test that init creates the store directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_path = Path(tmpdir) / "new_store"
            store = JSONLPacketStore(store_path)
            assert store.store_dir.exists()

    def test_add_and_get_packet(self) -> None:
        """Test adding and retrieving a packet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packet = _create_test_packet("pkt_test001")

            added, filename = store.add_packet(packet)
            assert added is True
            assert filename == "packets_000.jsonl"

            retrieved = store.get_packet("pkt_test001")
            assert retrieved is not None
            assert retrieved.id == "pkt_test001"
            assert retrieved.statement.subject == "MONDO:0000001"

    def test_contains(self) -> None:
        """Test checking packet existence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packet = _create_test_packet("pkt_exists")

            assert store.contains("pkt_exists") is False
            store.add_packet(packet)
            assert store.contains("pkt_exists") is True

    def test_add_multiple_packets(self) -> None:
        """Test adding multiple packets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(10)]

            results = store.add_packets(packets)
            assert len(results) == 10
            assert all(added for added, _ in results.values())

            stats = store.get_stats()
            assert stats["total_packets"] == 10

    def test_iter_packets(self) -> None:
        """Test iterating over all packets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(5)]
            store.add_packets(packets)

            retrieved_ids = [p.id for p in store.iter_packets()]
            assert len(retrieved_ids) == 5
            assert "pkt_000" in retrieved_ids

    def test_file_sealing_at_size_threshold(self) -> None:
        """Test that files are sealed when they reach size threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use small max size for testing
            small_max = 2000  # 2KB
            store = JSONLPacketStore(tmpdir, max_file_size=small_max)

            # Add packets until we exceed the threshold
            for i in range(20):
                packet = _create_test_packet(f"pkt_{i:03d}")
                store.add_packet(packet)

            # Should have created multiple files
            stats = store.get_stats()
            assert stats["total_files"] > 1
            assert stats["sealed_files"] >= 1

    def test_sealed_file_immutability(self) -> None:
        """Test that packets in sealed files are not overwritten."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use small max size to force sealing
            store = JSONLPacketStore(tmpdir, max_file_size=1500)

            # Add packets to fill first file
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(10)]
            store.add_packets(packets)

            # Get the first packet's location
            first_loc = store.get_location("pkt_000")
            assert first_loc is not None

            # Try to add the same packet again
            packet = _create_test_packet("pkt_000")
            added, filename = store.add_packet(packet, skip_if_sealed=True)

            # Should be skipped if in sealed file
            if store.is_in_sealed_file("pkt_000"):
                assert added is False
                # Location should be unchanged
                new_loc = store.get_location("pkt_000")
                assert new_loc is not None
                assert new_loc.file == first_loc.file

    def test_skip_if_sealed_false_raises(self) -> None:
        """Test that adding to sealed file raises with skip_if_sealed=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir, max_file_size=1000)

            # Add packets to fill and seal a file
            for i in range(10):
                store.add_packet(_create_test_packet(f"pkt_{i:03d}"))

            # Find a packet in a sealed file
            for i in range(10):
                if store.is_in_sealed_file(f"pkt_{i:03d}"):
                    with pytest.raises(ValueError, match="sealed file"):
                        store.add_packet(
                            _create_test_packet(f"pkt_{i:03d}"),
                            skip_if_sealed=False,
                        )
                    break

    def test_get_stats(self) -> None:
        """Test statistics reporting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(5)]
            store.add_packets(packets)

            stats = store.get_stats()
            assert stats["total_packets"] == 5
            assert stats["total_files"] == 1
            assert stats["open_file"] == "packets_000.jsonl"
            assert "total_size_mb" in stats

    def test_list_files(self) -> None:
        """Test listing files with metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir, max_file_size=1500)

            # Add enough to create multiple files
            for i in range(15):
                store.add_packet(_create_test_packet(f"pkt_{i:03d}"))

            files = store.list_files()
            assert len(files) >= 1
            assert all("filename" in f for f in files)
            assert all("status" in f for f in files)

    def test_rebuild_index(self) -> None:
        """Test rebuilding index from files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(5)]
            store.add_packets(packets)

            # Delete the index
            index_path = Path(tmpdir) / "index.json"
            index_path.unlink()

            # Clear in-memory cache
            store._index = None

            # Rebuild
            count = store.rebuild_index()
            assert count == 5

            # Should be able to get packets again
            assert store.get_packet("pkt_000") is not None

    def test_persistence_across_instances(self) -> None:
        """Test that data persists across store instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # First instance - add packets
            store1 = JSONLPacketStore(tmpdir)
            packets = [_create_test_packet(f"pkt_{i:03d}") for i in range(5)]
            store1.add_packets(packets)

            # Second instance - should see same packets
            store2 = JSONLPacketStore(tmpdir)
            assert store2.contains("pkt_000")
            assert store2.get_packet("pkt_002") is not None

            stats = store2.get_stats()
            assert stats["total_packets"] == 5

    def test_deterministic_file_assignment(self) -> None:
        """Test that same packet always goes to same file (once assigned)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            packet = _create_test_packet("pkt_deterministic")
            _, filename1 = store.add_packet(packet)
            loc1 = store.get_location("pkt_deterministic")

            # Try adding again - should go to same file (or be skipped)
            _, filename2 = store.add_packet(packet)

            # If file is still open, location should be updated
            # If sealed, packet should be skipped
            loc2 = store.get_location("pkt_deterministic")
            assert loc2 is not None
            assert loc1 is not None
            # The file should be the same or packet was skipped
            assert loc2.file == loc1.file or filename2 == loc1.file


class TestJSONLPacketStoreIntegration:
    """Integration tests for realistic usage patterns."""

    def test_large_batch_with_sealing(self) -> None:
        """Test adding a large batch that causes multiple file seals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 5KB max to force many seals
            store = JSONLPacketStore(tmpdir, max_file_size=5000)

            # Add 50 packets
            packets = [_create_test_packet(f"pkt_{i:04d}") for i in range(50)]
            results = store.add_packets(packets)

            assert all(added for added, _ in results.values())

            stats = store.get_stats()
            assert stats["sealed_files"] >= 1
            assert stats["total_packets"] == 50

            # All packets should be retrievable
            for i in range(50):
                p = store.get_packet(f"pkt_{i:04d}")
                assert p is not None

    def test_incremental_adds_respect_sealed_files(self) -> None:
        """Test that incremental pipeline runs don't modify sealed files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir, max_file_size=3000)

            # First run: add 20 packets
            batch1 = [_create_test_packet(f"pkt_{i:04d}") for i in range(20)]
            store.add_packets(batch1)

            sealed_before = store.get_stats()["sealed_files"]
            files_before = {f["filename"]: f["packet_count"] for f in store.list_files() if f["status"] == "sealed"}
            assert sealed_before >= 1, "Expected at least one sealed file after the first run"

            # Second run: try to re-add same packets + new ones
            batch2 = [_create_test_packet(f"pkt_{i:04d}") for i in range(25)]
            results = store.add_packets(batch2, skip_if_sealed=True)

            # Packets in sealed files should be skipped
            skipped = sum(1 for added, _ in results.values() if not added)
            assert skipped >= 1, "Expected re-added packets in sealed files to be skipped"

            # Sealed files should not change
            files_after = {f["filename"]: f["packet_count"] for f in store.list_files() if f["status"] == "sealed"}

            for filename, count in files_before.items():
                assert files_after.get(filename) == count, f"Sealed file {filename} was modified"


class TestPacketUpdateAndDeduplication:
    """Tests for packet update and deduplication functionality."""

    def test_update_packet_in_open_file_updates_index(self) -> None:
        """Test that updating a packet in open file updates the index location."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add initial packet
            packet1 = _create_test_packet("pkt_update")
            store.add_packet(packet1)
            loc1 = store.get_location("pkt_update")

            # Update with modified packet
            packet2 = _create_test_packet("pkt_update", subject="MONDO:0000002")
            store.add_packet(packet2)
            loc2 = store.get_location("pkt_update")

            # Index should point to new location
            assert loc2 is not None
            assert loc1 is not None
            assert loc2.offset > loc1.offset  # New version is appended after original

            # Retrieved packet should have updated data
            retrieved = store.get_packet("pkt_update")
            assert retrieved is not None
            assert retrieved.statement.subject == "MONDO:0000002"

    def test_update_does_not_increment_packet_count(self) -> None:
        """Test that updating an existing packet doesn't increment packet count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add initial packet
            packet1 = _create_test_packet("pkt_count_test")
            store.add_packet(packet1)

            stats1 = store.get_stats()
            assert stats1["total_packets"] == 1

            # Update packet
            packet2 = _create_test_packet("pkt_count_test", subject="MONDO:9999999")
            store.add_packet(packet2)

            stats2 = store.get_stats()
            # Should still be 1 packet (updated, not added)
            assert stats2["total_packets"] == 1

    def test_iter_packets_deduplicates_by_default(self) -> None:
        """Test that iter_packets only returns latest version of each packet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add packet twice (update)
            packet1 = _create_test_packet("pkt_dedup", subject="MONDO:0000001")
            store.add_packet(packet1)

            packet2 = _create_test_packet("pkt_dedup", subject="MONDO:0000002")
            store.add_packet(packet2)

            # Iterate - should only get latest version
            packets = list(store.iter_packets())
            assert len(packets) == 1
            assert packets[0].statement.subject == "MONDO:0000002"

    def test_iter_packets_without_deduplication(self) -> None:
        """Test iter_packets with deduplicate=False returns all versions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add packet twice
            packet1 = _create_test_packet("pkt_all_versions", subject="MONDO:0000001")
            store.add_packet(packet1)

            packet2 = _create_test_packet("pkt_all_versions", subject="MONDO:0000002")
            store.add_packet(packet2)

            # Iterate without deduplication - should get both versions
            packets = list(store.iter_packets(deduplicate=False))
            assert len(packets) == 2
            subjects = [p.statement.subject for p in packets]
            assert "MONDO:0000001" in subjects
            assert "MONDO:0000002" in subjects

    def test_multiple_updates_only_latest_retrieved(self) -> None:
        """Test that multiple updates result in only latest version returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add 5 versions
            for i in range(5):
                packet = _create_test_packet("pkt_multi_update", subject=f"MONDO:{i:07d}")
                store.add_packet(packet)

            # Get should return latest
            retrieved = store.get_packet("pkt_multi_update")
            assert retrieved is not None
            assert retrieved.statement.subject == "MONDO:0000004"

            # Iterate should return only 1
            packets = list(store.iter_packets())
            assert len(packets) == 1

    def test_update_mixed_with_new_packets(self) -> None:
        """Test updating some packets while adding new ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add initial packets
            store.add_packet(_create_test_packet("pkt_001"))
            store.add_packet(_create_test_packet("pkt_002"))
            store.add_packet(_create_test_packet("pkt_003"))

            # Update one and add new
            store.add_packet(_create_test_packet("pkt_002", subject="MONDO:UPDATED"))
            store.add_packet(_create_test_packet("pkt_004"))

            # Should have 4 unique packets
            packets = list(store.iter_packets())
            assert len(packets) == 4

            # pkt_002 should be updated
            pkt_002 = store.get_packet("pkt_002")
            assert pkt_002 is not None
            assert pkt_002.statement.subject == "MONDO:UPDATED"


class TestMergeEvidence:
    """Tests for merge_evidence functionality."""

    def test_merge_evidence_adds_lines(self) -> None:
        """Test that merge_evidence adds new evidence lines."""
        from mondo_ai.datamodel import SieveEvidenceLine

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add packet with no evidence
            packet = _create_test_packet("pkt_merge")
            store.add_packet(packet)

            # Merge new evidence
            new_line = SieveEvidenceLine(
                id="line_001",
                type="SieveEvidenceLine",
                directionOfEvidenceProvided="supports",
            )
            updated, filename = store.merge_evidence("pkt_merge", [new_line])

            assert updated is True

            # Retrieved packet should have evidence
            retrieved = store.get_packet("pkt_merge")
            assert retrieved is not None
            assert retrieved.hasEvidenceLines is not None
            assert len(retrieved.hasEvidenceLines) == 1
            assert retrieved.hasEvidenceLines[0].id == "line_001"

    def test_merge_evidence_deduplicates_by_id(self) -> None:
        """Test that merge_evidence doesn't add duplicate evidence lines."""
        from mondo_ai.datamodel import SieveEvidenceLine

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            # Add packet
            packet = _create_test_packet("pkt_dedup_merge")
            store.add_packet(packet)

            # Add evidence
            line1 = SieveEvidenceLine(id="line_001", type="SieveEvidenceLine")
            store.merge_evidence("pkt_dedup_merge", [line1])

            # Try to add same evidence again
            line1_dup = SieveEvidenceLine(id="line_001", type="SieveEvidenceLine")
            updated, _ = store.merge_evidence("pkt_dedup_merge", [line1_dup])

            # Should not be updated (no new evidence)
            assert updated is False

            retrieved = store.get_packet("pkt_dedup_merge")
            assert retrieved is not None
            assert retrieved.hasEvidenceLines is not None
            assert len(retrieved.hasEvidenceLines) == 1

    def test_merge_evidence_nonexistent_packet_raises(self) -> None:
        """Test that merging into nonexistent packet raises error."""
        from mondo_ai.datamodel import SieveEvidenceLine

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JSONLPacketStore(tmpdir)

            line = SieveEvidenceLine(id="line_001", type="SieveEvidenceLine")

            with pytest.raises(ValueError, match="not found"):
                store.merge_evidence("pkt_nonexistent", [line])

    def test_merge_evidence_sealed_packet_skipped(self) -> None:
        """Test that merging into sealed packet is skipped."""
        from mondo_ai.datamodel import SieveEvidenceLine

        with tempfile.TemporaryDirectory() as tmpdir:
            # Small file size to force sealing
            store = JSONLPacketStore(tmpdir, max_file_size=1000)

            # Add packets to fill and seal file
            for i in range(10):
                store.add_packet(_create_test_packet(f"pkt_{i:03d}"))

            # Find a packet in sealed file
            for i in range(10):
                pkt_id = f"pkt_{i:03d}"
                if store.is_in_sealed_file(pkt_id):
                    line = SieveEvidenceLine(id="new_line", type="SieveEvidenceLine")
                    updated, _ = store.merge_evidence(pkt_id, [line], skip_if_sealed=True)
                    assert updated is False
                    break
