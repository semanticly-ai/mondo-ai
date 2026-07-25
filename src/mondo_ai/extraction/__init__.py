"""OWL extraction modules."""

from mondo_ai.extraction.owl_extractor import (
    OWLExtractor,
    extract_synonyms_sample,
    extract_hierarchy_sample,
)
from mondo_ai.extraction.mondo_extractor import (
    MondoExtractor,
    AnnotationProvenance,
    create_consortium_evidence_line,
    create_expert_evidence_line,
    create_candidate_literature_evidence_line,
    extract_provenance_from_sources,
    extract_mondo_with_evidence,
    MONDO_CONSORTIUM,
)

__all__ = [
    # General OWL extraction
    "OWLExtractor",
    "extract_synonyms_sample",
    "extract_hierarchy_sample",
    # Mondo-specific extraction
    "MondoExtractor",
    "AnnotationProvenance",
    "create_consortium_evidence_line",
    "create_expert_evidence_line",
    "create_candidate_literature_evidence_line",
    "extract_provenance_from_sources",
    "extract_mondo_with_evidence",
    "MONDO_CONSORTIUM",
]
