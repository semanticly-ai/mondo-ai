"""Tests for the AI synthesis agent on SIEVE profile packets.

The agent reads a packet's lines and attached Publications and records its judgement as
ONE higher-order evidence line on the claim. No real API is called: the LLM is
pydantic-ai's TestModel with a fixed structured output.
"""

import copy
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from mondo_ai import __version__
from mondo_ai.agents.prompts import (
    PROMPT_VERSION,
    SYNTHESIS_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
    build_synthesis_prompt,
    format_evidence_section,
    format_publications,
)
from mondo_ai.agents.synthesis import (
    SYNTHESIS_AGENT,
    SYNTHESIS_METHOD,
    SynthesisAgent,
    SynthesisResponse,
    apply_synthesis,
    synthesis_line_id,
)
from mondo_ai.enrichment.enricher import paper_to_publication
from mondo_ai.enrichment.pubmed import ReferenceCache
from mondo_ai.packets.registry import PUBMED
from mondo_ai.scoring.scorer import explain
from tests.test_enrichment import DATE, assert_valid, built_packets, find, make_paper

MODEL = "test"


@pytest.fixture
def packets(tmp_path_factory) -> list[dict]:
    """The packets of the tiny build in tests/test_enrichment.py."""
    return built_packets(tmp_path_factory)


def _with_literature(packet: dict, *pmids: str) -> dict:
    """Attach Publications as the enricher does."""
    packet["statement"]["has_evidence"] = [
        paper_to_publication(make_paper(p, f"Paper {p}"), DATE) for p in pmids
    ]
    packet["reference_snapshot"].setdefault("resources", []).append(
        {
            "id": PUBMED,
            "type": "InformationResource",
            "label": "PubMed",
            "curie_prefixes": ["PMID"],
            "default_role": "PRIMARY",
        }
    )
    return packet


def _response(**kw) -> SynthesisResponse:
    fields = dict(
        direction="supports",
        strength=0.9,
        confidence=0.8,
        rationale="Two sources agree.",
        cited_ids=[],
    )
    fields.update(kw)
    return SynthesisResponse(**fields)  # type: ignore[arg-type]


def _agent(response: SynthesisResponse, **kw) -> SynthesisAgent:
    return SynthesisAgent(
        llm=TestModel(custom_output_args=response.model_dump()), date=DATE, **kw
    )


def _synthesis_lines(packet: dict) -> list[dict]:
    return [
        line
        for line in packet["statement"]["has_evidence_lines"]
        if line["contributions"][0]["contributor"] == SYNTHESIS_AGENT
    ]


# --- prompts ---------------------------------------------------------------------------


class TestPrompts:
    def test_system_prompt_asks_for_a_judgement_not_an_action(self):
        assert "ontology curator" in SYSTEM_PROMPT.lower()
        for action in ("ADD", "CONFIRM", "REJECT"):
            assert action not in SYSTEM_PROMPT

    def test_template_placeholders(self):
        for placeholder in (
            "{statement_text}",
            "{subject}",
            "{predicate}",
            "{object_value}",
            "{evidence_section}",
            "{publication_section}",
            "{score}",
        ):
            assert placeholder in SYNTHESIS_PROMPT_TEMPLATE

    def test_empty_sections(self):
        assert format_evidence_section([]) == "*No evidence lines*"
        assert format_publications([]) == "*No publications*"

    def test_line_formatting(self, packets):
        line = find(packets, "SynonymStatement", "mucoviscidosis")["statement"][
            "has_evidence_lines"
        ][0]
        text = format_evidence_section([line])
        assert line["id"] in text
        assert "supports" in text
        assert line["strength_of_evidence_provided"] in text
        assert "infores:disease-ontology" in text  # the item's resource
        assert "DOID:1485 has exact synonym mucoviscidosis" in text  # the prior claim
        assert "cystic fibrosis" in text  # the record label

    def test_mapping_line_formatting(self, packets):
        lines = find(packets, "MappingStatement", "DOID:1485")["statement"][
            "has_evidence_lines"
        ]
        text = format_evidence_section(lines)
        assert "ComputationalScore" in text
        assert "ExternalRecord DOID:1485" in text

    def test_publication_formatting_with_cached_abstract(self):
        pub = paper_to_publication(make_paper("5", "On CF"), DATE)
        text = format_publications([pub], {"PMID:5": "An abstract about CF."})
        assert "PMID:5" in text
        assert "On CF" in text
        assert "An abstract about CF." in text
        assert "No abstract" in format_publications([pub])

    def test_build_prompt(self, packets):
        packet = _with_literature(
            find(packets, "SynonymStatement", "mucoviscidosis"), "5"
        )
        prompt = build_synthesis_prompt(packet)
        assert packet["statement"]["statement_text"] in prompt
        assert "MONDO:1" in prompt
        assert "has exact synonym" in prompt
        assert "mucoviscidosis" in prompt
        assert packet["statement"]["has_evidence_lines"][0]["id"] in prompt
        assert "PMID:5" in prompt
        assert str(packet["statement"]["score"]) in prompt


