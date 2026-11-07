"""End-to-end pipeline tests, including the agent stage against a scripted transport.

The agent is exercised through a fake :class:`~mondo_ai.align.agent.Completer` rather than a
mocked-out adjudicator: the logic worth testing is the batching, the id validation, the
retry-in-isolation and the caching, and all of that lives *above* the transport. Replacing the
adjudicator would test nothing; replacing one function call tests all of it.
"""

from __future__ import annotations

import csv
import json
import pathlib

import pytest
import yaml

from mondo_ai.align.agent import (
    AgentAdjudicator,
    BatchVerdict,
    ItemVerdict,
    build_system_prompt,
    verdict_from_payload,
)
from mondo_ai.align.config import AlignmentConfig, ResourceSpec
from mondo_ai.align.evaluate import gold_standard
from mondo_ai.align.models import EXACT_MATCH, Adjudication, MappingRecord, ReviewTier
from mondo_ai.align.pipeline import AlignmentRunner
from mondo_ai.align.rules import known
from mondo_ai.align.triage import structural_check, triage


# --- fake transports ---------------------------------------------------------------------


def _items(user: str) -> list[dict]:
    return json.loads(user[user.index("["):])


class ScriptedCompleter:
    """Answers each item from a script keyed on subject id, defaulting to ``none``."""

    def __init__(self, script: dict[str, dict] | None = None) -> None:
        self.script = script or {}
        self.calls = 0
        self.batch_sizes: list[int] = []

    def __call__(self, system: str, user: str) -> BatchVerdict:
        self.calls += 1
        items = _items(user)
        self.batch_sizes.append(len(items))
        verdicts = []
        for item in items:
            sid = item["subject"]["id"]
            spec = self.script.get(sid, {})
            verdicts.append(ItemVerdict(
                subject_id=sid,
                decision=spec.get("decision", "none"),
                object_id=spec.get("object_id", ""),
                confidence=spec.get("confidence", 0.7),
                rules=spec.get("rules", ["RJ-004"]),
                justification=spec.get(
                    "justification", "Closest candidate shared only a word."),
                conflation=spec.get("conflation", []),
                needs_expert=spec.get("needs_expert", False),
                expert_question=spec.get("expert_question", ""),
            ))
        return BatchVerdict(verdicts=verdicts)


# --- the agent's discipline ---------------------------------------------------------------


def test_system_prompt_carries_the_rule_registry():
    prompt = build_system_prompt()
    for rule_id in ("EV-001", "SC-001", "SC-007", "PR-008", "CM-006", "RJ-004"):
        assert rule_id in prompt, f"{rule_id} must be citable by the model"
    assert "{rules}" not in prompt


def test_an_invented_identifier_is_rejected_not_repaired():
    with pytest.raises(ValueError, match="not offered"):
        verdict_from_payload(
            {"subject_id": "M:1", "decision": "exact", "object_id": "ICD10CM:INVENTED"},
            {"ICD10CM:A00"},
        )


def test_an_unknown_rule_id_is_dropped_and_noted():
    v = verdict_from_payload(
        {"subject_id": "M:1", "decision": "none", "rules": ["EV-001", "SC-999"],
         "justification": "Nothing fits."},
        set(),
    )
    assert v.rules == ["EV-001"]
    assert all(known(r) for r in v.rules)
    assert "SC-999" in v.justification


def test_a_bad_batch_is_retried_item_wise_so_neighbours_survive():
    """One invalid verdict must not discard the eleven good ones beside it."""

    class PoisonOneItem:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, system: str, user: str) -> BatchVerdict:
            self.calls += 1
            items = _items(user)
            out = []
            for item in items:
                sid = item["subject"]["id"]
                if sid == "M:2" and len(items) > 1:
                    out.append(ItemVerdict(subject_id=sid, decision="exact",
                                           object_id="ICD10CM:INVENTED"))
                elif sid == "M:2":
                    out.append(ItemVerdict(subject_id=sid, decision="exact",
                                           object_id="ICD10CM:INVENTED"))
                else:
                    out.append(ItemVerdict(subject_id=sid, decision="none",
                                           rules=["RJ-001"], justification="Nothing."))
            return BatchVerdict(verdicts=out)

    items = [
        {"subject": {"id": f"M:{i}", "label": "x"}, "candidates": [{"id": "ICD10CM:A00"}]}
        for i in (1, 2, 3)
    ]
    adj = AgentAdjudicator(completer=PoisonOneItem(), batch_size=3, cache_dir=None)
    out = adj.adjudicate(items)

    assert set(out) == {"M:1", "M:2", "M:3"}
    assert out["M:1"].decision == "none" and out["M:1"].decision_path == "A1"
    assert out["M:3"].decision == "none"
    assert adj.failures == ["M:2"]
    assert out["M:2"].needs_expert and out["M:2"].rule_ids == ["RJ-002"]


