"""The deterministic adjudication ladder.

First rung that fires decides. The rung id travels with the row as ``decision_path``, so
every mapping says which rule settled it and a reviewer can challenge the rung rather than
the verdict.

The ladder's defining property is what it *refuses* to do. It never downgrades a blocked
hit to a weaker predicate on its own authority: a lexical match against "Other porphyria"
is real evidence about strings and no evidence at all about extension, so the pair goes to a
stage that can reason about the rubric's children. Silently emitting ``broadMatch`` there
would look like progress and would put an unreviewed guess in the output.

| Rung | Condition | Result |
|------|-----------|--------|
| ``D0`` | object id is not a code, or is obsolete | reject, ``ST-005``/``ST-004`` |
| ``D1`` | unique raw label↔label hit, unblocked | ``exactMatch`` 0.99 |
| ``D2`` | unique normalised label↔label hit, unblocked | ``exactMatch`` 0.97 |
| ``D3`` | unique synonym-crossing hit, unblocked | ``exactMatch`` 0.92 |
| ``D4`` | unique declared-rewrite hit, unblocked | ``exactMatch`` at the rewrite's certainty |
| ``D5`` | a D1–D4 hit blocked by a scope flag | proposal only, to the agent |
| ``D6`` | two or more distinct objects tie at the winning tier | to the agent, ``PR-008`` |
| ``D7`` | retrieval-only candidates clearing the viability floor | to the agent |
| ``D8`` | retrieval returned only token-overlap noise | ``NoTermFound`` + ``RJ-004`` |

Example:
    >>> from mondo_ai.align.models import Candidate, Concept, Signal
    >>> subject = Concept(id="MONDO:1", label="Marfan syndrome")
    >>> hit = Candidate("ICD10CM:Q87.4", [Signal("lexical_exact", "EV-001", 1.0)], tier=1)
    >>> result = adjudicate(subject, [hit])
    >>> result.decision_path, result.adjudication.decision, result.adjudication.confidence
    ('D1', 'exact', 0.99)
    >>> result.settled
    True
"""

from __future__ import annotations

from dataclasses import dataclass

from mondo_ai.align.candidates import (
    TIER_NORMALIZED,
    TIER_RAW,
    TIER_RETRIEVAL,
    TIER_SYNONYM,
    TIER_VARIANT,
)
from mondo_ai.align.flags import BLOCKING_FLAGS
from mondo_ai.align.models import Adjudication, Candidate, Concept

#: Confidence awarded by each settling rung. D4 uses the rewrite's own certainty instead.
RUNG_CONFIDENCE = {"D1": 0.99, "D2": 0.97, "D3": 0.92}

#: Rungs whose verdict is final without consulting the agent.
SETTLING_RUNGS = frozenset({"D0", "D1", "D2", "D3", "D4"})


@dataclass(slots=True)
class LadderResult:
    """The ladder's verdict plus whether it stands on its own.

    Attributes:
        adjudication: The verdict.
        settled: Whether the verdict is final. Unsettled verdicts are proposals that the
            agent stage must confirm, correct or replace.
        review_candidates: The candidates worth putting in front of the agent, best first.
        ambiguity_count: How many distinct objects tied at the winning tier.
    """

    adjudication: Adjudication
    settled: bool
    review_candidates: list[Candidate]
    ambiguity_count: int

    @property
    def decision_path(self) -> str:
        """Shorthand for the rung id.

        Example:
            >>> LadderResult(Adjudication("deterministic", "none", decision_path="D7"),
            ...              False, [], 0).decision_path
            'D7'
        """
        return self.adjudication.decision_path


def _blocking(candidate: Candidate) -> list[str]:
    """Return the candidate's flags that forbid a straight ``exactMatch``.

    ``SC-007`` does not appear here — it is the *counter*-argument to a residual flag, and
    the blocking flag it competes with stays in the list. The presence of both is precisely
    the case that needs adjudication.

    Example:
        >>> _blocking(Candidate("X:1", flags=["SC-001", "SC-007"]))
        ['SC-001']
        >>> _blocking(Candidate("X:1", flags=["EV-001"]))
        []
    """
    return [f for f in candidate.flags if f in BLOCKING_FLAGS]


