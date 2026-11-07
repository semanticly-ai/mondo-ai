"""Scope detectors and the deterministic ladder, against the real fixtures.

These are the tests that encode the pipeline's quality claims. Each one names the curation
rule it is about, so a failure says which rule regressed rather than which assertion moved.
"""

from __future__ import annotations

import pytest

from mondo_ai.align.candidates import CandidateGenerator, category_key
from mondo_ai.align.deterministic import adjudicate
from mondo_ai.align.flags import (
    BLOCKING_FLAGS,
    disjunctive,
    finer_variant,
    numbered_subtype,
    object_flags,
    off_category,
    possibly_coextensive,
    residual_rubric,
    subject_flags,
    susceptibility,
)
from mondo_ai.align.models import EXACT_MATCH, Candidate, Signal
from mondo_ai.align.rules import REGISTRY, known


# --- the rule registry is the contract ---------------------------------------------------


def test_every_flag_a_detector_can_emit_is_a_real_rule():
    """A flag that is not in the registry would be an uncitable private label."""
    emitted = {
        "SC-001", "SC-002", "SC-003", "SC-004", "SC-005", "SC-006", "SC-007",
        "CM-002", "CM-003", "CM-005", "CM-006", "CM-007", "CM-009",
        "ST-004", "ST-005",
    }
    assert all(known(f) for f in emitted)
    assert BLOCKING_FLAGS <= set(REGISTRY)


def test_blocking_flags_agree_with_the_registry():
    """``BLOCKING_FLAGS`` must not disagree with each rule's own ``blocks_exact_match``."""
    for flag in BLOCKING_FLAGS:
        assert REGISTRY[flag].blocks_exact_match, f"{flag} blocks here but not in the registry"


# --- SC-001 / SC-002 vs SC-007: the hardest judgement ------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Other porphyria", "SC-001"),
        ("Other specified congenital malformation syndromes", "SC-001"),
        ("Polyneuropathy, unspecified", "SC-002"),
        ("Morquio mucopolysaccharidoses, unspecified", "SC-002"),
        ("Pneumonia, not elsewhere classified", "SC-001"),
        ("Cholera", None),
        ("Marfan's syndrome", None),
    ],
)
def test_residual_rubric_detection(label, expected):
    assert residual_rubric(label) == expected


def test_coextensive_rubric_is_distinguished_from_a_residual(objects):
    """``SC-007``: E76.21 divides only into Morquio subtypes, so it *is* MPS IV."""
    e76_21 = objects.concepts["ICD10CM:E76.21"]
    assert possibly_coextensive(
        "mucopolysaccharidosis type IV", e76_21, objects.concepts
    ), "E76.21 subdivides only into Morquio A/B/unspecified and should read as coextensive"


def test_residual_leaf_is_not_coextensive(objects):
    """``SC-001`` stands for a leaf rubric: no children means no evidence either way."""
    e80_29 = objects.concepts["ICD10CM:E80.29"]
    assert not possibly_coextensive(
        "hereditary coproporphyria", e80_29, objects.concepts
    )


def test_coextensive_requires_the_subject_to_be_on_the_table(objects):
    """A rubric unrelated to the subject is not a coextensive reading of it."""
    e76_21 = objects.concepts["ICD10CM:E76.21"]
    assert not possibly_coextensive("cholera", e76_21, objects.concepts)


# --- the other scope and conceptual-model detectors --------------------------------------


def test_disjunction_is_flagged_but_a_coordinated_modifier_is_not():
    assert disjunctive("Duchenne or Becker muscular dystrophy") == "SC-004"
    assert disjunctive("Acromegaly and pituitary gigantism") == "SC-004"
    assert disjunctive("Acute or chronic pancreatitis") is None


def test_finer_variants_are_flagged():
    assert finer_variant("Juvenile rheumatoid arthritis, right shoulder") == "SC-005"
    assert finer_variant("Juvenile arthritis") is None


def test_numbered_and_susceptibility_subjects_are_flagged(subjects):
    assert numbered_subtype("immunodeficiency 112") == "SC-006"
    assert susceptibility("malignant hyperthermia, susceptibility to") == "CM-006"
    assert subject_flags(subjects.concepts["MONDO:0011111"]) == ["SC-006"]
    assert "CM-006" in subject_flags(subjects.concepts["MONDO:0022222"])


