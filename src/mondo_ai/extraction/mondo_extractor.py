"""Curator and publication provenance on Mondo's own axioms.

Mondo annotates its axioms with where they came from: the curator (an ORCID, written as
an ``https://orcid.org/`` URL) and sometimes a publication (``PMID:``). In the SIEVE
profile Mondo's own assertion is not evidence for itself, but who curated it and what
it cites are: :mod:`mondo_ai.packets.mondo_provenance` turns them into curator
contributions and publications on the packets of claims already in Mondo.

This module reads that provenance in bulk, one SPARQL query per kind of axiom (a
per-entity loop is far too slow for the 255 MB mondo.owl):

- synonyms (``oboInOwl:has{Exact,Related,Narrow,Broad}Synonym``), annotated with
  ``oboInOwl:hasDbXref`` (and occasionally ``oboInOwl:source``);
- named-parent ``rdfs:subClassOf`` axioms, annotated with ``oboInOwl:source``;
- skos mappings (``skos:exactMatch`` etc.). Mondo does not annotate the skos triples
  themselves: their provenance is on the ``oboInOwl:hasDbXref`` axiom for the same
  target (``oboInOwl:source MONDO:equivalentTo`` plus the curator), so each skos triple
  takes the provenance of its xref axiom, and of the skos axiom if there is one.

The result is a plain dict keyed by ``(subject, predicate CURIE, object)``, the shape of a
packet's claim; synonym objects are casefolded. Only axioms with at least one ORCID or
PMID are kept.

Example:
    >>> prov = MondoProvenanceExtractor().extract("data/owl/2026-11-20/mondo.owl")  # doctest: +SKIP
    >>> prov[("MONDO:0000005", "rdfs:subClassOf", "MONDO:0100118")]  # doctest: +SKIP
    AxiomProvenance(orcids=('0000-0002-5002-8648',), pmids=())
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyoxigraph as px

from mondo_ai.extraction.owl_extractor import (
    OBO_IN_OWL,
    SKOS,
    _curie_from_iri,
    _get_converter,
    _value,
    extract_provenance_from_sources,
    normalize_xref,
)

MONDO_IRI_PREFIX = "http://purl.obolibrary.org/obo/MONDO_"
RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
HAS_DBXREF = f"{OBO_IN_OWL}hasDbXref"
SOURCE = f"{OBO_IN_OWL}source"

SYNONYM_PREDICATES = {
    f"{OBO_IN_OWL}hasExactSynonym": "oboInOwl:hasExactSynonym",
    f"{OBO_IN_OWL}hasRelatedSynonym": "oboInOwl:hasRelatedSynonym",
    f"{OBO_IN_OWL}hasNarrowSynonym": "oboInOwl:hasNarrowSynonym",
    f"{OBO_IN_OWL}hasBroadSynonym": "oboInOwl:hasBroadSynonym",
}
SKOS_PREDICATES = {
    f"{SKOS}exactMatch": "skos:exactMatch",
    f"{SKOS}closeMatch": "skos:closeMatch",
    f"{SKOS}broadMatch": "skos:broadMatch",
    f"{SKOS}narrowMatch": "skos:narrowMatch",
    f"{SKOS}relatedMatch": "skos:relatedMatch",
}
KIND_OF_PREDICATE = {
    **{p: "synonym" for p in SYNONYM_PREDICATES.values()},
    "rdfs:subClassOf": "subclass",
    **{p: "mapping" for p in SKOS_PREDICATES.values()},
}

AxiomKey = tuple[str, str, str]

_ORCID_URL = re.compile(r"https?://orcid\.org/", re.IGNORECASE)
_PMID_SPACED = re.compile(r"PMID:\s+(\d)", re.IGNORECASE)
_PUBMED_URL = re.compile(r"https?://(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/(\d+)", re.IGNORECASE)

_PREFIXES = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#> "
    f"PREFIX oio: <{OBO_IN_OWL}> "
)
_MONDO_SUBJECT = f"FILTER(isIRI(?e) && STRSTARTS(STR(?e), '{MONDO_IRI_PREFIX}'))"


@dataclass(frozen=True)
class AxiomProvenance:
    """The curators (ORCIDs, bare ``0000-...``) and publications (PMIDs, bare numbers) on an axiom.

    >>> bool(AxiomProvenance()), bool(AxiomProvenance(pmids=("1",)))
    (False, True)
    """

    orcids: tuple[str, ...] = ()
    pmids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        """True if the axiom names a curator or a publication."""
        return bool(self.orcids or self.pmids)


def _normalise_source(value: str) -> str:
    """Rewrite Mondo's ORCID and PubMed URLs (and ``PMID: n``) as the CURIEs the patterns read.

    >>> _normalise_source("https://orcid.org/0000-0002-6601-2165")
    'ORCID:0000-0002-6601-2165'
    >>> _normalise_source("https://pubmed.ncbi.nlm.nih.gov/27745641")
    'PMID:27745641'
    >>> _normalise_source("PMID: 333")
    'PMID:333'
    """
    value = _ORCID_URL.sub("ORCID:", value)
    value = _PUBMED_URL.sub(r"PMID:\1", value)
    return _PMID_SPACED.sub(r"PMID:\1", value)


def provenance_from_annotations(values: list[str]) -> AxiomProvenance:
    """The ORCIDs and PMIDs named by an axiom's annotation values, in order, without duplicates.

    >>> provenance_from_annotations(["MONDO:equivalentTo", "https://orcid.org/0000-0002-6601-2165"])
    AxiomProvenance(orcids=('0000-0002-6601-2165',), pmids=())
    """
    orcids, pmids = extract_provenance_from_sources([_normalise_source(v) for v in values])
    return AxiomProvenance(orcids=tuple(orcids), pmids=tuple(pmids))


class MondoProvenanceExtractor:
    """Read the ORCIDs and PMIDs on Mondo's synonym, subClassOf and mapping axioms."""

    def __init__(self) -> None:
        """Create an extractor; the OWL file is loaded by :meth:`extract`."""
        self._store: px.Store | None = None
        self._converter = _get_converter()

    def _curie(self, iri: str) -> str:
        return _curie_from_iri(iri, self._converter)

    def _rows(self, sparql: str) -> Iterator[px.QuerySolution]:
        if self._store is None:
            raise RuntimeError("Store not loaded")
        result = self._store.query(sparql)
        assert isinstance(result, px.QuerySolutions)  # SELECT queries yield solutions
        yield from result

    def _annotations(self, predicates: list[str], keys: list[str]) -> Iterator[tuple[str, str, str, str]]:
        """(subject, predicate, target, annotation value) of every annotated axiom on a Mondo subject."""
        preds = " ".join(f"<{p}>" for p in predicates)
        props = " ".join(f"<{k}>" for k in keys)
        for row in self._rows(
            f"""{_PREFIXES} SELECT ?e ?p ?t ?v WHERE {{
                VALUES ?p {{ {preds} }} VALUES ?k {{ {props} }}
                ?ax owl:annotatedSource ?e ; owl:annotatedProperty ?p ; owl:annotatedTarget ?t ; ?k ?v .
                {_MONDO_SUBJECT} }}"""
        ):
            yield _value(row["e"]), _value(row["p"]), _value(row["t"]), _value(row["v"])

    def extract(self, owl_path: str | Path) -> dict[AxiomKey, AxiomProvenance]:
        """Load a Mondo OWL file (RDF/XML or Turtle) and read the provenance of its axioms.

        Args:
            owl_path: Path to mondo.owl.

        Returns:
            ``(subject CURIE, predicate CURIE, object)`` -> provenance, for every synonym,
            named-parent subClassOf and skos mapping axiom that names an ORCID or PMID.
            Synonym objects are casefolded; mapping objects are the CURIEs Mondo writes
            in its xrefs (``OMIM:219700``).
        """
        path = Path(owl_path)
        store = px.Store()
        fmt = px.RdfFormat.TURTLE if path.suffix in (".ttl", ".turtle") else px.RdfFormat.RDF_XML
        store.bulk_load(path=str(path), format=fmt)
        self._store = store

        sources: dict[AxiomKey, list[str]] = defaultdict(list)
        self._read_synonyms(sources)
        self._read_subclass(sources)
        self._read_mappings(sources)

        out: dict[AxiomKey, AxiomProvenance] = {}
        for key, values in sources.items():
            prov = provenance_from_annotations(values)
            if prov:
                out[key] = prov
        return out

    def _read_synonyms(self, sources: dict[AxiomKey, list[str]]) -> None:
        for e, p, t, v in self._annotations(list(SYNONYM_PREDICATES), [HAS_DBXREF, SOURCE]):
            sources[(self._curie(e), SYNONYM_PREDICATES[p], t.casefold())].append(v)

    def _read_subclass(self, sources: dict[AxiomKey, list[str]]) -> None:
        for e, _, t, v in self._annotations([RDFS_SUBCLASS_OF], [SOURCE]):
            if "://" in t:  # named parents only, not restrictions (blank nodes)
                sources[(self._curie(e), "rdfs:subClassOf", self._curie(t))].append(v)

    def _read_mappings(self, sources: dict[AxiomKey, list[str]]) -> None:
        """skos mappings, with the provenance of the xref axiom for the same target."""
        xref_sources: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for e, _, t, v in self._annotations([HAS_DBXREF], [SOURCE]):
            xref = normalize_xref(t)
            xref_sources.setdefault((e, xref.casefold()), (xref, []))[1].append(v)

        skos_sources: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for e, p, t, v in self._annotations(list(SKOS_PREDICATES), [SOURCE, HAS_DBXREF]):
            skos_sources[(e, p, t)].append(v)

        preds = " ".join(f"<{p}>" for p in SKOS_PREDICATES)
        for row in self._rows(f"{_PREFIXES} SELECT ?e ?p ?o WHERE {{ VALUES ?p {{ {preds} }} ?e ?p ?o . {_MONDO_SUBJECT} }}"):
            e, p, o = _value(row["e"]), _value(row["p"]), _value(row["o"])
            target = self._curie(o)
            xref, values = xref_sources.get((e, target.casefold()), (target, []))
            values = values + skos_sources.get((e, p, o), [])
            if values:
                sources[(self._curie(e), SKOS_PREDICATES[p], xref)].extend(values)

    @staticmethod
    def counts(provenance: dict[AxiomKey, AxiomProvenance]) -> dict[str, dict[str, int]]:
        """Number of annotated axioms per kind (synonym, subclass, mapping), with an ORCID, with a PMID.

        >>> MondoProvenanceExtractor.counts({("MONDO:1", "rdfs:subClassOf", "MONDO:2"): AxiomProvenance(pmids=("1",))})
        {'synonym': {'axioms': 0, 'with_orcid': 0, 'with_pmid': 0}, 'subclass': {'axioms': 1, 'with_orcid': 0, 'with_pmid': 1}, 'mapping': {'axioms': 0, 'with_orcid': 0, 'with_pmid': 0}}
        """
        out = {kind: {"axioms": 0, "with_orcid": 0, "with_pmid": 0} for kind in ("synonym", "subclass", "mapping")}
        for (_, predicate, _), prov in provenance.items():
            c = out[KIND_OF_PREDICATE[predicate]]
            c["axioms"] += 1
            c["with_orcid"] += bool(prov.orcids)
            c["with_pmid"] += bool(prov.pmids)
        return out


def extract_mondo_provenance(owl_path: str | Path) -> dict[AxiomKey, AxiomProvenance]:
    """The provenance of Mondo's synonym, subClassOf and mapping axioms (see :class:`MondoProvenanceExtractor`)."""
    return MondoProvenanceExtractor().extract(owl_path)
