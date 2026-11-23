"""Tests for building SIEVE profile packets from source snapshots.

The fixtures are tiny synthetic snapshots of Mondo and three sources, normalized through a
tiny SSSOM file. Every packet must pass sieve's own profile checks with no ERROR, and its
score must be the one sieve's reference scorer computes.
"""

from pathlib import Path

import pytest
from sieve.inclusion_score import score_packet
from sieve.profile_checks import check_packet

from mondo_ai.config import get_default_config
from mondo_ai.datamodel import EntityMapping, EntitySnapshot, ResourceSnapshot, Synonym, SynonymScope
from mondo_ai.mapping.sssom_reader import SSSOMReader
from mondo_ai.packets.builder import PacketBuilder, Source, packet_id

DATE = "2026-11-20"

SSSOM_ROWS = [
    ("MONDO:1", "cystic fibrosis", "skos:exactMatch", "OMIM:219700"),
    ("MONDO:1", "cystic fibrosis", "skos:exactMatch", "Orphanet:586"),
    ("MONDO:1", "cystic fibrosis", "skos:exactMatch", "DOID:1485"),
    ("MONDO:1", "cystic fibrosis", "skos:exactMatch", "ICD10CM:E84"),
    ("MONDO:2", "lung disease", "skos:exactMatch", "DOID:850"),
    ("MONDO:2", "lung disease", "skos:exactMatch", "Orphanet:100"),
    ("MONDO:3", "disease", "skos:exactMatch", "DOID:4"),
    ("MONDO:4", "old thing", "skos:exactMatch", "DOID:9"),
]


def _sssom(tmp_path: Path) -> SSSOMReader:
    lines = ["# mapping_set_id: http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv"]
    lines.append("subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification")
    lines += ["\t".join([*row, "", "semapv:ManualMappingCuration"]) for row in SSSOM_ROWS]
    path = tmp_path / "mondo.sssom.tsv"
    path.write_text("\n".join(lines) + "\n")
    return SSSOMReader(path)


def _snap(rid: str, version: str, entities: list[EntitySnapshot]) -> ResourceSnapshot:
    return ResourceSnapshot(resource_id=rid, version=version, entity_count=len(entities), entities=entities)


def _syn(value: str, scope: SynonymScope = SynonymScope.EXACT, **kw) -> Synonym:
    return Synonym(value=value, scope=scope, **kw)


MONDO = _snap(
    "mondo",
    "http://purl.obolibrary.org/obo/mondo/releases/2026-11-03/mondo.owl",
    [
        EntitySnapshot(id="MONDO:1", label="cystic fibrosis", synonyms=[_syn("mucoviscidosis")], parents=["MONDO:2"]),
        EntitySnapshot(id="MONDO:2", label="lung disease", parents=["MONDO:3"]),
        EntitySnapshot(id="MONDO:3", label="disease"),
        EntitySnapshot(id="MONDO:4", label="old thing"),
    ],
)

OMIM = _snap(
    "omim",
    "http://purl.obolibrary.org/obo/mondo/releases/2026-11-18/omim.owl",
    [
        EntitySnapshot(
            id="OMIM:219700",
            label="cystic fibrosis",
            synonyms=[_syn("CF", type="OMO:0003000"), _syn("Mucoviscidosis"), _syn("cystic fibrosis")],
            mappings=[EntityMapping(object_id="Orphanet:586", predicate_id="skos:exactMatch")],
        ),
    ],
)

ORDO = _snap(
    "ordo",
    "https://www.orphadata.com/data/ontologies/ordo/last_version/ORDO_en_4.9.owl",
    [
        EntitySnapshot(
            id="Orphanet:586",
            label="Cystic fibrosis",
            synonyms=[_syn("CF")],
            classification_parents=["Orphanet:100"],
            mappings=[
                EntityMapping(object_id="OMIM:219700", predicate_id="oboInOwl:hasDbXref", mapping_source="E"),
                EntityMapping(object_id="ICD10CM:E84", predicate_id="oboInOwl:hasDbXref", mapping_source="NTBT"),
                EntityMapping(object_id="MONDO:1", predicate_id="oboInOwl:hasDbXref", mapping_source="E"),
            ],
        ),
        EntitySnapshot(id="Orphanet:100", label="Rare lung disease"),
    ],
)

