"""AI synthesis: an LLM's judgement of a packet's evidence, as one higher-order line.

The agent reads the claim, its evidence lines and the Publications attached to it, and
returns a direction, a strength, its confidence and a short rationale. That judgement is
recorded as ONE ``SieveEvidenceLine`` on the claim whose items are full copies of what it
read (the lines and the Publications), interpreted by an ``LLM_JUDGEMENT`` act of the
synthesis agent under the versioned synthesis prompt. The packet is then re-scored: the
line counts like any judgement line (q = the agent's trust, c = its confidence, its
strength capped by the lines it rests on).

The agent does not accept or reject anything. A curator signs off.

Example:
    >>> from mondo_ai.agents import SynthesisAgent
    >>> agent = SynthesisAgent()  # doctest: +SKIP
    >>> packet = agent.synthesize(packet)  # doctest: +SKIP
"""

import copy
import logging
import os
from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, Field

from mondo_ai import __version__
from mondo_ai.agents.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_synthesis_prompt,
)
from mondo_ai.enrichment.enricher import add_registry_entry, publications_permitted
from mondo_ai.enrichment.pubmed import ReferenceCache
from mondo_ai.packets.evidence import band, digest
from mondo_ai.scoring.scorer import score_packet

logger = logging.getLogger(__name__)

SYNTHESIS_AGENT = "mondoai:agent/synthesis"
SYNTHESIS_METHOD = "mondoai:method/synthesis-prompt"
AI_TRUST = [{"value": "ai-agent", "level": 0.7, "valid_from": "2026-01-01"}]

DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-20250514", "openai": "gpt-4o"}
API_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class SynthesisResponse(BaseModel):
    """The model's structured judgement of a packet's evidence."""

    direction: Literal["supports", "disputes"] = Field(
        description="Whether the evidence supports or disputes the claim."
    )
    strength: float = Field(
        ge=0, le=1, description="How strongly the evidence bears on the claim, 0-1."
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="Confidence that the evidence was read and linked correctly, 0-1.",
    )
    rationale: str = Field(description="One or two sentences.")
    cited_ids: list[str] = Field(
        default_factory=list, description="Ids of the lines and publications relied on."
    )


def synthesis_line_id(packet_id: str, prompt_version: str, model: str) -> str:
    """The id of the synthesis line for a packet, prompt version and model.

    >>> synthesis_line_id("mondoai:pkt/9ec61863ea85", "2.0", "test")
    'mondoai:el/83a5307bff8d-synthesis-2.0'
    """
    return f"mondoai:el/{digest(packet_id, model)}-synthesis-{prompt_version}"


def _is_synthesis(line: dict[str, Any]) -> bool:
    return any(
        a.get("contributor") == SYNTHESIS_AGENT for a in line.get("contributions") or []
    )


