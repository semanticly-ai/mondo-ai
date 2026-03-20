"""AI agents module for evidence synthesis."""

from mondo_ai.agents.synthesis import SynthesisAgent, create_agent
from mondo_ai.agents.prompts import (
    SYSTEM_PROMPT,
    SYNTHESIS_PROMPT_TEMPLATE,
    build_synthesis_prompt,
    format_evidence_section,
)

__all__ = [
    "SynthesisAgent",
    "create_agent",
    "SYSTEM_PROMPT",
    "SYNTHESIS_PROMPT_TEMPLATE",
    "build_synthesis_prompt",
    "format_evidence_section",
]
