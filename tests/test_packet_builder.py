"""Tests for the packet builder module."""

import pytest
from datetime import date

from mondo_ai.packets.builder import (
    PacketBuilder,
    PacketMerger,
    MONDO_CONSORTIUM,
    _hash_content,
    _generate_packet_id,
    _generate_statement_id,
    _generate_evidence_line_id,
    _generate_evidence_item_id,
)
from mondo_ai.datamodel import (
    AgentContribution,
    Coding,
    ContributionType,
    EvidenceItemType,
    SieveDocument,
    SieveStatement,
    EvidencePacket,
    TrustLevel,
)


class TestDeterministicIds:
    """Test that IDs are deterministic across runs."""

    def test_hash_content_deterministic(self):
        """Hash should be the same for the same input."""
        h1 = _hash_content("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        h2 = _hash_content("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        assert h1 == h2

    def test_hash_content_different_for_different_input(self):
        """Hash should differ for different input."""
        h1 = _hash_content("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        h2 = _hash_content("MONDO:0005015", "oio:hasExactSynonym", "DM")
        assert h1 != h2

    def test_packet_id_deterministic(self):
        """Packet ID should be deterministic for same statement."""
        id1 = _generate_packet_id("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        id2 = _generate_packet_id("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        assert id1 == id2
        assert id1.startswith("pkt_")

    def test_packet_id_different_for_different_statement(self):
        """Packet ID should differ for different statements."""
        id1 = _generate_packet_id("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        id2 = _generate_packet_id("MONDO:0005016", "oio:hasExactSynonym", "diabetes")
        assert id1 != id2

    def test_statement_id_deterministic(self):
        """Statement ID should be deterministic."""
        id1 = _generate_statement_id("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        id2 = _generate_statement_id("MONDO:0005015", "oio:hasExactSynonym", "diabetes")
        assert id1 == id2
        assert id1.startswith("stmt_")

    def test_evidence_line_id_deterministic(self):
        """Evidence line ID should be deterministic for same packet+source."""
        id1 = _generate_evidence_line_id("pkt_abc123", "doid")
        id2 = _generate_evidence_line_id("pkt_abc123", "doid")
        assert id1 == id2
        assert id1.startswith("el_")

    def test_evidence_line_id_different_for_different_source(self):
        """Evidence line ID should differ for different sources."""
        id1 = _generate_evidence_line_id("pkt_abc123", "doid")
        id2 = _generate_evidence_line_id("pkt_abc123", "ordo")
        assert id1 != id2

    def test_evidence_item_id_deterministic(self):
        """Evidence item ID should be deterministic."""
        id1 = _generate_evidence_item_id("el_abc123", "doid")
        id2 = _generate_evidence_item_id("el_abc123", "doid")
        assert id1 == id2
        assert id1.startswith("ev_")


class TestPacketBuilder:
    """Test PacketBuilder functionality."""

    def test_build_packet_deterministic_id(self):
        """Building packet from same statement should produce same ID."""
        builder = PacketBuilder()

        statement = SieveStatement(
            id="stmt_test",
            type="SieveStatement",
            subject="MONDO:0005015",
            subjectLabel="diabetes mellitus",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
            objectLabel="DM",
        )

        packet1 = builder.build_packet(
            statement=statement,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        packet2 = builder.build_packet(
            statement=statement,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        assert packet1.id == packet2.id

    def test_build_packet_different_id_for_different_statement(self):
        """Different statements should produce different packet IDs."""
        builder = PacketBuilder()

        statement1 = SieveStatement(
            id="stmt_test1",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
        )

        statement2 = SieveStatement(
            id="stmt_test2",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="diabetes mellitus",  # Different object
        )

        packet1 = builder.build_packet(
            statement=statement1,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        packet2 = builder.build_packet(
            statement=statement2,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        assert packet1.id != packet2.id

    def test_evidence_line_ids_deterministic(self):
        """Evidence line IDs within packet should be deterministic."""
        builder = PacketBuilder()

        statement = SieveStatement(
            id="stmt_test",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
        )

        packet1 = builder.build_packet(
            statement=statement,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        packet2 = builder.build_packet(
            statement=statement,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        assert packet1.hasEvidenceLines[0].id == packet2.hasEvidenceLines[0].id
        assert packet1.hasEvidenceLines[0].hasEvidenceItems[0].id == packet2.hasEvidenceLines[0].hasEvidenceItems[0].id


class TestPacketMerger:
    """Test PacketMerger functionality."""

    def test_merge_packets_same_statement(self):
        """Packets with same statement should be merged."""
        builder = PacketBuilder()
        merger = PacketMerger()

        statement = SieveStatement(
            id="stmt_shared",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
        )

        packet1 = builder.build_packet(
            statement=statement,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        packet2 = builder.build_packet(
            statement=statement,
            source_id="ordo",
            source_name="Orphanet",
            source_entity_id="Orphanet:12345",
            source_entity_label="diabetes mellitus",
        )

        merger.add_packet(packet1)
        merger.add_packet(packet2)

        merged = merger.get_packets()
        assert len(merged) == 1
        assert len(merged[0].hasEvidenceLines) == 2

    def test_no_merge_different_statements(self):
        """Packets with different statements should not merge."""
        builder = PacketBuilder()
        merger = PacketMerger()

        statement1 = SieveStatement(
            id="stmt_1",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
        )

        statement2 = SieveStatement(
            id="stmt_2",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="diabetes",
        )

        packet1 = builder.build_packet(
            statement=statement1,
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
        )

        packet2 = builder.build_packet(
            statement=statement2,
            source_id="ordo",
            source_name="Orphanet",
            source_entity_id="Orphanet:12345",
            source_entity_label="diabetes mellitus",
        )

        merger.add_packet(packet1)
        merger.add_packet(packet2)

        merged = merger.get_packets()
        assert len(merged) == 2


class TestOrcidEvidenceLine:
    """Test ORCID expert evidence line creation."""

    def test_creates_evidence_line(self):
        """Should create SieveEvidenceLine for ORCID."""
        builder = PacketBuilder()
        line = builder._create_orcid_evidence_line(
            packet_id="pkt_test",
            orcid="0000-0001-2345-6789",
        )
        assert line.id.startswith("el_")
        assert line.type == "SieveEvidenceLine"

    def test_contains_agent_contribution(self):
        """Evidence line should contain AgentContribution."""
        builder = PacketBuilder()
        line = builder._create_orcid_evidence_line(
            packet_id="pkt_test",
            orcid="0000-0001-2345-6789",
        )
        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, AgentContribution)
        assert item.evidenceItemType == EvidenceItemType.AgentContribution

    def test_agent_is_orcid_person(self):
        """Agent should be the ORCID person."""
        builder = PacketBuilder()
        line = builder._create_orcid_evidence_line(
            packet_id="pkt_test",
            orcid="0000-0001-2345-6789",
        )
        item = line.hasEvidenceItems[0]
        assert item.contributor.id == "ORCID:0000-0001-2345-6789"
        assert item.contributor.subtype == "person"

    def test_curator_trust_level(self):
        """Should use curator trust level."""
        builder = PacketBuilder()
        line = builder._create_orcid_evidence_line(
            packet_id="pkt_test",
            orcid="0000-0001-2345-6789",
        )
        item = line.hasEvidenceItems[0]
        assert item.trustLevel == TrustLevel.curator
        assert item.contributionType == ContributionType.decision

    def test_deterministic_ids(self):
        """Same ORCID should produce same IDs."""
        builder = PacketBuilder()
        line1 = builder._create_orcid_evidence_line("pkt_test", "0000-0001-2345-6789")
        line2 = builder._create_orcid_evidence_line("pkt_test", "0000-0001-2345-6789")
        assert line1.id == line2.id
        assert line1.hasEvidenceItems[0].id == line2.hasEvidenceItems[0].id


class TestPmidEvidenceLine:
    """Test PMID candidate literature evidence line creation."""

    def test_creates_evidence_line(self):
        """Should create SieveEvidenceLine for PMID."""
        builder = PacketBuilder()
        line = builder._create_pmid_evidence_line(
            packet_id="pkt_test",
            pmid="12345678",
        )
        assert line.id.startswith("el_")
        assert line.type == "SieveEvidenceLine"

    def test_contains_sieve_document(self):
        """Evidence line should contain SieveDocument."""
        builder = PacketBuilder()
        line = builder._create_pmid_evidence_line(
            packet_id="pkt_test",
            pmid="12345678",
        )
        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, SieveDocument)

    def test_pmid_in_document(self):
        """Document should have PMID."""
        builder = PacketBuilder()
        line = builder._create_pmid_evidence_line(
            packet_id="pkt_test",
            pmid="12345678",
        )
        item = line.hasEvidenceItems[0]
        assert item.pmid == "12345678"

    def test_candidate_subtype(self):
        """Should have candidate_reference subtype."""
        builder = PacketBuilder()
        line = builder._create_pmid_evidence_line(
            packet_id="pkt_test",
            pmid="12345678",
        )
        item = line.hasEvidenceItems[0]
        assert item.subtype is not None
        assert item.subtype.code == "candidate_reference"
        assert item.subtype.system == "mondo:evidence_status"

    def test_weak_strength(self):
        """Should have weak evidence strength."""
        builder = PacketBuilder()
        line = builder._create_pmid_evidence_line(
            packet_id="pkt_test",
            pmid="12345678",
        )
        assert line.strengthOfEvidenceProvided == "weak"
        assert line.scoreOfEvidenceProvided == 0.4


class TestConsortiumDecisionEvidenceLine:
    """Test Mondo consortium decision evidence line creation."""

    def test_creates_evidence_line(self):
        """Should create SieveEvidenceLine for consortium decision."""
        builder = PacketBuilder()
        line = builder._create_consortium_evidence_line(packet_id="pkt_test")
        assert line.id.startswith("el_")
        assert line.type == "SieveEvidenceLine"

    def test_contains_agent_contribution(self):
        """Evidence line should contain AgentContribution."""
        builder = PacketBuilder()
        line = builder._create_consortium_evidence_line(packet_id="pkt_test")
        assert len(line.hasEvidenceItems) == 1
        item = line.hasEvidenceItems[0]
        assert isinstance(item, AgentContribution)

    def test_consortium_agent(self):
        """Agent should be Mondo consortium."""
        builder = PacketBuilder()
        line = builder._create_consortium_evidence_line(packet_id="pkt_test")
        item = line.hasEvidenceItems[0]
        assert item.contributor.id == "mondo:consortium"
        assert item.contributor.subtype == "organization"
        assert "Mondo" in item.contributor.name

    def test_authority_trust_level(self):
        """Should use authority trust level."""
        builder = PacketBuilder()
        line = builder._create_consortium_evidence_line(packet_id="pkt_test")
        item = line.hasEvidenceItems[0]
        assert item.trustLevel == TrustLevel.authority
        assert item.contributionType == ContributionType.decision

    def test_version_in_description(self):
        """Version should appear in description if provided."""
        builder = PacketBuilder()
        line = builder._create_consortium_evidence_line(
            packet_id="pkt_test",
            source_version="v2024-01-01",
        )
        item = line.hasEvidenceItems[0]
        assert "v2024-01-01" in item.description


class TestBuildPacketWithProvenance:
    """Test build_packet with provenance parameters."""

    def _make_statement(self):
        """Create test statement."""
        return SieveStatement(
            id="stmt_test",
            type="SieveStatement",
            subject="MONDO:0005015",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="DM",
        )

    def test_external_source_creates_concordance(self):
        """Non-Mondo source should create concordance evidence."""
        builder = PacketBuilder()
        packet = builder.build_packet(
            statement=self._make_statement(),
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
            is_mondo_source=False,
        )
        assert len(packet.hasEvidenceLines) == 1
        assert packet.hasEvidenceLines[0].hasEvidenceItems[0].type == "ConcordanceItem"

    def test_mondo_source_creates_consortium_decision(self):
        """Mondo source should create consortium decision evidence."""
        builder = PacketBuilder()
        packet = builder.build_packet(
            statement=self._make_statement(),
            source_id="mondo",
            source_name="Mondo",
            source_entity_id="MONDO:0005015",
            source_entity_label="diabetes mellitus",
            is_mondo_source=True,
        )
        assert len(packet.hasEvidenceLines) == 1
        item = packet.hasEvidenceLines[0].hasEvidenceItems[0]
        assert item.type == "AgentContribution"
        assert item.contributor.id == "mondo:consortium"

    def test_orcids_create_additional_lines(self):
        """ORCIDs should create additional evidence lines."""
        builder = PacketBuilder()
        packet = builder.build_packet(
            statement=self._make_statement(),
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
            orcids=["0000-0001-2345-6789", "0000-0002-3456-7890"],
        )
        assert len(packet.hasEvidenceLines) == 3  # 1 concordance + 2 ORCID
        orcid_lines = [
            line for line in packet.hasEvidenceLines
            if isinstance(line.hasEvidenceItems[0], AgentContribution)
            and line.hasEvidenceItems[0].contributor.subtype == "person"
        ]
        assert len(orcid_lines) == 2

    def test_pmids_create_additional_lines(self):
        """PMIDs should create additional evidence lines."""
        builder = PacketBuilder()
        packet = builder.build_packet(
            statement=self._make_statement(),
            source_id="doid",
            source_name="Disease Ontology",
            source_entity_id="DOID:9351",
            source_entity_label="diabetes mellitus",
            pmids=["12345678", "87654321"],
        )
        assert len(packet.hasEvidenceLines) == 3  # 1 concordance + 2 PMID
        pmid_lines = [
            line for line in packet.hasEvidenceLines
            if isinstance(line.hasEvidenceItems[0], SieveDocument)
        ]
        assert len(pmid_lines) == 2

    def test_combined_provenance(self):
        """Should handle combined ORCIDs and PMIDs."""
        builder = PacketBuilder()
        packet = builder.build_packet(
            statement=self._make_statement(),
            source_id="mondo",
            source_name="Mondo",
            source_entity_id="MONDO:0005015",
            source_entity_label="diabetes mellitus",
            is_mondo_source=True,
            orcids=["0000-0001-2345-6789"],
            pmids=["12345678"],
        )
        # 1 consortium + 1 ORCID + 1 PMID = 3 lines
        assert len(packet.hasEvidenceLines) == 3
