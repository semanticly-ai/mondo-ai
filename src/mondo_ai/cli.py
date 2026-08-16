"""CLI interface for MondoAI.

MondoAI is an AI-assisted, evidence-driven sync pipeline for curating
content into the Mondo disease ontology.

Example:
    >>> # Run the full pipeline
    >>> # mondo-ai run --config config.yaml

    >>> # Run individual steps
    >>> # mondo-ai download
    >>> # mondo-ai extract --source omim
    >>> # mondo-ai build-packets
    >>> # mondo-ai score
    >>> # mondo-ai synthesize
"""

from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from mondo_ai.config import PipelineConfig

app = typer.Typer(
    name="mondo-ai",
    help="MondoAI: AI-assisted evidence-driven sync pipeline for Mondo disease ontology curation.",
    no_args_is_help=True,
)


# =============================================================================
# Pipeline Commands
# =============================================================================


@app.command()
def run(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to config file"),
    ] = Path("config.yaml"),
    steps: Annotated[
        str,
        typer.Option(help="Steps to run: all, download, extract, build, score, enrich, synthesize"),
    ] = "all",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be done without executing"),
    ] = False,
) -> None:
    """Run the MondoAI pipeline.

    Executes the complete evidence sync pipeline or specific steps.
    """
    cfg = PipelineConfig.from_yaml(config)
    typer.echo(f"Running pipeline with {len(cfg.resources)} resources...")
    typer.echo(f"Steps: {steps}")

    if dry_run:
        typer.echo("[DRY RUN] Would execute pipeline steps")
        return

    # TODO: Implement pipeline orchestration
    typer.echo("Pipeline execution not yet implemented")


@app.command()
def download(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to config file"),
    ] = Path("config.yaml"),
    source: Annotated[
        Optional[str],
        typer.Option("--source", "-s", help="Single source to download (default: all)"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Re-download even if cached"),
    ] = False,
) -> None:
    """Download source OWL files.

    Downloads and caches ontology files from configured sources.
    """
    cfg = PipelineConfig.from_yaml(config)

    sources_to_download = [r for r in cfg.resources if r.enabled]
    if source:
        sources_to_download = [r for r in sources_to_download if r.id == source]
        if not sources_to_download:
            typer.echo(f"Source '{source}' not found in config", err=True)
            raise typer.Exit(1)

    for resource in sources_to_download:
        typer.echo(f"Downloading {resource.name} from {resource.url}...")
        # TODO: Implement download logic
        typer.echo(f"  [NOT IMPLEMENTED] Would download to {cfg.mondo.cache_dir}/{resource.id}.owl")


@app.command()
def extract(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to config file"),
    ] = Path("config.yaml"),
    source: Annotated[
        Optional[str],
        typer.Option("--source", "-s", help="Single source to extract (default: all)"),
    ] = None,
) -> None:
    """Extract snapshots from OWL files.

    Parses downloaded ontology files and extracts labels, synonyms,
    definitions, and hierarchy into ResourceSnapshot objects.
    """
    cfg = PipelineConfig.from_yaml(config)

    sources_to_extract = [r for r in cfg.resources if r.enabled]
    if source:
        sources_to_extract = [r for r in sources_to_extract if r.id == source]
        if not sources_to_extract:
            typer.echo(f"Source '{source}' not found in config", err=True)
            raise typer.Exit(1)

    for resource in sources_to_extract:
        typer.echo(f"Extracting {resource.name}...")
        typer.echo(f"  Content types: {[c.value for c in resource.enabled_content]}")
        # TODO: Implement extraction logic
        typer.echo("  [NOT IMPLEMENTED] Would create ResourceSnapshot")


@app.command(name="build-packets")
def build_packets(
    snapshot_path: Annotated[
        Path,
        typer.Argument(help="Path to ResourceSnapshot YAML file"),
    ],
    sssom: Annotated[
        Path,
        typer.Option("--sssom", help="Path to SSSOM mapping file"),
    ],
    source_id: Annotated[
        str,
        typer.Option("--source-id", "-s", help="Source identifier (e.g., doid, omim)"),
    ],
    source_name: Annotated[
        str,
        typer.Option("--source-name", "-n", help="Human-readable source name"),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory for packets (overrides config)"),
    ] = None,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", "-c", help="Path to config file for scoring weights"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Maximum packets to generate"),
    ] = None,
    include_synonyms: Annotated[
        bool,
        typer.Option("--synonyms/--no-synonyms", help="Include synonym statements"),
    ] = True,
    include_hierarchy: Annotated[
        bool,
        typer.Option("--hierarchy/--no-hierarchy", help="Include hierarchy statements"),
    ] = False,
) -> None:
    """Build evidence packets from a ResourceSnapshot.

    Creates SieveStatements in Mondo space using SSSOM mappings,
    then builds EvidencePackets with ConcordanceItems from the source.

    Examples:

        # Build packets from DOID snapshot
        mondo-ai build-packets data/snapshots/doid_snapshot.yaml \\
            --sssom data/mondo.sssom.tsv \\
            --source-id doid --source-name "Disease Ontology"

        # Build only synonym packets (no hierarchy)
        mondo-ai build-packets data/snapshots/omim_snapshot.yaml \\
            --sssom data/mondo.sssom.tsv \\
            --source-id omim --source-name "OMIM" \\
            --no-hierarchy
    """
    import yaml
    from mondo_ai.datamodel import ResourceSnapshot
    from mondo_ai.mapping.sssom_reader import SSSOMReader
    from mondo_ai.statements.builder import StatementBuilder
    from mondo_ai.packets.builder import PacketBuilder
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not snapshot_path.exists():
        typer.echo(f"Error: Snapshot file not found: {snapshot_path}", err=True)
        raise typer.Exit(1)

    if not sssom.exists():
        typer.echo(f"Error: SSSOM file not found: {sssom}", err=True)
        raise typer.Exit(1)

    # Load config if provided
    cfg = None
    if config and config.exists():
        typer.echo(f"Loading config: {config}")
        cfg = PipelineConfig.from_yaml(config)

    # Determine output directory (CLI option > config > default)
    output_dir = output
    if output_dir is None:
        if cfg and cfg.jsonl_storage:
            output_dir = Path(cfg.jsonl_storage.store_dir)
        else:
            output_dir = Path("packet_store")

    typer.echo(f"Loading snapshot: {snapshot_path}")
    with open(snapshot_path) as f:
        data = yaml.safe_load(f)
    snapshot = ResourceSnapshot.model_validate(data)

    typer.echo(f"Loading SSSOM: {sssom}")
    sssom_reader = SSSOMReader(sssom)

    typer.echo(f"Building packets for {source_name} ({source_id})...")
    typer.echo(f"  Include synonyms: {include_synonyms}")
    typer.echo(f"  Include hierarchy: {include_hierarchy}")
    typer.echo(f"  JSONL store: {output_dir}")

    statement_builder = StatementBuilder(sssom_reader)
    packet_builder = PacketBuilder()

    # Get resource config for weight lookup
    resource_config = None
    if cfg:
        for r in cfg.resources:
            if r.id.lower() == source_id.lower():
                resource_config = r
                break
        if resource_config:
            typer.echo(f"  Using content weights from config for {source_id}")
        else:
            typer.echo(f"  Warning: No config found for source {source_id}, using defaults")

    # Initialize JSONL store
    store = JSONLPacketStore(output_dir)

    packets_created = 0
    entities_processed = 0
    entities_mapped = 0

    entities = snapshot.entities or []
    if limit:
        entities = entities[:limit]

    for entity in entities:
        entities_processed += 1

        # Build statements with mapping metadata
        stmt_mappings = statement_builder.build_statements_with_mapping(
            entity,
            include_synonyms=include_synonyms,
            include_hierarchy=include_hierarchy,
        )

        if stmt_mappings:
            entities_mapped += 1

        # Determine if this is a Mondo source
        is_mondo_source = source_id.lower() == "mondo"

        # Collect ORCIDs and PMIDs from entity
        all_orcids: list[str] = []
        all_pmids: list[str] = []

        # From synonyms
        for syn in entity.synonyms or []:
            if syn.orcids:
                all_orcids.extend(o for o in syn.orcids if o not in all_orcids)
            if syn.pmids:
                all_pmids.extend(p for p in syn.pmids if p not in all_pmids)

        # From definition
        if entity.definition_orcids:
            all_orcids.extend(o for o in entity.definition_orcids if o not in all_orcids)
        if entity.definition_pmids:
            all_pmids.extend(p for p in entity.definition_pmids if p not in all_pmids)

        for sm in stmt_mappings:
            # Get weight from resource config or use default
            predicate_code = sm.statement.predicate.code if sm.statement.predicate else ""
            if resource_config:
                weight = resource_config.get_weight(predicate_code or "")
            else:
                weight = 0.7  # Default

            # Build packet with mapping_set_id from SSSOM for provenance
            packet = packet_builder.build_packet(
                statement=sm.statement,
                source_id=source_id,
                source_name=source_name,
                source_entity_id=sm.source_entity_id,
                source_entity_label=sm.source_entity_label,
                mapping_predicate=sm.mapping_set_id or sm.mapping_justification or "unknown",
                weight=weight,
                orcids=all_orcids if all_orcids else None,
                pmids=all_pmids if all_pmids else None,
                is_mondo_source=is_mondo_source,
                source_version=snapshot.version,
            )

            # Add packet to JSONL store
            store.add_packet(packet)
            packets_created += 1

        if entities_processed % 1000 == 0:
            typer.echo(f"  Processed {entities_processed} entities, {packets_created} packets...")

    stats = store.get_stats()
    typer.echo("\nSummary:")
    typer.echo(f"  Entities processed: {entities_processed}")
    typer.echo(f"  Entities with Mondo mapping: {entities_mapped}")
    typer.echo(f"  Packets created: {packets_created}")
    typer.echo(f"  JSONL store: {output_dir}")
    typer.echo(f"  Store size: {stats['total_size_mb']:.2f} MB")


