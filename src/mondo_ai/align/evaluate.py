"""Evaluation against the subject resource's own asserted cross-references.

Mondo asserts its ICD-10-CM cross-references with precision qualifiers
(``MONDO:equivalentTo``, ``MONDO:mondoIsBroaderThanSource``, …). Those assertions are a
usable gold standard, on one condition: they must be **held out entirely** — not seeded as
anchors, not used as proxy evidence. :class:`~mondo_ai.align.config.EvalConfig` defaults to
holding them out for exactly this reason.

Two things are reported, and the second matters as much as the first:

* precision / recall / F1 on ``skos:exactMatch``, broken down by review tier and by rung, so
  the claim "AUTO_ACCEPT is safe to ship" is a measured claim rather than a design intention;
* the **disagreements**, which are a curation finding in their own right. Mondo's asserted
  mappings are not infallible, and a pipeline that confidently contradicts one has either
  found a bug in itself or a bug in Mondo. Both are worth a curator's time, which is why
  they are written out rather than only counted.

Example:
    >>> m = Metrics(true_positive=8, false_positive=2, false_negative=4)
    >>> m.precision, m.recall
    (0.8, 0.6667)
    >>> m.f1
    0.7273
    >>> Metrics().precision
    0.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any
from dataclasses import dataclass, field

from mondo_ai.align.models import EXACT_MATCH, MappingRecord, ResourceSet


@dataclass(slots=True)
class Metrics:
    """Counts and derived rates for one evaluation slice.

    Attributes:
        true_positive: Predicted exact match agreeing with an asserted one.
        false_positive: Predicted exact match where the gold says a different code, or none.
        false_negative: Asserted exact match the pipeline did not predict.
        true_negative: Subject with no asserted match and none predicted.
    """

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0

    @property
    def precision(self) -> float:
        """Fraction of predicted exact matches that are right.

        Example:
            >>> Metrics(true_positive=3, false_positive=1).precision
            0.75
        """
        denom = self.true_positive + self.false_positive
        return round(self.true_positive / denom, 4) if denom else 0.0

    @property
    def recall(self) -> float:
        """Fraction of asserted exact matches that were found.

        Example:
            >>> Metrics(true_positive=3, false_negative=1).recall
            0.75
        """
        denom = self.true_positive + self.false_negative
        return round(self.true_positive / denom, 4) if denom else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall.

        Example:
            >>> Metrics(true_positive=1, false_positive=1, false_negative=1).f1
            0.5
        """
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def as_dict(self) -> dict[str, float | int]:
        """Return the counts and rates together, for the report and for JSON.

        Example:
            >>> sorted(Metrics().as_dict())
            ['f1', 'false_negative', 'false_positive', 'precision', 'recall', 'true_negative', 'true_positive']
        """
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "true_negative": self.true_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass
class Evaluation:
    """A full evaluation result.

    Attributes:
        overall: Metrics over the **in-scope** subjects only — those the subject resource has
            actually cross-referenced to the object vocabulary. See :func:`evaluate` for why.
        by_tier: In-scope metrics per review tier.
        by_rung: In-scope metrics per decision path.
        predicate_confusion: ``(predicted, asserted)`` predicate pair counts, in scope.
        disagreements: Rows where prediction and assertion differ, plus out-of-scope
            predictions recorded as ``novel_mapping``.
        gold_size: In-scope subjects carrying an asserted exact match.
        evaluated: In-scope subjects.
        out_of_scope: Subjects with no qualified assertion to the object vocabulary.
        novel_predicted: Out-of-scope subjects for which a mapping was predicted anyway.
        gold_retired: In-scope subjects whose only asserted exact targets are retired in the
            object vocabulary, and which are therefore not scored.
    """

    overall: Metrics = field(default_factory=Metrics)
    by_tier: dict[str, Metrics] = field(default_factory=dict)
    by_rung: dict[str, Metrics] = field(default_factory=dict)
    predicate_confusion: Counter = field(default_factory=Counter)
    disagreements: list[dict[str, str]] = field(default_factory=list)
    gold_size: int = 0
    evaluated: int = 0
    out_of_scope: int = 0
    novel_predicted: int = 0
    gold_retired: int = 0


#: Mondo's xref qualifiers, mapped to the SSSOM predicate they assert.
QUALIFIER_PREDICATE = {
    "MONDO:equivalentTo": EXACT_MATCH,
    "MONDO:mondoIsBroaderThanSource": "skos:narrowMatch",
    "MONDO:mondoIsNarrowerThanSource": "skos:broadMatch",
    "MONDO:relatedTo": "skos:relatedMatch",
    "MONDO:closeTo": "skos:closeMatch",
}