# --- structured output -----------------------------------------------------------------


class TestSynthesisResponse:
    def test_valid(self):
        response = _response(cited_ids=["a"])
        assert response.direction == "supports"
        assert response.cited_ids == ["a"]

    def test_bounds_and_direction(self):
        with pytest.raises(ValidationError):
            _response(strength=1.5)
        with pytest.raises(ValidationError):
            _response(confidence=-0.1)
        with pytest.raises(ValidationError):
            _response(direction="neutral")

    def test_no_recommended_action(self):
        assert "action" not in SynthesisResponse.model_fields


# --- the synthesis line ----------------------------------------------------------------


class TestApplySynthesis:
    def test_one_higher_order_line(self, packets):
        packet = _with_literature(
            find(packets, "SynonymStatement", "mucoviscidosis"), "5", "6"
        )
        before = copy.deepcopy(packet["statement"]["has_evidence_lines"])
        apply_synthesis(
            packet,
            _response(cited_ids=[before[0]["id"], "PMID:5"]),
            model=MODEL,
            date=DATE,
            abstracts_read=True,
        )

        lines = packet["statement"]["has_evidence_lines"]
        assert lines[: len(before)] == before
        assert len(lines) == len(before) + 1
        line = lines[-1]
        assert line["id"] == synthesis_line_id(packet["id"], PROMPT_VERSION, MODEL)
        assert (
            line["has_evidence_items"] == before + packet["statement"]["has_evidence"]
        )
        assert line["direction_of_evidence_provided"] == "supports"
        assert line["score_of_evidence_provided"] == 0.9
        assert line["strength_of_evidence_provided"] == "strong"
        assert "computed_by_rule" not in line
        assert line["confidence"] == 0.8
        assert line["confidence_basis"] == "MEASURED"
        assert (
            before[0]["id"] in line["description"] and "PMID:5" in line["description"]
        )
        assert line["contributions"] == [
            {
                "id": f"{line['id']}#act",
                "type": "InterpretationActivity",
                "contributor": SYNTHESIS_AGENT,
                "role": "INTERPRETER",
                "date": DATE,
                "method_type": "LLM_JUDGEMENT",
                "specified_by": [SYNTHESIS_METHOD],
                "read_coverage": "ABSTRACT_ONLY",
                "access_status": "NOT_RECORDED",
                "identifier_verified": "VERIFIED",
                "rationale": "Two sources agree.",
            }
        ]
        assert_valid(packet)

    def test_registry_entries_in_snapshot(self, packets):
        packet = find(packets, "SubclassStatement", "MONDO:2")
        apply_synthesis(
            packet, _response(), model=MODEL, date=DATE, abstracts_read=False
        )
        snapshot = packet["reference_snapshot"]
        agent = next(a for a in snapshot["agents"] if a["id"] == SYNTHESIS_AGENT)
        assert agent == {
            "id": SYNTHESIS_AGENT,
            "type": "SieveAgent",
            "subtype": "software",
            "name": agent["name"],
            "software_version": __version__,
            "model_name": MODEL,
            "trust_levels": [
                {"value": "ai-agent", "level": 0.7, "valid_from": "2026-01-01"}
            ],
        }
        method = next(m for m in snapshot["methods"] if m["id"] == SYNTHESIS_METHOD)
        assert method["type"] == "SieveMethod" and method["version"] == PROMPT_VERSION
        assert_valid(packet)

    def test_structured_record_coverage_without_publications(self, packets):
        packet = find(packets, "SubclassStatement", "MONDO:2")
        apply_synthesis(
            packet, _response(), model=MODEL, date=DATE, abstracts_read=False
        )
        assert (
            _synthesis_lines(packet)[0]["contributions"][0]["read_coverage"]
            == "STRUCTURED_RECORD"
        )

    def test_title_only_coverage_when_no_abstract_was_read(self, packets):
        packet = _with_literature(
            find(packets, "SynonymStatement", "mucoviscidosis"), "5"
        )
        apply_synthesis(
            packet, _response(), model=MODEL, date=DATE, abstracts_read=False
        )
        assert (
            _synthesis_lines(packet)[0]["contributions"][0]["read_coverage"]
            == "TITLE_ONLY"
        )

    def test_rescored(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        score = packet["statement"]["score"]
        apply_synthesis(
            packet,
            _response(direction="disputes", strength=0.9, confidence=0.9),
            model=MODEL,
            date=DATE,
            abstracts_read=False,
        )
        assert packet["statement"]["score"] < score
        assert packet["statement"]["score"] == pytest.approx(
            explain(packet).s, abs=1e-4
        )
        assert_valid(packet)

    def test_resynthesis_replaces_the_line(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        n = len(packet["statement"]["has_evidence_lines"])
        apply_synthesis(
            packet,
            _response(strength=0.5),
            model=MODEL,
            date=DATE,
            abstracts_read=False,
        )
        apply_synthesis(
            packet,
            _response(strength=0.3),
            model=MODEL,
            date=DATE,
            abstracts_read=False,
        )
        lines = packet["statement"]["has_evidence_lines"]
        assert len(lines) == n + 1
        assert lines[-1]["score_of_evidence_provided"] == 0.3
        assert_valid(packet)

    def test_synthesis_does_not_read_other_synthesis_lines(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        apply_synthesis(
            packet, _response(), model="model-a", date=DATE, abstracts_read=False
        )
        apply_synthesis(
            packet, _response(), model="model-b", date=DATE, abstracts_read=False
        )
        line_b = packet["statement"]["has_evidence_lines"][-1]
        assert all(
            i["contributions"][0]["contributor"] != SYNTHESIS_AGENT
            for i in line_b["has_evidence_items"]
        )
        assert_valid(packet)

    def test_uncited_ids_are_dropped_and_flagged(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        apply_synthesis(
            packet,
            _response(cited_ids=["PMID:404"]),
            model=MODEL,
            date=DATE,
            abstracts_read=False,
        )
        line = _synthesis_lines(packet)[0]
        assert "PMID:404" not in line["description"]
        assert line["contributions"][0]["identifier_verified"] == "FAILED"

    def test_mapping_claim(self, packets):
        packet = find(packets, "MappingStatement", "DOID:1485")
        apply_synthesis(
            packet, _response(), model=MODEL, date=DATE, abstracts_read=False
        )
        assert len(_synthesis_lines(packet)) == 1
        assert_valid(packet)

    def test_nothing_to_read(self, packets):
        packet = find(packets, "SynonymStatement", "mucoviscidosis")
        packet["statement"]["has_evidence_lines"] = []
        before = copy.deepcopy(packet)
        assert (
            apply_synthesis(
                packet, _response(), model=MODEL, date=DATE, abstracts_read=False
            )
            == before
        )


# --- the agent -------------------------------------------------------------------------


class TestSynthesisAgentInit:
    @pytest.fixture(autouse=True)
    def _no_keys(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def test_anthropic_without_key(self):
        with pytest.raises(ValueError, match="No API key found"):
            SynthesisAgent(provider="anthropic")

    def test_openai_without_key(self):
        with pytest.raises(ValueError, match="No API key found"):
            SynthesisAgent(provider="openai")

    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            SynthesisAgent(provider="unknown")

    def test_provider_model_string(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with patch("pydantic_ai.Agent") as agent_cls:
            agent = SynthesisAgent(provider="anthropic", model="claude-x")
        assert agent.model == "claude-x"
        assert agent_cls.call_args.args[0] == "anthropic:claude-x"

    def test_llm_instance_needs_no_key(self):
        assert SynthesisAgent(llm=TestModel()).model == "test"


class TestSynthesisAgent:
    def test_synthesize(self, packets):
        packet = _with_literature(
            find(packets, "SynonymStatement", "mucoviscidosis"), "5"
        )
        line_id = packet["statement"]["has_evidence_lines"][0]["id"]
        out = _agent(_response(cited_ids=[line_id])).synthesize(packet)
        assert out is packet
        line = _synthesis_lines(packet)[0]
        assert line["id"] == synthesis_line_id(packet["id"], PROMPT_VERSION, "test")
        assert line["contributions"][0]["identifier_verified"] == "VERIFIED"
        assert line["contributions"][0]["read_coverage"] == "TITLE_ONLY"
        assert_valid(packet)

    def test_cached_abstracts_are_read(self, packets, tmp_path):
        cache = ReferenceCache(tmp_path)
        cache.put(make_paper("5", "Paper 5"))
        packet = _with_literature(
            find(packets, "SynonymStatement", "mucoviscidosis"), "5"
        )
        _agent(_response(), reference_cache=cache).synthesize(packet)
        assert (
            _synthesis_lines(packet)[0]["contributions"][0]["read_coverage"]
            == "ABSTRACT_ONLY"
        )
        assert_valid(packet)

    @pytest.mark.asyncio
    async def test_synthesize_async(self, packets):
        packet = find(packets, "SubclassStatement", "MONDO:2")
        await _agent(_response()).synthesize_async(packet)
        assert len(_synthesis_lines(packet)) == 1
        assert_valid(packet)

    def test_synthesize_packets(self, packets):
        out = _agent(_response()).synthesize_packets(packets)
        assert out == packets
        for packet in out:
            assert len(_synthesis_lines(packet)) == 1
            assert_valid(packet)
