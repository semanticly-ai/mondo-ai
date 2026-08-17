"""The static exporter dumps the packet store to a JSON payload for the docs browser.

The mkdocs "Evidence Browser" page loads this JSON client-side, so the browser
ships as static files on GitHub Pages. The payload must carry enough per-packet
metadata to render both the list and the expandable evidence detail.
"""

import json
from pathlib import Path

from mondo_ai.datamodel import EvidencePacket
from mondo_ai.datamodel.loaders import packet_from_dict
from mondo_ai.storage.jsonl_store import JSONLPacketStore
from mondo_ai.web.static_export import export_web_data


def _packet(pid: str, subject: str, value: str) -> EvidencePacket:
    return packet_from_dict(
        {
            "id": pid,
            "status": "UNREVIEWED",
            "statement": {
                "id": "s_" + pid,
                "type": "SieveStatement",
                "subject": subject,
                "subject_label": "asthma",
                "object": value,
                "object_label": value,
                "predicate": {"code": "oio:hasExactSynonym", "label": "exact synonym"},
            },
            "has_evidence_lines": [
                {
                    "id": "el_" + pid,
                    "type": "SieveEvidenceLine",
                    "direction_of_evidence_provided": "supports",
                    "strength_of_evidence_provided": "strong",
                    "score_of_evidence_provided": 0.95,
                    "has_evidence_items": [
                        {
                            "id": "ev_" + pid,
                            "type": "ConcordanceItem",
                            "source_name": "OMIM",
                            "source_id": "omim",
                            "source_subject": "OMIM:1",
                            "mapping_justification": "skos:exactMatch",
                        }
                    ],
                }
            ],
        }
    )


def test_export_web_data(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    store = JSONLPacketStore(store_dir)
    store.add_packets(
        [_packet("pkt_1", "MONDO:1", "bronchial asthma"), _packet("pkt_2", "MONDO:2", "AD")]
    )
    out = tmp_path / "docs" / "evidence" / "data.json"

    n = export_web_data(store_dir, out)

    assert n == 2
    assert out.exists()
    payload = json.loads(out.read_text())

    # Stats block
    assert payload["stats"]["total_packets"] == 2
    assert payload["stats"]["by_source"] == {"omim": 2}
    assert payload["stats"]["by_predicate"].get("exact synonym") == 2

    # Packet records with detail
    ids = {p["id"] for p in payload["packets"]}
    assert ids == {"pkt_1", "pkt_2"}
    p1 = next(p for p in payload["packets"] if p["id"] == "pkt_1")
    assert p1["subject"] == "MONDO:1"
    assert p1["subject_label"] == "asthma"
    assert p1["predicate"] == "exact synonym"
    assert p1["object"] == "bronchial asthma"
    assert p1["sources"] == ["omim"]
    assert p1["score"] is not None
    ev = p1["evidence"][0]
    assert ev["source"] == "OMIM"
    assert ev["direction"] == "supports"
    assert ev["strength"] == "strong"
    assert ev["justification"] == "skos:exactMatch"
