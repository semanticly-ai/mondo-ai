"""OWL file extraction using pyoxigraph.

This module converts a source ontology (RDF/XML or Turtle) into a ResourceSnapshot:
labels, synonyms (with scope, type and PMIDs from their axiom annotations),
definitions, named parents, classification parents (part_of), xrefs and skos mappings
(with ORDO's ECC precision code), and deprecation.

Every property is read with one SPARQL query over the whole file rather than one query
per entity, so large sources (ICD-10-CM has ~98,000 classes) extract in seconds.

Example:
    >>> from mondo_ai.extraction.owl_extractor import OWLExtractor
    >>> snapshot = OWLExtractor().extract("doid.owl", "doid", iri_filter="DOID_")  # doctest: +SKIP
"""

import re
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import curies
import pyoxigraph as px

from mondo_ai.datamodel import (
    EntityMapping,
    EntitySnapshot,
    ResourceSnapshot,
    Synonym,
    SynonymScope,
)

# Patterns for extracting ORCIDs and PMIDs from annotation sources
ORCID_PATTERN = re.compile(r"(?:ORCID:|orcid\.org/)(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
PMID_PATTERN = re.compile(r"PMID:(\d+)", re.IGNORECASE)

OBO_IN_OWL = "http://www.geneontology.org/formats/oboInOwl#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
EFO = "http://www.ebi.ac.uk/efo/"
IAO_DEFINITION = "http://purl.obolibrary.org/obo/IAO_0000115"
EFO_DEFINITION = f"{EFO}definition"
HAS_DBXREF = f"{OBO_IN_OWL}hasDbXref"
PART_OF = "http://purl.obolibrary.org/obo/BFO_0000050"
# ORDO annotates each xref with its precision (ECC) under this property.
ECC_PROPERTY = "http://purl.obolibrary.org/obo/ECO_0000218"

SYNONYM_PREDICATES = {
    f"{OBO_IN_OWL}hasExactSynonym": SynonymScope.EXACT,
    f"{OBO_IN_OWL}hasNarrowSynonym": SynonymScope.NARROW,
    f"{OBO_IN_OWL}hasBroadSynonym": SynonymScope.BROAD,
    f"{OBO_IN_OWL}hasRelatedSynonym": SynonymScope.RELATED,
    # EFO alternative_term used by ORDO (treated as EXACT)
    f"{EFO}alternative_term": SynonymScope.EXACT,
}

MAPPING_PREDICATES = {
    HAS_DBXREF: "oboInOwl:hasDbXref",
    f"{SKOS}exactMatch": "skos:exactMatch",
    f"{SKOS}closeMatch": "skos:closeMatch",
    f"{SKOS}broadMatch": "skos:broadMatch",
    f"{SKOS}narrowMatch": "skos:narrowMatch",
    f"{SKOS}relatedMatch": "skos:relatedMatch",
}

# Xref prefixes as written by the sources, normalised to the prefixes Mondo's SSSOM uses.
XREF_PREFIX_SYNONYMS = {
    "MIM": "OMIM",
    "ORDO": "Orphanet",
    "ORPHA": "Orphanet",
    "ICD-10": "ICD10WHO",
    "ICD10": "ICD10WHO",
    "MeSH": "MESH",
    "MSH": "MESH",
    "NCI": "NCIT",
    "UMLS_CUI": "UMLS",
}


def normalize_xref(xref: str) -> str:
    """Normalise a source xref CURIE to the prefix Mondo uses.

    >>> normalize_xref("MIM:219700")
    'OMIM:219700'
    >>> normalize_xref("DOID:4")
    'DOID:4'
    """
    if ":" not in xref:
        return xref
    prefix, local = xref.split(":", 1)
    return f"{XREF_PREFIX_SYNONYMS.get(prefix, prefix)}:{local.strip()}"


def extract_provenance_from_sources(sources: list[str]) -> tuple[list[str], list[str]]:
    """Extract ORCIDs and PMIDs from annotation source strings.

    Args:
        sources: List of source annotation values.

    Returns:
        Tuple of (orcids, pmids) lists.

    Example:
        >>> sources = ["ORCID:0000-0001-5208-3432", "PMID:12345678"]
        >>> orcids, pmids = extract_provenance_from_sources(sources)
        >>> orcids
        ['0000-0001-5208-3432']
        >>> pmids
        ['12345678']
        >>> extract_provenance_from_sources(["https://orcid.org/0000-0002-4142-7153"])[0]
        ['0000-0002-4142-7153']
    """
    orcids: list[str] = []
    pmids: list[str] = []
    for source in sources:
        for match in ORCID_PATTERN.finditer(source):
            if match.group(1) not in orcids:
                orcids.append(match.group(1))
        for match in PMID_PATTERN.finditer(source):
            if match.group(1) not in pmids:
                pmids.append(match.group(1))
    return orcids, pmids


def _get_converter() -> curies.Converter:
    """Get a CURIE converter with standard biomedical prefixes (Bioregistry OBO context)."""
    return curies.get_obo_converter()


def _curie_from_iri(iri: str, converter: curies.Converter | None = None) -> str:
    """Convert IRI to CURIE format using the curies library.

    >>> _curie_from_iri("http://purl.obolibrary.org/obo/DOID_4")
    'DOID:4'
    >>> _curie_from_iri("http://purl.obolibrary.org/obo/MONDO_0000001")
    'MONDO:0000001'
    >>> _curie_from_iri("https://omim.org/entry/219700")
    'OMIM:219700'
    >>> _curie_from_iri("http://purl.bioontology.org/ontology/ICD10CM/Q90.0")
    'ICD10CM:Q90.0'
    """
    if converter is None:
        converter = _get_converter()
    curie = converter.compress(iri)
    if curie:
        return curie
    if "omim.org/entry/" in iri:
        return f"OMIM:{iri.split('/entry/')[-1]}"
    if "omim.org/phenotypicSeries/PS" in iri:
        return f"OMIMPS:{iri.split('/phenotypicSeries/PS')[-1]}"
    if "Orphanet_" in iri:
        return f"Orphanet:{iri.split('Orphanet_')[-1]}"
    if "purl.bioontology.org/ontology/ICD10CM/" in iri:
        return f"ICD10CM:{iri.split('/ICD10CM/')[-1]}"
    if "linkedlifedata.com/resource/umls/id/" in iri:
        return f"UMLS:{iri.split('/id/')[-1]}"
    if "/obo/" in iri:
        parts = iri.split("/obo/")[-1]
        if "_" in parts:
            prefix, local = parts.rsplit("_", 1)
            return f"{prefix}:{local}"
    return iri


def _value(term: object) -> str:
    """The lexical value of a pyoxigraph term (IRI string or literal text)."""
    return str(getattr(term, "value", term))


def _ecc_code(annotation: str) -> str:
    """The ECC code at the start of an ORDO precision annotation.

    >>> _ecc_code("E (Exact mapping: the two concepts are equivalent)")
    'E'
    >>> _ecc_code("NTBT (ORPHA code's Narrower Term maps to a Broader Term)")
    'NTBT'
    """
    return annotation.split(" ", 1)[0].strip()


class OWLExtractor:
    """Extract entities from OWL files using pyoxigraph."""

    def __init__(self) -> None:
        """Initialize the extractor."""
        self._store: px.Store | None = None
        self._converter: curies.Converter | None = None

    def _curie(self, iri: str) -> str:
        if self._converter is None:
            self._converter = _get_converter()
        return _curie_from_iri(iri, self._converter)

    def _load_owl(self, path: Path) -> px.Store:
        """Load an RDF/XML (or Turtle) file into an in-memory pyoxigraph store."""
        store = px.Store()
        fmt = px.RdfFormat.TURTLE if path.suffix in (".ttl", ".turtle") else px.RdfFormat.RDF_XML
        store.bulk_load(path=str(path), format=fmt)
        return store

    def _rows(self, sparql: str) -> Iterator[px.QuerySolution]:
        if self._store is None:
            raise RuntimeError("Store not loaded")
        result = self._store.query(sparql)
        assert isinstance(result, px.QuerySolutions)  # SELECT queries yield solutions
        yield from result

    def extract(
        self,
        owl_path: str | Path,
        resource_id: str,
        resource_name: str | None = None,
        limit: int | None = None,
        iri_filter: str | None = None,
        skip_deprecated: bool = True,
    ) -> ResourceSnapshot:
        """Extract entities from an OWL file.

        Args:
            owl_path: Path to the OWL file (RDF/XML or Turtle).
            resource_id: Identifier for this resource (e.g., "doid").
            resource_name: Human-readable name (defaults to resource_id upper-cased).
            limit: Maximum number of entities to extract.
            iri_filter: IRI substring to filter entities (e.g., "DOID_", "Orphanet_").
            skip_deprecated: Skip deprecated/obsolete entities.

        Returns:
            ResourceSnapshot with extracted entities, sorted by id.
        """
        path = Path(owl_path)
        self._store = self._load_owl(path)
        checksum = sha256(path.read_bytes()).hexdigest()
        version = self._ontology_annotation("versionIRI") or self._ontology_annotation("versionInfo") or "unknown"

        entities = self._extract_entities(iri_filter, skip_deprecated)
        if limit:
            entities = entities[:limit]

        return ResourceSnapshot(
            resource_id=resource_id,
            resource_name=resource_name or resource_id.upper(),
            resource_url=str(path.absolute()),
            version=version,
            generated_at=datetime.now(timezone.utc),
            checksum=checksum,
            entity_count=len(entities),
            entities=entities,
        )

    def _ontology_annotation(self, prop: str) -> str | None:
        for row in self._rows(
            f"PREFIX owl: <http://www.w3.org/2002/07/owl#> SELECT ?v WHERE {{ ?o a owl:Ontology ; owl:{prop} ?v }}"
        ):
            return _value(row["v"])
        return None

    def _extract_entities(self, iri_filter: str | None, skip_deprecated: bool) -> list[EntitySnapshot]:
        """Read every property in one query each and assemble one EntitySnapshot per class."""
        keep = f"FILTER(isIRI(?e) && CONTAINS(STR(?e), '{iri_filter}'))" if iri_filter else "FILTER(isIRI(?e))"
        prefixes = (
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
            "PREFIX owl: <http://www.w3.org/2002/07/owl#> "
            f"PREFIX oio: <{OBO_IN_OWL}> "
        )

        labels: dict[str, str] = {}
        for row in self._rows(f"{prefixes} SELECT ?e ?l WHERE {{ ?e a owl:Class ; rdfs:label ?l . {keep} }}"):
            labels.setdefault(_value(row["e"]), _value(row["l"]))

        deprecated = {
            _value(row["e"])
            for row in self._rows(f"{prefixes} SELECT ?e ?d WHERE {{ ?e owl:deprecated ?d . {keep} }}")
            if _value(row["d"]).lower() == "true"
        }

        # Axiom annotations: synonym types and sources, xref precision, definition xrefs.
        syn_types: dict[tuple[str, str, str], str] = {}
        syn_sources: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        ecc: dict[tuple[str, str], str] = {}
        def_xrefs: dict[str, list[str]] = defaultdict(list)
        for row in self._rows(
            f"""{prefixes} SELECT ?e ?p ?t ?k ?v WHERE {{
                ?ax owl:annotatedSource ?e ; owl:annotatedProperty ?p ; owl:annotatedTarget ?t ; ?k ?v .
                FILTER(?k IN (oio:hasSynonymType, oio:source, oio:hasDbXref, <{ECC_PROPERTY}>))
                {keep} }}"""
        ):
            e, p, t, k, v = (_value(row[x]) for x in ("e", "p", "t", "k", "v"))
            if p in SYNONYM_PREDICATES:
                if k == f"{OBO_IN_OWL}hasSynonymType":
                    syn_types[(e, p, t)] = self._curie(v)
                elif k in (f"{OBO_IN_OWL}source", HAS_DBXREF):
                    syn_sources[(e, p, t)].append(v)
            elif p == HAS_DBXREF and k == ECC_PROPERTY:
                ecc[(e, t)] = _ecc_code(v)
            elif p in (IAO_DEFINITION, EFO_DEFINITION) and k == HAS_DBXREF:
                def_xrefs[e].append(v)

        synonyms: dict[str, list[Synonym]] = defaultdict(list)
        seen_syn: set[tuple[str, str, str]] = set()
        preds = " ".join(f"<{p}>" for p in SYNONYM_PREDICATES)
        for row in self._rows(f"{prefixes} SELECT ?e ?p ?v WHERE {{ VALUES ?p {{ {preds} }} ?e ?p ?v . {keep} }}"):
            e, p, v = _value(row["e"]), _value(row["p"]), _value(row["v"])
            key = (e, p, v)
            if key in seen_syn:
                continue
            seen_syn.add(key)
            orcids, pmids = extract_provenance_from_sources(syn_sources.get(key, []))
            synonyms[e].append(
                Synonym(
                    value=v,
                    scope=SYNONYM_PREDICATES[p],
                    type=syn_types.get(key),
                    orcids=orcids,
                    pmids=pmids,
                )
            )

        parents: dict[str, list[str]] = defaultdict(list)
        for row in self._rows(f"{prefixes} SELECT ?e ?p WHERE {{ ?e rdfs:subClassOf ?p . FILTER(isIRI(?p)) {keep} }}"):
            parents[_value(row["e"])].append(self._curie(_value(row["p"])))

        classification: dict[str, list[str]] = defaultdict(list)
        for row in self._rows(
            f"""{prefixes} SELECT ?e ?p WHERE {{
                ?e rdfs:subClassOf ?r . ?r owl:onProperty <{PART_OF}> ; owl:someValuesFrom ?p .
                FILTER(isIRI(?p)) {keep} }}"""
        ):
            classification[_value(row["e"])].append(self._curie(_value(row["p"])))

        definitions: dict[str, str] = {}
        for row in self._rows(
            f"{prefixes} SELECT ?e ?d WHERE {{ VALUES ?p {{ <{IAO_DEFINITION}> <{EFO_DEFINITION}> }} ?e ?p ?d . {keep} }}"
        ):
            definitions.setdefault(_value(row["e"]), _value(row["d"]))

        mappings: dict[str, list[EntityMapping]] = defaultdict(list)
        map_preds = " ".join(f"<{p}>" for p in MAPPING_PREDICATES)
        for row in self._rows(f"{prefixes} SELECT ?e ?p ?x WHERE {{ VALUES ?p {{ {map_preds} }} ?e ?p ?x . {keep} }}"):
            e, p, x = _value(row["e"]), _value(row["p"]), row["x"]
            raw = _value(x)
            target = self._curie(raw) if isinstance(x, px.NamedNode) else normalize_xref(raw)
            mappings[e].append(
                EntityMapping(object_id=target, predicate_id=MAPPING_PREDICATES[p], mapping_source=ecc.get((e, raw)))
            )

        entities: list[EntitySnapshot] = []
        for iri, label in labels.items():
            is_deprecated = iri in deprecated
            if skip_deprecated and is_deprecated:
                continue
            definition = definitions.get(iri)
            def_orcids, def_pmids = extract_provenance_from_sources(def_xrefs.get(iri, [])) if definition else ([], [])
            entities.append(
                EntitySnapshot(
                    id=self._curie(iri),
                    label=label,
                    synonyms=synonyms.get(iri, []),
                    definition=definition,
                    definition_orcids=def_orcids,
                    definition_pmids=def_pmids,
                    parents=sorted(set(parents.get(iri, []))),
                    classification_parents=sorted(set(classification.get(iri, []))),
                    mappings=mappings.get(iri, []),
                    deprecated=is_deprecated,
                )
            )
        entities.sort(key=lambda e: e.id)
        return entities


def extract_synonyms_sample(
    owl_path: str | Path,
    resource_id: str,
    count: int = 20,
) -> list[tuple[str, str, Synonym]]:
    """Extract a sample of exact synonyms from an OWL file.

    Returns list of (entity_curie, entity_label, synonym) tuples.
    """
    snapshot = OWLExtractor().extract(owl_path, resource_id)
    results: list[tuple[str, str, Synonym]] = []
    for entity in snapshot.entities or []:
        for syn in entity.synonyms or []:
            if syn.scope == SynonymScope.EXACT:
                results.append((entity.id, entity.label or "", syn))
                if len(results) >= count:
                    return results
    return results


def extract_hierarchy_sample(
    owl_path: str | Path,
    resource_id: str,
    count: int = 40,
) -> list[tuple[str, str, str, str]]:
    """Extract a sample of subClassOf relations from an OWL file.

    Returns list of (child_curie, child_label, parent_curie, parent_label) tuples.
    """
    snapshot = OWLExtractor().extract(owl_path, resource_id)
    labels = {e.id: e.label or e.id for e in (snapshot.entities or [])}
    results: list[tuple[str, str, str, str]] = []
    for entity in snapshot.entities or []:
        for parent_curie in entity.parents or []:
            results.append((entity.id, entity.label or entity.id, parent_curie, labels.get(parent_curie, parent_curie)))
            if len(results) >= count:
                return results
    return results
