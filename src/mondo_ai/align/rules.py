"""Citable mapping curation rules.

A mapping decision is only meaningful relative to the rules it was made under.
This module turns the prose rule registry (the ``maprule:`` families ``EV``, ``SC``,
``PR``, ``ST``, ``RJ`` and the disease conflation pack ``CM``) into data, so that every
row the pipeline emits can name the rules its verdict rests on.

The registry is deliberately flat and dependency-free: it is imported by the feature
detectors, the deterministic ladder, the agent prompt and the report writer, and all four
must cite the same identifiers.

Example:
    >>> rule("EV-001").title
    'exact match on primary label'
    >>> rule("SC-001").blocks_exact_match
    True
    >>> sorted(family_ids("PR"))[:3]
    ['PR-001', 'PR-002', 'PR-003']
    >>> render_rule_text(["EV-002", "CM-001"]).split(" | ")[1]
    'maprule:CM-001 disease/disorder conflation'
"""

from __future__ import annotations

from dataclasses import dataclass

MAPRULE_PREFIX = "maprule"
MAPRULE_URI_BASE = "https://w3id.org/mapping-rules/"


@dataclass(frozen=True)
class Rule:
    """A single citable curation rule.

    Attributes:
        id: Stable rule identifier, e.g. ``EV-001``.
        family: Two-letter family code (``EV``, ``SC``, ``PR``, ``ST``, ``RJ``, ``CM``).
        title: Short human-readable title, used verbatim in ``curation_rule_text``.
        meaning: One-paragraph explanation, used in the agent prompt.
        blocks_exact_match: True when the rule, once it fires, forbids ``skos:exactMatch``.
        rejection_ground: True when the rule is a terminal reason not to map at all.

    Example:
        >>> Rule("X-001", "EV", "t", "m").curie
        'maprule:X-001'
    """

    id: str
    family: str
    title: str
    meaning: str
    blocks_exact_match: bool = False
    rejection_ground: bool = False

    @property
    def curie(self) -> str:
        """Return the rule as a CURIE in the ``maprule`` namespace.

        Example:
            >>> rule("PR-001").curie
            'maprule:PR-001'
        """
        return f"{MAPRULE_PREFIX}:{self.id}"

    @property
    def uri(self) -> str:
        """Return the (proposed) resolvable URI for the rule.

        Example:
            >>> rule("PR-001").uri
            'https://w3id.org/mapping-rules/PR-001'
        """
        return f"{MAPRULE_URI_BASE}{self.id}"

    def cite(self) -> str:
        """Render the rule the way it appears in ``curation_rule_text``.

        Example:
            >>> rule("EV-001").cite()
            'maprule:EV-001 exact match on primary label'
        """
        return f"{self.curie} {self.title}"


def _r(
    rid: str,
    title: str,
    meaning: str,
    *,
    blocks: bool = False,
    rejects: bool = False,
) -> Rule:
    return Rule(rid, rid.split("-")[0], title, meaning, blocks, rejects)


#: Family EV — what was actually observed that supports the mapping.
EV_RULES = [
    _r("EV-001", "exact match on primary label",
       "Subject and object primary labels are identical. High confidence, very low cost."),
    _r("EV-002", "exact match on normalised label",
       "Identical after non-semantic normalisation only (case, accents, punctuation, "
       "whitespace). Equivalent in strength to EV-001."),
    _r("EV-003", "exact match on exact synonym",
       "One side's primary label equals the other's exact synonym. Medium-to-high confidence."),
    _r("EV-004", "match after declared lexical transformation",
       "Matched only after a documented, bounded rewrite (possessive, word order, "
       "British/American spelling, disease/disorder). The rule is recorded in "
       "subject_preprocessing."),
    _r("EV-005", "proxy mapping via third resource",
       "Both sides already map to the same concept in a trusted third resource."),
    _r("EV-006", "lexical similarity only",
       "Low edit distance, or one label contained in the other. Low confidence — never "
       "sufficient alone for exactMatch."),
    _r("EV-007", "synonym similarity",
       "Non-exact synonym overlap. Low confidence, not sufficient alone."),
    _r("EV-008", "hierarchical corroboration",
       "Parents, children or linked features are comparable. Medium confidence. A failing "
       "hierarchical comparison is weak evidence against, because primary organising "
       "relationships rarely correspond across semantic spaces."),
    _r("EV-009", "definition comparison",
       "Textual definitions were compared. Medium-to-high confidence, high cost."),
    _r("EV-010", "full expert review",
       "An expert aggregated hierarchy, axioms, definitions and external sources."),
    _r("EV-011", "external source confirmation",
       "The mapping is asserted by the source itself, via an explicit exact match."),
    _r("EV-012", "automated adjudication over retrieved candidates",
       "Candidates were retrieved lexically and a model judged equivalence. Records the "
       "reasoning; does not by itself license a curated assertion."),
]

