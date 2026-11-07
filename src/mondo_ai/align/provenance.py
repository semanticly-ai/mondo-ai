"""Provenance helpers: a run that cannot be reproduced is not a result.

Every loaded resource records where the bytes came from and what they hashed to. The
checksum goes into ``run.yaml`` and into the SSSOM set-level metadata, so a mapping set can
be re-derived — or shown to be stale — without guessing which release was in play.

Example:
    >>> import tempfile, pathlib
    >>> p = pathlib.Path(tempfile.mkdtemp()) / "x.txt"
    >>> _ = p.write_text("hello")
    >>> prov = file_provenance(p)
    >>> prov["sha256"][:12]
    '2cf24dba5fb0'
    >>> len(prov["retrieved"]) == 10
    True
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of a file, read in chunks.

    Args:
        path: File to hash.
        chunk: Read size in bytes. The default keeps a 100 MB OWL file off the heap.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "x"
        >>> _ = p.write_text("abc")
        >>> sha256_file(p)[:8]
        'ba7816bf'
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def file_provenance(path: str | Path) -> dict[str, str]:
    """Return the provenance fields a :class:`ResourceSet` records for a file.

    Uses the file's own modification date rather than today's, so re-running the pipeline
    over an unchanged download does not silently claim a fresh retrieval.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "x"
        >>> _ = p.write_text("abc")
        >>> sorted(file_provenance(p))
        ['retrieved', 'sha256']
    """
    p = Path(path)
    return {
        "sha256": sha256_file(p),
        "retrieved": date.fromtimestamp(p.stat().st_mtime).isoformat(),
    }


def content_hash(*parts: str) -> str:
    """Return a short stable hash over string parts, for cache keys and record ids.

    Example:
        >>> content_hash("a", "b") == content_hash("a", "b")
        True
        >>> content_hash("a", "b") == content_hash("ab")
        False
        >>> len(content_hash("a"))
        16
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]
