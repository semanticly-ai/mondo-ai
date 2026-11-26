"""Prompts for the evidence synthesis agent.

The agent reads one packet: the claim, its evidence lines (each a source's own claim,
record or score, already read by a rule) and the Publications attached to the claim. It
returns its own judgement of how the evidence bears on the claim: a direction, a
strength, its confidence and a short rationale. It does not decide anything; a curator
signs off.

Everything is formatted from the packet dict (the SIEVE profile shape).
"""

from typing import Any, Mapping

PROMPT_VERSION = "2.0"

SYSTEM_PROMPT = """You are an expert ontology curator for the Mondo Disease Ontology.
You read the evidence for one claim about a Mondo term (a synonym, a parent class or a
mapping) and judge how the evidence, taken together, bears on the claim.

Each evidence line is a source's own statement (in the source's ids, normalized to Mondo ids),
a source record, or a computed score, already rated by a rule. Publications are attached
but nobody has read them as evidence yet.

Judge:
- direction: "supports" or "disputes" the claim;
- strength: 0-1, how strongly the evidence you read bears on the claim in that direction;
- confidence: 0-1, how sure you are that you read and linked the evidence correctly;
- rationale: one or two sentences;
- cited_ids: the ids of the lines and publications you relied on, only ids shown to you.

Consider whether sources are independent, whether a publication is about the claim's
terms at all, and whether a record is obsolete. You give a judgement, not a decision:
a curator decides."""

SYNTHESIS_PROMPT_TEMPLATE = """Judge the evidence for this claim.

## Claim
- Statement: {statement_text}
- Type: {claim_type}
- Subject: {subject}
- Predicate: {predicate}
- Object: {object_value}
- Review state: {review_state}
- Current inclusion score S (-1 to 1, from the evidence lines): {score}

## Evidence lines
{evidence_section}

## Publications
{publication_section}
"""


def _record_label(item: dict[str, Any]) -> str | None:
    """The label of the first record a prior claim is reported in."""
    for record in item.get("reported_in") or []:
        if record.get("label"):
            return str(record["label"])
    return None


def format_item(item: dict[str, Any]) -> str:
    """One evidence item as a prompt line.

    >>> format_item({"id": "pc1", "type": "SynonymStatement", "subject": "DOID:1",
    ...              "predicate": {"code": "oboInOwl:hasExactSynonym", "label": "has exact synonym"},
    ...              "object": "CF", "resource": "infores:disease-ontology",
    ...              "reported_in": [{"id": "DOID:1", "type": "ExternalRecord", "label": "cystic fibrosis"}]})
    "- SynonymStatement pc1 from infores:disease-ontology: DOID:1 has exact synonym CF (record label 'cystic fibrosis')"
    >>> format_item({"id": "cs1", "type": "ComputationalScore", "value": "1.0", "parameters": "label=label"})
    '- ComputationalScore cs1: value 1.0 (label=label)'
    """
    kind, item_id = item["type"], item["id"]
    source = f" from {item['resource']}" if item.get("resource") else ""
    if kind == "ExternalRecord":
        label = f" '{item['label']}'" if item.get("label") else ""
        obsolete = " (obsolete in source)" if item.get("obsolete_in_source") else ""
        return f"- ExternalRecord {item_id}{source}:{label}{obsolete}"
    if kind == "ComputationalScore":
        parameters = f" ({item['parameters']})" if item.get("parameters") else ""
        return f"- ComputationalScore {item_id}: value {item.get('value')}{parameters}"
    if kind == "Publication":
        return f"- Publication {item_id}: {item.get('label') or 'untitled'}"
    if kind == "SieveEvidenceLine":
        return f"- Evidence line {item_id}: {item.get('direction_of_evidence_provided')}, score {item.get('score_of_evidence_provided')}"
    predicate = (item.get("predicate") or {}).get("label") or (
        item.get("predicate") or {}
    ).get("code")
    record = _record_label(item)
    record_text = f" (record label '{record}')" if record else ""
    return f"- {kind} {item_id}{source}: {item.get('subject')} {predicate} {item.get('object')}{record_text}"


def format_evidence_line(line: dict[str, Any]) -> str:
    """One evidence line: its id, direction, strength, score and items."""
    out = [
        f"### {line['id']}",
        f"Direction: {line['direction_of_evidence_provided']}; "
        f"strength: {line.get('strength_of_evidence_provided', 'not banded')}; "
        f"score: {line.get('score_of_evidence_provided', 'none')}",
    ]
    if line.get("description"):
        out.append(f"Description: {line['description']}")
    out += [format_item(item) for item in line.get("has_evidence_items") or []]
    return "\n".join(out)


def format_evidence_section(lines: list[dict[str, Any]]) -> str:
    """All evidence lines for the prompt.

    >>> format_evidence_section([])
    '*No evidence lines*'
    """
    if not lines:
        return "*No evidence lines*"
    return "\n\n".join(format_evidence_line(line) for line in lines)


def format_publications(
    publications: list[dict[str, Any]], abstracts: Mapping[str, str] | None = None
) -> str:
    """The attached Publications: id, title and the cached abstract when there is one.

    >>> format_publications([])
    '*No publications*'
    >>> print(format_publications([{"id": "PMID:1", "type": "Publication", "label": "On CF"}], {"PMID:1": "Text."}))
    ### PMID:1
    Title: On CF
    Abstract: Text.
    """
    if not publications:
        return "*No publications*"
    abstracts = abstracts or {}
    blocks = []
    for pub in publications:
        abstract = abstracts.get(pub["id"]) or "*No abstract available*"
        blocks.append(
            f"### {pub['id']}\nTitle: {pub.get('label') or 'untitled'}\nAbstract: {abstract}"
        )
    return "\n\n".join(blocks)


def build_synthesis_prompt(
    packet: dict[str, Any],
    lines: list[dict[str, Any]] | None = None,
    publications: list[dict[str, Any]] | None = None,
    abstracts: Mapping[str, str] | None = None,
) -> str:
    """The synthesis prompt for one packet.

    Args:
        packet: The packet.
        lines: The lines to show (default: all the statement's lines).
        publications: The Publications to show (default: those in ``statement.has_evidence``).
        abstracts: Cached abstracts by Publication id.

    Returns:
        The prompt.
    """
    statement = packet["statement"]
    if lines is None:
        lines = list(statement.get("has_evidence_lines") or [])
    if publications is None:
        publications = [
            i
            for i in statement.get("has_evidence") or []
            if i.get("type") == "Publication"
        ]
    predicate = statement.get("predicate") or {}
    return SYNTHESIS_PROMPT_TEMPLATE.format(
        statement_text=statement.get("statement_text", ""),
        claim_type=statement["type"],
        subject=statement["subject"],
        predicate=predicate.get("label") or predicate.get("code", ""),
        object_value=statement.get("object", ""),
        review_state=statement.get("review_state", ""),
        score=statement.get("score", 0.0),
        evidence_section=format_evidence_section(lines),
        publication_section=format_publications(publications, abstracts),
    )
