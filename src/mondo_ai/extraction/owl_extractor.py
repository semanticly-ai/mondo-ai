"""OWL file extraction using pyoxigraph.

This module extracts labels, synonyms, definitions, and hierarchy
from OWL ontology files, including annotation provenance (ORCIDs, PMIDs).

Example:
    >>> from mondo_ai.extraction.owl_extractor import OWLExtractor
    >>> extractor = OWLExtractor()
    >>> snapshot = extractor.extract("path/to/ontology.owl", "doid")
"""

import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import curies
import pyoxigraph as px

from mondo_ai.datamodel import (
    EntitySnapshot,
    ResourceSnapshot,
    Synonym,
    SynonymScope,
)

# Patterns for extracting ORCIDs and PMIDs from annotation sources
ORCID_PATTERN = re.compile(r"ORCID:(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
PMID_PATTERN = re.compile(r"PMID:(\d+)", re.IGNORECASE)


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
    """
    orcids: list[str] = []
    pmids: list[str] = []

    for source in sources:
        # Extract ORCIDs
        for match in ORCID_PATTERN.finditer(source):
            orcid = match.group(1)
            if orcid not in orcids:
                orcids.append(orcid)

        # Extract PMIDs
        for match in PMID_PATTERN.finditer(source):
            pmid = match.group(1)
            if pmid not in pmids:
                pmids.append(pmid)

    return orcids, pmids

# OWL/RDF namespaces
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
OWL_DEPRECATED = "http://www.w3.org/2002/07/owl#deprecated"
OWL_ONTOLOGY = "http://www.w3.org/2002/07/owl#Ontology"
OWL_VERSION_IRI = "http://www.w3.org/2002/07/owl#versionIRI"
OWL_VERSION_INFO = "http://www.w3.org/2002/07/owl#versionInfo"
OWL_AXIOM = "http://www.w3.org/2002/07/owl#Axiom"
OWL_ANNOTATED_SOURCE = "http://www.w3.org/2002/07/owl#annotatedSource"
OWL_ANNOTATED_PROPERTY = "http://www.w3.org/2002/07/owl#annotatedProperty"
OWL_ANNOTATED_TARGET = "http://www.w3.org/2002/07/owl#annotatedTarget"
OBO_IN_OWL = "http://www.geneontology.org/formats/oboInOwl#"
IAO_DEFINITION = "http://purl.obolibrary.org/obo/IAO_0000115"
SKOS_EXACT_MATCH = "http://www.w3.org/2004/02/skos/core#exactMatch"
SKOS_CLOSE_MATCH = "http://www.w3.org/2004/02/skos/core#closeMatch"
HAS_DBXREF = f"{OBO_IN_OWL}hasDbXref"
OIO_SOURCE = f"{OBO_IN_OWL}source"

# EFO namespace (used by ORDO)
EFO = "http://www.ebi.ac.uk/efo/"

# Synonym predicates
SYNONYM_PREDICATES = {
    f"{OBO_IN_OWL}hasExactSynonym": SynonymScope.EXACT,
    f"{OBO_IN_OWL}hasNarrowSynonym": SynonymScope.NARROW,
    f"{OBO_IN_OWL}hasBroadSynonym": SynonymScope.BROAD,
    f"{OBO_IN_OWL}hasRelatedSynonym": SynonymScope.RELATED,
    # EFO alternative_term used by ORDO (treat as EXACT)
    f"{EFO}alternative_term": SynonymScope.EXACT,
}


def _get_converter() -> curies.Converter:
    """Get a CURIE converter with standard biomedical prefixes.

    Uses the Bioregistry as the source of prefix mappings.
    """
    return curies.get_obo_converter()


def _curie_from_iri(iri: str, converter: curies.Converter | None = None) -> str:
    """Convert IRI to CURIE format using the curies library.

    >>> _curie_from_iri("http://purl.obolibrary.org/obo/DOID_4")
    'DOID:4'
    >>> _curie_from_iri("http://purl.obolibrary.org/obo/MONDO_0000001")
    'MONDO:0000001'
    """
    if converter is None:
        converter = _get_converter()

    # Try to compress using the curies converter
    curie = converter.compress(iri)
    if curie:
        return curie

    # Fallback: Handle special cases not in standard converter
    # OMIM entry format (https://omim.org/entry/...)
    if "omim.org/entry/" in iri:
        local = iri.split("/entry/")[-1]
        return f"OMIM:{local}"
    # OMIM phenotypic series format
    if "omim.org/phenotypicSeries/PS" in iri:
        local = iri.split("/phenotypicSeries/PS")[-1]
        return f"OMIMPS:{local}"
    # Orphanet (http://www.orpha.net/ORDO/Orphanet_...)
    if "Orphanet_" in iri:
        local = iri.split("Orphanet_")[-1]
        return f"Orphanet:{local}"
    # OBO PURLs not handled by converter
    if "/obo/" in iri:
        parts = iri.split("/obo/")[-1]
        if "_" in parts:
            prefix, local = parts.rsplit("_", 1)
            return f"{prefix}:{local}"
    # Fallback: return original IRI
    return iri


class OWLExtractor:
    """Extract entities from OWL files using pyoxigraph."""

    def __init__(self) -> None:
        """Initialize the extractor."""
        self._store: px.Store | None = None
        self._converter: curies.Converter | None = None

    def _get_curie_converter(self) -> curies.Converter:
        """Get or create the CURIE converter."""
        if self._converter is None:
            self._converter = _get_converter()
        return self._converter

    def _load_owl(self, path: Path) -> px.Store:
        """Load OWL file into pyoxigraph store."""
        store = px.Store()
        # Try RDF/XML first, then Turtle
        content = path.read_bytes()
        try:
            store.load(content, format=px.RdfFormat.RDF_XML)
        except Exception:
            try:
                store.load(content, format=px.RdfFormat.TURTLE)
            except Exception:
                # Try N-Triples as fallback
                store.load(content, format=px.RdfFormat.N_TRIPLES)
        return store

    def _query(self, sparql: str, variables: list[str] | None = None) -> Iterator[dict]:
        """Execute SPARQL query and yield results as dicts.

        Args:
            sparql: SPARQL query string.
            variables: List of variable names to extract (without ?).
        """
        if self._store is None:
            raise RuntimeError("Store not loaded")

        # Extract variable names from SELECT clause if not provided
        if variables is None:
            import re
            match = re.search(r"SELECT\s+(?:DISTINCT\s+)?(.*?)\s+WHERE", sparql, re.IGNORECASE)
            if match:
                var_str = match.group(1)
                variables = [v.strip().lstrip("?") for v in var_str.split() if v.startswith("?")]
            else:
                variables = []

        result = self._store.query(sparql)
        assert isinstance(result, px.QuerySolutions)  # SELECT queries yield solutions
        for solution in result:
            row = {}
            for var in variables:
                try:
                    row[var] = solution[var]
                except KeyError:
                    row[var] = None
            yield row

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
            owl_path: Path to the OWL file.
            resource_id: Identifier for this resource (e.g., "doid").
            resource_name: Human-readable name (defaults to resource_id).
            limit: Maximum number of entities to extract.
            iri_filter: IRI substring to filter entities (e.g., "DOID_", "Orphanet_").
            skip_deprecated: Skip deprecated/obsolete entities.

        Returns:
            ResourceSnapshot with extracted entities.
        """
        path = Path(owl_path)
        self._store = self._load_owl(path)

        # Calculate checksum
        checksum = sha256(path.read_bytes()).hexdigest()

        # Extract version from ontology
        version = self._get_version_iri() or self._get_version_info() or "unknown"

        entities = list(self._extract_entities(
            limit=limit,
            iri_filter=iri_filter,
            skip_deprecated=skip_deprecated,
        ))

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

    def _extract_entities(
        self,
        limit: int | None = None,
        iri_filter: str | None = None,
        skip_deprecated: bool = True,
    ) -> Iterator[EntitySnapshot]:
        """Extract all entities with their properties.

        Args:
            limit: Maximum number of entities to extract.
            iri_filter: IRI substring to filter entities.
            skip_deprecated: Skip deprecated/obsolete entities.
        """
        # Build query with optional IRI filter
        filter_clause = "FILTER(isIRI(?entity))"
        if iri_filter:
            filter_clause = f"FILTER(isIRI(?entity) && CONTAINS(STR(?entity), '{iri_filter}'))"

        query = f"""
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>

        SELECT DISTINCT ?entity ?label WHERE {{
            ?entity a owl:Class .
            ?entity rdfs:label ?label .
            {filter_clause}
        }}
        """
        # Note: We don't apply LIMIT here, as we filter after checking deprecated status
        # Instead we'll extract more and filter

        entity_labels: dict[str, str] = {}
        for row in self._query(query):
            entity_node = row["entity"]
            # Extract IRI value from NamedNode
            if hasattr(entity_node, "value"):
                entity_iri = entity_node.value
            else:
                entity_iri = str(entity_node)
                # Remove angle brackets if present
                if entity_iri.startswith("<") and entity_iri.endswith(">"):
                    entity_iri = entity_iri[1:-1]

            label_node = row["label"]
            if hasattr(label_node, "value"):
                label = label_node.value
            else:
                label = str(label_node)
            entity_labels[entity_iri] = label

        # Process each entity
        count = 0
        converter = self._get_curie_converter()
        for entity_iri, label in entity_labels.items():
            # Check if deprecated first (before doing expensive work)
            deprecated = self._is_deprecated(entity_iri)
            if skip_deprecated and deprecated:
                continue

            curie = _curie_from_iri(entity_iri, converter)

            # Get synonyms (with provenance)
            synonyms = list(self._get_synonyms(entity_iri))

            # Get parents
            parents = list(self._get_parents(entity_iri, converter))

            # Get definition with provenance
            definition = self._get_definition(entity_iri)
            def_orcids = None
            def_pmids = None
            if definition:
                # Get dbxrefs from definition axiom (often contain PMIDs)
                def_xrefs = self._get_definition_dbxrefs(entity_iri)
                def_orcids, def_pmids = extract_provenance_from_sources(def_xrefs)
                def_orcids = def_orcids if def_orcids else None
                def_pmids = def_pmids if def_pmids else None

            yield EntitySnapshot(
                id=curie,
                label=label,
                synonyms=synonyms,
                definition=definition,
                definition_orcids=def_orcids,
                definition_pmids=def_pmids,
                parents=parents,
                mappings=[],
                deprecated=deprecated,
            )

            count += 1
            if limit and count >= limit:
                return

    def _get_annotation_sources(
        self, entity_iri: str, property_iri: str, target_value: str
    ) -> list[str]:
        """Get source annotations for a specific axiom.

        Finds reified axiom annotations where:
        - owl:annotatedSource = entity_iri
        - owl:annotatedProperty = property_iri
        - owl:annotatedTarget = target_value
        - oboInOwl:source = ?source

        Args:
            entity_iri: The entity IRI.
            property_iri: The property IRI (e.g., hasExactSynonym).
            target_value: The annotation target value.

        Returns:
            List of source annotation values.
        """
        # Escape quotes in target value for SPARQL
        escaped_target = target_value.replace("\\", "\\\\").replace('"', '\\"')

        query = f"""
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        PREFIX oboInOwl: <http://www.geneontology.org/formats/oboInOwl#>

        SELECT ?source WHERE {{
            ?axiom a owl:Axiom .
            ?axiom owl:annotatedSource <{entity_iri}> .
            ?axiom owl:annotatedProperty <{property_iri}> .
            ?axiom owl:annotatedTarget "{escaped_target}" .
            ?axiom oboInOwl:source ?source .
        }}
        """
        sources = []
        for row in self._query(query):
            source = row["source"]
            if hasattr(source, "value"):
                source = source.value
            if source:
                sources.append(str(source))
        return sources

    def _get_definition_dbxrefs(self, entity_iri: str) -> list[str]:
        """Get database xrefs from definition axiom annotations.

        Args:
            entity_iri: The entity IRI.

        Returns:
            List of dbxref values from the definition axiom.
        """
        query = f"""
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        PREFIX oboInOwl: <http://www.geneontology.org/formats/oboInOwl#>
        PREFIX IAO: <http://purl.obolibrary.org/obo/IAO_>

        SELECT ?xref WHERE {{
            ?axiom a owl:Axiom .
            ?axiom owl:annotatedSource <{entity_iri}> .
            ?axiom owl:annotatedProperty IAO:0000115 .
            ?axiom oboInOwl:hasDbXref ?xref .
        }}
        """
        xrefs = []
        for row in self._query(query):
            xref = row["xref"]
            if hasattr(xref, "value"):
                xref = xref.value
            if xref:
                xrefs.append(str(xref))
        return xrefs

    def _get_synonyms(self, entity_iri: str) -> Iterator[Synonym]:
        """Get all synonyms for an entity with provenance.

        Extracts synonyms and their annotation sources, parsing ORCIDs
        and PMIDs from the sources.
        """
        for pred_iri, scope in SYNONYM_PREDICATES.items():
            query = f"""
            SELECT ?value WHERE {{
                <{entity_iri}> <{pred_iri}> ?value .
            }}
            """
            for row in self._query(query):
                value = row["value"]
                if hasattr(value, "value"):
                    value = value.value
                value_str = str(value)

                # Get source annotations for this synonym
                sources = self._get_annotation_sources(entity_iri, pred_iri, value_str)
                orcids, pmids = extract_provenance_from_sources(sources)

                yield Synonym(
                    value=value_str,
                    scope=scope,
                    orcids=orcids if orcids else None,
                    pmids=pmids if pmids else None,
                )

    def _get_parents(
        self, entity_iri: str, converter: curies.Converter | None = None
    ) -> Iterator[str]:
        """Get direct parent classes."""
        query = f"""
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?parent WHERE {{
            <{entity_iri}> rdfs:subClassOf ?parent .
            FILTER(isIRI(?parent))
        }}
        """
        for row in self._query(query):
            parent_node = row["parent"]
            if hasattr(parent_node, "value"):
                parent_iri = parent_node.value
            else:
                parent_iri = str(parent_node)
            yield _curie_from_iri(parent_iri, converter)

    def _get_definition(self, entity_iri: str) -> str | None:
        """Get definition for an entity."""
        query = f"""
        SELECT ?def WHERE {{
            <{entity_iri}> <{IAO_DEFINITION}> ?def .
        }}
        """
        for row in self._query(query):
            value = row["def"]
            if hasattr(value, "value"):
                return value.value
            return str(value)
        return None

    def _is_deprecated(self, entity_iri: str) -> bool:
        """Check if entity is deprecated."""
        query = f"""
        SELECT ?dep WHERE {{
            <{entity_iri}> <{OWL_DEPRECATED}> ?dep .
        }}
        """
        for row in self._query(query):
            value = row["dep"]
            if hasattr(value, "value"):
                return value.value == "true"
            return str(value).lower() == "true"
        return False

    def _get_version_iri(self) -> str | None:
        """Extract the owl:versionIRI from the ontology.

        Returns:
            The versionIRI as a string, or None if not found.
        """
        query = """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT ?versionIRI WHERE {
            ?ont a owl:Ontology .
            ?ont owl:versionIRI ?versionIRI .
        }
        """
        for row in self._query(query):
            version_node = row["versionIRI"]
            if hasattr(version_node, "value"):
                return version_node.value
            return str(version_node)
        return None

    def _get_version_info(self) -> str | None:
        """Extract the owl:versionInfo from the ontology (fallback).

        Returns:
            The versionInfo string, or None if not found.
        """
        query = """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT ?versionInfo WHERE {
            ?ont a owl:Ontology .
            ?ont owl:versionInfo ?versionInfo .
        }
        """
        for row in self._query(query):
            version_node = row["versionInfo"]
            if hasattr(version_node, "value"):
                return version_node.value
            return str(version_node)
        return None


def extract_synonyms_sample(
    owl_path: str | Path,
    resource_id: str,
    count: int = 20,
) -> list[tuple[str, str, Synonym]]:
    """Extract a sample of synonyms from an OWL file.

    Returns list of (entity_curie, entity_label, synonym) tuples.

    Example:
        >>> # synonyms = extract_synonyms_sample("doid.owl", "doid", count=20)
    """
    extractor = OWLExtractor()
    snapshot = extractor.extract(owl_path, resource_id, limit=count * 5)

    results: list[tuple[str, str, Synonym]] = []
    for entity in snapshot.entities or []:
        if entity.synonyms:
            for syn in entity.synonyms:
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
    extractor = OWLExtractor()
    snapshot = extractor.extract(owl_path, resource_id, limit=count * 3)

    # Build label lookup
    labels = {e.id: e.label or e.id for e in (snapshot.entities or [])}

    results: list[tuple[str, str, str, str]] = []
    for entity in snapshot.entities or []:
        if entity.parents:
            for parent_curie in entity.parents:
                parent_label = labels.get(parent_curie, parent_curie)
                results.append((
                    entity.id,
                    entity.label or entity.id,
                    parent_curie,
                    parent_label,
                ))
                if len(results) >= count:
                    return results
    return results
