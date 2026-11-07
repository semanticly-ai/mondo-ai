"""Triage: which rows a curator should actually look at, in what order.

This is the module the whole pipeline exists to serve. Everything upstream gathers evidence;
this decides where an expert's hour buys the most correctness.

``review_priority`` is a **transparent weighted sum**, not a learned score. The components
and their weights are published, every row carries its own component breakdown, and a
curator who disagrees with the ranking can see exactly which term put a row where it is. A
calibrated black box would rank better on average and would be un-arguable, which is the
wrong trade for a curation queue.

| Component | Weight | Why |
|---|---|---|
| ladder/agent disagreement | 0.22 | Two independent methods conflict. Highest-yield review. |
| agent uncertainty | 0.17 | The model says it is unsure. |
| Boomer conflict | 0.15 | Consistency conditioning took probability away from our claim. |
| ``possibly_coextensive`` | 0.13 | The registry's hardest judgement (``SC-007`` vs ``SC-001``). |
| ambiguity | 0.10 | ``PR-008``: assert nothing, ask. |
| structural defect | 0.10 | ``ST-001``/``ST-002``: set-level corruption a curator must resolve. |
| subject impact | 0.07 | A wrong grouping mapping propagates to its descendants. |
| declared conflation | 0.06 | A conceptual-model boundary was knowingly crossed. |

Example:
    >>> round(sum(WEIGHTS.values()), 6)
    1.0
"""

from __future__ import annotations

import math
from collections import defaultdict

from mondo_ai.align.models import (
    EXACT_MATCH,
    Adjudication,
    MappingRecord,
    ReviewTier,
)

#: Published weights of the priority components. They sum to 1.0 so a priority is readable
#: as "what fraction of the possible reasons to worry apply here".
WEIGHTS = {
    "disagreement": 0.22,
    "agent_uncertainty": 0.17,
    "boomer_conflict": 0.15,
    "coextensive": 0.13,
    "ambiguity": 0.10,
    "structural": 0.10,
    "impact": 0.07,
    "conflation": 0.06,
}

#: A row the agent itself asked a human about cannot rank below this.
NEEDS_EXPERT_FLOOR = 0.6
#: An auto-accepted row cannot rank above this, so it never displaces a real question.
AUTO_ACCEPT_CEILING = 0.2
#: Rungs whose settled verdicts are strong enough to ship without a spot check.
STRONG_RUNGS = frozenset({"D1", "D2", "D3"})
#: Priority at or above which a row that would otherwise be spot-checked becomes expert work.
EXPERT_THRESHOLD = 0.35


def impact_score(n_descendants: int) -> float:
    """Return the normalised impact of getting this subject wrong.

    Log-scaled and capped: the difference between a leaf and a 10-descendant grouping matters
    more than the difference between 1,000 and 10,000, because past a certain size the row is
    already unambiguously worth checking.

    Example:
        >>> impact_score(0)
        0.0
        >>> round(impact_score(10), 3)
        0.347
        >>> impact_score(100000)
        1.0
        >>> impact_score(10) < impact_score(100) < impact_score(1000)
        True
    """
    if n_descendants <= 0:
        return 0.0
    return min(1.0, math.log10(1 + n_descendants) / 3.0)


def ambiguity_score(count: int) -> float:
    """Return the normalised ambiguity contribution of a tie count.

    Example:
        >>> ambiguity_score(0), ambiguity_score(1)
        (0.0, 0.0)
        >>> ambiguity_score(2), ambiguity_score(3)
        (0.25, 0.5)
        >>> ambiguity_score(20)
        1.0
    """
    if count <= 1:
        return 0.0
    return min(1.0, (count - 1) / 4.0)


def priority_components(record: MappingRecord) -> dict[str, float]:
    """Return each priority component for a record, in [0,1].

    Example:
        >>> from mondo_ai.align.models import Candidate
        >>> det = Adjudication("deterministic", "exact", "I:A00", 0.99)
        >>> ag = Adjudication("agent", "broad", "I:A00", 0.4)
        >>> r = MappingRecord("M:1", deterministic=det, agent=ag, ambiguity_count=2,
        ...                   candidates=[Candidate("I:A00", flags=["SC-001", "SC-007"])],
        ...                   structural_flags=["ST-002"])
        >>> comps = priority_components(r)
        >>> comps["disagreement"], comps["coextensive"], comps["structural"]
        (0.5, 1.0, 1.0)
        >>> round(comps["agent_uncertainty"], 3)
        0.6
    """
    agent = record.agent
    if agent is None:
        agent_uncertainty = 0.0
    else:
        agent_uncertainty = max(0.0, 1.0 - agent.confidence * agent.agreement)

    coextensive = 1.0 if any(
        "SC-007" in c.flags and any(f in ("SC-001", "SC-002") for f in c.flags)
        for c in record.candidates
    ) else 0.0

    conflation = 1.0 if (agent and agent.conflation) else 0.0

    return {
        "disagreement": record.disagreement(),
        "agent_uncertainty": agent_uncertainty,
        "boomer_conflict": boomer_conflict(record),
        "coextensive": coextensive,
        "ambiguity": ambiguity_score(record.ambiguity_count),
        "structural": 1.0 if record.structural_flags else 0.0,
        "impact": record.priority_components.get("impact", 0.0),
        "conflation": conflation,
    }