@app.command(name="build-packets-multi")
def build_packets_multi(
    snapshot: Annotated[
        list[Path],
        typer.Option("--snapshot", help="Snapshot file (can specify multiple)"),
    ],
    source_id: Annotated[
        list[str],
        typer.Option("--source-id", help="Source ID for each snapshot (same order)"),
    ],
    source_name: Annotated[
        list[str],
        typer.Option("--source-name", help="Source name for each snapshot (same order)"),
    ],
    sssom: Annotated[
        Path,
        typer.Option("--sssom", help="Path to SSSOM mapping file"),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory for packets (overrides config)"),
    ] = None,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", "-c", help="Path to config file for scoring weights"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Maximum entities per source"),
    ] = None,
    include_synonyms: Annotated[
        bool,
        typer.Option("--synonyms/--no-synonyms", help="Include synonym statements"),
    ] = True,
    include_hierarchy: Annotated[
        bool,
        typer.Option("--hierarchy/--no-hierarchy", help="Include hierarchy statements"),
    ] = False,
) -> None:
    """Build and merge evidence packets from multiple sources.

    Creates packets from multiple ResourceSnapshots, merging evidence
    for statements that map to the same Mondo concept.

    Evidence scores are computed as: source_weight * predicate_weight
    Weights can be configured in config.yaml.

    Examples:

        # With config file for custom scoring weights
        mondo-ai build-packets-multi --config config.yaml \\
            --snapshot data/snapshots/doid_snapshot.yaml --source-id doid --source-name "DOID" \\
            --snapshot data/snapshots/ordo_snapshot.yaml --source-id ordo --source-name "Orphanet" \\
            --sssom data/mondo.sssom.tsv
    """
    import yaml
    from mondo_ai.datamodel import ResourceSnapshot
    from mondo_ai.mapping.sssom_reader import SSSOMReader
    from mondo_ai.statements.builder import StatementBuilder
    from mondo_ai.packets.builder import PacketBuilder, PacketMerger

    if len(snapshot) != len(source_id) or len(snapshot) != len(source_name):
        typer.echo("Error: Must provide same number of --snapshot, --source-id, and --source-name", err=True)
        raise typer.Exit(1)

    if not sssom.exists():
        typer.echo(f"Error: SSSOM file not found: {sssom}", err=True)
        raise typer.Exit(1)

    # Load config if provided
    cfg = None
    if config and config.exists():
        typer.echo(f"Loading config: {config}")
        cfg = PipelineConfig.from_yaml(config)

    # Determine output directory (CLI option > config > default)
    output_dir = output
    if output_dir is None:
        if cfg and cfg.jsonl_storage:
            output_dir = Path(cfg.jsonl_storage.store_dir)
        else:
            output_dir = Path("packet_store")

    typer.echo(f"Loading SSSOM: {sssom}")
    sssom_reader = SSSOMReader(sssom)
    statement_builder = StatementBuilder(sssom_reader)
    packet_builder = PacketBuilder()

    # Build lookup for resource configs
    resource_configs: dict[str, "ResourceConfig"] = {}
    if cfg:
        from mondo_ai.config import ResourceConfig
        for r in cfg.resources:
            resource_configs[r.id.lower()] = r
        typer.echo("Using content weights from config")

    merger = PacketMerger()

    total_statements = 0

    for snap_path, src_id, src_name in zip(snapshot, source_id, source_name):
        if not snap_path.exists():
            typer.echo(f"Warning: Snapshot not found: {snap_path}", err=True)
            continue

        # Get resource config for this source
        resource_config = resource_configs.get(src_id.lower())
        if resource_config:
            typer.echo(f"\nProcessing {src_name} ({src_id}) with config weights...")
        else:
            typer.echo(f"\nProcessing {src_name} ({src_id}) with default weights...")

        with open(snap_path) as f:
            data = yaml.safe_load(f)
        snap = ResourceSnapshot.model_validate(data)

        entities = snap.entities or []
        if limit:
            entities = entities[:limit]

        source_statements = 0
        for entity in entities:
            stmt_mappings = statement_builder.build_statements_with_mapping(
                entity,
                include_synonyms=include_synonyms,
                include_hierarchy=include_hierarchy,
            )

            # Determine if this is a Mondo source
            is_mondo_source = src_id.lower() == "mondo"

            # Collect ORCIDs and PMIDs from entity
            all_orcids: list[str] = []
            all_pmids: list[str] = []

            # From synonyms
            for syn in entity.synonyms or []:
                if syn.orcids:
                    all_orcids.extend(o for o in syn.orcids if o not in all_orcids)
                if syn.pmids:
                    all_pmids.extend(p for p in syn.pmids if p not in all_pmids)

            # From definition
            if entity.definition_orcids:
                all_orcids.extend(o for o in entity.definition_orcids if o not in all_orcids)
            if entity.definition_pmids:
                all_pmids.extend(p for p in entity.definition_pmids if p not in all_pmids)

            for sm in stmt_mappings:
                # Get weight from resource config or use default
                predicate_code = sm.statement.predicate.code if sm.statement.predicate else ""
                if resource_config:
                    weight = resource_config.get_weight(predicate_code or "")
                else:
                    weight = 0.7  # Default

                packet = packet_builder.build_packet(
                    statement=sm.statement,
                    source_id=src_id,
                    source_name=src_name,
                    source_entity_id=sm.source_entity_id,
                    source_entity_label=sm.source_entity_label,
                    mapping_predicate=sm.mapping_set_id or sm.mapping_justification or "unknown",
                    weight=weight,
                    orcids=all_orcids if all_orcids else None,
                    pmids=all_pmids if all_pmids else None,
                    is_mondo_source=is_mondo_source,
                    source_version=snap.version,
                )
                merger.add_packet(packet)
                source_statements += 1

        typer.echo(f"  Statements: {source_statements}")
        total_statements += source_statements

    # Save merged packets to JSONL store
    merged_packets = merger.get_packets()
    typer.echo(f"\nSaving {len(merged_packets)} merged packets to JSONL store...")

    from mondo_ai.storage.jsonl_store import JSONLPacketStore
    store = JSONLPacketStore(output_dir)
    store.add_packets(merged_packets)

    stats = store.get_stats()
    typer.echo("\nSummary:")
    typer.echo(f"  Total statements: {total_statements}")
    typer.echo(f"  Merged packets: {len(merged_packets)}")
    typer.echo(f"  JSONL store: {output_dir}")
    typer.echo(f"  Store size: {stats['total_size_mb']:.2f} MB")


