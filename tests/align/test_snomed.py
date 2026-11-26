"""Tests for the Mondo x SNOMED CT surface: OMOP extraction, OMOP seeds, SNOMED flags, and
the reconciliation stage that turns two seed mapping sets plus the discovery run into one
consolidated, justified mapping set.

The fixtures are real artefacts in the real formats — an OMOP vocabulary zip with
tab-delimited CSVs, an xlsx written with openpyxl, SSSOM files with a YAML header — because
the failure modes worth testing live in the parsing: a semantic tag left on a synonym, a
non-English description indexed as a synonym, an upgraded concept silently treated as current.

Each seed row in the fixture exists to exercise exactly one reconciliation outcome; the
mapping from case to outcome is the table in ``CASES`` below.
"""

from __future__ import annotations

import csv
import io
import textwrap
import zipfile
from pathlib import Path
from typing import Sequence

import pytest

from mondo_ai.align.adapters import load_tsv
from mondo_ai.align.flags import object_flags
from mondo_ai.align.models import Concept

ENGLISH = "4180186"
FRENCH = "4180190"

#: (concept_id, name, domain, class, standard, code, invalid_reason)
OMOP_CONCEPTS = [
    ("100", "Clinical finding", "Condition", "Clinical Finding", "S", "404684003", ""),
    ("101", "Disease", "Condition", "Disorder", "S", "64572001", ""),
    ("102", "Marfan's syndrome", "Condition", "Disorder", "S", "19346006", ""),
    ("103", "Cholera", "Condition", "Disorder", "S", "63650001", ""),
    ("104", "Cholera, old concept", "Condition", "Disorder", "", "99999001", "U"),
    ("105", "Osteoarthritis of left knee", "Condition", "Disorder", "S", "1000001", ""),
    ("106", "Adenoma", "Observation", "Morph Abnormality", "S", "32048006", ""),
    ("107", "Fever", "Condition", "Clinical Finding", "S", "386661006", ""),
    ("108", "Appendectomy", "Procedure", "Procedure", "S", "80146002", ""),
    ("109", "Typhoid fever", "Condition", "Disorder", "S", "4834000", ""),
    ("110", "Paratyphoid fever", "Condition", "Disorder", "S", "85904008", ""),
    ("111", "Brucellosis", "Condition", "Disorder", "S", "75702008", ""),
    ("112", "Brucella infection", "Condition", "Disorder", "S", "75702009", ""),
    ("113", "Scrub typhus", "Condition", "Disorder", "S", "240613006", ""),
    ("114", "Q fever", "Condition", "Disorder", "S", "186788009", ""),
    ("115", "Gone for good", "Condition", "Disorder", "", "99999002", "D"),
    ("900", "Cholera", "Condition", "Disorder", "S", "A00", ""),  # ICD10CM, other vocab
]

#: (concept_id, synonym, language)
OMOP_SYNONYMS = [
    ("102", "Marfan syndrome (disorder)", ENGLISH),
    ("102", "Marfan's syndrome", ENGLISH),
    ("102", "Syndrome de Marfan", FRENCH),
    ("109", "Typhoid fever (disorder)", ENGLISH),
    ("113", "Tsutsugamushi fever", ENGLISH),
]

#: (concept_id_1, concept_id_2, relationship_id, invalid_reason)
OMOP_RELATIONSHIPS = [
    ("101", "100", "Is a", ""),
    ("102", "101", "Is a", ""),
    ("103", "101", "Is a", ""),
    ("107", "100", "Is a", ""),
    ("109", "101", "Is a", ""),
    ("110", "101", "Is a", ""),
    ("105", "101", "Is a", ""),
    ("111", "101", "Is a", ""),
    ("112", "101", "Is a", ""),
    ("113", "101", "Is a", ""),
    ("114", "101", "Is a", ""),
    ("110", "109", "Is a", "D"),  # retired edge, must not become a parent
    ("104", "103", "Concept replaced by", ""),
    ("104", "101", "Concept was_a to", ""),  # weaker than replaced-by; must lose
]


