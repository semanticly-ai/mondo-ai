"""The run report: what happened, how good it is, and what to look at first.

Written so that a curator who reads only this file knows three things: how many rows need
their attention, whether the rows that do *not* need it are actually safe, and what the top
of the queue is. Coverage is reported without apology — Mondo is dominated by gene-level and
numbered OMIM subtypes for which ICD-10-CM has no counterpart, so a low exact-match rate is a
property of the vocabularies, not a failure of the run.

Example:
    >>> md_table([["a", "b"], ["1", "2"]])
    '| a | b |\\n| --- | --- |\\n| 1 | 2 |'
    >>> md_table([])
    ''
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Sequence

from mondo_ai.align.evaluate import Evaluation
from mondo_ai.align.models import MappingRecord, ResourceSet
from mondo_ai.align.rules import REGISTRY
from mondo_ai.align.triage import WEIGHTS


def md_table(rows: Sequence[Sequence[object]]) -> str:
    """Render a markdown table from a header row plus body rows.

    Example:
        >>> print(md_table([["id", "n"], ["D1", "10"]]))
        | id | n |
        | --- | --- |
        | D1 | 10 |
    """
    if not rows:
        return ""
    header, *body = rows
    out = ["| " + " | ".join(str(c) for c in header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    out.extend("| " + " | ".join(str(c) for c in r) + " |" for r in body)
    return "\n".join(out)


def rule_histogram(records: list[MappingRecord], limit: int = 20) -> list[tuple[str, int]]:
    """Count how often each curation rule was cited in a final verdict.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rs = [MappingRecord("M:1", final=Adjudication("x", "exact", "o",
        ...           rule_ids=["EV-001", "PR-001"])),
        ...       MappingRecord("M:2", final=Adjudication("x", "none", rule_ids=["EV-001"]))]
        >>> rule_histogram(rs)
        [('EV-001', 2), ('PR-001', 1)]
    """
    counter: Counter[str] = Counter()
    for r in records:
        if r.final is not None:
            counter.update(r.final.rule_ids)
    return counter.most_common(limit)


def write_report(
    path: Any,
    *,
    config: Any,
    subjects: ResourceSet,
    objects: ResourceSet,
    records: list[MappingRecord],
    evaluation: Evaluation | None,
    timings: dict[str, float],
    agent_stats: dict[str, Any],
    boomer_stats: dict[str, Any] | None = None,
    extra_sections: list[str] | None = None,
) -> str:
    """Write the markdown run report and return its text.

    Example:
        >>> import tempfile, pathlib
        >>> from mondo_ai.align.config import AlignmentConfig, ResourceSpec
        >>> from mondo_ai.align.models import Adjudication, ReviewTier
        >>> cfg = AlignmentConfig(run_id="r",
        ...     subject=ResourceSpec(name="mondo", prefix="MONDO", format="obo", path="m"),
        ...     object=ResourceSpec(name="icd", prefix="ICD10CM", format="obo", path="i"))
        >>> rec = MappingRecord("MONDO:1", "Cholera", object_label="Cholera",
        ...     final=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99,
        ...         rule_ids=["EV-001", "PR-001"], decision_path="D1",
        ...         justification="Identical labels."),
        ...     review_tier=ReviewTier.AUTO_ACCEPT)
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "report.md"
        >>> text = write_report(p, config=cfg,
        ...     subjects=ResourceSet(name="mondo", prefix="MONDO"),
        ...     objects=ResourceSet(name="icd", prefix="ICD10CM"),
        ...     records=[rec], evaluation=None, timings={"load": 1.0},
        ...     agent_stats={"enabled": False})
        >>> "# Alignment run `r`" in text
        True
        >>> "AUTO_ACCEPT" in text and "maprule:EV-001" in text
        True
        >>> "## Consistency conditioning (Boomer)" in text
        True
        >>> p.exists()
        True
    """
    tiers = Counter(r.review_tier.value for r in records)
    total = len(records) or 1
    mapped = sum(1 for r in records if r.final is not None and r.final.asserts_mapping)

    lines: list[str] = [
        f"# Alignment run `{config.run_id}`",
        "",
        f"{subjects.name} → {objects.name}, {date.today().isoformat()}.",
        "",
        "## Sources",
        "",
        md_table([
            ["role", "resource", "concepts", "version", "sha256", "retrieved"],
            ["subject", subjects.name, f"{len(subjects):,}", subjects.version or "—",
             subjects.sha256[:16] or "—", subjects.retrieved or "—"],
            ["object", objects.name, f"{len(objects):,}", objects.version or "—",
             objects.sha256[:16] or "—", objects.retrieved or "—"],
        ]),
        "",
        f"Use case: {config.use_case}",
        "",
        "## Review tiers",
        "",
        f"This is the answer to *what needs a human*: "
        f"**{tiers.get('EXPERT_REVIEW', 0):,} rows**, "
        f"{tiers.get('EXPERT_REVIEW', 0) / total:.1%} of the set.",
        "",
        md_table(
            [["tier", "rows", "share", "meaning"]]
            + [
                [tier, f"{tiers.get(tier, 0):,}", f"{tiers.get(tier, 0) / total:.1%}", meaning]
                for tier, meaning in (
                    ("AUTO_ACCEPT", "settled by a strong deterministic rung, nothing against it"),
                    ("SPOT_CHECK", "defensible; sample it"),
                    ("EXPERT_REVIEW", "**a human decides** — an adjudicator looked and could not settle it"),
                    ("QUEUED", "nobody has looked yet; the adjudication budget did not reach it"),
                    ("REJECTED", "actively decided against, with a rule"),
                    ("NO_MATCH", "recorded absence (`sssom:NoTermFound` + `RJ-001`/`RJ-004`)"),
                )
            ]
        ),
        "",
        "`QUEUED` and `EXPERT_REVIEW` are separate on purpose. `EXPERT_REVIEW` is the "
        "curator's worklist: an adjudicator examined the pair and a human has to decide. "
        "`QUEUED` is the absence of a verdict — raising `agent.max_items` converts those rows "
        "into real verdicts, and mixing them into `EXPERT_REVIEW` would bury the questions "
        "somebody has actually formed.",
        "",
        f"{mapped:,} of {len(records):,} subjects ({mapped / total:.1%}) carry a mapping of "
        f"any predicate. A low rate here is expected: the subject vocabulary is dominated by "
        f"gene-level and numbered subtypes for which the object vocabulary has no counterpart, "
        f"and a `NO_MATCH` row with `RJ-001` is the correct output in those cases.",
        "",
        "## Decision paths",
        "",
        md_table(
            [["rung / path", "rows", "what it means"]]
            + [
                [path, f"{count:,}", _PATH_MEANING.get(path, "")]
                for path, count in sorted(
                    Counter(
                        r.final.decision_path for r in records if r.final is not None
                    ).items()
                )
            ]
        ),
        "",
        "## Predicates",
        "",
        md_table(
            [["predicate", "rows"]]
            + [
                [pred, f"{count:,}"]
                for pred, count in Counter(
                    r.final.predicate for r in records if r.final is not None
                ).most_common()
            ]
        ),
        "",
        "## Curation rules cited",
        "",
        md_table(
            [["rule", "title", "rows"]]
            + [
                [f"`maprule:{rid}`", REGISTRY[rid].title if rid in REGISTRY else "?",
                 f"{count:,}"]
                for rid, count in rule_histogram(records)
            ]
        ),
        "",
        "## Structural defects",
        "",
    ]

    defects = Counter(f for r in records for f in r.structural_flags)
    if defects:
        lines.append(md_table(
            [["rule", "title", "subjects"]]
            + [[f"`maprule:{f}`", REGISTRY[f].title if f in REGISTRY else "?", f"{n:,}"]
               for f in sorted(defects) for n in [defects[f]]]
        ))
        lines.append("")
        lines.append(
            "A reverse proxy merge (`ST-002`) means two subject terms claim the same object "
            "code as an exact match. At least one is wrong, and no single row shows it, which "
            "is why every subject involved is escalated to `EXPERT_REVIEW`."
        )
    else:
        lines.append("None detected.")
    lines.append("")

    lines += [
        "## Agent adjudication",
        "",
        md_table(
            [["property", "value"]]
            + [[k, str(v)] for k, v in sorted(agent_stats.items())]
        ),
        "",
        "Rows the agent produced carry `semapv:CompositeMatching` and are Tier 3 under the "
        "Mondo Mapping Curation Guide: AI proposals for review, not curated assertions.",
        "",
    ]

    lines += _boomer_section(records, boomer_stats or {})

    if evaluation is not None:
        lines += _evaluation_section(evaluation)

    lines += [
        "## Review queue — top 25 by priority",
        "",
        f"`review_priority` is a transparent weighted sum of "
        f"{', '.join(f'`{k}` ({v})' for k, v in WEIGHTS.items())}.",
        "",
        md_table(
            [["priority", "tier", "subject", "→ object", "why", "justification"]]
            + [
                [
                    f"{r.review_priority:.3f}",
                    r.review_tier.value,
                    f"{r.subject_id} {r.subject_label}"[:60],
                    f"{r.final.object_id} {r.object_label}"[:50] if r.final else "",
                    _top_reason(r),
                    (r.final.justification if r.final else "")[:140],
                ]
                for r in records[:25]
            ]
        ),
        "",
        "## Timings",
        "",
        md_table(
            [["stage", "seconds"]]
            + [[k, f"{v:.1f}"] for k, v in timings.items()]
        ),
        "",
        "## Files",
        "",
        "| file | contents |",
        "| --- | --- |",
        "| `mappings.sssom.tsv` | the mapping set; non-standard columns declared in "
        "`extension_definitions` |",
        "| `system_table.tsv` | this queue, every row, ranked |",
        "| `records.jsonl` | full audit trail: every candidate, signal, flag and verdict |",
        "| `alignment.duckdb` | both tables, queryable |",
        "| `run.yaml` | config plus source checksums, for reproduction |",
        "| `disagreements.tsv` | where the run contradicts the subject resource's own xrefs |",
        "",
    ]
    for section in extra_sections or []:
        lines += [section, ""]

    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


_PATH_MEANING = {
    "D0": "object identifier is not a pullable code (`ST-005`)",
    "D1": "identical primary labels (`EV-001`)",
    "D2": "identical after non-semantic normalisation (`EV-002`)",
    "D3": "exact-synonym crossing (`EV-003`)",
    "D4": "one declared meaning-preserving rewrite (`EV-004`)",
    "D5": "lexical hit blocked by a scope flag — handed to adjudication",
    "D6": "several objects tied at the winning tier (`PR-008`)",
    "D7": "retrieval-only candidates above the viability floor",
    "D8": "retrieval returned token-overlap noise only (`RJ-004`)",
    "A1": "agent adjudication",
    "A1-cached": "agent adjudication, served from cache",
    "A1-failed": "agent response failed validation — escalated (`RJ-002`)",
}


def _top_reason(record: MappingRecord) -> str:
    """Return the dominant priority component for a record, for the report table.

    Example:
        >>> _top_reason(MappingRecord("M:1", priority_components={"ambiguity": 1.0}))
        'ambiguity'
        >>> _top_reason(MappingRecord("M:1"))
        '—'
    """
    weighted = {
        k: WEIGHTS[k] * v
        for k, v in record.priority_components.items()
        if k in WEIGHTS and v > 0
    }
    if not weighted:
        return "—"
    return max(weighted.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _evaluation_section(ev: Evaluation) -> list[str]:
    """Render the evaluation section.

    Example:
        >>> from mondo_ai.align.evaluate import Evaluation, Metrics
        >>> e = Evaluation(overall=Metrics(true_positive=5, false_positive=1),
        ...                gold_size=6, evaluated=10, out_of_scope=4, novel_predicted=2)
        >>> "0.8333" in "\\n".join(_evaluation_section(e))
        True
    """
    o = ev.overall
    lines = [
        "## Evaluation against held-out asserted mappings",
        "",
        "The subject resource's own cross-references to the object vocabulary were held out "
        "of the run entirely — not seeded as anchors, not used as proxy evidence — and then "
        "used as a gold standard.",
        "",
        f"**Scope.** {ev.evaluated:,} subjects are in scope: those the subject resource has "
        f"cross-referenced to this vocabulary at all, of which {ev.gold_size:,} carry an "
        f"asserted exact match. {ev.out_of_scope:,} subjects are out of scope — no "
        f"cross-reference exists, so the source has not looked, and absence is not a claim "
        f"that no code exists. Predictions there ({ev.novel_predicted:,} of them) are counted "
        f"as `novel_mapping` candidate additions and are **not** folded into precision in "
        f"either direction. Scoring them as errors would make precision a measure of the "
        f"source's coverage rather than of this pipeline's correctness."
        + (f" A further {ev.gold_retired:,} subjects assert exact matches only to concepts "
           f"the object vocabulary has retired; they are written as `gold_target_retired` "
           f"and not scored, because a stale assertion is no measure of the pipeline."
           if ev.gold_retired else ""),
        "",
        md_table([
            ["slice", "TP", "FP", "FN", "TN", "precision", "recall", "F1"],
            ["overall", o.true_positive, o.false_positive, o.false_negative,
             o.true_negative, o.precision, o.recall, o.f1],
        ]),
        "",
        "### By review tier",
        "",
        "The row that matters: `AUTO_ACCEPT` precision is the measured claim that those rows "
        "can ship without a human.",
        "",
        md_table(
            [["tier", "TP", "FP", "FN", "precision", "recall"]]
            + [
                [tier, m.true_positive, m.false_positive, m.false_negative,
                 m.precision, m.recall]
                for tier, m in ev.by_tier.items()
            ]
        ),
        "",
        "### By decision path",
        "",
        md_table(
            [["path", "TP", "FP", "FN", "precision"]]
            + [
                [path, m.true_positive, m.false_positive, m.false_negative, m.precision]
                for path, m in ev.by_rung.items()
            ]
        ),
        "",
        "### Predicate confusion",
        "",
        "Predicted predicate against the qualifier the subject resource asserts for the same "
        "object. `none` means no qualified assertion exists for that pair.",
        "",
        md_table(
            [["predicted", "asserted", "rows"]]
            + [
                [pred, asserted, f"{n:,}"]
                for (pred, asserted), n in ev.predicate_confusion.most_common(15)
            ]
        ),
        "",
        "### Disagreements",
        "",
        f"{len(ev.disagreements):,} rows written to `disagreements.tsv`. These are a curation "
        f"finding, not only an error signal.",
        "",
        "| kind | meaning |",
        "| --- | --- |",
        "| `novel_mapping` | out of scope: the source has no cross-reference here. A "
        "candidate addition. |",
        "| `different_object` | both name an exact match, and they disagree. One side has to "
        "lose. |",
        "| `declined_by_source` | the source cross-referenced this term but declined to call "
        "it equivalent. The strongest kind of contradiction, because somebody looked. |",
        "| `missed` | an asserted exact match this pipeline did not find. |",
        "",
        md_table(
            [["kind", "rows"]]
            + [[k, f"{n:,}"]
               for k, n in Counter(d["kind"] for d in ev.disagreements).most_common()]
        ),
        "",
    ]
    return lines


def _b(record: MappingRecord) -> dict[str, Any]:
    """Return a record's Boomer payload, or an empty dict.

    Example:
        >>> _b(MappingRecord("M:1"))
        {}
    """
    return record.boomer or {}


def _boomer_section(
    records: list[MappingRecord],
    stats: dict[str, Any],
) -> list[str]:
    """Render what consistency conditioning changed.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("M:1", "x", final=Adjudication("agent", "exact", "I:A", 0.9))
        >>> rec.boomer = {"prior": 0.9, "posterior": 0.04, "predicate": "skos:narrowMatch",
        ...               "agrees_with_pipeline": False, "clique_size": 3,
        ...               "clique_confidence": 0.92, "method": "exhaustive search"}
        >>> stats = {"ok": True, "hypotheses": 1, "interacting_cliques": 1}
        >>> out = chr(10).join(_boomer_section([rec], stats))
        >>> "0.900" in out and "0.040" in out
        True
        >>> "Where consistency disagrees most" in out
        True
        >>> off = chr(10).join(_boomer_section([], {"enabled": False}))
        >>> "Disabled for this run" in off
        True
        >>> broken = chr(10).join(_boomer_section([], {"enabled": True, "ok": False,
        ...                                           "error": "not found"}))
        >>> "Boomer did not run" in broken and "not found" in broken
        True
    """
    lines = ["## Consistency conditioning (Boomer)", ""]
    if not stats.get("enabled", True):
        lines += ["Disabled for this run (`boomer.enabled: false`), so no `boomer_prior` or "
                  "`boomer_posterior` columns were written.", ""]
        return lines
    if not stats.get("ok"):
        reason = stats.get("error") or stats.get("note") or "no reason recorded"
        lines += [
            "Boomer did not run, so the Boomer columns are empty. This does not invalidate "
            "the rest of the run — conditioning is additional evidence, not a prerequisite.",
            "",
            "```",
            str(reason)[:600],
            "```",
            "",
        ]
        return lines

    lines += [
        "The pipeline's own confidence is local: it looks at one subject and its candidates. "
        "Boomer looks at the set — it takes every asserted mapping as a hypothesis with that "
        "confidence as its prior, adds both sources' subclass axioms as hard constraints, and "
        "returns the most probable assignment that is actually *consistent*.",
        "",
        md_table([["property", "value"]]
                 + [[k, str(v)] for k, v in sorted(stats.items())]),
        "",
        "`boomer_prior` is the probability we put on the relation we asserted. "
        "`boomer_posterior` is that relation's **max-marginal** after conditioning: for each "
        "relation the mapping could take, the score of the best consistent assignment of its "
        "whole clique under that choice, normalised. The two coincide exactly for a mapping "
        "nothing constrains — which is the sanity check that the arithmetic is right.",
        "",
    ]

    conditioned = [r for r in records if r.boomer]

    def _drop(record: MappingRecord) -> float:
        """How much probability consistency took off this row's asserted relation."""
        data = record.boomer or {}
        posterior = data.get("posterior")
        prior = data.get("prior")
        if posterior is None or prior is None:
            return 0.0
        return float(prior) - float(posterior)

    moved = sorted((r for r in conditioned if _drop(r) > 0.05), key=_drop, reverse=True)
    disagree = [
        r for r in conditioned if not (r.boomer or {}).get("agrees_with_pipeline", True)
    ]

    unconstrained = [r for r in conditioned if not _b(r).get("constrained", True)]
    lines += [
        f"{len(conditioned):,} mappings were conditioned, of which {len(unconstrained):,} "
        f"are unconstrained — nothing in the set can move them, so their posterior equals "
        f"their prior exactly. Of the rest, consistency argues against {len(moved):,} "
        f"(posterior more than 0.05 below prior) and picks a different predicate for "
        f"{len(disagree):,}.",
        "",
    ]

    if disagree:
        shifts = Counter(
            ((r.final.predicate if r.final else "?"), _b(r).get("predicate", "?"))
            for r in disagree
        )
        lines += [
            "### The shape of the correction",
            "",
            "Which predicate we asserted against which one consistency chose instead. A run "
            "whose corrections all point one way is telling you something about the "
            "pipeline's bias, not just about individual rows.",
            "",
            md_table(
                [["we asserted", "Boomer chose", "rows"]]
                + [
                    [ours.replace("skos:", ""), theirs.replace("skos:", ""), f"{n:,}"]
                    for (ours, theirs), n in shifts.most_common(10)
                ]
            ),
            "",
        ]
    if moved:
        lines += [
            "### Where consistency disagrees most",
            "",
            "These are the highest-yield rows in the set: the pipeline was confident and the "
            "constraints would not allow it. Each is a curation finding no amount of local "
            "confidence would have produced.",
            "",
            md_table(
                [["prior", "posterior", "subject", "our predicate", "→ object",
                  "Boomer's predicate", "clique"]]
                + [
                    [
                        f"{float(_b(r)['prior']):.3f}",
                        f"{float(_b(r)['posterior']):.3f}",
                        f"{r.subject_id} {r.subject_label}"[:52],
                        (r.final.predicate if r.final else "").replace("skos:", ""),
                        f"{r.final.object_id if r.final else ''} {r.object_label}"[:42],
                        str(_b(r).get("predicate", "")).replace("skos:", ""),
                        str(_b(r).get("clique_size", "")),
                    ]
                    for r in moved[:20]
                ]
            ),
            "",
        ]
    return lines