def test_chapter_range_is_not_a_code(objects):
    assert object_flags(objects.concepts["ICD10CM:E00-E90"], "ICD10CM") == ["ST-005"]


def test_off_category_chapters_map_to_the_right_conflation_rule():
    assert off_category("ICD10CM:R48.1") == "CM-002"
    assert off_category("ICD10CM:Z94.0") == "CM-003"
    assert off_category("ICD10CM:S72.001A") == "CM-009"
    assert off_category("ICD10CM:E84") is None


# --- candidate generation ----------------------------------------------------------------


def test_possessive_rewrite_recovers_marfan(subjects, objects, object_index):
    """The rewrite that motivated the rule table: Mondo drops the possessive ICD keeps."""
    gen = CandidateGenerator(object_index, objects, object_prefix="ICD10CM")
    pool = gen.generate(subjects.concepts["MONDO:0007947"])
    by_id = {c.object_id: c for c in pool}
    assert "ICD10CM:Q87.4" in by_id
    cand = by_id["ICD10CM:Q87.4"]
    assert "strip_possessive" in cand.transforms()
    assert any(s.rule_id == "EV-004" for s in cand.signals)


def test_synonym_crossing_is_ev003_not_ev001(subjects, objects, object_index):
    """``hereditary coproporphyria`` hits E80.29 only via the rubric's inclusion synonym."""
    gen = CandidateGenerator(object_index, objects, object_prefix="ICD10CM")
    pool = gen.generate(subjects.concepts["MONDO:0008755"])
    by_id = {c.object_id: c for c in pool}
    assert "ICD10CM:E80.29" in by_id
    assert any(s.rule_id == "EV-003" for s in by_id["ICD10CM:E80.29"].signals)
    assert "SC-001" in by_id["ICD10CM:E80.29"].flags


def test_per_category_cap_keeps_the_general_rubric_reachable():
    """Laterality subcodes must not crowd a category out of its own candidate list."""
    assert category_key("ICD10CM:M08.011") == category_key("ICD10CM:M08") == "M08"


def test_coextensive_annotation_adds_sc007_alongside_the_residual(subjects, objects,
                                                                 object_index):
    gen = CandidateGenerator(object_index, objects, object_prefix="ICD10CM")
    subject = subjects.concepts["MONDO:0009654"]
    cands = [Candidate("ICD10CM:E76.219", flags=["SC-002"])]
    gen.annotate_coextensive(subject, cands)
    # E76.219 is a leaf, so the residual reading stands.
    assert cands[0].flags == ["SC-002"]

    cands = [Candidate("ICD10CM:E76.21", flags=["SC-002"])]
    gen.annotate_coextensive(subject, cands)
    assert "SC-007" in cands[0].flags
    assert "SC-002" in cands[0].flags, "the competing reading must survive, not be replaced"


# --- the ladder --------------------------------------------------------------------------


def _pool(object_id, tier, *, flags=(), rule="EV-001", certainty=1.0):
    return Candidate(
        object_id,
        [Signal("lexical_exact", rule, certainty)],
        flags=list(flags),
        tier=tier,
    )


def test_d1_settles_a_clean_unique_label_hit(subjects):
    subject = subjects.concepts["MONDO:0000100"]
    result = adjudicate(subject, [_pool("ICD10CM:A00", 1)])
    assert result.decision_path == "D1"
    assert result.settled
    assert result.adjudication.predicate == EXACT_MATCH
    assert "PR-001" in result.adjudication.rule_ids


def test_d5_refuses_to_downgrade_a_blocked_hit(subjects):
    """A residual rubric is real string evidence and no evidence about extension."""
    subject = subjects.concepts["MONDO:0008755"]
    result = adjudicate(subject, [_pool("ICD10CM:E80.29", 3, flags=["SC-001"])])
    assert result.decision_path == "D5"
    assert not result.settled
    assert result.adjudication.decision == "none", (
        "the ladder must not invent a broadMatch on its own authority"
    )
    assert "SC-001" in result.adjudication.rule_ids
    assert result.review_candidates[0].object_id == "ICD10CM:E80.29"


