"""OBO-Graphs JSON adapter.

The interchange format ROBOT emits, and the one most OBO ontologies can be converted to
without a triple store. Reads the first graph in the document.

Example:
    >>> node_id_to_curie("http://purl.obolibrary.org/obo/MONDO_0007947")
    'MONDO:0007947'
    >>> node_id_to_curie("MONDO:0007947")
    'MONDO:0007947'
"""

from __future__ import annotations

import json
from pathlib import Path

from mondo_ai.align.models import Concept, ResourceSet
from mondo_ai.align.provenance import file_provenance

_OBO_BASE = "http://purl.obolibrary.org/obo/"


def node_id_to_curie(node_id: str) -> str:
    """Contract an OBO-Graphs node id to a CURIE.

    Example:
        >>> node_id_to_curie("http://example.org/ns/A00")
        'http://example.org/ns/A00'
    """
    if node_id.startswith(_OBO_BASE):
        rest = node_id[len(_OBO_BASE):]
        if "_" in rest:
            pre, _, local = rest.partition("_")
            return f"{pre}:{local}"
    return node_id


def load_obojson(
    path: str | Path,
    prefix: str,
    name: str = "",
    *,
    source_url: str = "",
) -> ResourceSet:
    """Load an OBO-Graphs JSON file into a :class:`ResourceSet`.

    Example:
        >>> import tempfile, pathlib, json
        >>> doc = {"graphs": [{"nodes": [
        ...   {"id": "http://purl.obolibrary.org/obo/X_1", "lbl": "cholera",
        ...    "type": "CLASS",
        ...    "meta": {"definition": {"val": "An infection."},
        ...             "synonyms": [{"val": "vibrio infection", "pred": "hasExactSynonym"},
        ...                          {"val": "rice water stool", "pred": "hasRelatedSynonym"}],
        ...             "xrefs": [{"val": "ICD10CM:A00"}]}},
        ...   {"id": "http://purl.obolibrary.org/obo/X_2", "lbl": "sub", "type": "CLASS"}],
        ...   "edges": [{"sub": "http://purl.obolibrary.org/obo/X_2", "pred": "is_a",
        ...              "obj": "http://purl.obolibrary.org/obo/X_1"}]}]}
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.json"
        >>> _ = p.write_text(json.dumps(doc))
        >>> rs = load_obojson(p, "X")
        >>> rs.concepts["X:1"].exact_synonyms, rs.concepts["X:1"].xrefs
        (['vibrio infection'], ['ICD10CM:A00'])
        >>> rs.concepts["X:2"].parents, rs.concepts["X:1"].n_descendants
        (['X:1'], 1)
    """
    from mondo_ai.align.adapters.obo import link_hierarchy

    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    graphs = doc.get("graphs") or []
    graph = graphs[0] if graphs else {}

    concepts: dict[str, Concept] = {}
    for node in graph.get("nodes", []):
        if node.get("type") not in (None, "CLASS"):
            continue
        cid = node_id_to_curie(node.get("id", ""))
        if not cid.startswith(f"{prefix}:"):
            continue
        meta = node.get("meta") or {}
        exact: list[str] = []
        other: list[str] = []
        for syn in meta.get("synonyms", []):
            val = syn.get("val", "")
            if not val:
                continue
            (exact if syn.get("pred") == "hasExactSynonym" else other).append(val)
        concepts[cid] = Concept(
            id=cid,
            label=node.get("lbl", "") or "",
            definition=(meta.get("definition") or {}).get("val", "") or "",
            exact_synonyms=exact,
            other_synonyms=other,
            xrefs=[x.get("val", "") for x in meta.get("xrefs", []) if x.get("val")],
            obsolete=bool(meta.get("deprecated")),
        )

    for edge in graph.get("edges", []):
        if edge.get("pred") not in ("is_a", "rdfs:subClassOf"):
            continue
        sub = node_id_to_curie(edge.get("sub", ""))
        obj = node_id_to_curie(edge.get("obj", ""))
        if sub in concepts and obj in concepts:
            concepts[sub].parents.append(obj)

    rs = ResourceSet(
        name=name or prefix.lower(),
        prefix=prefix,
        concepts=concepts,
        source_path=str(path),
        source_url=source_url,
        version=str((graph.get("meta") or {}).get("version", "")),
        **file_provenance(path),
    )
    link_hierarchy(rs)
    return rs
