"""Tests for the validation module."""

import pytest
from pathlib import Path
import tempfile
import yaml

from mondo_ai.validation import (
    ValidationError,
    ValidationResult,
    convert_packet_to_curation_record_dict,
    validate_packet_data,
    validate_packet_file,
    validate_packet_directory,
)


# Sample packet data for testing (legacy format)
SAMPLE_SYNONYM_PACKET = {
    "id": "pkt_test_001",
    "statement": {
        "statement_id": "stmt_test_001",
        "subject_mondo_id": "MONDO:0011441",
        "subject_label": "complex regional pain syndrome type 1",
        "predicate": "EXACT_SYNONYM",
        "object_value": "Reflex sympathetic dystrophy",
    },
    "bucket": "NEW",
    "evidence": [
        {
            "id": "ev_test_001",
            "evidence_type": "CONCORDANCE",
            "direction": "SUPPORTS",
            "evidence_strength": 0.9,
            "description": "Test concordance evidence",
        }
    ],
    "evidence_score": 1.0,
    "status": "UNREVIEWED",
    "created_at": "2026-01-06T14:52:55.486664Z",
}

SAMPLE_HIERARCHY_PACKET = {
    "id": "pkt_test_002",
    "statement": {
        "statement_id": "stmt_test_002",
        "subject_mondo_id": "MONDO:0015691",
        "subject_label": "hypereosinophilic syndrome",
        "predicate": "PARENT",
        "object_id": "MONDO:0004805",
        "object_value": "leukocyte disorder",
    },
    "bucket": "CONFIRM",
    "evidence": [
        {
            "id": "ev_test_002",
            "evidence_type": "CONCORDANCE",
            "direction": "SUPPORTS",
            "evidence_strength": 0.85,
            "description": "Test hierarchy evidence",
        }
    ],
    "evidence_score": 1.0,
    "status": "UNREVIEWED",
}

# Sample SIEVE format packet
SAMPLE_SIEVE_PACKET = {
    "id": "pkt_sieve_001",
    "statement": {
        "id": "stmt_sieve_001",
        "type": "SieveStatement",
        "subject": "MONDO:0011441",
        "subjectLabel": "complex regional pain syndrome type 1",
        "predicate": {
            "code": "oio:hasExactSynonym",
            "label": "exact synonym",
            "system": "http://www.geneontology.org/formats/oboInOwl#",
        },
        "object": "Reflex sympathetic dystrophy",
        "objectLabel": "Reflex sympathetic dystrophy",
    },
    "status": "UNREVIEWED",
    "hasEvidenceLines": [
        {
            "id": "el_0000",
            "type": "SieveEvidenceLine",
            "hasEvidenceItems": [
                {
                    "id": "ev_ordo_0001",
                    "type": "ConcordanceItem",
                    "evidenceItemType": "ConcordanceItem",
                    "sourceName": "Orphanet",
                    "sourceSubject": "ORDO:12345",
                    "description": "Synonym from Orphanet (ORDO): 'Reflex sympathetic dystrophy'",
                }
            ],
            "directionOfEvidenceProvided": "supports",
            "strengthOfEvidenceProvided": "strong",
            "scoreOfEvidenceProvided": 0.9,
        }
    ],
    "evidence_synthesis": {
        "summary": "Strong evidence supports this synonym.",
        "score": {"value": 0.85, "description": "AI confidence score"},
        "direction": "supports",
    },
    "created": "2026-01-28",
}


