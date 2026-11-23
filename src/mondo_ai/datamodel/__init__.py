"""MondoAI data model.

Evidence packets follow the SIEVE profile of the SEPIO core (``schema/sieve.yaml`` and
``schema/sepio_core.yaml``, vendored verbatim from the sieve repository). The pipeline
handles packets as plain JSON dicts, the shape sieve's own ``profile_checks`` and
``inclusion_score`` read; the generated Pydantic classes below are for typed access to
single objects and for validating samples (``loaders.packet_model``).

The mondo-ai overlay (``schema/mondo_ai_sieve.yaml``) adds the classes that hold a
source ontology locally before it becomes evidence: ResourceSnapshot, EntitySnapshot,
Synonym and EntityMapping.
"""

from mondo_ai.datamodel.sieve_models import (
    # Profile classes
    EvidencePacket,
    EvidencePacketCollection,
    SieveRegistry,
    SieveStatement,
    SynonymStatement,
    SubclassStatement,
    MappingStatement,
    SieveEvidenceLine,
    InterpretationActivity,
    ExternalRecord,
    ComputationalScore,
    Publication,
    InformationResource,
    SourceRelease,
    SieveAgent,
    SieveMethod,
    AggregationRule,
    ResolutionChain,
    AlignmentStep,
    RetrievalMetadata,
    Coding,
    # Profile enums
    ClaimType,
    Direction,
    ReviewState,
    InterpretationMethod,
    ProposingChannel,
    ReadCoverage,
    # Mondo-AI extraction models
    ResourceSnapshot,
    EntitySnapshot,
    Synonym,
    EntityMapping,
    SynonymScope,
)

from mondo_ai.datamodel.loaders import (
    load_packet,
    load_packets,
    packet_model,
)

__all__ = [
    "EvidencePacket",
    "EvidencePacketCollection",
    "SieveRegistry",
    "SieveStatement",
    "SynonymStatement",
    "SubclassStatement",
    "MappingStatement",
    "SieveEvidenceLine",
    "InterpretationActivity",
    "ExternalRecord",
    "ComputationalScore",
    "Publication",
    "InformationResource",
    "SourceRelease",
    "SieveAgent",
    "SieveMethod",
    "AggregationRule",
    "ResolutionChain",
    "AlignmentStep",
    "RetrievalMetadata",
    "Coding",
    "ClaimType",
    "Direction",
    "ReviewState",
    "InterpretationMethod",
    "ProposingChannel",
    "ReadCoverage",
    "ResourceSnapshot",
    "EntitySnapshot",
    "Synonym",
    "EntityMapping",
    "SynonymScope",
    "load_packet",
    "load_packets",
    "packet_model",
]