#: Family SC — where the two concepts differ in extension rather than in kind.
SC_RULES = [
    _r("SC-001", 'undefined residual subset ("Other X")',
       "Object is a semantically undefined remainder class. MUST NOT be an exactMatch, even "
       "when the subject disease is listed under it as an inclusion term.", blocks=True),
    _r("SC-002", '"unspecified" or "excluding X" rubric',
       "Object is scoped by exclusion; its extension is not stable. MUST NOT be an "
       "exactMatch.", blocks=True),
    _r("SC-003", '"related conditions" grouping',
       "Object is an open-ended association bucket. MUST be skos:relatedMatch.", blocks=True),
    _r("SC-004", "disjunctive rubric",
       "Object names two or more distinct diseases and is broader by construction. Not an "
       "exactMatch.", blocks=True),
    _r("SC-005", "target finer than subject",
       "Object is a site, laterality, encounter, severity or stage variant. Object is "
       "narrower.", blocks=True),
    _r("SC-006", "subtype under type-agnostic rubric",
       "Subject is a subtype and the object is the rubric that abstracts over it. Object is "
       "broader. Not an exactMatch unless SC-008 is declared.", blocks=True),
    _r("SC-007", "rubric coextensive with subject",
       "The object's own subdivisions are all subtypes of the subject concept, so their "
       "extensions coincide. exactMatch is correct, including when the rubric's label "
       "carries 'unspecified'. Distinguishing this from SC-001 is the hardest judgement "
       "in this registry."),
    _r("SC-008", "granularity tolerance declared",
       "The use case explicitly permits mapping up to the next best available concept. The "
       "predicate MUST still reflect the true relation."),
]

#: Family PR — predicate selection. Direction: in `subject predicate object`,
#: skos:narrowMatch means the OBJECT is narrower.
PR_RULES = [
    _r("PR-001", "same real-world concept", "Subject and object denote the same disease."),
    _r("PR-002", "object is a subclass",
       "No target concept equals the subject; the object is narrower → skos:narrowMatch."),
    _r("PR-003", "object is a superclass",
       "No target concept equals the subject; the object is broader → skos:broadMatch."),
    _r("PR-004", "object is a sibling", "→ skos:closeMatch."),
    _r("PR-005", "conceptually related only",
       "No exact, broad, narrow or close fit → skos:relatedMatch. Should be rare."),
    _r("PR-006", "exactMatch is not OWL equivalence",
       "skos:exactMatch asserts referential sameness for integration purposes, not "
       "owl:equivalentClass."),
    _r("PR-007", "prefer exact or broad",
       "closeMatch and relatedMatch are hard to use analytically; an exclusion reason MUST "
       "be given where the external concept is out of scope."),
    _r("PR-008", "ambiguity is not a match",
       "Two or more equally defensible candidates → assert nothing.", blocks=True),
]

