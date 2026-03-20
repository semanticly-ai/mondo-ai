#!/usr/bin/env python
"""Migrate legacy evidence packets to SIEVE format.

This script converts evidence packets from the old mondo_ai schema
to the new SIEVE/SEPIO-based schema.

Usage:
    python scripts/migrate_to_sieve.py evidence_packets/
    python scripts/migrate_to_sieve.py evidence_packets/ --output evidence_packets_sieve/
"""

import sys
from datetime import date
from pathlib import Path

import yaml


def migrate_predicate(predicate: str) -> dict:
    """Convert predicate string to SEPIO Coding object."""
    predicate_map = {
        "EXACT_SYNONYM": {
            "code": "oio:hasExactSynonym",
            "label": "exact synonym",
            "system": "http://www.geneontology.org/formats/oboInOwl#",
        },
        "NARROW_SYNONYM": {
            "code": "oio:hasNarrowSynonym",
            "label": "narrow synonym",
            "system": "http://www.geneontology.org/formats/oboInOwl#",
        },
        "BROAD_SYNONYM": {
            "code": "oio:hasBroadSynonym",
            "label": "broad synonym",
            "system": "http://www.geneontology.org/formats/oboInOwl#",
        },
        "RELATED_SYNONYM": {
            "code": "oio:hasRelatedSynonym",
            "label": "related synonym",
            "system": "http://www.geneontology.org/formats/oboInOwl#",
        },
        "LABEL": {
            "code": "rdfs:label",
            "label": "label",
            "system": "http://www.w3.org/2000/01/rdf-schema#",
        },
        "DEFINITION": {
            "code": "IAO:0000115",
            "label": "definition",
            "system": "http://purl.obolibrary.org/obo/",
        },
        "PARENT": {
            "code": "rdfs:subClassOf",
            "label": "subclass of",
            "system": "http://www.w3.org/2000/01/rdf-schema#",
        },
    }
    return predicate_map.get(predicate, {"code": predicate, "label": predicate.lower()})


def migrate_direction(direction: str) -> str:
    """Convert direction to SIEVE format."""
    direction_map = {
        "SUPPORTS": "supports",
        "CONTRADICTS": "disputes",
        "UNCERTAIN": "neutral",
    }
    return direction_map.get(direction, "neutral")


def reliability_to_strength(reliability: float | None) -> str:
    """Convert reliability to SIEVE strength."""
    if reliability is None:
        return "moderate"
    if reliability >= 0.8:
        return "strong"
    if reliability >= 0.5:
        return "moderate"
    return "weak"


def migrate_evidence_item(evidence: dict, counter: int) -> dict:
    """Migrate a legacy evidence item to SIEVE format."""
    ev_type = evidence.get("evidence_type", "CONCORDANCE")

    if ev_type == "CONCORDANCE":
        return {
            "id": evidence.get("id", f"ev_{counter}"),
            "type": "ConcordanceItem",
            "evidenceItemType": "ConcordanceItem",
            "sourceName": evidence.get("source_resource_name"),
            "sourceId": evidence.get("source_resource"),
            "sourceSubject": evidence.get("source_entity_id"),
            "sourceSubjectLabel": evidence.get("source_entity_label"),
            "mappingJustification": evidence.get("mapping_predicate"),
            "description": evidence.get("description"),
        }
    elif ev_type == "LITERATURE":
        return {
            "id": evidence.get("id", f"ev_{counter}"),
            "type": "SieveDocument",
            "title": evidence.get("publication_title"),
            "pmid": evidence.get("publication_id", "").replace("PMID:", "") if evidence.get("publication_id") else None,
            "quote": evidence.get("quoted_text"),
            "quoteLocation": evidence.get("quote_location"),
            "description": evidence.get("description"),
        }
    elif ev_type == "COMPUTATIONAL":
        return {
            "id": evidence.get("id", f"ev_{counter}"),
            "type": "ComputationalResult",
            "description": evidence.get("description"),
        }
    else:
        # Unknown type - convert to generic
        return {
            "id": evidence.get("id", f"ev_{counter}"),
            "type": "SieveEvidenceItem",
            "description": evidence.get("description"),
        }


def migrate_evidence_to_line(evidence: dict, counter: int) -> dict:
    """Migrate a legacy evidence item to a SIEVE evidence line."""
    direction = migrate_direction(evidence.get("direction", "SUPPORTS"))
    strength = evidence.get("evidence_strength", 0.5)

    evidence_item = migrate_evidence_item(evidence, counter)

    return {
        "id": f"el_{counter:04d}",
        "type": "SieveEvidenceLine",
        "hasEvidenceItems": [evidence_item],
        "directionOfEvidenceProvided": direction,
        "strengthOfEvidenceProvided": reliability_to_strength(strength),
        "scoreOfEvidenceProvided": strength,
    }


