"""CLI interface for MondoAI.

MondoAI builds SIEVE profile evidence packets (one claim per packet, plain dicts) for
content that could go into the Mondo disease ontology, scores them with sieve's inclusion
score S, and enriches them with literature and AI synthesis lines.

Typical flow (``just rebuild-all`` does the first three steps):

    mondo-ai download --date 2026-11-20          # source OWL, mondo.owl, mondo.sssom.tsv
    mondo-ai extract --date 2026-11-20           # ResourceSnapshot YAML per source
    mondo-ai build-packets --snapshot-dir data/snapshots/2026-11-20 \\
        --sssom data/owl/2026-11-20/mondo.sssom.tsv
    mondo-ai score packet_store_merged
    mondo-ai enrich packet_store_merged --max-papers 3
    mondo-ai synthesize packet_store_merged --limit 10

Without ``--config`` every command uses the built-in default configuration
(``mondo-ai init-config`` writes it out for editing).
"""

import json
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable, Iterator, NoReturn, Optional

import typer
from typing_extensions import Annotated

from mondo_ai.config import PipelineConfig, get_default_config

app = typer.Typer(
    name="mondo-ai",
    help="MondoAI: AI-assisted evidence-driven sync pipeline for Mondo disease ontology curation.",
    no_args_is_help=True,
)

MONDO_IRI_FILTER = "MONDO_"

ConfigOption = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to config file (default: built-in configuration)"),
]
DataDirOption = Annotated[Path, typer.Option("--data-dir", help="Root data directory")]
DateOption = Annotated[
    Optional[str],
    typer.Option("--date", help="Release date (YYYY-MM-DD) naming the data subdirectories (default: today)"),
]
StoreArgument = Annotated[Path, typer.Argument(help="JSONL packet store directory")]
StoreOption = Annotated[Path, typer.Option("--store", "-s", help="JSONL packet store directory")]
DbOption = Annotated[Path, typer.Option("--db", help="Path to index database")]
ClaimTypeOption = Annotated[
    Optional[str],
    typer.Option("--claim-type", help="SynonymStatement, SubclassStatement or MappingStatement"),
]
MinScoreOption = Annotated[Optional[float], typer.Option("--min-score", help="Minimum inclusion score S")]
MaxScoreOption = Annotated[Optional[float], typer.Option("--max-score", help="Maximum inclusion score S")]


# =============================================================================
# Helpers
# =============================================================================


def _fail(message: str) -> NoReturn:
    """Print an error and exit with status 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _load_config(config: Optional[Path]) -> PipelineConfig:
    """The configuration in ``config``, or the built-in default."""
    return PipelineConfig.from_yaml(config) if config else get_default_config()


def _infores(source: str, cfg: PipelineConfig) -> str:
    """Resolve a resource id (``omim``) to its infores id; other values pass unchanged.

    >>> _infores("ordo", get_default_config()), _infores("infores:omim", get_default_config())
    ('infores:orphanet', 'infores:omim')
    """
    for r in cfg.resources:
        if r.id == source and r.infores:
            return r.infores
    return source


def _require_store(store_dir: Path) -> None:
    """Exit unless ``store_dir`` holds a JSONL packet store."""
    if not (store_dir / "manifest.json").exists():
        _fail(f"packet store not found: {store_dir}")


def _read_store(store_dir: Path) -> list[dict[str, Any]]:
    """Every packet of a JSONL store (latest version of each)."""
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    return list(JSONLPacketStore(store_dir).iter_packets())


def _matches(
    packet: dict[str, Any],
    claim_type: Optional[str] = None,
    review_state: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
) -> bool:
    """Whether a packet's statement passes the claim type, review state and S filters.

    A packet without a score fails any score filter.

    >>> p = {"statement": {"type": "SynonymStatement", "review_state": "PENDING_REVIEW", "score": 0.5}}
    >>> _matches(p, claim_type="SynonymStatement", min_score=0.4), _matches(p, max_score=0.4)
    (True, False)
    """
    st = packet["statement"]
    score = st.get("score")
    return (
        (claim_type is None or st["type"] == claim_type)
        and (review_state is None or st.get("review_state") == review_state)
        and (min_score is None or (score is not None and score >= min_score))
        and (max_score is None or (score is not None and score <= max_score))
    )


def _fmt_score(score: Optional[float]) -> str:
    """A score for a table cell.

    >>> _fmt_score(0.12345), _fmt_score(None)
    ('0.12', 'N/A')
    """
    return f"{score:.2f}" if score is not None else "N/A"


def _short(text: Any, width: int = 30) -> str:
    """Truncate a value to ``width`` characters.

    >>> _short("a" * 40, 10)
    'aaaaaaa...'
    """
    s = "" if text is None else str(text)
    return s if len(s) <= width else s[: width - 3] + "..."


def _print_table(rows: Iterable[dict[str, Any]]) -> None:
    """Print packet summaries (``summarize_packet`` dicts) as a table."""
    typer.echo(f"{'ID':<24} {'Type':<18} {'Subject':<15} {'S':>6} {'Object':<32} Sources")
    typer.echo("-" * 120)
    for r in rows:
        sources = r["sources"] if isinstance(r["sources"], str) else ",".join(r["sources"])
        typer.echo(
            f"{r['packet_id']:<24} {r['claim_type']:<18} {r['subject']:<15} {_fmt_score(r['score']):>6} "
            f"{_short(r['object'], 32):<32} {sources}"
        )


def _print_counts(title: str, counts: dict[str, int], top: Optional[int] = None) -> None:
    """Print a titled block of counts, largest first."""
    typer.echo(f"{title}:")
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
        typer.echo(f"  {key}: {n:,}")
    typer.echo("")


def _fetch(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` (written via a temporary file, so a failed download leaves nothing)."""
    import shutil
    import urllib.request

    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as response, open(tmp, "wb") as out:
        shutil.copyfileobj(response, out)
    tmp.replace(dest)


