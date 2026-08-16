"""Prompt templates for the evidence synthesis agent.

This module contains prompt templates for the AI agent that synthesizes
evidence and generates recommendations for ontology curation.
"""

SYSTEM_PROMPT = """You are an expert ontology curator specializing in the Mondo Disease Ontology.
Your task is to analyze evidence packets and recommend whether assertions should be:
- ADD: Added to the ontology (for new, well-supported statements)
- CONFIRM: Confirmed as correct (for existing statements with supporting evidence)
- REJECT: Not added/removed (for statements with contradicting or insufficient evidence)
- REVIEW: Flagged for human review (for controversial or ambiguous cases)

When analyzing evidence, consider:
1. The type and strength of each evidence item
2. Whether evidence supports or contradicts the assertion
3. The reliability of sources (concordance from major ontologies is strong)
4. Any literature evidence and its relevance
5. The overall balance of evidence

Be concise but thorough in your rationale. Cite specific evidence items by ID."""

SYNTHESIS_PROMPT_TEMPLATE = """Analyze the following evidence packet and provide a recommendation.

## Statement
- Subject: {subject_label} ({subject_id})
- Predicate: {predicate}
- Object: {object_value}

## Evidence Lines
{evidence_section}

## Current Score
Net Evidence Ratio: {evidence_score}

---

Based on this evidence, provide:
1. Your recommended action (ADD, CONFIRM, REJECT, or REVIEW)
2. Your confidence (0-1)
3. A brief summary synthesizing your analysis and recommendation
4. List any evidence IDs that most strongly support your recommendation

Respond in the following JSON format:
{{
  "action": "ADD|CONFIRM|REJECT|REVIEW",
  "confidence": 0.0-1.0,
  "summary": "Your synthesis explaining the evidence and recommendation",
  "key_evidence": ["ev_id_1", "ev_id_2"]
}}"""


def format_evidence_line(line) -> str:
    """Format a single evidence line for the prompt.

    Args:
        line: A SieveEvidenceLine object.

    Returns:
        Formatted string for the prompt.
    """
    output = []
    output.append(f"### Evidence Line: {line.id}")
    output.append(f"- Direction: {line.direction_of_evidence_provided or 'supports'}")
    output.append(f"- Strength: {line.strength_of_evidence_provided or 'moderate'}")
    output.append(f"- Score: {line.score_of_evidence_provided or 1.0}")

    # Format evidence items within this line
    if line.has_evidence_items:
        for item in line.has_evidence_items:
            output.append(f"\n  #### Evidence Item: {item.id}")

            # Get item type
            item_type = getattr(item, "evidence_item_type", None)
            if item_type:
                if hasattr(item_type, "value"):
                    item_type = item_type.value
                output.append(f"  - Type: {item_type}")

            # Description
            if hasattr(item, "description") and item.description:
                output.append(f"  - Description: {item.description}")

            # ConcordanceItem fields
            if hasattr(item, "source_name") and item.source_name:
                output.append(f"  - Source: {item.source_name}")
            if hasattr(item, "source_subject") and item.source_subject:
                output.append(f"  - Source Subject: {item.source_subject}")
            if hasattr(item, "source_subject_label") and item.source_subject_label:
                output.append(f"  - Source Label: {item.source_subject_label}")

            # SieveDocument fields
            if hasattr(item, "title") and item.title:
                output.append(f"  - Title: {item.title}")
            if hasattr(item, "pmid") and item.pmid:
                output.append(f"  - PMID: {item.pmid}")
            if hasattr(item, "quote") and item.quote:
                quote = item.quote[:200] + "..." if len(item.quote) > 200 else item.quote
                output.append(f"  - Quote: \"{quote}\"")

    output.append("")
    return "\n".join(output)


def format_evidence_section(evidence_lines: list | None) -> str:
    """Format all evidence lines for the prompt.

    Args:
        evidence_lines: List of SieveEvidenceLine objects.

    Returns:
        Formatted string for the prompt.
    """
    if not evidence_lines:
        return "*No evidence lines*"

    return "\n".join(format_evidence_line(line) for line in evidence_lines)


def build_synthesis_prompt(packet) -> str:
    """Build the synthesis prompt for a packet.

    Args:
        packet: The evidence packet to analyze.

    Returns:
        The formatted prompt string.
    """
    # Get predicate from Coding object
    predicate = packet.statement.predicate
    if predicate:
        if hasattr(predicate, "label") and predicate.label:
            predicate = predicate.label
        elif hasattr(predicate, "code") and predicate.code:
            predicate = predicate.code
        elif hasattr(predicate, "value"):
            predicate = predicate.value
    else:
        predicate = "unknown"

    # Get current evidence score from synthesis if available
    evidence_score: float = 0.0
    if packet.evidence_synthesis and packet.evidence_synthesis.score:
        evidence_score = packet.evidence_synthesis.score.value or 0.0
    else:
        # Calculate basic score from evidence lines
        if packet.has_evidence_lines:
            total = sum(
                line.score_of_evidence_provided or 1.0
                for line in packet.has_evidence_lines
                if line.direction_of_evidence_provided == "supports"
            )
            count = len(packet.has_evidence_lines)
            evidence_score = total / count if count else 0

    # Format evidence section
    evidence_section = format_evidence_section(packet.has_evidence_lines)

    return SYNTHESIS_PROMPT_TEMPLATE.format(
        subject_label=packet.statement.subject_label or "Unknown",
        subject_id=packet.statement.subject or "",
        predicate=predicate,
        object_value=packet.statement.object or "",
        evidence_section=evidence_section,
        evidence_score=round(evidence_score, 3),
    )
