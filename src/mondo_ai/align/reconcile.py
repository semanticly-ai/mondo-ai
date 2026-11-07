"""Reconciliation: the run's discoveries against existing mapping sets, pair by pair.

Discovery answers "what does the evidence say for each subject?" with one verdict per
subject. A surface like Mondo × SNOMED CT also starts from mapping sets that already exist —
Mondo's own cross-references, OMOP's ``Maps to`` rows — and the deliverable is every pair
anyone has proposed, each with the evidence for and against it. That is this stage.

Three commitments, in the order they matter:

1. **Seeds never enter discovery.** The ladder, the agent and the evaluation do not see them
   (:class:`~mondo_ai.align.config.ReconcileConfig`), so when a seed pair is backed by a
   string identity that backing is independent. Seeds meet the run only here.
2. **Agreement between seeds is not corroboration.** OMOP's Mondo mappings were built after
   Mondo cross-referenced SNOMED, and 6,323 of the two sets' pairs are identical; two
   sources repeating one assertion are one source. Only lexical evidence or an adjudicator
   *corroborates* a seed, so a pair that both seeds assert and no string supports is
   ``seed_only``, not ``corroborated``.
3. **Disagreement is the finding.** The statuses below are ordered so that the first problem
   found names the row, and every status other than ``corroborated``, ``seed_only`` and
   ``discovery_only`` is a question a curator has to answer.

========================  ==============  =====================================================
status                    tier            meaning
========================  ==============  =====================================================
``inactive_subject``      REJECTED        the subject is obsolete or absent in this release
``unknown_target``        REJECTED        the object is not in the extracted vocabulary (ST-003)
``inactive_target``       REJECTED        the object is retired in SNOMED (ST-004); successor named
``predicate_conflict``    EXPERT_REVIEW   seeds (or the agent) assert different predicates
``rule_conflict``         EXPERT_REVIEW   an exact claim against a blocking scope flag
``undeclared_conflation`` EXPERT_REVIEW   a CM-* flag the run does not permit
``object_conflict``       EXPERT_REVIEW   the subject is claimed exact to two objects (ST-001)
``reverse_proxy_merge``   EXPERT_REVIEW   the object is claimed exact by two subjects (ST-002)
``predicate_vs_lexical``  EXPERT_REVIEW   a non-exact claim where the strings are identical
``consistency_conflict``  EXPERT_REVIEW   Boomer overruled an otherwise clean row
``corroborated``          AUTO_ACCEPT     a seed exact claim backed by an unblocked string identity
                          / SPOT_CHECK    (SPOT_CHECK when a subject flag or permitted conflation)
``seed_only``             SPOT_CHECK      a seed claim with no independent backing, and nothing against it
``discovery_only``        run's own tier  a discovery no seed asserts: a candidate addition
========================  ==============  =====================================================

Example:
    >>> STATUS_TIER["seed_only"].value
    'SPOT_CHECK'
    >>> signal_tier(Signal("lexical_exact", "EV-003", 1.0))
    3
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from mondo_ai.align.candidates import CandidateGenerator
from mondo_ai.align.config import SeedSpec
from mondo_ai.align.flags import BLOCKING_FLAGS, subject_flags
from mondo_ai.align.index import build_index
from mondo_ai.align.models import (
    EXACT_MATCH,
    NO_TERM_FOUND,
    Adjudication,
    Candidate,
    Concept,
    MappingRecord,
    ResourceSet,
    ReviewTier,
    Signal,
)
from mondo_ai.align.rules import render_rule_curies

#: Field the matched string came from -> the SSSOM match-field property.
FIELD_PROPERTY = {
    "label": "rdfs:label",
    "exactSynonym": "oio:hasExactSynonym",
    "relatedSynonym": "oio:hasRelatedSynonym",
}

STATUS_TIER = {
    "inactive_subject": ReviewTier.REJECTED,
    "unknown_target": ReviewTier.REJECTED,
    "inactive_target": ReviewTier.REJECTED,
    "predicate_conflict": ReviewTier.EXPERT_REVIEW,
    "rule_conflict": ReviewTier.EXPERT_REVIEW,
    "undeclared_conflation": ReviewTier.EXPERT_REVIEW,
    "object_conflict": ReviewTier.EXPERT_REVIEW,
    "reverse_proxy_merge": ReviewTier.EXPERT_REVIEW,
    "predicate_vs_lexical": ReviewTier.EXPERT_REVIEW,
    "consistency_conflict": ReviewTier.EXPERT_REVIEW,
    "corroborated": ReviewTier.AUTO_ACCEPT,
    "seed_only": ReviewTier.SPOT_CHECK,
}

_MAPPING_PREDICATES = frozenset({
    "skos:exactMatch", "skos:broadMatch", "skos:narrowMatch", "skos:closeMatch",
    "skos:relatedMatch",
})


# --- seeds -------------------------------------------------------------------------------


@dataclass(slots=True)
class SeedRow:
    """One row of a seed mapping set, reduced to what reconciliation reads."""

    seed: str
    subject_id: str
    predicate_id: str
    object_id: str
    confidence: float
    justification: str = ""
    comment: str = ""
    mapping_source: str = ""


def load_seed(spec: SeedSpec, subject_prefix: str, object_prefix: str) -> list[SeedRow]:
    """Read one seed SSSOM file, keeping rows between the run's two vocabularies.

    The first row for a pair wins; a seed asserting two predicates for one pair would be a
    defect in the seed, and is not this stage's to resolve silently, so it is kept as the
    first-listed one and the duplicate count is not hidden — it is simply the row count
    difference reported in the run report.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "s.sssom.tsv"
        >>> _ = p.write_text("# mapping_set_id: x\\n"
        ...     "subject_id\\tpredicate_id\\tobject_id\\tconfidence\\n"
        ...     "MONDO:1\\tskos:exactMatch\\tSCTID:9\\t\\n"
        ...     "MONDO:1\\tskos:exactMatch\\tDOID:9\\t0.9\\n"
        ...     "MONDO:2\\tskos:broadMatch\\tSCTID:8\\t0.8\\n")
        >>> rows = load_seed(SeedSpec(name="m", path=p, default_confidence=0.95),
        ...                  "MONDO", "SCTID")
        >>> [(r.subject_id, r.object_id, r.confidence) for r in rows]
        [('MONDO:1', 'SCTID:9', 0.95), ('MONDO:2', 'SCTID:8', 0.8)]
    """
    with open(spec.path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    out: list[SeedRow] = []
    seen: set[tuple[str, str]] = set()
    for row in csv.DictReader(lines, delimiter="\t"):
        s, o, p = row.get("subject_id", ""), row.get("object_id", ""), row.get("predicate_id", "")
        if not (s.startswith(f"{subject_prefix}:") and o.startswith(f"{object_prefix}:")):
            continue
        if p not in _MAPPING_PREDICATES or (s, o) in seen:
            continue
        seen.add((s, o))
        conf = (row.get("confidence") or "").strip()
        out.append(SeedRow(
            seed=spec.name, subject_id=s, predicate_id=p, object_id=o,
            confidence=float(conf) if conf else spec.default_confidence,
            justification=row.get("mapping_justification", "") or "semapv:UnspecifiedMatching",
            comment=row.get("comment", "") or "",
            mapping_source=spec.mapping_source,
        ))
    return out


# --- lexical evidence for one pair -------------------------------------------------------


def signal_tier(signal: Signal) -> int:
    """Return the lexical tier a single signal stands for (lower is stronger).

    Example:
        >>> signal_tier(Signal("lexical_exact", "EV-001", 1.0))
        1
        >>> signal_tier(Signal("lexical_variant", "EV-004", 0.97))
        4
        >>> signal_tier(Signal("lexical_exact", "EV-007", 0.6))
        9
        >>> signal_tier(Signal("token_retrieval", "EV-006", 0.4))
        9
    """
    if signal.kind == "lexical_exact":
        return {"EV-001": 1, "EV-002": 2, "EV-003": 3}.get(signal.rule_id, 9)
    if signal.kind == "lexical_variant" and signal.rule_id == "EV-004":
        return 4
    return 9


def best_lexical(candidate: Candidate | None) -> Signal | None:
    """Return the strongest string-identity signal for a candidate, or None.

    Example:
        >>> c = Candidate("X:1", [Signal("lexical_variant", "EV-004", 0.97, "a"),
        ...                       Signal("lexical_exact", "EV-003", 1.0, "b")])
        >>> best_lexical(c).detail
        'b'
        >>> best_lexical(Candidate("X:2", [Signal("token_retrieval", "EV-006", 0.4)])) is None
        True
    """
    if candidate is None:
        return None
    ranked = sorted(
        (s for s in candidate.signals if signal_tier(s) <= 4),
        key=lambda s: (signal_tier(s), -s.certainty, s.detail, s.subject_field,
                       s.object_field, s.transform),
    )
    return ranked[0] if ranked else None


def pair_candidates(
    generator: CandidateGenerator,
    subject: Concept,
    object_ids: list[str],
) -> dict[str, Candidate]:
    """Run the discovery generator for one subject against a fixed set of objects.

    The same code path as discovery — same rewrites, same flags — over an index holding only
    the objects in question, so a seed pair's lexical evidence is computed exactly as a
    discovered pair's would be, including for retired objects the discovery index never
    holds.

    Example:
        >>> objs = ResourceSet(name="s", prefix="SCTID", concepts={
        ...     "SCTID:1": Concept(id="SCTID:1", label="Marfan's syndrome"),
        ...     "SCTID:2": Concept(id="SCTID:2", label="Cholera")})
        >>> gen = CandidateGenerator(build_index([]), objs)
        >>> got = pair_candidates(gen, Concept(id="M:1", label="Marfan syndrome"),
        ...                       ["SCTID:1", "SCTID:9"])
        >>> sorted(got), got["SCTID:1"].tier
        (['SCTID:1'], 4)
    """
    concepts = [generator.objects.concepts[o] for o in object_ids
                if o in generator.objects.concepts]
    if not concepts:
        return {}
    n = len(concepts) + 1
    local = CandidateGenerator(
        build_index(concepts), generator.objects, object_prefix=generator.object_prefix,
        max_candidates=n, max_per_category=n, retrieval_limit=n,
    )
    local._flag_cache = generator._flag_cache
    found = local.generate(subject)
    local.annotate_coextensive(subject, found)
    wanted = set(object_ids)
    return {c.object_id: c for c in found if c.object_id in wanted}


# --- consolidated pairs ------------------------------------------------------------------


@dataclass
class ConsolidatedMapping:
    """One subject/object pair with everything any source said about it."""

    subject_id: str
    object_id: str
    subject_label: str = ""
    object_label: str = ""
    seeds: dict[str, SeedRow] = field(default_factory=dict)
    discovery: Adjudication | None = None
    discovery_tier: str = ""
    candidate: Candidate | None = None
    lexical: Signal | None = None
    subject_flags: list[str] = field(default_factory=list)
    object_flags: list[str] = field(default_factory=list)
    predicate_id: str = ""
    confidence: float = 0.0
    status: str = ""
    review_tier: ReviewTier = ReviewTier.SPOT_CHECK
    rule_ids: list[str] = field(default_factory=list)
    structural_flags: list[str] = field(default_factory=list)
    object_status: str = ""
    replacement: str = ""
    comment: str = ""
    boomer: dict[str, Any] | None = None

    @property
    def lexical_tier(self) -> int:
        """Tier of the best string identity between the pair, 9 when there is none."""
        return signal_tier(self.lexical) if self.lexical else 9

    @property
    def blocking(self) -> list[str]:
        return [f for f in self.object_flags if f in BLOCKING_FLAGS]

    @property
    def agent_view(self) -> Adjudication | None:
        """The discovery verdict when an agent produced it; the ladder's is the lexical line."""
        if self.discovery is not None and self.discovery.source == "agent":
            return self.discovery
        return None

    @property
    def evidence_sources(self) -> list[str]:
        out = ["lexical"] if self.lexical is not None else []
        out.extend(self.seeds)
        if self.agent_view is not None:
            out.append("agent")
        return out

    @property
    def lexical_supports_exact(self) -> bool:
        return self.lexical is not None and not self.blocking

    @property
    def claims_exact(self) -> bool:
        """Whether this pair counts as an exact claim for the set-level ST checks."""
        return (self.predicate_id == EXACT_MATCH and self.object_status == ""
                and self.status not in ("inactive_subject", "unknown_target"))


