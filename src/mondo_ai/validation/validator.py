"""Evidence packet validation for conversion to CurationRecord format.

This module validates EvidencePackets by converting them to the
CurationRecord format and performing structural validation.

Supports both legacy mondo-ai packets and new SIEVE format packets.

Note: Full LinkML schema validation is currently skipped due to a known issue
with the `designates_type` feature in the schema. Basic structural
validation is performed instead.

Example:
    >>> from mondo_ai.validation import validate_packet_file
    >>> result = validate_packet_file(Path("packet.yaml"))  # doctest: +SKIP
    >>> print(result.summary())  # doctest: +SKIP
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ValidationError:
    """A validation error for a packet."""

    packet_id: str
    field: str
    message: str
    value: Any = None


@dataclass
class ValidationResult:
    """Result of validating a set of packets."""

    valid_count: int = 0
    invalid_count: int = 0
    errors: list[ValidationError] = field(default_factory=list)
    validated_records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Return True if all packets are valid."""
        return self.invalid_count == 0

    def summary(self) -> str:
        """Return a summary of validation results."""
        lines = [
            "Validation Summary:",
            f"  Valid packets: {self.valid_count}",
            f"  Invalid packets: {self.invalid_count}",
        ]
        if self.errors:
            lines.append(f"  Total errors: {len(self.errors)}")
            # Group errors by type
            error_types: dict[str, int] = {}
            for err in self.errors:
                key = err.field
                error_types[key] = error_types.get(key, 0) + 1
            for field_name, count in sorted(error_types.items()):
                lines.append(f"    - {field_name}: {count}")
        return "\n".join(lines)


# Valid values from CurationRecord schema
VALID_CURATION_STATUSES = {"UNREVIEWED", "ACCEPTED", "REJECTED", "CONTROVERSIAL"}
VALID_EVIDENCE_TYPES = {"CONCORDANCE", "LITERATURE", "EXPERT_REVIEW", "COMPUTATIONAL", "OTHER",
                        "ConcordanceItem", "SieveDocument", "ComputationalResult"}
VALID_EVIDENCE_DIRECTIONS = {"SUPPORTS", "CONTRADICTS", "UNCERTAIN", "supports", "disputes", "neutral"}
VALID_SOURCE_TYPES = {"ONTOLOGY", "TERMINOLOGY", "DATABASE", "OTHER"}


def _map_predicate_to_curie(predicate: str | dict[str, Any]) -> str:
    """Map mondo-ai predicate to OWL/OBO predicate CURIE.

    Args:
        predicate: The predicate string (e.g., "EXACT_SYNONYM") or Coding dict

    Returns:
        The mapped predicate CURIE (e.g., "oboInOwl:hasExactSynonym")
    """
    # Handle SIEVE Coding object
    if isinstance(predicate, dict):
        code = predicate.get("code", "")
        if code:
            return code
        return predicate.get("label", str(predicate))

    predicate_map = {
        "EXACT_SYNONYM": "oboInOwl:hasExactSynonym",
        "RELATED_SYNONYM": "oboInOwl:hasRelatedSynonym",
        "NARROW_SYNONYM": "oboInOwl:hasNarrowSynonym",
        "BROAD_SYNONYM": "oboInOwl:hasBroadSynonym",
        "LABEL": "rdfs:label",
        "DEFINITION": "IAO:0000115",
        "PARENT": "rdfs:subClassOf",
        "oio:hasExactSynonym": "oboInOwl:hasExactSynonym",
        "oio:hasRelatedSynonym": "oboInOwl:hasRelatedSynonym",
        "oio:hasNarrowSynonym": "oboInOwl:hasNarrowSynonym",
        "oio:hasBroadSynonym": "oboInOwl:hasBroadSynonym",
        "exact synonym": "oboInOwl:hasExactSynonym",
        "related synonym": "oboInOwl:hasRelatedSynonym",
        "narrow synonym": "oboInOwl:hasNarrowSynonym",
        "broad synonym": "oboInOwl:hasBroadSynonym",
    }
    return predicate_map.get(predicate, predicate)


def _is_sieve_format(packet_data: dict[str, Any]) -> bool:
    """Check if packet is in SIEVE format."""
    statement = packet_data.get("statement", {})
    # SIEVE format uses 'subject' instead of 'subject_mondo_id'
    # and 'has_evidence_lines' instead of 'evidence'
    return "subject" in statement or "has_evidence_lines" in packet_data


