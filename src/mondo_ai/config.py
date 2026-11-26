"""Configuration models for MondoAI pipeline.

This module provides Pydantic configuration models for loading and validating
the MondoAI pipeline configuration from YAML files.

Example:
    >>> config = PipelineConfig.from_yaml("config.yaml")
    >>> config.resources[0].id
    'omim'
"""

from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ContentType(str, Enum):
    """Types of content that can be extracted from sources."""

    LABELS = "labels"
    SYNONYMS = "synonyms"
    DEFINITIONS = "definitions"
    HIERARCHY = "hierarchy"
    MAPPINGS = "mappings"


class StorageBackend(str, Enum):
    """Supported storage backends."""

    DUCKDB = "duckdb"
    SQLITE = "sqlite"


class AIProvider(str, Enum):
    """Supported AI providers for agent synthesis."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ContentWeights(BaseModel):
    """Evidence weights for different content types within a source.

    Each source can have different reliability for different content types.
    For example, OMIM might be highly reliable for exact synonyms but less so
    for hierarchy relationships.

    The weight is the line strength ``score_of_evidence_provided`` (0-1) that a claim of
    this kind from this source contributes to the inclusion score.

    Example:
        >>> cw = ContentWeights(exact_synonym=0.95, related_synonym=0.85)
        >>> cw.get_weight("oboInOwl:hasExactSynonym")
        0.95
        >>> cw.get_weight("skos:exactMatch")
        0.6
    """

    exact_synonym: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Weight for exact synonym statements",
    )
    related_synonym: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Weight for related synonym statements",
    )
    broad_synonym: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Weight for broad synonym statements",
    )
    narrow_synonym: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Weight for narrow synonym statements",
    )
    label: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Weight for label/name statements",
    )
    definition: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Weight for definition statements",
    )
    parent: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Weight for subClassOf/parent statements",
    )
    mapping: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Weight for a mapping (xref or skos match) this source asserts",
    )
    default: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Default weight for unknown content types",
    )

    # Mapping from predicate codes to field names
    _PREDICATE_MAP: dict[str, str] = {
        "oboInOwl:hasExactSynonym": "exact_synonym",
        "oboInOwl:hasRelatedSynonym": "related_synonym",
        "oboInOwl:hasBroadSynonym": "broad_synonym",
        "oboInOwl:hasNarrowSynonym": "narrow_synonym",
        "rdfs:label": "label",
        "IAO:0000115": "definition",
        "rdfs:subClassOf": "parent",
        "skos:exactMatch": "mapping",
        "skos:broadMatch": "mapping",
        "skos:narrowMatch": "mapping",
        "skos:relatedMatch": "mapping",
    }

    def get_weight(self, predicate_code: str) -> float:
        """Get weight for a predicate code.

        Args:
            predicate_code: The predicate code (e.g., 'oio:hasExactSynonym').

        Returns:
            Weight for the predicate, or default if unknown.
        """
        field_name = self._PREDICATE_MAP.get(predicate_code)
        if field_name:
            return getattr(self, field_name)
        return self.default


class ResourceConfig(BaseModel):
    """Configuration for a single data source resource.

    Each resource has its own content weights, allowing fine-grained control
    over how much to trust different content types from each source.

    Example:
        >>> rc = ResourceConfig(
        ...     id="omim",
        ...     name="OMIM",
        ...     url="https://example.com/omim.owl",
        ...     content_weights=ContentWeights(exact_synonym=0.95),
        ... )
        >>> rc.content_weights.exact_synonym
        0.95
    """

    id: str = Field(..., description="Unique identifier for this resource (e.g., 'omim')")
    name: str = Field(..., description="Human-readable name")
    url: str = Field(..., description="URL to download the OWL file from")
    infores: str = Field(default="", description="Information resource CURIE (e.g., 'infores:omim')")
    curie_prefixes: list[str] = Field(
        default_factory=list,
        description="CURIE prefixes of this resource's records (e.g., ['OMIM', 'OMIMPS'])",
    )
    iri_filter: str = Field(default="", description="IRI substring selecting this resource's classes")
    quality: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Resource quality rating (the q factor of its lines); None = rule default",
    )
    default_role: str = Field(default="PRIMARY", description="PRIMARY or AGGREGATOR")
    derives_content_from: list[str] = Field(
        default_factory=list,
        description="Resource ids (e.g. 'omim') this resource takes content from",
    )
    enabled_content: list[ContentType] = Field(
        default_factory=lambda: [ContentType.SYNONYMS],
        description="Content types to extract from this resource",
    )
    content_weights: ContentWeights = Field(
        default_factory=ContentWeights,
        description="Evidence weights for each content type from this source",
    )
    enabled: bool = Field(default=True, description="Whether this resource is enabled")

    def get_weight(self, predicate_code: str) -> float:
        """Get the evidence weight for a predicate from this source.

        Args:
            predicate_code: The predicate code (e.g., 'oio:hasExactSynonym').

        Returns:
            Weight for the predicate from this source.
        """
        return self.content_weights.get_weight(predicate_code)


class ScoringConfig(BaseModel):
    """Parameters of the AggregationRules that compute the inclusion score S.

    S follows sieve's reference implementation (D-063): per line w = d * s * q * c, the
    strongest line per independence cluster counts, L = L0 + k * sum(w), S = tanh(L/2)
    in [-1, 1]. The values below are written into one versioned AggregationRule per claim
    type and frozen in every packet's reference_snapshot. They are illustrative, like
    sieve's example registry; calibrating them is the D-063 design task.

    Example:
        >>> ScoringConfig().strength_bands[0]
        'strong >= 0.8'
    """

    rule_version: str = Field(default="1.0", description="Version of the aggregation rules")
    scaling_factor: float = Field(default=2.0, description="k in L = L0 + k * sum(w)")
    default_quality: float = Field(
        default=0.5, ge=0.0, le=1.0, description="q for a line whose sources carry no quality rating"
    )
    confidence_floor: float = Field(
        default=0.3, ge=0.0, le=1.0, description="Lines with confidence below this are excluded"
    )
    source_ingest_prior: float = Field(
        default=0.0, description="Claim prior L0 for claims proposed by source ingest"
    )
    strength_bands: list[str] = Field(
        default_factory=lambda: ["strong >= 0.8", "moderate >= 0.4", "weak < 0.4"],
        description="Ordered bands of S and of line strengths",
    )


class EnrichmentConfig(BaseModel):
    """Configuration for PubMed enrichment."""

    enabled: bool = Field(default=False, description="Whether PubMed enrichment is enabled")
    min_score_for_enrichment: float = Field(
        default=-0.5,
        description="Minimum NER score to trigger enrichment (lower scores get enriched)",
    )
    max_papers_per_packet: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum papers to fetch per packet",
    )
    cache_dir: str = Field(
        default="data/references_cache",
        description="Directory to cache PubMed references",
    )
    ncbi_api_key: Optional[str] = Field(
        default=None,
        description="NCBI API key for higher rate limits",
    )


class AgentConfig(BaseModel):
    """Configuration for AI agent synthesis."""

    enabled: bool = Field(default=True, description="Whether AI synthesis is enabled")
    provider: AIProvider = Field(
        default=AIProvider.ANTHROPIC,
        description="AI provider to use",
    )
    model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model identifier",
    )
    prompt_version: str = Field(
        default="v1",
        description="Version of prompt template to use",
    )
    max_tokens: int = Field(
        default=1024,
        ge=100,
        le=4096,
        description="Maximum tokens for agent response",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Sampling temperature",
    )


class StorageConfig(BaseModel):
    """Configuration for data storage."""

    backend: StorageBackend = Field(
        default=StorageBackend.DUCKDB,
        description="Storage backend to use",
    )
    db_path: str = Field(
        default="packet_index/mondo_ai.duckdb",
        description="Path to database file",
    )
    packet_dir: str = Field(
        default="evidence_packets",
        description="Directory for evidence packet YAML files",
    )
    runs_dir: str = Field(
        default="runs",
        description="Directory for run metadata",
    )


class JSONLStorageConfig(BaseModel):
    """Configuration for JSONL packet storage (for hosting/distribution).

    JSONL storage provides immutable, partitioned storage for evidence packets.
    Files are sealed (made immutable) when they reach the size threshold,
    ensuring consistent content for hosting and distribution.

    Example:
        >>> config = JSONLStorageConfig(store_dir="packet_store", max_file_size_mb=50)
        >>> config.max_file_size_bytes
        52428800
    """

    enabled: bool = Field(
        default=True,
        description="Whether JSONL storage is enabled",
    )
    store_dir: str = Field(
        default="packet_store",
        description="Directory for JSONL packet files",
    )
    max_file_size_mb: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum file size in MB before sealing (immutable)",
    )

    @property
    def max_file_size_bytes(self) -> int:
        """Get max file size in bytes."""
        return self.max_file_size_mb * 1024 * 1024


class MondoConfig(BaseModel):
    """Configuration for Mondo ontology reference."""

    owl_url: str = Field(
        default="http://purl.obolibrary.org/obo/mondo.owl",
        description="URL to download Mondo OWL",
    )
    sssom_url: str = Field(
        default="http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv",
        description="URL to download Mondo SSSOM mappings",
    )
    cache_dir: str = Field(
        default="data/cache",
        description="Directory to cache downloaded files",
    )


class PipelineConfig(BaseModel):
    """Main configuration for the MondoAI pipeline.

    Example:
        >>> config = PipelineConfig(
        ...     resources=[ResourceConfig(id="omim", name="OMIM", url="https://example.com")],
        ...     mondo=MondoConfig(),
        ... )
        >>> len(config.resources)
        1
    """

    resources: list[ResourceConfig] = Field(
        default_factory=list,
        description="List of source resources to process",
    )
    mondo: MondoConfig = Field(
        default_factory=MondoConfig,
        description="Mondo ontology configuration",
    )
    scoring: ScoringConfig = Field(
        default_factory=ScoringConfig,
        description="Evidence scoring configuration",
    )
    enrichment: EnrichmentConfig = Field(
        default_factory=EnrichmentConfig,
        description="PubMed enrichment configuration",
    )
    agent: AgentConfig = Field(
        default_factory=AgentConfig,
        description="AI agent configuration",
    )
    storage: StorageConfig = Field(
        default_factory=StorageConfig,
        description="Storage configuration",
    )
    jsonl_storage: JSONLStorageConfig = Field(
        default_factory=JSONLStorageConfig,
        description="JSONL storage configuration for hosting/distribution",
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        """Load configuration from a YAML file.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            Loaded and validated configuration.

        Raises:
            FileNotFoundError: If the config file doesn't exist.
            ValidationError: If the config is invalid.

        Example:
            >>> import tempfile
            >>> import os
            >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            ...     _ = f.write("resources: []\\nmondo:\\n  owl_url: http://example.com")
            ...     tmp_path = f.name
            >>> config = PipelineConfig.from_yaml(tmp_path)
            >>> config.mondo.owl_url
            'http://example.com'
            >>> os.unlink(tmp_path)
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls.model_validate(data or {})

    def to_yaml(self, path: str | Path) -> None:
        """Save configuration to a YAML file.

        Args:
            path: Path to save the YAML configuration file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            yaml.dump(
                self.model_dump(mode="json", exclude_none=True),
                f,
                default_flow_style=False,
                sort_keys=False,
            )


def get_default_config() -> PipelineConfig:
    """Get the default pipeline configuration with OMIM, ORDO, DOID and ICD-10-CM.

    Returns:
        Default configuration with standard sources.

    Example:
        >>> config = get_default_config()
        >>> [r.id for r in config.resources]
        ['omim', 'ordo', 'doid', 'icd10cm']
    """
    all_content = [ContentType.SYNONYMS, ContentType.HIERARCHY, ContentType.MAPPINGS]
    return PipelineConfig(
        resources=[
            ResourceConfig(
                id="omim",
                name="OMIM",
                url="https://github.com/monarch-initiative/omim/releases/latest/download/omim.owl",
                infores="infores:omim",
                curie_prefixes=["OMIM", "OMIMPS"],
                iri_filter="omim.org/entry/",
                quality=0.9,
                enabled_content=all_content,
                content_weights=ContentWeights(
                    exact_synonym=0.95,
                    related_synonym=0.85,
                    broad_synonym=0.75,
                    narrow_synonym=0.75,
                    label=0.95,
                    definition=0.90,
                    parent=0.80,
                    mapping=0.5,
                ),
            ),
            ResourceConfig(
                id="ordo",
                name="Orphanet Rare Disease Ontology",
                url="https://www.orphadata.com/data/ontologies/ordo/last_version/ORDO_en_4.9.owl",
                infores="infores:orphanet",
                curie_prefixes=["Orphanet"],
                iri_filter="Orphanet_",
                quality=0.9,
                enabled_content=all_content,
                content_weights=ContentWeights(
                    exact_synonym=0.90,
                    related_synonym=0.80,
                    broad_synonym=0.70,
                    narrow_synonym=0.70,
                    label=0.90,
                    definition=0.85,
                    parent=0.85,
                    mapping=0.8,
                ),
            ),
            ResourceConfig(
                id="doid",
                name="Disease Ontology",
                url="http://purl.obolibrary.org/obo/doid.owl",
                infores="infores:disease-ontology",
                curie_prefixes=["DOID"],
                iri_filter="DOID_",
                quality=0.8,
                derives_content_from=["omim"],
                enabled_content=all_content,
                content_weights=ContentWeights(
                    exact_synonym=0.85,
                    related_synonym=0.75,
                    broad_synonym=0.65,
                    narrow_synonym=0.65,
                    label=0.85,
                    definition=0.80,
                    parent=0.80,
                    mapping=0.5,
                ),
            ),
            ResourceConfig(
                id="icd10cm",
                name="ICD-10-CM",
                url="http://purl.obolibrary.org/obo/mondo/sources/icd10cm.owl",
                infores="infores:icd10cm",
                curie_prefixes=["ICD10CM"],
                iri_filter="ontology/ICD10CM/",
                quality=0.8,
                enabled_content=all_content,
                content_weights=ContentWeights(
                    exact_synonym=0.80,
                    related_synonym=0.70,
                    broad_synonym=0.60,
                    narrow_synonym=0.60,
                    label=0.80,
                    definition=0.70,
                    parent=0.60,
                    mapping=0.5,
                ),
            ),
        ],
        mondo=MondoConfig(),
        scoring=ScoringConfig(),
        enrichment=EnrichmentConfig(enabled=False),
        agent=AgentConfig(enabled=True),
        storage=StorageConfig(),
        jsonl_storage=JSONLStorageConfig(),
    )
