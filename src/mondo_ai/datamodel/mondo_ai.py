from __future__ import annotations

import re
import sys
from datetime import (
    date,
    datetime,
    time
)
from decimal import Decimal
from enum import Enum
from typing import (
    Any,
    ClassVar,
    Literal,
    Optional,
    Union
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer
)


metamodel_version = "None"
version = "None"


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        serialize_by_alias = True,
        validate_by_name = True,
        validate_assignment = True,
        validate_default = True,
        extra = "forbid",
        arbitrary_types_allowed = True,
        use_enum_values = True,
        strict = False,
    )

    @model_serializer(mode='wrap', when_used='unless-none')
    def treat_empty_lists_as_none(
            self, handler: SerializerFunctionWrapHandler,
            info: SerializationInfo) -> dict[str, Any]:
        if info.exclude_none:
            _instance = self.model_copy()
            for field, field_info in type(_instance).model_fields.items():
                if getattr(_instance, field) == [] and not(
                        field_info.is_required()):
                    setattr(_instance, field, None)
        else:
            _instance = self
        return handler(_instance, info)



class LinkMLMeta(RootModel):
    root: dict[str, Any] = {}
    model_config = ConfigDict(frozen=True)

    def __getattr__(self, key:str):
        return getattr(self.root, key)

    def __getitem__(self, key:str):
        return self.root[key]

    def __setitem__(self, key:str, value):
        self.root[key] = value

    def __contains__(self, key:str) -> bool:
        return key in self.root


linkml_meta = LinkMLMeta({'default_prefix': 'mondoai',
     'default_range': 'string',
     'description': 'Data models for AI-assisted evidence-driven sync pipeline for '
                    'curating content into the Mondo disease ontology.',
     'id': 'https://w3id.org/monarch-initiative/mondo-ai',
     'imports': ['linkml:types'],
     'name': 'mondo_ai',
     'prefixes': {'DOID': {'prefix_prefix': 'DOID',
                           'prefix_reference': 'http://purl.obolibrary.org/obo/DOID_'},
                  'IAO': {'prefix_prefix': 'IAO',
                          'prefix_reference': 'http://purl.obolibrary.org/obo/IAO_'},
                  'MONDO': {'prefix_prefix': 'MONDO',
                            'prefix_reference': 'http://purl.obolibrary.org/obo/MONDO_'},
                  'OMIM': {'prefix_prefix': 'OMIM',
                           'prefix_reference': 'https://omim.org/entry/'},
                  'Orphanet': {'prefix_prefix': 'Orphanet',
                               'prefix_reference': 'http://www.orpha.net/ORDO/Orphanet_'},
                  'eco': {'prefix_prefix': 'eco',
                          'prefix_reference': 'http://purl.obolibrary.org/obo/ECO_'},
                  'linkml': {'prefix_prefix': 'linkml',
                             'prefix_reference': 'https://w3id.org/linkml/'},
                  'mondoai': {'prefix_prefix': 'mondoai',
                              'prefix_reference': 'https://w3id.org/monarch-initiative/mondo-ai/'},
                  'oio': {'prefix_prefix': 'oio',
                          'prefix_reference': 'http://www.geneontology.org/formats/oboInOwl#'},
                  'rdfs': {'prefix_prefix': 'rdfs',
                           'prefix_reference': 'http://www.w3.org/2000/01/rdf-schema#'},
                  'skos': {'prefix_prefix': 'skos',
                           'prefix_reference': 'http://www.w3.org/2004/02/skos/core#'}},
     'source_file': 'src/mondo_ai/schema/mondo_ai.yaml',
     'title': 'MondoAI Data Model'} )

class SynonymScope(str, Enum):
    """
    Scope of a synonym assertion.
    """
    EXACT = "EXACT"
    """
    Exact synonym (oio:hasExactSynonym)
    """
    NARROW = "NARROW"
    """
    Narrow synonym (oio:hasNarrowSynonym)
    """
    BROAD = "BROAD"
    """
    Broad synonym (oio:hasBroadSynonym)
    """
    RELATED = "RELATED"
    """
    Related synonym (oio:hasRelatedSynonym)
    """


class StatementPredicate(str, Enum):
    """
    Type of statement being asserted.
    """
    EXACT_SYNONYM = "EXACT_SYNONYM"
    """
    Exact synonym assertion
    """
    NARROW_SYNONYM = "NARROW_SYNONYM"
    """
    Narrow synonym assertion
    """
    BROAD_SYNONYM = "BROAD_SYNONYM"
    """
    Broad synonym assertion
    """
    RELATED_SYNONYM = "RELATED_SYNONYM"
    """
    Related synonym assertion
    """
    LABEL = "LABEL"
    """
    Primary label assertion
    """
    DEFINITION = "DEFINITION"
    """
    Definition text assertion
    """
    PARENT = "PARENT"
    """
    rdfs:subClassOf parent assertion
    """