#: Family ST — structural integrity, checked over the set rather than the row.
ST_RULES = [
    _r("ST-001", "proxy merge",
       "Two object concepts from one resource both exactMatch a single subject. Disallowed "
       "for precise mappings; a core curator must resolve it."),
    _r("ST-002", "reverse proxy merge",
       "Two subject concepts both exactMatch a single object id. Disallowed at every tier."),
    _r("ST-003", "object identifier must exist",
       "The object id must be a real, current identifier in the target vocabulary at the "
       "declared version.", blocks=True, rejects=True),
    _r("ST-004", "object must not be obsolete",
       "Deprecated or obsoleted target concepts must not carry exact matches.",
       blocks=True, rejects=True),
    _r("ST-005", "identifier is not a code",
       "The object id is a chapter range or belongs to a different vocabulary than its "
       "prefix claims. Invalid regardless of the label's plausibility.",
       blocks=True, rejects=True),
    _r("ST-006", "subject must be in scope",
       "The subject must be a disease, not a phenotypic feature or a gene.", blocks=True),
]

#: Family RJ — terminal rejection grounds. A rejection is a finding, not a dropped row.
RJ_RULES = [
    _r("RJ-001", "no candidate found",
       "Nothing in the target vocabulary denotes this concept.", rejects=True),
    _r("RJ-002", "ambiguous candidates",
       "Several defensible candidates, none clearly best.", rejects=True),
    _r("RJ-003", "homonym",
       "Labels agree but the concepts differ — the classic failure mode of label-only "
       "evidence.", rejects=True),
    _r("RJ-004", "token overlap only",
       "Candidate shares words but denotes a different disease. Retrieval noise.",
       rejects=True),
    _r("RJ-005", "target out of scope",
       "The target concept is not a disease under the subject resource's model.",
       blocks=True, rejects=True),
]

#: Family CM — conceptual model conflation (the disease/phenotype domain pack).
#: Conflation is not an error. It is a decision, and it must be visible.
CM_RULES = [
    _r("CM-001", "disease/disorder conflation",
       "Subject and object are technically distinct as disease vs disorder; conflated as "
       "acceptable."),
    _r("CM-002", "disease/phenotype conflation, murky case",
       "The object is modelled as a sign, symptom or clinical finding while the subject is "
       "a disease, and the distinction is genuinely unclear. Conflated to avoid losing "
       "associations. Acceptable."),
    _r("CM-003", "disease/phenotype distinction clear-cut",
       "Same shape as CM-002 but the distinction is unambiguous. Rejection ground.",
       blocks=True, rejects=True),
    _r("CM-004", "finding-vs-disorder conflation",
       "Target separates clinical finding from disorder, as SNOMED does. Conflated "
       "deliberately."),
    _r("CM-005", "etiological vs phenomenological divergence",
       "The same clinical picture is individuated by cause in one resource and by "
       "presentation in the other. Conflated where the use case does not distinguish them."),
    _r("CM-006", "susceptibility vs disease",
       "Subject is a susceptibility or predisposition locus; object is the clinical "
       "disease. Rejection ground — these are not the same entity.",
       blocks=True, rejects=True),
    _r("CM-007", "gene-centric vs disease-centric",
       "One side denotes a gene or molecular defect, the other a disease. Rejection ground.",
       blocks=True, rejects=True),
    _r("CM-008", "cross-species analog",
       "Entities are analogous across organisms rather than identical. Conflate only if the "
       "use case explicitly permits it."),
    _r("CM-009", "injury/event vs inherited condition",
       "Target rubric denotes an acute event or procedural complication where the subject "
       "denotes an inherited trait. Rejection ground.", blocks=True, rejects=True),
]

REGISTRY: dict[str, Rule] = {
    r.id: r for r in EV_RULES + SC_RULES + PR_RULES + ST_RULES + RJ_RULES + CM_RULES
}

FAMILIES = ("EV", "SC", "PR", "ST", "RJ", "CM")


def rule(rule_id: str) -> Rule:
    """Look up a rule by id.

    Args:
        rule_id: A bare id (``EV-001``) or a CURIE (``maprule:EV-001``).

    Returns:
        The :class:`Rule`.

    Raises:
        KeyError: If the id is not in the registry. Unknown rule ids are a defect, not a
            free-text field: an audit trail citing a rule that does not exist is worse
            than one citing none.

    Example:
        >>> rule("maprule:RJ-001").family
        'RJ'
    """
    key = rule_id.split(":")[-1].strip()
    if key not in REGISTRY:
        raise KeyError(f"unknown curation rule {rule_id!r}")
    return REGISTRY[key]