class TestConvertPacketToCurationRecordDict:
    """Tests for convert_packet_to_curation_record_dict function."""

    def test_synonym_packet_conversion(self):
        """Test converting a synonym packet to CurationRecord format."""
        result = convert_packet_to_curation_record_dict(SAMPLE_SYNONYM_PACKET)

        assert result["id"] == "pkt_test_001"
        assert result["status"] == "UNREVIEWED"
        assert result["confidence"] == 1.0

        # Check assertion
        assertion = result["assertion"]
        assert assertion["subject_id"] == "MONDO:0011441"
        assert assertion["subject_label"] == "complex regional pain syndrome type 1"
        assert assertion["predicate"] == "oboInOwl:hasExactSynonym"
        assert assertion["predicate_label"] == "EXACT_SYNONYM"
        assert assertion["object_label"] == "Reflex sympathetic dystrophy"
        # object_id should be a blank node for synonym literals
        assert assertion["object_id"].startswith("_:literal_")

        # Check evidence
        assert len(result["evidence"]) == 1
        evidence = result["evidence"][0]
        assert evidence["id"] == "ev_test_001"
        assert evidence["evidence_type"] == "CONCORDANCE"
        assert evidence["direction"] == "SUPPORTS"
        assert evidence["evidence_strength"] == 0.9

    def test_hierarchy_packet_conversion(self):
        """Test converting a hierarchy packet to CurationRecord format."""
        result = convert_packet_to_curation_record_dict(SAMPLE_HIERARCHY_PACKET)

        assert result["id"] == "pkt_test_002"

        # Check assertion
        assertion = result["assertion"]
        assert assertion["subject_id"] == "MONDO:0015691"
        assert assertion["predicate"] == "rdfs:subClassOf"
        assert assertion["predicate_label"] == "PARENT"
        # object_id should be the actual MONDO CURIE for hierarchy
        assert assertion["object_id"] == "MONDO:0004805"
        assert assertion["object_label"] == "leukocyte disorder"

    def test_predicate_mapping(self):
        """Test that predicates are mapped to correct CURIEs."""
        predicates = {
            "EXACT_SYNONYM": "oboInOwl:hasExactSynonym",
            "RELATED_SYNONYM": "oboInOwl:hasRelatedSynonym",
            "NARROW_SYNONYM": "oboInOwl:hasNarrowSynonym",
            "BROAD_SYNONYM": "oboInOwl:hasBroadSynonym",
            "LABEL": "rdfs:label",
            "DEFINITION": "IAO:0000115",
            "PARENT": "rdfs:subClassOf",
        }

        for mondo_pred, expected_curie in predicates.items():
            packet = {
                "id": "test",
                "statement": {
                    "subject_mondo_id": "MONDO:0000001",
                    "predicate": mondo_pred,
                    "object_value": "test value",
                },
                "evidence": [],
                "status": "UNREVIEWED",
            }
            result = convert_packet_to_curation_record_dict(packet)
            assert result["assertion"]["predicate"] == expected_curie

    def test_sieve_packet_conversion(self):
        """Test converting a SIEVE format packet to CurationRecord format."""
        result = convert_packet_to_curation_record_dict(SAMPLE_SIEVE_PACKET)

        assert result["id"] == "pkt_sieve_001"
        assert result["status"] == "UNREVIEWED"
        assert result["confidence"] == 0.85  # From evidence_synthesis.score.value

        # Check assertion
        assertion = result["assertion"]
        assert assertion["subject_id"] == "MONDO:0011441"
        assert assertion["subject_label"] == "complex regional pain syndrome type 1"
        assert assertion["predicate"] == "oio:hasExactSynonym"
        assert assertion["object_label"] == "Reflex sympathetic dystrophy"

        # Check evidence (converted from hasEvidenceLines)
        assert len(result["evidence"]) == 1
        evidence = result["evidence"][0]
        assert evidence["id"] == "ev_ordo_0001"
        assert evidence["evidence_type"] == "CONCORDANCE"
        assert evidence["direction"] == "SUPPORTS"
        assert evidence["evidence_strength"] == 0.9

    def test_sieve_coding_predicate(self):
        """Test that SIEVE Coding predicates are handled correctly."""
        packet = {
            "id": "test",
            "statement": {
                "subject": "MONDO:0000001",
                "predicate": {
                    "code": "oio:hasExactSynonym",
                    "label": "exact synonym",
                },
                "object": "test value",
            },
            "hasEvidenceLines": [],
            "status": "UNREVIEWED",
        }
        result = convert_packet_to_curation_record_dict(packet)
        assert result["assertion"]["predicate"] == "oio:hasExactSynonym"