def _sentence(m: ConsolidatedMapping) -> str:
    """Compose the one-sentence comment for a consolidated row."""
    seeds = ", ".join(f"{name} asserts {row.predicate_id}" for name, row in m.seeds.items())
    lex = ""
    if m.lexical is not None:
        lex = (f"the subject's {m.lexical.subject_field} matches the object's "
               f"{m.lexical.object_field} {m.lexical.detail!r} ({m.lexical.rule_id}"
               + (f", after {m.lexical.transform}" if m.lexical.transform else "") + ")")
    why = {
        "inactive_subject": "the subject is not a current term in this Mondo release",
        "unknown_target": "the object is not in the extracted SNOMED CT vocabulary",
        "inactive_target": (
            f"the object is retired in SNOMED CT (invalid_reason {m.object_status})"
            + (f"; its successor is {m.replacement}" if m.replacement else "")),
        "predicate_conflict": "the sources disagree on the predicate",
        "rule_conflict": f"an exact claim against {', '.join(m.blocking)}",
        "undeclared_conflation": "the pair crosses a conflation this run does not permit",
        "object_conflict": "the subject is claimed exact to more than one object (ST-001)",
        "reverse_proxy_merge": "the object is claimed exact by more than one subject (ST-002)",
        "predicate_vs_lexical": "a non-exact claim although the strings are identical",
        "consistency_conflict": "consistency conditioning overruled the asserted predicate",
        "corroborated": "independently corroborated by string identity",
        "seed_only": "no independent string evidence; resting on the seed alone",
        "discovery_only": "found by the run; no seed asserts it",
    }[m.status]
    parts = [p for p in (seeds, lex) if p]
    return f"{m.status}: {why}." + (f" Evidence: {'; '.join(parts)}." if parts else "")