def known(rule_id: str) -> bool:
    """Return whether a rule id is in the registry.

    Example:
        >>> known("EV-001"), known("ZZ-999")
        (True, False)
    """
    return rule_id.split(":")[-1].strip() in REGISTRY


def family_ids(family: str) -> list[str]:
    """Return every rule id in a family, sorted.

    Example:
        >>> family_ids("RJ")
        ['RJ-001', 'RJ-002', 'RJ-003', 'RJ-004', 'RJ-005']
    """
    return sorted(r.id for r in REGISTRY.values() if r.family == family)


def render_rule_text(rule_ids: list[str]) -> str:
    """Render rule ids as an SSSOM ``curation_rule_text`` value.

    Unknown ids are dropped rather than emitted, and duplicates collapse while preserving
    first-seen order.

    Args:
        rule_ids: Rule ids or CURIEs.

    Returns:
        A ``' | '``-separated citation string.

    Example:
        >>> render_rule_text(["EV-001", "PR-001"])
        'maprule:EV-001 exact match on primary label | maprule:PR-001 same real-world concept'
        >>> render_rule_text([])
        ''
    """
    seen: list[str] = []
    for rid in rule_ids:
        key = rid.split(":")[-1].strip()
        if key in REGISTRY and key not in seen:
            seen.append(key)
    return " | ".join(REGISTRY[k].cite() for k in seen)


def render_rule_curies(rule_ids: list[str]) -> str:
    """Render rule ids as an SSSOM ``curation_rule`` value: CURIEs only, pipe-separated.

    ``curation_rule`` is URI-valued and multivalued, so this is what a mapping row carries.
    The titles live once, in the published value set, instead of once per row: the full-text
    form made a 32,109-row file mostly repeated English and gave no machine anything to join
    on. Unknown ids are dropped rather than emitted — an unresolvable URI in a URI-valued slot
    is worse than a missing one.

    Example:
        >>> render_rule_curies(["EV-001", "PR-001"])
        'maprule:EV-001|maprule:PR-001'
        >>> render_rule_curies(["maprule:SC-002", "PR-002", "SC-002"])
        'maprule:SC-002|maprule:PR-002'
        >>> render_rule_curies(["NOPE-9"])
        ''
        >>> render_rule_curies([])
        ''
    """
    seen: list[str] = []
    for rid in rule_ids:
        key = rid.split(":")[-1].strip()
        if key in REGISTRY and key not in seen:
            seen.append(key)
    return "|".join(REGISTRY[k].curie for k in seen)


def blocks_exact_match(rule_ids: list[str]) -> bool:
    """Return whether any cited rule forbids ``skos:exactMatch``.

    Example:
        >>> blocks_exact_match(["EV-001"])
        False
        >>> blocks_exact_match(["EV-001", "SC-001"])
        True
    """
    return any(known(r) and rule(r).blocks_exact_match for r in rule_ids)


def prompt_digest(families: tuple[str, ...] = FAMILIES) -> str:
    """Render the registry compactly for inclusion in an LLM system prompt.

    Example:
        >>> "SC-007" in prompt_digest(("SC",))
        True
        >>> prompt_digest(("RJ",)).splitlines()[0]
        'RJ — rejection grounds'
    """
    headings = {
        "EV": "EV — evidence for sameness",
        "SC": "SC — scope, granularity and undefined subsets",
        "PR": "PR — predicate selection",
        "ST": "ST — structural integrity",
        "RJ": "RJ — rejection grounds",
        "CM": "CM — conceptual model conflation (disease/phenotype)",
    }
    out: list[str] = []
    for fam in families:
        out.append(headings[fam])
        for rid in family_ids(fam):
            r = REGISTRY[rid]
            out.append(f"  {r.id}  {r.title}: {r.meaning}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