def _resources(cfg: PipelineConfig, sources: Optional[list[str]]) -> list[Any]:
    """The enabled resources, restricted to ``sources`` if given (exits on an unknown id)."""
    enabled = [r for r in cfg.resources if r.enabled]
    if not sources:
        return enabled
    known = {r.id for r in enabled}
    if unknown := [s for s in sources if s not in known and s != "mondo"]:
        _fail(f"source(s) not in config: {', '.join(unknown)} (configured: {', '.join(sorted(known))})")
    return [r for r in enabled if r.id in sources]


def _owl_file(owl_dir: Path, resource_id: str) -> Path:
    """The OWL file of a resource: ``<id>.rdf.owl`` (a converted copy) if present, else ``<id>.owl``."""
    converted = owl_dir / f"{resource_id}.rdf.owl"
    return converted if converted.exists() else owl_dir / f"{resource_id}.owl"


# =============================================================================
# Acquisition and extraction
# =============================================================================


@app.command()
def download(
    config: ConfigOption = None,
    data_dir: DataDirOption = Path("data"),
    date: DateOption = None,
    source: Annotated[
        Optional[list[str]],
        typer.Option("--source", "-s", help="Source to download (repeatable; default: all enabled)"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-download files that exist")] = False,
) -> None:
    """Download source OWL files, mondo.owl and mondo.sssom.tsv into DATA_DIR/owl/DATE.

    URLs come from the config (each resource's ``url``, ``mondo.owl_url`` and
    ``mondo.sssom_url``). OMIM is released as OWL functional syntax: convert it with
    ``just convert-omim`` before extracting.

    Examples:

        mondo-ai download --date 2026-11-20
        mondo-ai download --source doid --source ordo --force
    """
    cfg = _load_config(config)
    owl_dir = data_dir / "owl" / (date or _date.today().isoformat())
    owl_dir.mkdir(parents=True, exist_ok=True)
    targets = [(r.url, owl_dir / f"{r.id}.owl") for r in _resources(cfg, source)]
    targets += [(cfg.mondo.owl_url, owl_dir / "mondo.owl"), (cfg.mondo.sssom_url, owl_dir / "mondo.sssom.tsv")]
    for url, dest in targets:
        if dest.exists() and not force:
            typer.echo(f"  cached: {dest}")
            continue
        typer.echo(f"Downloading {url} -> {dest}")
        _fetch(url, dest)
    typer.echo(f"\nFiles in {owl_dir}")


@app.command()
def extract(
    config: ConfigOption = None,
    data_dir: DataDirOption = Path("data"),
    date: DateOption = None,
    source: Annotated[
        Optional[list[str]],
        typer.Option("--source", "-s", help="Source to extract, or 'mondo' (repeatable; default: all and Mondo)"),
    ] = None,
    include_deprecated: Annotated[
        bool,
        typer.Option("--include-deprecated/--skip-deprecated", help="Keep obsolete classes (the builder uses them)"),
    ] = True,
) -> None:
    """Extract a ResourceSnapshot per source (and Mondo) from DATA_DIR/owl/DATE into DATA_DIR/snapshots/DATE.

    Each resource is filtered by its configured ``iri_filter``; Mondo by ``MONDO_``. A
    converted ``<id>.rdf.owl`` is read in preference to ``<id>.owl``.

    Examples:

        mondo-ai extract --date 2026-11-20
        mondo-ai extract --source doid --source mondo
    """
    from mondo_ai.extraction.owl_extractor import OWLExtractor
    from mondo_ai.extraction.snapshots import save_snapshot

    cfg = _load_config(config)
    day = date or _date.today().isoformat()
    owl_dir = data_dir / "owl" / day
    snapshot_dir = data_dir / "snapshots" / day
    jobs = [(r.id, r.name, r.iri_filter or None) for r in _resources(cfg, source)]
    if not source or "mondo" in source:
        jobs.append(("mondo", "Mondo", MONDO_IRI_FILTER))
    for resource_id, name, iri_filter in jobs:
        owl_path = _owl_file(owl_dir, resource_id)
        if not owl_path.exists():
            _fail(f"OWL file not found: {owl_path} (run `mondo-ai download` first)")
        typer.echo(f"Extracting {name} from {owl_path}...")
        snapshot = OWLExtractor().extract(
            owl_path=owl_path,
            resource_id=resource_id,
            resource_name=name,
            iri_filter=iri_filter,
            skip_deprecated=not include_deprecated,
        )
        out = save_snapshot(snapshot, snapshot_dir / f"{resource_id}.yaml")
        typer.echo(f"  {snapshot.entity_count:,} entities -> {out}")


@app.command(name="extract-owl")
def extract_owl(
    owl_path: Annotated[Path, typer.Argument(help="Path to OWL file to extract")],
    resource_id: Annotated[str, typer.Option("--resource-id", "-r", help="Resource identifier (e.g., doid, omim)")],
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Output path for snapshot YAML")] = None,
    iri_filter: Annotated[
        Optional[str], typer.Option("--iri-filter", help="IRI substring filter (e.g., 'DOID_', 'MONDO_')")
    ] = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-l", help="Maximum entities to extract")] = None,
    include_deprecated: Annotated[
        bool, typer.Option("--include-deprecated", help="Include deprecated entities")
    ] = False,
) -> None:
    """Extract a ResourceSnapshot from one OWL file.

    Examples:

        mondo-ai extract-owl data/owl/doid.owl -r doid -o data/snapshots/doid.yaml --include-deprecated
        mondo-ai extract-owl data/owl/mondo.owl -r mondo --iri-filter "MONDO_"
    """
    from mondo_ai.extraction.owl_extractor import OWLExtractor
    from mondo_ai.extraction.snapshots import save_snapshot

    if not owl_path.exists():
        _fail(f"OWL file not found: {owl_path}")
    typer.echo(f"Extracting from {owl_path}...")
    snapshot = OWLExtractor().extract(
        owl_path=owl_path,
        resource_id=resource_id,
        resource_name=resource_id.upper(),
        limit=limit,
        iri_filter=iri_filter,
        skip_deprecated=not include_deprecated,
    )
    entities = snapshot.entities or []
    typer.echo(f"  Entities: {len(entities):,}")
    typer.echo(f"  Synonyms: {sum(len(e.synonyms or []) for e in entities):,}")
    typer.echo(f"  Entities with parents: {sum(1 for e in entities if e.parents):,}")
    out = save_snapshot(snapshot, output or Path(f"data/snapshots/{resource_id}.yaml"))
    typer.echo(f"\nSnapshot saved to: {out}")


# =============================================================================
# Packet building
# =============================================================================


def _build(
    cfg: PipelineConfig,
    snapshot_dir: Path,
    sssom: Path,
    source: Optional[list[str]],
    modality: Optional[list[str]],
    raw_dir: Path,
    merged_dir: Path,
    validate_sample: int,
    date: Optional[str],
    mondo_owl: Optional[Path],
) -> None:
    """Build both packet stores from a snapshot directory; exits 1 if a sampled packet is invalid."""
    from mondo_ai.packets.builder import MODALITIES
    from mondo_ai.pipeline import PipelineRunner

    modalities = modality or list(MODALITIES)
    if unknown := [m for m in modalities if m not in MODALITIES]:
        _fail(f"unknown modality: {', '.join(unknown)} (choose from {', '.join(MODALITIES)})")
    for path in (sssom, snapshot_dir / "mondo.yaml", *([mondo_owl] if mondo_owl else [])):
        if not path.exists():
            _fail(f"not found: {path}")
    resources = _resources(cfg, source)
    snapshots = {r.id: snapshot_dir / f"{r.id}.yaml" for r in resources}
    if source:
        if missing := [p for p in snapshots.values() if not p.exists()]:
            _fail(f"snapshot not found: {', '.join(map(str, missing))}")
    else:
        snapshots = {rid: p for rid, p in snapshots.items() if p.exists()}
    if not snapshots:
        _fail(f"no source snapshots in {snapshot_dir}")

    typer.echo(f"Sources:    {', '.join(snapshots)}")
    typer.echo(f"Modalities: {', '.join(modalities)}")
    typer.echo(f"SSSOM:      {sssom}")
    if mondo_owl:
        typer.echo(f"Mondo OWL:  {mondo_owl}")
    runner = PipelineRunner(cfg, sssom, snapshot_dir / "mondo.yaml", dict(snapshots), date=date, mondo_owl=mondo_owl)
    summary = runner.run(raw_dir, merged_dir, modalities=modalities, validate_sample=validate_sample)
    typer.echo(f"\nRaw packets:    {summary.raw_packets:,} -> {raw_dir}")
    typer.echo(f"Merged packets: {summary.merged_packets:,} -> {merged_dir}")
    typer.echo(f"Validated sample: {summary.validated} ({len(summary.invalid)} invalid)")
    for packet_id, problems in summary.invalid.items():
        typer.echo(f"  {packet_id}: {problems[:3]}")
    if summary.invalid:
        raise typer.Exit(1)


@app.command(name="build-packets")
def build_packets(
    snapshot_dir: Annotated[
        Path, typer.Option("--snapshot-dir", help="Directory of <id>.yaml snapshots, including mondo.yaml")
    ],
    sssom: Annotated[Path, typer.Option("--sssom", help="Path to mondo.sssom.tsv")],
    source: Annotated[
        Optional[list[str]],
        typer.Option("--source", "-s", help="Source id (repeatable; default: every configured source with a snapshot)"),
    ] = None,
    modality: Annotated[
        Optional[list[str]],
        typer.Option("--modality", "-m", help="synonyms, subclass or mappings (repeatable; default: all)"),
    ] = None,
    raw_dir: Annotated[Path, typer.Option("--raw-dir", help="Raw store: one packet per claim and source")] = Path(
        "packet_store"
    ),
    merged_dir: Annotated[Path, typer.Option("--merged-dir", help="Merged store: one packet per claim")] = Path(
        "packet_store_merged"
    ),
    config: ConfigOption = None,
    validate_sample: Annotated[
        int, typer.Option("--validate-sample", help="Merged packets to validate (at least one per claim type)")
    ] = 500,
    mondo_owl: Annotated[
        Optional[Path],
        typer.Option("--mondo-owl", help="mondo.owl; applies Mondo's axiom provenance to claims already in Mondo"),
    ] = None,
    date: Annotated[Optional[str], typer.Option("--date", help="Packet version date (default: today)")] = None,
) -> None:
    """Build SIEVE profile packets and replace the raw and merged stores.

    Both stores are rewritten from scratch, each with a stats.md. Exits 1 if a validated
    packet has a schema or profile error.

    Examples:

        mondo-ai build-packets --snapshot-dir data/snapshots/2026-11-20 \\
            --sssom data/owl/2026-11-20/mondo.sssom.tsv \\
            --mondo-owl data/owl/2026-11-20/mondo.owl

        # Only DOID synonyms
        mondo-ai build-packets --snapshot-dir data/snapshots/2026-11-20 \\
            --sssom data/owl/2026-11-20/mondo.sssom.tsv --source doid --modality synonyms
    """
    _build(
        _load_config(config), snapshot_dir, sssom, source, modality, raw_dir, merged_dir, validate_sample, date, mondo_owl
    )


@app.command()
def run(
    config: ConfigOption = None,
    data_dir: DataDirOption = Path("data"),
    date: DateOption = None,
    steps: Annotated[
        str, typer.Option(help="Comma-separated steps: download, extract, build (or 'all')")
    ] = "all",
    raw_dir: Annotated[Path, typer.Option("--raw-dir", help="Raw packet store")] = Path("packet_store"),
    merged_dir: Annotated[Path, typer.Option("--merged-dir", help="Merged packet store")] = Path(
        "packet_store_merged"
    ),
    validate_sample: Annotated[int, typer.Option("--validate-sample", help="Merged packets to validate")] = 500,
) -> None:
    """Run download, extract and build-packets for one release date.

    Files go to DATA_DIR/owl/DATE and DATA_DIR/snapshots/DATE; packets are built with
    Mondo's axiom provenance from DATA_DIR/owl/DATE/mondo.owl. OMIM needs converting
    (``just convert-omim``) between download and extract.

    Examples:

        mondo-ai run --date 2026-11-20
        mondo-ai run --date 2026-11-20 --steps extract,build
    """
    wanted = {"download", "extract", "build"} if steps == "all" else {s.strip() for s in steps.split(",")}
    if unknown := wanted - {"download", "extract", "build"}:
        _fail(f"unknown step(s): {', '.join(sorted(unknown))}")
    day = date or _date.today().isoformat()
    if "download" in wanted:
        download(config=config, data_dir=data_dir, date=day, source=None, force=False)
    if "extract" in wanted:
        extract(config=config, data_dir=data_dir, date=day, source=None, include_deprecated=True)
    if "build" in wanted:
        owl_dir = data_dir / "owl" / day
        _build(
            _load_config(config),
            snapshot_dir=data_dir / "snapshots" / day,
            sssom=owl_dir / "mondo.sssom.tsv",
            source=None,
            modality=None,
            raw_dir=raw_dir,
            merged_dir=merged_dir,
            validate_sample=validate_sample,
            date=day,
            mondo_owl=owl_dir / "mondo.owl",
        )


# =============================================================================
# Scoring, enrichment, synthesis
# =============================================================================


@app.command()
def score(
    store_dir: StoreArgument = Path("packet_store_merged"),
    explain_id: Annotated[
        Optional[str],
        typer.Option("--explain", help="Print the score breakdown of one packet (the store is not changed)"),
    ] = None,
) -> None:
    """Re-score every packet with sieve's inclusion score S and write the store back.

    S comes from the claim type's AggregationRule frozen in each packet: per line
    w = d * s * q * c, the strongest line per independence cluster counts,
    L = L0 + k * sum(w), S = tanh(L/2). The score is not a decision.

    Examples:

        mondo-ai score packet_store_merged
        mondo-ai score packet_store_merged --explain mondoai:pkt/0123456789ab
    """
    from mondo_ai.pipeline import write_store
    from mondo_ai.scoring.scorer import explain, score_packet
    from mondo_ai.stats import score_bucket

    if explain_id:
        from mondo_ai.storage.jsonl_store import JSONLPacketStore

        _require_store(store_dir)
        packet = JSONLPacketStore(store_dir).get_packet(explain_id)
        if packet is None:
            _fail(f"packet not found: {explain_id}")
        b = explain(packet)
        typer.echo(f"Packet: {explain_id}")
        typer.echo(f"Rule: {b.rule_id}  L0 = {b.l0}  k = {b.k}")
        typer.echo("")
        typer.echo(f"{'Line':<44} {'state':<10} {'d':>3} {'s':>6} {'q':>6} {'c':>6} {'w':>7}  cluster")
        for c in b.lines:
            typer.echo(
                f"{_short(c.line_id, 44):<44} {c.state:<10} {c.d:>3} {c.s:>6.3f} {c.q:>6.3f} {c.c:>6.3f} "
                f"{c.w:>7.3f}  {c.cluster}"
            )
        typer.echo("")
        typer.echo(f"sum(w) = {b.sum_w:.4f}  L_evidence = {b.l_evidence:.4f}  competition = {b.competition:.4f}")
        typer.echo(f"L = {b.l_final:.4f}  S = {b.s:.4f}  band = {b.band or 'none'}")
        return

    packets = _read_store(store_dir)
    typer.echo(f"Scoring {len(packets):,} packets in {store_dir}...")
    changed = 0
    buckets: dict[str, int] = {}
    for packet in packets:
        before = packet["statement"].get("score")
        score_packet(packet)
        s = packet["statement"]["score"]
        changed += before != s
        buckets[score_bucket(s)] = buckets.get(score_bucket(s), 0) + 1
    write_store(store_dir, packets)
    typer.echo(f"  Scores changed: {changed:,}\n")
    typer.echo(f"{'S':<16} {'Packets':>9}")
    for bucket in ("S >= 0.8", "0.4 <= S < 0.8", "0 < S < 0.4", "S <= 0"):
        typer.echo(f"{bucket:<16} {buckets.get(bucket, 0):>9,}")


@app.command()
def enrich(
    store_dir: StoreArgument = Path("packet_store_merged"),
    max_papers: Annotated[int, typer.Option("--max-papers", help="Maximum papers to search for per packet")] = 3,
    min_score: Annotated[
        Optional[float], typer.Option("--min-score", help="Only enrich packets whose S is below this (default: all)")
    ] = None,
    claim_type: ClaimTypeOption = None,
    review_state: Annotated[
        Optional[str], typer.Option("--review-state", help="Only enrich packets in this review state")
    ] = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-l", help="Enrich at most this many packets")] = None,
    cache_dir: Annotated[Path, typer.Option("--cache", help="Directory for the PubMed reference cache")] = Path(
        "references_cache"
    ),
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Also search for packets that already have literature")
    ] = False,
) -> None:
    """Attach PubMed literature to packets as direct evidence and write the store back.

    Publications go into ``statement.has_evidence``; they are not interpreted, so S does
    not change. Mapping claims are never enriched (a Publication is not permitted
    evidence for them), nor are packets that already have literature unless --force.

    Examples:

        mondo-ai enrich packet_store_merged --max-papers 3
        mondo-ai enrich packet_store_merged --claim-type SynonymStatement --min-score 0.4 --limit 100
    """
    from mondo_ai.enrichment import PacketEnricher, PubMedClient
    from mondo_ai.pipeline import write_store

    packets = _read_store(store_dir)
    enricher = PacketEnricher(
        pubmed_client=PubMedClient(cache_dir=cache_dir),
        max_papers=max_papers,
        min_score_threshold=min_score,
        skip_enriched=not force,
    )
    candidates = [
        p for p in packets if _matches(p, claim_type=claim_type, review_state=review_state) and enricher.should_enrich(p)
    ][:limit]
    typer.echo(f"Enriching {len(candidates):,} of {len(packets):,} packets in {store_dir}...")
    enriched = 0
    for packet in candidates:
        before = len(packet["statement"].get("has_evidence") or [])
        enricher.enrich(packet)
        added = len(packet["statement"].get("has_evidence") or []) - before
        if added:
            enriched += 1
            typer.echo(f"  {packet['id']}: +{added} publication(s)")
    write_store(store_dir, packets)
    typer.echo(f"\nEnriched: {enriched} packets")


@app.command()
def synthesize(
    store_dir: StoreArgument = Path("packet_store_merged"),
    provider: Annotated[str, typer.Option("--provider", help="AI provider: anthropic or openai")] = "anthropic",
    model: Annotated[Optional[str], typer.Option("--model", help="Model name (default: best for provider)")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Synthesize at most this many packets")] = 10,
    claim_type: ClaimTypeOption = None,
    review_state: Annotated[
        Optional[str], typer.Option("--review-state", help="Only packets in this review state")
    ] = None,
    min_score: MinScoreOption = None,
    max_score: MaxScoreOption = None,
    cache_dir: Annotated[
        Path, typer.Option("--cache", help="PubMed reference cache; cached abstracts go into the prompt")
    ] = Path("references_cache"),
) -> None:
    """Add an AI synthesis line to packets, re-score them and write the store back.

    The model reads the claim's evidence lines and attached Publications; its judgement
    becomes one higher-order evidence line. It accepts or rejects nothing. Requires
    ANTHROPIC_API_KEY or OPENAI_API_KEY.

    Examples:

        mondo-ai synthesize packet_store_merged --limit 10
        mondo-ai synthesize packet_store_merged --provider openai --claim-type SynonymStatement \\
            --min-score 0 --max-score 0.8 --limit 5
    """
    from mondo_ai import agents
    from mondo_ai.enrichment import ReferenceCache
    from mondo_ai.pipeline import write_store

    packets = _read_store(store_dir)
    try:
        agent = agents.SynthesisAgent(provider=provider, model=model, reference_cache=ReferenceCache(cache_dir))
    except ValueError as e:  # unknown provider or missing API key
        _fail(str(e))
    typer.echo(f"Using {provider} with model {agent.model}")
    candidates = [p for p in packets if _matches(p, claim_type, review_state, min_score, max_score)][:limit]
    typer.echo(f"Synthesizing {len(candidates)} of {len(packets):,} packets in {store_dir}...")
    before = {p["id"]: len(p["statement"].get("has_evidence_lines") or []) for p in candidates}
    agent.synthesize_packets(candidates)
    done = 0
    for p in candidates:
        st = p["statement"]
        if len(st.get("has_evidence_lines") or []) == before[p["id"]]:
            typer.echo(f"  {p['id']}: no synthesis (nothing to read, or the model call failed)")
            continue
        done += 1
        line = st["has_evidence_lines"][-1]
        typer.echo(
            f"  {p['id']}: {line.get('direction_of_evidence_provided')} "
            f"({_fmt_score(line.get('score_of_evidence_provided'))}), S = {_fmt_score(st.get('score'))}"
        )
    write_store(store_dir, packets)
    typer.echo(f"\nSynthesized: {done} packets")


# =============================================================================
# Index and query
# =============================================================================


@app.command(name="index")
def index_packets(
    store_dir: StoreArgument = Path("packet_store_merged"),
    db_path: DbOption = Path("packet_index.db"),
    clear: Annotated[bool, typer.Option("--clear/--no-clear", help="Clear the existing index first")] = True,
) -> None:
    """Build a SQLite index over a JSONL packet store.

    Examples:

        mondo-ai index packet_store_merged
        mondo-ai index packet_store --db raw_index.db
    """
    from mondo_ai.storage.index import PacketIndex
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    typer.echo(f"Indexing {store_dir} into {db_path}...")
    index = PacketIndex(db_path)
    count = index.build_from_store(JSONLPacketStore(store_dir), clear=clear)
    index.close()
    typer.echo(f"Indexed {count:,} packets")


def _open_index(db_path: Path) -> Any:
    """Open an existing index (exits if there is none)."""
    from mondo_ai.storage.index import PacketIndex

    if not db_path.exists():
        _fail(f"index not found: {db_path} (run `mondo-ai index` first)")
    return PacketIndex(db_path)


@app.command()
def query(
    db_path: DbOption = Path("packet_index.db"),
    claim_type: ClaimTypeOption = None,
    subject: Annotated[Optional[str], typer.Option("--subject", help="Mondo id (exact)")] = None,
    predicate: Annotated[Optional[str], typer.Option("--predicate", "-p", help="Predicate CURIE or label")] = None,
    review_state: Annotated[
        Optional[str], typer.Option("--review-state", help="PENDING_REVIEW or NOT_VERIFIED")
    ] = None,
    source: Annotated[
        Optional[str], typer.Option("--source", help="Resource id (omim) or infores id among the evidence sources")
    ] = None,
    strength: Annotated[Optional[str], typer.Option("--strength", help="strong, moderate or weak")] = None,
    min_score: MinScoreOption = None,
    max_score: MaxScoreOption = None,
    text: Annotated[Optional[str], typer.Option("--text", help="Substring of the statement text")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum results")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Results to skip")] = 0,
    output_format: Annotated[str, typer.Option("--format", "-f", help="table, json or ids")] = "table",
    config: ConfigOption = None,
) -> None:
    """Query the packet index; results are ordered by S, highest first.

    Examples:

        mondo-ai query --claim-type SynonymStatement --review-state PENDING_REVIEW --min-score 0.8
        mondo-ai query --subject MONDO:0009061
        mondo-ai query --source omim --format ids
    """
    import dataclasses

    index = _open_index(db_path)
    results = index.query(
        claim_type=claim_type,
        subject=subject,
        predicate=predicate,
        review_state=review_state,
        source=_infores(source, _load_config(config)) if source else None,
        strength=strength,
        min_score=min_score,
        max_score=max_score,
        text=text,
        limit=limit,
        offset=offset,
    )
    index.close()
    if output_format == "ids":
        for r in results:
            typer.echo(r.packet_id)
    elif output_format == "json":
        typer.echo(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    elif not results:
        typer.echo("No packets found matching criteria.")
    else:
        typer.echo(f"Found {len(results)} packets:\n")
        _print_table(dataclasses.asdict(r) for r in results)


@app.command(name="index-stats")
def index_stats(db_path: DbOption = Path("packet_index.db")) -> None:
    """Show packet counts by claim type, review state, strength, S bucket, source and predicate.

    Examples:

        mondo-ai index-stats --db packet_index.db
    """
    index = _open_index(db_path)
    stats = index.get_stats()
    index.close()
    typer.echo("Packet Index Statistics")
    typer.echo("=" * 50)
    typer.echo(f"Total packets: {stats['total_packets']:,}")
    typer.echo(f"Unique Mondo terms: {stats['unique_subjects']:,}\n")
    _print_counts("By Claim Type", stats["by_claim_type"])
    _print_counts("By Review State", stats["by_review_state"])
    _print_counts("By Strength", stats["by_strength"])
    _print_counts("By Score Bucket", stats["score_buckets"])
    _print_counts("By Source", stats["by_source"])
    _print_counts("By Predicate (top 10)", stats["by_predicate"], top=10)


# =============================================================================
# Inspecting and exporting packets
# =============================================================================


def _print_packet_summary(packet: dict[str, Any]) -> None:
    """Print a readable summary of one packet: the claim, its score and its evidence."""
    st = packet["statement"]
    pred = st.get("predicate") or {}
    typer.echo(f"Packet:    {packet['id']}  (version {packet.get('version', 'N/A')})")
    typer.echo(f"Claim:     {st['type']}  {st['subject']}  {pred.get('code', '')}  {st['object']}")
    if st.get("statement_text"):
        typer.echo(f"Text:      {st['statement_text']}")
    typer.echo(f"Review:    {st.get('review_state', 'N/A')}")
    typer.echo(f"S:         {_fmt_score(st.get('score'))} ({st.get('strength', 'no band')}, {st.get('direction', 'no direction')})")
    lines = st.get("has_evidence_lines") or []
    typer.echo(f"\nEvidence lines: {len(lines)}")
    for i, line in enumerate(lines, 1):
        typer.echo(
            f"  [{i}] {line.get('direction_of_evidence_provided', '?')} "
            f"s={_fmt_score(line.get('score_of_evidence_provided'))} "
            f"cluster={line.get('independence_cluster', '-')}  {line.get('description', '')}"
        )
        for item in line.get("has_evidence_items") or []:
            typer.echo(f"      - {item.get('type', '?')}: {item.get('id', '')} {item.get('resource', '')}".rstrip())
    direct = st.get("has_evidence") or []
    if direct:
        typer.echo(f"\nDirect evidence: {len(direct)}")
        for item in direct:
            typer.echo(f"  - {item.get('type', '?')}: {item.get('id', '')} {_short(item.get('title'), 60)}")


@app.command()
def show(
    packet_id: Annotated[str, typer.Argument(help="Packet id, e.g. mondoai:pkt/0123456789ab")],
    store_dir: StoreOption = Path("packet_store_merged"),
    output_format: Annotated[str, typer.Option("--format", "-f", help="summary, yaml or json")] = "summary",
) -> None:
    """Show one packet from a JSONL store.

    Examples:

        mondo-ai show mondoai:pkt/0123456789ab
        mondo-ai show mondoai:pkt/0123456789ab-doid --store packet_store --format yaml
    """
    import yaml

    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    packet = JSONLPacketStore(store_dir).get_packet(packet_id)
    if packet is None:
        _fail(f"packet not found in {store_dir}: {packet_id}")
    if output_format == "yaml":
        typer.echo(yaml.safe_dump(packet, default_flow_style=False, sort_keys=False, allow_unicode=True))
    elif output_format == "json":
        typer.echo(json.dumps(packet, indent=2, ensure_ascii=False))
    else:
        _print_packet_summary(packet)


@app.command()
def export(
    store_dir: StoreArgument,
    output: Annotated[Path, typer.Argument(help="Output directory")],
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="yaml (one file per packet) or jsonl (packets.jsonl)")
    ] = "yaml",
    claim_type: ClaimTypeOption = None,
    review_state: Annotated[Optional[str], typer.Option("--review-state", help="Only this review state")] = None,
    min_score: MinScoreOption = None,
    max_score: MaxScoreOption = None,
) -> None:
    """Export packets of a store, optionally filtered, as YAML files or one JSONL file.

    Examples:

        mondo-ai export packet_store_merged out/ --claim-type SynonymStatement --min-score 0.8
        mondo-ai export packet_store_merged out/ --format jsonl --review-state PENDING_REVIEW
    """
    from mondo_ai.pipeline import export_packets_yaml

    if output_format not in ("yaml", "jsonl"):
        _fail(f"unknown format: {output_format}")
    packets = [p for p in _read_store(store_dir) if _matches(p, claim_type, review_state, min_score, max_score)]
    if output_format == "yaml":
        export_packets_yaml(packets, output)
    else:
        output.mkdir(parents=True, exist_ok=True)
        with open(output / "packets.jsonl", "w") as f:
            for p in packets:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    typer.echo(f"Exported {len(packets):,} packets to {output}")


