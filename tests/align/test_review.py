"""Review mode: an existing SSSOM file in, the same rows out with confidence columns.

The contract worth testing first is the one a reviewer of the output relies on without
checking: every input row comes back, in order, with every input cell untouched. Then the
three lines of evidence, then the boxes — the part that makes Boomer scale, and the part
whose failure would be silent.
"""

from __future__ import annotations

import json
import math

import pytest

from mondo_ai.align import boomer as bm
from mondo_ai.align.config import AlignmentConfig, ResourceSpec
from mondo_ai.align.models import Concept, ResourceSet
from mondo_ai.align.review import (
    REVIEW_EXTENSIONS,
    Box,
    ReviewBatch,
    Reviewer,
    ReviewRunner,
    ReviewVerdict,
    add_halos,
    make_boxes,
    read_sssom,
    solve_bin,
)

CANDIDATES = """\
# curie_map:
#   MONDO: http://purl.obolibrary.org/obo/MONDO_
#   ICD10CM: http://purl.bioontology.org/ontology/ICD10CM/
#   DOID: http://purl.obolibrary.org/obo/DOID_
#   skos: http://www.w3.org/2004/02/skos/core#
#   semapv: https://w3id.org/semapv/vocab/
# license: https://creativecommons.org/publicdomain/zero/1.0/
# mapping_set_id: https://example.org/candidates.sssom.tsv
subject_id\tsubject_label\tpredicate_id\tobject_id\tmapping_justification\tconfidence\tauthor_note
MONDO:0000100\tcholera\tskos:exactMatch\tICD10CM:A00\tsemapv:ManualMappingCuration\t\tkeep me
MONDO:0007947\tMarfan syndrome\tskos:exactMatch\tICD10CM:Q87.4\tsemapv:LexicalMatching\t0.8\t
MONDO:0010679\tDuchenne muscular dystrophy\tskos:exactMatch\tICD10CM:G71.01\tsemapv:ManualMappingCuration\t\tdisjunction
MONDO:0010679\tDuchenne muscular dystrophy\tskos:broadMatch\tICD10CM:G71.01\tsemapv:ManualMappingCuration\t\tsame pair again
MONDO:0005066\tmetabolic disease\tskos:exactMatch\tICD10CM:E00-E90\tsemapv:ManualMappingCuration\t\t
MONDO:0033333\tobsolete thing\tskos:exactMatch\tICD10CM:A01\tsemapv:ManualMappingCuration\t\t
MONDO:0000100\tcholera\tskos:exactMatch\tDOID:1498\tsemapv:ManualMappingCuration\t\tother vocabulary
"""


@pytest.fixture
def config(fixture_dir, tmp_path) -> AlignmentConfig:
    """A review config over the shared fixtures; Boomer off, as in the pipeline tests."""
    cfg = AlignmentConfig(
        run_id="review-test",
        subject=ResourceSpec(name="mondo", prefix="MONDO", format="obo",
                             path=fixture_dir / "subject.obo"),
        object=ResourceSpec(
            name="icd10cm", prefix="ICD10CM", format="owl",
            path=fixture_dir / "object.owl",
            iri_base="http://purl.bioontology.org/ontology/ICD10CM/"),
        output_root=tmp_path / "alignments",
    )
    cfg.boomer.enabled = False
    cfg.agent.cache_dir = ""
    return cfg


@pytest.fixture
def candidates(tmp_path):
    path = tmp_path / "candidates.sssom.tsv"
    path.write_text(CANDIDATES)
    return path


class ScriptedReviewer:
    """Answers each mapping from a script keyed on mapping id."""

    def __init__(self, script: dict[str, dict] | None = None) -> None:
        self.script = script or {}
        self.seen: list[str] = []

    def __call__(self, system: str, user: str) -> ReviewBatch:
        items = json.loads(user[user.index("["):])
        out = []
        for item in items:
            self.seen.append(item["mapping_id"])
            spec = self.script.get(item["mapping_id"], {})
            out.append(ReviewVerdict(
                mapping_id=item["mapping_id"], relation=spec.get("relation", "exact"),
                confidence=spec.get("confidence", 0.9), rules=spec.get("rules", ["EV-001"]),
                justification=spec.get("justification", "Same disease.")))
        return ReviewBatch(verdicts=out)


def _run(config, candidates, tmp_path, completer=None):
    return ReviewRunner(config, candidates, tmp_path / "out", log=lambda _: None,
                        completer=completer).run()


# --- the contract: same rows, same cells -------------------------------------------------


