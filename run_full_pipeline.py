#!/usr/bin/env python
"""Rebuild packet_store/ and packet_store_merged/ from source snapshots.

Reads the ResourceSnapshots in a snapshot directory (``just extract-all`` writes them)
and mondo.sssom.tsv + mondo.owl from the source directory (``just download-all``),
builds SIEVE profile packets for synonyms, subclass and mappings, attaches Mondo's own
axiom provenance to claims already in Mondo, validates a sample, and replaces both
stores (with their stats.md).

Usage:
    uv run python run_full_pipeline.py SNAPSHOT_DIR SOURCE_DIR
"""

import sys
import time
from pathlib import Path

from mondo_ai.config import get_default_config
from mondo_ai.pipeline import PipelineRunner


def main() -> None:
    """Run the full pipeline."""
    snapshot_dir, source_dir = Path(sys.argv[1]), Path(sys.argv[2])
    sssom_path = source_dir / "mondo.sssom.tsv"
    mondo_owl = source_dir / "mondo.owl"
    config = get_default_config()
    snapshots = {r.id: snapshot_dir / f"{r.id}.yaml" for r in config.resources if (snapshot_dir / f"{r.id}.yaml").exists()}

    print(f"Sources: {', '.join(f'{k} ({v})' for k, v in snapshots.items())}")
    print(f"Mondo:   {snapshot_dir / 'mondo.yaml'}")
    print(f"SSSOM:   {sssom_path}")
    print(f"Mondo axiom provenance: {mondo_owl}")

    start = time.time()
    runner = PipelineRunner(config, sssom_path, snapshot_dir / "mondo.yaml", snapshots, mondo_owl=mondo_owl)
    summary = runner.run(Path("packet_store"), Path("packet_store_merged"))
    print(
        f"\nRaw packets: {summary.raw_packets:,}\nMerged packets: {summary.merged_packets:,}\n"
        f"Validated sample: {summary.validated} ({len(summary.invalid)} invalid)\n"
        f"Took {time.time() - start:.0f}s; stats in packet_store_merged/stats.md"
    )
    for packet_id, problems in summary.invalid.items():
        print(f"  {packet_id}: {problems[:3]}")
    if summary.invalid:
        sys.exit(1)


if __name__ == "__main__":
    main()
