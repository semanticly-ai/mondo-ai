"""The web app renders profile packets (dicts) from a JSONL store.

Packets come from the real builder fixtures, plus one packet carrying what enrichment and
synthesis add: a PubMed Publication in ``has_evidence`` and a higher-order LLM line that
nests copies of the other lines.
"""

from copy import deepcopy
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from mondo_ai.packets.evidence import band
from mondo_ai.packets.validate import validate_packet
from mondo_ai.scoring.scorer import score_packet
from mondo_ai.storage.jsonl_store import JSONLPacketStore
from mondo_ai.web.app import create_app, packet_path, packet_view
from tests.test_packet_builder import DATE, _find, built  # noqa: F401  (fixture)

SYNTH_AGENT = "mondoai:agent/synthesis"
SYNTH_METHOD = "mondoai:method/synthesis-prompt"
RATIONALE = "Three independent sources carry the abbreviation; the paper uses it for cystic fibrosis."


def synthesised(packet: dict) -> dict:
    """A copy of ``packet`` with a PubMed hit in has_evidence and an LLM synthesis line."""
    p = deepcopy(packet)
    st = p["statement"]
    pub = {
        "id": "PMID:31000001",
        "type": "Publication",
        "resource": "infores:pubmed",
        "label": "CF in adults",
        "record_metadata": {"retrieved_from": "infores:pubmed", "retrieval_date": DATE},
        "generation_context": [{"code": "sieve:basis_unknown"}],
    }
    st["has_evidence"] = [pub]
    rule = p["reference_snapshot"]["aggregation_rules"][0]
    line_id = st["id"].replace("mondoai:st/", "mondoai:el/") + "-synthesis-v1"
    st["has_evidence_lines"].append(
        {
            "id": line_id,
            "type": "SieveEvidenceLine",
            "description": "LLM synthesis of the evidence lines and literature.",
            "has_evidence_items": [deepcopy(li) for li in st["has_evidence_lines"]] + [deepcopy(pub)],
            "direction_of_evidence_provided": "supports",
            "score_of_evidence_provided": 0.7,
            "strength_of_evidence_provided": band(0.7, rule["strength_bands"]),
            "confidence": 0.85,
            "confidence_basis": "MEASURED",
            "date_attached": DATE,
            "contributions": [
                {
                    "id": f"{line_id}#act",
                    "type": "InterpretationActivity",
                    "contributor": SYNTH_AGENT,
                    "role": "INTERPRETER",
                    "date": DATE,
                    "method_type": "LLM_JUDGEMENT",
                    "specified_by": [SYNTH_METHOD],
                    "read_coverage": "ABSTRACT_ONLY",
                    "access_status": "NOT_RECORDED",
                    "rationale": RATIONALE,
                }
            ],
        }
    )
    snap = p["reference_snapshot"]
    snap["agents"].append(
        {
            "id": SYNTH_AGENT,
            "type": "SieveAgent",
            "subtype": "software",
            "name": "mondo-ai synthesis agent",
            "software_version": "0.0.0",
            "model_name": "test-model",
            "trust_levels": [{"value": "ai-agent", "level": 0.7, "valid_from": "2026-01-01"}],
        }
    )
    snap["methods"].append({"id": SYNTH_METHOD, "type": "SieveMethod", "version": "v1"})
    snap["resources"].append(
        {
            "id": "infores:pubmed",
            "type": "InformationResource",
            "label": "PubMed",
            "curie_prefixes": ["PMID"],
            "default_role": "PRIMARY",
        }
    )
    return score_packet(p)