def _at_tier(candidates: list[Candidate], tier: int) -> list[Candidate]:
    """Return candidates whose best tier equals ``tier``.

    Example:
        >>> _at_tier([Candidate("X:1", tier=1), Candidate("X:2", tier=2)], 2)[0].object_id
        'X:2'
    """
    return [c for c in candidates if c.tier == tier]


#: Minimum share of the subject's discriminative IDF mass a retrieval-only candidate must
#: cover to be worth adjudicating. Measured on the full Mondo x ICD-10-CM run: raising this
#: from 0 to 0.3 removes 13% of the adjudication queue and costs 7 of 63 recoverable gold
#: exact matches. 0.5 would remove 54% of the queue and cost 22 of 63, which is the wrong
#: trade — the queue is cheap to lengthen and recall is expensive to rebuild.
DEFAULT_MIN_RETRIEVAL_SCORE = 0.3


def adjudicate(
    subject: Concept,
    candidates: list[Candidate],
    *,
    subject_flags: list[str] | None = None,
    min_retrieval_score: float = DEFAULT_MIN_RETRIEVAL_SCORE,
) -> LadderResult:
    """Run the ladder for one subject.

    Args:
        subject: The subject concept.
        candidates: Its candidate pool, as produced by
            :class:`~mondo_ai.align.candidates.CandidateGenerator`.
        subject_flags: Flags detected on the subject itself (``SC-006``, ``CM-006``,
            ``CM-007``). A flagged subject never auto-accepts: a susceptibility locus and a
            numbered subtype are the two cases where a confident lexical match is most
            likely to be wrong.

    Returns:
        A :class:`LadderResult`.

    Example:
        A clean unique hit settles at D1:

        >>> from mondo_ai.align.models import Signal
        >>> subj = Concept(id="MONDO:1", label="Cholera")
        >>> c = Candidate("ICD10CM:A00", [Signal("lexical_exact", "EV-001", 1.0)], tier=1)
        >>> r = adjudicate(subj, [c])
        >>> r.decision_path, r.settled, r.adjudication.object_id
        ('D1', True, 'ICD10CM:A00')

        A residual rubric blocks it down to D5, a proposal for the agent:

        >>> c2 = Candidate("ICD10CM:E80.29", [Signal("lexical_exact", "EV-001", 1.0)],
        ...                flags=["SC-001"], tier=1)
        >>> r2 = adjudicate(subj, [c2])
        >>> r2.decision_path, r2.settled, r2.adjudication.decision
        ('D5', False, 'none')
        >>> "SC-001" in r2.adjudication.rule_ids
        True

        Two objects tied at the same tier is D6 — ambiguity, not a match:

        >>> a = Candidate("ICD10CM:A00", [Signal("lexical_exact", "EV-001", 1.0)], tier=1)
        >>> b = Candidate("ICD10CM:A01", [Signal("lexical_exact", "EV-001", 1.0)], tier=1)
        >>> r3 = adjudicate(subj, [a, b])
        >>> r3.decision_path, r3.ambiguity_count, "PR-008" in r3.adjudication.rule_ids
        ('D6', 2, True)

        Nothing at all is D7 with an explicit rejection ground:

        >>> r4 = adjudicate(subj, [])
        >>> r4.decision_path, r4.settled, r4.adjudication.rule_ids
        ('D7', True, ['RJ-001'])

        A non-code identifier is terminal at D0:

        >>> bad = Candidate("ICD10CM:E00-E90", [Signal("lexical_exact", "EV-001", 1.0)],
        ...                 flags=["ST-005"], tier=1)
        >>> r5 = adjudicate(subj, [bad])
        >>> r5.decision_path, r5.settled, r5.adjudication.decision
        ('D0', True, 'none')

        A flagged subject never settles, however clean the hit:

        >>> r6 = adjudicate(Concept(id="MONDO:2", label="immunodeficiency 112"), [c],
        ...                 subject_flags=["SC-006"])
        >>> r6.decision_path, r6.settled
        ('D5', False)
    """
    subject_flags = subject_flags or []
    usable = [c for c in candidates if "ST-005" not in c.flags]
    dropped_for_id = [c for c in candidates if "ST-005" in c.flags]

    # D0 — the object identifier is not a pullable code. Terminal: no scope analysis of a
    # chapter range is meaningful, and passing it on as a candidate invites a plausible
    # label to carry an unusable id into the output.
    if dropped_for_id and not usable:
        worst = dropped_for_id[0]
        return LadderResult(
            Adjudication(
                source="deterministic",
                decision="none",
                confidence=0.0,
                rule_ids=["ST-005", "RJ-001"],
                justification=(
                    f"The only lexical match was {worst.object_id}, which is a chapter "
                    f"range or foreign-vocabulary identifier rather than a code."
                ),
                decision_path="D0",
            ),
            settled=True,
            review_candidates=[],
            ambiguity_count=0,
        )

    for rung, tier in (("D1", TIER_RAW), ("D2", TIER_NORMALIZED),
                       ("D3", TIER_SYNONYM), ("D4", TIER_VARIANT)):
        at_tier = _at_tier(usable, tier)
        if not at_tier:
            continue
        distinct = {c.object_id for c in at_tier}
        if len(distinct) > 1:
            # D6 — several objects are equally well supported at the strongest tier that
            # fired. PR-008: assert nothing, ask.
            return LadderResult(
                Adjudication(
                    source="deterministic",
                    decision="none",
                    confidence=0.0,
                    rule_ids=sorted({r for c in at_tier for r in c.rule_ids()}) + ["PR-008"],
                    justification=(
                        f"{len(distinct)} objects match at tier {tier} "
                        f"({', '.join(sorted(distinct))}); ambiguity is not a match."
                    ),
                    decision_path="D6",
                    alternatives=sorted(distinct),
                ),
                settled=False,
                review_candidates=at_tier + [c for c in usable if c not in at_tier],
                ambiguity_count=len(distinct),
            )

        winner = at_tier[0]
        blocking = _blocking(winner)
        if blocking or subject_flags:
            # D5 — the strings agree and the extension may not. Proposal only.
            reasons = blocking + subject_flags
            return LadderResult(
                Adjudication(
                    source="deterministic",
                    decision="none",
                    confidence=0.0,
                    rule_ids=winner.rule_ids() + subject_flags,
                    justification=(
                        f"Lexical match to {winner.object_id} at tier {tier}, blocked by "
                        f"{', '.join(reasons)}; extension must be judged, not inferred."
                    ),
                    decision_path="D5",
                    alternatives=[winner.object_id],
                ),
                settled=False,
                review_candidates=[winner] + [c for c in usable if c is not winner],
                ambiguity_count=1,
            )

        confidence = RUNG_CONFIDENCE.get(rung)
        if confidence is None:  # D4 inherits the rewrite's own certainty
            confidence = round(min(0.9, winner.best_certainty()), 3)
        # Non-blocking flags are not obstacles, but they are not nothing either: a CM-002
        # disease/phenotype conflation permitted at set level still has to appear on the row
        # that used it, or the set is unreviewable however good its individual verdicts.
        conflation = [f for f in winner.flags if f.startswith("CM-")]
        return LadderResult(
            Adjudication(
                source="deterministic",
                decision="exact",
                object_id=winner.object_id,
                confidence=confidence,
                rule_ids=winner.rule_ids() + ["PR-001"],
                justification=_settled_justification(rung, winner, conflation),
                decision_path=rung,
                conflation=conflation,
            ),
            settled=True,
            review_candidates=[],
            ambiguity_count=1,
        )

    retrieval = _at_tier(usable, TIER_RETRIEVAL)
    viable = [c for c in retrieval if c.score >= min_retrieval_score]

    if retrieval and not viable:
        # D8 — retrieval returned things that share a word with the subject and nothing that
        # covers its discriminative content. RJ-004 is the right recorded verdict: this is a
        # deterministic, reproducible judgement about token overlap, not an expert question,
        # and the rejected candidates stay in records.jsonl for audit. Sending these to a
        # curator would bury the real questions under retrieval noise.
        best = max(c.score for c in retrieval)
        return LadderResult(
            Adjudication(
                source="deterministic",
                decision="none",
                confidence=0.0,
                rule_ids=["EV-006", "RJ-004", "RJ-001"],
                justification=(
                    f"No string identity, and the best of {len(retrieval)} retrieved "
                    f"candidates covers only {best:.0%} of the subject's discriminative "
                    f"tokens (closest: {retrieval[0].object_id}); token overlap alone is not "
                    f"evidence of sameness."
                ),
                decision_path="D8",
                alternatives=[c.object_id for c in retrieval[:5]],
            ),
            settled=True,
            review_candidates=[],
            ambiguity_count=0,
        )

    if viable:
        retrieval = viable
        # D7 with candidates — no string identity anywhere, so retrieval found things that
        # share words. EV-006 is not evidence of sameness; a judgement is required.
        return LadderResult(
            Adjudication(
                source="deterministic",
                decision="none",
                confidence=0.0,
                rule_ids=["EV-006"],
                justification=(
                    f"No string identity; {len(retrieval)} candidates retrieved on token "
                    f"overlap alone."
                ),
                decision_path="D7",
                alternatives=[c.object_id for c in retrieval],
            ),
            settled=False,
            review_candidates=retrieval,
            ambiguity_count=len({c.object_id for c in retrieval}),
        )

    # D7 with nothing — a recorded absence, which is a finding.
    return LadderResult(
        Adjudication(
            source="deterministic",
            decision="none",
            confidence=0.0,
            rule_ids=["RJ-001"],
            justification=(
                f"No candidate in the target vocabulary denotes {subject.label!r}."
            ),
            decision_path="D7",
        ),
        settled=True,
        review_candidates=[],
        ambiguity_count=0,
    )