def migrate_statement(statement: dict) -> dict:
    """Migrate a legacy CanonicalStatement to SieveStatement."""
    predicate = statement.get("predicate", "EXACT_SYNONYM")
    predicate_coding = migrate_predicate(predicate)

    subject_label = statement.get("subject_label")
    object_value = statement.get("object_value", "")

    return {
        "id": statement.get("statement_id", "stmt_unknown"),
        "type": "SieveStatement",
        "subject": statement.get("subject_mondo_id"),
        "subjectLabel": subject_label,
        "predicate": predicate_coding,
        "object": object_value,
        "objectLabel": object_value,
        "statementText": f"{subject_label or statement.get('subject_mondo_id')} has {predicate_coding['label']} '{object_value}'",
    }


def migrate_synthesis(synthesis: dict) -> dict | None:
    """Migrate legacy EvidenceSynthesis to SIEVE format."""
    if not synthesis:
        return None

    # Map action to direction
    action = synthesis.get("action", "REVIEW")
    direction_map = {
        "ADD": "supports",
        "CONFIRM": "supports",
        "REJECT": "disputes",
        "REVIEW": "neutral",
    }
    direction = direction_map.get(action, "neutral")

    confidence = synthesis.get("confidence", 0)

    result = {
        "summary": synthesis.get("summary", ""),
        "score": {
            "value": confidence,
            "description": "AI confidence score",
        },
        "direction": direction,
        "cited_evidence": synthesis.get("cited_evidence", []),
    }

    # Add generated_by if model info available
    model_id = synthesis.get("model_id")
    if model_id:
        result["generated_by"] = {
            "id": f"act_{synthesis.get('generated_at', 'unknown')[:8]}",
            "type": "CurationActivity",
            "contributor": {
                "id": model_id,
                "type": "Agent",
                "name": model_id,
            },
        }

    return result


def migrate_packet(packet: dict) -> dict:
    """Migrate a legacy EvidencePacket to SIEVE format."""
    # Migrate statement
    statement = migrate_statement(packet.get("statement", {}))

    # Migrate evidence to evidence lines
    evidence_lines = []
    for i, ev in enumerate(packet.get("evidence", [])):
        evidence_lines.append(migrate_evidence_to_line(ev, i))

    # Migrate synthesis
    synthesis = migrate_synthesis(packet.get("evidence_synthesis"))

    # Map status
    status = packet.get("status", "UNREVIEWED")

    # Build new packet
    new_packet = {
        "id": packet.get("id"),
        "statement": statement,
        "status": status,
        "hasEvidenceLines": evidence_lines if evidence_lines else None,
        "evidence_synthesis": synthesis,
        "created": str(date.today()),
    }

    # Remove None values
    return {k: v for k, v in new_packet.items() if v is not None}


def migrate_file(input_path: Path, output_path: Path) -> bool:
    """Migrate a single YAML file.

    Returns True if successful, False otherwise.
    """
    try:
        with open(input_path) as f:
            data = yaml.safe_load(f)

        if not data:
            print(f"  Skipping empty file: {input_path}")
            return False

        # Check if already in SIEVE format
        if "hasEvidenceLines" in data:
            print(f"  Already in SIEVE format: {input_path}")
            return True

        # Migrate
        migrated = migrate_packet(data)

        # Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            yaml.dump(migrated, f, default_flow_style=False, sort_keys=False)

        print(f"  Migrated: {input_path} -> {output_path}")
        return True

    except Exception as e:
        print(f"  Error migrating {input_path}: {e}")
        return False


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python scripts/migrate_to_sieve.py <input_dir> [--output <output_dir>]")
        sys.exit(1)

    input_dir = Path(sys.argv[1])

    # Check for output directory argument
    output_dir = input_dir
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_dir = Path(sys.argv[idx + 1])

    if not input_dir.exists():
        print(f"Error: Input directory does not exist: {input_dir}")
        sys.exit(1)

    print(f"Migrating packets from {input_dir} to {output_dir}")
    print("-" * 50)

    # Find all packet YAML files
    yaml_files = list(input_dir.glob("pkt_*.yaml"))
    if not yaml_files:
        print("No packet files found (pkt_*.yaml)")
        sys.exit(0)

    success_count = 0
    error_count = 0

    for yaml_file in yaml_files:
        relative_path = yaml_file.relative_to(input_dir)
        output_path = output_dir / relative_path

        if migrate_file(yaml_file, output_path):
            success_count += 1
        else:
            error_count += 1

    print("-" * 50)
    print(f"Migration complete: {success_count} success, {error_count} errors")


if __name__ == "__main__":
    main()
