"""Constructors for the evidence a mondo-ai packet nests, as profile-shaped dicts.

Each function returns one object of the SIEVE profile (``schema/sieve.yaml``) as a plain
dict: an ExternalRecord as read from a source release, a source's own claim (a prior
claim, typed by claim type) with the resolution chains that normalize its ids to Mondo, a
ComputationalScore, and the evidence line that interprets them. Ids are derived from
content, so the same input always gives the same object and copies of an item nested in
several places are identical, as the profile requires.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from mondo_ai.packets.registry import AGENT, CONCORDANCE_METHOD, NORMALIZATION_METHOD, LEXICAL_METHOD, PUBMED

BASIS_UNKNOWN = {"code": "sieve:basis_unknown"}

PREDICATE_LABELS = {
    "oboInOwl:hasExactSynonym": "has exact synonym",
    "oboInOwl:hasRelatedSynonym": "has related synonym",
    "oboInOwl:hasNarrowSynonym": "has narrow synonym",
    "oboInOwl:hasBroadSynonym": "has broad synonym",
    "rdfs:subClassOf": "subclass of",
    "skos:exactMatch": "exact match",
    "skos:broadMatch": "broad match",
    "skos:narrowMatch": "narrow match",
    "skos:relatedMatch": "related match",
}


def digest(*parts: str, length: int = 12) -> str:
    """A short content hash.

    >>> digest("a", "b")
    '0eab8a0a3380'
    """
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:length]


def coding(code: str) -> dict[str, str]:
    """A Coding for a predicate CURIE, with its label.

    >>> coding("skos:exactMatch")
    {'code': 'skos:exactMatch', 'label': 'exact match'}
    """
    out = {"code": code}
    if code in PREDICATE_LABELS:
        out["label"] = PREDICATE_LABELS[code]
    return out


@dataclass(frozen=True)
class Release:
    """Where and when a source was read: the resource, release and retrieval date."""

    source_id: str
    resource: str
    release: str
    version: str
    retrieval_date: str


def external_record(rel: Release, curie: str, label: str | None, obsolete: bool) -> dict[str, Any]:
    """An ExternalRecord: a source record as read from a release."""
    metadata: dict[str, Any] = {
        "retrieved_from": rel.resource,
        "source_release": rel.release,
        "retrieval_date": rel.retrieval_date,
    }
    record: dict[str, Any] = {"id": curie, "type": "ExternalRecord", "resource": rel.resource}
    if label:
        record["label"] = label
        metadata["source_label"] = label
    record["obsolete_in_source"] = obsolete
    record["record_metadata"] = metadata
    return record


def publication(pmid: str) -> dict[str, Any]:
    """A Publication cited by a source on one of its claims."""
    return {"id": f"PMID:{pmid}", "type": "Publication", "resource": PUBMED}


@dataclass(frozen=True)
class Normalization:
    """One skos:exactMatch row of mondo.sssom.tsv used to normalize a source id to a Mondo id."""

    source_id: str
    mondo_id: str
    justification: str | None
    mapping_set: str
    mapping_set_version: str


def resolution_chain(slot: str, g: Normalization, date: str) -> dict[str, Any]:
    """A one-step ResolutionChain normalizing a source id to a Mondo id through a curated mapping.

    Id to id is NORMALIZATION; GROUNDING is reserved for resolving a string to an id.
    """
    chain_id = f"mondoai:rc/{digest(slot, g.source_id, g.mondo_id, g.mapping_set_version)}"
    step: dict[str, Any] = {
        "id": f"{chain_id}-1",
        "type": "AlignmentStep",
        "category": "NORMALIZATION",
        "method": "DETERMINISTIC_RULE",
        "input_value": g.source_id,
        "output_value": g.mondo_id,
        "predicate": "skos:exactMatch",
        "mapping_set": g.mapping_set,
        "mapping_set_version": g.mapping_set_version,
        "quality_category": "normalization_curated",
        "confidence": 1.0,
        "confidence_basis": "DETERMINISTIC",
    }
    if g.justification:
        step["mapping_justification"] = g.justification
    return {
        "id": chain_id,
        "type": "ResolutionChain",
        "entity_slot": slot,
        "date": date,
        "specified_by": [NORMALIZATION_METHOD],
        "chain_confidence": 1.0,
        "steps": [step],
    }


def prior_claim(
    claim_type: str,
    rel: Release,
    subject: str,
    predicate: str,
    obj: str,
    reported_in: list[dict[str, Any]],
    resolution_chains: list[dict[str, Any]],
    generation_context: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A claim a source makes in its own ids, nested as an evidence item."""
    return {
        "id": f"mondoai:pc/{digest(claim_type, rel.source_id, subject, predicate, obj)}",
        "type": claim_type,
        "subject": subject,
        "predicate": coding(predicate),
        "object": obj,
        "resource": rel.resource,
        "generation_context": generation_context or [BASIS_UNKNOWN],
        "reported_in": reported_in,
        "resolution_chains": resolution_chains,
    }


def computational_score(value: float, parameters: str, software_version: str) -> dict[str, Any]:
    """A ComputationalScore produced by the lexical matcher."""
    return {
        "id": f"mondoai:cs/{digest(parameters, str(value))}",
        "type": "ComputationalScore",
        "value": str(value),
        "specified_by": [LEXICAL_METHOD],
        "software_version": software_version,
        "parameters": parameters,
    }


def band(score: float, bands: list[str]) -> str | None:
    """The first band whose condition the score satisfies.

    >>> band(0.85, ["strong >= 0.8", "moderate >= 0.4", "weak < 0.4"])
    'strong'
    """
    from sieve.inclusion_score import band_for

    return band_for(score, bands)


def evidence_line(
    line_id: str,
    items: list[dict[str, Any]],
    direction: str,
    score: float,
    rule: dict[str, Any],
    cluster: str,
    date: str,
    description: str,
    release: str | None = None,
) -> dict[str, Any]:
    """A rule-based SieveEvidenceLine interpreting the items for the enclosing claim."""
    line: dict[str, Any] = {
        "id": line_id,
        "type": "SieveEvidenceLine",
        "description": description,
        "has_evidence_items": items,
        "direction_of_evidence_provided": direction,
        "score_of_evidence_provided": score,
        "computed_by_rule": rule["id"],
        "independence_cluster": cluster,
        "date_attached": date,
        "last_confirmed": date,
        "contributions": [
            {
                "id": f"{line_id}#act",
                "type": "InterpretationActivity",
                "contributor": AGENT,
                "role": "INTERPRETER",
                "date": date,
                "method_type": "RULE_BASED_STRENGTH",
                "specified_by": [CONCORDANCE_METHOD],
                "read_coverage": "STRUCTURED_RECORD",
            }
        ],
    }
    strength = band(score, rule["strength_bands"])
    if strength:
        line["strength_of_evidence_provided"] = strength
    if release:
        line["last_confirmed_against"] = release
    return line