def test_caching_means_a_rerun_costs_nothing(tmp_path):
    items = [{"subject": {"id": "M:1", "label": "x"},
              "candidates": [{"id": "ICD10CM:A00"}]}]
    script = {"M:1": {"decision": "exact", "object_id": "ICD10CM:A00",
                      "rules": ["EV-002", "PR-001"], "justification": "Same."}}

    first = ScriptedCompleter(script)
    a = AgentAdjudicator(completer=first, cache_dir=tmp_path)
    assert a.adjudicate(items)["M:1"].decision == "exact"
    assert first.calls == 1

    second = ScriptedCompleter(script)
    b = AgentAdjudicator(completer=second, cache_dir=tmp_path)
    out = b.adjudicate(items)
    assert second.calls == 0, "a warm cache must not reach the transport"
    assert b.cache_hits == 1
    assert out["M:1"].decision == "exact"
    assert out["M:1"].decision_path == "A1-cached"


def test_a_changed_candidate_pool_invalidates_only_its_own_item(tmp_path):
    base = {"subject": {"id": "M:1", "label": "x"}, "candidates": [{"id": "ICD10CM:A00"}]}
    changed = {"subject": {"id": "M:1", "label": "x"},
               "candidates": [{"id": "ICD10CM:A00"}, {"id": "ICD10CM:A01"}]}
    a = AgentAdjudicator(completer=ScriptedCompleter(), cache_dir=tmp_path)
    assert a.cache_key(base) != a.cache_key(changed)


def test_concurrency_produces_the_same_verdicts_as_serial():
    items = [
        {"subject": {"id": f"M:{i}", "label": "x"}, "candidates": [{"id": "ICD10CM:A00"}]}
        for i in range(37)
    ]
    serial = AgentAdjudicator(completer=ScriptedCompleter(), batch_size=5, concurrency=1)
    parallel = AgentAdjudicator(completer=ScriptedCompleter(), batch_size=5, concurrency=8)
    a, b = serial.adjudicate(items), parallel.adjudicate(items)
    assert set(a) == set(b) == {f"M:{i}" for i in range(37)}
    assert serial.calls == parallel.calls == 8


# --- structural checks -------------------------------------------------------------------


def test_reverse_proxy_merge_escalates_every_subject_involved():
    # `deterministic=`, not `final=`: triage recomputes the final verdict from the
    # adjudicators, which is what the real pipeline does.
    records = [
        MappingRecord("M:1", deterministic=Adjudication(
            "deterministic", "exact", "ICD10CM:A00", 0.99, decision_path="D1")),
        MappingRecord("M:2", deterministic=Adjudication(
            "deterministic", "exact", "ICD10CM:A00", 0.99, decision_path="D1")),
        MappingRecord("M:3", deterministic=Adjudication(
            "deterministic", "exact", "ICD10CM:A01", 0.99, decision_path="D1")),
    ]
    for r in records:
        r.final = r.deterministic
    defects = structural_check(records)
    assert sorted(defects) == ["M:1", "M:2"]
    assert defects["M:1"] == ["ST-002"]

    out = triage(records)
    escalated = {r.subject_id for r in out if r.review_tier is ReviewTier.EXPERT_REVIEW}
    assert escalated == {"M:1", "M:2"}


def test_broad_matches_to_a_shared_grouping_are_not_a_defect():
    records = [
        MappingRecord("M:1", final=Adjudication("agent", "broad", "ICD10CM:E00")),
        MappingRecord("M:2", final=Adjudication("agent", "broad", "ICD10CM:E00")),
    ]
    # No exactMatch involved, so no claim of referential sameness collides.
    assert structural_check(records) == {}


