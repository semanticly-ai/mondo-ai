"""Contract between mondo-ai and the SIEVE profile it vendors.

The profile schema files are copied verbatim from the sieve repository. These tests keep
the copies identical to sieve's (when a checkout sits next to this one), and check that
sieve's own example packets load, validate and score the same way here.
"""

from pathlib import Path

import pytest
import yaml
from sieve.inclusion_score import score_packet

from mondo_ai.datamodel import ResourceSnapshot, SynonymStatement, packet_model
from mondo_ai.packets.validate import validate_packet

SCHEMA_DIR = Path(__file__).parent.parent / "src" / "mondo_ai" / "schema"
SIEVE = Path(__file__).parent.parent.parent / "sieve"
EXAMPLES = SIEVE / "tests" / "data" / "profile" / "valid"


@pytest.mark.skipif(not SIEVE.exists(), reason="no sieve checkout next to mondo-ai")
@pytest.mark.parametrize("name", ["sieve.yaml", "sepio_core.yaml"])
def test_vendored_schema_is_identical_to_sieve(name: str) -> None:
    assert (SCHEMA_DIR / name).read_bytes() == (SIEVE / "schema" / name).read_bytes()


def test_overlay_imports_the_profile() -> None:
    overlay = yaml.safe_load((SCHEMA_DIR / "mondo_ai_sieve.yaml").read_text())
    assert overlay["imports"] == ["linkml:types", "sieve"]
    assert set(overlay["classes"]) == {"ResourceSnapshot", "EntitySnapshot", "Synonym", "EntityMapping"}


def test_generated_model_has_profile_and_snapshot_classes() -> None:
    assert "subject" in SynonymStatement.model_fields
    assert "entities" in ResourceSnapshot.model_fields


@pytest.mark.skipif(not EXAMPLES.exists(), reason="no sieve checkout next to mondo-ai")
@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.yaml")) if EXAMPLES.exists() else [], ids=lambda p: p.stem)
def test_sieve_examples_are_valid_here(path: Path) -> None:
    packet = yaml.safe_load(path.read_text())
    assert validate_packet(packet) == []
    model = packet_model(packet)
    assert type(model.statement).__name__ == packet["statement"]["type"]
    assert -1 <= score_packet(packet).s <= 1
