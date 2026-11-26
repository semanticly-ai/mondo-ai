"""Typer sub-app for the alignment pipeline: ``mondo-ai align ...``.

The deterministic stages are also reachable individually, because "why did this row come out
that way" is the question this pipeline gets asked most and answering it should not require a
full rerun.

Example:
    >>> app.info.name
    'align'
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typing_extensions import Annotated

app = typer.Typer(
    name="align",
    help="Align two resources into an SSSOM mapping set with rule-cited justifications.",
    no_args_is_help=True,
)


@app.command()
def run(
    config: Annotated[Path, typer.Option("--config", "-c", help="Run config YAML.")],
    limit: Annotated[int, typer.Option(help="Cap subjects processed (0 = all).")] = -1,
    no_agent: Annotated[bool, typer.Option("--no-agent", help="Deterministic only.")] = False,
    max_agent_items: Annotated[
        int, typer.Option(help="Cap items sent to the model (0 = all).")] = -1,
    model: Annotated[str, typer.Option(help="Override the adjudication model id.")] = "",
    run_id: Annotated[str, typer.Option(help="Override the run id / output directory.")] = "",
) -> None:
    """Run an alignment end to end and write every artifact."""
    from mondo_ai.align.config import AlignmentConfig
    from mondo_ai.align.pipeline import AlignmentRunner

    cfg = AlignmentConfig.from_yaml(config)
    if limit >= 0:
        cfg.limit_subjects = limit
    if no_agent:
        cfg.agent.enabled = False
    if max_agent_items >= 0:
        cfg.agent.max_items = max_agent_items
    if model:
        cfg.agent.model = model
    if run_id:
        cfg.run_id = run_id

    typer.echo(f"aligning {cfg.subject.name} -> {cfg.object.name} (run {cfg.run_id})")
    result = AlignmentRunner(cfg).run()

    typer.echo("")
    typer.echo("review tiers:")
    from mondo_ai.align.emit import tier_counts

    for tier, count in tier_counts(result.records).items():
        typer.echo(f"  {tier:<14} {count:>8,}")
    if result.evaluation is not None:
        o = result.evaluation.overall
        typer.echo("")
        typer.echo(f"exactMatch vs held-out asserted mappings: "
                   f"precision {o.precision} recall {o.recall} F1 {o.f1}")
    typer.echo("")
    typer.echo(f"report: {result.paths['report']}")


@app.command()
def review(
    input_path: Annotated[Path, typer.Argument(help="Candidate SSSOM TSV to review.")],
    config: Annotated[Path, typer.Option(
        "--config", "-c", help="Run config naming the subject/object resources.")] = Path(
        "conf/align/mondo-snomed.yaml"),
    out: Annotated[Path | None, typer.Option(
        "--out", "-o", help="Output directory (default alignments/review/<file stem>).")] = None,
    no_agent: Annotated[bool, typer.Option("--no-agent", help="No reviewing model.")] = False,
    max_agent_items: Annotated[
        int, typer.Option(help="Cap rows sent to the reviewing model (0 = all).")] = -1,
    model: Annotated[str, typer.Option(help="Override the reviewing model id.")] = "",
    no_boomer: Annotated[bool, typer.Option("--no-boomer", help="Skip Boomer.")] = False,
) -> None:
    """Review an SSSOM file: same rows out, with lexical, Boomer and reviewer confidence."""
    from mondo_ai.align.config import AlignmentConfig
    from mondo_ai.align.review import ReviewRunner

    cfg = AlignmentConfig.from_yaml(config)
    if no_agent:
        cfg.review.agent = False
    if max_agent_items >= 0:
        cfg.review.max_items = max_agent_items
    if model:
        cfg.agent.model = model
    if no_boomer:
        cfg.boomer.enabled = False
    stem = input_path.name.removesuffix(".tsv").removesuffix(".sssom")
    out_dir = out or cfg.output_root / "review" / stem

    typer.echo(f"reviewing {input_path} against {cfg.subject.name} x {cfg.object.name}")
    result = ReviewRunner(cfg, input_path, out_dir).run()
    from collections import Counter

    typer.echo("")
    for tier, n in Counter(r["review_tier"] for r in result.table.rows).most_common():
        typer.echo(f"  {tier or '(out of scope)':<14} {n:>8,}")
    typer.echo("")
    typer.echo(f"reviewed: {result.paths['reviewed']}")
    typer.echo(f"report:   {result.paths['report']}")


@app.command("sssom-subset")
def sssom_subset(
    source: Annotated[Path, typer.Argument(help="SSSOM TSV to take rows from.")],
    subject_prefix: Annotated[str, typer.Option("--subject-prefix", "-s")],
    object_prefix: Annotated[str, typer.Option("--object-prefix", "-t")],
    out: Annotated[Path, typer.Option("--out", "-o", help="SSSOM TSV to write.")],
) -> None:
    """Write the rows of an SSSOM file between two prefixes, metadata kept."""
    from mondo_ai.align.review import subset_sssom

    n = subset_sssom(source, out, subject_prefix, object_prefix)
    typer.echo(f"{out}: {n:,} {subject_prefix} -> {object_prefix} rows")


@app.command()
def explain(
    config: Annotated[Path, typer.Option("--config", "-c", help="Run config YAML.")],
    subject_id: Annotated[str, typer.Argument(help="Subject CURIE to explain.")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit the raw payload.")] = False,
) -> None:
    """Show the candidate pool, flags and ladder verdict for one subject.

    The fast path for "why did this row come out that way": it loads both resources, builds
    the index, and runs the deterministic stages for a single term without touching a model.
    """
    from mondo_ai.align.adapters import load_resource
    from mondo_ai.align.candidates import CandidateGenerator
    from mondo_ai.align.config import AlignmentConfig
    from mondo_ai.align.deterministic import adjudicate
    from mondo_ai.align.flags import subject_flags
    from mondo_ai.align.index import build_index

    cfg = AlignmentConfig.from_yaml(config)
    subj_kwargs = cfg.subject.adapter_kwargs()
    if cfg.subject.format == "obo":
        subj_kwargs.setdefault("keep_xref_prefixes", (cfg.object.prefix,))
    subjects = load_resource(cfg.subject.format, **subj_kwargs)
    objects = load_resource(cfg.object.format, **cfg.object.adapter_kwargs())

    subject = subjects.concepts.get(subject_id)
    if subject is None:
        raise typer.BadParameter(f"{subject_id} not found in {subjects.name}")

    generator = CandidateGenerator(
        build_index(objects.active()), objects, object_prefix=cfg.object.prefix,
        max_candidates=cfg.candidates.max_candidates,
        max_per_category=cfg.candidates.max_per_category,
        retrieval_limit=cfg.candidates.retrieval_limit,
        enable_fuzzy=cfg.candidates.enable_fuzzy,
    )
    candidates = generator.generate(subject)
    generator.annotate_coextensive(subject, candidates)
    sflags = subject_flags(subject)
    result = adjudicate(subject, candidates, subject_flags=sflags)

    if as_json:
        from mondo_ai.align.agent import render_item

        typer.echo(json.dumps({
            "item": render_item(subject, candidates, subjects.concepts, objects.concepts),
            "subject_flags": sflags,
            "verdict": {
                "decision_path": result.decision_path,
                "settled": result.settled,
                "decision": result.adjudication.decision,
                "object_id": result.adjudication.object_id,
                "rules": result.adjudication.rule_ids,
                "justification": result.adjudication.justification,
            },
        }, indent=2))
        return

    typer.echo(f"{subject.id}  {subject.label}")
    if subject.definition:
        typer.echo(f"  def: {subject.definition[:200]}")
    if subject.exact_synonyms:
        typer.echo(f"  exact synonyms: {', '.join(subject.exact_synonyms[:8])}")
    if sflags:
        typer.echo(f"  subject flags: {', '.join(sflags)}")
    xrefs = [x for x in subject.xrefs if x.startswith(f"{cfg.object.prefix}:")]
    if xrefs:
        typer.echo("  asserted xrefs: " + ", ".join(
            f"{x} [{subject.extra.get('xref_' + x, 'unqualified')}]" for x in xrefs))
    typer.echo("")
    typer.echo(f"  {len(candidates)} candidates:")
    for c in candidates:
        obj = objects.concepts.get(c.object_id)
        typer.echo(f"    tier {c.tier}  {c.object_id}  {obj.label if obj else ''}")
        typer.echo(f"        signals: "
                   f"{', '.join(f'{s.rule_id}/{s.kind}' for s in c.signals)}")
        if c.flags:
            typer.echo(f"        flags:   {', '.join(c.flags)}")
        if obj is not None and obj.children:
            kids = [f"{k} {objects.concepts[k].label}"
                    for k in obj.children[:4] if k in objects.concepts]
            typer.echo(f"        children: {'; '.join(kids)}")
    typer.echo("")
    adj = result.adjudication
    typer.echo(f"  ladder: {result.decision_path} "
               f"({'settled' if result.settled else 'handed to adjudication'})")
    typer.echo(f"  verdict: {adj.decision} {adj.object_id or '-'} "
               f"(confidence {adj.confidence})")
    typer.echo(f"  rules: {', '.join(adj.rule_ids)}")
    typer.echo(f"  because: {adj.justification}")


@app.command()
def rules(
    family: Annotated[str, typer.Option(help="Restrict to one family: EV/SC/PR/ST/RJ/CM.")] = "",
) -> None:
    """Print the citable curation rule registry."""
    from mondo_ai.align.rules import FAMILIES, prompt_digest

    families = (family.upper(),) if family else FAMILIES
    unknown = [f for f in families if f not in FAMILIES]
    if unknown:
        raise typer.BadParameter(f"unknown families {unknown}; known: {list(FAMILIES)}")
    typer.echo(prompt_digest(families))


@app.command()
def valueset(
    check: Annotated[
        bool, typer.Option("--check", help="Fail if the artefacts are out of date.")] = False,
    root: Annotated[Path, typer.Option(help="Repository root.")] = Path("."),
) -> None:
    """Generate the published curation-rule value set from the registry.

    Three shapes of the same registry: a SKOS concept scheme so `maprule:` CURIEs resolve, a
    LinkML enum to validate against, and the human page. All generated, so the registry the
    code cites and the registry the mapping sets publish cannot disagree.
    """
    from mondo_ai.align.valueset import check_all, write_all

    if check:
        stale = check_all(root)
        if stale:
            typer.echo("out of date with mondo_ai.align.rules:")
            for path in stale:
                typer.echo(f"  {path}")
            raise typer.Exit(1)
        typer.echo("value set is up to date")
        return

    for path, changed in sorted(write_all(root).items()):
        typer.echo(f"  {'updated' if changed else 'unchanged'}  {path}")


@app.command("extract-snomed")
def extract_snomed(
    zip_path: Annotated[Path, typer.Argument(help="OMOP/Athena vocabulary download (.zip).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="TSV to write.")] = Path(
        "data/align/snomed.tsv"),
) -> None:
    """Extract the SNOMED CT concepts a disease alignment needs from an OMOP download.

    Deterministic: the same zip always yields the same bytes. Writes a provenance sidecar
    next to the TSV with the zip's checksum and the SNOMED edition it carries.
    """
    from mondo_ai.align.omop import extract_snomed as run_extract

    prov = run_extract(zip_path, out)
    typer.echo(f"{out}: {prov['concepts']:,} concepts, {prov['vocabulary_version']}")
    typer.echo(f"  by class: {prov['by_class']}")
    typer.echo(f"  by invalid_reason: {prov['by_invalid_reason']}, "
               f"{prov['with_replacement']:,} with a successor")


@app.command("omop-seeds")
def omop_seeds(
    xlsx: Annotated[Path, typer.Argument(help="OMOP Mondo 'Maps to' relationships (.xlsx).")],
    metadata: Annotated[Path, typer.Argument(help="Their CONCEPT_RELATIONSHIP_METADATA (.csv).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="SSSOM TSV to write.")] = Path(
        "data/align/omop_mondo_snomed.sssom.tsv"),
) -> None:
    """Convert OMOP's Mondo -> SNOMED CT mappings and their metadata into a seed SSSOM set."""
    from mondo_ai.align.omop import omop_seed_sssom

    counts = omop_seed_sssom(xlsx, metadata, out)
    typer.echo(f"{out}: {counts}")


