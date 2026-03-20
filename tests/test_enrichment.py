"""Tests for the enrichment module."""

from unittest.mock import MagicMock, patch

import pytest

from mondo_ai.datamodel import (
    ConcordanceItem,
    CurationStatus,
    EvidenceDirection,
    EvidenceItemType,
    EvidencePacket,
    EvidenceStrength,
    SieveDocument,
    SieveEvidenceLine,
    SieveStatement,
    StatementPredicate,
    predicate_to_coding,
)
from mondo_ai.enrichment.pubmed import (
    PubMedClient,
    PubMedPaper,
    ReferenceCache,
    build_search_query,
)
from mondo_ai.enrichment.enricher import PacketEnricher, has_literature_evidence


class TestBuildSearchQuery:
    """Tests for the build_search_query function."""

    def test_basic_query(self):
        """Test building a basic search query."""
        query = build_search_query("asthma", "bronchial asthma")
        assert "asthma" in query
        assert "bronchial asthma" in query

    def test_disease_only(self):
        """Test query with disease only."""
        query = build_search_query("asthma", "")
        assert "asthma" in query

    def test_synonym_only(self):
        """Test query with synonym only."""
        query = build_search_query("", "bronchial asthma")
        assert "bronchial asthma" in query


class TestPubMedPaper:
    """Tests for the PubMedPaper dataclass."""

    def test_paper_creation(self):
        """Test creating a PubMed paper."""
        paper = PubMedPaper(
            pmid="12345678",
            title="Test Paper Title",
            abstract="This is the abstract.",
            authors=["Smith J", "Jones M"],
            journal="Test Journal",
            year="2023",
            doi="10.1234/test",
        )
        assert paper.pmid == "12345678"
        assert paper.title == "Test Paper Title"
        assert paper.reference_id == "PMID:12345678"

    def test_reference_id_property(self):
        """Test the reference_id property."""
        paper = PubMedPaper(
            pmid="99999999",
            title="Test",
            abstract=None,
            authors=[],
            journal=None,
            year=None,
            doi=None,
        )
        assert paper.reference_id == "PMID:99999999"


class TestReferenceCache:
    """Tests for the ReferenceCache class."""

    def test_cache_path(self, tmp_path):
        """Test cache path generation."""
        cache = ReferenceCache(cache_dir=tmp_path)
        path = cache._get_cache_path("12345678")
        assert path.name == "PMID_12345678.md"
        assert path.parent == tmp_path

    def test_paper_to_markdown(self, tmp_path):
        """Test converting paper to markdown."""
        paper = PubMedPaper(
            pmid="12345678",
            title="Test Paper",
            abstract="Test abstract content.",
            authors=["Smith J"],
            journal="Test Journal",
            year="2023",
            doi="10.1234/test",
        )
        markdown = paper.to_markdown()
        assert "---" in markdown
        assert "title: Test Paper" in markdown
        assert "Test abstract content." in markdown

    def test_put_and_get(self, tmp_path):
        """Test saving and loading a paper from cache."""
        cache = ReferenceCache(cache_dir=tmp_path)
        paper = PubMedPaper(
            pmid="12345678",
            title="Test Paper",
            abstract="Test abstract content.",
            authors=["Smith J"],
            journal="Test Journal",
            year="2023",
            doi=None,
        )
        # Save
        cache.put(paper)
        # Load
        loaded = cache.get("12345678")
        assert loaded is not None
        assert loaded.pmid == "12345678"
        assert loaded.title == "Test Paper"

    def test_get_missing(self, tmp_path):
        """Test loading a missing paper returns None."""
        cache = ReferenceCache(cache_dir=tmp_path)
        result = cache.get("nonexistent")
        assert result is None