def reconcile(
    records: list[MappingRecord],
    seeds: list[list[SeedRow]],
    subjects: ResourceSet,
    objects: ResourceSet,
    generator: CandidateGenerator,
    *,
    permitted_conflations: list[str],
) -> list[ConsolidatedMapping]:
    """Build the consolidated, pair-level mapping set.

    Args:
        records: The triaged discovery records.
        seeds: Each seed set's rows, in trust order.
        subjects: The subject resource.
        objects: The object resource, including obsolete concepts.
        generator: The discovery candidate generator, reused for per-pair evidence.
        permitted_conflations: ``CM-*`` ids the run permits.

    Returns:
        One :class:`ConsolidatedMapping` per pair, sorted by subject then object.
    """
    pairs: dict[tuple[str, str], ConsolidatedMapping] = {}

    def pair(s: str, o: str) -> ConsolidatedMapping:
        key = (s, o)
        if key not in pairs:
            pairs[key] = ConsolidatedMapping(subject_id=s, object_id=o)
        return pairs[key]

    for seed_rows in seeds:
        for row in seed_rows:
            pair(row.subject_id, row.object_id).seeds[row.seed] = row
    for r in records:
        if r.final is not None and r.final.asserts_mapping:
            m = pair(r.subject_id, r.final.object_id)
            m.discovery = r.final
            m.discovery_tier = r.review_tier.value

    by_subject: dict[str, list[str]] = defaultdict(list)
    for s, o in pairs:
        by_subject[s].append(o)
    for s, objs in by_subject.items():
        subject = subjects.concepts.get(s)
        found = pair_candidates(generator, subject, sorted(objs)) if subject else {}
        sflags = subject_flags(subject) if subject else []
        for o in objs:
            m = pairs[(s, o)]
            m.subject_label = subject.label if subject else ""
            obj = objects.concepts.get(o)
            m.object_label = obj.label if obj else ""
            m.candidate = found.get(o)
            m.lexical = best_lexical(m.candidate)
            # From the detectors, not from the candidate: a seed pair with no string in
            # common has no candidate, and its object's scope flags matter all the same.
            m.object_flags = (list(m.candidate.flags) if m.candidate is not None
                              else list(generator.flags_for(o)))
            m.subject_flags = sflags
            m.object_status = ("missing" if obj is None
                               else obj.extra.get("invalid_reason", "D") if obj.obsolete else "")
            m.replacement = obj.extra.get("replaced_by", "") if obj is not None else ""
            _propose(m)
            if subject is None or subject.obsolete:
                m.status = "inactive_subject"

    claims_by_subject: dict[str, set[str]] = defaultdict(set)
    claims_by_object: dict[str, set[str]] = defaultdict(set)
    for m in pairs.values():
        if m.claims_exact:
            claims_by_subject[m.subject_id].add(m.object_id)
            claims_by_object[m.object_id].add(m.subject_id)

    for m in pairs.values():
        if m.claims_exact and len(claims_by_subject[m.subject_id]) > 1:
            m.structural_flags.append("ST-001")
        if m.claims_exact and len(claims_by_object[m.object_id]) > 1:
            m.structural_flags.append("ST-002")
        _classify(m, permitted_conflations)
        m.comment = _sentence(m)

    return [pairs[k] for k in sorted(pairs)]