def synthesis_inputs(
    packet: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What the agent reads: the claim's lines (not earlier synthesis lines) and its attached Publications.

    Publications are read only for claim types they are evidence for.
    """
    statement = packet["statement"]
    lines = [
        line
        for line in statement.get("has_evidence_lines") or []
        if not _is_synthesis(line)
    ]
    publications = []
    if publications_permitted(statement["type"]):
        publications = [
            i
            for i in statement.get("has_evidence") or []
            if i.get("type") == "Publication"
        ]
    return lines, publications


def _rule(packet: dict[str, Any]) -> dict[str, Any]:
    claim_type = packet["statement"]["type"]
    return next(
        r
        for r in packet["reference_snapshot"]["aggregation_rules"]
        if r["claim_type"] == claim_type
    )


def apply_synthesis(
    packet: dict[str, Any],
    response: SynthesisResponse,
    model: str,
    date: str,
    abstracts_read: bool,
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, Any]:
    """Record a synthesis judgement on the packet as one higher-order line and re-score.

    A previous synthesis line with the same id (same packet, prompt version and model) is
    replaced. A packet with nothing to read is returned unchanged.

    Args:
        packet: The packet (modified in place).
        response: The model's judgement.
        model: Model name, recorded on the synthesis agent.
        date: Date of the act (ISO).
        abstracts_read: Whether any Publication abstract was in the prompt.
        prompt_version: Version of the synthesis prompt.

    Returns:
        The packet.
    """
    statement = packet["statement"]
    line_id = synthesis_line_id(packet["id"], prompt_version, model)
    lines, publications = synthesis_inputs(packet)
    if not lines and not publications:
        return packet

    items = copy.deepcopy(lines) + copy.deepcopy(publications)
    read_ids = {item["id"] for item in items}
    cited = [i for i in response.cited_ids if i in read_ids]
    if publications:
        coverage = "ABSTRACT_ONLY" if abstracts_read else "TITLE_ONLY"
    else:
        coverage = "STRUCTURED_RECORD"
    description = f"{model} judged {len(lines)} evidence lines and {len(publications)} publications (synthesis prompt {prompt_version})."
    if cited:
        description += f" Relied on: {', '.join(cited)}."

    line: dict[str, Any] = {
        "id": line_id,
        "type": "SieveEvidenceLine",
        "description": description,
        "has_evidence_items": items,
        "direction_of_evidence_provided": response.direction,
        "score_of_evidence_provided": response.strength,
    }
    strength = band(response.strength, _rule(packet)["strength_bands"])
    if strength:
        line["strength_of_evidence_provided"] = strength
    line |= {
        "confidence": response.confidence,
        "confidence_basis": "MEASURED",
        "date_attached": date,
        "contributions": [
            {
                "id": f"{line_id}#act",
                "type": "InterpretationActivity",
                "contributor": SYNTHESIS_AGENT,
                "role": "INTERPRETER",
                "date": date,
                "method_type": "LLM_JUDGEMENT",
                "specified_by": [SYNTHESIS_METHOD],
                "read_coverage": coverage,
                "access_status": "NOT_RECORDED",
                "identifier_verified": "VERIFIED"
                if len(cited) == len(response.cited_ids)
                else "FAILED",
                "rationale": response.rationale,
            }
        ],
    }

    statement["has_evidence_lines"] = [
        existing
        for existing in statement.get("has_evidence_lines") or []
        if existing["id"] != line_id
    ] + [line]
    add_registry_entry(
        packet,
        "agents",
        {
            "id": SYNTHESIS_AGENT,
            "type": "SieveAgent",
            "subtype": "software",
            "name": "mondo-ai evidence synthesis agent",
            "software_version": __version__,
            "model_name": model,
            "trust_levels": copy.deepcopy(AI_TRUST),
        },
    )
    add_registry_entry(
        packet,
        "methods",
        {
            "id": SYNTHESIS_METHOD,
            "type": "SieveMethod",
            "label": "LLM synthesis of a claim's evidence lines and attached literature",
            "version": prompt_version,
        },
    )
    return score_packet(packet)


class SynthesisAgent:
    """Runs the synthesis LLM (Anthropic or OpenAI through pydantic-ai) over packets."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        llm: Any = None,
        reference_cache: ReferenceCache | None = None,
        date: str | None = None,
    ) -> None:
        """Initialize the synthesis agent.

        Args:
            provider: AI provider ("anthropic" or "openai").
            model: Model name (defaults to the provider's default model).
            api_key: API key (uses the provider's environment variable if not provided).
            llm: A pydantic-ai model instance to use instead of provider and model (e.g.
                ``TestModel`` in tests); no API key is needed then.
            reference_cache: Cache of PubMed papers; cached abstracts go into the prompt.
            date: Date of the synthesis acts (ISO); defaults to today.
        """
        self.provider = provider.lower()
        self.prompt_version = PROMPT_VERSION
        self.reference_cache = reference_cache
        self.date = date or _date.today().isoformat()
        if llm is not None:
            self.model = str(llm.model_name)
            self.api_key = api_key
        else:
            if self.provider not in DEFAULT_MODELS:
                raise ValueError(f"Unknown provider: {provider}")
            self.model = model or DEFAULT_MODELS[self.provider]
            self.api_key = api_key or os.environ.get(API_KEY_VARS[self.provider])
            if not self.api_key:
                raise ValueError(
                    f"No API key found for {provider}. Set {API_KEY_VARS[self.provider]} "
                    "environment variable or pass api_key parameter."
                )
        from pydantic_ai import Agent as PydanticAgent

        self._agent = PydanticAgent(
            llm if llm is not None else f"{self.provider}:{self.model}",
            system_prompt=SYSTEM_PROMPT,
            output_type=SynthesisResponse,
        )

    def _abstracts(self, publications: list[dict[str, Any]]) -> dict[str, str]:
        """Cached abstracts of the Publications, by id."""
        if self.reference_cache is None:
            return {}
        out = {}
        for pub in publications:
            paper = self.reference_cache.get(pub["id"].removeprefix("PMID:"))
            if paper is not None and paper.abstract:
                out[pub["id"]] = paper.abstract
        return out

    def _prompt(self, packet: dict[str, Any]) -> tuple[str, bool] | None:
        """The prompt for a packet and whether it holds an abstract; None if there is nothing to read."""
        lines, publications = synthesis_inputs(packet)
        if not lines and not publications:
            return None
        abstracts = self._abstracts(publications)
        return build_synthesis_prompt(packet, lines, publications, abstracts), bool(
            abstracts
        )

    def _apply(
        self, packet: dict[str, Any], response: SynthesisResponse, abstracts_read: bool
    ) -> dict[str, Any]:
        return apply_synthesis(
            packet, response, self.model, self.date, abstracts_read, self.prompt_version
        )

    async def synthesize_async(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Synthesize one packet (async); see ``synthesize``."""
        prompt = self._prompt(packet)
        if prompt is None:
            return packet
        result = await self._agent.run(prompt[0])
        return self._apply(packet, result.output, prompt[1])

    def synthesize(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Add the model's synthesis line to the packet and re-score it.

        Args:
            packet: The packet (modified in place).

        Returns:
            The packet.
        """
        prompt = self._prompt(packet)
        if prompt is None:
            return packet
        result = self._agent.run_sync(prompt[0])
        return self._apply(packet, result.output, prompt[1])

    def synthesize_packets(self, packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Synthesize many packets in place; a packet whose model call fails is left unchanged."""
        for packet in packets:
            try:
                self.synthesize(packet)
            except Exception as e:  # a failing API call must not stop the batch
                logger.warning("Synthesis failed for %s: %s", packet["id"], e)
        return packets


def create_agent(
    provider: str = "anthropic", model: str | None = None
) -> SynthesisAgent:
    """Create a synthesis agent with the specified provider.

    Args:
        provider: AI provider ("anthropic" or "openai").
        model: Model name (optional).

    Returns:
        Configured SynthesisAgent instance.
    """
    return SynthesisAgent(provider=provider, model=model)
