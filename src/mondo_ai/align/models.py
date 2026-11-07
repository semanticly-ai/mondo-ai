"""Data model for the alignment pipeline.

Plain dataclasses, not Pydantic, for everything on the hot path: an ICD-10-CM run holds
~100k :class:`Concept` objects and considers millions of pairs, and validation overhead at
that scale buys nothing that the tests do not already buy. Pydantic is used only where
data crosses a trust boundary — the agent's JSON output (:mod:`mondo_ai.align.agent`) and
the run config (:mod:`mondo_ai.align.config`).

Example:
    >>> c = Concept(id="MONDO:0007947", label="Marfan syndrome")
    >>> c.all_strings()
    [('Marfan syndrome', 'label')]
    >>> c2 = Concept(id="ICD10CM:Q87.4", label="Marfan's syndrome",
    ...              exact_synonyms=["Marfan syndrome"])
    >>> [f for _, f in c2.all_strings()]
    ['label', 'exactSynonym']
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- predicates ------------------------------------------------------------------------

EXACT_MATCH = "skos:exactMatch"
BROAD_MATCH = "skos:broadMatch"
NARROW_MATCH = "skos:narrowMatch"
CLOSE_MATCH = "skos:closeMatch"
RELATED_MATCH = "skos:relatedMatch"
NO_TERM_FOUND = "sssom:NoTermFound"

#: Agent decision vocabulary -> SSSOM predicate. ``none`` means "assert nothing", which is
#: a decision in its own right and is emitted as ``sssom:NoTermFound``.
DECISION_TO_PREDICATE = {
    "exact": EXACT_MATCH,
    "broad": BROAD_MATCH,
    "narrow": NARROW_MATCH,
    "close": CLOSE_MATCH,
    "related": RELATED_MATCH,
    "none": NO_TERM_FOUND,
}


class ReviewTier(str, Enum):
    """How much human attention a row needs before it can be believed.

    ``AUTO_ACCEPT`` does not mean "curated" — under the Mondo tiering policy every row this
    pipeline emits is Tier 3 (AI-curated). It means no expert hour would change it.

    ``QUEUED`` and ``EXPERT_REVIEW`` are deliberately separate, and keeping them separate is
    what makes the triage table worth reading. ``EXPERT_REVIEW`` means *an adjudicator looked
    and a human has to decide*. ``QUEUED`` means *nobody has looked yet* — the ladder handed
    the item over and the adjudication budget did not reach it. Collapsing the two made 89% of
    a full Mondo run read as "expert review", which is true of neither and useless as a queue.
    """

    AUTO_ACCEPT = "AUTO_ACCEPT"
    SPOT_CHECK = "SPOT_CHECK"
    EXPERT_REVIEW = "EXPERT_REVIEW"
    QUEUED = "QUEUED"
    REJECTED = "REJECTED"
    NO_MATCH = "NO_MATCH"


# --- concepts --------------------------------------------------------------------------


@dataclass(slots=True)
class Concept:
    """One class from a resource, reduced to what alignment needs.

    Attributes:
        id: CURIE, e.g. ``MONDO:0007947`` or ``ICD10CM:Q87.4``.
        label: Primary label. May be empty for malformed sources.
        definition: Textual definition, where the source has one.
        exact_synonyms: Synonyms the source marks as exact.
        other_synonyms: Broad, narrow, related or unqualified synonyms.
        parents: CURIEs of named direct superclasses.
        children: CURIEs of named direct subclasses; filled in by the loader.
        xrefs: Cross-references as CURIEs, used for proxy evidence.
        obsolete: Whether the source marks the class deprecated.
        n_descendants: Transitive descendant count; drives the impact term in triage.
        extra: Source-specific leftovers, kept so nothing is silently discarded.
    """

    id: str
    label: str = ""
    definition: str = ""
    exact_synonyms: list[str] = field(default_factory=list)
    other_synonyms: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    xrefs: list[str] = field(default_factory=list)
    obsolete: bool = False
    n_descendants: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    def all_strings(self) -> list[tuple[str, str]]:
        """Return every string the concept can be matched on, with its field.

        Field provenance is carried through because evidence strength differs: a hit on
        the primary label is ``EV-001``, on an exact synonym ``EV-003``, on anything else
        ``EV-007``.

        Example:
            >>> Concept(id="X:1", label="a", exact_synonyms=["b"],
            ...         other_synonyms=["c"]).all_strings()
            [('a', 'label'), ('b', 'exactSynonym'), ('c', 'relatedSynonym')]
        """
        out: list[tuple[str, str]] = []
        if self.label:
            out.append((self.label, "label"))
        out.extend((s, "exactSynonym") for s in self.exact_synonyms if s)
        out.extend((s, "relatedSynonym") for s in self.other_synonyms if s)
        return out


@dataclass(slots=True)
class ResourceSet:
    """A loaded resource: its concepts plus the provenance needed to reproduce the run.

    Example:
        >>> rs = ResourceSet(name="mondo", prefix="MONDO",
        ...                  concepts={"MONDO:1": Concept(id="MONDO:1", label="x")})
        >>> len(rs), rs.prefix
        (1, 'MONDO')
    """

    name: str
    prefix: str
    concepts: dict[str, Concept] = field(default_factory=dict)
    source_path: str = ""
    source_url: str = ""
    sha256: str = ""
    version: str = ""
    retrieved: str = ""

    def __len__(self) -> int:
        return len(self.concepts)

    def active(self) -> list[Concept]:
        """Return non-obsolete concepts that carry a label.

        Example:
            >>> rs = ResourceSet(name="r", prefix="P", concepts={
            ...     "P:1": Concept(id="P:1", label="a"),
            ...     "P:2": Concept(id="P:2", label="b", obsolete=True),
            ...     "P:3": Concept(id="P:3")})
            >>> [c.id for c in rs.active()]
            ['P:1']
        """
        return [c for c in self.concepts.values() if c.label and not c.obsolete]


# --- evidence --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Signal:
    """One piece of evidence that a subject and an object might correspond.

    Attributes:
        kind: Generator that produced it (``lexical_exact``, ``token_retrieval``, ...).
        rule_id: The ``EV-*`` rule this signal instantiates.
        certainty: Strength in [0,1]; the deterministic ladder uses the max over signals.
        detail: Human-readable specifics — the matched string, the rewrite applied.
        subject_field: Which subject string matched (``label`` / ``exactSynonym`` / ...).
        object_field: Which object string matched.
        transform: Lexical rewrite rule id, where one was applied.

    Example:
        >>> Signal("lexical_exact", "EV-001", 1.0, "marfan syndrome").rule_id
        'EV-001'
    """

    kind: str
    rule_id: str
    certainty: float
    detail: str = ""
    subject_field: str = ""
    object_field: str = ""
    transform: str = ""


@dataclass(slots=True)
class Candidate:
    """A subject/object pair under consideration, with all evidence gathered for it.

    Attributes:
        object_id: The candidate object CURIE.
        signals: Every signal that fired for this pair.
        flags: Scope/conceptual-model flags, as ``SC-*``/``CM-*``/``ST-*`` rule ids.
        tier: Lexical tier the best signal came from (1 raw, 2 normalised, 3 synonym,
            4 rewrite, 9 retrieval only). Lower is stronger.
        score: Retrieval score, where retrieval contributed.
    """

    object_id: str
    signals: list[Signal] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    tier: int = 9
    score: float = 0.0

    def best_certainty(self) -> float:
        """Return the strongest signal certainty.

        Example:
            >>> c = Candidate("X:1", [Signal("a", "EV-006", 0.4),
            ...                       Signal("b", "EV-002", 0.95)])
            >>> c.best_certainty()
            0.95
            >>> Candidate("X:2").best_certainty()
            0.0
        """
        return max((s.certainty for s in self.signals), default=0.0)

    def rule_ids(self) -> list[str]:
        """Return evidence and flag rule ids, de-duplicated, evidence first.

        Example:
            >>> Candidate("X:1", [Signal("a", "EV-001", 1.0)],
            ...           flags=["SC-001"]).rule_ids()
            ['EV-001', 'SC-001']
        """
        out: list[str] = []
        for s in self.signals:
            if s.rule_id not in out:
                out.append(s.rule_id)
        for f in self.flags:
            if f not in out:
                out.append(f)
        return out

    def transforms(self) -> list[str]:
        """Return the distinct lexical rewrites that contributed.

        Example:
            >>> Candidate("X:1", [Signal("a", "EV-004", 0.97,
            ...                          transform="strip_possessive")]).transforms()
            ['strip_possessive']
        """
        out: list[str] = []
        for s in self.signals:
            if s.transform and s.transform not in out:
                out.append(s.transform)
        return out


# --- verdicts --------------------------------------------------------------------------


@dataclass(slots=True)
class Adjudication:
    """A verdict on one subject, from one adjudicator.

    Attributes:
        source: ``deterministic`` or ``agent``.
        decision: One of :data:`DECISION_TO_PREDICATE`'s keys.
        object_id: Chosen object, or empty for ``none``.
        confidence: The adjudicator's own confidence in [0,1].
        rule_ids: Rules the verdict rests on.
        justification: One concrete sentence naming the object.
        decision_path: Ladder rung (``D1``…) or agent path (``A-batch``).
        alternatives: Other ids that were seriously considered.
        conflation: ``CM-*`` ids the verdict knowingly crossed.
        needs_expert: The adjudicator's own request for a human.
        expert_question: What to ask that human.
        agreement: Self-consistency share across samples, where sampled.
    """

    source: str
    decision: str
    object_id: str = ""
    confidence: float = 0.0
    rule_ids: list[str] = field(default_factory=list)
    justification: str = ""
    decision_path: str = ""
    alternatives: list[str] = field(default_factory=list)
    conflation: list[str] = field(default_factory=list)
    needs_expert: bool = False
    expert_question: str = ""
    agreement: float = 1.0
    #: True when the ladder handed this item over and no adjudicator examined it. Distinct
    #: from ``needs_expert``, which is a verdict; this is the absence of one.
    unexamined: bool = False

    @property
    def predicate(self) -> str:
        """Return the SSSOM predicate for this decision.

        Example:
            >>> Adjudication("deterministic", "exact").predicate
            'skos:exactMatch'
            >>> Adjudication("agent", "none").predicate
            'sssom:NoTermFound'
        """
        return DECISION_TO_PREDICATE.get(self.decision, NO_TERM_FOUND)

    @property
    def asserts_mapping(self) -> bool:
        """Return whether the verdict names an object at all.

        Example:
            >>> Adjudication("agent", "exact", "X:1").asserts_mapping
            True
            >>> Adjudication("agent", "none").asserts_mapping
            False
        """
        return self.decision != "none" and bool(self.object_id)


@dataclass(slots=True)
class MappingRecord:
    """The full audit trail for one subject: candidates, verdicts, triage.

    This is what lands in ``records.jsonl``. The SSSOM row is a projection of it.
    """

    subject_id: str
    subject_label: str = ""
    subject_definition: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    deterministic: Adjudication | None = None
    agent: Adjudication | None = None
    final: Adjudication | None = None
    review_tier: ReviewTier = ReviewTier.NO_MATCH
    review_priority: float = 0.0
    priority_components: dict[str, float] = field(default_factory=dict)
    ambiguity_count: int = 0
    structural_flags: list[str] = field(default_factory=list)
    object_label: str = ""
    #: Boomer's findings for this mapping, or None when Boomer did not see it. An absent
    #: probability and a probability of zero are different claims, so this stays None rather
    #: than defaulting to an empty distribution.
    boomer: dict[str, Any] | None = None

    def candidate(self, object_id: str) -> Candidate | None:
        """Return the candidate with the given object id, if present.

        Example:
            >>> r = MappingRecord("M:1", candidates=[Candidate("X:1")])
            >>> r.candidate("X:1").object_id
            'X:1'
            >>> r.candidate("X:9") is None
            True
        """
        for c in self.candidates:
            if c.object_id == object_id:
                return c
        return None

    def disagreement(self) -> float:
        """Return how far the two adjudicators diverge, in [0,1].

        ``0.0`` when they agree on object and predicate (or only one ran), ``0.5`` when
        they agree on the object but not the predicate, ``1.0`` when they name different
        objects. Disagreement is the highest-yield thing a reviewer can look at, which is
        why it carries the largest weight in :mod:`mondo_ai.align.triage`.

        Example:
            >>> a = Adjudication("deterministic", "exact", "X:1")
            >>> b = Adjudication("agent", "exact", "X:1")
            >>> MappingRecord("M:1", deterministic=a, agent=b).disagreement()
            0.0
            >>> c = Adjudication("agent", "broad", "X:1")
            >>> MappingRecord("M:1", deterministic=a, agent=c).disagreement()
            0.5
            >>> d = Adjudication("agent", "exact", "X:2")
            >>> MappingRecord("M:1", deterministic=a, agent=d).disagreement()
            1.0
            >>> MappingRecord("M:1", deterministic=a).disagreement()
            0.0
        """
        if self.deterministic is None or self.agent is None:
            return 0.0
        if self.deterministic.object_id != self.agent.object_id:
            return 1.0
        if self.deterministic.decision != self.agent.decision:
            return 0.5
        return 0.0
