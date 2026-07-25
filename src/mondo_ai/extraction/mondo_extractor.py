"""Mondo-specific extraction with evidence provenance.

This module extracts entities from Mondo with rich provenance:
- Organizational decision (consortium inclusion)
- Expert determination (ORCID in source)
- Candidate literature (PMID in source)

Example:
    >>> from mondo_ai.extraction.mondo_extractor import MondoExtractor
    >>> extractor = MondoExtractor()
    >>> snapshot = extractor.extract("path/to/mondo.owl")  # doctest: +SKIP
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import curies
import pyoxigraph as px

from mondo_ai.datamodel import (
    Agent,
    AgentContribution,
    Coding,
    ContributionType,
    EntitySnapshot,
    EvidenceDirection,
    EvidenceItemType,
    EvidenceStrength,
    ResourceSnapshot,
    SieveDocument,
    SieveEvidenceLine,
    Synonym,
    SynonymScope,
    TrustLevel,
)

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
HAS_DBXREF = f"{OBO_IN_OWL}hasDbXref"
SOURCE = f"{OBO_IN_OWL}source"

# Synonym predicates
SYNONYM_PREDICATES = {
    f"{OBO_IN_OWL}hasExactSynonym": SynonymScope.EXACT,
    f"{OBO_IN_OWL}hasNarrowSynonym": SynonymScope.NARROW,
    f"{OBO_IN_OWL}hasBroadSynonym": SynonymScope.BROAD,
    f"{OBO_IN_OWL}hasRelatedSynonym": SynonymScope.RELATED,
}

# Patterns for extracting ORCIDs and PMIDs
ORCID_PATTERN = re.compile(r"ORCID:(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
PMID_PATTERN = re.compile(r"PMID:(\d+)", re.IGNORECASE)

# Mondo consortium agent
MONDO_CONSORTIUM = Agent(
    id="mondo:consortium",
    type="Agent",
    name="Mondo Disease Ontology Consortium",
    subtype="organization",
)


@dataclass
class AnnotationProvenance:
    """Provenance information extracted from annotation sources.

    Attributes:
        orcids: List of ORCID identifiers found.
        pmids: List of PMID identifiers found.
        raw_sources: Original source strings.
    """

    orcids: list[str] = field(default_factory=list)
    pmids: list[str] = field(default_factory=list)
    raw_sources: list[str] = field(default_factory=list)


def _get_converter() -> curies.Converter:
    """Get a CURIE converter with standard biomedical prefixes."""
    return curies.get_obo_converter()


def _curie_from_iri(iri: str, converter: curies.Converter | None = None) -> str:
    """Convert IRI to CURIE format."""
    if converter is None:
        converter = _get_converter()

    curie = converter.compress(iri)
    if curie:
        return curie

    # Fallback for OBO PURLs
    if "/obo/" in iri:
        parts = iri.split("/obo/")[-1]
        if "_" in parts:
            prefix, local = parts.rsplit("_", 1)
            return f"{prefix}:{local}"
    return iri


def create_consortium_evidence_line(
    entity_id: str,
    version: str,
) -> SieveEvidenceLine:
    """Create evidence line for Mondo consortium organizational decision.

    Args:
        entity_id: The Mondo entity ID (e.g., MONDO:0005015).
        version: The Mondo version/release.

    Returns:
        SieveEvidenceLine with AgentContribution for consortium decision.
    """
    contribution = AgentContribution(
        id=f"ev_consortium_{hash(entity_id) % 100000:05d}",
        type="AgentContribution",
        evidenceItemType=EvidenceItemType.AgentContribution,
        contributor=MONDO_CONSORTIUM,
        trustLevel=TrustLevel.authority,
        contributionType=ContributionType.decision,
        content="Term included in Mondo Disease Ontology",
        reference=version,
        description=f"Organizational decision: {entity_id} included in Mondo",
    )

    return SieveEvidenceLine(
        id=f"el_consortium_{hash(entity_id) % 100000:05d}",
        type="SieveEvidenceLine",
        hasEvidenceItems=[contribution],
        directionOfEvidenceProvided=EvidenceDirection.supports.value,
        strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        scoreOfEvidenceProvided=0.6,
    )


def create_expert_evidence_line(
    orcid: str,
    entity_id: str,
    annotation_type: str = "synonym",
) -> SieveEvidenceLine:
    """Create evidence line for expert curator determination.

    Args:
        orcid: The ORCID identifier (e.g., 0000-0001-5208-3432).
        entity_id: The entity this annotation is about.
        annotation_type: Type of annotation (synonym, definition, etc.).

    Returns:
        SieveEvidenceLine with AgentContribution for expert determination.
    """
    orcid_curie = f"ORCID:{orcid}"
    contribution = AgentContribution(
        id=f"ev_expert_{hash(orcid + entity_id) % 100000:05d}",
        type="AgentContribution",
        evidenceItemType=EvidenceItemType.AgentContribution,
        contributor=Agent(
            id=orcid_curie,
            type="Agent",
            subtype="person",
        ),
        trustLevel=TrustLevel.curator,
        contributionType=ContributionType.decision,
        content=f"Expert curator determination for {annotation_type}",
        description=f"Curator {orcid_curie} approved this {annotation_type}",
    )

    return SieveEvidenceLine(
        id=f"el_expert_{hash(orcid + entity_id) % 100000:05d}",
        type="SieveEvidenceLine",
        hasEvidenceItems=[contribution],
        directionOfEvidenceProvided=EvidenceDirection.supports.value,
        strengthOfEvidenceProvided=EvidenceStrength.strong.value,
        scoreOfEvidenceProvided=0.8,
    )


def create_candidate_literature_evidence_line(
    pmid: str,
    entity_id: str,
) -> SieveEvidenceLine:
    """Create evidence line for candidate (unverified) literature reference.

    Args:
        pmid: The PubMed ID (just the number).
        entity_id: The entity this annotation is about.

    Returns:
        SieveEvidenceLine with SieveDocument marked as candidate.
    """
    # Use subtype Coding to mark as candidate/unverified
    candidate_subtype = Coding(
        code="candidate_reference",
        label="Candidate Literature Reference",
        system="mondo:evidence_status",
    )

    document = SieveDocument(
        id=f"ev_lit_{pmid}_{hash(entity_id) % 100000:05d}",
        type="SieveDocument",
        pmid=pmid,
        subtype=candidate_subtype,
        urls=[f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"],
        description=f"Candidate reference PMID:{pmid} (cited in source, not yet verified)",
    )

    return SieveEvidenceLine(
        id=f"el_lit_{pmid}_{hash(entity_id) % 100000:05d}",
        type="SieveEvidenceLine",
        hasEvidenceItems=[document],
        directionOfEvidenceProvided=EvidenceDirection.supports.value,
        strengthOfEvidenceProvided=EvidenceStrength.weak.value,
        scoreOfEvidenceProvided=0.4,
    )


def extract_provenance_from_sources(sources: list[str]) -> AnnotationProvenance:
    """Extract ORCIDs and PMIDs from annotation source strings.

    Args:
        sources: List of source annotation values.

    Returns:
        AnnotationProvenance with extracted identifiers.

    Example:
        >>> sources = ["ORCID:0000-0001-5208-3432", "PMID:12345678"]
        >>> prov = extract_provenance_from_sources(sources)
        >>> prov.orcids
        ['0000-0001-5208-3432']
        >>> prov.pmids
        ['12345678']
    """
    provenance = AnnotationProvenance(raw_sources=sources)

    for source in sources:
        # Extract ORCIDs
        for match in ORCID_PATTERN.finditer(source):
            orcid = match.group(1)
            if orcid not in provenance.orcids:
                provenance.orcids.append(orcid)

        # Extract PMIDs
        for match in PMID_PATTERN.finditer(source):
            pmid = match.group(1)
            if pmid not in provenance.pmids:
                provenance.pmids.append(pmid)

    return provenance


class MondoExtractor:
    """Extract entities from Mondo with rich provenance."""

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
        content = path.read_bytes()
        try:
            store.load(content, format=px.RdfFormat.RDF_XML)
        except Exception:
            try:
                store.load(content, format=px.RdfFormat.TURTLE)
            except Exception:
                store.load(content, format=px.RdfFormat.N_TRIPLES)
        return store

    def _query(self, sparql: str, variables: list[str] | None = None) -> Iterator[dict]:
        """Execute SPARQL query and yield results as dicts."""
        if self._store is None:
            raise RuntimeError("Store not loaded")

        if variables is None:
            import re

            match = re.search(
                r"SELECT\s+(?:DISTINCT\s+)?(.*?)\s+WHERE", sparql, re.IGNORECASE
            )
            if match:
                var_str = match.group(1)
                variables = [
                    v.strip().lstrip("?") for v in var_str.split() if v.startswith("?")
                ]
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

    def _get_value(self, node) -> str | None:
        """Extract string value from RDF node."""
        if node is None:
            return None
        if hasattr(node, "value"):
            return node.value
        return str(node)

    def _get_version_iri(self) -> str | None:
        """Extract the owl:versionIRI from the ontology."""
        query = """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT ?versionIRI WHERE {
            ?ont a owl:Ontology .
            ?ont owl:versionIRI ?versionIRI .
        }
        """
        for row in self._query(query):
            return self._get_value(row["versionIRI"])
        return None

    def _get_version_info(self) -> str | None:
        """Extract the owl:versionInfo from the ontology (fallback)."""
        query = """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT ?versionInfo WHERE {
            ?ont a owl:Ontology .
            ?ont owl:versionInfo ?versionInfo .
        }
        """
        for row in self._query(query):
            return self._get_value(row["versionInfo"])
        return None

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
        escaped_target = target_value.replace('"', '\\"')

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
            source = self._get_value(row["source"])
            if source:
                sources.append(source)
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
            xref = self._get_value(row["xref"])
            if xref:
                xrefs.append(xref)
        return xrefs

    def extract(
        self,
        owl_path: str | Path,
        limit: int | None = None,
        skip_deprecated: bool = True,
    ) -> ResourceSnapshot:
        """Extract entities from Mondo OWL file.

        Args:
            owl_path: Path to the Mondo OWL file.
            limit: Maximum number of entities to extract.
            skip_deprecated: Skip deprecated/obsolete entities.

        Returns:
            ResourceSnapshot with extracted entities.
        """
        path = Path(owl_path)
        self._store = self._load_owl(path)

        checksum = sha256(path.read_bytes()).hexdigest()
        version = self._get_version_iri() or self._get_version_info() or "unknown"

        entities = list(
            self._extract_entities(
                version=version,
                limit=limit,
                skip_deprecated=skip_deprecated,
            )
        )

        return ResourceSnapshot(
            resource_id="mondo",
            resource_name="Mondo Disease Ontology",
            resource_url=str(path.absolute()),
            version=version,
            generated_at=datetime.now(timezone.utc),
            checksum=checksum,
            entity_count=len(entities),
            entities=entities,
        )

    def _iter_entities_with_evidence(
        self,
        version: str,
        limit: int | None = None,
        skip_deprecated: bool = True,
    ) -> Iterator[tuple[EntitySnapshot, list[SieveEvidenceLine]]]:
        """Extract entities together with their evidence lines.

        Single source of truth for Mondo entity extraction. Callers that only
        need the snapshot (``extract``) discard the evidence; callers that need
        provenance (``extract_mondo_with_evidence``) keep it.
        """
        query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>

        SELECT DISTINCT ?entity ?label WHERE {
            ?entity a owl:Class .
            ?entity rdfs:label ?label .
            FILTER(isIRI(?entity) && CONTAINS(STR(?entity), "MONDO_"))
        }
        """

        entity_labels: dict[str, str] = {}
        for row in self._query(query):
            entity_iri = self._get_value(row["entity"])
            label = self._get_value(row["label"])
            if entity_iri and label:
                entity_labels[entity_iri] = label

        count = 0
        converter = self._get_curie_converter()

        for entity_iri, label in entity_labels.items():
            if skip_deprecated and self._is_deprecated(entity_iri):
                continue

            curie = _curie_from_iri(entity_iri, converter)

            # Get synonyms with provenance
            synonyms = list(self._get_synonyms_with_provenance(entity_iri, curie))

            # Get parents
            parents = list(self._get_parents(entity_iri, converter))

            # Get definition with provenance
            definition, def_provenance = self._get_definition_with_provenance(
                entity_iri
            )

            entity = EntitySnapshot(
                id=curie,
                label=label,
                synonyms=[s for s, _ in synonyms],
                definition=definition,
                parents=parents,
                mappings=[],
                deprecated=False,
            )
            evidence_lines = self._build_entity_evidence_lines(
                entity_id=curie,
                version=version,
                synonyms=synonyms,
                definition_provenance=def_provenance,
            )
            yield entity, evidence_lines

            count += 1
            if limit and count >= limit:
                return

    def _extract_entities(
        self,
        version: str,
        limit: int | None = None,
        skip_deprecated: bool = True,
    ) -> Iterator[EntitySnapshot]:
        """Extract entities as snapshots (evidence lines are discarded here)."""
        for entity, _ in self._iter_entities_with_evidence(
            version=version, limit=limit, skip_deprecated=skip_deprecated
        ):
            yield entity

    def _get_synonyms_with_provenance(
        self, entity_iri: str, entity_id: str
    ) -> Iterator[tuple[Synonym, AnnotationProvenance]]:
        """Get synonyms with their source provenance."""
        for pred_iri, scope in SYNONYM_PREDICATES.items():
            query = f"""
            SELECT ?value WHERE {{
                <{entity_iri}> <{pred_iri}> ?value .
            }}
            """
            for row in self._query(query):
                value = self._get_value(row["value"])
                if not value:
                    continue

                # Get source annotations for this synonym axiom
                sources = self._get_annotation_sources(entity_iri, pred_iri, value)
                provenance = extract_provenance_from_sources(sources)

                synonym = Synonym(value=value, scope=scope)
                yield synonym, provenance

    def _get_definition_with_provenance(
        self, entity_iri: str
    ) -> tuple[str | None, AnnotationProvenance]:
        """Get definition with its source provenance."""
        query = f"""
        SELECT ?def WHERE {{
            <{entity_iri}> <{IAO_DEFINITION}> ?def .
        }}
        """
        definition = None
        for row in self._query(query):
            definition = self._get_value(row["def"])
            break

        # Get dbxrefs from definition axiom (often contain PMIDs)
        xrefs = self._get_definition_dbxrefs(entity_iri)
        provenance = extract_provenance_from_sources(xrefs)

        return definition, provenance

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
            parent_iri = self._get_value(row["parent"])
            if parent_iri:
                yield _curie_from_iri(parent_iri, converter)

    def _is_deprecated(self, entity_iri: str) -> bool:
        """Check if entity is deprecated."""
        query = f"""
        SELECT ?dep WHERE {{
            <{entity_iri}> <{OWL_DEPRECATED}> ?dep .
        }}
        """
        for row in self._query(query):
            value = self._get_value(row["dep"])
            if value:
                return value.lower() == "true"
        return False

    def _build_entity_evidence_lines(
        self,
        entity_id: str,
        version: str,
        synonyms: list[tuple[Synonym, AnnotationProvenance]],
        definition_provenance: AnnotationProvenance,
    ) -> list[SieveEvidenceLine]:
        """Build all evidence lines for an entity.

        Args:
            entity_id: The Mondo entity ID.
            version: The Mondo version.
            synonyms: List of (synonym, provenance) tuples.
            definition_provenance: Provenance from definition.

        Returns:
            List of evidence lines for this entity.
        """
        lines: list[SieveEvidenceLine] = []

        # 1. Consortium decision (always present)
        lines.append(create_consortium_evidence_line(entity_id, version))

        # Collect all unique ORCIDs and PMIDs
        all_orcids: set[str] = set()
        all_pmids: set[str] = set()

        for _, prov in synonyms:
            all_orcids.update(prov.orcids)
            all_pmids.update(prov.pmids)

        all_orcids.update(definition_provenance.orcids)
        all_pmids.update(definition_provenance.pmids)

        # 2. Expert determinations (one per unique ORCID)
        for orcid in all_orcids:
            lines.append(create_expert_evidence_line(orcid, entity_id))

        # 3. Candidate literature (one per unique PMID)
        for pmid in all_pmids:
            lines.append(create_candidate_literature_evidence_line(pmid, entity_id))

        return lines