def _tsv(header: list[str], rows: Sequence[Sequence[str]]) -> str:
    return "\n".join(["\t".join(header)] + ["\t".join(r) for r in rows]) + "\n"


def write_omop_zip(path: Path) -> Path:
    """Write a small OMOP vocabulary download with the real file and column names."""
    concept = _tsv(
        ["concept_id", "concept_name", "domain_id", "vocabulary_id", "concept_class_id",
         "standard_concept", "concept_code", "valid_start_date", "valid_end_date",
         "invalid_reason"],
        [(cid, name, dom, "ICD10CM" if code == "A00" else "SNOMED", cls, std, code,
          "20020131", "20991231", inv)
         for cid, name, dom, cls, std, code, inv in OMOP_CONCEPTS])
    synonym = _tsv(["concept_id", "concept_synonym_name", "language_concept_id"],
                   OMOP_SYNONYMS)
    rel = _tsv(["concept_id_1", "concept_id_2", "relationship_id", "valid_start_date",
                "valid_end_date", "invalid_reason"],
               [(a, b, r, "20020131", "20991231", inv)
                for a, b, r, inv in OMOP_RELATIONSHIPS])
    vocab = _tsv(["vocabulary_id", "vocabulary_name", "vocabulary_reference",
                  "vocabulary_version", "vocabulary_concept_id"],
                 [("SNOMED", "SNOMED CT", "x", "2026-02-01 SNOMED CT International Edition",
                   "44819097")])
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("CONCEPT.csv", concept)
        zf.writestr("CONCEPT_SYNONYM.csv", synonym)
        zf.writestr("CONCEPT_RELATIONSHIP.csv", rel)
        zf.writestr("VOCABULARY.csv", vocab)
    return path


SUBJECT_OBO = """\
format-version: 1.2
data-version: releases/2026-11-01

[Term]
id: MONDO:0000001
name: disease

[Term]
id: MONDO:0007947
name: Marfan syndrome
is_a: MONDO:0000001
xref: SCTID:19346006 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0000100
name: cholera
is_a: MONDO:0000001
xref: SCTID:99999001 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0005000
name: typhoid fever
is_a: MONDO:0000001
xref: SCTID:4834000 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0005001
name: paratyphoid fever
is_a: MONDO:0000001

[Term]
id: MONDO:0002000
name: osteoarthritis of knee
is_a: MONDO:0000001

[Term]
id: MONDO:0004000
name: adenoma
is_a: MONDO:0000001
xref: SCTID:32048006 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0003000
name: hyperpyrexia
is_a: MONDO:0000001

[Term]
id: MONDO:0009000
name: brucellosis
is_a: MONDO:0000001
xref: SCTID:75702008 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0010000
name: rickettsiosis tsutsugamushi
is_a: MONDO:0000001

[Term]
id: MONDO:0010001
name: scrub typhus
is_a: MONDO:0000001
xref: SCTID:240613006 {source="MONDO:equivalentTo"}

[Term]
id: MONDO:0011000
name: Q fever
is_a: MONDO:0000001

[Term]
id: MONDO:0012000
name: appendicitis-like thing
is_a: MONDO:0000001
"""

MONDO_SSSOM = """\
# curie_map:
#   MONDO: http://purl.obolibrary.org/obo/MONDO_
#   SCTID: http://snomed.info/id/
#   skos: http://www.w3.org/2004/02/skos/core#
#   semapv: https://w3id.org/semapv/vocab/
# mapping_set_id: http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv
subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification
MONDO:0007947\tMarfan syndrome\tskos:exactMatch\tSCTID:19346006\t\tsemapv:UnspecifiedMatching
MONDO:0000100\tcholera\tskos:exactMatch\tSCTID:99999001\t\tsemapv:UnspecifiedMatching
MONDO:0005000\ttyphoid fever\tskos:exactMatch\tSCTID:4834000\t\tsemapv:UnspecifiedMatching
MONDO:0004000\tadenoma\tskos:exactMatch\tSCTID:32048006\t\tsemapv:UnspecifiedMatching
MONDO:0009000\tbrucellosis\tskos:exactMatch\tSCTID:75702008\t\tsemapv:UnspecifiedMatching
MONDO:0010001\tscrub typhus\tskos:exactMatch\tSCTID:240613006\t\tsemapv:UnspecifiedMatching
MONDO:0007947\tMarfan syndrome\tskos:exactMatch\tDOID:14323\t\tsemapv:UnspecifiedMatching
"""

