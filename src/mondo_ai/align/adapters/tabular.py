"""Adapters for tabular and structured-file resources.

Not every alignment target is an ontology. A curated TSV of local codes, a YAML list of
concepts from a data dictionary, or the object side of an existing SSSOM set are all
legitimate inputs, and the rest of the pipeline neither knows nor cares which adapter
produced its concepts.

Column and key names are declared rather than guessed, with conventional defaults.

Example:
    >>> split_multi("a|b | c", "|")
    ['a', 'b', 'c']
    >>> split_multi("", "|")
    []
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from mondo_ai.align.models import Concept, ResourceSet
from mondo_ai.align.provenance import file_provenance


def split_multi(value: str, sep: str) -> list[str]:
    """Split a delimited cell, trimming and dropping empties.

    Example:
        >>> split_multi("x;;y", ";")
        ['x', 'y']
    """
    if not value:
        return []
    return [p.strip() for p in value.split(sep) if p.strip()]


def _finish(
    concepts: dict[str, Concept],
    path: Path,
    prefix: str,
    name: str,
    source_url: str,
) -> ResourceSet:
    from mondo_ai.align.adapters.obo import link_hierarchy

    rs = ResourceSet(
        name=name or prefix.lower(),
        prefix=prefix,
        concepts=concepts,
        source_path=str(path),
        source_url=source_url,
        **file_provenance(path),
    )
    link_hierarchy(rs)
    return rs


def load_tsv(
    path: str | Path,
    prefix: str,
    name: str = "",
    *,
    id_column: str = "id",
    label_column: str = "label",
    definition_column: str = "definition",
    exact_synonym_column: str = "exact_synonyms",
    other_synonym_column: str = "synonyms",
    parent_column: str = "parents",
    obsolete_column: str = "",
    multivalue_sep: str = "|",
    delimiter: str = "\t",
    source_url: str = "",
) -> ResourceSet:
    """Load concepts from a delimited text file.

    Identifiers that already carry the resource prefix are used as-is; bare local ids are
    prefixed. Unknown columns are preserved in ``extra`` rather than dropped, so a
    downstream question about a column the pipeline did not model is still answerable from
    ``records.jsonl``.

    ``obsolete_column``, when declared, marks rows whose value is ``true`` as obsolete:
    they stay loaded, so a seed mapping pointing at one resolves to "retired" rather than to
    "does not exist", but they are not indexed. A ``<path>.provenance.json`` sidecar written
    by the extraction step supplies the resource version, when present.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.tsv"
        >>> _ = p.write_text("id\\tlabel\\texact_synonyms\\tnotes\\n"
        ...                  "1\\tcholera\\tvibrio cholerae infection\\tfoo\\n")
        >>> rs = load_tsv(p, "LOCAL")
        >>> rs.concepts["LOCAL:1"].label, rs.concepts["LOCAL:1"].extra["notes"]
        ('cholera', 'foo')
        >>> _ = p.write_text("id\\tlabel\\tobsolete\\n1\\tcholera\\ttrue\\n2\\ttyphoid\\tfalse\\n")
        >>> rs = load_tsv(p, "LOCAL", obsolete_column="obsolete")
        >>> [c.id for c in rs.active()]
        ['LOCAL:2']
    """
    path = Path(path)
    concepts: dict[str, Concept] = {}
    known = {id_column, label_column, definition_column, exact_synonym_column,
             other_synonym_column, parent_column, obsolete_column}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter=delimiter):
            raw_id = (row.get(id_column) or "").strip()
            if not raw_id:
                continue
            cid = raw_id if ":" in raw_id else f"{prefix}:{raw_id}"
            parents = split_multi(row.get(parent_column, "") or "", multivalue_sep)
            concepts[cid] = Concept(
                id=cid,
                label=(row.get(label_column) or "").strip(),
                definition=(row.get(definition_column) or "").strip(),
                exact_synonyms=split_multi(
                    row.get(exact_synonym_column, "") or "", multivalue_sep),
                other_synonyms=split_multi(
                    row.get(other_synonym_column, "") or "", multivalue_sep),
                parents=[p if ":" in p else f"{prefix}:{p}" for p in parents],
                obsolete=bool(obsolete_column)
                and (row.get(obsolete_column) or "").strip().lower() == "true",
                extra={k: (v or "") for k, v in row.items() if k not in known and k},
            )
    rs = _finish(concepts, path, prefix, name, source_url)
    sidecar = Path(f"{path}.provenance.json")
    if sidecar.exists():
        import json

        rs.version = str(json.loads(sidecar.read_text()).get("vocabulary_version", ""))
    return rs


def load_yaml(
    path: str | Path,
    prefix: str,
    name: str = "",
    *,
    concepts_key: str = "concepts",
    id_key: str = "id",
    label_key: str = "label",
    definition_key: str = "definition",
    exact_synonym_key: str = "exact_synonyms",
    other_synonym_key: str = "synonyms",
    parent_key: str = "parents",
    source_url: str = "",
) -> ResourceSet:
    """Load concepts from a YAML file.

    Accepts either a top-level list, or a mapping with a list under ``concepts_key``, or a
    mapping of id -> record. Each shape occurs in real configuration files and guessing
    between them is unambiguous.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.yaml"
        >>> _ = p.write_text("concepts:\\n"
        ...                  "  - id: A\\n    label: cholera\\n"
        ...                  "  - id: B\\n    label: typhoid\\n    parents: [A]\\n")
        >>> rs = load_yaml(p, "L")
        >>> sorted(rs.concepts), rs.concepts["L:B"].parents
        (['L:A', 'L:B'], ['L:A'])
        >>> p2 = pathlib.Path(tempfile.mkdtemp()) / "m.yaml"
        >>> _ = p2.write_text("A: {label: cholera}\\nB: {label: typhoid}\\n")
        >>> sorted(load_yaml(p2, "L").concepts)
        ['L:A', 'L:B']
    """
    import yaml

    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    records: list[dict[str, Any]]
    if isinstance(doc, list):
        records = [r for r in doc if isinstance(r, dict)]
    elif isinstance(doc, dict) and isinstance(doc.get(concepts_key), list):
        records = [r for r in doc[concepts_key] if isinstance(r, dict)]
    elif isinstance(doc, dict):
        records = [{**v, id_key: k} for k, v in doc.items() if isinstance(v, dict)]
    else:
        records = []

    known = {id_key, label_key, definition_key, exact_synonym_key, other_synonym_key,
             parent_key}
    concepts: dict[str, Concept] = {}
    for rec in records:
        raw_id = str(rec.get(id_key, "")).strip()
        if not raw_id:
            continue
        cid = raw_id if ":" in raw_id else f"{prefix}:{raw_id}"
        parents = [str(p) for p in (rec.get(parent_key) or [])]
        concepts[cid] = Concept(
            id=cid,
            label=str(rec.get(label_key, "") or "").strip(),
            definition=str(rec.get(definition_key, "") or "").strip(),
            exact_synonyms=[str(s) for s in (rec.get(exact_synonym_key) or [])],
            other_synonyms=[str(s) for s in (rec.get(other_synonym_key) or [])],
            parents=[p if ":" in p else f"{prefix}:{p}" for p in parents],
            extra={k: str(v) for k, v in rec.items() if k not in known},
        )
    return _finish(concepts, path, prefix, name, source_url)


def load_sssom(
    path: str | Path,
    prefix: str,
    name: str = "",
    *,
    side: str = "object",
    source_url: str = "",
) -> ResourceSet:
    """Load one side of an SSSOM file as a concept set.

    Useful for aligning against a vocabulary you only have as a mapping set, and for
    loading a seed/anchor set. Comment lines carrying the YAML header block are skipped.

    Args:
        side: ``subject`` or ``object`` — which side of each row to harvest.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "m.sssom.tsv"
        >>> _ = p.write_text("# curie_map:\\n"
        ...                  "subject_id\\tsubject_label\\tobject_id\\tobject_label\\n"
        ...                  "M:1\\tcholera\\tI:A00\\tCholera\\n")
        >>> rs = load_sssom(p, "I")
        >>> rs.concepts["I:A00"].label
        'Cholera'
        >>> load_sssom(p, "M", side="subject").concepts["M:1"].label
        'cholera'
    """
    if side not in ("subject", "object"):
        raise ValueError(f"side must be 'subject' or 'object', got {side!r}")
    path = Path(path)
    concepts: dict[str, Concept] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    for row in csv.DictReader(lines, delimiter="\t"):
        cid = (row.get(f"{side}_id") or "").strip()
        if not cid:
            continue
        label = (row.get(f"{side}_label") or "").strip()
        c = concepts.get(cid)
        if c is None:
            concepts[cid] = Concept(id=cid, label=label)
        elif label and not c.label:
            c.label = label
    return _finish(concepts, path, prefix, name, source_url)