def boomer_conflict(record: MappingRecord) -> float:
    """Return how much consistency conditioning argues against this row, in [0,1].

    Boomer sees something no local scorer can: that accepting this mapping makes another one
    impossible. Where it takes probability away from the relation we asserted, that loss is
    the most specific reason to review a row in the whole component set — the pipeline was
    confident and the world would not allow it.

    Three cases, in order of how much they say:

    * Boomer chose a **different predicate** — at least 0.5, and more if the posterior it
      leaves our claim is low. A disagreement here is not a soft signal.
    * Boomer agreed, but the **posterior fell below the prior** — the drop itself.
    * Boomer did not see the row, or left it untouched — 0.0. Absence of evidence is not
      evidence, so an un-conditioned row does not get a conflict score.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("M:1", final=Adjudication("agent", "exact", "I:A", 0.9))
        >>> boomer_conflict(rec)
        0.0
        >>> rec.boomer = {"prior": 0.9, "posterior": 0.9, "predicate": "skos:exactMatch",
        ...               "agrees_with_pipeline": True}
        >>> boomer_conflict(rec)
        0.0
        >>> rec.boomer = {"prior": 0.9, "posterior": 0.4, "predicate": "skos:exactMatch",
        ...               "agrees_with_pipeline": True}
        >>> round(boomer_conflict(rec), 4)
        0.5
        >>> rec.boomer = {"prior": 0.7, "posterior": 0.03, "predicate": "skos:narrowMatch",
        ...               "agrees_with_pipeline": False}
        >>> round(boomer_conflict(rec), 4)
        0.67
        >>> rec.boomer = {"prior": 0.7, "posterior": None, "predicate": "skos:narrowMatch",
        ...               "agrees_with_pipeline": False}
        >>> boomer_conflict(rec)
        0.5
    """
    data = record.boomer
    if not data:
        return 0.0
    posterior = data.get("posterior")
    prior = data.get("prior")
    disagrees = not data.get("agrees_with_pipeline", True)
    if posterior is None or prior is None:
        return 0.5 if disagrees else 0.0
    drop = max(0.0, float(prior) - float(posterior))
    return max(0.5, drop) if disagrees else drop


def review_priority(components: dict[str, float]) -> float:
    """Return the weighted priority for a component breakdown.

    Example:
        >>> review_priority({k: 0.0 for k in WEIGHTS})
        0.0
        >>> review_priority({k: 1.0 for k in WEIGHTS})
        1.0
        >>> review_priority({"disagreement": 1.0})
        0.22
    """
    return round(
        sum(WEIGHTS[k] * max(0.0, min(1.0, components.get(k, 0.0))) for k in WEIGHTS), 4
    )


def choose_final(record: MappingRecord) -> Adjudication:
    """Pick the verdict that goes into the mapping set.

    The agent's verdict wins wherever it ran, because the ladder only hands over cases it
    could not settle: a rung that settled produced no agent call at all. The one exception is
    a failed agent call, whose verdict carries no information and would otherwise erase the
    ladder's recorded reason for the hand-off.

    Example:
        >>> det = Adjudication("deterministic", "exact", "I:A00", 0.99, decision_path="D1")
        >>> choose_final(MappingRecord("M:1", deterministic=det)).source
        'deterministic'
        >>> ag = Adjudication("agent", "broad", "I:E83", 0.7, decision_path="A1")
        >>> choose_final(MappingRecord("M:1", deterministic=det, agent=ag)).source
        'agent'
        >>> bad = Adjudication("agent", "none", decision_path="A1-failed")
        >>> choose_final(MappingRecord("M:1", deterministic=det, agent=bad)).decision_path
        'D1'
    """
    if record.agent is not None and record.agent.decision_path != "A1-failed":
        return record.agent
    if record.deterministic is not None:
        return record.deterministic
    return Adjudication(source="deterministic", decision="none", rule_ids=["RJ-001"])