def _propose(m: ConsolidatedMapping) -> None:
    """Choose the predicate and confidence a pair is proposed with.

    Seeds in trust order decide the predicate; without seeds, the discovery verdict does.
    Confidence is the strongest line that supports that predicate: a seed asserting it, the
    discovery verdict asserting it, or — for an exact proposal — an unblocked string identity.
    """
    if m.seeds:
        m.predicate_id = next(iter(m.seeds.values())).predicate_id
    elif m.discovery is not None:
        m.predicate_id = m.discovery.predicate
    support = [row.confidence for row in m.seeds.values() if row.predicate_id == m.predicate_id]
    if m.discovery is not None and m.discovery.predicate == m.predicate_id:
        support.append(m.discovery.confidence)
    if m.predicate_id == EXACT_MATCH and m.lexical_supports_exact and m.lexical is not None:
        support.append(m.lexical.certainty)
    m.confidence = round(max(support, default=0.0), 4)


def _classify(m: ConsolidatedMapping, permitted: list[str]) -> None:
    """Assign status, tier and cited rules; first problem found names the row."""
    lex_rules = m.candidate.rule_ids() if m.candidate is not None else []
    conflations = sorted({f for f in m.object_flags if f.startswith("CM-")})
    asserted = {row.predicate_id for row in m.seeds.values()}
    if m.agent_view is not None:
        asserted.add(m.agent_view.predicate)

    status, rules = "", list(lex_rules)
    if m.status == "inactive_subject":
        status, rules = "inactive_subject", ["ST-004"]
    elif m.object_status == "missing":
        status, rules = "unknown_target", ["ST-003"]
    elif m.object_status:
        status, rules = "inactive_target", ["ST-004"] + lex_rules
    elif len(asserted) > 1:
        status = "predicate_conflict"
    elif m.predicate_id == EXACT_MATCH and m.blocking:
        status = "rule_conflict"
    elif any(c not in permitted for c in conflations):
        status = "undeclared_conflation"
    elif "ST-001" in m.structural_flags:
        status, rules = "object_conflict", ["ST-001"] + lex_rules
    elif "ST-002" in m.structural_flags:
        status, rules = "reverse_proxy_merge", ["ST-002"] + lex_rules
    elif m.predicate_id != EXACT_MATCH and m.lexical_supports_exact and m.lexical_tier <= 3:
        status = "predicate_vs_lexical"
    elif m.seeds and m.predicate_id == EXACT_MATCH and m.lexical_supports_exact:
        status, rules = "corroborated", lex_rules + ["PR-001"]
    elif m.seeds:
        status = "seed_only"
    else:
        status = "discovery_only"
        rules = list(m.discovery.rule_ids) if m.discovery is not None else lex_rules

    m.status = status
    m.rule_ids = list(dict.fromkeys(rules + m.subject_flags))
    if status == "discovery_only":
        m.review_tier = ReviewTier(m.discovery_tier) if m.discovery_tier else ReviewTier.QUEUED
    else:
        m.review_tier = STATUS_TIER[status]
        if status == "corroborated" and (m.subject_flags or conflations):
            m.review_tier = ReviewTier.SPOT_CHECK


