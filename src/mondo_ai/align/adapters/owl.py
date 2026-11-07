"""OWL / RDF adapter, streaming via pyoxigraph.

Streams quads rather than loading a store: the ICD-10-CM component from mondo-ingest is
102 MB / 910k triples and streaming keeps peak memory to the concept dictionary itself.

Annotation vocabularies vary across sources, so the adapter reads the union of the ones
that actually occur in OBO-ecosystem and UMLS2RDF output — ``rdfs:label``,
``skos:prefLabel``, ``skos:altLabel``, ``oboInOwl:hasExactSynonym`` and friends — rather
than branching per source. A source that uses none of them yields concepts without labels,
which :meth:`ResourceSet.active` then excludes and the loader reports.

Reified ``owl:Axiom`` blocks are skipped: their annotated targets duplicate the direct
assertions and would double every synonym.

Example:
    >>> curie_from_iri("http://purl.bioontology.org/ontology/ICD10CM/Q87.4",
    ...                {"http://purl.bioontology.org/ontology/ICD10CM/": "ICD10CM"})
    'ICD10CM:Q87.4'
    >>> curie_from_iri("http://purl.obolibrary.org/obo/MONDO_0007947",
    ...                {"http://purl.obolibrary.org/obo/MONDO_": "MONDO"})
    'MONDO:0007947'
    >>> curie_from_iri("http://example.org/x", {}) is None
    True
"""

from __future__ import annotations

from pathlib import Path

from mondo_ai.align.models import Concept, ResourceSet
from mondo_ai.align.provenance import file_provenance

RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
OIO = "http://www.geneontology.org/formats/oboInOwl#"
IAO_DEF = "http://purl.obolibrary.org/obo/IAO_0000115"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

#: Predicates carrying a primary label.
LABEL_PREDS = frozenset({f"{RDFS}label", f"{SKOS}prefLabel"})
#: Predicates carrying a synonym the source calls exact.
EXACT_SYN_PREDS = frozenset({f"{OIO}hasExactSynonym", f"{SKOS}altLabel"})
#: Predicates carrying any other synonym.
OTHER_SYN_PREDS = frozenset({
    f"{OIO}hasRelatedSynonym", f"{OIO}hasBroadSynonym", f"{OIO}hasNarrowSynonym",
    f"{SKOS}hiddenLabel",
})
#: Predicates carrying a textual definition.
DEF_PREDS = frozenset({IAO_DEF, f"{SKOS}definition", f"{RDFS}comment"})


def curie_from_iri(iri: str, bases: dict[str, str]) -> str | None:
    """Contract an IRI to a CURIE using declared base -> prefix pairs.

    Longest base wins, so overlapping bases (``.../obo/MONDO_`` inside ``.../obo/``)
    resolve to the more specific one.

    Args:
        iri: The full IRI.
        bases: Mapping of IRI prefix string to CURIE prefix.

    Returns:
        The CURIE, or None if no base matches.

    Example:
        >>> bases = {"http://purl.obolibrary.org/obo/": "obo",
        ...          "http://purl.obolibrary.org/obo/MONDO_": "MONDO"}
        >>> curie_from_iri("http://purl.obolibrary.org/obo/MONDO_1", bases)
        'MONDO:1'
    """
    best_base, best_prefix = "", ""
    for base, prefix in bases.items():
        if iri.startswith(base) and len(base) > len(best_base):
            best_base, best_prefix = base, prefix
    if not best_base:
        return None
    return f"{best_prefix}:{iri[len(best_base):]}"