#: (Mondo local id, SNOMED code, vocabulary_id_2, predicate, confidence)
OMOP_SEEDS = [
    ("MONDO_0007947", "19346006", "SNOMED", "exactMatch", "1"),
    ("MONDO_0000100", "99999001", "SNOMED", "exactMatch", "0.98"),
    ("MONDO_0005000", "4834000", "SNOMED", "broadMatch", "0.9"),
    ("MONDO_0002000", "1000001", "SNOMED", "exactMatch", "0.95"),
    ("MONDO_0003000", "386661006", "SNOMED", "exactMatch", "0.85"),
    ("MONDO_0005001", "85904008", "SNOMED", "exactMatch", "1"),
    ("MONDO_0009000", "75702009", "SNOMED", "exactMatch", "0.96"),
    ("MONDO_0010000", "240613006", "SNOMED", "exactMatch", "0.97"),
    ("MONDO_0011000", "186788009", "SNOMED", "broadMatch", "0.91"),
    ("MONDO_0012000", "80146002", "SNOMED", "exactMatch", "0.81"),
    ("MONDO_0012000", "8000/0", "ICDO3", "exactMatch", "0.9"),
    ("MONDO_0007947", "64572001", "SNOMED", "", "0.9"),  # metadata without a predicate
]

#: Every seeded pair and what reconciliation must make of it: (status, review tier).
CASES = {
    ("MONDO:0007947", "SCTID:19346006"): ("corroborated", "AUTO_ACCEPT"),
    ("MONDO:0000100", "SCTID:99999001"): ("inactive_target", "REJECTED"),
    ("MONDO:0005000", "SCTID:4834000"): ("predicate_conflict", "EXPERT_REVIEW"),
    ("MONDO:0002000", "SCTID:1000001"): ("rule_conflict", "EXPERT_REVIEW"),
    ("MONDO:0004000", "SCTID:32048006"): ("rule_conflict", "EXPERT_REVIEW"),
    ("MONDO:0003000", "SCTID:386661006"): ("seed_only", "SPOT_CHECK"),
    ("MONDO:0005001", "SCTID:85904008"): ("corroborated", "AUTO_ACCEPT"),
    ("MONDO:0009000", "SCTID:75702008"): ("object_conflict", "EXPERT_REVIEW"),
    ("MONDO:0009000", "SCTID:75702009"): ("object_conflict", "EXPERT_REVIEW"),
    ("MONDO:0010000", "SCTID:240613006"): ("reverse_proxy_merge", "EXPERT_REVIEW"),
    ("MONDO:0010001", "SCTID:240613006"): ("reverse_proxy_merge", "EXPERT_REVIEW"),
    ("MONDO:0011000", "SCTID:186788009"): ("predicate_vs_lexical", "EXPERT_REVIEW"),
    ("MONDO:0012000", "SCTID:80146002"): ("unknown_target", "REJECTED"),
    ("MONDO:0007947", "SCTID:64572001"): ("seed_only", "SPOT_CHECK"),
}