def _convert_sieve_packet(packet_data: dict[str, Any]) -> dict[str, Any]:
    """Convert a SIEVE format packet to CurationRecord format.

    Args:
        packet_data: The packet data in SIEVE format

    Returns:
        A dict in CurationRecord format
    """
    statement = packet_data.get("statement", {})

    # Get predicate and map to CURIE
    predicate = statement.get("predicate", "")
    predicate_curie = _map_predicate_to_curie(predicate)
    predicate_label = predicate.get("label", str(predicate)) if isinstance(predicate, dict) else predicate

    # Get object value
    object_value = statement.get("object", "") or statement.get("object_label", "")
    object_id = statement.get("objectId")

    # If no object_id, use object_value as a literal identifier
    if not object_id and object_value:
        object_id = f"_:literal_{hash(object_value) % 10000:04d}"

    # Build assertion in CurationRecord format
    assertion = {
        "subject_id": statement.get("subject"),
        "subject_label": statement.get("subject_label"),
        "predicate": predicate_curie,
        "predicate_label": predicate_label,
        "object_id": object_id or object_value,
        "object_label": object_value,
        "display_text": f"{statement.get('subject_label', '')} {predicate_label} {object_value}",
    }

    # Build evidence items from SIEVE has_evidence_lines
    evidence_items = []
    for line in packet_data.get("has_evidence_lines", []):
        direction = line.get("direction_of_evidence_provided", "supports")
        # Map SIEVE direction to uppercase
        direction_map = {"supports": "SUPPORTS", "disputes": "CONTRADICTS", "neutral": "UNCERTAIN"}
        direction = direction_map.get(direction, direction.upper() if direction else "SUPPORTS")

        strength = line.get("score_of_evidence_provided", 1.0)

        for item in line.get("has_evidence_items", []):
            item_type = item.get("evidence_item_type") or item.get("type", "OTHER")
            if isinstance(item_type, dict):
                item_type = item_type.get("value", "OTHER")

            # Map SIEVE item types to validation types
            type_map = {"ConcordanceItem": "CONCORDANCE", "SieveDocument": "LITERATURE",
                       "ComputationalResult": "COMPUTATIONAL"}
            ev_type = type_map.get(item_type, item_type)

            record_ev = {
                "id": item.get("id"),
                "evidence_type": ev_type,
                "direction": direction,
                "evidence_strength": strength,
                "description": item.get("description"),
            }

            # Add ConcordanceItem-specific fields
            if item_type == "ConcordanceItem":
                if item.get("source_name"):
                    record_ev["source_name"] = item.get("source_name")
                if item.get("source_subject"):
                    record_ev["source_subject_id"] = item.get("source_subject")
                if item.get("source_subject_label"):
                    record_ev["source_subject_label"] = item.get("source_subject_label")
                record_ev["source_type"] = "ONTOLOGY"

            # Add SieveDocument-specific fields
            if item_type == "SieveDocument":
                if item.get("pmid"):
                    record_ev["publication_id"] = f"PMID:{item.get('pmid')}"
                if item.get("title"):
                    record_ev["publication_title"] = item.get("title")

            evidence_items.append(record_ev)

    # Get status
    status = packet_data.get("status", "UNREVIEWED")
    if isinstance(status, dict):
        status = status.get("value", "UNREVIEWED")

    # Get confidence from evidence_synthesis if available
    confidence = None
    synthesis = packet_data.get("evidence_synthesis")
    if synthesis:
        score = synthesis.get("score")
        if isinstance(score, dict):
            confidence = score.get("value")
        elif isinstance(score, (int, float)):
            confidence = score

    # Build the CurationRecord dict
    record = {
        "id": packet_data.get("id"),
        "assertion": assertion,
        "evidence": evidence_items if evidence_items else None,
        "status": status,
        "confidence": confidence,
    }

    # Add timestamps if present
    if packet_data.get("created"):
        record["created_at"] = packet_data.get("created")

    return record