@app.command()
def score(
    path: Annotated[
        Path,
        typer.Argument(help="Path to packet file or directory"),
    ] = Path("evidence_packets"),
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory for scored packets"),
    ] = None,
    accept_threshold: Annotated[
        float,
        typer.Option("--accept", help="NER threshold to auto-accept"),
    ] = 0.7,
    reject_threshold: Annotated[
        float,
        typer.Option("--reject", help="NER threshold to auto-reject"),
    ] = -0.3,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Limit number of packets to process"),
    ] = None,
) -> None:
    """Score evidence packets using Net Evidence Ratio (NER).

    Calculates NER for each packet based on evidence line scores and directions:
        NER = (S+ - S-) / (S+ + S- + S?)

    Where S+ = supporting, S- = disputing, S? = neutral evidence scores.

    Examples:

        # Score all packets in directory
        mondo-ai score evidence_packets

        # Score single packet
        mondo-ai score evidence_packets/pkt_abc123.yaml

        # Custom thresholds
        mondo-ai score evidence_packets --accept 0.8 --reject -0.5
    """
    import yaml
    from mondo_ai.datamodel import load_packet
    from mondo_ai.scoring.scorer import Scorer

    scorer = Scorer(accept_threshold=accept_threshold, reject_threshold=reject_threshold)

    output_dir = output or path if path.is_dir() else (output or path.parent)

    # Process files
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("pkt_*.yaml"))
        if limit:
            files = files[:limit]

    typer.echo(f"Scoring {len(files)} packets...")
    typer.echo(f"  Accept threshold: {accept_threshold}")
    typer.echo(f"  Reject threshold: {reject_threshold}")

    accept_count = 0
    reject_count = 0
    review_count = 0

    for filepath in files:
        packet = load_packet(filepath)
        scored_packet = scorer.score_packet(packet)

        ner = scored_packet.evidence_synthesis.score.value if scored_packet.evidence_synthesis and scored_packet.evidence_synthesis.score else 0
        recommendation = scorer.get_recommendation(ner)

        if recommendation == "ACCEPT":
            accept_count += 1
        elif recommendation == "REJECT":
            reject_count += 1
        else:
            review_count += 1

        # Save scored packet
        out_path = Path(output_dir) / filepath.name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            yaml.dump(
                scored_packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
                f,
                default_flow_style=False,
                sort_keys=False,
            )

    typer.echo("\nSummary:")
    typer.echo(f"  ACCEPT (NER >= {accept_threshold}): {accept_count}")
    typer.echo(f"  REJECT (NER <= {reject_threshold}): {reject_count}")
    typer.echo(f"  REVIEW (needs human review): {review_count}")
    typer.echo(f"  Output: {output_dir}")