def test_every_input_row_comes_back_in_order_with_its_cells_untouched(
        config, candidates, tmp_path):
    config.review.agent = False
    before = read_sssom(candidates)
    result = _run(config, candidates, tmp_path)
    after = read_sssom(result.paths["reviewed"])

    assert len(after.rows) == len(before.rows)
    assert after.columns[: len(before.columns)] == before.columns
    for old, new in zip(before.rows, after.rows):
        assert {k: new[k] for k in before.columns} == old
    for column in REVIEW_EXTENSIONS:
        assert column in after.columns
    assert after.metadata["mapping_set_id"] == before.metadata["mapping_set_id"]


def test_rows_outside_the_two_vocabularies_are_kept_and_labelled(config, candidates, tmp_path):
    config.review.agent = False
    rows = read_sssom(_run(config, candidates, tmp_path).paths["reviewed"]).rows
    doid = next(r for r in rows if r["object_id"] == "DOID:1498")
    assert doid["review_status"] == "out_of_scope_prefix"
    assert doid["author_note"] == "other vocabulary"


def test_deterministic_evidence_names_the_known_cases(config, candidates, tmp_path):
    config.review.agent = False
    rows = {(r["subject_id"], r["object_id"], r["predicate_id"]): r
            for r in read_sssom(_run(config, candidates, tmp_path).paths["reviewed"]).rows}
    cholera = rows[("MONDO:0000100", "ICD10CM:A00", "skos:exactMatch")]
    assert cholera["review_status"] == "corroborated"
    assert float(cholera["lexical_confidence"]) > 0.9
    assert rows[("MONDO:0033333", "ICD10CM:A01", "skos:exactMatch")]["review_tier"] == "REJECTED"
    chapter = rows[("MONDO:0005066", "ICD10CM:E00-E90", "skos:exactMatch")]
    assert chapter["review_tier"] == "EXPERT_REVIEW"
    # The duplicate pair keeps its own predicate and says it was reviewed as the first one.
    dup = rows[("MONDO:0010679", "ICD10CM:G71.01", "skos:broadMatch")]
    assert dup["review_comment"].startswith("duplicate pair")


def test_reviewed_file_round_trips_through_sssom_py(config, candidates, tmp_path):
    sssom = pytest.importorskip("sssom.parsers")
    config.review.agent = False
    result = _run(config, candidates, tmp_path)
    msdf = sssom.parse_sssom_table(str(result.paths["reviewed"]))
    assert len(msdf.df) == len(read_sssom(candidates).rows)


# --- the reviewing model -----------------------------------------------------------------


def test_reviewer_confidence_is_the_models_probability_for_the_asserted_row(
        config, candidates, tmp_path):
    script = {"MONDO:0010679 ICD10CM:G71.01": {
        "relation": "broad", "confidence": 0.1, "rules": ["SC-004"],
        "justification": "G71.01 also covers Becker, so it is broader."}}
    rows = read_sssom(_run(config, candidates, tmp_path,
                           ScriptedReviewer(script)).paths["reviewed"]).rows
    dmd = next(r for r in rows if r["object_id"] == "ICD10CM:G71.01"
               and r["predicate_id"] == "skos:exactMatch")
    assert dmd["reviewer_confidence"] == "0.1"
    assert dmd["reviewer_predicate"] == "skos:broadMatch"
    assert "SC-004" in dmd["reviewer_rules"]
    # The second row for the pair asserts another predicate: the relation call carries
    # over, the probability (which was about the first predicate) does not.
    dup = next(r for r in rows if r["object_id"] == "ICD10CM:G71.01"
               and r["predicate_id"] == "skos:broadMatch")
    assert dup["reviewer_predicate"] == "skos:broadMatch"
    assert dup["reviewer_confidence"] == ""


def test_the_budget_reaches_the_most_contested_rows_and_leaves_the_rest_empty(
        config, candidates, tmp_path):
    config.review.max_items = 1
    fake = ScriptedReviewer()
    result = _run(config, candidates, tmp_path, fake)
    rows = read_sssom(result.paths["reviewed"]).rows
    reviewed = [r for r in rows if r["reviewer_confidence"]]
    assert len(fake.seen) == 1 and len(reviewed) == 1
    assert reviewed[0]["review_tier"] == "EXPERT_REVIEW"
    assert result.stats["reviewer"]["not_reached"] >= 1
    # Unreached rows are empty, never zero: nobody looked.
    assert all(r["reviewer_confidence"] == "" for r in rows if r is not reviewed[0])