class TestValidatePacketData:
    """Tests for validate_packet_data function."""

    def test_valid_packet(self):
        """Test validation of a valid packet."""
        errors, record = validate_packet_data(SAMPLE_SYNONYM_PACKET)

        assert len(errors) == 0
        assert record is not None
        assert record["id"] == "pkt_test_001"

    def test_valid_sieve_packet(self):
        """Test validation of a valid SIEVE packet."""
        errors, record = validate_packet_data(SAMPLE_SIEVE_PACKET)

        assert len(errors) == 0
        assert record is not None
        assert record["id"] == "pkt_sieve_001"

    def test_missing_id(self):
        """Test validation fails when ID is missing."""
        packet = {**SAMPLE_SYNONYM_PACKET, "id": None}
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any(e.field == "id" for e in errors)
        assert record is None

    def test_missing_subject_id(self):
        """Test validation fails when subject_mondo_id is missing."""
        packet = {
            **SAMPLE_SYNONYM_PACKET,
            "statement": {
                **SAMPLE_SYNONYM_PACKET["statement"],
                "subject_mondo_id": None,
            }
        }
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any("subject_id" in e.field for e in errors)
        assert record is None

    def test_missing_sieve_subject(self):
        """Test validation fails when SIEVE subject is missing."""
        packet = {
            **SAMPLE_SIEVE_PACKET,
            "statement": {
                **SAMPLE_SIEVE_PACKET["statement"],
                "subject": None,
            }
        }
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any("subject_id" in e.field for e in errors)
        assert record is None

    def test_invalid_status(self):
        """Test validation fails with invalid status."""
        packet = {**SAMPLE_SYNONYM_PACKET, "status": "INVALID_STATUS"}
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any(e.field == "status" for e in errors)
        assert record is None

    def test_invalid_evidence_type(self):
        """Test validation fails with invalid evidence type."""
        packet = {
            **SAMPLE_SYNONYM_PACKET,
            "evidence": [{
                "id": "ev_test",
                "evidence_type": "INVALID_TYPE",
                "direction": "SUPPORTS",
            }]
        }
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any("evidence_type" in e.field for e in errors)

    def test_invalid_evidence_direction(self):
        """Test validation fails with invalid evidence direction."""
        packet = {
            **SAMPLE_SYNONYM_PACKET,
            "evidence": [{
                "id": "ev_test",
                "evidence_type": "CONCORDANCE",
                "direction": "INVALID_DIRECTION",
            }]
        }
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any("direction" in e.field for e in errors)

    def test_evidence_strength_out_of_range(self):
        """Test validation fails with evidence strength out of range."""
        packet = {
            **SAMPLE_SYNONYM_PACKET,
            "evidence": [{
                "id": "ev_test",
                "evidence_type": "CONCORDANCE",
                "direction": "SUPPORTS",
                "evidence_strength": 1.5,  # Out of range
            }]
        }
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any("evidence_strength" in e.field for e in errors)

    def test_confidence_out_of_range(self):
        """Test validation fails with confidence out of range."""
        packet = {**SAMPLE_SYNONYM_PACKET, "evidence_score": -2.0}
        errors, record = validate_packet_data(packet)

        assert len(errors) > 0
        assert any(e.field == "confidence" for e in errors)


class TestValidatePacketFile:
    """Tests for validate_packet_file function."""

    def test_valid_file(self):
        """Test validation of a valid YAML file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(SAMPLE_SYNONYM_PACKET, f)
            f.flush()

            result = validate_packet_file(Path(f.name))

            assert result.valid_count == 1
            assert result.invalid_count == 0
            assert len(result.errors) == 0
            assert len(result.validated_records) == 1

    def test_valid_sieve_file(self):
        """Test validation of a valid SIEVE format YAML file."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(SAMPLE_SIEVE_PACKET, f)
            f.flush()

            result = validate_packet_file(Path(f.name))

            assert result.valid_count == 1
            assert result.invalid_count == 0
            assert len(result.errors) == 0
            assert len(result.validated_records) == 1

    def test_invalid_yaml(self):
        """Test validation fails with invalid YAML."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("invalid: yaml: content: [")
            f.flush()

            result = validate_packet_file(Path(f.name))

            assert result.valid_count == 0
            assert result.invalid_count == 1
            assert len(result.errors) > 0
            assert any("parse" in e.message.lower() for e in result.errors)

    def test_list_file_skipped(self):
        """Test that files containing lists are skipped."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump([SAMPLE_SYNONYM_PACKET], f)  # List instead of dict
            f.flush()

            result = validate_packet_file(Path(f.name))

            assert result.valid_count == 0
            assert result.invalid_count == 1
            assert any("list" in e.message.lower() for e in result.errors)


