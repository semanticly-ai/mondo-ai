"""Immutable, gzip-compressed JSONL packet storage.

This module provides an append-only, partitioned storage system for evidence packets.
Packets (SIEVE profile EvidencePackets, as JSON dicts) are stored as JSON lines in
gzip files that are sealed once they reach a size threshold.

Each file is a series of independent gzip members, each holding a block of up to
``BLOCK_SIZE`` packets (the BGZF idea). The index records, per packet, the member's byte
offset and length and the packet's line within it, so a lookup decompresses one block,
and compression still sees a whole block (profile packets repeat their reference
snapshot and record copies, so a block compresses about tenfold). Any standard gzip
reader reads a file as one JSONL stream: ``gzip -dc packets_000.jsonl.gz``.

Key Properties:
    - Deterministic: Same packet ID always maps to the same file (once assigned)
    - Immutable: Sealed files never change
    - Append-only: New packets go to the current open file
    - Indexed: Fast lookups via packet ID -> (file, block, line)

Storage Structure:
    packet_store/
    ├── manifest.json          # File metadata (status, compressed size, packet count)
    ├── index.json             # packet_id -> [file, block offset, block length, line]
    ├── packets_000.jsonl.gz   # Sealed file (~50MB compressed)
    └── packets_001.jsonl.gz   # Open file (current write target)

Example:
    >>> from mondo_ai.storage.jsonl_store import JSONLPacketStore
    >>> store = JSONLPacketStore("packet_store/")  # doctest: +SKIP
    >>> store.add_packets(packets)  # doctest: +SKIP
    >>> retrieved = store.get_packet("mondoai:pkt/9ec61863ea85")  # doctest: +SKIP
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

# A packet is a SIEVE profile EvidencePacket as a JSON dict.
Packet = dict[str, Any]

# Default max file size: 50 MB compressed (soft limit - triggers sealing)
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024

# Default hard limit: 90 MB (error if exceeded)
DEFAULT_HARD_LIMIT = 90 * 1024 * 1024

# Packets per gzip member when writing in bulk
BLOCK_SIZE = 1000

FILE_SUFFIX = ".jsonl.gz"


class FileSizeExceededError(Exception):
    """Raised when a file exceeds the hard size limit."""

    pass


@dataclass
class FileMetadata:
    """Metadata for a single store file.

    Attributes:
        filename: Name of the file (e.g., "packets_000.jsonl.gz").
        status: Either "open" (accepting writes) or "sealed" (immutable).
        size_bytes: Current (compressed) file size in bytes.
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
        offset: Byte offset of the gzip member (block) holding the packet.
        length: Compressed length of that member in bytes.
        line: Line of the packet within the decompressed block.
    """

    file: str
    offset: int = 0
    length: int = 0
    line: int = 0

    def to_dict(self) -> list:
        """Convert to a compact list for JSON serialization.

        >>> PacketLocation("packets_000.jsonl.gz", 10, 20, 3).to_dict()
        ['packets_000.jsonl.gz', 10, 20, 3]
        """
        return [self.file, self.offset, self.length, self.line]

    @classmethod
    def from_dict(cls, data: list | dict) -> "PacketLocation":
        """Create from the list form written by ``to_dict`` (or a dict with the same keys)."""
        if isinstance(data, dict):
            return cls(data["file"], data.get("offset", 0), data.get("length", 0), data.get("line", 0))
        return cls(*data)


@dataclass
class Manifest:
    """Store manifest tracking all files and their metadata.

    Attributes:
        version: Manifest format version.
        files: List of file metadata.
        total_packets: Total packets across all files.
        total_size_bytes: Total (compressed) size across all files.
    """

    version: str = "2.0"
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
            version=data.get("version", "2.0"),
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


def iter_members(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Split concatenated gzip members: yield (offset, compressed length, decompressed bytes).

    >>> blob = gzip.compress(b"a\\n") + gzip.compress(b"b\\nc\\n")
    >>> [(o, text) for o, _, text in iter_members(blob)]
    [(0, b'a\\n'), (22, b'b\\nc\\n')]
    """
    pos = 0
    while pos < len(data):
        d = zlib.decompressobj(wbits=31)
        text = d.decompress(data[pos:])
        length = len(data) - pos - len(d.unused_data)
        yield pos, length, text
        pos += length