def test_a_verdict_that_fails_validation_is_recorded_as_no_review():
    class Bad:
        def __call__(self, system, user):
            items = json.loads(user[user.index("["):])
            return ReviewBatch(verdicts=[ReviewVerdict(
                mapping_id=i["mapping_id"], relation="probably", confidence=0.5)
                for i in items])

    reviewer = Reviewer(completer=Bad(), model="m", batch_size=2)
    out = reviewer.review([{"mapping_id": "A B"}, {"mapping_id": "C D"}])
    assert out == {}
    assert sorted(reviewer.failures) == ["A B", "C D"]


def test_review_cache_makes_a_rerun_free(tmp_path):
    first = ScriptedReviewer()
    Reviewer(completer=first, model="m", cache_dir=tmp_path).review([{"mapping_id": "A B"}])
    second = ScriptedReviewer()
    again = Reviewer(completer=second, model="m", cache_dir=tmp_path)
    assert again.review([{"mapping_id": "A B"}])["A B"].confidence == 0.9
    assert second.seen == [] and again.cache_hits == 1


# --- boxes --------------------------------------------------------------------------------


def _inversion() -> tuple[ResourceSet, ResourceSet, list[tuple[str, str]]]:
    """Mondo has A above B; SNOMED has B' above A'. Mapping A=A' and B=B' is a cycle.

    The shape of *encephalomyelitis* / *myelitis* on the real set: the hierarchies disagree,
    so at most one of the two exact mappings can hold.
    """
    s = ResourceSet(name="m", prefix="M", concepts={
        "M:A": Concept(id="M:A", children=["M:B"]),
        "M:B": Concept(id="M:B", parents=["M:A"]),
        "M:C": Concept(id="M:C")})
    o = ResourceSet(name="s", prefix="S", concepts={
        "S:B": Concept(id="S:B", children=["S:A"]),
        "S:A": Concept(id="S:A", parents=["S:B"]),
        "S:C": Concept(id="S:C")})
    return s, o, [("M:A", "S:A"), ("M:B", "S:B"), ("M:C", "S:C")]


def test_comparable_mappings_share_a_box_and_unrelated_ones_do_not():
    s, o, pairs = _inversion()
    boxes = make_boxes(pairs, s, o)
    assert [b.pairs for b in boxes] == [[("M:A", "S:A"), ("M:B", "S:B")], [("M:C", "S:C")]]


def test_a_hard_cut_box_still_sees_its_neighbours_through_the_halo():
    s, o, pairs = _inversion()
    boxes = make_boxes(pairs, s, o, max_box=1)
    assert all(len(b.pairs) == 1 for b in boxes), "max_box=1 must cut the inversion apart"
    add_halos(boxes, s, o)
    by_core = {b.pairs[0]: b.halo for b in boxes}
    assert by_core[("M:A", "S:A")] == [("M:B", "S:B")]
    assert by_core[("M:C", "S:C")] == []


@pytest.mark.integration
def test_boxes_with_halos_reproduce_the_single_box_answer(tmp_path):
    """The scaling claim, against the real solver: cut the inversion into one-row boxes,
    give them halos, and every posterior must match solving everything as one box.

    Without the halo the cut boxes each see one mapping alone, and both come back with
    posterior equal to prior: the inversion silently disappears. That was the failure on
    the real Mondo x SNOMED CT set before halos existed.
    """
    if bm.find_boomer() is None:
        pytest.skip("boomer executable not on PATH; see docs/alignment.md")
    s, o, pairs = _inversion()
    priors = {p: bm.priors_for("skos:exactMatch", 0.9) for p in pairs}
    preds = {p: "skos:exactMatch" for p in pairs}
    cfg = AlignmentConfig(
        run_id="t", subject=ResourceSpec(name="m", prefix="M", format="obo", path="m"),
        object=ResourceSpec(name="s", prefix="S", format="obo", path="s")).boomer
    kw = dict(subject_iri="http://x/M_", object_iri="http://y/S_", cfg=cfg, timeout=300)

    whole = solve_bin(tmp_path / "whole", [Box("all", sorted(pairs))], priors, preds, s, o, **kw)
    boxes = make_boxes(pairs, s, o, max_box=1)
    add_halos(boxes, s, o)
    cut = solve_bin(tmp_path / "cut", boxes, priors, preds, s, o, **kw)

    assert not whole.failed and not cut.failed
    inverted = [p for p in pairs[:2] if whole.cells[p]["posterior"] < 0.9 - 0.05]
    assert inverted, "the single box must see the inversion"
    for p in pairs:
        assert math.isclose(whole.cells[p]["posterior"], cut.cells[p]["posterior"],
                            abs_tol=1e-6), p
    # The unconstrained mapping keeps its prior exactly, boxed or not.
    assert cut.cells[("M:C", "S:C")]["posterior"] == cut.cells[("M:C", "S:C")]["prior"]