def test_d6_asserts_nothing_when_two_objects_tie(subjects):
    subject = subjects.concepts["MONDO:0000100"]
    result = adjudicate(
        subject, [_pool("ICD10CM:A00", 2), _pool("ICD10CM:A01", 2)]
    )
    assert result.decision_path == "D6"
    assert result.adjudication.decision == "none"
    assert "PR-008" in result.adjudication.rule_ids
    assert result.ambiguity_count == 2


def test_d0_rejects_a_chapter_range_terminally(subjects):
    subject = subjects.concepts["MONDO:0005066"]
    result = adjudicate(subject, [_pool("ICD10CM:E00-E90", 1, flags=["ST-005"])])
    assert result.decision_path == "D0"
    assert result.settled
    assert "ST-005" in result.adjudication.rule_ids


def test_d7_records_absence_as_a_finding(subjects):
    result = adjudicate(subjects.concepts["MONDO:0000100"], [])
    assert result.decision_path == "D7"
    assert result.settled
    assert result.adjudication.rule_ids == ["RJ-001"]


def test_a_flagged_subject_never_auto_settles(subjects):
    """A numbered subtype with a confident lexical hit is where matching is most wrong."""
    subject = subjects.concepts["MONDO:0011111"]
    result = adjudicate(subject, [_pool("ICD10CM:A00", 1)], subject_flags=["SC-006"])
    assert result.decision_path == "D5"
    assert not result.settled


def test_permitted_conflation_is_cited_on_the_row(subjects):
    """CM-002 is permitted at set level, and must still appear on the row that used it."""
    subject = subjects.concepts["MONDO:0002169"]
    result = adjudicate(subject, [_pool("ICD10CM:R48.1", 1, flags=["CM-002"])])
    assert result.settled
    assert result.adjudication.conflation == ["CM-002"]
    assert "CM-002" in result.adjudication.rule_ids or "CM-002" in result.adjudication.conflation
    assert "conflation" in result.adjudication.justification


# --- the viability floor: D8 --------------------------------------------------------------


def test_token_overlap_noise_settles_at_d8_rather_than_joining_the_queue(subjects):
    """``RJ-004``: sharing one word is a deterministic finding, not an expert question."""
    subject = subjects.concepts["MONDO:0011111"]
    noise = Candidate(
        "ICD10CM:A00", [Signal("token_retrieval", "EV-006", 0.2)], tier=9, score=0.2
    )
    result = adjudicate(subject, [noise], min_retrieval_score=0.3)
    assert result.decision_path == "D8"
    assert result.settled, "noise must not consume adjudication budget"
    assert "RJ-004" in result.adjudication.rule_ids
    assert result.adjudication.decision == "none"
    # The rejected candidate is still named, so the judgement is auditable.
    assert "ICD10CM:A00" in result.adjudication.alternatives


def test_a_viable_retrieval_candidate_still_reaches_adjudication(subjects):
    subject = subjects.concepts["MONDO:0011111"]
    viable = Candidate(
        "ICD10CM:A00", [Signal("token_retrieval", "EV-006", 0.6)], tier=9, score=0.6
    )
    result = adjudicate(subject, [viable], min_retrieval_score=0.3)
    assert result.decision_path == "D7"
    assert not result.settled
    assert result.review_candidates == [viable]


def test_the_floor_is_configurable_and_zero_keeps_everything(subjects):
    subject = subjects.concepts["MONDO:0011111"]
    noise = Candidate(
        "ICD10CM:A00", [Signal("token_retrieval", "EV-006", 0.05)], tier=9, score=0.05
    )
    assert adjudicate(subject, [noise], min_retrieval_score=0.0).decision_path == "D7"


# --- OBO parsing: the hierarchy has to survive the file's own syntax ----------------------


def test_is_a_trailing_modifiers_do_not_destroy_the_hierarchy():
    """Regression: `is_a: X {source="..."} ! label` must yield `X`, not `X {source=...}`.

    Stripping only the `!` comment left the brace block glued to the identifier, which then
    matched no concept and was dropped without a word. Mondo's release carries 36,637 such
    lines, so the loaded hierarchy was measurably thinner than the file — and descendant
    counts, the `SC-007` coextensive test and Boomer's hard constraints all read from it.
    """
    from mondo_ai.align.adapters.obo import parse_is_a

    assert parse_is_a('is_a: MONDO:0024290 {source="NCIT:C118172"} ! adenoma') \
        == "MONDO:0024290"
    assert parse_is_a("is_a: MONDO:0005015 ! diabetes") == "MONDO:0005015"
    assert parse_is_a("is_a: MONDO:0005015") == "MONDO:0005015"