@app.command()
def enrich(
    path: Annotated[
        Path,
        typer.Argument(help="Path to packet file or directory"),
    ] = Path("evidence_packets"),
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory for enriched packets"),
    ] = None,
    max_papers: Annotated[
        int,
        typer.Option("--max-papers", help="Maximum papers to add per packet"),
    ] = 3,
    min_score: Annotated[
        Optional[float],
        typer.Option("--min-score", help="Only enrich packets below this score (None = all)"),
    ] = None,
    cache_dir: Annotated[
        Path,
        typer.Option("--cache", help="Directory for reference cache"),
    ] = Path("references_cache"),
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Re-enrich packets even if they already have literature evidence"),
    ] = False,
) -> None:
    """Enrich evidence packets with PubMed literature.

    Searches PubMed for relevant papers and adds SieveDocument evidence
    to packets. By default, skips packets that already have literature
    evidence (incremental mode). Use --force to re-enrich all packets.

    Examples:

        # Enrich all packets in directory (skips already enriched)
        mondo-ai enrich evidence_packets

        # Enrich single packet
        mondo-ai enrich evidence_packets/pkt_000001.yaml

        # Only enrich low-scoring packets
        mondo-ai enrich evidence_packets --min-score 0.5

        # Force re-enrich all packets (ignore existing literature)
        mondo-ai enrich evidence_packets --force
    """
    import yaml
    from mondo_ai.datamodel import load_packet
    from mondo_ai.enrichment import PacketEnricher, PubMedClient

    # Initialize enricher
    pubmed = PubMedClient(cache_dir=cache_dir)
    enricher = PacketEnricher(
        pubmed_client=pubmed,
        max_papers=max_papers,
        min_score_threshold=min_score,
        skip_enriched=not force,
    )

    output_dir = output or path if path.is_dir() else (output or path.parent)

    # Process files
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("pkt_*.yaml"))

    enriched_count = 0
    skipped_enriched = 0
    skipped_score = 0
    for filepath in files:
        typer.echo(f"Processing: {filepath.name}")

        packet = load_packet(filepath)

        if not enricher.should_enrich(packet):
            # Determine why it was skipped
            from mondo_ai.enrichment import has_literature_evidence
            if not force and has_literature_evidence(packet):
                typer.echo("  Skipping (already has literature evidence)")
                skipped_enriched += 1
            else:
                score = 0.0
                if packet.evidence_synthesis and packet.evidence_synthesis.score:
                    score = packet.evidence_synthesis.score.value or 0.0
                typer.echo(f"  Skipping (score {score:.2f} >= threshold)")
                skipped_score += 1
            continue

        original_line_count = len(packet.has_evidence_lines) if packet.has_evidence_lines else 0
        enriched_packet = enricher.enrich(packet)
        new_line_count = len(enriched_packet.has_evidence_lines) if enriched_packet.has_evidence_lines else 0
        papers_added = new_line_count - original_line_count

        if papers_added > 0:
            typer.echo(f"  Added {papers_added} literature evidence items")
            enriched_count += 1

            # Save enriched packet
            out_path = Path(output_dir) / filepath.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                yaml.dump(
                    enriched_packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

    typer.echo("\nSummary:")
    typer.echo(f"  Enriched: {enriched_count} packets")
    if skipped_enriched > 0:
        typer.echo(f"  Skipped (already enriched): {skipped_enriched}")
    if skipped_score > 0:
        typer.echo(f"  Skipped (score threshold): {skipped_score}")


@app.command()
def synthesize(
    path: Annotated[
        Path,
        typer.Argument(help="Path to packet file or directory"),
    ] = Path("evidence_packets"),
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory for synthesized packets"),
    ] = None,
    provider: Annotated[
        str,
        typer.Option("--provider", help="AI provider: anthropic or openai"),
    ] = "anthropic",
    model: Annotated[
        Optional[str],
        typer.Option("--model", help="Model name (default: best for provider)"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Limit number of packets to process"),
    ] = None,
) -> None:
    """Run AI synthesis on evidence packets.

    Uses pydantic-ai to analyze evidence and generate recommendations
    with citations and rationale.

    Requires ANTHROPIC_API_KEY or OPENAI_API_KEY environment variable.

    Examples:

        # Synthesize all packets (using Anthropic Claude)
        mondo-ai synthesize evidence_packets

        # Synthesize single packet
        mondo-ai synthesize evidence_packets/pkt_000001.yaml

        # Use OpenAI instead
        mondo-ai synthesize evidence_packets --provider openai

        # Limit to first 5 packets
        mondo-ai synthesize evidence_packets --limit 5
    """
    import yaml
    from mondo_ai.datamodel import load_packet
    from mondo_ai.agents import SynthesisAgent

    # Initialize agent
    try:
        agent = SynthesisAgent(provider=provider, model=model)
        typer.echo(f"Using {provider} with model {agent.model}")
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    output_dir = output or path if path.is_dir() else (output or path.parent)

    # Process files
    if path.is_file():
        files = [path]
    else:
        files = sorted(path.glob("pkt_*.yaml"))
        if limit:
            files = files[:limit]

    typer.echo(f"Processing {len(files)} packets...")

    for filepath in files:
        typer.echo(f"\nProcessing: {filepath.name}")

        packet = load_packet(filepath)

        try:
            synthesis = agent.synthesize(packet)
            # synthesis is already attached to packet.evidence_synthesis

            # Display synthesis results
            score = synthesis.score.value if synthesis.score else 0.0
            direction = synthesis.direction or "unknown"
            summary = synthesis.summary or ""
            typer.echo(f"  Direction: {direction}")
            typer.echo(f"  Score: {score:.2f}")
            typer.echo(f"  Summary: {summary[:100]}...")

            # Save synthesized packet
            out_path = Path(output_dir) / filepath.name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                yaml.dump(
                    packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

        except Exception as e:
            typer.echo(f"  Error: {e}", err=True)

    typer.echo("\nSynthesis complete")


# =============================================================================
# Index & Query Commands
# =============================================================================


@app.command(name="index")
def index_packets(
    path: Annotated[
        Path,
        typer.Argument(help="Path to packet directory"),
    ] = Path("evidence_packets"),
    db_path: Annotated[
        Path,
        typer.Option("--db", help="Path to index database"),
    ] = Path("packet_index.db"),
    clear: Annotated[
        bool,
        typer.Option("--clear/--no-clear", help="Clear existing index"),
    ] = True,
) -> None:
    """Build a searchable index from evidence packets.

    Creates a SQLite index for fast querying of packet metadata.

    Examples:

        # Index all packets
        mondo-ai index evidence_packets

        # Append to existing index
        mondo-ai index evidence_packets --no-clear
    """
    from mondo_ai.storage import PacketIndex

    if not path.exists():
        typer.echo(f"Error: Directory not found: {path}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Building index from: {path}")
    typer.echo(f"  Database: {db_path}")

    index = PacketIndex(db_path)
    count = index.build_from_directory(path, clear=clear)
    index.close()

    typer.echo(f"\nIndexed {count} packets")


@app.command()
def query(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="Path to index database"),
    ] = Path("packet_index.db"),
    subject: Annotated[
        Optional[str],
        typer.Option("--subject", "-s", help="Filter by Mondo ID"),
    ] = None,
    predicate: Annotated[
        Optional[str],
        typer.Option("--predicate", "-p", help="Filter by predicate (e.g., 'synonym')"),
    ] = None,
    status: Annotated[
        Optional[str],
        typer.Option("--status", help="Filter by status: UNREVIEWED, APPROVED, REJECTED"),
    ] = None,
    min_score: Annotated[
        Optional[float],
        typer.Option("--min-score", help="Minimum NER score"),
    ] = None,
    max_score: Annotated[
        Optional[float],
        typer.Option("--max-score", help="Maximum NER score"),
    ] = None,
    source: Annotated[
        Optional[str],
        typer.Option("--source", help="Filter by source (doid, ordo, omim)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum results to return"),
    ] = 20,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: table, json, ids"),
    ] = "table",
) -> None:
    """Query the packet index.

    Search and filter packets by subject, status, score ranges, and source.

    Examples:

        # Find high-scoring packets
        mondo-ai query --min-score 0.9

        # Find packets for a specific Mondo term
        mondo-ai query --subject MONDO:0005015

        # Find packets needing review
        mondo-ai query --min-score 0.3 --max-score 0.7

        # Find OMIM evidence
        mondo-ai query --source omim
    """
    import json
    from mondo_ai.storage import PacketIndex

    if not db_path.exists():
        typer.echo(f"Error: Index not found: {db_path}", err=True)
        typer.echo("Run 'mondo-ai index' first to build the index.", err=True)
        raise typer.Exit(1)

    index = PacketIndex(db_path)
    results = index.query(
        subject_id=subject,
        predicate=predicate,
        status=status,
        min_score=min_score,
        max_score=max_score,
        source_id=source,
        limit=limit,
    )
    index.close()

    if not results:
        typer.echo("No packets found matching criteria.")
        return

    if output_format == "ids":
        for r in results:
            typer.echo(r.packet_id)
    elif output_format == "json":
        import dataclasses
        typer.echo(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    else:
        # Table format
        typer.echo(f"Found {len(results)} packets:\n")
        typer.echo(f"{'ID':<20} {'Subject':<15} {'Score':>6} {'Sources':<12} {'Object':<30}")
        typer.echo("-" * 85)
        for r in results:
            score_str = f"{r.score:.2f}" if r.score is not None else "N/A"
            obj_short = (r.object_value[:27] + "...") if len(r.object_value) > 30 else r.object_value
            typer.echo(f"{r.packet_id:<20} {r.subject_id:<15} {score_str:>6} {r.source_ids:<12} {obj_short:<30}")


@app.command()
def show(
    packet_id: Annotated[
        str,
        typer.Argument(help="Packet ID or file path"),
    ],
    packets_dir: Annotated[
        Path,
        typer.Option("--dir", "-d", help="Directory containing packets"),
    ] = Path("evidence_packets"),
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: yaml, json, summary"),
    ] = "summary",
) -> None:
    """Show details of a single packet.

    Display full evidence packet with all evidence items and synthesis.

    Examples:

        # Show packet summary
        mondo-ai show pkt_abc123

        # Show full YAML
        mondo-ai show pkt_abc123 --format yaml

        # Show from specific file
        mondo-ai show evidence_packets/pkt_abc123.yaml
    """
    import json
    import yaml
    from mondo_ai.datamodel import load_packet

    # Determine file path
    if packet_id.endswith(".yaml"):
        file_path = Path(packet_id)
    else:
        file_path = packets_dir / f"{packet_id}.yaml"

    if not file_path.exists():
        typer.echo(f"Error: Packet not found: {file_path}", err=True)
        raise typer.Exit(1)

    packet = load_packet(file_path)

    if output_format == "yaml":
        typer.echo(yaml.dump(
            packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
            default_flow_style=False,
            sort_keys=False,
        ))
    elif output_format == "json":
        typer.echo(json.dumps(
            packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
            indent=2,
        ))
    else:
        # Summary format
        typer.echo(f"Packet: {packet.id}")
        status_val = packet.status.value if hasattr(packet.status, "value") else (packet.status or "N/A")
        typer.echo(f"Status: {status_val}")
        typer.echo("")
        typer.echo("Statement:")
        typer.echo(f"  Subject: {packet.statement.subject} ({packet.statement.subject_label or 'N/A'})")
        pred = packet.statement.predicate.label if packet.statement.predicate else "N/A"
        typer.echo(f"  Predicate: {pred}")
        typer.echo(f"  Object: {packet.statement.object}")
        typer.echo("")
        typer.echo(f"Evidence Lines: {len(packet.has_evidence_lines or [])}")
        for i, line in enumerate(packet.has_evidence_lines or []):
            direction = line.direction_of_evidence_provided or "N/A"
            strength = line.strength_of_evidence_provided or "N/A"
            score = line.score_of_evidence_provided
            score_str = f"{score:.2f}" if score else "N/A"
            typer.echo(f"  [{i+1}] {direction} ({strength}, score={score_str})")
            for item in line.has_evidence_items or []:
                item_type = item.type or "Unknown"
                desc = getattr(item, "description", "") or ""
                typer.echo(f"      - {item_type}: {desc[:50]}")
        typer.echo("")
        if packet.evidence_synthesis:
            syn = packet.evidence_synthesis
            score = syn.score.value if syn.score else None
            score_str = f"{score:.2f}" if score is not None else "N/A"
            typer.echo("Synthesis:")
            typer.echo(f"  Score: {score_str}")
            typer.echo(f"  Direction: {syn.direction or 'N/A'}")
            typer.echo(f"  Summary: {syn.summary or 'N/A'}")