DOID = _snap(
    "doid",
    "http://purl.obolibrary.org/obo/doid/releases/2026-10-29/doid.owl",
    [
        EntitySnapshot(
            id="DOID:1485",
            label="cystic fibrosis",
            synonyms=[_syn("CF"), _syn("pancreatic fibrosis", SynonymScope.RELATED, pmids=["123"])],
            parents=["DOID:850"],
            mappings=[
                EntityMapping(object_id="OMIM:219700", predicate_id="oboInOwl:hasDbXref"),
                EntityMapping(object_id="ICD10CM:E84.0", predicate_id="oboInOwl:hasDbXref"),
            ],
        ),
        EntitySnapshot(id="DOID:850", label="lung disease", parents=["DOID:4"]),
        EntitySnapshot(id="DOID:4", label="disease"),
        EntitySnapshot(id="DOID:9", label="old thing", deprecated=True, synonyms=[_syn("ancient name")]),
    ],
)

ICD10CM = _snap(
    "icd10cm",
    "http://purl.obolibrary.org/obo/mondo/sources/2026-01-09/icd10cm.owl",
    [
        EntitySnapshot(id="ICD10CM:E84", label="Cystic fibrosis"),
        EntitySnapshot(id="ICD10CM:E84.0", label="Cystic fibrosis with pulmonary manifestations", parents=["ICD10CM:E84"]),
    ],
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    config = get_default_config()
    resources = {r.id: r for r in config.resources}
    sources = [Source(resources[s.resource_id], s) for s in (OMIM, ORDO, DOID, ICD10CM)]
    builder = PacketBuilder(
        sources=sources,
        mondo=MONDO,
        sssom=_sssom(tmp_path_factory.mktemp("sssom")),
        config=config,
        date=DATE,
    )
    raw, merged = builder.build(["synonyms", "subclass", "mappings"])
    return {"raw": raw, "merged": {p["statement"]["id"]: p for p in merged}, "merged_list": merged}


def _find(built, claim_type: str, subject: str, predicate: str, obj: str) -> dict:
    for p in built["merged_list"]:
        st = p["statement"]
        if (st["type"], st["subject"], st["predicate"]["code"], st["object"]) == (claim_type, subject, predicate, obj):
            return p
    raise AssertionError(f"no packet {claim_type} {subject} {predicate} {obj}")


def _line_sources(packet: dict) -> set[str]:
    return {line["independence_cluster"] for line in packet["statement"]["has_evidence_lines"]}


# --- profile conformance ---------------------------------------------------------------


def test_every_packet_passes_the_profile_checks(built):
    for packet in [*built["raw"], *built["merged_list"]]:
        errors = [i for i in check_packet(packet) if i.severity == "ERROR"]
        assert errors == [], (packet["id"], errors)


def test_statement_score_is_sieves_inclusion_score(built):
    for packet in built["merged_list"]:
        breakdown = score_packet(packet)
        st = packet["statement"]
        assert st["score"] == pytest.approx(breakdown.s, abs=1e-4)
        assert st["strength"] == breakdown.band
        assert st["direction"] == ("supports" if breakdown.s > 0 else "disputes")


@pytest.mark.parametrize(
    "claim",
    [
        ("SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF"),
        ("SubclassStatement", "MONDO:1", "rdfs:subClassOf", "MONDO:2"),
        ("MappingStatement", "MONDO:1", "skos:exactMatch", "OMIM:219700"),
        ("MappingStatement", "MONDO:4", "skos:exactMatch", "DOID:9"),
    ],
)
def test_packet_validates_against_the_linkml_schema(built, claim):
    from mondo_ai.packets.validate import schema_errors

    assert schema_errors(_find(built, *claim)) == []


def test_schema_check_rejects_a_broken_packet(built):
    import copy

    from mondo_ai.packets.validate import schema_errors

    packet = copy.deepcopy(_find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF"))
    packet["reference_snapshot"]["resources"][0]["default_role"] = "BOGUS"
    packet["version"] = ["not", "a", "string"]
    assert len(schema_errors(packet)) >= 2


def test_packet_ids_are_deterministic():
    assert packet_id("SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF") == packet_id(
        "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "cf"
    )
    assert packet_id("SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF").startswith("mondoai:pkt/")


# --- synonyms --------------------------------------------------------------------------


def test_synonym_merged_from_three_sources(built):
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    assert _line_sources(packet) >= {"infores:orphanet", "infores:omim"}
    assert len(packet["statement"]["has_evidence_lines"]) == 3
    assert packet["statement"]["review_state"] == "PENDING_REVIEW"
    assert packet["statement"]["proposing_channel"] == "SOURCE_INGEST"


def test_source_id_resolution_is_a_normalization_step(built):
    """A source id resolved to a Mondo id is id-to-id normalisation, not grounding (string to id)."""
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    line = next(li for li in packet["statement"]["has_evidence_lines"] if "omim" in li["id"])
    (chain,) = line["has_evidence_items"][0]["resolution_chains"]
    (step,) = chain["steps"]
    assert step["category"] == "NORMALIZATION"
    assert step["quality_category"] == "normalization_curated"
    assert chain["specified_by"] == ["mondoai:method/sssom-normalization"]


def test_synonym_line_holds_a_normalized_prior_claim(built):
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    line = next(li for li in packet["statement"]["has_evidence_lines"] if "omim" in li["id"])
    (claim,) = line["has_evidence_items"]
    assert claim["type"] == "SynonymStatement"
    assert (claim["subject"], claim["object"]) == ("OMIM:219700", "CF")
    assert claim["reported_in"][0]["id"] == "OMIM:219700"
    assert claim["reported_in"][0]["record_metadata"]["source_label"] == "cystic fibrosis"
    assert claim["generation_context"] == [{"code": "OMO:0003000", "label": "synonym type"}]
    (chain,) = claim["resolution_chains"]
    assert chain["entity_slot"] == "subject"
    assert (chain["steps"][0]["input_value"], chain["steps"][0]["output_value"]) == ("OMIM:219700", "MONDO:1")
    assert line["contributions"][0]["method_type"] == "RULE_BASED_STRENGTH"
    assert line["score_of_evidence_provided"] == 0.95


def test_doid_line_clusters_with_omim_when_both_carry_the_name(built):
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    doid = next(li for li in packet["statement"]["has_evidence_lines"] if "doid" in li["id"])
    assert doid["independence_cluster"] == "infores:omim"


def test_raw_store_has_one_packet_per_claim_and_source(built):
    cf = [p for p in built["raw"] if p["statement"]["object"] == "CF"]
    assert len(cf) == 3
    assert all(len(p["statement"]["has_evidence_lines"]) == 1 for p in cf)
    assert len({p["id"] for p in cf}) == 3


def test_synonym_already_in_mondo_is_not_verified_and_case_variants_merge(built):
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "mucoviscidosis")
    assert packet["statement"]["review_state"] == "NOT_VERIFIED"
    assert {li["has_evidence_items"][0]["object"] for li in packet["statement"]["has_evidence_lines"]} == {
        "Mucoviscidosis"
    }


def test_synonym_equal_to_mondo_label_is_skipped(built):
    with pytest.raises(AssertionError):
        _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "cystic fibrosis")