def test_priority_is_monotonic_in_disagreement():
    agree = MappingRecord(
        "M:1",
        deterministic=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99),
        agent=Adjudication("agent", "exact", "ICD10CM:A00", 0.9),
    )
    disagree = MappingRecord(
        "M:2",
        deterministic=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99),
        agent=Adjudication("agent", "exact", "ICD10CM:A01", 0.9),
    )
    out = triage([agree, disagree])
    assert out[0].subject_id == "M:2"
    assert out[0].review_priority > out[1].review_priority


# --- the whole pipeline ------------------------------------------------------------------


@pytest.fixture
def config(fixture_dir, tmp_path) -> AlignmentConfig:
    """A run config for the fixtures, with Boomer off.

    Boomer is an external Java binary. Leaving it enabled here made every end-to-end test
    start a JVM — 1.6s each, a hard dependency on a tool that is not installed everywhere, and
    an intermittent failure under load. The Boomer path has its own integration test below.
    """
    cfg = AlignmentConfig(
        run_id="test",
        subject=ResourceSpec(name="mondo", prefix="MONDO", format="obo",
                             path=fixture_dir / "subject.obo"),
        object=ResourceSpec(
            name="icd10cm", prefix="ICD10CM", format="owl",
            path=fixture_dir / "object.owl",
            iri_base="http://purl.bioontology.org/ontology/ICD10CM/"),
        conflations_permitted=["CM-001", "CM-002"],
        output_root=tmp_path / "alignments",
    )
    cfg.boomer.enabled = False
    return cfg


def test_full_run_writes_every_artifact_and_gets_the_named_cases_right(config):
    script = {
        # The SC-007 case: the model is given E76.21's children and should accept it.
        "MONDO:0009654": {"decision": "exact", "object_id": "ICD10CM:E76.21",
                          "confidence": 0.85, "rules": ["EV-006", "SC-007", "PR-001"],
                          "justification": "E76.21 divides only into Morquio A/B."},
        # The SC-001 case: a residual rubric is not the disease.
        "MONDO:0008755": {"decision": "narrow", "object_id": "ICD10CM:E80.29",
                          "confidence": 0.8, "rules": ["SC-001", "PR-002"],
                          "justification": "E80.29 is a residual rubric, not the disease."},
        # The SC-004 case: a disjunction is broader by construction.
        "MONDO:0010679": {"decision": "broad", "object_id": "ICD10CM:G71.01",
                          "confidence": 0.9, "rules": ["SC-004", "PR-003"],
                          "justification": "G71.01 covers Becker too."},
    }
    config.agent.cache_dir = ""
    result = AlignmentRunner(config, log=lambda _: None,
                             completer=ScriptedCompleter(script)).run()

    for role in ("sssom", "system_table", "records", "duckdb", "report", "run",
                 "disagreements"):
        assert result.paths[role].exists(), f"{role} was not written"

    by_id = {r.subject_id: r for r in result.records}

    # D1: identical labels, nothing against it.
    cholera = by_id["MONDO:0000100"]
    assert cholera.final.predicate == EXACT_MATCH
    assert cholera.final.object_id == "ICD10CM:A00"
    # D2, not D1: Mondo writes "cholera", ICD-10-CM writes "Cholera". D1 demands
    # byte-identical primary labels; case is settled one rung down, at EV-002.
    assert cholera.final.decision_path == "D2"
    assert cholera.review_tier is ReviewTier.AUTO_ACCEPT

    # D4: the possessive rewrite.
    marfan = by_id["MONDO:0007947"]
    assert marfan.final.object_id == "ICD10CM:Q87.4"
    assert marfan.final.decision_path == "D4"
    assert "strip_possessive" in marfan.candidate("ICD10CM:Q87.4").transforms()

    # SC-007 accepted by the agent, and it is not auto-accepted.
    mps = by_id["MONDO:0009654"]
    assert mps.final.object_id == "ICD10CM:E76.21"
    assert mps.final.predicate == EXACT_MATCH
    assert mps.final.source == "agent"
    assert mps.review_tier is not ReviewTier.AUTO_ACCEPT

    # SC-001 becomes a narrowMatch with its rule, not a silent exactMatch.
    porphyria = by_id["MONDO:0008755"]
    assert porphyria.final.predicate == "skos:narrowMatch"
    assert "SC-001" in porphyria.final.rule_ids

    # A chapter range never reaches the output as an object.
    metabolic = by_id["MONDO:0005066"]
    assert metabolic.final.object_id == ""
    assert "ST-005" in metabolic.final.rule_ids

    # A susceptibility subject is never auto-accepted.
    suscept = by_id["MONDO:0022222"]
    assert suscept.review_tier is not ReviewTier.AUTO_ACCEPT

    # Obsolete subjects are not in the run at all.
    assert "MONDO:0033333" not in by_id