def _settled_justification(
    rung: str,
    winner: Candidate,
    conflation: list[str] | None = None,
) -> str:
    """Compose the one-sentence justification for a settled rung.

    Example:
        >>> from mondo_ai.align.models import Signal
        >>> c = Candidate("ICD10CM:Q87.4", [Signal("lexical_variant", "EV-004", 0.97,
        ...               transform="strip_possessive")], tier=4)
        >>> _settled_justification("D4", c)
        "Matched ICD10CM:Q87.4 after the declared rewrite 'strip_possessive'; the rewrite is meaning-preserving, so the labels denote the same concept."
        >>> _settled_justification("D1", Candidate("ICD10CM:R48.1"), ["CM-002"])
        'Primary labels of subject and ICD10CM:R48.1 are identical; accepted across a set-level permitted conceptual-model conflation (CM-002).'
    """
    suffix = (
        f"; accepted across a set-level permitted conceptual-model conflation "
        f"({', '.join(conflation)})."
        if conflation
        else ", and no scope or conceptual-model flag applies."
    )
    if rung == "D1":
        return f"Primary labels of subject and {winner.object_id} are identical{suffix}"
    if rung == "D2":
        return (
            f"Primary labels of subject and {winner.object_id} are identical after "
            f"non-semantic normalisation only{suffix}"
        )
    if rung == "D3":
        return f"An exact synonym links the subject to {winner.object_id}{suffix}"
    transforms = winner.transforms()
    applied = transforms[0] if transforms else "a declared rewrite"
    return (
        f"Matched {winner.object_id} after the declared rewrite {applied!r}; the rewrite "
        f"is meaning-preserving, so the labels denote the same concept."
    )
