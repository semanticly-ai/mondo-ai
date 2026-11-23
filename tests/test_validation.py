"""Tests for the packet validation report layer (validation/validator.py)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from mondo_ai.storage.jsonl_store import JSONLPacketStore
from mondo_ai.validation import (
    ValidationError,
    ValidationResult,
    validate_packet_data,
    validate_packet_directory,
    validate_packet_file,
    validate_store,
)

EXAMPLES = Path(__file__).parent / "data" / "sieve_example_packets.json"


@pytest.fixture
def packets() -> list[dict[str, Any]]:
    """Three real pipeline packets: one synonym, one subclass, one mapping claim."""
    return json.loads(EXAMPLES.read_text())


def _broken(packet: dict[str, Any]) -> dict[str, Any]:
    """A copy of the packet whose line contributor is missing from its snapshot (a profile ERROR)."""
    bad = copy.deepcopy(packet)
    bad["reference_snapshot"]["agents"] = []
    return bad


def _with_unrecorded_read(packet: dict[str, Any]) -> dict[str, Any]:
    """A copy with a publication in its line and read coverage NOT_RECORDED (a WARNING)."""
    out = copy.deepcopy(packet)
    line = out["statement"]["has_evidence_lines"][0]
    line["has_evidence_items"].append({"id": "PMID:123", "type": "Publication", "resource": "infores:pubmed"})
    for act in line["contributions"]:
        act["read_coverage"] = "NOT_RECORDED"
        act["access_status"] = "NOT_RECORDED"
    resources = out["reference_snapshot"]["resources"]
    resources.append(
        {"id": "infores:pubmed", "type": "InformationResource", "label": "PubMed", "curie_prefixes": ["PMID"], "default_role": "PRIMARY"}
    )
    return out


class TestValidatePacketData:
    def test_valid_packets(self, packets):
        for packet in packets:
            result = validate_packet_data(packet)
            assert result.is_valid, result.errors
            assert (result.valid_count, result.invalid_count) == (1, 0)
            assert result.errors == [] and result.warnings == []

    def test_schema_error_is_reported(self, packets):
        bad = copy.deepcopy(packets[0])
        bad["reference_snapshot"]["resources"][0]["default_role"] = "BOGUS"
        result = validate_packet_data(bad)
        assert not result.is_valid
        assert "SCHEMA" in {e.code for e in result.errors}

    def test_profile_error_is_reported_with_its_code(self, packets):
        result = validate_packet_data(_broken(packets[0]))
        assert not result.is_valid
        assert result.invalid_count == 1
        assert all(isinstance(e, ValidationError) for e in result.errors)
        assert all(e.packet_id == packets[0]["id"] for e in result.errors)
        assert "UNRESOLVED_REFERENCE" in {e.code for e in result.errors}

    def test_warning_does_not_invalidate(self, packets):
        result = validate_packet_data(_with_unrecorded_read(packets[0]))
        assert result.is_valid, result.errors
        assert [w.code for w in result.warnings] == ["READ_COVERAGE_NOT_RECORDED"]
        assert result.warnings[0].packet_id == packets[0]["id"]


class TestSummary:
    def test_counts_errors_and_warnings_by_code(self, packets):
        result = ValidationResult()
        for p in [packets[0], _with_unrecorded_read(packets[1]), _broken(packets[2])]:
            result.merge(validate_packet_data(p))
        assert (result.valid_count, result.invalid_count) == (2, 1)
        text = result.summary()
        assert "Valid packets: 2" in text
        assert "Invalid packets: 1" in text
        assert "Warnings: 1" in text
        assert "READ_COVERAGE_NOT_RECORDED: 1" in text

    def test_clean_summary(self, packets):
        text = validate_packet_data(packets[0]).summary()
        assert "Valid packets: 1" in text
        assert "Errors: 0" in text
        assert "Warnings: 0" in text


class TestValidatePacketFile:
    def test_yaml_packet(self, packets, tmp_path):
        path = tmp_path / "packet.yaml"
        path.write_text(yaml.safe_dump(packets[0], sort_keys=False))
        result = validate_packet_file(path)
        assert result.is_valid and result.valid_count == 1

    def test_json_list_of_packets(self, packets, tmp_path):
        path = tmp_path / "packets.json"
        path.write_text(json.dumps([packets[0], _broken(packets[1])]))
        result = validate_packet_file(path)
        assert (result.valid_count, result.invalid_count) == (1, 1)

    def test_jsonl_file(self, packets, tmp_path):
        path = tmp_path / "packets_000.jsonl"
        path.write_text("\n".join(json.dumps(p) for p in packets) + "\n")
        assert validate_packet_file(path).valid_count == 3

    def test_unparseable_file(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("id: [unclosed")
        result = validate_packet_file(path)
        assert result.invalid_count == 1
        assert result.errors[0].code == "PARSE"
        assert result.errors[0].packet_id == "bad"

    def test_not_a_packet(self, tmp_path):
        path = tmp_path / "scalar.yaml"
        path.write_text("just a string\n")
        result = validate_packet_file(path)
        assert result.invalid_count == 1
        assert result.errors[0].code == "PARSE"


class TestValidatePacketDirectory:
    def test_validates_yaml_and_json_files(self, packets, tmp_path):
        (tmp_path / "a.yaml").write_text(yaml.safe_dump(packets[0]))
        (tmp_path / "b.json").write_text(json.dumps(_broken(packets[1])))
        (tmp_path / "notes.txt").write_text("ignored")
        result = validate_packet_directory(tmp_path)
        assert (result.valid_count, result.invalid_count) == (1, 1)

    def test_skips_summary_files(self, packets, tmp_path):
        (tmp_path / "a.yaml").write_text(yaml.safe_dump(packets[0]))
        (tmp_path / "summary.yaml").write_text(yaml.safe_dump({"total": 1}))
        assert validate_packet_directory(tmp_path).valid_count == 1

    def test_empty_directory(self, tmp_path):
        result = validate_packet_directory(tmp_path)
        assert (result.valid_count, result.invalid_count) == (0, 0)


class TestValidateStore:
    def _store(self, tmp_path: Path, packets: list[dict[str, Any]]) -> Path:
        store = JSONLPacketStore(tmp_path / "store")
        store.add_packets(packets)
        return store.store_dir

    def test_whole_store(self, packets, tmp_path):
        store_dir = self._store(tmp_path, packets[:2] + [_broken(packets[2])])
        result = validate_store(store_dir)
        assert (result.valid_count, result.invalid_count) == (2, 1)

    def test_sample(self, packets, tmp_path):
        store_dir = self._store(tmp_path, packets)
        result = validate_store(store_dir, sample=2)
        assert result.valid_count + result.invalid_count == 2

    def test_sample_is_deterministic(self, packets, tmp_path):
        store_dir = self._store(tmp_path, packets[:2] + [_broken(packets[2])])
        first = validate_store(store_dir, sample=1, seed=7)
        second = validate_store(store_dir, sample=1, seed=7)
        assert first.checked_ids == second.checked_ids

    def test_sample_larger_than_store(self, packets, tmp_path):
        store_dir = self._store(tmp_path, packets)
        assert validate_store(store_dir, sample=10).valid_count == 3

    def test_missing_store(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            validate_store(tmp_path / "nope")
