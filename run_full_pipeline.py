#!/usr/bin/env python
"""Run the full MondoAI pipeline: extract -> score -> merge -> export to JSONL.

This script generates evidence packets with concordance evidence from
OMIM, ORDO, and DOID, scores them, merges packets about the same statement,
and stores them in JSONL format.

Usage:
    uv run python run_full_pipeline.py
"""

from pathlib import Path

from mondo_ai.packets.builder import PacketMerger
from mondo_ai.pipeline import PipelineRunner
from mondo_ai.stats import compute_stats
from mondo_ai.storage.jsonl_store import JSONLPacketStore


def main():
    """Run the full pipeline."""
    print("=" * 60)
    print("MONDO-AI FULL PIPELINE RUN")
    print("=" * 60)

    # Paths to source files (local data/ acquisition dir)
    data_path = Path("data")
    sssom_path = data_path / "mondo.sssom.tsv"
    omim_path = data_path / "owl" / "omim.owl"
    ordo_path = data_path / "owl" / "ordo.owl"
    doid_path = data_path / "owl" / "doid.owl"

    raw_store_dir = Path("packet_store")
    merged_store_dir = Path("packet_store_merged")

    # Verify files exist
    print("\n[1] Checking source files...")
    for path, name in [
        (sssom_path, "SSSOM"),
        (omim_path, "OMIM"),
        (ordo_path, "ORDO"),
        (doid_path, "DOID"),
    ]:
        if path.exists():
            print(f"    ✓ {name}: {path}")
        else:
            print(f"    ✗ {name}: NOT FOUND at {path}")
            return

    # Step 1: Run pipeline to generate base packets
    print("\n[2] Extracting evidence packets (OMIM, ORDO, DOID)...")
    runner = PipelineRunner(
        sssom_path=sssom_path,
        omim_owl_path=omim_path,
        ordo_owl_path=ordo_path,
        doid_owl_path=doid_path,
    )

    packets = runner.run(
        omim_synonym_count=100000,
        ordo_synonym_count=100000,
        doid_synonym_count=100000,
        doid_hierarchy_count=100000,
    )
    print(f"    Generated {len(packets)} raw packets")

    # Step 2: Export raw packets to JSONL store
    print("\n[3] Exporting raw packets to JSONL store...")
    raw_store = JSONLPacketStore(raw_store_dir)
    raw_results = raw_store.add_packets(packets)
    raw_added = sum(1 for was_added, _ in raw_results.values() if was_added)
    print(f"    Added {raw_added} packets to {raw_store_dir}")

    # Step 3: Merge packets with the same statement
    print("\n[4] Merging packets by statement...")
    merger = PacketMerger()
    for packet in packets:
        merger.add_packet(packet)
    merged_packets = merger.get_packets()
    print(f"    {len(packets)} raw -> {len(merged_packets)} merged packets")

    multi_evidence = [p for p in merged_packets if len(p.has_evidence_lines or []) >= 2]
    print(f"    Packets with 2+ evidence lines: {len(multi_evidence)}")
    triple_evidence = [p for p in merged_packets if len(p.has_evidence_lines or []) >= 3]
    print(f"    Packets with 3+ evidence lines: {len(triple_evidence)}")

    # Step 4: Export merged packets to JSONL store
    print("\n[5] Exporting merged packets to JSONL store...")
    merged_store = JSONLPacketStore(merged_store_dir)
    merged_results = merged_store.add_packets(merged_packets)
    merged_added = sum(1 for was_added, _ in merged_results.values() if was_added)
    print(f"    Added {merged_added} packets to {merged_store_dir}")

    # Step 5: Generate stats
    print("\n[6] Computing statistics...")
    stats = compute_stats(merged_packets)
    md = stats.to_markdown()
    stats_path = merged_store_dir / "stats.md"
    with open(stats_path, "w") as f:
        f.write(md)
    print(f"    Stats written to {stats_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"\nRaw packets: {len(packets)}")
    print(f"Merged packets: {len(merged_packets)}")
    print(f"Unique Mondo subjects: {stats.unique_subjects}")
    print(f"With 2+ evidence lines: {len(multi_evidence)}")
    print(f"With 3+ evidence lines: {len(triple_evidence)}")

    # Show a sample multi-evidence packet
    if multi_evidence:
        print("\n" + "-" * 60)
        print("SAMPLE MULTI-EVIDENCE PACKET:")
        print("-" * 60)
        sample = sorted(multi_evidence, key=lambda p: len(p.has_evidence_lines or []), reverse=True)[0]
        pred = sample.statement.predicate
        pred_label = pred.label if pred else "?"
        print(f"ID: {sample.id}")
        print(f"Statement: {sample.statement.subject_label} [{pred_label}] '{sample.statement.object}'")
        print(f"Evidence lines: {len(sample.has_evidence_lines or [])}")
        for el in sample.has_evidence_lines or []:
            src = "?"
            for item in el.has_evidence_items or []:
                src = getattr(item, "source_id", getattr(item, "source_name", "?"))
            print(f"  - {src}: direction={el.direction_of_evidence_provided}, "
                  f"strength={el.strength_of_evidence_provided}, "
                  f"score={el.score_of_evidence_provided}")

    print(f"\nMerged store: {merged_store_dir.absolute()}/")
    print(f"Stats: {stats_path.absolute()}")


if __name__ == "__main__":
    main()
