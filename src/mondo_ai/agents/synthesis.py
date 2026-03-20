"""AI agent for synthesizing evidence and generating recommendations.

This module uses pydantic-ai to analyze evidence packets and generate
recommendations for ontology curation.

Example:
    >>> from mondo_ai.agents import SynthesisAgent
    >>> agent = SynthesisAgent()
    >>> synthesis = await agent.synthesize(packet)
    >>> print(synthesis.direction, synthesis.summary)
"""

import os
from datetime import date, datetime, timezone
from uuid import uuid4

from pydantic import BaseModel

from mondo_ai.agents.prompts import SYSTEM_PROMPT, build_synthesis_prompt
from mondo_ai.datamodel import (
    Agent,
    CurationActivity,
    EvidenceDirection,
    EvidencePacket,
    EvidenceSynthesis,
    RecommendedAction,
    Score,
)


class SynthesisResponse(BaseModel):
    """Response from the synthesis agent."""

    action: str
    confidence: float
    summary: str
    key_evidence: list[str] = []


def _action_to_direction(action: RecommendedAction) -> EvidenceDirection:
    """Map recommended action to SIEVE evidence direction.

    Args:
        action: The recommended action.

    Returns:
        EvidenceDirection enum value.
    """
    # ADD and CONFIRM suggest the evidence supports the statement
    if action in (RecommendedAction.ADD, RecommendedAction.CONFIRM):
        return EvidenceDirection.supports
    # REJECT suggests the evidence disputes the statement
    if action == RecommendedAction.REJECT:
        return EvidenceDirection.disputes
    # REVIEW is neutral
    return EvidenceDirection.neutral


class SynthesisAgent:
    """AI agent for synthesizing evidence and generating recommendations.

    Uses pydantic-ai with Anthropic Claude or OpenAI GPT models.
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Initialize the synthesis agent.

        Args:
            provider: AI provider ("anthropic" or "openai").
            model: Model name (defaults to provider's best model).
            api_key: API key (uses environment variable if not provided).
        """
        self.provider = provider.lower()

        if self.provider == "anthropic":
            self.model = model or "claude-sonnet-4-20250514"
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        elif self.provider == "openai":
            self.model = model or "gpt-4o"
            self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        else:
            raise ValueError(f"Unknown provider: {provider}")

        if not self.api_key:
            raise ValueError(
                f"No API key found for {provider}. "
                f"Set {'ANTHROPIC_API_KEY' if provider == 'anthropic' else 'OPENAI_API_KEY'} "
                "environment variable or pass api_key parameter."
            )

        self.prompt_version = "v1.0"
        self._agent = None
        self._initialize_agent()

    def _initialize_agent(self) -> None:
        """Initialize the pydantic-ai agent."""
        try:
            from pydantic_ai import Agent as PydanticAgent

            if self.provider == "anthropic":
                self._agent = PydanticAgent(
                    f"anthropic:{self.model}",
                    system_prompt=SYSTEM_PROMPT,
                    output_type=SynthesisResponse,
                )
            else:
                self._agent = PydanticAgent(
                    f"openai:{self.model}",
                    system_prompt=SYSTEM_PROMPT,
                    output_type=SynthesisResponse,
                )
        except ImportError as e:
            raise ImportError(
                "pydantic-ai is required for synthesis. Install with: pip install pydantic-ai"
            ) from e

    def _parse_action(self, action_str: str) -> RecommendedAction:
        """Parse action string to RecommendedAction enum.

        Args:
            action_str: The action string from the AI response.

        Returns:
            RecommendedAction enum value.
        """
        action_map = {
            "ADD": RecommendedAction.ADD,
            "CONFIRM": RecommendedAction.CONFIRM,
            "REJECT": RecommendedAction.REJECT,
            "REVIEW": RecommendedAction.REVIEW,
        }
        return action_map.get(action_str.upper(), RecommendedAction.REVIEW)

    def _create_curation_activity(self) -> CurationActivity:
        """Create a CurationActivity for the AI-generated synthesis.

        Returns:
            CurationActivity with the model info.
        """
        return CurationActivity(
            id=f"act_{uuid4().hex[:8]}",
            type="CurationActivity",
            contributor=Agent(
                id=self.model,
                type="Agent",
                name=self.model,
                description=f"AI synthesis agent ({self.provider})",
            ),
            date=str(date.today()),
            used=[f"prompt_version:{self.prompt_version}"],
        )

    async def synthesize_async(self, packet: EvidencePacket) -> EvidenceSynthesis:
        """Synthesize evidence and generate a synthesis (async).

        Args:
            packet: The evidence packet to analyze.

        Returns:
            EvidenceSynthesis with the synthesis result.
        """
        if self._agent is None:
            raise RuntimeError("Agent not initialized")

        # Build the prompt
        prompt = build_synthesis_prompt(packet)

        # Run the agent
        result = await self._agent.run(prompt)

        # Parse the response (pydantic-ai 1.x uses .output for structured output)
        response = result.output
        action = self._parse_action(response.action)

        return EvidenceSynthesis(
            summary=response.summary,
            score=Score(value=response.confidence, description="AI confidence score"),
            direction=_action_to_direction(action),
            cited_evidence=response.key_evidence,
            generated_by=self._create_curation_activity(),
        )

    def synthesize(self, packet: EvidencePacket, update_packet: bool = True) -> EvidenceSynthesis:
        """Synthesize evidence and generate a synthesis (sync wrapper).

        Args:
            packet: The evidence packet to analyze.
            update_packet: If True, also updates packet.evidence_synthesis in place.

        Returns:
            EvidenceSynthesis with the synthesis result.
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        synthesis = loop.run_until_complete(self.synthesize_async(packet))

        if update_packet:
            packet.evidence_synthesis = synthesis

        return synthesis

    async def synthesize_packets_async(
        self,
        packets: list[EvidencePacket],
    ) -> list[tuple[EvidencePacket, EvidenceSynthesis]]:
        """Synthesize multiple packets (async).

        Args:
            packets: List of evidence packets.

        Returns:
            List of (packet, synthesis) tuples.
        """
        results = []
        for packet in packets:
            try:
                synthesis = await self.synthesize_async(packet)
                # Update the packet with the synthesis
                packet.evidence_synthesis = synthesis
                results.append((packet, synthesis))
            except Exception as e:
                print(f"Error synthesizing packet {packet.id}: {e}")
                # Create a fallback synthesis
                fallback = EvidenceSynthesis(
                    summary=f"Synthesis failed: {e}",
                    score=Score(value=0.0, description="Failed synthesis"),
                    direction=EvidenceDirection.neutral,
                    generated_by=self._create_curation_activity(),
                )
                results.append((packet, fallback))
        return results

    def synthesize_packets(
        self,
        packets: list[EvidencePacket],
    ) -> list[tuple[EvidencePacket, EvidenceSynthesis]]:
        """Synthesize multiple packets (sync wrapper).

        Args:
            packets: List of evidence packets.

        Returns:
            List of (packet, synthesis) tuples.
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.synthesize_packets_async(packets))


def create_agent(
    provider: str = "anthropic",
    model: str | None = None,
) -> SynthesisAgent:
    """Create a synthesis agent with the specified provider.

    Args:
        provider: AI provider ("anthropic" or "openai").
        model: Model name (optional).

    Returns:
        Configured SynthesisAgent instance.
    """
    return SynthesisAgent(provider=provider, model=model)