@app.command()
def export(
    output: Annotated[
        Path,
        typer.Argument(help="Output directory"),
    ],
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to config file"),
    ] = Path("config.yaml"),
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: yaml, jsonl"),
    ] = "yaml",
    bucket: Annotated[
        Optional[str],
        typer.Option("--bucket", "-b", help="Filter by bucket"),
    ] = None,
) -> None:
    """Export packets to files.

    Export evidence packets to YAML or JSONL format.
    """
    typer.echo(f"Exporting to: {output}")
    typer.echo(f"  Format: {output_format}")
    if bucket:
        typer.echo(f"  Bucket filter: {bucket}")
    # TODO: Implement export logic
    typer.echo("[NOT IMPLEMENTED] Would export packets")


# =============================================================================
# Validation Commands
# =============================================================================


@app.command()
def validate(
    path: Annotated[
        Path,
        typer.Argument(help="Path to packet file or directory"),
    ] = Path("evidence_packets"),
    export_records: Annotated[
        Optional[Path],
        typer.Option("--export-records", help="Export validated packets as individual CurationRecord files to this directory"),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Fail on any validation warning"),
    ] = False,
) -> None:
    """Validate evidence packets using the CurationRecord LinkML schema.

    Validates packets against the CurationRecord LinkML schema and
    optionally exports them in curation-record format.
    """
    from mondo_ai.validation import (
        validate_packet_file,
        validate_packet_directory,
    )

    if path.is_file():
        typer.echo(f"Validating file: {path}")
        result = validate_packet_file(path)
    else:
        typer.echo(f"Validating directory: {path}")
        result = validate_packet_directory(path)

    typer.echo("")
    typer.echo(result.summary())

    if result.errors:
        typer.echo("")
        typer.echo("Errors (first 10):")
        for err in result.errors[:10]:
            typer.echo(f"  {err.packet_id}: {err.field} - {err.message}")
        if len(result.errors) > 10:
            typer.echo(f"  ... and {len(result.errors) - 10} more errors")

    if export_records and result.validated_records:
        import yaml

        # Create output directory
        export_records.mkdir(parents=True, exist_ok=True)

        # Records are already dicts in CurationRecord format
        # Remove None values for cleaner export
        def clean_dict(d):
            if isinstance(d, dict):
                return {k: clean_dict(v) for k, v in d.items() if v is not None}
            elif isinstance(d, list):
                return [clean_dict(i) for i in d]
            return d

        # Export each record as an individual file
        for record in result.validated_records:
            cleaned = clean_dict(record)
            record_id = cleaned.get("id", "unknown")
            # Use the record ID as the filename
            filename = f"{record_id}.yaml"
            filepath = export_records / filename
            with open(filepath, "w") as f:
                yaml.dump(cleaned, f, default_flow_style=False, sort_keys=False)

        typer.echo("")
        typer.echo(f"Exported {len(result.validated_records)} records to: {export_records}/")

    if strict and result.errors:
        raise typer.Exit(1)


# =============================================================================
# Utility Commands
# =============================================================================


@app.command(name="index-stats")
def index_stats(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="Path to index database"),
    ] = Path("packet_index.db"),
) -> None:
    """Show packet index statistics.

    Display counts of packets by status, source, predicate, and score ranges.

    Examples:

        mondo-ai index-stats
        mondo-ai index-stats --db custom_index.db
    """
    from mondo_ai.storage import PacketIndex

    if not db_path.exists():
        typer.echo(f"Error: Index not found: {db_path}", err=True)
        typer.echo("Run 'mondo-ai index' first to build the index.", err=True)
        raise typer.Exit(1)

    index = PacketIndex(db_path)
    stats = index.get_stats()
    index.close()

    typer.echo("Packet Index Statistics")
    typer.echo("=" * 50)
    typer.echo(f"Total packets: {stats['total_packets']}")
    typer.echo(f"Unique Mondo terms: {stats['unique_subjects']}")
    typer.echo("")

    typer.echo("By Status:")
    for status, count in stats.get("by_status", {}).items():
        typer.echo(f"  {status}: {count}")
    typer.echo("")

    typer.echo("By Score Bucket:")
    for bucket, count in stats.get("score_buckets", {}).items():
        typer.echo(f"  {bucket}: {count}")
    typer.echo("")

    typer.echo("By Source:")
    for source, count in sorted(stats.get("by_source", {}).items(), key=lambda x: -x[1]):
        typer.echo(f"  {source}: {count}")
    typer.echo("")

    typer.echo("By Predicate (top 5):")
    for pred, count in list(stats.get("by_predicate", {}).items())[:5]:
        typer.echo(f"  {pred}: {count}")