class PacketBucket(str, Enum):
    """
    Classification of evidence packet relative to Mondo.
    """
    NEW = "NEW"
    """
    Statement not currently present in Mondo
    """
    CONFIRM = "CONFIRM"
    """
    Statement already present in Mondo
    """


class EvidenceType(str, Enum):
    """
    Type of evidence (determines concrete class).
    """
    CONCORDANCE = "CONCORDANCE"
    """
    Cross-ontology/source agreement
    """
    LITERATURE = "LITERATURE"
    """
    Published literature evidence
    """
    COMPUTATIONAL = "COMPUTATIONAL"
    """
    Algorithmically computed evidence
    """


class EvidenceDirection(str, Enum):
    """
    Whether evidence supports or contradicts the statement.
    """
    SUPPORTS = "SUPPORTS"
    """
    Evidence supports the statement
    """
    CONTRADICTS = "CONTRADICTS"
    """
    Evidence contradicts the statement
    """
    UNCERTAIN = "UNCERTAIN"
    """
    Direction is uncertain or neutral
    """


class CurationStatus(str, Enum):
    """
    Status of curation review.
    """
    UNREVIEWED = "UNREVIEWED"
    """
    Not yet reviewed (default)
    """
    ACCEPTED = "ACCEPTED"
    """
    Approved by curator
    """
    REJECTED = "REJECTED"
    """
    Rejected by curator
    """
    CONTROVERSIAL = "CONTROVERSIAL"
    """
    Conflicting opinions, requires discussion
    """


class RecommendedAction(str, Enum):
    """
    AI-recommended action for a statement.
    """
    ADD = "ADD"
    """
    Recommend adding this statement to Mondo
    """
    CONFIRM = "CONFIRM"
    """
    Confirm existing assertion is correct
    """
    REJECT = "REJECT"
    """
    Recommend rejecting this statement
    """
    REVIEW = "REVIEW"
    """
    Requires human review (uncertain)
    """


class RunStatus(str, Enum):
    """
    Status of a pipeline run.
    """
    RUNNING = "RUNNING"
    """
    Run is currently in progress
    """
    COMPLETED = "COMPLETED"
    """
    Run completed successfully
    """
    FAILED = "FAILED"
    """
    Run failed with error
    """
    CANCELLED = "CANCELLED"
    """
    Run was cancelled
    """