def write_omop_seed_files(d: Path) -> tuple[Path, Path]:
    """Write the OMOP seed as it ships: an xlsx of relationships plus a metadata CSV."""
    from openpyxl import Workbook  # type: ignore[import-untyped]

    wb = Workbook()
    ws = wb.active
    ws.title = "concept_relationship_manual"
    ws.append(["concept_code_1", "concept_code_2", "vocabulary_id_1", "vocabulary_id_2",
               "relationship_id", "valid_start_date", "valid_end_date", "invalid_reason"])
    for s, o, vocab, _, _ in OMOP_SEEDS:
        # The real file stores numeric SNOMED codes as numbers, which read back as floats.
        code = float(o) if o.isdigit() else o
        ws.append([s, code, "Mondo", vocab, "Maps to", "2026-07-14", "2099-12-31", None])
    xlsx = d / "concept_relationship_manual.xlsx"
    wb.save(xlsx)

    meta = d / "concept_relationship_metadata_stage.csv"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["concept_code_1", "vocabulary_id_1", "relationship_id", "concept_code_2",
                "vocabulary_id_2", "confidence", "relationship_predicate_id",
                "mapping_source", "mapping_tool"])
    for s, o, vocab, pred, conf in OMOP_SEEDS:
        w.writerow([s, "Mondo", "Maps to", o, vocab, conf, pred,
                    "Machine-produced mappings with spot-curation", "AM-tool_U"])
    meta.write_text(buf.getvalue())
    return xlsx, meta


@pytest.fixture(scope="module")
def snomed_dir(tmp_path_factory) -> Path:
    """Every input file the SNOMED surface needs, written once."""
    from mondo_ai.align.omop import extract_snomed, omop_seed_sssom

    d = tmp_path_factory.mktemp("snomed")
    write_omop_zip(d / "vocab.zip")
    extract_snomed(d / "vocab.zip", d / "snomed.tsv")
    (d / "mondo.obo").write_text(textwrap.dedent(SUBJECT_OBO))
    (d / "mondo.sssom.tsv").write_text(MONDO_SSSOM)
    xlsx, meta = write_omop_seed_files(d)
    omop_seed_sssom(xlsx, meta, d / "omop.sssom.tsv")
    return d


@pytest.fixture(scope="module")
def snomed(snomed_dir: Path):
    return load_tsv(snomed_dir / "snomed.tsv", "SCTID", "snomed", obsolete_column="obsolete")


# --- OMOP extraction ---------------------------------------------------------------------


def test_extraction_keeps_disease_relevant_snomed_classes_only(snomed):
    assert "SCTID:19346006" in snomed.concepts  # Disorder
    assert "SCTID:386661006" in snomed.concepts  # Clinical Finding
    assert "SCTID:32048006" in snomed.concepts  # Morph Abnormality, flagged downstream
    assert "SCTID:80146002" not in snomed.concepts  # Procedure
    assert "SCTID:A00" not in snomed.concepts  # another vocabulary entirely


def test_extraction_strips_semantic_tags_and_keeps_english_only(snomed):
    marfan = snomed.concepts["SCTID:19346006"]
    assert marfan.label == "Marfan's syndrome"
    # The FSN loses its "(disorder)" tag; the synonym equal to the label is not repeated;
    # the French description is not a synonym of an English matching surface.
    assert marfan.exact_synonyms == ["Marfan syndrome"]
    assert marfan.extra["semantic_tag"] == "disorder"


def test_extraction_builds_hierarchy_from_valid_is_a_only(snomed):
    assert snomed.concepts["SCTID:85904008"].parents == ["SCTID:64572001"]
    assert "SCTID:85904008" in snomed.concepts["SCTID:64572001"].children


def test_inactive_concepts_are_kept_obsolete_with_their_strongest_replacement(snomed):
    old = snomed.concepts["SCTID:99999001"]
    assert old.obsolete
    assert old.extra["invalid_reason"] == "U"
    assert old.extra["replaced_by"] == "SCTID:63650001"
    assert old.extra["replacement_relation"] == "Concept replaced by"
    gone = snomed.concepts["SCTID:99999002"]
    assert gone.obsolete and gone.extra["replaced_by"] == ""
    assert "SCTID:99999001" not in {c.id for c in snomed.active()}


def test_extraction_records_the_snomed_version(snomed_dir):
    import json

    prov = json.loads((snomed_dir / "snomed.tsv.provenance.json").read_text())
    assert prov["vocabulary_version"].startswith("2026-02-01 SNOMED CT")
    assert len(prov["source_sha256"]) == 64
    assert prov["concepts"] == 15