class TestHasLiteratureEvidence:
    """Tests for the has_literature_evidence function."""

    def test_empty_packet(self):
        """Test packet with no evidence lines."""
        stmt = SieveStatement(
            id="test_stmt",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            object="diabetes",
        )
        packet = EvidencePacket(
            id="test_pkt",
            statement=stmt,
            status=CurationStatus.UNREVIEWED,
        )
        assert has_literature_evidence(packet) is False

    def test_packet_with_concordance_only(self):
        """Test packet with only concordance evidence."""
        stmt = SieveStatement(
            id="test_stmt",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            object="diabetes",
        )
        concordance = ConcordanceItem(
            id="ev_001",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="OMIM",
            sourceId="omim",
            sourceSubject="OMIM:125853",
            sourceSubjectLabel="Diabetes mellitus",
        )
        line = SieveEvidenceLine(
            id="el_001",
            type="SieveEvidenceLine",
            hasEvidenceItems=[concordance],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        packet = EvidencePacket(
            id="test_pkt",
            statement=stmt,
            status=CurationStatus.UNREVIEWED,
            hasEvidenceLines=[line],
        )
        assert has_literature_evidence(packet) is False

    def test_packet_with_literature(self):
        """Test packet with literature evidence."""
        stmt = SieveStatement(
            id="test_stmt",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            object="diabetes",
        )
        doc = SieveDocument(
            id="ev_002",
            type="SieveDocument",
            title="A study about diabetes",
            pmid="12345678",
        )
        line = SieveEvidenceLine(
            id="el_002",
            type="SieveEvidenceLine",
            hasEvidenceItems=[doc],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        packet = EvidencePacket(
            id="test_pkt",
            statement=stmt,
            status=CurationStatus.UNREVIEWED,
            hasEvidenceLines=[line],
        )
        assert has_literature_evidence(packet) is True

    def test_packet_with_mixed_evidence(self):
        """Test packet with both concordance and literature evidence."""
        stmt = SieveStatement(
            id="test_stmt",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            object="diabetes",
        )
        concordance = ConcordanceItem(
            id="ev_001",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="OMIM",
            sourceId="omim",
            sourceSubject="OMIM:125853",
            sourceSubjectLabel="Diabetes mellitus",
        )
        doc = SieveDocument(
            id="ev_002",
            type="SieveDocument",
            title="A study about diabetes",
            pmid="12345678",
        )
        concordance_line = SieveEvidenceLine(
            id="el_001",
            type="SieveEvidenceLine",
            hasEvidenceItems=[concordance],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        literature_line = SieveEvidenceLine(
            id="el_002",
            type="SieveEvidenceLine",
            hasEvidenceItems=[doc],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        packet = EvidencePacket(
            id="test_pkt",
            statement=stmt,
            status=CurationStatus.UNREVIEWED,
            hasEvidenceLines=[concordance_line, literature_line],
        )
        assert has_literature_evidence(packet) is True


class TestPacketEnricher:
    """Tests for the PacketEnricher class."""

    def _make_packet(self, score: float | None = None) -> EvidencePacket:
        """Create a test packet with SIEVE structure."""
        stmt = SieveStatement(
            id="test_stmt_001",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            predicate=predicate_to_coding(StatementPredicate.EXACT_SYNONYM),
            object="diabetes",
            objectLabel="diabetes",
        )
        return EvidencePacket(
            id="test_pkt",
            statement=stmt,
            status=CurationStatus.UNREVIEWED,
            hasEvidenceLines=[],
        )

    def _make_paper(self, pmid: str = "12345678", title: str = "Test Paper") -> PubMedPaper:
        """Create a test paper."""
        return PubMedPaper(
            pmid=pmid,
            title=title,
            abstract="A study about the topic.",
            authors=["Smith J"],
            journal="Test Journal",
            year="2023",
            doi=None,
        )

    def test_should_enrich_no_threshold(self):
        """Test should_enrich when no threshold is set."""
        mock_client = MagicMock()
        enricher = PacketEnricher(pubmed_client=mock_client, min_score_threshold=None)
        packet = self._make_packet()
        assert enricher.should_enrich(packet) is True

    def test_enrich_adds_literature(self):
        """Test that enrich adds literature evidence."""
        mock_client = MagicMock()
        mock_client.search_and_fetch.return_value = [self._make_paper("12345678", "Diabetes Study")]
        enricher = PacketEnricher(pubmed_client=mock_client, max_papers=1)
        packet = self._make_packet()

        enriched = enricher.enrich(packet)

        # SIEVE: evidence is in hasEvidenceLines
        assert len(enriched.hasEvidenceLines) == 1
        line = enriched.hasEvidenceLines[0]
        assert line.directionOfEvidenceProvided == "supports"
        # Evidence items contain SieveDocument
        assert len(line.hasEvidenceItems) == 1
        doc = line.hasEvidenceItems[0]
        assert doc.pmid == "12345678"

    def test_enrich_packets_batch(self):
        """Test batch enrichment."""
        mock_client = MagicMock()
        mock_client.search_and_fetch.return_value = [self._make_paper("11111111", "Paper 1")]
        enricher = PacketEnricher(pubmed_client=mock_client, max_papers=1)
        packets = [self._make_packet() for _ in range(3)]

        enriched = enricher.enrich_packets(packets)

        assert len(enriched) == 3
        for packet in enriched:
            assert len(packet.hasEvidenceLines) == 1

    def test_skip_enriched_by_default(self):
        """Test that packets with literature are skipped by default."""
        mock_client = MagicMock()
        mock_client.search_and_fetch.return_value = [self._make_paper()]
        enricher = PacketEnricher(pubmed_client=mock_client, max_papers=1)

        # Create a packet that already has literature evidence
        packet = self._make_packet()
        doc = SieveDocument(
            id="ev_existing",
            type="SieveDocument",
            title="Existing Study",
            pmid="99999999",
        )
        line = SieveEvidenceLine(
            id="el_existing",
            type="SieveEvidenceLine",
            hasEvidenceItems=[doc],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        packet.hasEvidenceLines = [line]

        # Should skip enrichment
        assert enricher.should_enrich(packet) is False

        # Enrich should not add new evidence
        enriched = enricher.enrich(packet)
        assert len(enriched.hasEvidenceLines) == 1
        assert enriched.hasEvidenceLines[0].hasEvidenceItems[0].pmid == "99999999"

    def test_force_enrichment_with_skip_enriched_false(self):
        """Test that skip_enriched=False allows re-enrichment."""
        mock_client = MagicMock()
        mock_client.search_and_fetch.return_value = [self._make_paper("11111111", "New Study")]
        enricher = PacketEnricher(pubmed_client=mock_client, max_papers=1, skip_enriched=False)

        # Create a packet that already has literature evidence
        packet = self._make_packet()
        doc = SieveDocument(
            id="ev_existing",
            type="SieveDocument",
            title="Existing Study",
            pmid="99999999",
        )
        line = SieveEvidenceLine(
            id="el_existing",
            type="SieveEvidenceLine",
            hasEvidenceItems=[doc],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=EvidenceStrength.moderate.value,
        )
        packet.hasEvidenceLines = [line]

        # Should enrich even though it has literature
        assert enricher.should_enrich(packet) is True

        # Enrich should add new evidence
        enriched = enricher.enrich(packet)
        assert len(enriched.hasEvidenceLines) == 2
        pmids = [
            line.hasEvidenceItems[0].pmid
            for line in enriched.hasEvidenceLines
            if hasattr(line.hasEvidenceItems[0], "pmid")
        ]
        assert "99999999" in pmids
        assert "11111111" in pmids
