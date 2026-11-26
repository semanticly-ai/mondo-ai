"""SSSOM mapping file reader.

This module reads SSSOM (Simple Standard for Sharing Ontological Mappings)
files and provides lookup functionality for translating between ontologies.

Example:
    >>> from mondo_ai.mapping.sssom_reader import SSSOMReader
    >>> reader = SSSOMReader("mondo.sssom.tsv")  # doctest: +SKIP
    >>> mondo_id = reader.get_mondo_id("DOID:4")  # doctest: +SKIP
    >>> print(mondo_id)  # doctest: +SKIP
    'MONDO:0000001'
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class Mapping:
    """A single SSSOM mapping."""

    subject_id: str
    subject_label: str | None
    predicate_id: str
    object_id: str
    object_label: str | None
    mapping_justification: str | None


class SSSOMReader:
    """Reader for SSSOM mapping files."""

    def __init__(self, path: str | Path) -> None:
        """Initialize reader with path to SSSOM file.

        Args:
            path: Path to the SSSOM TSV file.
        """
        self.path = Path(path)
        self._mappings: list[Mapping] = []
        self._mondo_to_external: dict[str, list[Mapping]] = {}
        self._external_to_mondo: dict[str, list[Mapping]] = {}
        self._mapping_set_id: str | None = None
        self._loaded = False

    def _load(self) -> None:
        """Load and parse the SSSOM file."""
        if self._loaded:
            return

        with open(self.path) as f:
            header_found = False
            header_cols: list[str] = []

            for line in f:
                line = line.strip()
                if not line:
                    continue

                # Parse metadata from comment lines
                if line.startswith("#"):
                    # Extract mapping_set_id from metadata
                    if line.startswith("# mapping_set_id:"):
                        self._mapping_set_id = line.split(":", 1)[1].strip()
                    continue

                # Parse header
                if not header_found:
                    header_cols = line.split("\t")
                    header_found = True
                    continue

                # Parse data row
                parts = line.split("\t")
                row = dict(zip(header_cols, parts))

                mapping = Mapping(
                    subject_id=row.get("subject_id", ""),
                    subject_label=row.get("subject_label"),
                    predicate_id=row.get("predicate_id", ""),
                    object_id=row.get("object_id", ""),
                    object_label=row.get("object_label"),
                    mapping_justification=row.get("mapping_justification"),
                )

                self._mappings.append(mapping)

                # Index by subject (MONDO)
                if mapping.subject_id.startswith("MONDO:"):
                    if mapping.subject_id not in self._mondo_to_external:
                        self._mondo_to_external[mapping.subject_id] = []
                    self._mondo_to_external[mapping.subject_id].append(mapping)

                    # Index by object (external ID)
                    if mapping.object_id not in self._external_to_mondo:
                        self._external_to_mondo[mapping.object_id] = []
                    self._external_to_mondo[mapping.object_id].append(mapping)

        self._loaded = True

    def get_mondo_id(self, external_id: str) -> str | None:
        """Get Mondo ID for an external ID.

        Args:
            external_id: External ID (e.g., "DOID:4", "OMIM:100100").

        Returns:
            Mondo ID if mapping exists, None otherwise.

        Example:
            >>> # reader = SSSOMReader("mondo.sssom.tsv")
            >>> # reader.get_mondo_id("DOID:4")
            >>> # 'MONDO:0000001'
        """
        self._load()
        mappings = self._external_to_mondo.get(external_id, [])
        if mappings:
            # Prefer exact matches
            for m in mappings:
                if "exactMatch" in m.predicate_id:
                    return m.subject_id
            return mappings[0].subject_id
        return None

    def get_mondo_mapping(self, external_id: str) -> Mapping | None:
        """Get full mapping for an external ID to Mondo.

        Args:
            external_id: External ID.

        Returns:
            Mapping object if found, None otherwise.
        """
        self._load()
        mappings = self._external_to_mondo.get(external_id, [])
        if mappings:
            for m in mappings:
                if "exactMatch" in m.predicate_id:
                    return m
            return mappings[0]
        return None

    def get_exact_mondo_mapping(self, external_id: str) -> Mapping | None:
        """Get the skos:exactMatch mapping from Mondo to an external ID, if there is one.

        Unlike ``get_mondo_mapping`` this never falls back to a broad or narrow match, so
        it is safe for normalizing the ids of a source's own claim to Mondo ids.

        Args:
            external_id: External ID.

        Returns:
            The exactMatch Mapping, or None.
        """
        self._load()
        for m in self._external_to_mondo.get(external_id, []):
            if m.predicate_id == "skos:exactMatch":
                return m
        return None

    def get_external_ids(self, mondo_id: str, prefix: str | None = None) -> list[str]:
        """Get external IDs mapped to a Mondo ID.

        Args:
            mondo_id: Mondo ID (e.g., "MONDO:0000001").
            prefix: Optional prefix filter (e.g., "DOID", "OMIM").

        Returns:
            List of external IDs.
        """
        self._load()
        mappings = self._mondo_to_external.get(mondo_id, [])
        result = [m.object_id for m in mappings]
        if prefix:
            result = [oid for oid in result if oid.startswith(f"{prefix}:")]
        return result

    def iter_mappings(
        self,
        predicate: str | None = None,
        object_prefix: str | None = None,
    ) -> Iterator[Mapping]:
        """Iterate over mappings with optional filters.

        Args:
            predicate: Filter by predicate (e.g., "skos:exactMatch").
            object_prefix: Filter by object prefix (e.g., "DOID").

        Yields:
            Mapping objects matching filters.
        """
        self._load()
        for mapping in self._mappings:
            if predicate and predicate not in mapping.predicate_id:
                continue
            if object_prefix and not mapping.object_id.startswith(f"{object_prefix}:"):
                continue
            yield mapping

    def get_mappings_for_prefix(self, prefix: str) -> dict[str, str]:
        """Get all mappings for a given prefix as external -> mondo dict.

        Args:
            prefix: External prefix (e.g., "DOID", "OMIM", "Orphanet").

        Returns:
            Dict mapping external IDs to Mondo IDs.

        Example:
            >>> # reader = SSSOMReader("mondo.sssom.tsv")
            >>> # doid_mappings = reader.get_mappings_for_prefix("DOID")
        """
        self._load()
        result: dict[str, str] = {}
        for mapping in self._mappings:
            if mapping.object_id.startswith(f"{prefix}:"):
                # Only keep exact matches, prefer first occurrence
                if "exactMatch" in mapping.predicate_id:
                    if mapping.object_id not in result:
                        result[mapping.object_id] = mapping.subject_id
        return result

    @property
    def mapping_count(self) -> int:
        """Get total number of mappings."""
        self._load()
        return len(self._mappings)

    @property
    def mapping_set_id(self) -> str | None:
        """Get the mapping_set_id from SSSOM metadata.

        Returns:
            The mapping_set_id URI if present in metadata, None otherwise.
        """
        self._load()
        return self._mapping_set_id
