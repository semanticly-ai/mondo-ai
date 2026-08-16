"""The web app renders full packet metadata (not silently-empty fields).

The models are snake_case with ``extra="forbid"``. If a template references a
stale camelCase attribute (e.g. ``subjectLabel``), Jinja resolves it to Undefined
and renders nothing -- the page loads but shows no metadata. These tests guard
against that by asserting concrete field values appear in the rendered HTML.
"""

from mondo_ai.datamodel.loaders import packet_from_dict
from mondo_ai.web.app import templates


def _rich_packet_dict() -> dict:
    """A packet exercising statement, synthesis, and a Concordance evidence item."""
    return {
        "id": "pkt_web_test",
        "status": "UNREVIEWED",
        "created": "2026-08-15",
        "statement": {
            "id": "stmt_1",
            "type": "SieveStatement",
            "statement_text": "asthma has exact synonym 'bronchial asthma'",
            "subject": "MONDO:0004979",
            "subject_label": "asthma",
            "object": "bronchial asthma",
            "object_label": "bronchial asthma",
            "predicate": {
                "code": "oio:hasExactSynonym",
                "label": "exact synonym",
                "system": "http://www.geneontology.org/formats/oboInOwl#",
            },
        },
        "has_evidence_lines": [
            {
                "id": "el_1",
                "type": "SieveEvidenceLine",
                "direction_of_evidence_provided": "supports",
                "strength_of_evidence_provided": "strong",
                "score_of_evidence_provided": 0.95,
                "has_evidence_items": [
                    {
                        "id": "ev_1",
                        "type": "ConcordanceItem",
                        "evidence_item_type": "ConcordanceItem",
                        "source_name": "OMIM",
                        "source_id": "omim",
                        "source_subject": "OMIM:600807",
                        "source_subject_label": "asthma",
                        "mapping_justification": "skos:exactMatch",
                    }
                ],
            }
        ],
        "evidence_synthesis": {
            "summary": "Scored by deterministic evidence scorer",
            "direction": "supports",
            "score": {"value": 1.0, "description": "Net Evidence Ratio (NER)"},
        },
    }


def test_packet_detail_renders_all_metadata() -> None:
    """Every metadata field on a rich packet appears in the rendered detail HTML."""
    packet = packet_from_dict(_rich_packet_dict())
    html = templates.get_template("packet_detail.html").render(packet=packet)

    # Statement metadata
    assert "asthma" in html
    assert "bronchial asthma" in html
    assert "MONDO:0004979" in html
    assert "exact synonym" in html
    assert "asthma has exact synonym" in html  # statement_text (apostrophes escaped)

    # Evidence line metadata
    assert "supports" in html
    assert "strong" in html
    assert "0.950" in html  # score_of_evidence_provided formatted

    # Concordance evidence-item metadata
    assert "OMIM" in html
    assert "OMIM:600807" in html
    assert "skos:exactMatch" in html  # mapping_justification


def test_packet_detail_shows_subject_label_as_heading() -> None:
    """The <h1> uses subject_label, not the generic fallback."""
    packet = packet_from_dict(_rich_packet_dict())
    html = templates.get_template("packet_detail.html").render(packet=packet)
    # The fallback heading must NOT be used when a subject_label is present.
    assert "Evidence Packet" not in html