def test_modifier_bearing_is_a_lines_are_linked(tmp_path):
    """The same bug, seen through the loader rather than the line parser."""
    from mondo_ai.align.adapters import load_obo

    obo = tmp_path / "t.obo"
    obo.write_text(
        "[Term]\nid: X:1\nname: parent\n\n"
        "[Term]\nid: X:2\nname: child\n"
        'is_a: X:1 {source="NCIT:C1"} ! parent\n'
    )
    rs = load_obo(obo, "X")
    assert rs.concepts["X:2"].parents == ["X:1"]
    assert rs.concepts["X:1"].children == ["X:2"]
    assert rs.concepts["X:1"].n_descendants == 1


# --- EV-007: a related synonym is not evidence of sameness -------------------------------


def test_related_synonym_hit_never_settles_as_exact():
    """EV-007 is "not sufficient alone"; a related-synonym string identity must be judged.

    Mondo's related synonyms include broader and narrower terms, so a hit through one is a
    candidate worth adjudicating and never a D3 exact match.
    """
    from mondo_ai.align.index import build_index
    from mondo_ai.align.models import Concept, ResourceSet

    objs = ResourceSet(name="s", prefix="SCTID", concepts={
        "SCTID:1": Concept(id="SCTID:1", label="Hereditary neuropathy")})
    gen = CandidateGenerator(build_index(list(objs.concepts.values())), objs)
    subject = Concept(id="MONDO:1", label="Charcot-Marie-Tooth disease type 1A",
                      other_synonyms=["hereditary neuropathy"])
    pool = gen.generate(subject)
    assert pool[0].object_id == "SCTID:1"
    assert pool[0].tier == 9
    assert "EV-007" in {s.rule_id for s in pool[0].signals}
    assert "EV-004" not in {s.rule_id for s in pool[0].signals}
    result = adjudicate(subject, pool)
    assert result.decision_path == "D7" and not result.settled


def test_signal_order_does_not_depend_on_the_hash_seed():
    """Two runs on the same inputs must write the same bytes.

    The index holds sets; iterating one leaks the process's string-hash seed into signal
    order, and from there into ``subject_preprocessing`` and ``curation_rule``. Found by
    diffing two full Mondo x SNOMED CT runs.
    """
    import os
    import subprocess
    import sys

    code = (
        "from mondo_ai.align.candidates import CandidateGenerator\n"
        "from mondo_ai.align.index import build_index\n"
        "from mondo_ai.align.models import Concept, ResourceSet\n"
        "o = Concept(id='S:1', label='Disease', exact_synonyms=['Disorder', 'Syndrome'])\n"
        "objs = ResourceSet(name='s', prefix='S', concepts={o.id: o})\n"
        "g = CandidateGenerator(build_index([o]), objs)\n"
        "c = g.generate(Concept(id='M:1', label='disease', exact_synonyms=['disorder']))[0]\n"
        "print([(s.rule_id, s.detail, s.transform) for s in c.signals])\n"
        # Retrieval: two targets whose three-term IDF masses are equal on paper and differ
        # only in the order they are summed, which is the tie that reordered a real row.
        "fill, n = [], 0\n"
        "for w, df in (('aa', 1), ('bb', 3), ('cc', 7), ('dd', 1), ('ee', 3), ('ff', 7)):\n"
        "    for _ in range(df):\n"
        "        n += 1\n"
        "        fill.append(Concept(id=f'F:{n:03d}', label=f'{w} filler{n}'))\n"
        "idx = build_index(fill + [Concept(id='T:1', label='aa bb cc'),\n"
        "                          Concept(id='T:2', label='dd ee ff')])\n"
        "print(idx.retrieve(frozenset({'aa', 'bb', 'cc', 'dd', 'ee', 'ff'}), limit=2))\n"
    )
    outputs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       check=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout
        for seed in ("1", "2", "3", "4", "5", "6", "7", "8")
    }
    assert len(outputs) == 1, outputs
