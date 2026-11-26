"""Smoke tests for the mondo-ai CLI, one per command family.

Every test runs on a tiny packet store built from the synthetic snapshots of
tests/test_packet_builder.py. Nothing touches the network or a real LLM: downloads go
through a patched fetcher, PubMed through a fake client, synthesis through pydantic-ai's
TestModel.
"""

import json
from pathlib import Path

import pytest
import yaml
from pydantic_ai.models.test import TestModel
from typer.testing import CliRunner

from mondo_ai import cli
from mondo_ai.agents.synthesis import SynthesisAgent, SynthesisResponse
from mondo_ai.config import get_default_config
from mondo_ai.enrichment.pubmed import PubMedPaper
from mondo_ai.extraction.snapshots import load_snapshot, save_snapshot
from mondo_ai.packets.builder import PacketBuilder, Source
from mondo_ai.packets.validate import validate_packet
from mondo_ai.pipeline import write_store
from mondo_ai.storage.jsonl_store import JSONLPacketStore
from tests.test_packet_builder import DATE, DOID, ICD10CM, MONDO, OMIM, ORDO, _sssom

runner = CliRunner()
MINI_SOURCE = Path(__file__).parent / "data" / "mini_source.owl"


def invoke(*args: str | Path):
    """Run the CLI and fail with its output when it exits non-zero."""
    result = runner.invoke(cli.app, [str(a) for a in args])
    assert result.exit_code == 0, result.output
    return result


def _get(store: Path, packet_id: str) -> dict:
    """A packet from a store, failing the test if it is missing."""
    packet = JSONLPacketStore(store).get_packet(packet_id)
    assert packet is not None, packet_id
    return packet


@pytest.fixture(scope="module")
def merged_packets(tmp_path_factory) -> list[dict]:
    """Merged packets of all three modalities from the builder fixtures."""
    config = get_default_config()
    resources = {r.id: r for r in config.resources}
    builder = PacketBuilder(
        sources=[Source(resources[s.resource_id], s) for s in (OMIM, ORDO, DOID, ICD10CM)],
        mondo=MONDO,
        sssom=_sssom(tmp_path_factory.mktemp("sssom")),
        config=config,
        date=DATE,
    )
    return builder.build()[1]


@pytest.fixture
def store(tmp_path: Path, merged_packets: list[dict]) -> Path:
    """A fresh merged store (a deep copy per test, since commands rewrite it)."""
    packets = json.loads(json.dumps(merged_packets))
    write_store(tmp_path / "store", packets)
    return tmp_path / "store"


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    """A snapshot directory as `extract` writes it, plus mondo.sssom.tsv."""
    d = tmp_path / "snapshots"
    for snap in (OMIM, ORDO, DOID, ICD10CM, MONDO):
        save_snapshot(snap, d / f"{snap.resource_id}.yaml")
    _sssom(d)
    return d


def _packets(store_dir: Path) -> list[dict]:
    return list(JSONLPacketStore(store_dir).iter_packets())


def _first(store_dir: Path, claim_type: str) -> dict:
    return next(p for p in _packets(store_dir) if p["statement"]["type"] == claim_type)


def test_help_lists_the_commands() -> None:
    out = invoke("--help").output
    for name in ("build-packets", "score", "enrich", "synthesize", "stats", "export-web-data", "align"):
        assert name in out
    assert "build-packets-multi" not in out


# ---------------------------------------------------------------- acquisition


def test_download_fetches_config_urls(tmp_path: Path, monkeypatch) -> None:
    fetched: list[tuple[str, Path]] = []

    def fake_fetch(url: str, dest: Path) -> None:
        fetched.append((url, dest))
        dest.write_text("x")

    monkeypatch.setattr(cli, "_fetch", fake_fetch)
    invoke("download", "--data-dir", tmp_path, "--date", DATE, "--source", "doid")
    owl_dir = tmp_path / "owl" / DATE
    assert fetched == [
        ("http://purl.obolibrary.org/obo/doid.owl", owl_dir / "doid.owl"),
        ("http://purl.obolibrary.org/obo/mondo.owl", owl_dir / "mondo.owl"),
        ("http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv", owl_dir / "mondo.sssom.tsv"),
    ]
    # cached files are not fetched again without --force
    fetched.clear()
    invoke("download", "--data-dir", tmp_path, "--date", DATE, "--source", "doid")
    assert fetched == []