def test_extraction_is_byte_for_byte_reproducible(snomed_dir, tmp_path):
    from mondo_ai.align.omop import extract_snomed

    extract_snomed(snomed_dir / "vocab.zip", tmp_path / "again.tsv")
    assert (tmp_path / "again.tsv").read_bytes() == (snomed_dir / "snomed.tsv").read_bytes()


# --- OMOP seeds --------------------------------------------------------------------------


def _sssom_rows(path: Path) -> list[dict[str, str]]:
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def test_omop_seed_becomes_sssom_with_the_metadata_predicate(snomed_dir):
    rows = _sssom_rows(snomed_dir / "omop.sssom.tsv")
    by_pair = {(r["subject_id"], r["object_id"]): r for r in rows}
    typhoid = by_pair[("MONDO:0005000", "SCTID:4834000")]
    assert typhoid["predicate_id"] == "skos:broadMatch"
    assert typhoid["confidence"] == "0.9"
    # AM-tool is an automapping approach code, not a tool name and not a curation claim.
    assert typhoid["mapping_justification"] == "semapv:UnspecifiedMatching"
    assert "AM-tool_U" in typhoid["comment"]


def test_omop_seed_drops_non_snomed_targets_and_says_so(snomed_dir):
    rows = _sssom_rows(snomed_dir / "omop.sssom.tsv")
    assert all(r["object_id"].startswith("SCTID:") for r in rows)
    assert len(rows) == 11
    header = (snomed_dir / "omop.sssom.tsv").read_text()
    assert "ICDO3: 1" in header


def test_omop_maps_to_without_a_predicate_is_read_as_the_weaker_broad_claim(snomed_dir):
    rows = _sssom_rows(snomed_dir / "omop.sssom.tsv")
    row = next(r for r in rows if r["object_id"] == "SCTID:64572001")
    assert row["predicate_id"] == "skos:broadMatch"
    assert "no relationship_predicate_id" in row["comment"]
    assert "without a predicate: 1" in (snomed_dir / "omop.sssom.tsv").read_text()


def test_omop_seed_refuses_an_xlsx_and_metadata_that_disagree(snomed_dir, tmp_path):
    from mondo_ai.align.omop import omop_seed_sssom

    xlsx, meta = write_omop_seed_files(tmp_path)
    text = meta.read_text().splitlines()
    meta.write_text("\n".join(text[:-1]) + "\n")  # one relationship has no metadata
    with pytest.raises(ValueError, match="no metadata"):
        omop_seed_sssom(xlsx, meta, tmp_path / "out.sssom.tsv")


# --- SNOMED flags ------------------------------------------------------------------------


def test_snomed_clinical_finding_is_a_declared_conflation(snomed):
    assert object_flags(snomed.concepts["SCTID:386661006"], "SCTID") == ["CM-004"]


def test_snomed_morphology_is_not_a_disease(snomed):
    assert "RJ-005" in object_flags(snomed.concepts["SCTID:32048006"], "SCTID")


def test_snomed_laterality_is_a_finer_variant(snomed):
    assert "SC-005" in object_flags(snomed.concepts["SCTID:1000001"], "SCTID")


def test_snomed_disorder_carries_no_flag(snomed):
    assert object_flags(snomed.concepts["SCTID:19346006"], "SCTID") == []


def test_snomed_class_flags_do_not_leak_into_other_vocabularies():
    c = Concept(id="ICD10CM:A00", label="Cholera", extra={"concept_class_id": "Morph Abnormality"})
    assert object_flags(c, "ICD10CM") == []


