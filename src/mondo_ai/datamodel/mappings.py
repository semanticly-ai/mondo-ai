"""Type mappings between mondo-ai predicates/enums and SIEVE/SEPIO types.

This module provides utilities for converting between mondo-ai specific
enums (like StatementPredicate) and SEPIO data types (like Coding).

Example:
    >>> from mondo_ai.datamodel.mappings import predicate_to_coding
    >>> coding = predicate_to_coding(StatementPredicate.EXACT_SYNONYM)
    >>> coding.code
    'oio:hasExactSynonym'
"""

from mondo_ai.datamodel.sieve_models import (
    Coding,
    EvidenceDirection,
    StatementPredicate,
)


def predicate_to_coding(predicate: StatementPredicate | str) -> Coding:
    """Convert a StatementPredicate to a SEPIO Coding object.

    Args:
        predicate: StatementPredicate enum value or string.

    Returns:
        Coding object with code, label, and system.

    Example:
        >>> predicate_to_coding(StatementPredicate.EXACT_SYNONYM)
        Coding(code='oio:hasExactSynonym', label='exact synonym', ...)
    """
    if isinstance(predicate, str):
        predicate = StatementPredicate(predicate)

    mapping = {
        StatementPredicate.EXACT_SYNONYM: Coding(
            code="oio:hasExactSynonym",
            label="exact synonym",
            system="http://www.geneontology.org/formats/oboInOwl#",
        ),
        StatementPredicate.NARROW_SYNONYM: Coding(
            code="oio:hasNarrowSynonym",
            label="narrow synonym",
            system="http://www.geneontology.org/formats/oboInOwl#",
        ),
        StatementPredicate.BROAD_SYNONYM: Coding(
            code="oio:hasBroadSynonym",
            label="broad synonym",
            system="http://www.geneontology.org/formats/oboInOwl#",
        ),
        StatementPredicate.RELATED_SYNONYM: Coding(
            code="oio:hasRelatedSynonym",
            label="related synonym",
            system="http://www.geneontology.org/formats/oboInOwl#",
        ),
        StatementPredicate.LABEL: Coding(
            code="rdfs:label",
            label="label",
            system="http://www.w3.org/2000/01/rdf-schema#",
        ),
        StatementPredicate.DEFINITION: Coding(
            code="IAO:0000115",
            label="definition",
            system="http://purl.obolibrary.org/obo/",
        ),
        StatementPredicate.PARENT: Coding(
            code="rdfs:subClassOf",
            label="subclass of",
            system="http://www.w3.org/2000/01/rdf-schema#",
        ),
    }
    return mapping[predicate]


def coding_to_predicate(coding: Coding) -> StatementPredicate | None:
    """Convert a SEPIO Coding object to a StatementPredicate.

    Args:
        coding: SEPIO Coding object.

    Returns:
        StatementPredicate enum value or None if not mappable.
    """
    code_mapping = {
        "oio:hasExactSynonym": StatementPredicate.EXACT_SYNONYM,
        "oio:hasNarrowSynonym": StatementPredicate.NARROW_SYNONYM,
        "oio:hasBroadSynonym": StatementPredicate.BROAD_SYNONYM,
        "oio:hasRelatedSynonym": StatementPredicate.RELATED_SYNONYM,
        "rdfs:label": StatementPredicate.LABEL,
        "IAO:0000115": StatementPredicate.DEFINITION,
        "rdfs:subClassOf": StatementPredicate.PARENT,
    }
    return code_mapping.get(coding.code)


# SIEVE uses lowercase direction values: 'supports', 'disputes', 'neutral'
DIRECTION_TO_SIEVE = {
    EvidenceDirection.supports: "supports",
    EvidenceDirection.disputes: "disputes",
    EvidenceDirection.neutral: "neutral",
    # String values for flexibility
    "supports": "supports",
    "disputes": "disputes",
    "neutral": "neutral",
}

# Map from SIEVE direction strings to enum
SIEVE_TO_DIRECTION = {
    "supports": EvidenceDirection.supports,
    "disputes": EvidenceDirection.disputes,
    "neutral": EvidenceDirection.neutral,
}


def direction_to_sieve(direction: EvidenceDirection | str) -> str:
    """Convert EvidenceDirection to SIEVE direction string.

    Args:
        direction: EvidenceDirection enum value or string.

    Returns:
        SIEVE direction string ('supports', 'disputes', 'neutral').

    Example:
        >>> direction_to_sieve(EvidenceDirection.supports)
        'supports'
    """
    if isinstance(direction, EvidenceDirection):
        return direction.value
    return DIRECTION_TO_SIEVE.get(direction, "neutral")


def sieve_to_direction(direction_str: str) -> EvidenceDirection:
    """Convert SIEVE direction string to EvidenceDirection enum.

    Args:
        direction_str: SIEVE direction string.

    Returns:
        EvidenceDirection enum value.
    """
    return SIEVE_TO_DIRECTION.get(direction_str, EvidenceDirection.neutral)