@app.command()
def evaluate(
    config: Annotated[Path, typer.Option("--config", "-c", help="Run config YAML.")],
    records: Annotated[
        Path | None,
        typer.Option(help="records.jsonl to score (default: the run's own)."),
    ] = None,
) -> None:
    """Re-score an existing run against the held-out asserted mappings.

    Useful after hand-editing verdicts, and as a way to score a run whose evaluation was
    disabled at the time.
    """
    from mondo_ai.align.adapters import load_resource
    from mondo_ai.align.config import AlignmentConfig
    from mondo_ai.align.evaluate import all_asserted, evaluate as run_eval, gold_standard
    from mondo_ai.align.models import Adjudication, MappingRecord, ReviewTier

    cfg = AlignmentConfig.from_yaml(config)
    path = records or (cfg.output_dir / "records.jsonl")
    if not path.exists():
        raise typer.BadParameter(f"{path} not found; run the alignment first")

    subj_kwargs = cfg.subject.adapter_kwargs()
    if cfg.subject.format == "obo":
        subj_kwargs.setdefault("keep_xref_prefixes", (cfg.object.prefix,))
    subjects = load_resource(cfg.subject.format, **subj_kwargs)

    loaded: list[MappingRecord] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            doc = json.loads(line)
            final = doc.get("final") or {}
            loaded.append(MappingRecord(
                subject_id=doc["subject_id"],
                subject_label=doc.get("subject_label", ""),
                final=Adjudication(
                    source=final.get("source", "deterministic"),
                    decision=final.get("decision", "none"),
                    object_id=final.get("object_id", ""),
                    confidence=final.get("confidence", 0.0),
                    rule_ids=final.get("rule_ids", []),
                    justification=final.get("justification", ""),
                    decision_path=final.get("decision_path", ""),
                ),
                review_tier=ReviewTier(doc.get("review_tier", "NO_MATCH")),
            ))

    objects = load_resource(cfg.object.format, **cfg.object.adapter_kwargs())
    gold = gold_standard(subjects, cfg.object.prefix, cfg.evaluation.gold_qualifier)
    ev = run_eval(loaded, gold, all_asserted(subjects, cfg.object.prefix), objects=objects)
    typer.echo(json.dumps({
        "overall": ev.overall.as_dict(),
        "by_tier": {k: v.as_dict() for k, v in ev.by_tier.items()},
        "by_rung": {k: v.as_dict() for k, v in ev.by_rung.items()},
        "gold_size": ev.gold_size,
        "evaluated": ev.evaluated,
        "disagreements": len(ev.disagreements),
    }, indent=2))