def apply_boomer(
    mappings: list[ConsolidatedMapping],
    verdicts: dict[tuple[str, str], Any],
) -> int:
    """Attach Boomer's verdicts, escalating clean rows it overrules.

    Returns:
        The number of rows escalated.

    Example:
        >>> from mondo_ai.align.boomer import BoomerVerdict
        >>> m = ConsolidatedMapping("M:1", "S:1", predicate_id="skos:exactMatch",
        ...                         status="seed_only", review_tier=ReviewTier.SPOT_CHECK)
        >>> v = BoomerVerdict("M:1", "S:1", "subclass", 0.1, was_most_probable=False,
        ...                   clique_size=3)
        >>> apply_boomer([m], {("M:1", "S:1"): v})
        1
        >>> m.status, m.review_tier.value, m.boomer["predicate"]
        ('consistency_conflict', 'EXPERT_REVIEW', 'skos:broadMatch')
    """
    escalated = 0
    for m in mappings:
        v = verdicts.get((m.subject_id, m.object_id))
        if v is None:
            continue
        agrees = v.predicate == m.predicate_id
        m.boomer = {"predicate": v.predicate, "agrees": agrees, "clique_size": v.clique_size,
                    "clique_confidence": v.clique_confidence, "method": v.method}
        if not agrees and m.status in ("corroborated", "seed_only", "discovery_only"):
            m.status = "consistency_conflict"
            m.review_tier = ReviewTier.EXPERT_REVIEW
            m.comment = _sentence(m)
            escalated += 1
    return escalated


# --- emission ----------------------------------------------------------------------------

CONSOLIDATED_STANDARD = [
    "subject_id", "subject_label", "predicate_id", "object_id", "object_label",
    "mapping_justification", "confidence", "match_string", "subject_match_field",
    "object_match_field", "subject_preprocessing", "mapping_tool", "mapping_tool_version",
    "mapping_date", "curation_rule", "comment",
]