def test_emitted_sssom_declares_its_extension_slots_and_protocol(config):
    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()

    text = result.paths["sssom"].read_text()
    header = yaml.safe_load(
        "\n".join(ln[1:] for ln in text.splitlines() if ln.startswith("#"))
    )
    declared = {d["slot_name"] for d in header["extension_definitions"]}
    assert {"review_tier", "review_priority", "decision_path", "risk_flags",
            "mapping_tier"} <= declared

    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    columns = body[0].split("\t")
    for slot in declared:
        assert slot in columns, f"{slot} is declared but not emitted"

    # The review protocol must name the conflations the set permits and the tiering.
    assert "CM-002" in header["comment"]
    assert "Tier 3" in header["comment"]
    assert header["subject_source"] == "mondo"


def test_no_match_rows_are_distinguishable_from_unexamined_rows(config):
    """The one output that must never happen: an unexamined term reading as "no code"."""
    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()

    rows = list(csv.DictReader(
        result.paths["system_table"].read_text().splitlines(), delimiter="\t"))
    for row in rows:
        if row["review_tier"] == "NO_MATCH":
            assert "maprule:RJ-001" in row["curation_rule"], (
                "a NO_MATCH row must carry the rejection ground that justifies it"
            )
        if row["review_tier"] == "EXPERT_REVIEW" and row["object_id"] == "sssom:NoTermFound":
            assert row["expert_question"], (
                "an unexamined term must carry the question, not an empty absence"
            )


def test_run_record_captures_enough_to_reproduce(config):
    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()
    doc = yaml.safe_load(result.paths["run"].read_text())
    assert doc["sources"]["subject"]["sha256"]
    assert doc["sources"]["object"]["sha256"]
    assert doc["config"]["object"]["iri_base"]
    assert doc["review_tiers"]


def test_gold_standard_is_held_out_by_default(config, subjects):
    """With hold_out true, no asserted mapping may enter as proxy evidence."""
    gold = gold_standard(subjects, "ICD10CM", "MONDO:equivalentTo")
    assert "MONDO:0007947" in gold

    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()
    for record in result.records:
        for cand in record.candidates:
            assert not any(s.kind == "proxy" for s in cand.signals), (
                "a held-out gold mapping leaked into candidate generation"
            )
    assert result.evaluation is not None
    assert result.evaluation.overall.true_positive > 0


def test_undeclared_conflation_escalates(config):
    """A boundary the run did not authorise is a curator's call, not a shipped row."""
    script = {"MONDO:0002169": {"decision": "exact", "object_id": "ICD10CM:R48.1",
                                "confidence": 0.95, "rules": ["EV-002", "PR-001"],
                                "conflation": ["CM-005"],
                                "justification": "Same finding."}}
    config.agent.cache_dir = ""
    result = AlignmentRunner(config, log=lambda _: None,
                             completer=ScriptedCompleter(script)).run()
    rec = {r.subject_id: r for r in result.records}["MONDO:0002169"]
    if rec.final.source == "agent":
        assert "CM-005" in rec.structural_flags
        assert rec.review_tier is ReviewTier.EXPERT_REVIEW


