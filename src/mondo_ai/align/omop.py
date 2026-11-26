"""SNOMED CT and OMOP seed mappings, from the OHDSI Standardized Vocabularies download.

Two deterministic conversions, both run as ``just`` recipes rather than implicitly inside a
run, because their inputs are licensed downloads that the pipeline must never fetch on its
own and their outputs are what ``run.yaml`` checksums.

:func:`extract_snomed` reduces an Athena vocabulary zip (CONCEPT, CONCEPT_SYNONYM,
CONCEPT_RELATIONSHIP, VOCABULARY — five gigabytes uncompressed) to one TSV of the SNOMED
concepts a disease alignment can use, in the shape :func:`~mondo_ai.align.adapters.load_tsv`
reads. The judgement calls, each of which a reviewer should be able to find in one place:

* **Classes.** ``Disorder`` and ``Clinical Finding`` are the targets. ``Morph Abnormality``
  is kept too, but only so that seed mappings into it resolve and can be flagged ``RJ-005``:
  Mondo's neoplasm terms are sometimes cross-referenced to the morphology rather than the
  disorder, and a morphology is not a condition anyone can code. Everything else —
  procedures, situations, substances — is dropped.
* **Inactive concepts are kept**, marked obsolete, with their strongest historical
  replacement. Seeds point at 393 of them; dropping them would make those rows look like
  invented identifiers (``ST-003``) when the truth is that SNOMED moved on (``ST-004``).
* **Synonyms are English descriptions**, with the FSN's semantic tag (``(disorder)``)
  stripped and the tag kept as ``semantic_tag``. SNOMED descriptions of one concept are
  synonymous by construction, so they are loaded as *exact* synonyms — the one place this
  pipeline trusts a source's synonymy without a scope qualifier, and it is recorded in the
  provenance sidecar so the choice is visible.
* **Hierarchy** is the valid ``Is a`` rows between kept concepts. Retired edges are not
  parents.

:func:`omop_seed_sssom` turns OMOP's Mondo→SNOMED ``Maps to`` rows (an xlsx) and their
metadata (a CSV with the SSSOM-ish predicate and confidence) into an SSSOM file. The two must
describe the same relationships; a relationship without metadata is an error, not a default.

Example:
    >>> strip_semantic_tag("Marfan syndrome (disorder)")
    ('Marfan syndrome', 'disorder')
    >>> strip_semantic_tag("Hepatitis (viral)")
    ('Hepatitis (viral)', '')
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

from mondo_ai.align.provenance import sha256_file

#: SNOMED CT concept classes kept by :func:`extract_snomed`, as OMOP names them.
DEFAULT_CLASSES = ("Disorder", "Clinical Finding", "Morph Abnormality")

#: OMOP's concept id for English.
ENGLISH = "4180186"

#: OMOP relationships from an inactive concept to its successor, strongest first. "Replaced
#: by" and "same as" are SNOMED's own historical associations for a like-for-like successor;
#: "possibly equivalent", "was a" and "alternative" are weaker and only used when nothing
#: stronger exists. The relation used is recorded beside the replacement.
REPLACEMENT_RELATIONS = (
    "Concept replaced by",
    "Concept same_as to",
    "Concept poss_eq to",
    "Concept was_a to",
    "Concept alt_to to",
)

#: SNOMED semantic tags, as they close a fully specified name. Only these are stripped: a
#: trailing parenthesis in a clinical name ("Hepatitis (viral)") is part of the name.
SEMANTIC_TAGS = frozenset({
    "disorder", "finding", "morphologic abnormality", "situation", "event", "procedure",
    "observable entity", "body structure", "qualifier value", "regime/therapy",
    "clinical finding", "disease",
})

_TAG_RE = re.compile(r"\s*\(([a-z/ -]+)\)$")

TSV_COLUMNS = [
    "id", "label", "exact_synonyms", "parents", "semantic_tag", "concept_class_id",
    "domain_id", "standard_concept", "invalid_reason", "obsolete", "replaced_by",
    "replacement_relation", "omop_concept_id",
]


def strip_semantic_tag(name: str) -> tuple[str, str]:
    """Split a SNOMED description into its text and its semantic tag, if it has one.

    Example:
        >>> strip_semantic_tag("Adenoma, no subtype (morphologic abnormality)")
        ('Adenoma, no subtype', 'morphologic abnormality')
        >>> strip_semantic_tag("Cholera")
        ('Cholera', '')
    """
    m = _TAG_RE.search(name)
    if m and m.group(1) in SEMANTIC_TAGS:
        return name[: m.start()].rstrip(), m.group(1)
    return name, ""


def _rows(zf: zipfile.ZipFile, member: str) -> Iterator[list[str]]:
    """Yield the data rows of one OMOP table as split fields.

    OMOP tables are tab-separated with no quoting at all — concept names contain bare double
    quotes — so a CSV reader would mis-parse them. A plain split is the correct reader.
    """
    with zf.open(member) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", newline="")
        next(text)
        for line in text:
            yield line.rstrip("\r\n").split("\t")


def _header(zf: zipfile.ZipFile, member: str) -> list[str]:
    with zf.open(member) as fh:
        return io.TextIOWrapper(fh, encoding="utf-8").readline().rstrip("\r\n").split("\t")


def _clean(value: str) -> str:
    """Make a string safe for a pipe-joined TSV cell.

    Example:
        >>> _clean("a | b\\tc")
        'a / b c'
    """
    return " ".join(value.replace("|", "/").split())


def extract_snomed(
    zip_path: str | Path,
    out_path: str | Path,
    *,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
) -> dict[str, object]:
    """Extract the SNOMED concepts a disease alignment needs from an OMOP vocabulary zip.

    Writes ``out_path`` (TSV, one concept per row, sorted by id) and
    ``<out_path>.provenance.json``. Output is byte-for-byte reproducible from the same zip.

    Args:
        zip_path: An Athena download.
        out_path: The TSV to write.
        classes: OMOP ``concept_class_id`` values to keep.

    Returns:
        The provenance record that was written.
    """
    zip_path, out_path = Path(zip_path), Path(out_path)
    zf = zipfile.ZipFile(zip_path)
    keep = set(classes)

    cols = {c: i for i, c in enumerate(_header(zf, "CONCEPT.csv"))}
    i_id, i_name, i_vocab = cols["concept_id"], cols["concept_name"], cols["vocabulary_id"]
    i_class, i_code = cols["concept_class_id"], cols["concept_code"]
    i_dom, i_std, i_inv = cols["domain_id"], cols["standard_concept"], cols["invalid_reason"]

    code_of: dict[str, str] = {}  # every SNOMED concept, for replacement targets
    kept: dict[str, dict[str, str]] = {}
    for f in _rows(zf, "CONCEPT.csv"):
        if f[i_vocab] != "SNOMED":
            continue
        code_of[f[i_id]] = f[i_code]
        if f[i_class] not in keep:
            continue
        kept[f[i_id]] = {
            "label": _clean(f[i_name]), "concept_class_id": f[i_class],
            "domain_id": f[i_dom], "standard_concept": f[i_std],
            "invalid_reason": f[i_inv], "code": f[i_code],
        }

    synonyms: dict[str, set[str]] = defaultdict(set)
    tags: dict[str, str] = {}
    for f in _rows(zf, "CONCEPT_SYNONYM.csv"):
        cid, name, lang = f[0], f[1], f[2]
        if cid not in kept or lang != ENGLISH:
            continue
        text, tag = strip_semantic_tag(_clean(name))
        if tag:
            tags.setdefault(cid, tag)
        if text:
            synonyms[cid].add(text)

    parents: dict[str, set[str]] = defaultdict(set)
    replacement: dict[str, tuple[int, str, str]] = {}
    rank = {r: n for n, r in enumerate(REPLACEMENT_RELATIONS)}
    for f in _rows(zf, "CONCEPT_RELATIONSHIP.csv"):
        a, b, rel, invalid = f[0], f[1], f[2], f[5] if len(f) > 5 else ""
        if a not in kept or invalid:
            continue
        if rel == "Is a":
            if b in kept and b != a:
                parents[a].add(b)
        elif rel in rank and b in code_of and kept[a]["invalid_reason"]:
            best = replacement.get(a)
            candidate = (rank[rel], code_of[b], rel)
            if best is None or candidate < best:
                replacement[a] = candidate

    version = ""
    for f in _rows(zf, "VOCABULARY.csv"):
        if f[0] == "SNOMED":
            version = f[3]

    rows = []
    for cid, c in kept.items():
        label = c["label"]
        syns = sorted(s for s in synonyms.get(cid, ()) if s.casefold() != label.casefold())
        rep = replacement.get(cid)
        rows.append({
            "id": f"SCTID:{c['code']}",
            "label": label,
            "exact_synonyms": "|".join(syns),
            "parents": "|".join(sorted(f"SCTID:{kept[p]['code']}" for p in parents.get(cid, ()))),
            "semantic_tag": tags.get(cid, ""),
            "concept_class_id": c["concept_class_id"],
            "domain_id": c["domain_id"],
            "standard_concept": c["standard_concept"],
            "invalid_reason": c["invalid_reason"],
            "obsolete": "true" if c["invalid_reason"] else "false",
            "replaced_by": f"SCTID:{rep[1]}" if rep else "",
            "replacement_relation": rep[2] if rep else "",
            "omop_concept_id": cid,
        })
    rows.sort(key=lambda r: r["id"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        # Default quoting, matched by load_tsv's reader: SNOMED names can open with a
        # double quote ('"Dancing" eyes'), which an unquoted cell would mis-parse.
        w = csv.DictWriter(fh, fieldnames=TSV_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    provenance: dict[str, object] = {
        "source": zip_path.name,
        "source_sha256": sha256_file(zip_path),
        "vocabulary_version": version,
        "classes": list(classes),
        "concepts": len(rows),
        "by_class": dict(sorted(Counter(r["concept_class_id"] for r in rows).items())),
        "by_invalid_reason": dict(sorted(
            Counter(r["invalid_reason"] or "valid" for r in rows).items())),
        "with_replacement": sum(1 for r in rows if r["replaced_by"]),
        "synonym_scope": "English descriptions loaded as exact synonyms; FSN semantic "
                         "tags stripped into semantic_tag",
    }
    Path(f"{out_path}.provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return provenance


# --- OMOP seed mappings ------------------------------------------------------------------

#: OMOP metadata predicate -> SSSOM predicate. Read per row in the Mondo -> SNOMED direction:
#: ``broadMatch`` means the SNOMED concept is the broader one ("uphill" mapping).
OMOP_PREDICATE = {
    "exactMatch": "skos:exactMatch",
    "broadMatch": "skos:broadMatch",
    "narrowMatch": "skos:narrowMatch",
    "closeMatch": "skos:closeMatch",
    "relatedMatch": "skos:relatedMatch",
}

#: Predicate for a ``Maps to`` row whose metadata gives none. OMOP's convention is that a
#: ``Maps to`` target is the same concept or the nearest broader standard one, so the claim
#: the row actually makes is "same or broader". Recording the weaker half, rather than
#: guessing exact, keeps an unstated equivalence out of the file; reconciliation then flags
#: the ones whose strings are identical (``predicate_vs_lexical``) for a human to upgrade.
MISSING_PREDICATE = "skos:broadMatch"

OMOP_SEED_SET_ID = "https://w3id.org/mondo-ai/align/seed/omop-mondo-snomed"

SEED_COLUMNS = [
    "subject_id", "predicate_id", "object_id", "mapping_justification", "confidence",
    "mapping_provider", "publication_date", "comment",
]


def _mondo_curie(code: str) -> str:
    """Normalise OMOP's Mondo concept code to a CURIE.

    Example:
        >>> _mondo_curie("MONDO_0000001")
        'MONDO:0000001'
        >>> _mondo_curie("MONDO:0000001")
        'MONDO:0000001'
    """
    return code.replace("_", ":", 1) if code.startswith("MONDO_") else code


def _cell(value: object) -> str:
    """Render a spreadsheet cell as the code it holds.

    Excel stores numeric SNOMED codes as numbers, so ``64572001`` reads back as a float and
    ``str()`` would give ``'64572001.0'`` — an identifier that exists nowhere.

    Example:
        >>> _cell(64572001.0), _cell(64572001), _cell("8000/0"), _cell(None)
        ('64572001', '64572001', '8000/0', '')
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def omop_seed_sssom(
    xlsx_path: str | Path,
    metadata_path: str | Path,
    out_path: str | Path,
) -> dict[str, int]:
    """Convert OMOP's Mondo ``Maps to`` relationships plus their metadata into SSSOM.

    Only SNOMED targets are emitted; rows into other vocabularies (ICD-O-3, Cancer Modifier)
    are counted in the header comment rather than silently lost.

    The justification follows the choice made in the niehs_standards OMOP→SSSOM transform:
    ``AM-tool`` is an automapping approach code, so ``semapv:UnspecifiedMatching``, with the
    raw OMOP fields kept in ``comment``. "Spot-curation" is not a per-row review claim and is
    not promoted to one.

    Raises:
        ValueError: If a relationship has no metadata row, or the metadata has a predicate
            outside :data:`OMOP_PREDICATE`.

    Returns:
        Row counts: emitted, and dropped per foreign vocabulary.
    """
    from openpyxl import load_workbook  # type: ignore[import-untyped]

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    sheet = wb.worksheets[0]
    it = sheet.iter_rows(values_only=True)
    header = [str(h) for h in next(it)]
    relationships = [dict(zip(header, (_cell(v) for v in row)))
                     for row in it if any(v is not None for v in row)]
    wb.close()

    with open(metadata_path, newline="", encoding="utf-8") as fh:
        meta = {(r["concept_code_1"], r["concept_code_2"], r["relationship_id"]): r
                for r in csv.DictReader(fh)}

    rows: list[dict[str, str]] = []
    dropped: Counter[str] = Counter()
    no_predicate = 0
    for rel in relationships:
        key = (rel["concept_code_1"], rel["concept_code_2"], rel["relationship_id"])
        m = meta.get(key)
        if m is None:
            raise ValueError(f"relationship {key} has no metadata row in {metadata_path}")
        if rel["vocabulary_id_2"] != "SNOMED":
            dropped[rel["vocabulary_id_2"]] += 1
            continue
        note = ""
        if not m["relationship_predicate_id"]:
            predicate, note = MISSING_PREDICATE, (
                "; no relationship_predicate_id in the metadata, so 'Maps to' is read as "
                "same-or-broader and recorded as the weaker broadMatch")
            no_predicate += 1
        else:
            predicate = OMOP_PREDICATE.get(m["relationship_predicate_id"], "")
        if not predicate:
            raise ValueError(f"unknown OMOP predicate {m['relationship_predicate_id']!r} "
                             f"on {key}")
        rows.append({
            "subject_id": _mondo_curie(rel["concept_code_1"]),
            "predicate_id": predicate,
            "object_id": f"SCTID:{rel['concept_code_2']}",
            "mapping_justification": "semapv:UnspecifiedMatching",
            "confidence": m["confidence"],
            "mapping_provider": "https://www.ohdsi.org/",
            "publication_date": rel["valid_start_date"][:10],
            "comment": (f"OMOP relationship_id={rel['relationship_id']}; "
                        f"mapping_tool={m['mapping_tool']}; "
                        f"mapping_source={m['mapping_source']}{note}"),
        })
    rows.sort(key=lambda r: (r["subject_id"], r["object_id"]))

    header_lines = [
        f"mapping_set_id: {OMOP_SEED_SET_ID}",
        "mapping_set_title: OMOP Mondo to SNOMED CT Maps to relationships",
        "license: https://www.ohdsi.org/",
        "subject_source: http://purl.obolibrary.org/obo/mondo.owl",
        "object_source: http://snomed.info/sct",
        "curie_map:",
        "  MONDO: http://purl.obolibrary.org/obo/MONDO_",
        "  SCTID: http://snomed.info/id/",
        "  skos: http://www.w3.org/2004/02/skos/core#",
        "  semapv: https://w3id.org/semapv/vocab/",
        "comment: >-",
        f"  Converted from {Path(xlsx_path).name} (sha256 {sha256_file(xlsx_path)[:16]}) "
        f"and {Path(metadata_path).name} (sha256 {sha256_file(metadata_path)[:16]}). "
        f"Rows to other vocabularies dropped:"
        + ("".join(f" {k}: {v}," for k, v in sorted(dropped.items())).rstrip(",") or " none")
        + f". Rows without a predicate: {no_predicate}, recorded as {MISSING_PREDICATE}.",
    ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        for line in header_lines:
            fh.write(f"#{line}\n")
        w = csv.DictWriter(fh, fieldnames=SEED_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"emitted": len(rows), "without_predicate": no_predicate,
            **{f"dropped_{k}": v for k, v in sorted(dropped.items())}}
