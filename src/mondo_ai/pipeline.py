"""MondoAI pipeline runner.

Reads source snapshots (``ResourceSnapshot`` YAML, see ``extraction.snapshots``), Mondo's
snapshot and mondo.sssom.tsv, builds SIEVE profile packets for the requested modalities,
optionally attaches the provenance on Mondo's own axioms (curators, PMIDs) to the claims
already in Mondo, validates a sample of them, and writes two JSONL stores: raw (one packet per claim and
source) and merged (one packet per claim), each with a ``stats.md``.

Example:
    >>> from mondo_ai.config import get_default_config
    >>> runner = PipelineRunner(  # doctest: +SKIP
    ...     get_default_config(),
    ...     sssom_path="data/owl/2026-11-20/mondo.sssom.tsv",
    ...     mondo_snapshot="data/snapshots/2026-11-20/mondo.yaml",
    ...     snapshots={"doid": "data/snapshots/2026-11-20/doid.yaml"},
    ... )
    >>> summary = runner.run("packet_store", "packet_store_merged")  # doctest: +SKIP
"""

import random
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from mondo_ai.config import PipelineConfig
from mondo_ai.extraction.mondo_extractor import extract_mondo_provenance
from mondo_ai.extraction.snapshots import load_snapshot
from mondo_ai.mapping.sssom_reader import SSSOMReader
from mondo_ai.packets.builder import MODALITIES, PacketBuilder, Source
from mondo_ai.packets.mondo_provenance import apply_mondo_provenance
from mondo_ai.packets.validate import validate_packet
from mondo_ai.stats import compute_stats
from mondo_ai.storage.jsonl_store import JSONLPacketStore


@dataclass
class RunSummary:
    """What a pipeline run produced."""

    raw_packets: int
    merged_packets: int
    validated: int
    invalid: dict[str, list[str]] = field(default_factory=dict)


class PipelineRunner:
    """Run the MondoAI evidence pipeline from snapshots to packet stores."""

    def __init__(
        self,
        config: PipelineConfig,
        sssom_path: str | Path,
        mondo_snapshot: str | Path,
        snapshots: Mapping[str, str | Path],
        date: str | None = None,
        mondo_owl: str | Path | None = None,
    ) -> None:
        """Initialize the runner.

        Args:
            config: Pipeline configuration; its resources say which snapshots are sources.
            sssom_path: Path to mondo.sssom.tsv.
            mondo_snapshot: Path to the Mondo snapshot YAML.
            snapshots: Source id -> snapshot YAML path. Ids must be configured resources.
            date: Run date (ISO); defaults to today.
            mondo_owl: Optional path to mondo.owl. When given, the curators (ORCIDs) and
                PMIDs on Mondo's own axioms are attached to the claims already in Mondo.
        """
        self.config = config
        self.sssom_path = Path(sssom_path)
        self.mondo_snapshot = Path(mondo_snapshot)
        self.snapshots = {sid: Path(p) for sid, p in snapshots.items()}
        self.date = date or _date.today().isoformat()
        self.mondo_owl = Path(mondo_owl) if mondo_owl else None
        resources = {r.id for r in config.resources if r.enabled}
        if unknown := set(self.snapshots) - resources:
            raise ValueError(f"snapshots for unconfigured resources: {sorted(unknown)}")

    def build(self, modalities: Iterable[str] = MODALITIES) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build (raw, merged) packets for the requested modalities."""
        sources = [
            Source(resource, load_snapshot(self.snapshots[resource.id]))
            for resource in self.config.resources
            if resource.id in self.snapshots
        ]
        builder = PacketBuilder(
            sources=sources,
            mondo=load_snapshot(self.mondo_snapshot),
            sssom=SSSOMReader(self.sssom_path),
            config=self.config,
            date=self.date,
        )
        raw, merged = builder.build(modalities)
        if self.mondo_owl is None:
            return raw, merged
        provenance = extract_mondo_provenance(self.mondo_owl)
        return (
            [apply_mondo_provenance(p, provenance) for p in raw],
            [apply_mondo_provenance(p, provenance) for p in merged],
        )

    def run(
        self,
        raw_dir: str | Path,
        merged_dir: str | Path,
        modalities: Iterable[str] = MODALITIES,
        validate_sample: int = 500,
        seed: int = 0,
    ) -> RunSummary:
        """Build packets, validate a sample, and replace both stores with the result.

        Args:
            raw_dir: Directory of the raw store (one packet per claim and source).
            merged_dir: Directory of the merged store (one packet per claim).
            modalities: Which claim modalities to build.
            validate_sample: How many merged packets (at least one per claim type) to
                validate against the LinkML schema and the sieve profile checks.
            seed: Seed for drawing the validation sample.

        Returns:
            A RunSummary; ``invalid`` maps packet ids to their validation problems.
        """
        raw, merged = self.build(modalities)
        sample = _sample(merged, validate_sample, seed)
        invalid = {p["id"]: problems for p in sample if (problems := validate_packet(p))}
        write_store(raw_dir, raw)
        write_store(merged_dir, merged)
        return RunSummary(raw_packets=len(raw), merged_packets=len(merged), validated=len(sample), invalid=invalid)


def _sample(packets: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """A random sample of n packets that includes at least one of each claim type."""
    rng = random.Random(seed)
    first_of_type: dict[str, dict[str, Any]] = {}
    for p in packets:
        first_of_type.setdefault(p["statement"]["type"], p)
    chosen = {p["id"]: p for p in first_of_type.values()}
    for p in rng.sample(packets, min(n, len(packets))):
        chosen.setdefault(p["id"], p)
    return list(chosen.values())


def write_store(store_dir: str | Path, packets: list[dict[str, Any]]) -> JSONLPacketStore:
    """Replace a JSONL store's contents with ``packets`` and write its stats.md."""
    store = JSONLPacketStore(store_dir)
    store.reset()
    store.add_packets(packets)
    (store.store_dir / "stats.md").write_text(compute_stats(packets).to_markdown() + "\n")
    return store


def export_packets_yaml(packets: list[dict[str, Any]], output_dir: str | Path) -> None:
    """Export packets to one YAML file each, named after the packet id.

    Args:
        packets: Packet dicts.
        output_dir: Output directory.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for packet in packets:
        filename = packet["id"].replace(":", "_").replace("/", "_") + ".yaml"
        with open(output_dir / filename, "w") as f:
            yaml.safe_dump(packet, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