CONSOLIDATED_EXTENSIONS: dict[str, tuple[str, str]] = {
    "review_tier": ("http://w3id.org/mondo-ai/align/reviewTier", "xsd:string"),
    "reconciliation_status": (
        "http://w3id.org/mondo-ai/align/reconciliationStatus", "xsd:string"),
    "evidence_sources": ("http://w3id.org/mondo-ai/align/evidenceSources", "xsd:string"),
    "seed_predicates": ("http://w3id.org/mondo-ai/align/seedPredicates", "xsd:string"),
    "discovery_predicate": ("http://w3id.org/mondo-ai/align/discoveryPredicate", "xsd:string"),
    "discovery_path": ("http://w3id.org/mondo-ai/align/discoveryPath", "xsd:string"),
    "risk_flags": ("http://w3id.org/mondo-ai/align/riskFlags", "xsd:string"),
    "structural_flags": ("http://w3id.org/mondo-ai/align/structuralFlags", "xsd:string"),
    "object_status": ("http://w3id.org/mondo-ai/align/objectStatus", "xsd:string"),
    "replacement_object": ("http://w3id.org/mondo-ai/align/replacementObject", "xsd:string"),
    "boomer_predicate": ("http://w3id.org/mondo-ai/align/boomerPredicate", "xsd:string"),
    "boomer_agrees": ("http://w3id.org/mondo-ai/align/boomerAgrees", "xsd:boolean"),
    "mapping_tier": ("http://w3id.org/mondo-ai/align/mappingTier", "xsd:integer"),
}

EVIDENCE_STANDARD = [
    "subject_id", "subject_label", "predicate_id", "object_id", "object_label",
    "mapping_justification", "confidence", "match_string", "subject_match_field",
    "object_match_field", "subject_preprocessing", "mapping_source", "mapping_tool",
    "mapping_tool_version", "mapping_date", "curation_rule", "comment",
]

EVIDENCE_EXTENSIONS: dict[str, tuple[str, str]] = {
    "evidence_source": ("http://w3id.org/mondo-ai/align/evidenceSource", "xsd:string"),
    "risk_flags": ("http://w3id.org/mondo-ai/align/riskFlags", "xsd:string"),
}


def _justification(m: ConsolidatedMapping) -> str:
    """Return the row's ``mapping_justification``.

    Example:
        >>> m = ConsolidatedMapping("M:1", "S:1",
        ...     lexical=Signal("lexical_exact", "EV-001", 1.0, "x", "label", "label"))
        >>> _justification(m)
        'semapv:LexicalMatching'
        >>> m.seeds["mondo"] = SeedRow("mondo", "M:1", "skos:exactMatch", "S:1", 0.9,
        ...                            "semapv:ManualMappingCuration")
        >>> _justification(m)
        'semapv:CompositeMatching'
        >>> m.lexical = None
        >>> _justification(m)
        'semapv:ManualMappingCuration'
    """
    sources = m.evidence_sources
    if len(sources) > 1:
        return "semapv:CompositeMatching"
    if sources == ["lexical"]:
        return "semapv:LexicalMatching"
    if sources == ["agent"]:
        return "semapv:CompositeMatching"
    if m.seeds:
        return next(iter(m.seeds.values())).justification
    return "semapv:UnspecifiedMatching"


def _lexical_cells(signal: Signal | None) -> dict[str, str]:
    if signal is None:
        return {"match_string": "", "subject_match_field": "", "object_match_field": "",
                "subject_preprocessing": ""}
    return {
        "match_string": signal.detail,
        "subject_match_field": FIELD_PROPERTY.get(signal.subject_field, ""),
        "object_match_field": FIELD_PROPERTY.get(signal.object_field, ""),
        "subject_preprocessing": signal.transform,
    }


def consolidated_row(m: ConsolidatedMapping, version: str, today: str) -> dict[str, Any]:
    """Project one consolidated pair into its SSSOM row."""
    return {
        "subject_id": m.subject_id,
        "subject_label": m.subject_label,
        "predicate_id": m.predicate_id or NO_TERM_FOUND,
        "object_id": m.object_id,
        "object_label": m.object_label,
        "mapping_justification": _justification(m),
        "confidence": m.confidence,
        **_lexical_cells(m.lexical),
        "mapping_tool": "mondo-ai align",
        "mapping_tool_version": version,
        "mapping_date": today,
        "curation_rule": render_rule_curies(m.rule_ids),
        "comment": m.comment,
        "review_tier": m.review_tier.value,
        "reconciliation_status": m.status,
        "evidence_sources": "|".join(m.evidence_sources),
        "seed_predicates": "|".join(f"{k}={v.predicate_id}" for k, v in m.seeds.items()),
        "discovery_predicate": m.discovery.predicate if m.discovery else "",
        "discovery_path": m.discovery.decision_path if m.discovery else "",
        "risk_flags": "|".join(sorted(set(m.object_flags) | set(m.subject_flags))),
        "structural_flags": "|".join(m.structural_flags),
        "object_status": m.object_status,
        "replacement_object": m.replacement,
        "boomer_predicate": m.boomer["predicate"] if m.boomer else "",
        "boomer_agrees": m.boomer["agrees"] if m.boomer else "",
        "mapping_tier": 3,
    }


