"""Helpers for loading SIEVE data models with proper polymorphism handling.

The SIEVE schema uses union types for evidence items (InformationEntity subclasses).
Pydantic's default deserialization doesn't handle this well, so we need to
manually dispatch to the correct subclass based on the 'type' field.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from mondo_ai.datamodel.sieve_models import (
    AgentContribution,
    ConcordanceItem,
    EvidencePacket,
    SieveDataItem,
    SieveDocument,
    SieveStudyResult,
    ComputationalResult,
    TextMiningResult,
)


# Map type strings to classes
EVIDENCE_ITEM_TYPES: dict[str, type[BaseModel]] = {
    "ConcordanceItem": ConcordanceItem,
    "AgentContribution": AgentContribution,
    "SieveDocument": SieveDocument,
    "SieveDataItem": SieveDataItem,
    "SieveStudyResult": SieveStudyResult,
    "ComputationalResult": ComputationalResult,
    "TextMiningResult": TextMiningResult,
    # Also handle base type names
    "Document": SieveDocument,
    "DataItem": SieveDataItem,
    "StudyResult": SieveStudyResult,
}


def _convert_evidence_items(data: dict[str, Any]) -> dict[str, Any]:
    """Convert evidence items to proper typed instances.

    This handles the polymorphism issue where InformationEntity subclass
    fields are rejected when deserializing through the base class.

    Args:
        data: Raw packet data dict from YAML.

    Returns:
        Data dict with evidence items converted to proper typed objects.
    """
    if "has_evidence_lines" not in data or not data["has_evidence_lines"]:
        return data

    for line in data["has_evidence_lines"]:
        if "has_evidence_items" not in line or not line["has_evidence_items"]:
            continue

        converted_items: list[Any] = []
        for item in line["has_evidence_items"]:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                item_class = EVIDENCE_ITEM_TYPES.get(item_type)
                if item_class:
                    # Validate through the correct subclass
                    converted_items.append(item_class.model_validate(item))
                else:
                    # Keep as-is for unknown types
                    converted_items.append(item)
            else:
                converted_items.append(item)

        line["has_evidence_items"] = converted_items

    return data


def packet_from_dict(data: dict[str, Any]) -> EvidencePacket:
    """Build an EvidencePacket from a raw dict, dispatching evidence items.

    Args:
        data: Raw packet data dict (e.g. parsed from YAML/JSON).

    Returns:
        EvidencePacket instance with evidence items as their concrete subclasses.
    """
    return EvidencePacket.model_validate(_convert_evidence_items(dict(data)))


def load_packet(path: Path | str) -> EvidencePacket:
    """Load an EvidencePacket from a YAML file with proper type handling.

    Args:
        path: Path to the YAML file.

    Returns:
        EvidencePacket instance.

    Example:
        >>> from mondo_ai.datamodel.loaders import load_packet
        >>> packet = load_packet("evidence_packets/pkt_abc123.yaml")  # doctest: +SKIP
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    return packet_from_dict(data)


def load_packets(path: Path | str, pattern: str = "pkt_*.yaml") -> list[EvidencePacket]:
    """Load multiple EvidencePackets from a directory.

    Args:
        path: Path to directory containing packet files.
        pattern: Glob pattern for packet files.

    Returns:
        List of EvidencePacket instances.
    """
    path = Path(path)
    packets = []
    for filepath in sorted(path.glob(pattern)):
        packets.append(load_packet(filepath))
    return packets