class TestValidatePacketDirectory:
    """Tests for validate_packet_directory function."""

    def test_valid_directory(self):
        """Test validation of a directory with valid packets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create two valid packet files
            for i, packet in enumerate([SAMPLE_SYNONYM_PACKET, SAMPLE_HIERARCHY_PACKET]):
                packet_copy = {**packet, "id": f"pkt_test_{i:03d}"}
                with open(tmppath / f"pkt_{i:03d}.yaml", "w") as f:
                    yaml.dump(packet_copy, f)

            result = validate_packet_directory(tmppath)

            assert result.valid_count == 2
            assert result.invalid_count == 0
            assert len(result.validated_records) == 2

    def test_mixed_format_directory(self):
        """Test validation of a directory with both legacy and SIEVE packets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Legacy packet
            with open(tmppath / "legacy.yaml", "w") as f:
                yaml.dump(SAMPLE_SYNONYM_PACKET, f)

            # SIEVE packet
            with open(tmppath / "sieve.yaml", "w") as f:
                yaml.dump(SAMPLE_SIEVE_PACKET, f)

            result = validate_packet_directory(tmppath)

            assert result.valid_count == 2
            assert result.invalid_count == 0
            assert len(result.validated_records) == 2

    def test_mixed_directory(self):
        """Test validation of a directory with valid and invalid packets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Valid packet
            with open(tmppath / "valid.yaml", "w") as f:
                yaml.dump(SAMPLE_SYNONYM_PACKET, f)

            # Invalid packet (missing required fields)
            invalid_packet = {"id": "invalid", "status": "INVALID"}
            with open(tmppath / "invalid.yaml", "w") as f:
                yaml.dump(invalid_packet, f)

            result = validate_packet_directory(tmppath)

            assert result.valid_count == 1
            assert result.invalid_count == 1

    def test_skips_special_files(self):
        """Test that summary.yaml and curation_records.yaml are skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Valid packet
            with open(tmppath / "valid.yaml", "w") as f:
                yaml.dump(SAMPLE_SYNONYM_PACKET, f)

            # Files that should be skipped
            with open(tmppath / "summary.yaml", "w") as f:
                yaml.dump({"total": 1}, f)
            with open(tmppath / "curation_records.yaml", "w") as f:
                yaml.dump([SAMPLE_SYNONYM_PACKET], f)

            result = validate_packet_directory(tmppath)

            # Only the valid.yaml should be counted
            assert result.valid_count == 1
            assert result.invalid_count == 0


class TestValidationResult:
    """Tests for ValidationResult class."""

    def test_is_valid_property(self):
        """Test is_valid property."""
        result = ValidationResult(valid_count=5, invalid_count=0)
        assert result.is_valid is True

        result = ValidationResult(valid_count=5, invalid_count=1)
        assert result.is_valid is False

    def test_summary_no_errors(self):
        """Test summary with no errors."""
        result = ValidationResult(valid_count=10, invalid_count=0)
        summary = result.summary()

        assert "Valid packets: 10" in summary
        assert "Invalid packets: 0" in summary
        assert "errors" not in summary.lower()

    def test_summary_with_errors(self):
        """Test summary with errors."""
        result = ValidationResult(
            valid_count=8,
            invalid_count=2,
            errors=[
                ValidationError("pkt1", "status", "Invalid status"),
                ValidationError("pkt2", "status", "Invalid status"),
                ValidationError("pkt2", "id", "Missing ID"),
            ]
        )
        summary = result.summary()

        assert "Valid packets: 8" in summary
        assert "Invalid packets: 2" in summary
        assert "Total errors: 3" in summary
        assert "status: 2" in summary
        assert "id: 1" in summary