def assign_tier(record: MappingRecord, final: Adjudication, priority: float) -> ReviewTier:
    """Return the review tier for a record.

    ``AUTO_ACCEPT`` is reserved for settled strong rungs with nothing against them.
    ``REJECTED`` is for an actively decided-against pair — it is a finding, and it keeps a
    row so that the reason survives. ``NO_MATCH`` is the recorded absence.

    Example:
        >>> det = Adjudication("deterministic", "exact", "I:A00", 0.99, decision_path="D1")
        >>> assign_tier(MappingRecord("M:1", deterministic=det), det, 0.0).value
        'AUTO_ACCEPT'
        >>> assign_tier(MappingRecord("M:1", deterministic=det, structural_flags=["ST-002"]),
        ...             det, 0.4).value
        'EXPERT_REVIEW'
        >>> ag = Adjudication("agent", "exact", "I:A00", 0.9, decision_path="A1")
        >>> assign_tier(MappingRecord("M:1", agent=ag), ag, 0.1).value
        'SPOT_CHECK'
        >>> low = Adjudication("agent", "exact", "I:A00", 0.3, decision_path="A1")
        >>> assign_tier(MappingRecord("M:1", agent=low), low, 0.5).value
        'EXPERT_REVIEW'
        >>> none_v = Adjudication("agent", "none", rule_ids=["RJ-001"], decision_path="A1")
        >>> assign_tier(MappingRecord("M:1", agent=none_v), none_v, 0.1).value
        'NO_MATCH'
        >>> rej = Adjudication("agent", "none", rule_ids=["SC-001", "PR-002"],
        ...                    justification="Residual rubric.", decision_path="A1")
        >>> assign_tier(MappingRecord("M:1", agent=rej,
        ...     candidates=[__import__("mondo_ai.align.models", fromlist=["Candidate"]
        ...         ).Candidate("I:E80.29")]), rej, 0.1).value
        'REJECTED'
    """
    # Unexamined first. An item the ladder handed over that no adjudicator reached has
    # decision "none" and candidates, which would otherwise read as REJECTED — a verdict
    # nobody reached. And calling it EXPERT_REVIEW is just as wrong: it is not a question
    # anyone has formed yet, and mixed into the queue it buries the ones that are.
    if final.unexamined:
        return ReviewTier.QUEUED

    if final.needs_expert:
        return ReviewTier.EXPERT_REVIEW

    if not final.asserts_mapping:
        # A "none" against candidates that were actually considered is a decision; a "none"
        # with nothing to consider is an absence. Both are rows; only one is a verdict.
        if record.candidates and final.rule_ids != ["RJ-001"]:
            return ReviewTier.REJECTED
        return ReviewTier.NO_MATCH

    if record.structural_flags or priority >= EXPERT_THRESHOLD:
        return ReviewTier.EXPERT_REVIEW

    if (
        final.source == "deterministic"
        and final.decision_path in STRONG_RUNGS
        and record.agent is None
        and record.ambiguity_count <= 1
        # A row that crossed a conceptual-model boundary is defensible but not unexamined-
        # safe: the boundary was crossed by policy, and policy is what a spot check tests.
        and not final.conflation
    ):
        return ReviewTier.AUTO_ACCEPT

    return ReviewTier.SPOT_CHECK


def structural_check(records: list[MappingRecord]) -> dict[str, list[str]]:
    """Detect set-level defects invisible row by row.

    ``ST-001`` proxy merge: two objects both exactMatch one subject. Cannot arise from this
    pipeline's one-verdict-per-subject shape, but is checked anyway because a merged or
    hand-edited set can contain it.

    ``ST-002`` reverse proxy merge: two subjects both exactMatch one object. This one does
    arise routinely, and it is disallowed at every tier — two Mondo terms claiming the same
    ICD code means at least one is wrong.

    Returns:
        Subject id -> flags, for subjects involved in a defect.

    Example:
        >>> a = MappingRecord("M:1", final=Adjudication("agent", "exact", "I:A00"))
        >>> b = MappingRecord("M:2", final=Adjudication("agent", "exact", "I:A00"))
        >>> c = MappingRecord("M:3", final=Adjudication("agent", "exact", "I:A01"))
        >>> out = structural_check([a, b, c])
        >>> sorted(out), out["M:1"]
        (['M:1', 'M:2'], ['ST-002'])

        A non-exact predicate is not a proxy merge: broad matches to a shared grouping are
        expected and legitimate.

        >>> d = MappingRecord("M:4", final=Adjudication("agent", "broad", "I:E00"))
        >>> e = MappingRecord("M:5", final=Adjudication("agent", "broad", "I:E00"))
        >>> structural_check([d, e])
        {}
    """
    by_object: dict[str, list[str]] = defaultdict(list)
    for r in records:
        final = r.final
        if final is None or final.predicate != EXACT_MATCH or not final.object_id:
            continue
        by_object[final.object_id].append(r.subject_id)

    out: dict[str, list[str]] = {}
    for _object_id, subjects in by_object.items():
        if len(subjects) > 1:
            for s in subjects:
                out.setdefault(s, []).append("ST-002")
    return out