@app.command(name="init-config")
def init_config(
    output: Annotated[
        Path,
        typer.Argument(help="Output path for config file"),
    ] = Path("config.yaml"),
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing file"),
    ] = False,
) -> None:
    """Generate a sample configuration file.

    Creates a config.yaml with default settings for OMIM, ORDO, and DOID.
    """
    if output.exists() and not force:
        typer.echo(f"File already exists: {output}", err=True)
        typer.echo("Use --force to overwrite", err=True)
        raise typer.Exit(1)

    from mondo_ai.config import get_default_config

    cfg = get_default_config()
    cfg.to_yaml(output)
    typer.echo(f"Created config file: {output}")


@app.command(name="extract-owl")
def extract_owl(
    owl_path: Annotated[
        Path,
        typer.Argument(help="Path to OWL file to extract"),
    ],
    resource_id: Annotated[
        str,
        typer.Option("--resource-id", "-r", help="Resource identifier (e.g., doid, omim)"),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output path for snapshot YAML"),
    ] = None,
    iri_filter: Annotated[
        Optional[str],
        typer.Option("--iri-filter", help="IRI substring filter (e.g., 'DOID_', 'MONDO_')"),
    ] = None,
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Maximum entities to extract"),
    ] = None,
    include_deprecated: Annotated[
        bool,
        typer.Option("--include-deprecated", help="Include deprecated entities"),
    ] = False,
) -> None:
    """Extract a ResourceSnapshot from an OWL file.

    Parses an OWL ontology file and extracts entities with their labels,
    synonyms, definitions, and parent relationships.

    Examples:

        # Extract DOID snapshot
        mondo-ai extract-owl data/owl/doid.owl -r doid -o data/snapshots/doid.yaml

        # Extract Mondo snapshot with filter
        mondo-ai extract-owl data/owl/mondo.owl -r mondo --iri-filter "MONDO_"

        # Extract sample of 100 entities
        mondo-ai extract-owl data/owl/ordo.owl -r ordo --limit 100
    """
    import yaml
    from mondo_ai.extraction.owl_extractor import OWLExtractor

    if not owl_path.exists():
        typer.echo(f"Error: OWL file not found: {owl_path}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Extracting from {owl_path}...")
    typer.echo(f"  Resource ID: {resource_id}")
    if iri_filter:
        typer.echo(f"  IRI filter: {iri_filter}")
    if limit:
        typer.echo(f"  Limit: {limit}")

    extractor = OWLExtractor()
    snapshot = extractor.extract(
        owl_path=owl_path,
        resource_id=resource_id,
        resource_name=resource_id.upper(),
        limit=limit,
        iri_filter=iri_filter,
        skip_deprecated=not include_deprecated,
    )

    entity_count = len(snapshot.entities) if snapshot.entities else 0
    typer.echo(f"\nExtracted {entity_count} entities")

    # Count synonyms and parents
    total_synonyms = sum(len(e.synonyms or []) for e in (snapshot.entities or []))
    entities_with_parents = sum(1 for e in (snapshot.entities or []) if e.parents)
    typer.echo(f"  Total synonyms: {total_synonyms}")
    typer.echo(f"  Entities with parents: {entities_with_parents}")

    # Determine output path
    if output is None:
        output = Path(f"data/snapshots/{resource_id}_snapshot.yaml")

    # Ensure output directory exists
    output.parent.mkdir(parents=True, exist_ok=True)

    # Save snapshot
    with open(output, "w") as f:
        # Convert to dict for YAML serialization
        snapshot_dict = snapshot.model_dump(mode="json", exclude_none=True)
        yaml.dump(snapshot_dict, f, default_flow_style=False, sort_keys=False)

    typer.echo(f"\nSnapshot saved to: {output}")


# =============================================================================
# JSONL Storage Commands
# =============================================================================


@app.command(name="jsonl-export")
def jsonl_export(
    source: Annotated[
        Path,
        typer.Argument(help="Source directory containing YAML packets"),
    ] = Path("evidence_packets"),
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
    max_file_size: Annotated[
        int,
        typer.Option("--max-size", help="Maximum file size in MB before sealing"),
    ] = 50,
    skip_sealed: Annotated[
        bool,
        typer.Option("--skip-sealed/--overwrite-sealed", help="Skip packets in sealed files"),
    ] = True,
) -> None:
    """Export YAML packets to immutable JSONL store.

    Converts YAML evidence packets to JSONL format with automatic file
    partitioning. Files are sealed (made immutable) when they reach the
    size threshold.

    Examples:

        # Export all packets to default store
        mondo-ai jsonl-export evidence_packets

        # Custom store location and 25MB file size
        mondo-ai jsonl-export evidence_packets --store my_store --max-size 25
    """
    from mondo_ai.datamodel import load_packet
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not source.exists():
        typer.echo(f"Error: Source not found: {source}", err=True)
        raise typer.Exit(1)

    # Initialize store
    max_bytes = max_file_size * 1024 * 1024
    store = JSONLPacketStore(store_dir, max_file_size=max_bytes)

    typer.echo("Exporting packets to JSONL store")
    typer.echo(f"  Source: {source}")
    typer.echo(f"  Store: {store_dir}")
    typer.echo(f"  Max file size: {max_file_size} MB")
    typer.echo("")

    # Get packet files
    if source.is_file():
        files = [source]
    else:
        files = sorted(source.glob("pkt_*.yaml"))

    added_count = 0
    skipped_count = 0

    for filepath in files:
        packet = load_packet(filepath)
        added, filename = store.add_packet(packet, skip_if_sealed=skip_sealed)

        if added:
            added_count += 1
        else:
            skipped_count += 1

    stats = store.get_stats()
    typer.echo("\nExport complete:")
    typer.echo(f"  Packets added: {added_count}")
    typer.echo(f"  Packets skipped (in sealed files): {skipped_count}")
    typer.echo(f"  Total files: {stats['total_files']}")
    typer.echo(f"  Sealed files: {stats['sealed_files']}")
    typer.echo(f"  Total size: {stats['total_size_mb']:.2f} MB")


@app.command(name="jsonl-stats")
def jsonl_stats(
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
) -> None:
    """Show JSONL store statistics.

    Display information about the JSONL packet store including
    file counts, sizes, and sealing status.

    Examples:

        mondo-ai jsonl-stats
        mondo-ai jsonl-stats --store my_store
    """
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not store_dir.exists():
        typer.echo(f"Error: Store not found: {store_dir}", err=True)
        raise typer.Exit(1)

    store = JSONLPacketStore(store_dir)
    stats = store.get_stats()

    typer.echo("JSONL Packet Store Statistics")
    typer.echo("=" * 50)
    typer.echo(f"Store directory: {stats['store_dir']}")
    typer.echo(f"Max file size: {stats['max_file_size_mb']:.0f} MB")
    typer.echo("")
    typer.echo(f"Total packets: {stats['total_packets']}")
    typer.echo(f"Indexed packets: {stats['indexed_packets']}")
    typer.echo(f"Total size: {stats['total_size_mb']:.2f} MB")
    typer.echo("")
    typer.echo(f"Total files: {stats['total_files']}")
    typer.echo(f"Sealed files: {stats['sealed_files']}")
    typer.echo(f"Open file: {stats['open_file'] or 'None'}")
    if stats['open_file']:
        typer.echo(f"Open file size: {stats['open_file_size_mb']:.2f} MB")

    typer.echo("")
    typer.echo("Files:")
    for f in store.list_files():
        status_icon = "🔒" if f["status"] == "sealed" else "📝"
        size_mb = f["size_bytes"] / (1024 * 1024)
        typer.echo(f"  {status_icon} {f['filename']}: {f['packet_count']} packets, {size_mb:.2f} MB")