class ResourceSnapshot(ConfiguredBaseModel):
    """
    Extracted content from a source ontology at a specific version.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    resource_id: str = Field(default=..., description="""Unique identifier for this resource (e.g., \"omim\", \"ordo\", \"doid\")""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    resource_name: Optional[str] = Field(default=None, description="""Human-readable name of the resource""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    resource_url: Optional[str] = Field(default=None, description="""URL where the resource was downloaded from""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    version: Optional[str] = Field(default=None, description="""Version identifier of the source""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    generated_at: Optional[datetime ] = Field(default=None, description="""Timestamp when this snapshot was generated""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot', 'AssertionProvenance', 'EvidenceSynthesis']} })
    checksum: Optional[str] = Field(default=None, description="""SHA256 checksum of the source file""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    entity_count: Optional[int] = Field(default=None, description="""Number of entities in this snapshot""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })
    entities: Optional[list[EntitySnapshot]] = Field(default=[], description="""List of extracted entities""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot']} })


class EntitySnapshot(ConfiguredBaseModel):
    """
    Extracted entity data from a source ontology.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    id: str = Field(default=..., description="""Source entity identifier (e.g., OMIM:123456)""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    label: Optional[str] = Field(default=None, description="""Primary label/name of the entity""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })
    synonyms: Optional[list[Synonym]] = Field(default=[], description="""List of synonyms for this entity""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })
    definition: Optional[str] = Field(default=None, description="""Definition text""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })
    parents: Optional[list[str]] = Field(default=[], description="""Direct parent entity IDs (rdfs:subClassOf)""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })
    mappings: Optional[list[EntityMapping]] = Field(default=[], description="""Cross-references and mappings to other ontologies""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })
    deprecated: Optional[bool] = Field(default=None, description="""Whether this entity is deprecated/obsolete""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot']} })


class Synonym(ConfiguredBaseModel):
    """
    A synonym with scope and source information.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    value: str = Field(default=..., description="""The synonym text""", json_schema_extra = { "linkml_meta": {'domain_of': ['Synonym']} })
    scope: Optional[SynonymScope] = Field(default=None, description="""Scope of the synonym (exact, narrow, broad, related)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Synonym']} })
    type: Optional[str] = Field(default=None, description="""Type of synonym (e.g., acronym, abbreviation)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Synonym']} })
    source: Optional[str] = Field(default=None, description="""Source of this synonym assertion""", json_schema_extra = { "linkml_meta": {'domain_of': ['Synonym']} })


class EntityMapping(ConfiguredBaseModel):
    """
    A mapping/cross-reference to another ontology entity.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    object_id: str = Field(default=..., description="""Target entity ID""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntityMapping', 'CanonicalStatement']} })
    predicate_id: Optional[str] = Field(default=None, description="""Mapping predicate (e.g., skos:exactMatch)""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntityMapping']} })
    mapping_source: Optional[str] = Field(default=None, description="""Source of this mapping""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntityMapping']} })


class CanonicalStatement(ConfiguredBaseModel):
    """
    A statement about a Mondo entity derived from external sources. Statements are the unit of comparison and evidence gathering.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    statement_id: str = Field(default=..., description="""Deterministic ID (SHA256 hash of subject + predicate + object)""", json_schema_extra = { "linkml_meta": {'domain_of': ['CanonicalStatement']} })
    subject_mondo_id: str = Field(default=..., description="""Mondo entity ID this statement is about""", json_schema_extra = { "linkml_meta": {'domain_of': ['CanonicalStatement']} })
    subject_label: Optional[str] = Field(default=None, description="""Label of the Mondo entity""", json_schema_extra = { "linkml_meta": {'domain_of': ['CanonicalStatement']} })
    predicate: StatementPredicate = Field(default=..., description="""Type of statement (synonym, parent, etc.)""", json_schema_extra = { "linkml_meta": {'domain_of': ['CanonicalStatement']} })
    object_value: str = Field(default=..., description="""Value of the statement. For synonyms this is the synonym text, for hierarchy this is the parent label.""", json_schema_extra = { "linkml_meta": {'domain_of': ['CanonicalStatement']} })
    object_id: Optional[str] = Field(default=None, description="""For predicates with entity targets (like PARENT), this is the target entity's CURIE.""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntityMapping', 'CanonicalStatement']} })


class EvidencePacket(ConfiguredBaseModel):
    """
    A package of evidence for a candidate statement. This is the primary output unit of the MondoAI pipeline.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    id: str = Field(default=..., description="""Unique packet identifier""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    statement: CanonicalStatement = Field(default=..., description="""The statement this packet provides evidence for""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    bucket: PacketBucket = Field(default=..., description="""Whether this is NEW to Mondo or CONFIRMS existing""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    provenance: Optional[AssertionProvenance] = Field(default=None, description="""Origin and attribution of the assertion""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    evidence: Optional[list[Evidence]] = Field(default=[], description="""List of evidence items""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    evidence_score: Optional[float] = Field(default=None, description="""Net Evidence Ratio: (S+ - S-) / (S+ + S- + S?) Ranges from -1 (all contradicting) to +1 (all supporting)""", ge=-1, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    evidence_synthesis: Optional[EvidenceSynthesis] = Field(default=None, description="""AI-synthesized evidence synthesis""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    status: Optional[CurationStatus] = Field(default=CurationStatus.UNREVIEWED, description="""Curation status of this packet""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket', 'PipelineRun'],
         'ifabsent': 'string(UNREVIEWED)'} })
    curator_orcid: Optional[str] = Field(default=None, description="""ORCID of curator who reviewed this packet""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    created_at: Optional[datetime ] = Field(default=None, description="""When this packet was created""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })
    updated_at: Optional[datetime ] = Field(default=None, description="""When this packet was last updated""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket']} })


class AssertionProvenance(ConfiguredBaseModel):
    """
    Origin and attribution of the assertion.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    attributed_to: Optional[list[str]] = Field(default=[], description="""ORCIDs or identifiers of original creators""", json_schema_extra = { "linkml_meta": {'domain_of': ['AssertionProvenance']} })
    generated_at: Optional[date] = Field(default=None, description="""When the assertion was generated""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot', 'AssertionProvenance', 'EvidenceSynthesis']} })
    source_version: Optional[str] = Field(default=None, description="""Version identifier of source (e.g., \"OMIM 2024-05-01\")""", json_schema_extra = { "linkml_meta": {'domain_of': ['AssertionProvenance']} })
    source_uri: Optional[str] = Field(default=None, description="""URI of the source resource""", json_schema_extra = { "linkml_meta": {'domain_of': ['AssertionProvenance']} })


class Evidence(ConfiguredBaseModel):
    """
    Base class for all evidence types.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'abstract': True,
         'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    id: str = Field(default=..., description="""Unique evidence item identifier""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    evidence_type: EvidenceType = Field(default=..., description="""Type of evidence (CONCORDANCE, LITERATURE, COMPUTATIONAL)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    direction: Optional[EvidenceDirection] = Field(default=EvidenceDirection.SUPPORTS, description="""Whether this evidence supports or contradicts""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'string(SUPPORTS)'} })
    evidence_strength: Optional[float] = Field(default=1.0, description="""Strength of this evidence (0=weak, 1=strong)""", ge=0, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'float(1.0)'} })
    eco_code: Optional[str] = Field(default=None, description="""Evidence & Conclusion Ontology code""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    eco_label: Optional[str] = Field(default=None, description="""Label of the ECO term""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    description: Optional[str] = Field(default=None, description="""Human-readable description of this evidence""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })


class ConcordanceEvidence(Evidence):
    """
    Evidence from cross-ontology/source agreement.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'class_uri': 'mondoai:ConcordanceEvidence',
         'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    source_resource: str = Field(default=..., description="""Resource ID this evidence comes from (e.g., \"omim\")""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    source_resource_name: Optional[str] = Field(default=None, description="""Human-readable name of the source""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    source_entity_id: str = Field(default=..., description="""Source entity ID that provides this evidence""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    source_entity_label: Optional[str] = Field(default=None, description="""Label of the source entity""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    mapping_predicate: Optional[str] = Field(default=None, description="""SSSOM predicate mapping source to Mondo""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    mapping_justification: Optional[str] = Field(default=None, description="""SSSOM mapping justification""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence']} })
    reliability_weight: Optional[float] = Field(default=1.0, description="""Source-specific reliability weight from config""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConcordanceEvidence'], 'ifabsent': 'float(1.0)'} })
    id: str = Field(default=..., description="""Unique evidence item identifier""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    evidence_type: EvidenceType = Field(default=..., description="""Type of evidence (CONCORDANCE, LITERATURE, COMPUTATIONAL)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    direction: Optional[EvidenceDirection] = Field(default=EvidenceDirection.SUPPORTS, description="""Whether this evidence supports or contradicts""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'string(SUPPORTS)'} })
    evidence_strength: Optional[float] = Field(default=1.0, description="""Strength of this evidence (0=weak, 1=strong)""", ge=0, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'float(1.0)'} })
    eco_code: Optional[str] = Field(default=None, description="""Evidence & Conclusion Ontology code""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    eco_label: Optional[str] = Field(default=None, description="""Label of the ECO term""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    description: Optional[str] = Field(default=None, description="""Human-readable description of this evidence""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })


class LiteratureEvidence(Evidence):
    """
    Evidence from published literature.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'class_uri': 'mondoai:LiteratureEvidence',
         'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    publication_id: str = Field(default=..., description="""Publication identifier (e.g., PMID:12345678)""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    publication_title: Optional[str] = Field(default=None, description="""Title of the publication""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    publication_year: Optional[int] = Field(default=None, description="""Year of publication""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    quoted_text: Optional[str] = Field(default=None, description="""Verbatim excerpt from publication""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    quote_location: Optional[str] = Field(default=None, description="""Section/page/paragraph reference""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    explanation: Optional[str] = Field(default=None, description="""How the quoted text supports/contradicts the statement""", json_schema_extra = { "linkml_meta": {'domain_of': ['LiteratureEvidence']} })
    id: str = Field(default=..., description="""Unique evidence item identifier""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    evidence_type: EvidenceType = Field(default=..., description="""Type of evidence (CONCORDANCE, LITERATURE, COMPUTATIONAL)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    direction: Optional[EvidenceDirection] = Field(default=EvidenceDirection.SUPPORTS, description="""Whether this evidence supports or contradicts""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'string(SUPPORTS)'} })
    evidence_strength: Optional[float] = Field(default=1.0, description="""Strength of this evidence (0=weak, 1=strong)""", ge=0, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'float(1.0)'} })
    eco_code: Optional[str] = Field(default=None, description="""Evidence & Conclusion Ontology code""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    eco_label: Optional[str] = Field(default=None, description="""Label of the ECO term""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    description: Optional[str] = Field(default=None, description="""Human-readable description of this evidence""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })


class ComputationalEvidence(Evidence):
    """
    Algorithmically computed evidence.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'class_uri': 'mondoai:ComputationalEvidence',
         'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    method: Optional[str] = Field(default=None, description="""Name or description of the method""", json_schema_extra = { "linkml_meta": {'domain_of': ['ComputationalEvidence']} })
    method_uri: Optional[str] = Field(default=None, description="""URI identifying the method""", json_schema_extra = { "linkml_meta": {'domain_of': ['ComputationalEvidence']} })
    confidence_score: Optional[float] = Field(default=None, description="""Confidence score from the method""", json_schema_extra = { "linkml_meta": {'domain_of': ['ComputationalEvidence']} })
    parameters: Optional[str] = Field(default=None, description="""Parameters used in the computation""", json_schema_extra = { "linkml_meta": {'domain_of': ['ComputationalEvidence']} })
    id: str = Field(default=..., description="""Unique evidence item identifier""", json_schema_extra = { "linkml_meta": {'domain_of': ['EntitySnapshot', 'EvidencePacket', 'Evidence']} })
    evidence_type: EvidenceType = Field(default=..., description="""Type of evidence (CONCORDANCE, LITERATURE, COMPUTATIONAL)""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    direction: Optional[EvidenceDirection] = Field(default=EvidenceDirection.SUPPORTS, description="""Whether this evidence supports or contradicts""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'string(SUPPORTS)'} })
    evidence_strength: Optional[float] = Field(default=1.0, description="""Strength of this evidence (0=weak, 1=strong)""", ge=0, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence'], 'ifabsent': 'float(1.0)'} })
    eco_code: Optional[str] = Field(default=None, description="""Evidence & Conclusion Ontology code""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    eco_label: Optional[str] = Field(default=None, description="""Label of the ECO term""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })
    description: Optional[str] = Field(default=None, description="""Human-readable description of this evidence""", json_schema_extra = { "linkml_meta": {'domain_of': ['Evidence']} })


class EvidenceSynthesis(ConfiguredBaseModel):
    """
    AI-synthesized summary and recommendation for an assertion.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    summary: str = Field(default=..., description="""Textual summary synthesizing the evidence for the assertion""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    confidence: float = Field(default=..., description="""Confidence score (0-1) reflecting how strongly the agent supports the assertion""", ge=0, le=1, json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    action: Optional[RecommendedAction] = Field(default=None, description="""Recommended action for this statement (ADD, CONFIRM, REJECT, REVIEW)""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    cited_evidence: Optional[list[str]] = Field(default=[], description="""Evidence IDs cited in the synthesis""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    model_id: Optional[str] = Field(default=None, description="""AI model used (e.g., \"claude-sonnet-4-20250514\")""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    prompt_version: Optional[str] = Field(default=None, description="""Version of the prompt template used""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidenceSynthesis']} })
    generated_at: Optional[datetime ] = Field(default=None, description="""When this synthesis was generated""", json_schema_extra = { "linkml_meta": {'domain_of': ['ResourceSnapshot', 'AssertionProvenance', 'EvidenceSynthesis']} })


class PipelineRun(ConfiguredBaseModel):
    """
    Metadata for a pipeline execution run.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://w3id.org/monarch-initiative/mondo-ai'})

    run_id: str = Field(default=..., description="""Unique run identifier (typically timestamp-based)""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    started_at: datetime  = Field(default=..., json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    completed_at: Optional[datetime ] = Field(default=None, json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    status: Optional[RunStatus] = Field(default=None, description="""Status of the run""", json_schema_extra = { "linkml_meta": {'domain_of': ['EvidencePacket', 'PipelineRun']} })
    config_snapshot: Optional[str] = Field(default=None, description="""JSON-serialized config used for this run""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    sources_processed: Optional[list[str]] = Field(default=[], description="""List of source IDs processed""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    packets_created: Optional[int] = Field(default=None, description="""Number of evidence packets created""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    packets_new: Optional[int] = Field(default=None, description="""Number of NEW bucket packets""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    packets_confirm: Optional[int] = Field(default=None, description="""Number of CONFIRM bucket packets""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })
    error_message: Optional[str] = Field(default=None, description="""Error message if run failed""", json_schema_extra = { "linkml_meta": {'domain_of': ['PipelineRun']} })


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
ResourceSnapshot.model_rebuild()
EntitySnapshot.model_rebuild()
Synonym.model_rebuild()
EntityMapping.model_rebuild()
CanonicalStatement.model_rebuild()
EvidencePacket.model_rebuild()
AssertionProvenance.model_rebuild()
Evidence.model_rebuild()
ConcordanceEvidence.model_rebuild()
LiteratureEvidence.model_rebuild()
ComputationalEvidence.model_rebuild()
EvidenceSynthesis.model_rebuild()
PipelineRun.model_rebuild()
