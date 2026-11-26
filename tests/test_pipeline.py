"""Tests for the pipeline runner: snapshots in, raw and merged packet stores out."""

from pathlib import Path

from mondo_ai.config import get_default_config
from mondo_ai.extraction.snapshots import save_snapshot
from mondo_ai.pipeline import PipelineRunner
from mondo_ai.storage.jsonl_store import JSONLPacketStore
from tests.test_packet_builder import DOID, ICD10CM, MONDO, OMIM, ORDO, _sssom


def test_pipeline_writes_raw_and_merged_stores(tmp_path: Path) -> None:
    snaps = {s.resource_id: save_snapshot(s, tmp_path / f"{s.resource_id}.yaml") for s in (OMIM, ORDO, DOID, ICD10CM)}
    mondo = save_snapshot(MONDO, tmp_path / "mondo.yaml")
    sssom = _sssom(tmp_path)

    # stale content in the target store must be replaced, not appended to
    stale = JSONLPacketStore(tmp_path / "merged")
    stale.add_packets([{"id": "old", "statement": {"subject": "MONDO:9"}}])

    runner = PipelineRunner(get_default_config(), sssom.path, mondo, snaps, date="2026-11-20")
    summary = runner.run(tmp_path / "raw", tmp_path / "merged", validate_sample=50)

    raw = list(JSONLPacketStore(tmp_path / "raw").iter_packets())
    merged = list(JSONLPacketStore(tmp_path / "merged").iter_packets())
    assert len(raw) == summary.raw_packets > len(merged) == summary.merged_packets
    assert "old" not in {p["id"] for p in merged}
    assert {p["statement"]["type"] for p in merged} == {"SynonymStatement", "SubclassStatement", "MappingStatement"}
    assert summary.invalid == {}
    assert (tmp_path / "merged" / "stats.md").read_text().startswith("# Evidence Packet Statistics")
    assert (tmp_path / "raw" / "stats.md").exists()


def test_pipeline_applies_mondo_axiom_provenance(tmp_path: Path, monkeypatch) -> None:
    from mondo_ai import pipeline
    from mondo_ai.extraction.mondo_extractor import AxiomProvenance

    # MONDO:1 'mucoviscidosis' (exact) is in Mondo; give its axiom a curator and a PMID
    provenance = {("MONDO:1", "oboInOwl:hasExactSynonym", "mucoviscidosis"): AxiomProvenance(("0000-0001-5208-3432",), ("42",))}
    monkeypatch.setattr(pipeline, "extract_mondo_provenance", lambda path: provenance)
    snaps = {s.resource_id: save_snapshot(s, tmp_path / f"{s.resource_id}.yaml") for s in (OMIM, DOID)}
    mondo = save_snapshot(MONDO, tmp_path / "mondo.yaml")
    runner = PipelineRunner(
        get_default_config(), _sssom(tmp_path).path, mondo, snaps, date="2026-11-20", mondo_owl=tmp_path / "mondo.owl"
    )
    raw, merged = runner.build(["synonyms"])
    (packet,) = [p for p in merged if p["statement"]["object"] == "mucoviscidosis"]
    assert packet["statement"]["contributions"][0]["contributor"] == "orcid:0000-0001-5208-3432"
    assert [e["id"] for e in packet["statement"]["has_evidence"]] == ["PMID:42"]
    assert all("contributions" in p["statement"] for p in raw if p["statement"]["object"] == "mucoviscidosis")


def test_pipeline_modalities_can_be_selected(tmp_path: Path) -> None:
    snaps = {s.resource_id: save_snapshot(s, tmp_path / f"{s.resource_id}.yaml") for s in (OMIM, DOID)}
    mondo = save_snapshot(MONDO, tmp_path / "mondo.yaml")
    runner = PipelineRunner(get_default_config(), _sssom(tmp_path).path, mondo, snaps, date="2026-11-20")
    _, merged = runner.build(["synonyms"])
    assert {p["statement"]["type"] for p in merged} == {"SynonymStatement"}