@app.command(name="jsonl-get")
def jsonl_get(
    packet_id: Annotated[
        str,
        typer.Argument(help="Packet ID to retrieve"),
    ],
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: yaml, json, summary"),
    ] = "summary",
) -> None:
    """Retrieve a packet from the JSONL store.

    Look up a packet by ID and display its contents.

    Examples:

        mondo-ai jsonl-get pkt_abc123
        mondo-ai jsonl-get pkt_abc123 --format yaml
        mondo-ai jsonl-get pkt_abc123 --format json
    """
    import json
    import yaml
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not store_dir.exists():
        typer.echo(f"Error: Store not found: {store_dir}", err=True)
        raise typer.Exit(1)

    store = JSONLPacketStore(store_dir)
    packet = store.get_packet(packet_id)

    if packet is None:
        typer.echo(f"Error: Packet not found: {packet_id}", err=True)
        raise typer.Exit(1)

    # Show location info
    location = store.get_location(packet_id)
    assert location is not None  # packet existence checked above
    is_sealed = store.is_in_sealed_file(packet_id)
    status_str = "sealed 🔒" if is_sealed else "open 📝"
    typer.echo(f"Found in: {location.file} ({status_str})")
    typer.echo("")

    if output_format == "yaml":
        typer.echo(yaml.dump(
            packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
            default_flow_style=False,
            sort_keys=False,
        ))
    elif output_format == "json":
        typer.echo(json.dumps(
            packet.model_dump(mode="json", exclude_none=True, serialize_as_any=True),
            indent=2,
        ))
    else:
        # Summary format
        typer.echo(f"Packet: {packet.id}")
        status_val = packet.status.value if hasattr(packet.status, "value") else (packet.status or "N/A")
        typer.echo(f"Status: {status_val}")
        typer.echo("")
        typer.echo("Statement:")
        typer.echo(f"  Subject: {packet.statement.subject} ({packet.statement.subject_label or 'N/A'})")
        pred = packet.statement.predicate.label if packet.statement.predicate else "N/A"
        typer.echo(f"  Predicate: {pred}")
        typer.echo(f"  Object: {packet.statement.object}")
        typer.echo("")
        typer.echo(f"Evidence Lines: {len(packet.has_evidence_lines or [])}")


@app.command(name="jsonl-rebuild-index")
def jsonl_rebuild_index(
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
) -> None:
    """Rebuild the JSONL store index.

    Scan all JSONL files and rebuild the packet index. Useful for
    recovery or after manual file modifications.

    Examples:

        mondo-ai jsonl-rebuild-index
        mondo-ai jsonl-rebuild-index --store my_store
    """
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not store_dir.exists():
        typer.echo(f"Error: Store not found: {store_dir}", err=True)
        raise typer.Exit(1)

    store = JSONLPacketStore(store_dir)

    typer.echo(f"Rebuilding index for: {store_dir}")
    count = store.rebuild_index()
    typer.echo(f"Indexed {count} packets")


@app.command(name="jsonl-browse")
def jsonl_browse(
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
    subject: Annotated[
        Optional[str],
        typer.Option("--subject", help="Filter by Mondo ID (contains match)"),
    ] = None,
    predicate: Annotated[
        Optional[str],
        typer.Option("--predicate", "-p", help="Filter by predicate (contains match)"),
    ] = None,
    source: Annotated[
        Optional[str],
        typer.Option("--source", help="Filter by source ID"),
    ] = None,
    min_score: Annotated[
        Optional[float],
        typer.Option("--min-score", help="Minimum evidence score"),
    ] = None,
    max_score: Annotated[
        Optional[float],
        typer.Option("--max-score", help="Maximum evidence score"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", help="Maximum packets to show"),
    ] = 20,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Skip first N packets"),
    ] = 0,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: table, json, ids"),
    ] = "table",
) -> None:
    """Browse packets in a JSONL store.

    List and filter packets from the JSONL store with various criteria.
    Supports pagination with --limit and --offset.

    Examples:

        # List all packets (first 20)
        mondo-ai jsonl-browse

        # Filter by Mondo subject
        mondo-ai jsonl-browse --subject MONDO:0005015

        # Filter by source
        mondo-ai jsonl-browse --source doid

        # Filter by score range
        mondo-ai jsonl-browse --min-score 0.5 --max-score 0.8

        # Pagination
        mondo-ai jsonl-browse --limit 50 --offset 100

        # Output as JSON
        mondo-ai jsonl-browse --format json
    """
    import json
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not store_dir.exists():
        typer.echo(f"Error: Store not found: {store_dir}", err=True)
        raise typer.Exit(1)

    store = JSONLPacketStore(store_dir)

    typer.echo(f"Browsing packets in: {store_dir}")

    # Collect matching packets
    results = []
    skipped = 0
    count = 0

    for packet in store.iter_packets():
        # Apply filters
        if subject:
            if not packet.statement.subject or subject.lower() not in packet.statement.subject.lower():
                continue

        if predicate:
            pred_str = ""
            if packet.statement.predicate:
                pred_str = packet.statement.predicate.label or packet.statement.predicate.code or ""
            if predicate.lower() not in pred_str.lower():
                continue

        if source:
            # Check if source is in any evidence line
            source_found = False
            for line in packet.has_evidence_lines or []:
                for item in line.has_evidence_items or []:
                    if hasattr(item, "source_id") and item.source_id:
                        if source.lower() in item.source_id.lower():
                            source_found = True
                            break
                if source_found:
                    break
            if not source_found:
                continue

        # Check score filters
        score = None
        if packet.evidence_synthesis and packet.evidence_synthesis.score:
            score = packet.evidence_synthesis.score.value

        if min_score is not None and (score is None or score < min_score):
            continue
        if max_score is not None and (score is None or score > max_score):
            continue

        # Apply pagination
        if skipped < offset:
            skipped += 1
            continue

        if count >= limit:
            break

        # Extract summary info
        pred = ""
        if packet.statement.predicate:
            pred = packet.statement.predicate.label or packet.statement.predicate.code or ""

        # Get sources
        sources = set()
        for line in packet.has_evidence_lines or []:
            for item in line.has_evidence_items or []:
                if hasattr(item, "source_id") and item.source_id:
                    sources.add(item.source_id)

        results.append({
            "id": packet.id,
            "subject": packet.statement.subject or "",
            "subject_label": packet.statement.subject_label or "",
            "predicate": pred,
            "object": packet.statement.object or "",
            "score": score,
            "sources": ",".join(sorted(sources)),
            "evidence_count": len(packet.has_evidence_lines or []),
        })
        count += 1

    if not results:
        typer.echo("No packets found matching criteria.")
        return

    if output_format == "ids":
        for r in results:
            typer.echo(r["id"])
    elif output_format == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        # Table format
        typer.echo(f"\nFound {len(results)} packets (showing {offset+1}-{offset+len(results)}):\n")
        typer.echo(f"{'ID':<20} {'Subject':<15} {'Score':>6} {'Sources':<12} {'Object':<30}")
        typer.echo("-" * 85)
        for r in results:
            score_str = f"{r['score']:.2f}" if r["score"] is not None else "N/A"
            obj_val = str(r["object"]) if r["object"] is not None else ""
            obj_short = (obj_val[:27] + "...") if len(obj_val) > 30 else obj_val
            typer.echo(f"{r['id']:<20} {r['subject']:<15} {score_str:>6} {r['sources']:<12} {obj_short:<30}")


