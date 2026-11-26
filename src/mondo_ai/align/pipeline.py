"""End-to-end orchestration of an alignment run.

Stage order is fixed and each stage's output is inspectable, because the useful failure mode
of a mapping pipeline is not a crash — it is a plausible wrong answer, and the only defence is
being able to see what each stage decided.

The one ordering subtlety: the agent stage runs on a *priority-ordered* slice of the unsettled
queue, not on an arbitrary prefix. With ``agent.max_items`` set, the items that get a model's
attention are the ones where the deterministic stage found the most to worry about. The rest
stay queued as ``EXPERT_REVIEW`` and say so, rather than silently appearing as "no match".

Example:
    >>> from mondo_ai.align.models import Candidate, Concept
    >>> preliminary_priority(Concept(id="M:1", label="x", n_descendants=100),
    ...                      [Candidate("I:1", flags=["SC-001", "SC-007"])], 1) > 0.1
    True
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from mondo_ai.align.adapters import load_resource
from mondo_ai.align.agent import (
    AgentAdjudicator,
    AnthropicCompleter,
    render_item,
)
from mondo_ai.align.candidates import CandidateGenerator
from mondo_ai.align.config import AlignmentConfig
from mondo_ai.align.deterministic import adjudicate
from mondo_ai.align.emit import (
    set_metadata,
    tier_counts,
    write_duckdb,
    write_records,
    write_sssom,
    write_system_table,
)
from mondo_ai.align.evaluate import Evaluation, all_asserted, evaluate, gold_standard
from mondo_ai.align.flags import subject_flags as detect_subject_flags
from mondo_ai.align.index import build_index
from mondo_ai.align.models import Candidate, Concept, MappingRecord, ResourceSet
from mondo_ai.align.provenance import sha256_file
from mondo_ai.align.report import write_report
from mondo_ai.align.triage import finalise, impact_score, triage


#: Ceiling on a consolidated row's prior for Boomer; the deterministic ladder's own maximum.
MAX_SEED_PRIOR = 0.99


def _obo_iri(prefix: str) -> str:
    """Fall back to the OBO IRI pattern for a prefix with no declared ``iri_base``.

    Only reached for OBO-format resources, where the IRI is a documented convention rather
    than something the config has to restate.

    Example:
        >>> _obo_iri("MONDO")
        'http://purl.obolibrary.org/obo/MONDO_'
    """
    return f"http://purl.obolibrary.org/obo/{prefix}_"


def preliminary_priority(
    subject: Concept,
    candidates: list[Candidate],
    ambiguity: int,
) -> float:
    """Score an unsettled item before the agent has seen it, to order the queue.

    A cheaper cousin of :func:`~mondo_ai.align.triage.review_priority`: the real score needs
    the agent's verdict, and this one decides who gets one. It weighs the two things known in
    advance — how contested the item is and how much damage a wrong answer does.

    Example:
        >>> from mondo_ai.align.models import Candidate, Concept
        >>> leaf = Concept(id="M:1", label="x")
        >>> group = Concept(id="M:2", label="y", n_descendants=500)
        >>> plain = [Candidate("I:1")]
        >>> preliminary_priority(group, plain, 1) > preliminary_priority(leaf, plain, 1)
        True
        >>> hard = [Candidate("I:1", flags=["SC-001", "SC-007"])]
        >>> preliminary_priority(leaf, hard, 1) > preliminary_priority(leaf, plain, 1)
        True
        >>> preliminary_priority(leaf, plain, 4) > preliminary_priority(leaf, plain, 1)
        True
    """
    coextensive = 1.0 if any(
        "SC-007" in c.flags and any(f in ("SC-001", "SC-002") for f in c.flags)
        for c in candidates
    ) else 0.0
    contested = min(1.0, max(0, ambiguity - 1) / 4.0)
    strongest = min((c.tier for c in candidates), default=9)
    # A blocked string-identity hit is a more consequential judgement than pure retrieval
    # noise, so it is worth a model call sooner.
    lexical = 1.0 if strongest <= 4 else 0.0
    return round(
        0.35 * coextensive
        + 0.25 * contested
        + 0.25 * lexical
        + 0.15 * impact_score(subject.n_descendants),
        4,
    )


@dataclass
class RunResult:
    """Everything a completed run produced.

    Attributes:
        records: Every record, triaged and sorted by descending priority.
        subjects: The loaded subject resource.
        objects: The loaded object resource.
        evaluation: Evaluation result, if evaluation ran.
        consolidated: The pair-level reconciled set, if reconciliation ran.
        timings: Seconds per stage.
        agent_stats: Agent call/cache/failure counts.
        boomer_stats: Boomer hypothesis/clique counts and conflict tallies.
        paths: Written output files by role.
    """

    records: list[MappingRecord]
    subjects: ResourceSet
    objects: ResourceSet
    evaluation: Evaluation | None = None
    consolidated: list[Any] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    agent_stats: dict[str, Any] = field(default_factory=dict)
    boomer_stats: dict[str, Any] = field(default_factory=dict)
    paths: dict[str, Path] = field(default_factory=dict)


class AlignmentRunner:
    """Runs one alignment configuration end to end.

    Args:
        config: The run config.
        log: Where progress goes. Defaults to printing, because a full run takes minutes and
            silence is indistinguishable from a hang.
        completer: Transport override for the agent stage, for tests.
    """

    def __init__(
        self,
        config: AlignmentConfig,
        *,
        log: Callable[[str], None] | None = None,
        completer: Any = None,
    ) -> None:
        self.config = config
        self.log = log if log is not None else print
        self.completer = completer
        self.timings: dict[str, float] = {}

    def _timed(self, name: str, fn: Callable[[], Any]) -> Any:
        start = time.time()
        out = fn()
        self.timings[name] = time.time() - start
        self.log(f"  {name}: {self.timings[name]:.1f}s")
        return out

    # --- stages -----------------------------------------------------------------------

    def load(self) -> tuple[ResourceSet, ResourceSet]:
        """Load both resources.

        The subject adapter is asked to retain xrefs to the object prefix, because those are
        both the evaluation gold standard and the optional proxy-evidence source.
        """
        subj_kwargs = self.config.subject.adapter_kwargs()
        if self.config.subject.format == "obo":
            subj_kwargs.setdefault("keep_xref_prefixes", (self.config.object.prefix,))
        subjects = self._timed(
            "load_subject",
            lambda: load_resource(self.config.subject.format, **subj_kwargs),
        )
        objects = self._timed(
            "load_object",
            lambda: load_resource(
                self.config.object.format, **self.config.object.adapter_kwargs()
            ),
        )
        self.log(f"  subject {subjects.name}: {len(subjects):,} concepts, "
                 f"{len(subjects.active()):,} active")
        self.log(f"  object  {objects.name}: {len(objects):,} concepts, "
                 f"{len(objects.active()):,} active")
        return subjects, objects

    def select_subjects(self, subjects: ResourceSet) -> list[Concept]:
        """Return the subjects to process, in a stable order.

        Ordering is by identifier, not by dictionary order, so a capped run is reproducible.
        """
        pool = subjects.active()
        if self.config.subject_id_file:
            wanted = {
                line.strip()
                for line in Path(self.config.subject_id_file).read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
            pool = [c for c in pool if c.id in wanted]
        pool.sort(key=lambda c: c.id)
        if self.config.limit_subjects:
            pool = pool[: self.config.limit_subjects]
        return pool

    def run(self) -> RunResult:
        """Execute every stage and write every output file.

        Returns:
            The :class:`RunResult`.
        """
        cfg = self.config
        out_dir = cfg.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        self.log("[1/8] loading resources")
        subjects, objects = self.load()

        gold = gold_standard(
            subjects, cfg.object.prefix, cfg.evaluation.gold_qualifier
        ) if cfg.evaluation.enabled else {}
        if cfg.evaluation.enabled:
            self.log(f"  gold standard: {len(gold):,} subjects with an asserted "
                     f"{cfg.evaluation.gold_qualifier}")

        seed_mappings: dict[str, list[str]] = {}
        if not cfg.evaluation.hold_out:
            seed_mappings = {k: sorted(v) for k, v in gold.items()}
            self.log(f"  seeding {len(seed_mappings):,} asserted mappings as proxy evidence "
                     f"(evaluation.hold_out is false, so evaluation numbers are NOT honest)")

        self.log(f"[2/8] indexing {objects.name}")
        index = self._timed("index", lambda: build_index(objects.active()))

        generator = CandidateGenerator(
            index,
            objects,
            object_prefix=cfg.object.prefix,
            max_candidates=cfg.candidates.max_candidates,
            max_per_category=cfg.candidates.max_per_category,
            retrieval_limit=cfg.candidates.retrieval_limit,
            enable_fuzzy=cfg.candidates.enable_fuzzy,
            seed_mappings=seed_mappings,
        )

        pool = self.select_subjects(subjects)
        self.log(f"[3/8] generating candidates and running the ladder for {len(pool):,} subjects")
        records, unsettled = self._timed(
            "candidates_and_ladder", lambda: self._ladder_pass(pool, generator, subjects)
        )

        self.log(f"[4/8] agent adjudication: {len(unsettled):,} unsettled")
        agent_stats = self._timed(
            "agent", lambda: self._agent_pass(records, unsettled, subjects, objects)
        )

        self.log("[5/8] boomer: conditioning on consistency")
        # finalise() before Boomer, triage() after: Boomer needs the final verdicts to build
        # its hypothesis table, and its answer then feeds review_priority.
        finalise(records)
        boomer_stats = self._timed(
            "boomer", lambda: self._boomer_pass(records, subjects, objects, out_dir))

        self.log("[6/8] triage")
        self._timed(
            "triage",
            lambda: triage(records, permitted_conflations=cfg.conflations_permitted),
        )
        for r in records:
            if r.final is not None and r.final.object_id:
                obj = objects.concepts.get(r.final.object_id)
                r.object_label = obj.label if obj else ""

        evaluation = None
        if cfg.evaluation.enabled:
            self.log("[7/8] evaluation")
            asserted = all_asserted(subjects, cfg.object.prefix)
            evaluation = self._timed(
                "evaluate", lambda: evaluate(records, gold, asserted, objects=objects)
            )
            o = evaluation.overall
            self.log(f"  exactMatch precision {o.precision} recall {o.recall} F1 {o.f1} "
                     f"(TP {o.true_positive} FP {o.false_positive} FN {o.false_negative})")

        consolidated: list[Any] = []
        reconcile_stats: dict[str, Any] = {}
        if cfg.reconcile.enabled:
            self.log("[7/8] reconciling against seed mapping sets")
            consolidated, reconcile_stats = self._timed(
                "reconcile",
                lambda: self._reconcile_pass(records, subjects, objects, generator, out_dir),
            )

        self.log("[8/8] writing outputs")
        paths = self._timed(
            "emit",
            lambda: self._emit(out_dir, records, subjects, objects, evaluation, agent_stats,
                               boomer_stats, consolidated, reconcile_stats),
        )

        return RunResult(
            records=records,
            subjects=subjects,
            objects=objects,
            evaluation=evaluation,
            consolidated=consolidated,
            timings=self.timings,
            agent_stats=agent_stats,
            boomer_stats=boomer_stats,
            paths=paths,
        )

    # --- stage implementations ---------------------------------------------------------

    def _ladder_pass(
        self,
        pool: list[Concept],
        generator: CandidateGenerator,
        subjects: ResourceSet,
    ) -> tuple[list[MappingRecord], list[tuple[float, MappingRecord]]]:
        """Generate candidates and run the ladder for every subject."""
        records: list[MappingRecord] = []
        unsettled: list[tuple[float, MappingRecord]] = []
        for n, subject in enumerate(pool, 1):
            if n % 5000 == 0:
                self.log(f"    {n:,}/{len(pool):,}")
            candidates = generator.generate(subject)
            generator.annotate_coextensive(subject, candidates)
            sflags = detect_subject_flags(subject)
            result = adjudicate(
                subject,
                candidates,
                subject_flags=sflags,
                min_retrieval_score=self.config.candidates.min_retrieval_score,
            )

            record = MappingRecord(
                subject_id=subject.id,
                subject_label=subject.label,
                subject_definition=subject.definition,
                candidates=candidates,
                deterministic=result.adjudication,
                ambiguity_count=result.ambiguity_count,
            )
            record.priority_components = {"impact": impact_score(subject.n_descendants)}
            records.append(record)
            if not result.settled and result.review_candidates:
                unsettled.append((
                    preliminary_priority(subject, result.review_candidates,
                                         result.ambiguity_count),
                    record,
                ))
        unsettled.sort(key=lambda kv: (-kv[0], kv[1].subject_id))
        return records, unsettled

    def _agent_pass(
        self,
        records: list[MappingRecord],
        unsettled: list[tuple[float, MappingRecord]],
        subjects: ResourceSet,
        objects: ResourceSet,
    ) -> dict[str, Any]:
        """Adjudicate the highest-priority unsettled items with the model."""
        cfg = self.config.agent
        stats: dict[str, Any] = {
            "enabled": cfg.enabled,
            "model": cfg.model if cfg.enabled else "",
            "concurrency": cfg.concurrency,
            "batch_size": cfg.batch_size,
            "unsettled_total": len(unsettled),
            "sent": 0,
            "api_calls": 0,
            "cache_hits": 0,
            "validation_failures": 0,
            "deferred_to_expert": 0,
        }
        if not cfg.enabled or not unsettled:
            stats["deferred_to_expert"] = len(unsettled)
            self._defer(record for _, record in unsettled)
            return stats

        selected = unsettled if cfg.max_items == 0 else unsettled[: cfg.max_items]
        deferred = [] if cfg.max_items == 0 else unsettled[cfg.max_items:]
        stats["sent"] = len(selected)
        stats["deferred_to_expert"] = len(deferred)
        self._defer(record for _, record in deferred)

        by_id = {record.subject_id: record for _, record in selected}
        items = [
            render_item(
                subjects.concepts[record.subject_id],
                record.candidates,
                subjects.concepts,
                objects.concepts,
            )
            for _, record in selected
        ]

        completer = self.completer or AnthropicCompleter(
            model=cfg.model, max_tokens=cfg.max_tokens, effort=cfg.effort
        )
        adjudicator = AgentAdjudicator(
            completer=completer,
            model=cfg.model,
            batch_size=cfg.batch_size,
            concurrency=cfg.concurrency,
            cache_dir=cfg.cache_dir or None,
            on_progress=lambda done, total: (
                self.log(f"    {done:,}/{total:,}") if done % (cfg.batch_size * 5) == 0 else None
            ),
        )
        verdicts = adjudicator.adjudicate(items)
        for subject_id, verdict in verdicts.items():
            record = by_id.get(subject_id)
            if record is not None:
                record.agent = verdict

        stats["api_calls"] = adjudicator.calls
        stats["cache_hits"] = adjudicator.cache_hits
        stats["validation_failures"] = len(adjudicator.failures)

        usage = getattr(completer, "usage", None)
        if usage:
            from mondo_ai.align.agent import estimate_cost

            stats.update({f"tokens_{k}": v for k, v in usage.items()})
            cost = estimate_cost(cfg.model, usage)
            stats["cost_usd"] = cost if cost is not None else "unpriced model"
            if stats["sent"]:
                per_item = (cost / stats["sent"]) if cost is not None else None
                stats["cost_usd_per_item"] = (
                    round(per_item, 5) if per_item is not None else "unpriced model")
                if per_item is not None and stats["deferred_to_expert"]:
                    stats["cost_usd_to_finish_queue"] = round(
                        per_item * stats["deferred_to_expert"], 2)
        return stats

    def _boomer_pass(
        self,
        records: list[MappingRecord],
        subjects: ResourceSet,
        objects: ResourceSet,
        out_dir: Path,
    ) -> dict[str, Any]:
        """Condition the asserted mappings on consistency, in one or two Boomer passes.

        Pass one submits every asserted mapping as a hypothesis with the pipeline's own
        confidence as its prior, plus both sources' subclass axioms over the mapped signature
        as hard constraints. Boomer partitions the hypotheses into cliques and returns the
        most probable *consistent* assignment.

        Pass two, optional, produces the per-mapping posteriors: for each mapping in an
        interacting clique and each relation it has prior mass on, a renamed copy of the
        clique pins that choice, and the copies' scores normalise into a max-marginal. All
        copies go into a single invocation, because Boomer partitions by connectivity and
        renamed copies are disconnected by construction.

        A failure degrades to no Boomer columns and a recorded reason. An external solver
        being absent or timing out is not a reason to lose an otherwise complete alignment.
        """
        from mondo_ai.align import boomer as bm

        cfg = self.config.boomer
        stats: dict[str, Any] = {"enabled": cfg.enabled}
        if not cfg.enabled:
            return stats

        priors: dict[tuple[str, str], tuple[float, ...]] = {}
        for record in records:
            final = record.final
            if final is None or not final.object_id:
                continue
            priors[(record.subject_id, final.object_id)] = bm.priors_for(
                final.predicate, final.confidence)
        stats["hypotheses"] = len(priors)
        if not priors:
            stats["note"] = "no asserted mappings to condition"
            return stats

        work = out_dir / "boomer"
        rows = [(s, o, p) for (s, o), p in sorted(priors.items())]
        run = bm.run_boomer(
            work / "pass1", rows, subjects, objects,
            subject_iri=self.config.subject.iri_base or _obo_iri(subjects.prefix),
            object_iri=self.config.object.iri_base or _obo_iri(objects.prefix),
            executable=cfg.executable,
            window_count=cfg.window_count,
            runs=cfg.runs,
            exhaustive_search_limit=cfg.exhaustive_search_limit,
            subsequent_solutions=cfg.subsequent_solutions,
            timeout=cfg.timeout_seconds,
            log=self.log,
        )
        stats["background_classes"] = run.n_classes
        if not run.ok:
            stats["ok"] = False
            stats["error"] = run.stderr_tail[:500]
            self.log(f"    boomer unavailable: {run.stderr_tail.splitlines()[:1]}")
            return stats

        interacting = run.interacting_cliques()
        stats.update({
            "ok": True,
            "cliques": len([c for c in run.cliques if not c.is_singleton_group]),
            "interacting_cliques": len(interacting),
            "largest_clique": max((len(c.verdicts) for c in interacting), default=1),
            "overruled_by_consistency": sum(
                1 for v in run.verdicts.values() if v.overruled),
        })
        self.log(f"    {stats['interacting_cliques']:,} interacting cliques, "
                 f"{stats['overruled_by_consistency']:,} mappings overruled")

        posteriors: dict[tuple[str, str], dict[str, float]] = {}
        coverage: dict[tuple[str, str], float] = {}
        if cfg.marginals and interacting:
            vrows, index = bm.build_marginal_inputs(
                interacting, priors, max_clique_size=cfg.max_marginal_clique_size)
            stats["marginal_variants"] = len(index)
            if vrows:
                pass2 = work / "pass2"
                pass2.mkdir(parents=True, exist_ok=True)
                onto = pass2 / "variants.owl"
                bm.write_variant_ontology(
                    onto, vrows, subjects, objects,
                    self.config.subject.iri_base or _obo_iri(subjects.prefix),
                    self.config.object.iri_base or _obo_iri(objects.prefix))
                run2 = bm.run_boomer(
                    pass2, vrows, subjects, objects,
                    subject_iri=self.config.subject.iri_base or _obo_iri(subjects.prefix),
                    object_iri=self.config.object.iri_base or _obo_iri(objects.prefix),
                    ontology_path=onto,
                    executable=cfg.executable,
                    window_count=cfg.window_count,
                    runs=cfg.runs,
                    exhaustive_search_limit=cfg.exhaustive_search_limit,
                    subsequent_solutions=cfg.subsequent_solutions,
                    timeout=cfg.timeout_seconds,
                    log=self.log,
                )
                if run2.ok:
                    posteriors, coverage = bm.marginals_from_variants(
                        run2.cliques, index, priors)
                    stats["marginals_computed"] = len(posteriors)
                    stats["marginals_complete"] = sum(
                        1 for c in coverage.values()
                        if c >= bm.MIN_POSTERIOR_COVERAGE)
                else:
                    stats["marginals_error"] = run2.stderr_tail[:300]

        stats["annotated"] = bm.attach_to_records(
            records, run, priors, posteriors, coverage)
        moved = [
            r for r in records
            if r.boomer and r.boomer.get("posterior") is not None
            and abs(float(r.boomer["prior"]) - float(r.boomer["posterior"])) > 0.05
        ]
        stats["posterior_moved_from_prior"] = len(moved)
        return stats

    def _reconcile_pass(
        self,
        records: list[MappingRecord],
        subjects: ResourceSet,
        objects: ResourceSet,
        generator: CandidateGenerator,
        out_dir: Path,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Reconcile the settled run against its seed mapping sets, pair by pair.

        Runs after discovery has finished, so nothing here can leak into the ladder, the
        agent or the evaluation. Boomer, when enabled for both stages, then conditions the
        consolidated set: unlike the discovery set it holds every seed claim, so it is where
        two seeds' claims on one subtree can contradict each other.
        """
        from mondo_ai.align import reconcile as rc

        cfg = self.config
        seeds = [rc.load_seed(spec, cfg.subject.prefix, cfg.object.prefix)
                 for spec in cfg.reconcile.seeds]
        if cfg.limit_subjects or cfg.subject_id_file:
            # A restricted run reconciles only the subjects discovery looked at; otherwise a
            # smoke report would be dominated by seed pairs nobody ran the ladder for.
            selected = {r.subject_id for r in records}
            seeds = [[row for row in rows if row.subject_id in selected] for rows in seeds]
        for spec, rows in zip(cfg.reconcile.seeds, seeds):
            self.log(f"  seed {spec.name}: {len(rows):,} pairs")
        mappings = rc.reconcile(records, seeds, subjects, objects, generator,
                                permitted_conflations=cfg.conflations_permitted)
        stats: dict[str, Any] = {
            "seeds": {spec.name: len(rows) for spec, rows in zip(cfg.reconcile.seeds, seeds)},
            "pairs": len(mappings),
            "names": [spec.name for spec in cfg.reconcile.seeds],
            "seed_rows": seeds,
        }

        if cfg.reconcile.boomer and cfg.boomer.enabled:
            from mondo_ai.align import boomer as bm

            # Capped at the ladder's own ceiling: OMOP writes confidence 1 on half its rows,
            # and a prior of exactly 1.0 is a hard constraint — two of them that contradict
            # each other leave Boomer "no possible resolution" for the whole run. A seed's
            # confidence is a claim, not a certainty.
            hypotheses: list[tuple[str, str, tuple[float, ...]]] = [
                (m.subject_id, m.object_id,
                 bm.priors_for(m.predicate_id, min(m.confidence, MAX_SEED_PRIOR)))
                for m in mappings
                if m.review_tier.value != "REJECTED" and m.predicate_id in bm.PREDICATE_RELATION
            ]
            run = bm.run_boomer(
                out_dir / "boomer" / "reconcile", hypotheses, subjects, objects,
                subject_iri=cfg.subject.iri_base or _obo_iri(subjects.prefix),
                object_iri=cfg.object.iri_base or _obo_iri(objects.prefix),
                executable=cfg.boomer.executable, window_count=cfg.boomer.window_count,
                runs=cfg.boomer.runs, exhaustive_search_limit=cfg.boomer.exhaustive_search_limit,
                subsequent_solutions=cfg.boomer.subsequent_solutions,
                timeout=cfg.boomer.timeout_seconds, log=self.log,
            )
            boomer: dict[str, Any] = {"hypotheses": len(hypotheses), "ok": run.ok}
            if run.ok:
                boomer["escalated"] = rc.apply_boomer(mappings, run.verdicts)
                boomer["overruled"] = sum(1 for v in run.verdicts.values() if v.overruled)
            else:
                boomer["error"] = run.stderr_tail[:300]
            stats["boomer"] = boomer
            self.log(f"    boomer over consolidated set: {boomer}")
        return mappings, stats

    @staticmethod
    def _defer(records: Any) -> None:
        """Mark unsettled records no adjudicator reached, as ``QUEUED`` rather than a verdict.

        An unexamined item that emits a bare ``sssom:NoTermFound`` is the one output this
        pipeline must never produce: downstream it is indistinguishable from "we looked and
        there is nothing", which is a much stronger claim. Nor may it read as
        ``EXPERT_REVIEW``: that tier is the curator's worklist, and padding it with items
        nobody has examined destroys the only signal it carries.
        """
        for record in records:
            det = record.deterministic
            if det is None:
                continue
            det.unexamined = True
            if not det.expert_question:
                det.expert_question = (
                    "Deterministic matching could not settle this and the adjudication budget "
                    "did not reach it. Nobody has judged this pair yet."
                )

    def _emit(
        self,
        out_dir: Path,
        records: list[MappingRecord],
        subjects: ResourceSet,
        objects: ResourceSet,
        evaluation: Evaluation | None,
        agent_stats: dict[str, Any],
        boomer_stats: dict[str, Any] | None = None,
        consolidated: list[Any] | None = None,
        reconcile_stats: dict[str, Any] | None = None,
    ) -> dict[str, Path]:
        """Write every output artifact."""
        from mondo_ai import __version__ as version

        counts = tier_counts(records)
        metadata = set_metadata(self.config, subjects, objects, version, counts)

        sssom_path = out_dir / "mappings.sssom.tsv"
        table_path = out_dir / "system_table.tsv"
        records_path = out_dir / "records.jsonl"
        duckdb_path = out_dir / "alignment.duckdb"
        report_path = out_dir / "report.md"
        run_path = out_dir / "run.yaml"
        disagreements_path = out_dir / "disagreements.tsv"

        write_sssom(sssom_path, records, metadata, version)
        write_system_table(table_path, records)
        write_records(records_path, records)
        write_duckdb(duckdb_path, table_path, sssom_path)

        if evaluation is not None:
            self._write_disagreements(disagreements_path, evaluation)

        reconciled_paths: dict[str, Path] = {}
        sections: list[str] = []
        if self.config.reconcile.enabled and reconcile_stats:
            from mondo_ai.align import reconcile as rc

            reconciled_paths = rc.write_outputs(
                out_dir, consolidated or [], records, metadata, self.config.run_id, version)
            sections.append(rc.report_section(
                consolidated or [], reconcile_stats["seed_rows"], reconcile_stats["names"],
                reconcile_stats.get("boomer")))

        write_report(
            report_path,
            config=self.config,
            subjects=subjects,
            objects=objects,
            records=records,
            evaluation=evaluation,
            timings=self.timings,
            agent_stats=agent_stats,
            boomer_stats=boomer_stats or {},
            extra_sections=sections,
        )
        self._write_run_record(run_path, subjects, objects, counts, agent_stats,
                               boomer_stats or {}, reconcile_stats or {})

        for role, path in (("sssom", sssom_path), ("system_table", table_path),
                           ("records", records_path), ("report", report_path),
                           *reconciled_paths.items()):
            self.log(f"  {role}: {path}")
        return {
            "sssom": sssom_path, "system_table": table_path, "records": records_path,
            "duckdb": duckdb_path, "report": report_path, "run": run_path,
            "disagreements": disagreements_path, **reconciled_paths,
        }

    @staticmethod
    def _write_disagreements(path: Path, evaluation: Evaluation) -> None:
        """Write the rows where the run contradicts the subject resource's own xrefs."""
        import csv

        columns = ["kind", "subject_id", "subject_label", "predicted", "asserted",
                   "predicted_predicate", "decision_path", "review_tier", "justification"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, delimiter="\t",
                                    lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in evaluation.disagreements:
                writer.writerow(row)

    def _write_run_record(
        self,
        path: Path,
        subjects: ResourceSet,
        objects: ResourceSet,
        counts: dict[str, int],
        agent_stats: dict[str, Any],
        boomer_stats: dict[str, Any],
        reconcile_stats: dict[str, Any] | None = None,
    ) -> None:
        """Write the reproducibility record."""
        import yaml

        from mondo_ai import __version__ as version

        doc = {
            "tool": "mondo-ai align",
            "tool_version": version,
            "config": yaml.safe_load(self.config.to_yaml()),
            "sources": {
                "subject": {
                    "name": subjects.name, "sha256": subjects.sha256,
                    "version": subjects.version, "retrieved": subjects.retrieved,
                    "url": subjects.source_url, "path": subjects.source_path,
                    "concepts": len(subjects),
                },
                "object": {
                    "name": objects.name, "sha256": objects.sha256,
                    "version": objects.version, "retrieved": objects.retrieved,
                    "url": objects.source_url, "path": objects.source_path,
                    "concepts": len(objects),
                },
            },
            "review_tiers": counts,
            "agent": agent_stats,
            "boomer": boomer_stats,
            "reconcile": {
                "seeds": [
                    {"name": spec.name, "path": str(spec.path),
                     "sha256": sha256_file(spec.path),
                     "pairs": (reconcile_stats or {}).get("seeds", {}).get(spec.name)}
                    for spec in self.config.reconcile.seeds
                ],
                "pairs": (reconcile_stats or {}).get("pairs"),
                "boomer": (reconcile_stats or {}).get("boomer"),
            } if self.config.reconcile.enabled else {"enabled": False},
            "timings_seconds": {k: round(v, 2) for k, v in self.timings.items()},
        }
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