def test_emitted_set_round_trips_through_sssom_py(config):
    """The emitted file must still be a parseable SSSOM set, not merely a TSV.

    Regression: ``author_id`` was originally filled with ``"mondo-ai align (deterministic)"``.
    It is a CURIE-valued slot, so sssom-py silently dropped **every row** — the file parsed,
    reported zero mappings, and nothing said why. The Mondo curation guide wants Tier 3
    provenance in ``mapping_tool`` / ``mapping_tool_version`` anyway, and inventing an ORCID
    for a pipeline would be worse than leaving the slot out.
    """
    sssom = pytest.importorskip("sssom.parsers")

    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()

    msdf = sssom.parse_sssom_table(str(result.paths["sssom"]))
    n_rows = sum(1 for _ in result.records)
    assert len(msdf.df) == n_rows, (
        "sssom-py dropped rows; a slot value is probably not of the type SSSOM expects"
    )
    assert "author_id" not in msdf.df.columns
    for prefix in ("MONDO", "ICD10CM", "skos", "semapv", "maprule"):
        assert prefix in msdf.prefix_map, f"{prefix} must be declared in curie_map"
    assert msdf.prefix_map["MONDO"] == "http://purl.obolibrary.org/obo/MONDO_"


def test_cost_is_reported_so_raising_the_cap_is_an_informed_decision():
    from mondo_ai.align.agent import estimate_cost

    usage = {"input_tokens": 500_000, "output_tokens": 200_000,
             "cache_read_input_tokens": 2_000_000, "cache_creation_input_tokens": 10_000}
    cost = estimate_cost("claude-opus-5", usage)
    assert cost is not None and cost > 0
    assert estimate_cost("a-model-that-does-not-exist-yet", usage) is None, (
        "an unpriced model must report None, not a confident zero"
    )


def test_unexamined_rows_are_queued_not_passed_off_as_expert_questions(config):
    """The distinction that makes the triage table readable.

    Regression: with these collapsed, a full Mondo run reported 89% `EXPERT_REVIEW`, which was
    true of neither the rows nor the curator's actual worklist.
    """
    config.agent.enabled = True
    config.agent.max_items = 1  # everything else is deferred unexamined
    config.agent.cache_dir = ""
    result = AlignmentRunner(config, log=lambda _: None,
                             completer=ScriptedCompleter()).run()

    tiers = {r.subject_id: r.review_tier for r in result.records}
    queued = [sid for sid, t in tiers.items() if t is ReviewTier.QUEUED]
    assert queued, "the budget was 1 item; some subjects must be QUEUED"

    for sid in queued:
        rec = {r.subject_id: r for r in result.records}[sid]
        assert rec.final.unexamined
        assert rec.final.expert_question, "a queued row must say that nobody has judged it"
        assert rec.review_priority < 0.6, (
            "an unexamined row must not claim the needs-expert priority floor and outrank "
            "questions an adjudicator actually formed"
        )

    # And the tier survives into both emitted tables.
    rows = list(csv.DictReader(
        result.paths["system_table"].read_text().splitlines(), delimiter="\t"))
    assert any(row["review_tier"] == "QUEUED" for row in rows)


# --- curation rules are cited by CURIE, not by prose --------------------------------------


def test_rows_cite_rules_by_curie_and_resolve_against_the_value_set(config):
    """A 32k-row file must not carry the registry's prose in every cell.

    The `curation_rule` slot is URI-valued, so the row carries `maprule:SC-001|maprule:PR-002`
    and the reader joins against the published value set. The prose belongs in the value set,
    once, and in the set-level protocol paragraph a human reads once.
    """
    from mondo_ai.align.rules import REGISTRY
    from mondo_ai.align.valueset import render_skos

    config.agent.enabled = False
    result = AlignmentRunner(config, log=lambda _: None).run()

    body = [ln for ln in result.paths["sssom"].read_text().splitlines()
            if not ln.startswith("#")]
    rows = [r for r in csv.DictReader(body, delimiter="\t") if r.get("subject_id")]
    assert rows
    assert "curation_rule_text" not in rows[0], "the prose column must be gone from rows"

    ttl = render_skos()
    cited = set()
    for row in rows:
        for curie in (row["curation_rule"] or "").split("|"):
            if not curie:
                continue
            assert curie.startswith("maprule:"), f"{curie!r} is not a maprule CURIE"
            assert " " not in curie, f"{curie!r} carries prose, not just an identifier"
            cited.add(curie)
    assert cited, "no rules were cited at all"
    for curie in cited:
        assert curie.split(":")[1] in REGISTRY
        assert f"{curie} a skos:Concept" in ttl, f"{curie} is not in the published value set"

    header = yaml.safe_load(
        "\n".join(ln[1:] for ln in result.paths["sssom"].read_text().splitlines()
                  if ln.startswith("#")))
    assert header["curie_map"]["maprule"] == "https://w3id.org/mapping-rules/", (
        "rows cite maprule: CURIEs, so the namespace must be declared in curie_map"
    )