def evidence_rows(
    mappings: list[ConsolidatedMapping],
    records: list[MappingRecord],
    version: str,
    today: str,
) -> list[dict[str, Any]]:
    """Return the evidence ledger: one SSSOM row per pair per line of evidence.

    The SSSOM-native shape, the way OAK's lexmatch writes one row per matching field: a pair
    supported by a string identity, Mondo and OMOP has three rows, each with its own
    justification and provenance. Lexical rows are also written for every string-identity
    candidate the discovery run considered, so the ledger is a complete lexmatch over the
    run, not only over the pairs someone asserted.
    """
    out: list[dict[str, Any]] = []
    covered: set[tuple[str, str]] = set()

    def lexical_row(s: str, s_label: str, o: str, o_label: str, cand: Candidate,
                    sig: Signal) -> dict[str, Any]:
        return {
            "subject_id": s, "subject_label": s_label, "predicate_id": EXACT_MATCH,
            "object_id": o, "object_label": o_label,
            "mapping_justification": "semapv:LexicalMatching",
            "confidence": round(sig.certainty, 4), **_lexical_cells(sig),
            "mapping_source": "", "mapping_tool": "mondo-ai align",
            "mapping_tool_version": version, "mapping_date": today,
            "curation_rule": render_rule_curies(cand.rule_ids()),
            "comment": "", "evidence_source": "lexical",
            "risk_flags": "|".join(sorted(cand.flags)),
        }

    for m in mappings:
        covered.add((m.subject_id, m.object_id))
        if m.lexical is not None and m.candidate is not None:
            out.append(lexical_row(m.subject_id, m.subject_label, m.object_id,
                                   m.object_label, m.candidate, m.lexical))
        for name, row in m.seeds.items():
            out.append({
                "subject_id": m.subject_id, "subject_label": m.subject_label,
                "predicate_id": row.predicate_id, "object_id": m.object_id,
                "object_label": m.object_label, "mapping_justification": row.justification,
                "confidence": row.confidence, **_lexical_cells(None),
                "mapping_source": row.mapping_source, "mapping_tool": "",
                "mapping_tool_version": "", "mapping_date": "", "curation_rule": "",
                "comment": row.comment, "evidence_source": name, "risk_flags": "",
            })
        agent = m.agent_view
        if agent is not None:
            out.append({
                "subject_id": m.subject_id, "subject_label": m.subject_label,
                "predicate_id": agent.predicate, "object_id": m.object_id,
                "object_label": m.object_label,
                "mapping_justification": "semapv:CompositeMatching",
                "confidence": round(agent.confidence, 4), **_lexical_cells(None),
                "mapping_source": "", "mapping_tool": "mondo-ai align",
                "mapping_tool_version": version, "mapping_date": today,
                "curation_rule": render_rule_curies(agent.rule_ids),
                "comment": agent.justification, "evidence_source": "agent", "risk_flags": "",
            })

    for r in sorted(records, key=lambda r: r.subject_id):
        for cand in sorted(r.candidates, key=lambda c: c.object_id):
            key = (r.subject_id, cand.object_id)
            sig = best_lexical(cand)
            if sig is None or key in covered:
                continue
            out.append(lexical_row(r.subject_id, r.subject_label, cand.object_id, "",
                                   cand, sig))
    order = {"lexical": 0, "agent": 9}
    out.sort(key=lambda d: (d["subject_id"], d["object_id"],
                            order.get(d["evidence_source"], 1), d["evidence_source"]))
    return out