def undeclared_conflations(
    final: Adjudication,
    permitted: frozenset[str] | set[str] | list[str],
) -> list[str]:
    """Return conflation rules the verdict invoked that the set did not declare.

    A conflation the run declared up front is a policy decision the reader has already been
    told about. One the adjudicator reached for on its own is a different thing: it crossed a
    boundary nobody authorised, which is exactly the case a curator has to rule on. Those
    rows are escalated rather than quietly shipped.

    Example:
        >>> a = Adjudication("agent", "exact", "I:H25.0", conflation=["CM-002", "CM-005"])
        >>> undeclared_conflations(a, ["CM-001", "CM-002"])
        ['CM-005']
        >>> undeclared_conflations(a, ["CM-002", "CM-005"])
        []
        >>> undeclared_conflations(Adjudication("agent", "exact", "I:1"), [])
        []
    """
    allowed = {r.split(":")[-1] for r in permitted}
    return [r for r in final.conflation if r.split(":")[-1] not in allowed]


def finalise(records: list[MappingRecord]) -> list[MappingRecord]:
    """Pick each record's final verdict, without scoring it.

    Separated from :func:`triage` because Boomer has to run *between* the two: it needs the
    final verdicts to build its hypothesis table, and its answer then feeds the priority.

    Example:
        >>> det = Adjudication("deterministic", "exact", "I:A00", 0.99, decision_path="D1")
        >>> recs = finalise([MappingRecord("M:1", deterministic=det)])
        >>> recs[0].final.object_id
        'I:A00'
    """
    for r in records:
        r.final = choose_final(r)
    return records


def triage(
    records: list[MappingRecord],
    *,
    permitted_conflations: list[str] | None = None,
) -> list[MappingRecord]:
    """Finalise verdicts, run structural checks, and score every record.

    Runs in two passes because the structural checks need every final verdict before any
    priority can be computed: a reverse proxy merge is a property of the set, and a row
    scored before the set is known would under-rank it.

    Args:
        records: The records to finalise.
        permitted_conflations: ``CM-*`` ids the run declared. A verdict invoking one outside
            this list is escalated to ``EXPERT_REVIEW``.

    Returns:
        The same records, mutated, sorted by descending priority then subject id.

    Example:
        An undeclared conflation escalates, however confident the verdict:

        >>> a = Adjudication("agent", "exact", "I:H25.0", 0.9, decision_path="A1",
        ...                  conflation=["CM-005"])
        >>> out = triage([MappingRecord("M:9", agent=a)],
        ...              permitted_conflations=["CM-001"])
        >>> out[0].review_tier.value, "CM-005" in out[0].structural_flags
        ('EXPERT_REVIEW', True)
        >>> out2 = triage([MappingRecord("M:9", agent=a)],
        ...               permitted_conflations=["CM-001", "CM-005"])
        >>> out2[0].review_tier.value
        'SPOT_CHECK'

    Example:
        >>> det = Adjudication("deterministic", "exact", "I:A00", 0.99, decision_path="D1")
        >>> clean = MappingRecord("M:1", deterministic=det)
        >>> dup_a = MappingRecord("M:2", deterministic=Adjudication(
        ...     "deterministic", "exact", "I:A01", 0.99, decision_path="D1"))
        >>> dup_b = MappingRecord("M:3", deterministic=Adjudication(
        ...     "deterministic", "exact", "I:A01", 0.99, decision_path="D1"))
        >>> out = triage([clean, dup_a, dup_b])
        >>> [r.subject_id for r in out]
        ['M:2', 'M:3', 'M:1']
        >>> out[0].review_tier.value, out[-1].review_tier.value
        ('EXPERT_REVIEW', 'AUTO_ACCEPT')
        >>> out[0].structural_flags
        ['ST-002']
    """
    finalise(records)

    defects = structural_check(records)
    for r in records:
        if r.subject_id in defects:
            for flag in defects[r.subject_id]:
                if flag not in r.structural_flags:
                    r.structural_flags.append(flag)

    permitted = permitted_conflations or []
    for r in records:
        final = r.final
        assert final is not None
        for rule_id in undeclared_conflations(final, permitted):
            if rule_id not in r.structural_flags:
                r.structural_flags.append(rule_id)

    for r in records:
        comps = priority_components(r)
        priority = review_priority(comps)
        final = r.final
        assert final is not None  # set in the first pass
        if final.needs_expert and not final.unexamined:
            priority = max(priority, NEEDS_EXPERT_FLOOR)
        tier = assign_tier(r, final, priority)
        if tier is ReviewTier.AUTO_ACCEPT:
            priority = min(priority, AUTO_ACCEPT_CEILING)
        r.priority_components = comps
        r.review_priority = priority
        r.review_tier = tier

    records.sort(key=lambda r: (-r.review_priority, r.subject_id))
    return records