def test_download_unknown_source_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["download", "--data-dir", str(tmp_path), "--source", "nope"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_extract_owl_writes_snapshot(tmp_path: Path) -> None:
    out = tmp_path / "doid.yaml"
    invoke("extract-owl", MINI_SOURCE, "-r", "doid", "-o", out, "--include-deprecated")
    snap = load_snapshot(out)
    assert snap.resource_id == "doid"
    assert snap.entities


def test_extract_reads_dated_owl_dir(tmp_path: Path) -> None:
    owl_dir = tmp_path / "owl" / DATE
    owl_dir.mkdir(parents=True)
    (owl_dir / "doid.owl").write_bytes(MINI_SOURCE.read_bytes())
    invoke("extract", "--data-dir", tmp_path, "--date", DATE, "--source", "doid")
    snap = load_snapshot(tmp_path / "snapshots" / DATE / "doid.yaml")
    assert snap.resource_id == "doid"


# ---------------------------------------------------------------- packets


def test_build_packets_writes_both_stores(tmp_path: Path, snapshot_dir: Path) -> None:
    out = invoke(
        "build-packets",
        "--snapshot-dir", snapshot_dir,
        "--sssom", snapshot_dir / "mondo.sssom.tsv",
        "--raw-dir", tmp_path / "raw",
        "--merged-dir", tmp_path / "merged",
        "--date", DATE,
        "--validate-sample", "20",
    ).output
    assert "Merged packets" in out
    merged = _packets(tmp_path / "merged")
    assert {p["statement"]["type"] for p in merged} == {"SynonymStatement", "SubclassStatement", "MappingStatement"}
    assert _packets(tmp_path / "raw")


def test_build_packets_source_and_modality_selection(tmp_path: Path, snapshot_dir: Path) -> None:
    invoke(
        "build-packets",
        "--snapshot-dir", snapshot_dir,
        "--sssom", snapshot_dir / "mondo.sssom.tsv",
        "--source", "omim",
        "--source", "doid",
        "--modality", "synonyms",
        "--raw-dir", tmp_path / "raw",
        "--merged-dir", tmp_path / "merged",
        "--date", DATE,
    )
    merged = _packets(tmp_path / "merged")
    assert {p["statement"]["type"] for p in merged} == {"SynonymStatement"}
    sources = {item["resource"] for p in merged for line in p["statement"]["has_evidence_lines"]
               for item in line["has_evidence_items"] if item.get("resource")}
    assert "infores:orphanet" not in sources


def test_build_packets_accepts_mondo_owl(tmp_path: Path, snapshot_dir: Path) -> None:
    mondo_owl = Path(__file__).parent / "data" / "mini_mondo.owl"
    out = invoke(
        "build-packets",
        "--snapshot-dir", snapshot_dir,
        "--sssom", snapshot_dir / "mondo.sssom.tsv",
        "--mondo-owl", mondo_owl,
        "--modality", "synonyms",
        "--raw-dir", tmp_path / "raw",
        "--merged-dir", tmp_path / "merged",
        "--date", DATE,
    ).output
    assert str(mondo_owl) in out
    assert _packets(tmp_path / "merged")


def test_run_build_step_reads_dated_dirs(tmp_path: Path, snapshot_dir: Path) -> None:
    snap_day = tmp_path / "data" / "snapshots" / DATE
    owl_day = tmp_path / "data" / "owl" / DATE
    snap_day.parent.mkdir(parents=True)
    snapshot_dir.rename(snap_day)
    owl_day.mkdir(parents=True)
    (snap_day / "mondo.sssom.tsv").rename(owl_day / "mondo.sssom.tsv")
    (owl_day / "mondo.owl").write_bytes((Path(__file__).parent / "data" / "mini_mondo.owl").read_bytes())
    invoke(
        "run", "--data-dir", tmp_path / "data", "--date", DATE, "--steps", "build",
        "--raw-dir", tmp_path / "raw", "--merged-dir", tmp_path / "merged",
    )
    assert _packets(tmp_path / "merged")


def test_run_rejects_unknown_step(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["run", "--data-dir", str(tmp_path), "--steps", "score"])
    assert result.exit_code == 1
    assert "score" in result.output


def test_build_packets_rejects_unknown_modality(tmp_path: Path, snapshot_dir: Path) -> None:
    result = runner.invoke(cli.app, [
        "build-packets", "--snapshot-dir", str(snapshot_dir), "--sssom", str(snapshot_dir / "mondo.sssom.tsv"),
        "--modality", "definitions", "--raw-dir", str(tmp_path / "raw"), "--merged-dir", str(tmp_path / "merged"),
    ])
    assert result.exit_code == 1
    assert "definitions" in result.output


def test_build_packets_missing_source_snapshot_fails(tmp_path: Path, snapshot_dir: Path) -> None:
    (snapshot_dir / "ordo.yaml").unlink()
    result = runner.invoke(cli.app, [
        "build-packets", "--snapshot-dir", str(snapshot_dir), "--sssom", str(snapshot_dir / "mondo.sssom.tsv"),
        "--source", "ordo", "--raw-dir", str(tmp_path / "raw"), "--merged-dir", str(tmp_path / "merged"),
    ])
    assert result.exit_code == 1
    assert "ordo" in result.output


# ---------------------------------------------------------------- scoring


def test_score_rescores_store(store: Path) -> None:
    # tamper with one score; `score` must put sieve's value back
    packets = _packets(store)
    expected = packets[0]["statement"]["score"]
    packets[0]["statement"]["score"] = -0.99
    write_store(store, packets)
    out = invoke("score", store).output
    assert "S >= 0.8" in out or "0.4 <= S < 0.8" in out
    assert _get(store, packets[0]["id"])["statement"]["score"] == expected


def test_score_explain(store: Path) -> None:
    pid = _first(store, "SynonymStatement")["id"]
    out = invoke("score", store, "--explain", pid).output
    assert "S =" in out
    assert "cluster" in out


def test_score_explain_unknown_packet(store: Path) -> None:
    result = runner.invoke(cli.app, ["score", str(store), "--explain", "mondoai:pkt/nope"])
    assert result.exit_code == 1


# ---------------------------------------------------------------- enrichment and synthesis


class FakePubMed:
    """Returns one fixed paper for every search."""

    def __init__(self, *args, **kwargs) -> None:
        self.queries: list[str] = []

    def search_and_fetch(self, query: str, max_results: int = 3) -> list[PubMedPaper]:
        self.queries.append(query)
        return [PubMedPaper(pmid="999", title="A paper on CF", abstract="CF is ...", authors=["A B"],
                            journal="J", year="2020", doi=None)]


def test_enrich_attaches_publications(store: Path, monkeypatch) -> None:
    import mondo_ai.enrichment as enrichment

    monkeypatch.setattr(enrichment, "PubMedClient", FakePubMed)
    out = invoke("enrich", store, "--max-papers", "1", "--claim-type", "SynonymStatement", "--limit", "2").output
    assert "Enriched: 2" in out
    enriched = [p for p in _packets(store) if p["statement"].get("has_evidence")]
    assert len(enriched) == 2
    assert all(p["statement"]["type"] == "SynonymStatement" for p in enriched)
    for p in enriched:
        assert validate_packet(p) == []


def test_synthesize_adds_a_line(store: Path, monkeypatch) -> None:
    import mondo_ai.agents as agents

    response = SynthesisResponse(direction="supports", strength=0.9, confidence=0.8, rationale="Agree.", cited_ids=[])

    def fake_agent(provider: str = "anthropic", model=None, **kw) -> SynthesisAgent:
        return SynthesisAgent(llm=TestModel(custom_output_args=response.model_dump()), date=DATE, **kw)

    monkeypatch.setattr(agents, "SynthesisAgent", fake_agent)
    pid = _first(store, "SynonymStatement")["id"]
    before = len(_get(store, pid)["statement"]["has_evidence_lines"])
    out = invoke("synthesize", store, "--limit", "1", "--provider", "anthropic", "--claim-type", "SynonymStatement").output
    assert "Synthesized: 1" in out
    after = _get(store, pid)["statement"]
    assert len(after["has_evidence_lines"]) == before + 1
    assert validate_packet(_get(store, pid)) == []


def test_synthesize_without_api_key_fails(store: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["synthesize", str(store), "--limit", "1"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


# ---------------------------------------------------------------- index and query


def test_index_query_and_stats(store: Path, tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    assert "Indexed" in invoke("index", store, "--db", db).output

    ids = invoke("query", "--db", db, "--claim-type", "MappingStatement", "--format", "ids").output.split()
    assert ids and all(i.startswith("mondoai:pkt/") for i in ids)

    rows = json.loads(invoke("query", "--db", db, "--source", "omim", "--format", "json").output)
    assert rows and all("infores:omim" in r["sources"] for r in rows)

    rows = json.loads(invoke("query", "--db", db, "--min-score", "0.5", "--max-score", "1", "--format", "json").output)
    assert all(0.5 <= r["score"] <= 1 for r in rows)

    table = invoke("query", "--db", db, "--review-state", "PENDING_REVIEW").output
    assert "MONDO:" in table

    out = invoke("index-stats", "--db", db).output
    for heading in ("By Claim Type", "By Review State", "By Strength", "By Score Bucket"):
        assert heading in out


def test_query_without_index_fails(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["query", "--db", str(tmp_path / "none.db")])
    assert result.exit_code == 1


# ---------------------------------------------------------------- show, export, browse


def test_show_formats(store: Path) -> None:
    pid = _first(store, "SynonymStatement")["id"]
    summary = invoke("show", pid, "--store", store).output
    assert "SynonymStatement" in summary and "Evidence lines" in summary
    assert json.loads(invoke("show", pid, "--store", store, "--format", "json").output)["id"] == pid
    assert yaml.safe_load(invoke("show", pid, "--store", store, "--format", "yaml").output)["id"] == pid


def test_show_unknown_packet(store: Path) -> None:
    result = runner.invoke(cli.app, ["show", "mondoai:pkt/nope", "--store", str(store)])
    assert result.exit_code == 1


def test_export_yaml_and_jsonl(store: Path, tmp_path: Path) -> None:
    invoke("export", store, tmp_path / "yaml", "--claim-type", "SubclassStatement")
    files = list((tmp_path / "yaml").glob("*.yaml"))
    assert files
    assert all(yaml.safe_load(f.read_text())["statement"]["type"] == "SubclassStatement" for f in files)

    invoke("export", store, tmp_path / "jsonl", "--format", "jsonl")
    lines = (tmp_path / "jsonl" / "packets.jsonl").read_text().splitlines()
    assert len(lines) == len(_packets(store))


def test_jsonl_browse_and_stats(store: Path) -> None:
    rows = json.loads(invoke("jsonl-browse", "--store", store, "--claim-type", "SynonymStatement",
                             "--source", "doid", "--format", "json").output)
    assert rows and all(r["claim_type"] == "SynonymStatement" and "infores:disease-ontology" in r["sources"]
                        for r in rows)
    out = invoke("jsonl-stats", "--store", store).output
    assert "Total packets" in out
    assert "Indexed" in invoke("jsonl-rebuild-index", "--store", store).output


# ---------------------------------------------------------------- validation, stats, web, config


def test_validate_store_file_and_sample(store: Path, tmp_path: Path) -> None:
    out = invoke("validate", store, "--sample", "5").output
    assert "Invalid packets: 0" in out

    packet = _packets(store)[0]
    good = tmp_path / "p.yaml"
    good.write_text(yaml.safe_dump(packet))
    assert "Valid packets: 1" in invoke("validate", good).output

    bad = tmp_path / "bad.yaml"
    del packet["statement"]["subject"]
    bad.write_text(yaml.safe_dump(packet))
    result = runner.invoke(cli.app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "Invalid packets: 1" in result.output


def test_stats_positional_store(store: Path, tmp_path: Path) -> None:
    out_md = tmp_path / "s.md"
    out = invoke("stats", store, "-o", out_md).output
    assert "Total packets" in out
    assert out_md.read_text().startswith("# Evidence Packet Statistics")


def test_export_web_data(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "data.json"
    invoke("export-web-data", "--store", store, "-o", out)
    assert json.loads(out.read_text())["packets"]


def test_init_config_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    invoke("init-config", path)
    assert runner.invoke(cli.app, ["init-config", str(path)]).exit_code == 1
    from mondo_ai.config import PipelineConfig

    assert [r.infores for r in PipelineConfig.from_yaml(path).resources][0] == "infores:omim"