# --- reconciliation ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def snomed_config(snomed_dir: Path, tmp_path_factory):
    from mondo_ai.align.config import AlignmentConfig

    out = tmp_path_factory.mktemp("snomed-out")
    return AlignmentConfig.model_validate({
        "run_id": "mondo-snomed-test",
        "subject": {"name": "mondo", "prefix": "MONDO", "format": "obo",
                    "path": str(snomed_dir / "mondo.obo")},
        "object": {"name": "snomed", "prefix": "SCTID", "format": "tsv",
                   "path": str(snomed_dir / "snomed.tsv"), "iri_base": "http://snomed.info/id/",
                   "options": {"obsolete_column": "obsolete"}},
        "conflations_permitted": ["CM-001", "CM-002", "CM-004"],
        "agent": {"enabled": False},
        "boomer": {"enabled": False},
        "reconcile": {
            "enabled": True,
            "seeds": [
                {"name": "mondo", "path": str(snomed_dir / "mondo.sssom.tsv"),
                 "mapping_source": "http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv",
                 "default_confidence": 0.95},
                {"name": "omop", "path": str(snomed_dir / "omop.sssom.tsv"),
                 "mapping_source": "https://w3id.org/mondo-ai/align/seed/omop-mondo-snomed"},
            ],
        },
        "output_root": str(out),
    })


@pytest.fixture(scope="module")
def snomed_run(snomed_config):
    from mondo_ai.align.pipeline import AlignmentRunner

    return AlignmentRunner(snomed_config, log=lambda _: None).run()


@pytest.fixture(scope="module")
def consolidated(snomed_run) -> dict[tuple[str, str], dict[str, str]]:
    rows = _sssom_rows(snomed_run.paths["consolidated"])
    return {(r["subject_id"], r["object_id"]): r for r in rows}


@pytest.mark.parametrize("pair", sorted(CASES))
def test_every_seed_pair_gets_its_reconciliation_outcome(consolidated, pair):
    status, tier = CASES[pair]
    row = consolidated[pair]
    assert (row["reconciliation_status"], row["review_tier"]) == (status, tier), row["comment"]


def test_seeds_are_held_out_of_discovery(snomed_run):
    # Marfan's discovery verdict rests on the lexical ladder alone; no seed signal leaked in.
    rec = next(r for r in snomed_run.records if r.subject_id == "MONDO:0007947")
    kinds = {s.kind for c in rec.candidates for s in c.signals}
    assert "proxy" not in kinds


def test_a_discovery_without_a_seed_is_a_lexical_row(consolidated):
    # cholera's seeds both point at an upgraded concept; the ladder found the live one.
    row = consolidated[("MONDO:0000100", "SCTID:63650001")]
    assert row["reconciliation_status"] == "discovery_only"
    assert row["mapping_justification"] == "semapv:LexicalMatching"
    assert row["evidence_sources"] == "lexical"


def test_corroborated_rows_carry_lexmatch_style_justification(consolidated):
    row = consolidated[("MONDO:0007947", "SCTID:19346006")]
    assert row["predicate_id"] == "skos:exactMatch"
    assert row["mapping_justification"] == "semapv:CompositeMatching"
    assert row["evidence_sources"] == "lexical|mondo|omop"
    assert row["match_string"] == "Marfan syndrome"
    assert row["subject_match_field"] == "rdfs:label"
    assert row["object_match_field"] == "oio:hasExactSynonym"
    assert row["seed_predicates"] == "mondo=skos:exactMatch|omop=skos:exactMatch"
    assert "maprule:EV-003" in row["curation_rule"]


def test_inactive_target_names_its_replacement(consolidated):
    row = consolidated[("MONDO:0000100", "SCTID:99999001")]
    assert row["object_status"] == "U"
    assert row["replacement_object"] == "SCTID:63650001"
    assert "maprule:ST-004" in row["curation_rule"]


def test_rule_conflicts_cite_the_rule(consolidated):
    assert "maprule:SC-005" in consolidated[("MONDO:0002000", "SCTID:1000001")]["curation_rule"]
    assert "maprule:RJ-005" in consolidated[("MONDO:0004000", "SCTID:32048006")]["curation_rule"]


def test_structural_conflicts_cite_st_001_and_st_002(consolidated):
    assert "ST-001" in consolidated[("MONDO:0009000", "SCTID:75702009")]["structural_flags"]
    assert "ST-002" in consolidated[("MONDO:0010000", "SCTID:240613006")]["structural_flags"]