def extract_mondo_with_evidence(
    owl_path: str | Path,
    limit: int | None = None,
) -> tuple[ResourceSnapshot, dict[str, list[SieveEvidenceLine]]]:
    """Extract Mondo entities with their evidence lines.

    This is the main entry point for Mondo extraction with full provenance.

    Args:
        owl_path: Path to the Mondo OWL file.
        limit: Maximum number of entities to extract.

    Returns:
        Tuple of (ResourceSnapshot, dict mapping entity_id to evidence lines).

    Example:
        >>> # snapshot, evidence = extract_mondo_with_evidence("mondo.owl", limit=10)
    """
    extractor = MondoExtractor()
    extractor._store = extractor._load_owl(Path(owl_path))

    version = extractor._get_version_iri() or extractor._get_version_info() or "unknown"

    entities: list[EntitySnapshot] = []
    evidence_by_entity: dict[str, list[SieveEvidenceLine]] = {}

    for entity, evidence_lines in extractor._iter_entities_with_evidence(
        version=version, limit=limit
    ):
        entities.append(entity)
        evidence_by_entity[entity.id] = evidence_lines

    checksum = sha256(Path(owl_path).read_bytes()).hexdigest()

    snapshot = ResourceSnapshot(
        resource_id="mondo",
        resource_name="Mondo Disease Ontology",
        resource_url=str(Path(owl_path).absolute()),
        version=version,
        generated_at=datetime.now(timezone.utc),
        checksum=checksum,
        entity_count=len(entities),
        entities=entities,
    )

    return snapshot, evidence_by_entity