def _encode(packet: Packet) -> bytes:
    return (json.dumps(packet, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


class JSONLPacketStore:
    """Immutable gzip JSONL storage for evidence packets.

    This store manages evidence packets with the following guarantees:
    - Once a file is sealed (reaches size threshold), it never changes
    - Packet IDs are permanently assigned to files
    - New packets are appended to the current open file
    - Fast lookups via an index mapping packet IDs to file locations

    Example:
        >>> store = JSONLPacketStore("packet_store/", max_file_size=50*1024*1024)  # doctest: +SKIP
        >>> store.add_packet(packet)  # doctest: +SKIP
        >>> packet = store.get_packet("mondoai:pkt/9ec61863ea85")  # doctest: +SKIP
        >>> for packet in store.iter_packets():  # doctest: +SKIP
        ...     print(packet["id"])
    """

    def __init__(
        self,
        store_dir: str | Path,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        hard_limit: int = DEFAULT_HARD_LIMIT,
        block_size: int = BLOCK_SIZE,
    ) -> None:
        """Initialize the packet store.

        Args:
            store_dir: Directory for storing packet files and metadata.
            max_file_size: Soft limit - compressed file size in bytes before sealing (default: 50MB).
            hard_limit: Hard limit - error if file exceeds this size (default: 90MB).
            block_size: Packets per gzip member when writing in bulk.
        """
        self._store_dir = Path(store_dir)
        self._max_file_size = max_file_size
        self._hard_limit = hard_limit
        self._block_size = block_size
        self._manifest_path = self._store_dir / "manifest.json"
        self._index_path = self._store_dir / "index.json"

        # In-memory caches
        self._manifest: Manifest | None = None
        self._index: dict[str, PacketLocation] | None = None

        self._store_dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_dir(self) -> Path:
        """Get the store directory path."""
        return self._store_dir

    @property
    def max_file_size(self) -> int:
        """Get the maximum (compressed) file size before sealing."""
        return self._max_file_size

    def _load_manifest(self) -> Manifest:
        """Load or create the manifest."""
        if self._manifest is None:
            if self._manifest_path.exists():
                with open(self._manifest_path) as f:
                    self._manifest = Manifest.from_dict(json.load(f))
            else:
                self._manifest = Manifest()
        return self._manifest

    def _save_manifest(self) -> None:
        """Save the manifest to disk."""
        with open(self._manifest_path, "w") as f:
            json.dump(self._load_manifest().to_dict(), f, indent=2)

    def _load_index(self) -> dict[str, PacketLocation]:
        """Load or create the index."""
        if self._index is None:
            if self._index_path.exists():
                with open(self._index_path) as f:
                    self._index = {k: PacketLocation.from_dict(v) for k, v in json.load(f).items()}
            else:
                self._index = {}
        return self._index

    def _save_index(self) -> None:
        """Save the index to disk."""
        with open(self._index_path, "w") as f:
            json.dump({k: v.to_dict() for k, v in self._load_index().items()}, f, separators=(",", ":"))

    def _get_or_create_open_file(self) -> FileMetadata:
        """Get the current open file or create a new one."""
        manifest = self._load_manifest()
        open_file = manifest.get_open_file()
        if open_file is None:
            open_file = FileMetadata(filename=f"packets_{len(manifest.files):03d}{FILE_SUFFIX}", status="open")
            manifest.files.append(open_file)
            (self._store_dir / open_file.filename).touch()
        return open_file

    def _file_meta(self, filename: str) -> FileMetadata | None:
        for file_meta in self._load_manifest().files:
            if file_meta.filename == filename:
                return file_meta
        return None

    def contains(self, packet_id: str) -> bool:
        """Check if a packet exists in the store."""
        return packet_id in self._load_index()

    def get_location(self, packet_id: str) -> PacketLocation | None:
        """Get the location of a packet, or None."""
        return self._load_index().get(packet_id)

    def is_in_sealed_file(self, packet_id: str) -> bool:
        """Check if a packet is in a sealed (immutable) file."""
        location = self.get_location(packet_id)
        if location is None:
            return False
        file_meta = self._file_meta(location.file)
        return file_meta is not None and file_meta.status == "sealed"

    def reset(self) -> None:
        """Delete every file of this store (packets, manifest, index, stats) to rebuild it."""
        for path in self._store_dir.iterdir():
            if path.is_file() and (
                path.name.startswith("packets_") or path.name in ("manifest.json", "index.json", "stats.md")
            ):
                path.unlink()
        self._manifest = Manifest()
        self._index = {}
        _read_block.cache_clear()

    def _write(self, packets: list[Packet], skip_if_sealed: bool) -> dict[str, tuple[bool, str]]:
        """Write packets to the open file(s) in blocks; update the in-memory index and manifest."""
        index = self._load_index()
        manifest = self._load_manifest()
        results: dict[str, tuple[bool, str]] = {}
        pending: list[Packet] = []
        for packet in packets:
            location = index.get(packet["id"])
            if location is not None:
                file_meta = self._file_meta(location.file)
                if file_meta is not None and file_meta.status == "sealed":
                    if skip_if_sealed:
                        results[packet["id"]] = (False, location.file)
                        continue
                    raise ValueError(f"Packet {packet['id']} is in sealed file {location.file}")
            pending.append(packet)

        for start in range(0, len(pending), self._block_size):
            block = pending[start : start + self._block_size]
            open_file = self._get_or_create_open_file()
            member = gzip.compress(b"".join(_encode(p) for p in block), mtime=0)
            offset = open_file.size_bytes
            with open(self._store_dir / open_file.filename, "ab") as f:
                f.write(member)
            for line, packet in enumerate(block):
                is_update = packet["id"] in index
                index[packet["id"]] = PacketLocation(open_file.filename, offset, len(member), line)
                if not is_update:
                    open_file.packet_count += 1
                    manifest.total_packets += 1
                results[packet["id"]] = (True, open_file.filename)
            open_file.size_bytes += len(member)
            manifest.total_size_bytes += len(member)
            if open_file.size_bytes > self._hard_limit:
                raise FileSizeExceededError(
                    f"File {open_file.filename} exceeds hard limit of "
                    f"{self._hard_limit / (1024 * 1024):.0f}MB "
                    f"(current size: {open_file.size_bytes / (1024 * 1024):.1f}MB). "
                    f"This should not happen - check for unusual packet sizes."
                )
            if open_file.size_bytes >= self._max_file_size:
                open_file.status = "sealed"
                open_file.sealed_at = datetime.now().isoformat()
        return results

    def add_packet(self, packet: Packet, skip_if_sealed: bool = True) -> tuple[bool, str]:
        """Add one packet (written as its own block).

        If the packet ID already exists in a sealed file, it is skipped
        (if skip_if_sealed=True) or an error is raised. A packet already in the open
        file is appended again and the index points to the new version.

        Args:
            packet: The evidence packet (a profile dict) to add.
            skip_if_sealed: If True, skip packets already in sealed files.

        Returns:
            Tuple of (was_added, filename). was_added is False if skipped.

        Raises:
            ValueError: If packet is in sealed file and skip_if_sealed=False.
        """
        result = self._write([packet], skip_if_sealed)[packet["id"]]
        self._save_index()
        self._save_manifest()
        return result

    def add_packets(self, packets: Iterable[Packet], skip_if_sealed: bool = True) -> dict[str, tuple[bool, str]]:
        """Add many packets in blocks, writing the index and manifest once at the end.

        Args:
            packets: Evidence packets (profile dicts) to add.
            skip_if_sealed: If True, skip packets already in sealed files.

        Returns:
            Dictionary mapping packet_id -> (was_added, filename).
        """
        results = self._write(list(packets), skip_if_sealed)
        self._save_index()
        self._save_manifest()
        return results

    def merge_evidence(
        self,
        packet_id: str,
        new_evidence_lines: list[dict],
        skip_if_sealed: bool = True,
    ) -> tuple[bool, str]:
        """Merge new evidence lines into an existing packet's statement.

        Args:
            packet_id: ID of the packet to update.
            new_evidence_lines: SieveEvidenceLine dicts to add.
            skip_if_sealed: If True, skip if packet is in a sealed file.

        Returns:
            Tuple of (was_updated, filename). was_updated is False if skipped.

        Raises:
            ValueError: If packet doesn't exist or is in sealed file and skip_if_sealed=False.
        """
        packet = self.get_packet(packet_id)
        if packet is None:
            raise ValueError(f"Packet {packet_id} not found in store")

        location = self.get_location(packet_id)
        if self.is_in_sealed_file(packet_id):
            if skip_if_sealed:
                return (False, location.file if location else "")
            raise ValueError(f"Packet {packet_id} is in a sealed file")

        lines = packet["statement"].setdefault("has_evidence_lines", [])
        existing = {line["id"] for line in lines}
        added = [line for line in new_evidence_lines if line["id"] not in existing]
        if not added:
            return (False, location.file if location else "")
        lines.extend(added)
        return self.add_packet(packet, skip_if_sealed=skip_if_sealed)

    def get_packet(self, packet_id: str) -> Packet | None:
        """Retrieve a packet by ID (decompresses the one block holding it).

        Args:
            packet_id: The packet ID to retrieve.

        Returns:
            The packet dict if found, None otherwise.
        """
        location = self.get_location(packet_id)
        if location is None:
            return None
        path = self._store_dir / location.file
        if not path.exists():
            return None
        lines = _read_block(str(path), location.offset, location.length)
        data: Packet = json.loads(lines[location.line])
        return data

    def iter_packets(
        self,
        file_filter: str | None = None,
        deduplicate: bool = True,
    ) -> Iterator[Packet]:
        """Iterate over all packets in the store, in file order.

        Args:
            file_filter: Optional filename to filter by (e.g., "packets_000.jsonl.gz").
            deduplicate: If True (default), only return the latest version of each
                packet (as tracked by the index). Set to False to get all versions.

        Yields:
            Packet dicts.
        """
        index = self._load_index() if deduplicate else {}
        for file_meta in self._load_manifest().files:
            if file_filter and file_meta.filename != file_filter:
                continue
            path = self._store_dir / file_meta.filename
            if not path.exists():
                continue
            for offset, _, text in iter_members(path.read_bytes()):
                for line, raw in enumerate(text.splitlines()):
                    if not raw.strip():
                        continue
                    data = json.loads(raw)
                    loc = index.get(data.get("id")) if deduplicate else None
                    if loc is not None and (loc.file, loc.offset, loc.line) != (file_meta.filename, offset, line):
                        continue  # an older version
                    yield data

    def iter_file(self, filename: str) -> Iterator[Packet]:
        """Iterate over packets in a specific file."""
        yield from self.iter_packets(file_filter=filename)

    def get_stats(self) -> dict:
        """Get store statistics."""
        manifest = self._load_manifest()
        index = self._load_index()
        open_file = manifest.get_open_file()
        return {
            "store_dir": str(self._store_dir),
            "max_file_size_mb": self._max_file_size / (1024 * 1024),
            "total_files": len(manifest.files),
            "sealed_files": len(manifest.get_sealed_files()),
            "open_file": open_file.filename if open_file else None,
            "open_file_size_mb": (open_file.size_bytes / (1024 * 1024) if open_file else 0),
            "total_packets": manifest.total_packets,
            "indexed_packets": len(index),
            "total_size_mb": manifest.total_size_bytes / (1024 * 1024),
        }

    def list_files(self) -> list[dict]:
        """List all files with their metadata."""
        return [f.to_dict() for f in self._load_manifest().files]

    def rebuild_index(self) -> int:
        """Rebuild the index by scanning all files (recovery, or after manual changes).

        Returns:
            Number of packets indexed.
        """
        new_index: dict[str, PacketLocation] = {}
        for file_meta in self._load_manifest().files:
            path = self._store_dir / file_meta.filename
            if not path.exists():
                continue
            for offset, length, text in iter_members(path.read_bytes()):
                for line, raw in enumerate(text.splitlines()):
                    if raw.strip():
                        new_index[json.loads(raw)["id"]] = PacketLocation(file_meta.filename, offset, length, line)
        self._index = new_index
        self._save_index()
        return len(new_index)


@lru_cache(maxsize=8)
def _read_block(path: str, offset: int, length: int) -> list[bytes]:
    """Decompress one gzip member of a store file into its lines (cached)."""
    with open(path, "rb") as f:
        f.seek(offset)
        return gzip.decompress(f.read(length)).splitlines()
