"""TextMiningResult (the SIEVE 'C' shape) validates and round-trips.

TextMiningResult is mondo-ai's overlay evidence item for text-mining / LLM
extraction output. It is_a SieveDataItem, so it inherits ``value`` (the span
text) and the CuratedEvidence slots, and adds optional extraction provenance.
Because every extraction slot is optional, an item with only ``value`` is
indistinguishable from a plain evidence item -- the "A stays valid" property.
"""

from mondo_ai.datamodel import TextMiningResult
from mondo_ai.datamodel.loaders import packet_from_dict


def _packet_dict(item: dict) -> dict:
    return {
        "id": "pkt_tmr_test",
        "status": "UNREVIEWED",
        "statement": {
            "id": "stmt_1",
            "type": "SieveStatement",
            "subject": "MONDO:0005015",
            "object": "MONDO:0005151",
        },
        "has_evidence_lines": [
            {
                "id": "el_1",
                "type": "SieveEvidenceLine",
                "direction_of_evidence_provided": "supports",
                "has_evidence_items": [item],
            }
        ],
    }


def test_a_shape_only_value_validates() -> None:
    """An item with only `value` is still valid (A stays valid)."""
    item = {
        "id": "ev_1",
        "type": "TextMiningResult",
        "value": "asthma is a respiratory disease",
    }
    packet = packet_from_dict(_packet_dict(item))
    assert packet.has_evidence_lines is not None
    items = packet.has_evidence_lines[0].has_evidence_items
    assert items is not None
    got = items[0]
    assert isinstance(got, TextMiningResult)
    assert got.value == "asthma is a respiratory disease"
    assert got.extraction_score is None


def test_c_shape_full_extraction_provenance_round_trips() -> None:
    """A full C item keeps its extraction slots through load."""
    item = {
        "id": "ev_2",
        "type": "TextMiningResult",
        "value": "asthma is a respiratory disease",
        "extraction_score": 0.91,
        "document_section": "Abstract",
        "text_location": "sentence 3",
        "extraction_method": "claude-opus-4-8",
    }
    packet = packet_from_dict(_packet_dict(item))
    assert packet.has_evidence_lines is not None
    items = packet.has_evidence_lines[0].has_evidence_items
    assert items is not None
    got = items[0]
    assert isinstance(got, TextMiningResult)
    assert got.extraction_score == 0.91
    assert got.document_section == "Abstract"
    assert got.text_location == "sentence 3"
    assert got.extraction_method == "claude-opus-4-8"


def test_direct_construction() -> None:
    tmr = TextMiningResult(
        id="ev_3", type="TextMiningResult", value="x", extraction_score=0.5
    )
    assert tmr.value == "x"
    assert tmr.extraction_score == 0.5
