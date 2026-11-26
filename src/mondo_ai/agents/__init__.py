"""AI agents module for evidence synthesis."""

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
    create_agent,
    synthesis_line_id,
)

__all__ = [
    "PROMPT_VERSION",
    "SYNTHESIS_AGENT",
    "SYNTHESIS_METHOD",
    "SYNTHESIS_PROMPT_TEMPLATE",
    "SYSTEM_PROMPT",
    "SynthesisAgent",
    "SynthesisResponse",
    "apply_synthesis",
    "build_synthesis_prompt",
    "create_agent",
    "format_evidence_section",
    "format_publications",
    "synthesis_line_id",
]