def test_deprecated_source_entity_gives_no_synonym(built):
    assert not [p for p in built["merged_list"] if p["statement"]["object"] == "ancient name"]


def test_pmid_becomes_publication_in_reported_in(built):
    packet = _find(built, "SynonymStatement", "MONDO:1", "oboInOwl:hasRelatedSynonym", "pancreatic fibrosis")
    claim = packet["statement"]["has_evidence_lines"][0]["has_evidence_items"][0]
    assert [r["id"] for r in claim["reported_in"]] == ["DOID:1485", "PMID:123"]


# --- subclass --------------------------------------------------------------------------


def test_subclass_from_doid_is_in_mondo(built):
    packet = _find(built, "SubclassStatement", "MONDO:1", "rdfs:subClassOf", "MONDO:2")
    assert packet["statement"]["review_state"] == "NOT_VERIFIED"
    claims = [li["has_evidence_items"][0] for li in packet["statement"]["has_evidence_lines"]]
    by_subject = {c["subject"]: c for c in claims}
    assert set(by_subject) == {"DOID:1485", "Orphanet:586"}
    assert {g["entity_slot"] for g in by_subject["DOID:1485"]["resolution_chains"]} == {"subject", "object"}
    assert by_subject["Orphanet:586"]["generation_context"][0]["code"] == "BFO:0000050"


