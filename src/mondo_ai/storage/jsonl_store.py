"""Immutable JSONL packet storage system.

This module provides an append-only, partitioned storage system for evidence packets.
Packets are stored in JSONL files that are sealed once they reach a size threshold.

Key Properties:
    - Deterministic: Same packet ID always maps to the same file (once assigned)
    - Immutable: Sealed files never change
    - Append-only: New packets go to the current open file
    - Indexed: Fast lookups via packet ID -> file location mapping

Storage Structure:
    packet_store/
    ├── manifest.json        # File metadata (status, size, packet count)
    ├── index.json           # packet_id -> file mapping
    ├── packets_000.jsonl    # Sealed file (~50MB)
    ├── packets_001.jsonl    # Sealed file (~50MB)
    └── packets_002.jsonl    # Open file (current write target)

Example:
    >>> from mondo_ai.storage.jsonl_store import JSONLPacketStore
    >>> store = JSONLPacketStore("packet_store/")  # doctest: +SKIP
    >>> store.add_packet(packet)  # doctest: +SKIP
    >>> retrieved = store.get_packet("pkt_abc123")  # doctest: +SKIP
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from mondo_ai.datamodel import EvidencePacket


# Default max file size: 50 MB (soft limit - triggers sealing)
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024

# Default hard limit: 90 MB (error if exceeded)
DEFAULT_HARD_LIMIT = 90 * 1024 * 1024


class FileSizeExceededError(Exception):
    """Raised when a file exceeds the hard size limit."""

    pass


@dataclass
class FileMetadata:
    """Metadata for a single JSONL file.

    Attributes:
        filename: Name of the JSONL file (e.g., "packets_000.jsonl").
        status: Either "open" (accepting writes) or "sealed" (immutable).
        size_bytes: Current file size in bytes.
        packet_count: Number of packets in the file.
        created: ISO timestamp when file was created.
        sealed_at: ISO timestamp when file was sealed (None if open).
    """

    filename: str
    status: str  # "open" or "sealed"
    size_bytes: int = 0
    packet_count: int = 0
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    sealed_at: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "filename": self.filename,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "packet_count": self.packet_count,
            "created": self.created,
            "sealed_at": self.sealed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileMetadata":
        """Create from dictionary."""
        return cls(
            filename=data["filename"],
            status=data["status"],
            size_bytes=data.get("size_bytes", 0),
            packet_count=data.get("packet_count", 0),
            created=data.get("created", datetime.now().isoformat()),
            sealed_at=data.get("sealed_at"),
        )


@dataclass
class PacketLocation:
    """Location of a packet within the store.

    Attributes:
        file: Filename containing the packet.
        offset: Byte offset within the file (for direct access).
        length: Length of the JSON line in bytes.
    """

    file: str
    offset: int = 0
    length: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {"file": self.file, "offset": self.offset, "length": self.length}

    @classmethod
    def from_dict(cls, data: dict) -> "PacketLocation":
        """Create from dictionary."""
        return cls(
            file=data["file"],
            offset=data.get("offset", 0),
            length=data.get("length", 0),
        )


@dataclass
class Manifest:
    """Store manifest tracking all files and their metadata.

    Attributes:
        version: Manifest format version.
        files: List of file metadata.
        total_packets: Total packets across all files.
        total_size_bytes: Total size across all files.
    """

    version: str = "1.0"
    files: list[FileMetadata] = field(default_factory=list)
    total_packets: int = 0
    total_size_bytes: int = 0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": self.version,
            "files": [f.to_dict() for f in self.files],
            "total_packets": self.total_packets,
            "total_size_bytes": self.total_size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        """Create from dictionary."""
        return cls(
            version=data.get("version", "1.0"),
            files=[FileMetadata.from_dict(f) for f in data.get("files", [])],
            total_packets=data.get("total_packets", 0),
            total_size_bytes=data.get("total_size_bytes", 0),
        )

    def get_open_file(self) -> FileMetadata | None:
        """Get the current open file, if any."""
        for f in self.files:
            if f.status == "open":
                return f
        return None

    def get_sealed_files(self) -> list[FileMetadata]:
        """Get all sealed files."""
        return [f for f in self.files if f.status == "sealed"]


class JSONLPacketStore:
    """Immutable JSONL storage for evidence packets.

    This store manages evidence packets in JSONL files with the following guarantees:
    - Once a file is sealed (reaches size threshold), it never changes
    - Packet IDs are permanently assigned to files
    - New packets are appended to the current open file
    - Fast lookups via an index mapping packet IDs to file locations

    Example:
        >>> store = JSONLPacketStore("packet_store/", max_file_size=50*1024*1024)  # doctest: +SKIP
        >>> store.add_packet(packet)  # doctest: +SKIP
        >>> packet = store.get_packet("pkt_abc123")  # doctest: +SKIP
        >>> for packet in store.iter_packets():  # doctest: +SKIP
        ...     print(packet.id)
    """

    def __init__(
        self,
        store_dir: str | Path,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        hard_limit: int = DEFAULT_HARD_LIMIT,
    ) -> None:
        """Initialize the packet store.

        Args:
            store_dir: Directory for storing JSONL files and metadata.
            max_file_size: Soft limit - file size in bytes before sealing (default: 50MB).
            hard_limit: Hard limit - error if file exceeds this size (default: 90MB).
        """
        self._store_dir = Path(store_dir)
        self._max_file_size = max_file_size
        self._hard_limit = hard_limit
        self._manifest_path = self._store_dir / "manifest.json"
        self._index_path = self._store_dir / "index.json"

        # In-memory caches
        self._manifest: Manifest | None = None
        self._index: dict[str, PacketLocation] | None = None

        # Ensure store directory exists
        self._store_dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_dir(self) -> Path:
        """Get the store directory path."""
        return self._store_dir

    @property
    def max_file_size(self) -> int:
        """Get the maximum file size before sealing."""
        return self._max_file_size

    def _load_manifest(self) -> Manifest:
        """Load or create the manifest."""
        if self._manifest is not None:
            return self._manifest

        if self._manifest_path.exists():
            with open(self._manifest_path) as f:
                data = json.load(f)
            self._manifest = Manifest.from_dict(data)
        else:
            self._manifest = Manifest()

        return self._manifest

    def _save_manifest(self) -> None:
        """Save the manifest to disk."""
        manifest = self._load_manifest()
        with open(self._manifest_path, "w") as f:
            json.dump(manifest.to_dict(), f, indent=2)

    def _load_index(self) -> dict[str, PacketLocation]:
        """Load or create the index."""
        if self._index is not None:
            return self._index

        if self._index_path.exists():
            with open(self._index_path) as f:
                data = json.load(f)
            self._index = {k: PacketLocation.from_dict(v) for k, v in data.items()}
        else:
            self._index = {}

        return self._index

    def _save_index(self) -> None:
        """Save the index to disk."""
        index = self._load_index()
        with open(self._index_path, "w") as f:
            json.dump({k: v.to_dict() for k, v in index.items()}, f)

    def _get_next_filename(self) -> str:
        """Generate the next JSONL filename."""
        manifest = self._load_manifest()
        next_num = len(manifest.files)
        return f"packets_{next_num:03d}.jsonl"

    def _get_or_create_open_file(self) -> FileMetadata:
        """Get the current open file or create a new one."""
        manifest = self._load_manifest()
        open_file = manifest.get_open_file()

        if open_file is None:
            # Create a new file
            filename = self._get_next_filename()
            open_file = FileMetadata(filename=filename, status="open")
            manifest.files.append(open_file)
            self._save_manifest()

            # Create the empty file
            file_path = self._store_dir / filename
            file_path.touch()

        return open_file

    def _seal_file(self, file_meta: FileMetadata) -> None:
        """Seal a file, marking it as immutable."""
        file_meta.status = "sealed"
        file_meta.sealed_at = datetime.now().isoformat()
        self._save_manifest()

    def _check_and_seal_if_needed(self, file_meta: FileMetadata) -> bool:
        """Check if file should be sealed and seal it if so.

        Returns:
            True if file was sealed, False otherwise.
        """
        if file_meta.size_bytes >= self._max_file_size:
            self._seal_file(file_meta)
            return True
        return False

    def contains(self, packet_id: str) -> bool:
        """Check if a packet exists in the store.

        Args:
            packet_id: The packet ID to check.

        Returns:
            True if packet exists, False otherwise.
        """
        index = self._load_index()
        return packet_id in index

    def get_location(self, packet_id: str) -> PacketLocation | None:
        """Get the location of a packet.

        Args:
            packet_id: The packet ID to look up.

        Returns:
            PacketLocation if found, None otherwise.
        """
        index = self._load_index()
        return index.get(packet_id)

    def is_in_sealed_file(self, packet_id: str) -> bool:
        """Check if a packet is in a sealed (immutable) file.

        Args:
            packet_id: The packet ID to check.

        Returns:
            True if packet is in a sealed file, False otherwise.
        """
        location = self.get_location(packet_id)
        if location is None:
            return False

        manifest = self._load_manifest()
        for file_meta in manifest.files:
            if file_meta.filename == location.file:
                return file_meta.status == "sealed"

        return False

    def add_packet(
        self,
        packet: "EvidencePacket",
        skip_if_sealed: bool = True,
    ) -> tuple[bool, str]:
        """Add a packet to the store.

        If the packet ID already exists in a sealed file, it will be skipped
        (if skip_if_sealed=True) or raise an error.

        Args:
            packet: The evidence packet to add.
            skip_if_sealed: If True, skip packets already in sealed files.

        Returns:
            Tuple of (was_added, filename). was_added is False if skipped.

        Raises:
            ValueError: If packet is in sealed file and skip_if_sealed=False.
        """
        index = self._load_index()
        manifest = self._load_manifest()

        # Check if packet already exists
        is_update = False
        if packet.id in index:
            location = index[packet.id]
            # Check if it's in a sealed file
            for file_meta in manifest.files:
                if file_meta.filename == location.file and file_meta.status == "sealed":
                    if skip_if_sealed:
                        return (False, location.file)
                    raise ValueError(
                        f"Packet {packet.id} is in sealed file {location.file}"
                    )

            # Packet exists in open file - update it
            # Append new version, index points to latest (last one wins on read)
            is_update = True

        # Get or create open file
        open_file = self._get_or_create_open_file()
        file_path = self._store_dir / open_file.filename

        # Serialize packet to JSON line
        packet_json = json.dumps(
            packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
            separators=(",", ":"),  # Compact JSON
        )
        line = packet_json + "\n"
        line_bytes = line.encode("utf-8")

        # Get current file size for offset
        current_size = file_path.stat().st_size if file_path.exists() else 0

        # Append to file
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)

        # Update index
        index[packet.id] = PacketLocation(
            file=open_file.filename,
            offset=current_size,
            length=len(line_bytes),
        )
        self._save_index()

        # Update manifest
        open_file.size_bytes = current_size + len(line_bytes)
        manifest.total_size_bytes += len(line_bytes)
        if not is_update:
            open_file.packet_count += 1
            manifest.total_packets += 1

        # Check hard limit - error if exceeded
        if open_file.size_bytes > self._hard_limit:
            raise FileSizeExceededError(
                f"File {open_file.filename} exceeds hard limit of "
                f"{self._hard_limit / (1024 * 1024):.0f}MB "
                f"(current size: {open_file.size_bytes / (1024 * 1024):.1f}MB). "
                f"This should not happen - check for unusual packet sizes."
            )

        # Check if we need to seal (soft limit)
        self._check_and_seal_if_needed(open_file)
        self._save_manifest()

        return (True, open_file.filename)

    def add_packets(
        self,
        packets: list["EvidencePacket"],
        skip_if_sealed: bool = True,
    ) -> dict[str, tuple[bool, str]]:
        """Add multiple packets to the store.

        Args:
            packets: List of evidence packets to add.
            skip_if_sealed: If True, skip packets already in sealed files.

        Returns:
            Dictionary mapping packet_id -> (was_added, filename).
        """
        results = {}
        for packet in packets:
            results[packet.id] = self.add_packet(packet, skip_if_sealed=skip_if_sealed)
        return results

    def merge_evidence(
        self,
        packet_id: str,
        new_evidence_lines: list,
        skip_if_sealed: bool = True,
    ) -> tuple[bool, str]:
        """Merge new evidence lines into an existing packet.

        This provides minimal-diff updates by only adding new evidence lines
        to an existing packet rather than requiring the full packet to be
        rewritten by the caller.

        Args:
            packet_id: ID of the packet to update.
            new_evidence_lines: List of SieveEvidenceLine objects to add.
            skip_if_sealed: If True, skip if packet is in a sealed file.

        Returns:
            Tuple of (was_updated, filename). was_updated is False if skipped.

        Raises:
            ValueError: If packet doesn't exist or is in sealed file and skip_if_sealed=False.
        """
        # Get existing packet
        packet = self.get_packet(packet_id)
        if packet is None:
            raise ValueError(f"Packet {packet_id} not found in store")

        # Check if in sealed file
        if self.is_in_sealed_file(packet_id):
            if skip_if_sealed:
                location = self.get_location(packet_id)
                return (False, location.file if location else "")
            raise ValueError(f"Packet {packet_id} is in a sealed file")

        # Merge evidence lines - avoid duplicates by ID
        existing_ids = set()
        if packet.hasEvidenceLines:
            for line in packet.hasEvidenceLines:
                if line.id:
                    existing_ids.add(line.id)
        else:
            packet.hasEvidenceLines = []

        added_count = 0
        for new_line in new_evidence_lines:
            if new_line.id and new_line.id in existing_ids:
                continue  # Skip duplicate
            packet.hasEvidenceLines.append(new_line)
            existing_ids.add(new_line.id)
            added_count += 1

        if added_count == 0:
            # No new evidence to add
            location = self.get_location(packet_id)
            return (False, location.file if location else "")

        # Save updated packet
        return self.add_packet(packet, skip_if_sealed=skip_if_sealed)

    def get_packet(self, packet_id: str) -> "EvidencePacket | None":
        """Retrieve a packet by ID.

        Args:
            packet_id: The packet ID to retrieve.

        Returns:
            The EvidencePacket if found, None otherwise.
        """
        from mondo_ai.datamodel.loaders import _convert_evidence_items
        from mondo_ai.datamodel import EvidencePacket

        location = self.get_location(packet_id)
        if location is None:
            return None

        file_path = self._store_dir / location.file
        if not file_path.exists():
            return None

        # Read at offset for efficiency
        with open(file_path, "rb") as f:
            f.seek(location.offset)
            line = f.read(location.length).decode("utf-8")

        data = json.loads(line.strip())
        # Convert evidence items to proper typed objects
        data = _convert_evidence_items(data)
        return EvidencePacket.model_validate(data)

    def iter_packets(
        self,
        file_filter: str | None = None,
        deduplicate: bool = True,
    ) -> Iterator["EvidencePacket"]:
        """Iterate over all packets in the store.

        Args:
            file_filter: Optional filename to filter by (e.g., "packets_000.jsonl").
            deduplicate: If True (default), only return the latest version of each
                packet (as tracked by the index). Set to False to get all versions.

        Yields:
            EvidencePacket objects.
        """
        from mondo_ai.datamodel.loaders import _convert_evidence_items
        from mondo_ai.datamodel import EvidencePacket

        manifest = self._load_manifest()
        index = self._load_index() if deduplicate else {}

        for file_meta in manifest.files:
            if file_filter and file_meta.filename != file_filter:
                continue

            file_path = self._store_dir / file_meta.filename
            if not file_path.exists():
                continue

            offset = 0
            with open(file_path, "rb") as f:
                for line_bytes in f:
                    line = line_bytes.decode("utf-8").strip()
                    line_len = len(line_bytes)

                    if not line:
                        offset += line_len
                        continue

                    data = json.loads(line)
                    packet_id = data.get("id")

                    # If deduplicating, only yield if this is the indexed location
                    if deduplicate and packet_id in index:
                        loc = index[packet_id]
                        if loc.file != file_meta.filename or loc.offset != offset:
                            # This is an old version, skip it
                            offset += line_len
                            continue

                    # Convert evidence items to proper typed objects
                    data = _convert_evidence_items(data)
                    yield EvidencePacket.model_validate(data)
                    offset += line_len

    def iter_file(self, filename: str) -> Iterator["EvidencePacket"]:
        """Iterate over packets in a specific file.

        Args:
            filename: The JSONL filename to read.

        Yields:
            EvidencePacket objects from that file.
        """
        yield from self.iter_packets(file_filter=filename)

    def get_stats(self) -> dict:
        """Get store statistics.

        Returns:
            Dictionary with store statistics.
        """
        manifest = self._load_manifest()
        index = self._load_index()

        sealed_files = manifest.get_sealed_files()
        open_file = manifest.get_open_file()

        return {
            "store_dir": str(self._store_dir),
            "max_file_size_mb": self._max_file_size / (1024 * 1024),
            "total_files": len(manifest.files),
            "sealed_files": len(sealed_files),
            "open_file": open_file.filename if open_file else None,
            "open_file_size_mb": (
                open_file.size_bytes / (1024 * 1024) if open_file else 0
            ),
            "total_packets": manifest.total_packets,
            "indexed_packets": len(index),
            "total_size_mb": manifest.total_size_bytes / (1024 * 1024),
        }

    def list_files(self) -> list[dict]:
        """List all files with their metadata.

        Returns:
            List of file metadata dictionaries.
        """
        manifest = self._load_manifest()
        return [f.to_dict() for f in manifest.files]

    def rebuild_index(self) -> int:
        """Rebuild the index by scanning all files.

        This is useful for recovery or after manual file modifications.

        Returns:
            Number of packets indexed.
        """
        from mondo_ai.datamodel.loaders import _convert_evidence_items
        from mondo_ai.datamodel import EvidencePacket

        manifest = self._load_manifest()
        new_index: dict[str, PacketLocation] = {}

        for file_meta in manifest.files:
            file_path = self._store_dir / file_meta.filename
            if not file_path.exists():
                continue

            offset = 0
            with open(file_path, "rb") as f:
                for line in f:
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        offset += len(line)
                        continue

                    data = json.loads(line_str)
                    # Convert evidence items to proper typed objects
                    data = _convert_evidence_items(data)
                    packet = EvidencePacket.model_validate(data)

                    new_index[packet.id] = PacketLocation(
                        file=file_meta.filename,
                        offset=offset,
                        length=len(line),
                    )
                    offset += len(line)

        self._index = new_index
        self._save_index()

        return len(new_index)