def convert_packet_to_curation_record_dict(packet_data: dict[str, Any]) -> dict[str, Any]:
    """Convert a mondo-ai EvidencePacket dict to CurationRecord dict format.

    This converts from mondo-ai's schema (statement with subject_mondo_id, etc.)
    or SIEVE schema to CurationRecord schema (assertion with subject_id, object_id, etc.).

    Args:
        packet_data: The packet data as loaded from YAML

    Returns:
        A dict in CurationRecord format
    """
    # Check if this is SIEVE format
    if _is_sieve_format(packet_data):
        return _convert_sieve_packet(packet_data)

    # Legacy format conversion
    statement = packet_data.get("statement", {})

    # Get predicate and map to CURIE
    predicate = statement.get("predicate", "")
    if isinstance(predicate, dict):
        predicate = predicate.get("value", str(predicate))
    predicate_curie = _map_predicate_to_curie(predicate)

    # For synonyms, object_id should be a string value (not a CURIE)
    # Sieve schema requires object_id as uriorcurie
    object_value = statement.get("object_value", "")
    object_id = statement.get("object_id")

    # If no object_id, use object_value as a literal identifier
    if not object_id and object_value:
        # For synonym predicates, object is a literal string
        # Use a blank node identifier for the literal
        object_id = f"_:literal_{hash(object_value) % 10000:04d}"

    # Build assertion in CurationRecord format
    assertion = {
        "subject_id": statement.get("subject_mondo_id"),
        "subject_label": statement.get("subject_label"),
        "predicate": predicate_curie,
        "predicate_label": predicate,
        "object_id": object_id or object_value,
        "object_label": object_value,
        "display_text": f"{statement.get('subject_label', '')} {predicate} {object_value}",
    }

    # Build evidence items in CurationRecord format
    evidence_items = []
    for ev in packet_data.get("evidence", []):
        ev_type = ev.get("evidence_type", "OTHER")
        if isinstance(ev_type, dict):
            ev_type = ev_type.get("value", "OTHER")

        ev_direction = ev.get("direction", "SUPPORTS")
        if isinstance(ev_direction, dict):
            ev_direction = ev_direction.get("value", "SUPPORTS")

        record_ev = {
            "id": ev.get("id"),
            "evidence_type": ev_type,
            "direction": ev_direction,
            "evidence_strength": ev.get("evidence_strength", 1.0),
            "description": ev.get("description"),
        }

        # Add concordance-specific fields if present
        if ev_type == "CONCORDANCE":
            if ev.get("source_resource"):
                record_ev["source"] = ev.get("source_resource")
            if ev.get("source_resource_name"):
                record_ev["source_name"] = ev.get("source_resource_name")
            if ev.get("source_entity_id"):
                record_ev["source_subject_id"] = ev.get("source_entity_id")
            if ev.get("source_entity_label"):
                record_ev["source_subject_label"] = ev.get("source_entity_label")
            record_ev["source_type"] = "ONTOLOGY"

        evidence_items.append(record_ev)

    # Get status
    status = packet_data.get("status", "UNREVIEWED")
    if isinstance(status, dict):
        status = status.get("value", "UNREVIEWED")

    # Build the CurationRecord dict
    record = {
        "id": packet_data.get("id"),
        "assertion": assertion,
        "evidence": evidence_items if evidence_items else None,
        "status": status,
        "confidence": packet_data.get("evidence_score"),
    }

    # Add timestamps if present
    if packet_data.get("created_at"):
        record["created_at"] = packet_data.get("created_at")
    if packet_data.get("updated_at"):
        record["updated_at"] = packet_data.get("updated_at")

    return record


def _validate_curation_record(record: dict[str, Any], packet_id: str) -> list[ValidationError]:
    """Validate a CurationRecord dict structurally.

    This performs basic structural validation against CurationRecord schema
    requirements without using the full LinkML validator (which has
    issues with polymorphic types).

    Args:
        record: The converted CurationRecord dict
        packet_id: The original packet ID for error messages

    Returns:
        List of validation errors
    """
    errors: list[ValidationError] = []

    # Validate id (required)
    if not record.get("id"):
        errors.append(ValidationError(
            packet_id=packet_id,
            field="id",
            message="Record ID is required",
        ))

    # Validate assertion (required)
    assertion = record.get("assertion")
    if not assertion:
        errors.append(ValidationError(
            packet_id=packet_id,
            field="assertion",
            message="Assertion is required",
        ))
    else:
        # Validate assertion fields (all required in CurationRecord schema)
        if not assertion.get("subject_id"):
            errors.append(ValidationError(
                packet_id=packet_id,
                field="assertion.subject_id",
                message="Subject ID is required",
            ))
        if not assertion.get("predicate"):
            errors.append(ValidationError(
                packet_id=packet_id,
                field="assertion.predicate",
                message="Predicate is required",
            ))
        if not assertion.get("object_id"):
            errors.append(ValidationError(
                packet_id=packet_id,
                field="assertion.object_id",
                message="Object ID is required",
            ))

    # Validate status (required, must be valid enum value)
    status = record.get("status")
    if not status:
        errors.append(ValidationError(
            packet_id=packet_id,
            field="status",
            message="Status is required",
        ))
    elif status not in VALID_CURATION_STATUSES:
        errors.append(ValidationError(
            packet_id=packet_id,
            field="status",
            message=f"Invalid status '{status}'. Must be one of: {VALID_CURATION_STATUSES}",
            value=status,
        ))

    # Validate confidence (optional, but must be 0-1 if present)
    confidence = record.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)):
            errors.append(ValidationError(
                packet_id=packet_id,
                field="confidence",
                message="Confidence must be a number",
                value=confidence,
            ))
        elif confidence < 0 or confidence > 1:
            errors.append(ValidationError(
                packet_id=packet_id,
                field="confidence",
                message="Confidence must be between 0 and 1",
                value=confidence,
            ))

    # Validate evidence items
    evidence_list = record.get("evidence", [])
    if evidence_list:
        for i, ev in enumerate(evidence_list):
            # evidence_type is required
            ev_type = ev.get("evidence_type")
            if not ev_type:
                errors.append(ValidationError(
                    packet_id=packet_id,
                    field=f"evidence[{i}].evidence_type",
                    message="Evidence type is required",
                ))
            elif ev_type not in VALID_EVIDENCE_TYPES:
                errors.append(ValidationError(
                    packet_id=packet_id,
                    field=f"evidence[{i}].evidence_type",
                    message=f"Invalid evidence type '{ev_type}'. Must be one of: {VALID_EVIDENCE_TYPES}",
                    value=ev_type,
                ))

            # direction validation (optional, defaults to SUPPORTS)
            direction = ev.get("direction")
            if direction and direction not in VALID_EVIDENCE_DIRECTIONS:
                errors.append(ValidationError(
                    packet_id=packet_id,
                    field=f"evidence[{i}].direction",
                    message=f"Invalid direction '{direction}'. Must be one of: {VALID_EVIDENCE_DIRECTIONS}",
                    value=direction,
                ))

            # evidence_strength validation (optional, 0-1)
            strength = ev.get("evidence_strength")
            if strength is not None:
                if not isinstance(strength, (int, float)):
                    errors.append(ValidationError(
                        packet_id=packet_id,
                        field=f"evidence[{i}].evidence_strength",
                        message="Evidence strength must be a number",
                        value=strength,
                    ))
                elif strength < 0 or strength > 1:
                    errors.append(ValidationError(
                        packet_id=packet_id,
                        field=f"evidence[{i}].evidence_strength",
                        message="Evidence strength must be between 0 and 1",
                        value=strength,
                    ))

            # source_type validation for concordance evidence
            source_type = ev.get("source_type")
            if source_type and source_type not in VALID_SOURCE_TYPES:
                errors.append(ValidationError(
                    packet_id=packet_id,
                    field=f"evidence[{i}].source_type",
                    message=f"Invalid source type '{source_type}'. Must be one of: {VALID_SOURCE_TYPES}",
                    value=source_type,
                ))

    return errors