# =============================================================================
# Validation, statistics, web
# =============================================================================


@app.command()
def validate(
    path: Annotated[Path, typer.Argument(help="Packet store, packet file (yaml/json/jsonl) or directory of files")],
    sample: Annotated[
        Optional[int], typer.Option("--sample", help="Validate only this many packets of a store, at random")
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Seed of the sample")] = 0,
    strict: Annotated[bool, typer.Option("--strict", help="Also fail on profile warnings")] = False,
) -> None:
    """Validate packets against the LinkML schema and sieve's profile checks.

    Exits 1 if any packet has an error (or, with --strict, a warning).

    Examples:

        mondo-ai validate packet_store_merged --sample 500
        mondo-ai validate exported/mondoai_pkt_0123456789ab.yaml
    """
    from mondo_ai.validation import validate_packet_directory, validate_packet_file, validate_store

    if not path.exists():
        _fail(f"not found: {path}")
    if path.is_file():
        typer.echo(f"Validating file: {path}")
        result = validate_packet_file(path)
    elif (path / "manifest.json").exists():
        typer.echo(f"Validating store: {path}" + (f" (sample of {sample})" if sample else ""))
        result = validate_store(path, sample=sample, seed=seed)
    else:
        typer.echo(f"Validating directory: {path}")
        result = validate_packet_directory(path)
    typer.echo("")
    typer.echo(result.summary())
    for title, issues in (("Errors", result.errors), ("Warnings", result.warnings)):
        if issues:
            typer.echo(f"\n{title} (first 10):")
            for issue in issues[:10]:
                typer.echo(f"  {issue.packet_id}: {issue.code} - {issue.message}")
            if len(issues) > 10:
                typer.echo(f"  ... and {len(issues) - 10} more")
    if result.errors or (strict and result.warnings):
        raise typer.Exit(1)


@app.command(name="stats")
def stats(
    store_dir: StoreArgument = Path("packet_store_merged"),
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Markdown output (default: STORE_DIR/stats.md)")
    ] = None,
) -> None:
    """Write a statistics dashboard (markdown) for a packet store.

    Examples:

        mondo-ai stats packet_store_merged
        mondo-ai stats packet_store -o reports/raw_stats.md
    """
    from mondo_ai.stats import compute_stats
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    typer.echo(f"Computing statistics from {store_dir}...")
    packet_stats = compute_stats(JSONLPacketStore(store_dir).iter_packets())
    output_path = output or (store_dir / "stats.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(packet_stats.to_markdown() + "\n")
    typer.echo(f"\nStatistics dashboard written to: {output_path}")
    typer.echo(f"  Total packets: {packet_stats.total_packets:,}")
    typer.echo(f"  Total evidence lines: {packet_stats.total_evidence_lines:,}")
    typer.echo(f"  Total evidence items: {packet_stats.total_evidence_items:,}")
    typer.echo(f"  Unique Mondo subjects: {packet_stats.unique_subjects:,}")


@app.command(name="export-web-data")
def export_web_data_cmd(
    store: Annotated[Path, typer.Option("--store", help="Packet store directory to export")] = Path(
        "packet_store_merged"
    ),
    output: Annotated[Path, typer.Option("--output", "-o", help="Output JSON path for the docs browser")] = Path(
        "docs/evidence/data.json"
    ),
) -> None:
    """Export the packet store to JSON for the static docs Evidence Browser.

    The mkdocs "Evidence Browser" page loads this file client-side, so the
    browser deploys as static files on GitHub Pages. Run before `mkdocs build`.

    Examples:

        mondo-ai export-web-data
        mondo-ai export-web-data --store packet_store_merged -o docs/evidence/data.json
    """
    from mondo_ai.web.static_export import export_web_data

    _require_store(store)
    n = export_web_data(store, output)
    typer.echo(f"Exported {n} packets to {output}")


@app.command(name="init-config")
def init_config(
    output: Annotated[Path, typer.Argument(help="Output path for config file")] = Path("config.yaml"),
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing file")] = False,
) -> None:
    """Write the built-in configuration (OMIM, Orphanet, DOID, ICD-10-CM) to a YAML file."""
    if output.exists() and not force:
        _fail(f"file already exists: {output} (use --force to overwrite)")
    get_default_config().to_yaml(output)
    typer.echo(f"Created config file: {output}")


# =============================================================================
# JSONL store maintenance
# =============================================================================


@app.command(name="jsonl-stats")
def jsonl_stats(store_dir: StoreOption = Path("packet_store_merged")) -> None:
    """Show the files, sizes and sealing status of a JSONL packet store.

    Examples:

        mondo-ai jsonl-stats --store packet_store
    """
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    store = JSONLPacketStore(store_dir)
    s = store.get_stats()
    typer.echo("JSONL Packet Store Statistics")
    typer.echo("=" * 50)
    typer.echo(f"Store directory: {s['store_dir']}")
    typer.echo(f"Max file size: {s['max_file_size_mb']:.0f} MB\n")
    typer.echo(f"Total packets: {s['total_packets']:,}")
    typer.echo(f"Indexed packets: {s['indexed_packets']:,}")
    typer.echo(f"Total size: {s['total_size_mb']:.2f} MB\n")
    typer.echo(f"Total files: {s['total_files']}  (sealed: {s['sealed_files']}, open: {s['open_file'] or 'none'})")
    typer.echo("\nFiles:")
    for f in store.list_files():
        typer.echo(
            f"  [{f['status']}] {f['filename']}: {f['packet_count']:,} packets, {f['size_bytes'] / 1024 / 1024:.2f} MB"
        )


@app.command(name="jsonl-rebuild-index")
def jsonl_rebuild_index(store_dir: StoreOption = Path("packet_store_merged")) -> None:
    """Rebuild a JSONL store's packet-id index by scanning its files.

    Examples:

        mondo-ai jsonl-rebuild-index --store packet_store
    """
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    count = JSONLPacketStore(store_dir).rebuild_index()
    typer.echo(f"Indexed {count:,} packets")


def _browse(
    packets: Iterable[dict[str, Any]],
    subject: Optional[str],
    predicate: Optional[str],
    source: Optional[str],
    claim_type: Optional[str],
    review_state: Optional[str],
    min_score: Optional[float],
    max_score: Optional[float],
) -> Iterator[dict[str, Any]]:
    """Summaries of the packets that pass the filters (subject and predicate: substring match)."""
    from mondo_ai.storage.index import summarize_packet

    for packet in packets:
        if not _matches(packet, claim_type, review_state, min_score, max_score):
            continue
        row = summarize_packet(packet)
        if subject and subject.lower() not in row["subject"].lower():
            continue
        pred = f"{row['predicate']} {row['predicate_label'] or ''}".lower()
        if predicate and predicate.lower() not in pred:
            continue
        if source and source not in row["sources"]:
            continue
        yield row


@app.command(name="jsonl-browse")
def jsonl_browse(
    store_dir: StoreOption = Path("packet_store_merged"),
    subject: Annotated[Optional[str], typer.Option("--subject", help="Mondo id (substring match)")] = None,
    predicate: Annotated[
        Optional[str], typer.Option("--predicate", "-p", help="Predicate CURIE or label (substring match)")
    ] = None,
    source: Annotated[
        Optional[str], typer.Option("--source", help="Resource id (omim) or infores id among the evidence sources")
    ] = None,
    claim_type: ClaimTypeOption = None,
    review_state: Annotated[Optional[str], typer.Option("--review-state", help="Review state")] = None,
    min_score: MinScoreOption = None,
    max_score: MaxScoreOption = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum packets to show")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Skip the first N matches")] = 0,
    output_format: Annotated[str, typer.Option("--format", "-f", help="table, json or ids")] = "table",
    config: ConfigOption = None,
) -> None:
    """Browse and filter the packets of a JSONL store without building an index.

    Examples:

        mondo-ai jsonl-browse --subject MONDO:0009061
        mondo-ai jsonl-browse --source doid --claim-type SynonymStatement --min-score 0.8
        mondo-ai jsonl-browse --limit 50 --offset 100 --format json
    """
    from itertools import islice

    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    _require_store(store_dir)
    rows = list(
        islice(
            _browse(
                JSONLPacketStore(store_dir).iter_packets(),
                subject,
                predicate,
                _infores(source, _load_config(config)) if source else None,
                claim_type,
                review_state,
                min_score,
                max_score,
            ),
            offset,
            offset + limit,
        )
    )
    if output_format == "ids":
        for r in rows:
            typer.echo(r["packet_id"])
    elif output_format == "json":
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        typer.echo("No packets found matching criteria.")
    else:
        typer.echo(f"Showing matches {offset + 1}-{offset + len(rows)} in {store_dir}:\n")
        _print_table(rows)


# The alignment pipeline is a sub-app rather than a flat set of commands: it has its own
# config, its own stages and its own vocabulary, and flattening it would put twenty more
# verbs into an already busy top level.
from mondo_ai.align.cli import app as align_app  # noqa: E402

app.add_typer(align_app)


def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
