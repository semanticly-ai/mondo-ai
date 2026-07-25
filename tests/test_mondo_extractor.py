"""Tests for the Mondo extractor module."""


from mondo_ai.datamodel import (
    AgentContribution,
    Coding,
    ContributionType,
    EvidenceDirection,
    EvidenceItemType,
    EvidenceStrength,
    SieveDocument,
    TrustLevel,
)
from mondo_ai.extraction.mondo_extractor import (
    MONDO_CONSORTIUM,
    create_candidate_literature_evidence_line,
    create_consortium_evidence_line,
    create_expert_evidence_line,
    extract_provenance_from_sources,
)


class TestExtractProvenanceFromSources:
    """Tests for extract_provenance_from_sources function."""

    def test_empty_sources(self):
        """Test with empty source list."""
        prov = extract_provenance_from_sources([])
        assert prov.orcids == []
        assert prov.pmids == []
        assert prov.raw_sources == []

    def test_orcid_extraction(self):
        """Test extracting ORCIDs from sources."""
        sources = ["ORCID:0000-0001-5208-3432"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432"]
        assert prov.pmids == []

    def test_pmid_extraction(self):
        """Test extracting PMIDs from sources."""
        sources = ["PMID:12345678"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == []
        assert prov.pmids == ["12345678"]

    def test_mixed_sources(self):
        """Test extracting both ORCIDs and PMIDs."""
        sources = [
            "ORCID:0000-0001-5208-3432",
            "PMID:12345678",
            "ORCID:0000-0002-1234-5678",
        ]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432", "0000-0002-1234-5678"]
        assert prov.pmids == ["12345678"]

    def test_case_insensitive(self):
        """Test case-insensitive pattern matching."""
        sources = ["orcid:0000-0001-5208-3432", "pmid:99999999"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432"]
        assert prov.pmids == ["99999999"]

    def test_embedded_in_text(self):
        """Test extracting identifiers embedded in longer text."""
        sources = ["Reviewed by ORCID:0000-0001-5208-3432 on 2024-01-01"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432"]

    def test_multiple_in_single_source(self):
        """Test extracting multiple identifiers from a single source string."""
        sources = ["ORCID:0000-0001-5208-3432, PMID:12345678"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432"]
        assert prov.pmids == ["12345678"]

    def test_deduplication(self):
        """Test that duplicate identifiers are deduplicated."""
        sources = ["ORCID:0000-0001-5208-3432", "ORCID:0000-0001-5208-3432"]
        prov = extract_provenance_from_sources(sources)
        assert prov.orcids == ["0000-0001-5208-3432"]

    def test_preserves_raw_sources(self):
        """Test that raw sources are preserved."""
        sources = ["source1", "source2"]
        prov = extract_provenance_from_sources(sources)
        assert prov.raw_sources == ["source1", "source2"]


class TestCreateConsortiumEvidenceLine:
    """Tests for create_consortium_evidence_line function."""

    def test_creates_evidence_line(self):
        """Test creating consortium evidence line."""
        line = create_consortium_evidence_line(
            entity_id="MONDO:0005015",
            version="http://purl.obolibrary.org/obo/mondo/releases/2024-01-01/mondo.owl",
        )

        assert line.type == "SieveEvidenceLine"
        assert line.directionOfEvidenceProvided == EvidenceDirection.supports.value
        assert line.strengthOfEvidenceProvided == EvidenceStrength.moderate.value
        assert line.scoreOfEvidenceProvided == 0.6

    def test_contains_agent_contribution(self):
        """Test that evidence line contains AgentContribution."""
        line = create_consortium_evidence_line(
            entity_id="MONDO:0005015",
            version="v2024-01-01",
        )

        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, AgentContribution)
        assert item.type == "AgentContribution"
        assert item.evidenceItemType == EvidenceItemType.AgentContribution

    def test_consortium_agent(self):
        """Test that contribution is from Mondo consortium."""
        line = create_consortium_evidence_line(
            entity_id="MONDO:0005015",
            version="v2024-01-01",
        )

        item = line.hasEvidenceItems[0]
        assert item.contributor.id == "mondo:consortium"
        assert item.contributor.name == "Mondo Disease Ontology Consortium"
        assert item.contributor.subtype == "organization"

    def test_trust_level_and_type(self):
        """Test contribution trust level and type."""
        line = create_consortium_evidence_line(
            entity_id="MONDO:0005015",
            version="v2024-01-01",
        )

        item = line.hasEvidenceItems[0]
        assert item.trustLevel == TrustLevel.authority
        assert item.contributionType == ContributionType.decision


class TestCreateExpertEvidenceLine:
    """Tests for create_expert_evidence_line function."""

    def test_creates_evidence_line(self):
        """Test creating expert evidence line."""
        line = create_expert_evidence_line(
            orcid="0000-0001-5208-3432",
            entity_id="MONDO:0005015",
        )

        assert line.type == "SieveEvidenceLine"
        assert line.directionOfEvidenceProvided == EvidenceDirection.supports.value
        assert line.strengthOfEvidenceProvided == EvidenceStrength.strong.value
        assert line.scoreOfEvidenceProvided == 0.8

    def test_contains_agent_contribution(self):
        """Test that evidence line contains AgentContribution."""
        line = create_expert_evidence_line(
            orcid="0000-0001-5208-3432",
            entity_id="MONDO:0005015",
        )

        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, AgentContribution)
        assert item.type == "AgentContribution"

    def test_orcid_as_contributor(self):
        """Test that ORCID is used as contributor ID."""
        line = create_expert_evidence_line(
            orcid="0000-0001-5208-3432",
            entity_id="MONDO:0005015",
        )

        item = line.hasEvidenceItems[0]
        assert item.contributor.id == "ORCID:0000-0001-5208-3432"
        assert item.contributor.subtype == "person"

    def test_curator_trust_level(self):
        """Test contribution has curator trust level."""
        line = create_expert_evidence_line(
            orcid="0000-0001-5208-3432",
            entity_id="MONDO:0005015",
        )

        item = line.hasEvidenceItems[0]
        assert item.trustLevel == TrustLevel.curator
        assert item.contributionType == ContributionType.decision


class TestCreateCandidateLiteratureEvidenceLine:
    """Tests for create_candidate_literature_evidence_line function."""

    def test_creates_evidence_line(self):
        """Test creating candidate literature evidence line."""
        line = create_candidate_literature_evidence_line(
            pmid="12345678",
            entity_id="MONDO:0005015",
        )

        assert line.type == "SieveEvidenceLine"
        assert line.directionOfEvidenceProvided == EvidenceDirection.supports.value
        # Candidate literature has weak strength (unverified)
        assert line.strengthOfEvidenceProvided == EvidenceStrength.weak.value
        assert line.scoreOfEvidenceProvided == 0.4

    def test_contains_sieve_document(self):
        """Test that evidence line contains SieveDocument."""
        line = create_candidate_literature_evidence_line(
            pmid="12345678",
            entity_id="MONDO:0005015",
        )

        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, SieveDocument)
        assert item.type == "SieveDocument"

    def test_pmid_and_url(self):
        """Test that PMID and URL are set correctly."""
        line = create_candidate_literature_evidence_line(
            pmid="12345678",
            entity_id="MONDO:0005015",
        )

        item = line.hasEvidenceItems[0]
        assert item.pmid == "12345678"
        assert item.urls == ["https://pubmed.ncbi.nlm.nih.gov/12345678/"]

    def test_candidate_subtype(self):
        """Test that document has candidate subtype."""
        line = create_candidate_literature_evidence_line(
            pmid="12345678",
            entity_id="MONDO:0005015",
        )

        item = line.hasEvidenceItems[0]
        assert item.subtype is not None
        assert isinstance(item.subtype, Coding)
        assert item.subtype.code == "candidate_reference"
        assert item.subtype.label == "Candidate Literature Reference"
        assert item.subtype.system == "mondo:evidence_status"

    def test_description_indicates_unverified(self):
        """Test that description indicates the reference is unverified."""
        line = create_candidate_literature_evidence_line(
            pmid="12345678",
            entity_id="MONDO:0005015",
        )

        item = line.hasEvidenceItems[0]
        assert "not yet verified" in item.description.lower()


class TestMondoConsortiumAgent:
    """Tests for the MONDO_CONSORTIUM constant."""

    def test_agent_properties(self):
        """Test Mondo consortium agent has correct properties."""
        assert MONDO_CONSORTIUM.id == "mondo:consortium"
        assert MONDO_CONSORTIUM.name == "Mondo Disease Ontology Consortium"
        assert MONDO_CONSORTIUM.subtype == "organization"