def test_seed_only_row_records_the_permitted_conflation(consolidated):
    row = consolidated[("MONDO:0003000", "SCTID:386661006")]
    assert "CM-004" in row["risk_flags"]
    assert row["mapping_justification"] == "semapv:UnspecifiedMatching"


def test_seed_rows_to_other_vocabularies_are_ignored(consolidated):
    assert not any(o.startswith("DOID:") for _, o in consolidated)


def test_evidence_ledger_has_one_row_per_pair_and_source(snomed_run):
    rows = _sssom_rows(snomed_run.paths["evidence"])
    marfan = [r for r in rows
              if (r["subject_id"], r["object_id"]) == ("MONDO:0007947", "SCTID:19346006")]
    assert sorted(r["evidence_source"] for r in marfan) == ["lexical", "mondo", "omop"]
    lexical = next(r for r in marfan if r["evidence_source"] == "lexical")
    assert lexical["mapping_justification"] == "semapv:LexicalMatching"
    omop = next(r for r in marfan if r["evidence_source"] == "omop")
    assert omop["mapping_source"] == "https://w3id.org/mondo-ai/align/seed/omop-mondo-snomed"


def test_consolidated_set_round_trips_through_sssom_py(snomed_run):
    from sssom.parsers import parse_sssom_table

    for role in ("consolidated", "evidence"):
        msdf = parse_sssom_table(snomed_run.paths[role])
        assert len(msdf.df) == len(_sssom_rows(snomed_run.paths[role])), role


def test_reconciliation_is_reported(snomed_run):
    text = snomed_run.paths["report"].read_text()
    assert "## Reconciliation with seed mapping sets" in text
    assert "predicate_conflict" in text


def test_a_rerun_reproduces_the_consolidated_set_exactly(snomed_config, snomed_run, tmp_path):
    from mondo_ai.align.pipeline import AlignmentRunner

    again = snomed_config.model_copy(update={"output_root": tmp_path})
    result = AlignmentRunner(again, log=lambda _: None).run()
    for role in ("consolidated", "evidence", "sssom"):
        assert result.paths[role].read_bytes() == snomed_run.paths[role].read_bytes(), role


# --- evaluation against a gold standard that points at retired concepts --------------------


def test_a_gold_xref_to_a_retired_concept_is_not_scored():
    """Mondo xrefs SNOMED concepts that SNOMED has since retired.

    Scoring a prediction of the live successor as a false positive measures Mondo's
    staleness, not the pipeline; such subjects are reported as ``gold_target_retired``.
    """
    from mondo_ai.align.evaluate import evaluate
    from mondo_ai.align.models import Adjudication, MappingRecord, ResourceSet, ReviewTier

    objects = ResourceSet(name="s", prefix="SCTID", concepts={
        "SCTID:1": Concept(id="SCTID:1", label="Microcephalus", obsolete=True),
        "SCTID:2": Concept(id="SCTID:2", label="Microcephaly"),
        "SCTID:3": Concept(id="SCTID:3", label="Cholera"),
    })
    records = [
        MappingRecord("M:1", final=Adjudication("deterministic", "exact", "SCTID:2",
                                                decision_path="D2"),
                      review_tier=ReviewTier.AUTO_ACCEPT),
        MappingRecord("M:2", final=Adjudication("deterministic", "exact", "SCTID:3",
                                                decision_path="D1"),
                      review_tier=ReviewTier.AUTO_ACCEPT),
    ]
    gold = {"M:1": {"SCTID:1"}, "M:2": {"SCTID:3"}}
    asserted = {"M:1": {"SCTID:1": "skos:exactMatch"}, "M:2": {"SCTID:3": "skos:exactMatch"}}
    ev = evaluate(records, gold, asserted, objects=objects)
    assert (ev.overall.true_positive, ev.overall.false_positive) == (1, 0)
    assert ev.gold_retired == 1
    assert [d["kind"] for d in ev.disagreements] == ["gold_target_retired"]