def test_value_set_artefacts_match_the_registry():
    """The published value set is generated; a stale checked-in copy is a defect.

    The root is resolved from this file rather than from the working directory, so the test
    means the same thing under `pytest`, under `just test`, and from a subdirectory.
    """
    from mondo_ai.align.valueset import check_all

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    stale = check_all(repo_root)
    assert not stale, (
        "run `just align-valueset`; these are out of date with mondo_ai.align.rules: "
        + ", ".join(str(p) for p in stale)
    )


def test_justfile_views_reference_columns_that_exist():
    """The `just align-*` inspection views resolve columns by name; those names must exist.

    Regression: the views originally indexed fields by position, and when the Boomer columns
    took the system table from 16 to 21 columns `align-queue` silently printed `risk_flags`
    where it meant `top_priority_reason` — an inspection tool being quietly wrong rather than
    failing. They now look the columns up in the header, which moves the breakage to a rename;
    this test catches that.
    """
    import re

    from mondo_ai.align.emit import SYSTEM_TABLE_COLUMNS

    justfile = pathlib.Path(__file__).resolve().parents[2] / "align.just"
    text = justfile.read_text()
    referenced = set(re.findall(r'\$c\["([a-z_0-9]+)"\]', text))
    assert referenced, "no column references found; did the views stop using named lookup?"
    unknown = sorted(referenced - set(SYSTEM_TABLE_COLUMNS))
    assert not unknown, (
        f"align.just reads columns that the system table does not emit: {unknown}"
    )

    # The consolidated-set views use `$k[...]`, against the reconciled SSSOM columns.
    from mondo_ai.align.reconcile import CONSOLIDATED_EXTENSIONS, CONSOLIDATED_STANDARD

    reconciled = set(re.findall(r'\$k\["([a-z_0-9]+)"\]', text))
    assert reconciled, "no consolidated-set column references found"
    unknown = sorted(reconciled - set(CONSOLIDATED_STANDARD) - set(CONSOLIDATED_EXTENSIONS))
    assert not unknown, f"align.just reads consolidated columns that do not exist: {unknown}"


# --- Boomer: the real solver, behind a marker -----------------------------------------------


@pytest.mark.integration
def test_boomer_conditions_the_set_and_posterior_equals_prior_when_unconstrained(config):
    """Exercise the real Boomer binary end to end, and assert its defining invariant.

    The invariant is the one that proves the max-marginal arithmetic: a mapping nothing in the
    set constrains must come back with `posterior == prior` exactly. An earlier version pinned
    each option with near-certain prior mass and forgot to subtract that mass back out, which
    made every unconstrained mapping collapse to a uniform 0.25 — the prior had been discarded.
    This test fails if that regresses.

    Skipped rather than failed where boomer is not installed: it is an external tool, and its
    absence is not a defect in this repository.
    """
    from mondo_ai.align.boomer import find_boomer

    if find_boomer() is None:
        pytest.skip("boomer executable not on PATH; see docs/alignment.md")

    config.agent.enabled = False
    config.boomer.enabled = True
    result = AlignmentRunner(config, log=lambda _: None).run()

    assert result.boomer_stats.get("ok"), (
        f"boomer did not run: {result.boomer_stats.get('error')}"
    )
    conditioned = [r for r in result.records if r.boomer]
    assert conditioned, "boomer ran but annotated nothing"

    unconstrained = [r for r in conditioned if not r.boomer["constrained"]]
    assert unconstrained, "the fixture produced no unconstrained mappings to check"
    for record in unconstrained:
        assert record.boomer["posterior"] == record.boomer["prior"], (
            f"{record.subject_id}: an unconstrained mapping's posterior must equal its prior "
            f"exactly, got {record.boomer['posterior']} against {record.boomer['prior']}"
        )

    # The columns must reach the emitted files, not just the in-memory records.
    body = [ln for ln in result.paths["sssom"].read_text().splitlines()
            if not ln.startswith("#")]
    rows = [r for r in csv.DictReader(body, delimiter="\t") if r.get("subject_id")]
    assert any(r["boomer_prior"] for r in rows)
    assert any(r["boomer_posterior"] for r in rows)