def write_tsv(
    path: Path,
    rows: list[dict[str, Any]],
    metadata: dict[str, Any],
    columns: list[str],
) -> int:
    """Write rows as SSSOM TSV under a YAML header block."""
    import yaml

    header = yaml.safe_dump(metadata, sort_keys=False, default_flow_style=False, width=100)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        for line in header.splitlines():
            fh.write(f"#{line}\n")
        w = csv.DictWriter(fh, fieldnames=columns, delimiter="\t", lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def reconciled_metadata(
    base: dict[str, Any],
    run_id: str,
    kind: str,
    extensions: dict[str, tuple[str, str]],
    description: str,
) -> dict[str, Any]:
    """Derive a reconciled set's header from the run's own set metadata."""
    md = dict(base)
    md["mapping_set_id"] = f"https://w3id.org/mondo-ai/align/{run_id}/{kind}"
    md["mapping_set_title"] = f"{base.get('mapping_set_title', run_id)} — {kind}"
    md["mapping_set_description"] = description
    curie_map = dict(md.get("curie_map", {}))
    curie_map.setdefault("oio", "http://www.geneontology.org/formats/oboInOwl#")
    md["curie_map"] = curie_map
    md["extension_definitions"] = [
        {"slot_name": slot, "property": prop, "type_hint": hint}
        for slot, (prop, hint) in extensions.items()
    ]
    return md


def write_outputs(
    out_dir: Path,
    mappings: list[ConsolidatedMapping],
    records: list[MappingRecord],
    base_metadata: dict[str, Any],
    run_id: str,
    version: str,
) -> dict[str, Path]:
    """Write ``consolidated.sssom.tsv`` and ``evidence.sssom.tsv``."""
    today = date.today().isoformat()
    consolidated = out_dir / "consolidated.sssom.tsv"
    evidence = out_dir / "evidence.sssom.tsv"
    write_tsv(
        consolidated, [consolidated_row(m, version, today) for m in mappings],
        reconciled_metadata(
            base_metadata, run_id, "consolidated", CONSOLIDATED_EXTENSIONS,
            "Every pair asserted by a seed mapping set or found by the run, one row per "
            "pair, with its reconciliation status, review tier and the evidence for it. "
            "Seeds were held out of discovery, so lexical support is independent of them."),
        CONSOLIDATED_STANDARD + list(CONSOLIDATED_EXTENSIONS))
    write_tsv(
        evidence, evidence_rows(mappings, records, version, today),
        reconciled_metadata(
            base_metadata, run_id, "evidence", EVIDENCE_EXTENSIONS,
            "The evidence ledger: one row per pair per line of evidence (lexical match, "
            "each seed set, the adjudicating agent), each with its own justification."),
        EVIDENCE_STANDARD + list(EVIDENCE_EXTENSIONS))
    return {"consolidated": consolidated, "evidence": evidence}


# --- report ------------------------------------------------------------------------------


def report_section(
    mappings: list[ConsolidatedMapping],
    seeds: list[list[SeedRow]],
    names: list[str],
    boomer_stats: dict[str, Any] | None = None,
    examples: int = 3,
) -> str:
    """Return the markdown section the run report carries for reconciliation."""
    from mondo_ai.align.report import md_table

    status_tier = Counter((m.status, m.review_tier.value) for m in mappings)
    tiers = Counter(m.review_tier.value for m in mappings)
    lines = [
        "## Reconciliation with seed mapping sets",
        "",
        f"{len(mappings):,} subject/object pairs: every pair a seed asserts plus every "
        f"mapping the run found. Seeds were held out of discovery, so a string identity "
        f"backing a seed pair is independent evidence. **{tiers.get('EXPERT_REVIEW', 0):,} "
        f"pairs need a human.**",
        "",
    ]
    seed_rows = []
    for name, rows in zip(names, seeds):
        mine = [m for m in mappings if name in m.seeds]
        st = Counter(m.status for m in mine)
        seed_rows.append([
            name, f"{len(rows):,}", f"{len({r.subject_id for r in rows}):,}",
            f"{st.get('corroborated', 0):,}", f"{st.get('seed_only', 0):,}",
            f"{sum(v for k, v in st.items() if STATUS_TIER.get(k) == ReviewTier.EXPERT_REVIEW):,}",
            f"{st.get('inactive_target', 0) + st.get('unknown_target', 0):,}",
        ])
    lines += [md_table([["seed", "pairs", "subjects", "corroborated", "seed only",
                         "need a human", "dead target"]] + seed_rows), ""]

    if len(seeds) >= 2:
        a, b = ({(r.subject_id, r.object_id) for r in rows} for rows in seeds[:2])
        lines += [
            f"Overlap of `{names[0]}` and `{names[1]}`: {len(a & b):,} identical pairs, "
            f"{len(a - b):,} only in `{names[0]}`, {len(b - a):,} only in `{names[1]}`. "
            f"Identical pairs are not counted as corroboration: they are most likely one "
            f"assertion repeated.",
            "",
        ]

    lines += [md_table([["status", "tier", "pairs"]] + [
        [s, t, f"{n:,}"] for (s, t), n in sorted(status_tier.items(), key=lambda kv: -kv[1])
    ]), ""]

    if boomer_stats:
        lines += [
            "Boomer over the consolidated set: "
            + ", ".join(f"{k} {v}" for k, v in boomer_stats.items() if k != "error") + ".",
            "",
        ]

    lines += ["### Examples by status", ""]
    for status in [s for s in STATUS_TIER if s not in ("corroborated",)] + ["discovery_only"]:
        shown = [m for m in mappings if m.status == status][:examples]
        if not shown:
            continue
        lines.append(f"**{status}**")
        lines.append("")
        for m in shown:
            lines.append(f"- `{m.subject_id}` {m.subject_label} → `{m.object_id}` "
                         f"{m.object_label} ({m.predicate_id}): {m.comment}")
        lines.append("")
    lines += [
        "Files: `consolidated.sssom.tsv` (one row per pair), `evidence.sssom.tsv` (one row "
        "per pair per line of evidence).",
        "",
    ]
    return "\n".join(lines)