def validate_packet_data(packet_data: dict[str, Any]) -> tuple[list[ValidationError], dict[str, Any] | None]:
    """Validate a single packet dict and convert to CurationRecord format.

    Args:
        packet_data: The packet data as loaded from YAML

    Returns:
        Tuple of (validation errors, converted CurationRecord or None if invalid)
    """
    errors: list[ValidationError] = []
    packet_id = packet_data.get("id", "unknown")

    # Convert to CurationRecord format
    try:
        curation_record = convert_packet_to_curation_record_dict(packet_data)
    except Exception as e:
        errors.append(ValidationError(
            packet_id=packet_id,
            field="conversion",
            message=f"Failed to convert to CurationRecord format: {e}",
        ))
        return errors, None

    # Validate the converted record
    validation_errors = _validate_curation_record(curation_record, packet_id)
    errors.extend(validation_errors)

    if errors:
        return errors, None
    return errors, curation_record


def validate_packet_file(path: Path) -> ValidationResult:
    """Validate a packet from a YAML file and convert to CurationRecord format.

    Args:
        path: Path to the YAML file

    Returns:
        ValidationResult with errors and converted record if valid
    """
    result = ValidationResult()

    try:
        with open(path) as f:
            packet_data = yaml.safe_load(f)
    except Exception as e:
        result.invalid_count = 1
        result.errors.append(ValidationError(
            packet_id=path.stem,
            field="file",
            message=f"Failed to parse YAML: {e}",
        ))
        return result

    # Skip files that contain lists (like exported curation_records.yaml)
    if isinstance(packet_data, list):
        result.invalid_count = 1
        result.errors.append(ValidationError(
            packet_id=path.stem,
            field="file",
            message="File contains a list, not a single packet. Skipping.",
        ))
        return result

    errors, curation_record = validate_packet_data(packet_data)

    if errors:
        result.invalid_count = 1
        result.errors = errors
    else:
        result.valid_count = 1
        if curation_record:
            result.validated_records.append(curation_record)

    return result


def validate_packet_directory(path: Path) -> ValidationResult:
    """Validate all packets in a directory and convert to CurationRecord format.

    Args:
        path: Path to the directory

    Returns:
        ValidationResult with summary
    """
    result = ValidationResult()

    for yaml_file in sorted(path.glob("*.yaml")):
        # Skip known non-packet files
        if yaml_file.name in ("summary.yaml", "curation_records.yaml"):
            continue

        file_result = validate_packet_file(yaml_file)
        result.valid_count += file_result.valid_count
        result.invalid_count += file_result.invalid_count
        result.errors.extend(file_result.errors)
        result.validated_records.extend(file_result.validated_records)

    return result
