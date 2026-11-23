"""Load SIEVE profile packets.

Packets are plain dicts in the shape of the profile (as loaded from YAML or JSON).
``packet_model`` validates one through the generated Pydantic model, which resolves the
polymorphic evidence slots by their ``type``; it is slow (tens of milliseconds per
packet), so use it for single packets and samples, not whole stores.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from mondo_ai.datamodel.sieve_models import EvidencePacket


def packet_model(packet: dict[str, Any]) -> EvidencePacket:
    """Validate a packet dict against the generated Pydantic model.

    >>> p = packet_model({
    ...     "id": "mondoai:pkt/x", "version": "2026-11-20",
    ...     "statement": {"id": "mondoai:st/x", "type": "SynonymStatement", "subject": "MONDO:1",
    ...                   "predicate": {"code": "oboInOwl:hasExactSynonym"}, "object": "x"},
    ...     "reference_snapshot": {"id": "mondoai:pkt/x#references"}})
    >>> type(p.statement).__name__
    'SynonymStatement'
    """
    return EvidencePacket.model_validate(packet)


def load_packet(path: Path | str) -> dict[str, Any]:
    """Load one packet dict from a YAML or JSON file.

    >>> load_packet("pkt.yaml")  # doctest: +SKIP
    """
    path = Path(path)
    with open(path) as f:
        if path.suffix == ".json":
            data: dict[str, Any] = json.load(f)
        else:
            data = yaml.safe_load(f)
    return data


def load_packets(path: Path | str, pattern: str = "*.yaml") -> list[dict[str, Any]]:
    """Load every packet file matching ``pattern`` in a directory, sorted by name."""
    return [load_packet(p) for p in sorted(Path(path).glob(pattern))]