def test_entailed_subclass_is_not_verified(built):
    packet = _find(built, "SubclassStatement", "MONDO:2", "rdfs:subClassOf", "MONDO:3")
    assert packet["statement"]["review_state"] == "NOT_VERIFIED"


def test_subclass_needs_both_ends_normalized(built):
    assert not [
        p for p in built["merged_list"] if p["statement"]["type"] == "SubclassStatement" and "E84" in p["statement"]["object"]
    ]


# --- mappings --------------------------------------------------------------------------


def test_mondo_mapping_gets_lexical_and_cross_source_lines(built):
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "OMIM:219700")
    st = packet["statement"]
    assert st["review_state"] == "NOT_VERIFIED"
    lines = {li["id"].rsplit("/", 1)[-1].split("-", 1)[-1]: li for li in st["has_evidence_lines"]}
    assert lines["lexical"]["has_evidence_items"][1]["type"] == "ComputationalScore"
    assert lines["lexical"]["has_evidence_items"][1]["value"] == "1.0"
    # ORDO (E) and DOID (untyped xref) both point Orphanet:586 / DOID:1485 at OMIM:219700
    assert lines["ordo"]["direction_of_evidence_provided"] == "supports"
    assert lines["doid"]["direction_of_evidence_provided"] == "supports"
    assert lines["ordo"]["has_evidence_items"][0]["generation_context"] == [{"code": "ECC:E", "label": "ORDO mapping precision"}]
    assert st["score"] > 0


def test_ordo_narrower_xref_disputes_an_exact_mapping(built):
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "ICD10CM:E84")
    ordo = next(li for li in packet["statement"]["has_evidence_lines"] if li["id"].endswith("-ordo"))
    assert ordo["direction_of_evidence_provided"] == "disputes"
    assert ordo["has_evidence_items"][0]["predicate"]["code"] == "skos:broadMatch"


def test_reverse_mondo_xref_from_ordo_supports(built):
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "Orphanet:586")
    ordo = next(li for li in packet["statement"]["has_evidence_lines"] if li["id"].endswith("-ordo"))
    claim = ordo["has_evidence_items"][0]
    assert (claim["subject"], claim["object"]) == ("Orphanet:586", "MONDO:1")
    assert ordo["direction_of_evidence_provided"] == "supports"


def test_obsolete_object_disputes_the_mapping(built):
    packet = _find(built, "MappingStatement", "MONDO:4", "skos:exactMatch", "DOID:9")
    (line,) = [li for li in packet["statement"]["has_evidence_lines"] if li["id"].endswith("-obsolete")]
    assert line["direction_of_evidence_provided"] == "disputes"
    assert line["has_evidence_items"][0]["obsolete_in_source"] is True
    assert packet["statement"]["object_obsolete_in_source_version"].endswith("doid.owl")
    assert packet["statement"]["score"] < 0


def test_candidate_mapping_from_a_source_xref(built):
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "ICD10CM:E84.0")
    assert packet["statement"]["review_state"] == "PENDING_REVIEW"
    assert _line_sources(packet) == {"infores:disease-ontology"}


def test_reference_snapshot_holds_only_what_the_packet_uses(built):
    packet = _find(built, "MappingStatement", "MONDO:1", "skos:exactMatch", "ICD10CM:E84.0")
    snap = packet["reference_snapshot"]
    assert [r["id"] for r in snap["aggregation_rules"]] == ["mondoai:rule/mapping-v1.0"]
    # DOID derives content from OMIM, so OMIM comes along with it
    assert {r["id"] for r in snap["resources"]} == {"infores:disease-ontology", "infores:omim"}