def gold_standard(
    subjects: ResourceSet,
    object_prefix: str,
    qualifier: str = "MONDO:equivalentTo",
) -> dict[str, set[str]]:
    """Extract asserted mappings with a given qualifier, as subject -> object ids.

    Args:
        subjects: The loaded subject resource; its concepts must have been loaded with
            xrefs retained.
        object_prefix: Only xrefs with this prefix are considered.
        qualifier: The xref ``source`` qualifier that marks the assertion.

    Example:
        >>> from mondo_ai.align.models import Concept
        >>> rs = ResourceSet(name="m", prefix="MONDO", concepts={
        ...   "MONDO:1": Concept(id="MONDO:1", label="a", xrefs=["ICD10CM:A00", "UMLS:C1"],
        ...       extra={"xref_ICD10CM:A00": "MONDO:equivalentTo",
        ...              "xref_UMLS:C1": "MONDO:equivalentTo"}),
        ...   "MONDO:2": Concept(id="MONDO:2", label="b", xrefs=["ICD10CM:A01"],
        ...       extra={"xref_ICD10CM:A01": "MONDO:mondoIsBroaderThanSource"}),
        ...   "MONDO:3": Concept(id="MONDO:3", label="c", xrefs=["ICD10CM:A02"])})
        >>> gold_standard(rs, "ICD10CM")
        {'MONDO:1': {'ICD10CM:A00'}}
        >>> gold_standard(rs, "ICD10CM", "MONDO:mondoIsBroaderThanSource")
        {'MONDO:2': {'ICD10CM:A01'}}
    """
    out: dict[str, set[str]] = {}
    want = f"{object_prefix}:"
    for c in subjects.concepts.values():
        hits = {
            x for x in c.xrefs
            if x.startswith(want) and c.extra.get(f"xref_{x}") == qualifier
        }
        if hits:
            out[c.id] = hits
    return out


def all_asserted(subjects: ResourceSet, object_prefix: str) -> dict[str, dict[str, str]]:
    """Return every qualified xref, as subject -> {object_id: predicate}.

    Used for the predicate confusion table, where a prediction of ``broadMatch`` against an
    assertion of ``exactMatch`` is a different and more interesting error than a miss.

    Example:
        >>> from mondo_ai.align.models import Concept
        >>> rs = ResourceSet(name="m", prefix="MONDO", concepts={
        ...   "MONDO:1": Concept(id="MONDO:1", label="a", xrefs=["ICD10CM:A00"],
        ...       extra={"xref_ICD10CM:A00": "MONDO:mondoIsBroaderThanSource"})})
        >>> all_asserted(rs, "ICD10CM")
        {'MONDO:1': {'ICD10CM:A00': 'skos:narrowMatch'}}
    """
    out: dict[str, dict[str, str]] = {}
    want = f"{object_prefix}:"
    for c in subjects.concepts.values():
        row = {}
        for x in c.xrefs:
            if not x.startswith(want):
                continue
            pred = QUALIFIER_PREDICATE.get(c.extra.get(f"xref_{x}", ""))
            if pred:
                row[x] = pred
        if row:
            out[c.id] = row
    return out


