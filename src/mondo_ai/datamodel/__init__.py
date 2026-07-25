"""MondoAI Data Model (SIEVE/SEPIO-based).

This module contains the LinkML-generated Pydantic models for the MondoAI pipeline,
based on the SIEVE evidence framework which extends SEPIO.
"""

# Primary SIEVE/SEPIO models
from mondo_ai.datamodel.sieve_models import (
    # Core SIEVE classes
    EvidencePacket,
    SieveStatement,
    SieveEvidenceLine,
    EvidenceSynthesis,
    Score,
    CurationActivity,
    # Evidence item types
    SieveEvidenceItem,
    ConcordanceItem,
    SieveDocument,
    SieveDataItem,
    SieveStudyResult,
    ComputationalResult,
    AgentContribution,
    # SEPIO base classes
    Entity,
    InformationEntity,
    Statement,
    EvidenceLine,
    Document,
    DataItem,
    DataSet,
    StudyResult,
    Activity,
    Contribution,
    Agent,
    Proposition,
    StudyGroup,
    # Utility classes
    Coding,
    Qualifier,
    Expression,
    Extension,
    RecordMetadata,
    Characteristic,
    # Enums from SIEVE
    CurationStatus,
    EvidenceDirection,
    EvidenceStrength,
    EvidenceItemType,
    TrustLevel,
    ContributionChannel,
    ContributionType,
)

# Mondo-AI specific models (extraction and pipeline)
from mondo_ai.datamodel.sieve_models import (
    ResourceSnapshot,
    EntitySnapshot,
    Synonym,
    EntityMapping,
)

# Mondo-AI specific enums
from mondo_ai.datamodel.sieve_models import (
    StatementPredicate,
    RecommendedAction,
    SynonymScope,
)

# Mapping utilities
from mondo_ai.datamodel.mappings import (
    predicate_to_coding,
    coding_to_predicate,
    direction_to_sieve,
    sieve_to_direction,
)

# Loader utilities (handle polymorphism)
from mondo_ai.datamodel.loaders import (
    load_packet,
    load_packets,
)

__all__ = [
    # Core SIEVE classes
    "EvidencePacket",
    "SieveStatement",
    "SieveEvidenceLine",
    "EvidenceSynthesis",
    "Score",
    "CurationActivity",
    # Evidence item types
    "SieveEvidenceItem",
    "ConcordanceItem",
    "SieveDocument",
    "SieveDataItem",
    "SieveStudyResult",
    "ComputationalResult",
    "AgentContribution",
    # SEPIO base classes
    "Entity",
    "InformationEntity",
    "Statement",
    "EvidenceLine",
    "Document",
    "DataItem",
    "DataSet",
    "StudyResult",
    "Activity",
    "Contribution",
    "Agent",
    "Proposition",
    "StudyGroup",
    # Utility classes
    "Coding",
    "Qualifier",
    "Expression",
    "Extension",
    "RecordMetadata",
    "Characteristic",
    # Enums
    "CurationStatus",
    "EvidenceDirection",
    "EvidenceStrength",
    "EvidenceItemType",
    "TrustLevel",
    "ContributionChannel",
    "ContributionType",
    "StatementPredicate",
    "RecommendedAction",
    "SynonymScope",
    # Mondo-AI specific models
    "ResourceSnapshot",
    "EntitySnapshot",
    "Synonym",
    "EntityMapping",
    # Mapping utilities
    "predicate_to_coding",
    "coding_to_predicate",
    "direction_to_sieve",
    "sieve_to_direction",
    # Loader utilities
    "load_packet",
    "load_packets",
]