@app.command(name="jsonl-load")
def jsonl_load(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory containing JSONL files"),
    ],
    pattern: Annotated[
        str,
        typer.Option("--pattern", help="Glob pattern for JSONL files"),
    ] = "*.jsonl",
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: summary, table, json"),
    ] = "summary",
    limit: Annotated[
        Optional[int],
        typer.Option("--limit", "-l", help="Maximum packets to process"),
    ] = None,
) -> None:
    """Load and display packets from JSONL files in any directory.

    Reads all JSONL files matching the pattern from the specified directory.
    Unlike jsonl-browse, this doesn't require an initialized store with
    manifest/index - it reads raw JSONL files directly.

    Examples:

        # Load from any directory
        mondo-ai jsonl-load /path/to/jsonl/files

        # Custom pattern
        mondo-ai jsonl-load data/ --pattern "packets_*.jsonl"

        # Output as JSON
        mondo-ai jsonl-load data/ --format json

        # Limit output
        mondo-ai jsonl-load data/ --limit 10
    """
    import json
    from mondo_ai.datamodel import EvidencePacket
    from mondo_ai.datamodel.loaders import _convert_evidence_items

    if not directory.exists():
        typer.echo(f"Error: Directory not found: {directory}", err=True)
        raise typer.Exit(1)

    files = sorted(directory.glob(pattern))
    if not files:
        typer.echo(f"No JSONL files found matching '{pattern}' in {directory}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Loading JSONL files from: {directory}")
    typer.echo(f"  Pattern: {pattern}")
    typer.echo(f"  Files found: {len(files)}")
    typer.echo("")

    packets: list[EvidencePacket] = []
    total_lines = 0
    errors = 0

    for filepath in files:
        typer.echo(f"  Reading: {filepath.name}")
        with open(filepath, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                total_lines += 1

                if limit and len(packets) >= limit:
                    break

                try:
                    data = json.loads(line)
                    data = _convert_evidence_items(data)
                    packet = EvidencePacket.model_validate(data)
                    packets.append(packet)
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        typer.echo(f"    Error on line {line_num}: {e}", err=True)

        if limit and len(packets) >= limit:
            break

    typer.echo("")
    typer.echo(f"Loaded {len(packets)} packets from {total_lines} lines")
    if errors > 0:
        typer.echo(f"Errors: {errors}")

    if output_format == "summary":
        # Show summary statistics
        typer.echo("")
        typer.echo("Summary:")

        # Count by subject
        subjects = set()
        predicates: dict[str, int] = {}
        sources: dict[str, int] = {}
        scores = []

        for packet in packets:
            if packet.statement.subject:
                subjects.add(packet.statement.subject)

            pred = ""
            if packet.statement.predicate:
                pred = packet.statement.predicate.label or packet.statement.predicate.code or "unknown"
            predicates[pred] = predicates.get(pred, 0) + 1

            for ev_line in packet.has_evidence_lines or []:
                for item in ev_line.has_evidence_items or []:
                    if hasattr(item, "source_id") and item.source_id:
                        sources[item.source_id] = sources.get(item.source_id, 0) + 1

            if packet.evidence_synthesis and packet.evidence_synthesis.score:
                if packet.evidence_synthesis.score.value is not None:
                    scores.append(packet.evidence_synthesis.score.value)

        typer.echo(f"  Unique subjects: {len(subjects)}")
        typer.echo(f"  Packets with scores: {len(scores)}")
        if scores:
            typer.echo(f"  Score range: {min(scores):.2f} - {max(scores):.2f}")
            typer.echo(f"  Score mean: {sum(scores)/len(scores):.2f}")

        typer.echo("")
        typer.echo("By predicate:")
        for pred, count in sorted(predicates.items(), key=lambda x: -x[1])[:5]:
            typer.echo(f"  {pred}: {count}")

        typer.echo("")
        typer.echo("By source:")
        for src, count in sorted(sources.items(), key=lambda x: -x[1])[:5]:
            typer.echo(f"  {src}: {count}")

    elif output_format == "table":
        typer.echo("")
        typer.echo(f"{'ID':<20} {'Subject':<15} {'Score':>6} {'Evidence':>8} {'Object':<30}")
        typer.echo("-" * 85)
        for packet in packets[:50]:  # Limit table to 50 rows
            score = None
            if packet.evidence_synthesis and packet.evidence_synthesis.score:
                score = packet.evidence_synthesis.score.value
            score_str = f"{score:.2f}" if score is not None else "N/A"
            obj = packet.statement.object or ""
            obj_short = (obj[:27] + "...") if len(obj) > 30 else obj
            ev_count = len(packet.has_evidence_lines or [])
            typer.echo(f"{packet.id:<20} {packet.statement.subject or '':<15} {score_str:>6} {ev_count:>8} {obj_short:<30}")

        if len(packets) > 50:
            typer.echo(f"... and {len(packets) - 50} more packets")

    elif output_format == "json":
        typer.echo(json.dumps(
            [p.model_dump(mode="json", exclude_none=True, serialize_as_any=True) for p in packets],
            indent=2,
        ))


@app.command(name="stats")
def stats(
    store_dir: Annotated[
        Path,
        typer.Option("--store", "-s", help="JSONL store directory"),
    ] = Path("packet_store"),
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output markdown file path"),
    ] = None,
) -> None:
    """Generate a statistics dashboard from evidence packets.

    Reads all packets from the JSONL store and produces a markdown
    report with counts and distributions by predicate, source, status,
    evidence direction, strength, and score.

    Examples:

        # Generate stats from default store
        mondo-ai stats

        # Custom store and output path
        mondo-ai stats --store my_store -o reports/stats.md
    """
    from mondo_ai.stats import compute_stats
    from mondo_ai.storage.jsonl_store import JSONLPacketStore

    if not store_dir.exists():
        typer.echo(f"Error: Store not found: {store_dir}", err=True)
        raise typer.Exit(1)

    store = JSONLPacketStore(store_dir)
    typer.echo(f"Computing statistics from {store_dir}...")

    packet_stats = compute_stats(store.iter_packets())

    # Default output path
    output_path = output or (store_dir / "stats.md")

    md = packet_stats.to_markdown()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(md)

    typer.echo(f"\nStatistics dashboard written to: {output_path}")
    typer.echo(f"  Total packets: {packet_stats.total_packets:,}")
    typer.echo(f"  Total evidence lines: {packet_stats.total_evidence_lines:,}")
    typer.echo(f"  Total evidence items: {packet_stats.total_evidence_items:,}")
    typer.echo(f"  Unique Mondo subjects: {packet_stats.unique_subjects:,}")


def main() -> None:
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