def evaluate(
    records: list[MappingRecord],
    gold: dict[str, set[str]],
    asserted: dict[str, dict[str, str]] | None = None,
    *,
    objects: ResourceSet | None = None,
) -> Evaluation:
    """Score predictions against a held-out gold standard, scoped honestly.

    **Scope is the whole design of this function.** Mondo cross-references only a small
    fraction of its terms to ICD-10-CM, and the absence of a cross-reference is not a claim
    that no code exists — it usually means nobody has curated that term against ICD-10-CM
    yet. Counting every uncurated prediction as a false positive would make precision a
    measure of Mondo's coverage rather than of this pipeline's correctness; on the smoke run
    that first produced these numbers it reported 0.20 precision, of which every single
    "error" was a plausible new mapping.

    So a subject is **in scope** only if it carries at least one qualified cross-reference to
    the object vocabulary. There, Mondo has looked, and a disagreement is a real disagreement:

    * predicted exact matching an asserted exact → true positive;
    * predicted exact where an exact is asserted to a *different* object → false positive
      **and** false negative, and a recorded ``different_object`` disagreement;
    * predicted exact where the subject is cross-referenced but *not* as exact → false
      positive, because Mondo considered this pair and declined to call it equivalent;
    * asserted exact not predicted → false negative, recorded as ``missed``;
    * no assertion of exact and none predicted → true negative.

    When ``objects`` is given, asserted exact targets that the object vocabulary has
    **retired** are set aside: a source pointing at a retired concept is stale, and scoring
    the live successor as a false positive would measure the source's staleness, not this
    pipeline. A subject whose only gold targets are retired is written as
    ``gold_target_retired`` and scored in neither direction. On the first Mondo × SNOMED CT
    smoke run this was 27 of 70 "false positives" and 27 of 163 "misses".

    Out-of-scope predictions are counted as ``novel_predicted`` and written to the
    disagreement list as ``novel_mapping``. They are candidate additions to Mondo, which is a
    finding, not an error — and deliberately not folded into precision, in either direction.

    Example:
        >>> from mondo_ai.align.models import Adjudication, ReviewTier
        >>> def rec(sid, oid, tier=ReviewTier.AUTO_ACCEPT, path="D1", dec="exact"):
        ...     return MappingRecord(sid, final=Adjudication("deterministic", dec, oid,
        ...         decision_path=path), review_tier=tier)
        >>> records = [rec("M:1", "I:A00"),           # agrees with gold
        ...            rec("M:2", "I:XXX"),           # gold says I:A01
        ...            rec("M:3", "", dec="none"),    # gold says I:A02, missed
        ...            rec("M:4", "I:A09"),           # xreffed, but not as exact
        ...            rec("M:5", "I:Z99"),           # not xreffed at all -> novel
        ...            rec("M:6", "", dec="none")]    # xreffed non-exact, nothing predicted
        >>> gold = {"M:1": {"I:A00"}, "M:2": {"I:A01"}, "M:3": {"I:A02"}}
        >>> asserted = {"M:1": {"I:A00": "skos:exactMatch"},
        ...             "M:2": {"I:A01": "skos:exactMatch"},
        ...             "M:3": {"I:A02": "skos:exactMatch"},
        ...             "M:4": {"I:A09": "skos:narrowMatch"},
        ...             "M:6": {"I:B01": "skos:broadMatch"}}
        >>> ev = evaluate(records, gold, asserted)
        >>> ev.evaluated, ev.out_of_scope, ev.novel_predicted, ev.gold_size
        (5, 1, 1, 3)
        >>> o = ev.overall
        >>> o.true_positive, o.false_positive, o.false_negative, o.true_negative
        (1, 2, 2, 1)
        >>> o.precision, o.recall
        (0.3333, 0.3333)
        >>> sorted({d["kind"] for d in ev.disagreements})
        ['declined_by_source', 'different_object', 'missed', 'novel_mapping']
        >>> ev.by_tier["AUTO_ACCEPT"].true_positive
        1
    """
    asserted = asserted or {}
    ev = Evaluation()
    by_tier: dict[str, Metrics] = defaultdict(Metrics)
    by_rung: dict[str, Metrics] = defaultdict(Metrics)

    for r in records:
        final = r.final
        if final is None:
            continue
        tier = r.review_tier.value
        rung = final.decision_path or "?"
        gold_objects = gold.get(r.subject_id, set())
        subject_asserted = asserted.get(r.subject_id, {})
        if objects is not None and gold_objects:
            retired = {g for g in gold_objects
                       if g in objects.concepts and objects.concepts[g].obsolete}
            gold_objects = gold_objects - retired
            if retired and not gold_objects:
                ev.gold_retired += 1
                ev.disagreements.append(
                    _row(r, final, "gold_target_retired", "|".join(sorted(retired))))
                continue
        predicted_exact = final.predicate == EXACT_MATCH and bool(final.object_id)
        in_scope = bool(subject_asserted) or bool(gold_objects)

        if not in_scope:
            ev.out_of_scope += 1
            if final.asserts_mapping:
                ev.novel_predicted += 1
                ev.disagreements.append(_row(r, final, "novel_mapping", ""))
            continue

        ev.evaluated += 1
        if gold_objects:
            ev.gold_size += 1
        buckets = (ev.overall, by_tier[tier], by_rung[rung])

        if predicted_exact and final.object_id in gold_objects:
            for b in buckets:
                b.true_positive += 1
        elif predicted_exact and gold_objects:
            for b in buckets:
                b.false_positive += 1
                b.false_negative += 1
            ev.disagreements.append(
                _row(r, final, "different_object", "|".join(sorted(gold_objects))))
        elif predicted_exact:
            # The subject is cross-referenced to this vocabulary but no exact match is
            # asserted. Mondo considered the area and declined; a predicted exact here is a
            # contradiction, not an uncurated gap.
            for b in buckets:
                b.false_positive += 1
            ev.disagreements.append(
                _row(r, final, "declined_by_source",
                     "|".join(f"{k} {v}" for k, v in sorted(subject_asserted.items()))))
        elif gold_objects:
            for b in buckets:
                b.false_negative += 1
            ev.disagreements.append(
                _row(r, final, "missed", "|".join(sorted(gold_objects))))
        else:
            for b in buckets:
                b.true_negative += 1

        if final.object_id:
            ev.predicate_confusion[
                (final.predicate, subject_asserted.get(final.object_id, "none"))] += 1

    ev.by_tier = dict(sorted(by_tier.items()))
    ev.by_rung = dict(sorted(by_rung.items()))
    return ev


def _row(
    record: MappingRecord,
    final: Any,
    kind: str,
    asserted: str,
) -> dict[str, str]:
    """Build one disagreement row.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> a = Adjudication("agent", "exact", "I:A00", decision_path="A1")
        >>> _row(MappingRecord("M:1", "x"), a, "missed", "I:A01")["kind"]
        'missed'
    """
    return {
        "kind": kind,
        "subject_id": record.subject_id,
        "subject_label": record.subject_label,
        "predicted": final.object_id,
        "asserted": asserted,
        "predicted_predicate": final.predicate,
        "decision_path": final.decision_path,
        "review_tier": record.review_tier.value,
        "justification": final.justification,
    }