def load_owl(
    path: str | Path,
    prefix: str,
    iri_base: str,
    name: str = "",
    *,
    source_url: str = "",
    extra_bases: dict[str, str] | None = None,
    rdf_format: str = "",
) -> ResourceSet:
    """Load an OWL/RDF file into a :class:`ResourceSet`.

    Args:
        path: Path to the RDF file.
        prefix: CURIE prefix for concepts of interest, e.g. ``ICD10CM``.
        iri_base: IRI prefix that ``prefix`` abbreviates.
        name: Resource name; defaults to the lower-cased prefix.
        source_url: Recorded for provenance.
        extra_bases: Additional IRI-base -> prefix pairs, used to contract xref and parent
            IRIs that belong to other vocabularies.
        rdf_format: Override the format guessed from the file extension
            (``rdfxml``, ``turtle``, ``ntriples``, ...).

    Returns:
        The resource, with ``children`` and ``n_descendants`` filled in.

    Example:
        >>> import tempfile, pathlib
        >>> rdf = '''<?xml version="1.0"?>
        ... <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        ...   xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
        ...   xmlns:owl="http://www.w3.org/2002/07/owl#"
        ...   xmlns:oio="http://www.geneontology.org/formats/oboInOwl#">
        ...   <owl:Class rdf:about="http://ex.org/T/A00">
        ...     <rdfs:label>Cholera</rdfs:label>
        ...     <oio:hasExactSynonym>Vibrio cholerae infection</oio:hasExactSynonym>
        ...   </owl:Class>
        ...   <owl:Class rdf:about="http://ex.org/T/A00.0">
        ...     <rdfs:label>Cholera due to Vibrio cholerae 01</rdfs:label>
        ...     <rdfs:subClassOf rdf:resource="http://ex.org/T/A00"/>
        ...   </owl:Class>
        ... </rdf:RDF>'''
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.owl"
        >>> _ = p.write_text(rdf)
        >>> rs = load_owl(p, "T", "http://ex.org/T/")
        >>> sorted(rs.concepts)
        ['T:A00', 'T:A00.0']
        >>> rs.concepts["T:A00"].label, rs.concepts["T:A00"].exact_synonyms
        ('Cholera', ['Vibrio cholerae infection'])
        >>> rs.concepts["T:A00.0"].parents, rs.concepts["T:A00"].n_descendants
        (['T:A00'], 1)
    """
    import pyoxigraph as ox

    from mondo_ai.align.adapters.obo import link_hierarchy

    path = Path(path)
    bases = {iri_base: prefix}
    if extra_bases:
        bases.update(extra_bases)

    fmt = _resolve_format(ox, path, rdf_format)
    concepts: dict[str, Concept] = {}
    axiom_subjects: set[str] = set()
    version = ""

    def get(cid: str) -> Concept:
        c = concepts.get(cid)
        if c is None:
            c = Concept(id=cid)
            concepts[cid] = c
        return c

    for quad in ox.parse(path=str(path), format=fmt):
        subj, pred, obj = quad.subject, quad.predicate, quad.object
        if not isinstance(subj, ox.NamedNode):
            continue
        s_iri, p_iri = subj.value, pred.value

        # Reified axiom blocks restate direct assertions; reading both doubles synonyms.
        if p_iri == RDF_TYPE and isinstance(obj, ox.NamedNode) and obj.value == f"{OWL}Axiom":
            axiom_subjects.add(s_iri)
            continue
        if s_iri in axiom_subjects:
            continue

        if p_iri == f"{OWL}versionIRI" and isinstance(obj, ox.NamedNode):
            version = obj.value
            continue

        cid = curie_from_iri(s_iri, {iri_base: prefix})
        if cid is None:
            continue
        if isinstance(obj, ox.Literal):
            text = obj.value.strip()
            if not text:
                continue
            c = get(cid)
            if p_iri in LABEL_PREDS:
                if not c.label:
                    c.label = text
                elif text not in c.exact_synonyms:
                    c.exact_synonyms.append(text)
            elif p_iri in EXACT_SYN_PREDS:
                if text not in c.exact_synonyms:
                    c.exact_synonyms.append(text)
            elif p_iri in OTHER_SYN_PREDS:
                if text not in c.other_synonyms:
                    c.other_synonyms.append(text)
            elif p_iri in DEF_PREDS:
                if not c.definition:
                    c.definition = text
            elif p_iri == f"{OWL}deprecated" and text.lower() == "true":
                c.obsolete = True
            elif p_iri == f"{OIO}hasDbXref":
                c.xrefs.append(text)
        elif isinstance(obj, ox.NamedNode):
            if p_iri == f"{RDFS}subClassOf":
                parent = curie_from_iri(obj.value, {iri_base: prefix})
                if parent and parent != cid:
                    get(cid).parents.append(parent)
            elif p_iri == f"{OIO}hasDbXref":
                x = curie_from_iri(obj.value, bases)
                if x:
                    get(cid).xrefs.append(x)
            elif p_iri == f"{RDF_TYPE}" and obj.value == f"{OWL}Class":
                get(cid)

    # UMLS2RDF and ODK normalisation both mint a synonym identical to the label
    # (oboInOwl:hasSynonymType mondo#GENERATED_FROM_LABEL). Keeping it would double
    # every string in the index for no gain.
    for c in concepts.values():
        if c.label:
            c.exact_synonyms = [s for s in c.exact_synonyms if s != c.label]
            c.other_synonyms = [s for s in c.other_synonyms if s != c.label]

    rs = ResourceSet(
        name=name or prefix.lower(),
        prefix=prefix,
        concepts=concepts,
        source_path=str(path),
        source_url=source_url,
        version=version,
        **file_provenance(path),
    )
    link_hierarchy(rs)
    return rs


def _resolve_format(ox, path: Path, override: str):
    """Pick a pyoxigraph RDF format from an override or the file suffix."""
    names = {
        "rdfxml": "RDF_XML", "rdf": "RDF_XML", "owl": "RDF_XML", "xml": "RDF_XML",
        "turtle": "TURTLE", "ttl": "TURTLE",
        "ntriples": "N_TRIPLES", "nt": "N_TRIPLES",
        "n3": "N3", "trig": "TRIG", "nquads": "N_QUADS",
        "jsonld": "N_TRIPLES",
    }
    key = (override or path.suffix.lstrip(".")).lower()
    attr = names.get(key)
    if attr is None:
        raise ValueError(
            f"cannot determine RDF format for {path.name!r}; pass rdf_format explicitly "
            f"(one of: {', '.join(sorted(set(names)))})"
        )
    return getattr(ox.RdfFormat, attr)