@pytest.fixture(scope="module")
def synth_packet(built) -> dict:  # noqa: F811
    p = synthesised(_find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF"))
    assert validate_packet(p) == []
    return p


@pytest.fixture(scope="module")
def client(built, synth_packet, tmp_path_factory) -> TestClient:  # noqa: F811
    store = JSONLPacketStore(tmp_path_factory.mktemp("store"))
    others = [p for p in built["merged_list"] if p["id"] != synth_packet["id"]]
    store.add_packets([*others, synth_packet])
    return TestClient(create_app(store.store_dir))


def _get(client: TestClient, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return response.text


# --- routes ------------------------------------------------------------------------------


def test_packet_path_routes_by_the_hash_part():
    assert packet_path("mondoai:pkt/abc123") == "/packets/abc123"
    assert packet_path("mondoai:pkt/abc123-omim") == "/packets/abc123-omim"
    assert packet_path("other:x/y") == "/packets/" + quote("other:x/y", safe="")


def test_detail_resolves_short_and_encoded_ids(client, built):  # noqa: F811
    packet = built["merged_list"][0]
    assert packet["id"] in _get(client, packet_path(packet["id"]))
    assert packet["id"] in _get(client, "/packets/" + quote(packet["id"], safe=""))


def test_unknown_packet_is_404(client):
    response = client.get("/packets/doesnotexist")
    assert response.status_code == 404
    assert "doesnotexist" in response.text


# --- dashboard and list ------------------------------------------------------------------


def test_dashboard_shows_profile_counts(client, built):  # noqa: F811
    html = _get(client, "/")
    for text in ("SynonymStatement", "SubclassStatement", "MappingStatement", "PENDING_REVIEW", "NOT_VERIFIED"):
        assert text in html
    assert "S &gt;= 0.8" in html or "0.4 &lt;= S &lt; 0.8" in html
    assert "infores:omim" in html
    assert f"{len(built['merged_list'])}" in html


def test_list_shows_statement_text_score_and_links(client, synth_packet):
    html = _get(client, "/packets?per_page=200")
    st = synth_packet["statement"]
    assert f'href="{packet_path(synth_packet["id"])}"' in html
    assert "has exact synonym &#39;CF&#39;" in html
    assert f"{st['score']:.2f}" in html
    assert st["strength"] in html


def test_list_filters_by_claim_type_review_state_source_and_score(client, built):  # noqa: F811
    html = _get(client, "/packets?per_page=200&claim_type=SubclassStatement")
    assert "subclass of" in html and "has exact synonym" not in html

    html = _get(client, "/packets?per_page=200&review_state=NOT_VERIFIED&claim_type=SynonymStatement")
    assert "mucoviscidosis" in html and "&#39;CF&#39;" not in html

    html = _get(client, "/packets?per_page=200&source=infores:icd10cm")
    assert "pancreatic fibrosis" not in html

    html = _get(client, "/packets?per_page=200&max_score=0")
    assert "DOID:9" in html and "ICD10CM:E84 " in html and "2 results" in html

    html = _get(client, "/packets?per_page=200&q=pancreatic")
    assert "pancreatic fibrosis" in html and "1 results" in html


# --- detail ------------------------------------------------------------------------------


def test_detail_heading_and_statement(client, synth_packet):
    st = synth_packet["statement"]
    html = _get(client, packet_path(synth_packet["id"]))
    assert "<h1" in html and "has exact synonym &#39;CF&#39;" in html.split("<h1", 1)[1].split("</h1>")[0]
    for text in ("MONDO:1", "has exact synonym", "oboInOwl:hasExactSynonym", "PENDING_REVIEW", st["strength"]):
        assert text in html
    assert f"{st['score']:.3f}" in html


def test_detail_renders_prior_claims_with_record_and_resource(client, synth_packet):
    html = _get(client, packet_path(synth_packet["id"]))
    assert "OMIM:219700" in html and "cystic fibrosis" in html
    assert "infores:omim" in html
    assert "RULE_BASED_STRENGTH" in html
    assert "mondoai:agent/concordance-pipeline" in html
    assert "0.950" in html  # the OMIM line's score


def test_detail_renders_synthesis_line_once_with_links(client, synth_packet):
    html = _get(client, packet_path(synth_packet["id"]))
    assert RATIONALE in html
    assert "0.85" in html  # confidence
    assert "LLM_JUDGEMENT" in html
    lines = synth_packet["statement"]["has_evidence_lines"]
    for line in lines[:-1]:
        anchor = line["id"].rsplit("/", 1)[-1]
        assert f'id="line-{anchor}"' in html
        assert f'href="#line-{anchor}"' in html
        # descriptions are rendered once: the nested copy is a link, not a second rendering
        assert html.count(f">{line['description']}</div>") == 1


def test_detail_renders_publications_as_pubmed_links(client, synth_packet):
    html = _get(client, packet_path(synth_packet["id"]))
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/31000001/"' in html
    assert "CF in adults" in html


def test_detail_renders_computational_score(client, built):  # noqa: F811
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "OMIM:219700")
    html = _get(client, packet_path(packet["id"]))
    assert "ComputationalScore" in html
    assert "match=label_label" in html  # parameters
    assert "mondoai:method/lexical-match" in html


def test_detail_renders_reference_snapshot(client, synth_packet):
    html = _get(client, packet_path(synth_packet["id"]))
    assert "<details" in html
    assert "Orphanet Rare Disease Ontology" in html
    assert "mondoai:rule/synonym-v1.0" in html
    assert "strong &gt;= 0.8" in html


def test_packet_view_heading_falls_back_to_subject(built):  # noqa: F811
    packet = deepcopy(built["merged_list"][0])
    del packet["statement"]["statement_text"]
    assert packet_view(packet)["heading"] == packet["statement"]["subject"]


def test_api_stats(client, built):  # noqa: F811
    data = client.get("/api/stats").json()
    assert data["total_packets"] == len(built["merged_list"])
    assert "SynonymStatement" in data["by_claim_type"]
