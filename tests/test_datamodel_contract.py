"""Contract tests for the SIEVE/SEPIO data model.

These tests lock down the behavioral contract of the data model classes
before schema refactoring. They ensure roundtrip serialization, enum values,
polymorphism, and mapping utilities all work correctly.
"""

import pytest
from datetime import date

from mondo_ai.datamodel import (
    # Core SIEVE classes
    EvidencePacket,
    SieveStatement,
    SieveEvidenceLine,
    EvidenceSynthesis,
    Score,
    CurationActivity,
    # Evidence item types
    ConcordanceItem,
    SieveDocument,
    ComputationalResult,
    AgentContribution,
    # SEPIO base classes
    Entity,
    InformationEntity,
    Statement,
    EvidenceLine,
    Document,
    Contribution,
    Agent,
    Coding,
    # Enums
    CurationStatus,
    EvidenceDirection,
    EvidenceStrength,
    EvidenceItemType,
    TrustLevel,
    ContributionChannel,
    ContributionType,
    StatementPredicate,
    RecommendedAction,
    SynonymScope,
    # Mondo-AI specific
    ResourceSnapshot,
    EntitySnapshot,
    Synonym,
    # Mapping utilities
    predicate_to_coding,
    coding_to_predicate,
    direction_to_sieve,
    sieve_to_direction,
)
from mondo_ai.datamodel.loaders import _convert_evidence_items, EVIDENCE_ITEM_TYPES


# =============================================================================
# Enum contract tests
# =============================================================================


class TestEnumValues:
    """All enums have the expected set of values."""

    def test_curation_status(self) -> None:
        assert set(CurationStatus) == {
            CurationStatus.UNREVIEWED,
            CurationStatus.ACCEPTED,
            CurationStatus.REJECTED,
            CurationStatus.CONTROVERSIAL,
        }

    def test_evidence_direction(self) -> None:
        assert set(EvidenceDirection) == {
            EvidenceDirection.supports,
            EvidenceDirection.disputes,
            EvidenceDirection.neutral,
        }

    def test_evidence_strength(self) -> None:
        assert set(EvidenceStrength) == {
            EvidenceStrength.strong,
            EvidenceStrength.moderate,
            EvidenceStrength.weak,
        }

    def test_evidence_item_type(self) -> None:
        assert set(EvidenceItemType) == {
            EvidenceItemType.DataItem,
            EvidenceItemType.Document,
            EvidenceItemType.StudyResult,
            EvidenceItemType.ConcordanceItem,
            EvidenceItemType.ComputationalResult,
            EvidenceItemType.AgentContribution,
        }

    def test_trust_level(self) -> None:
        assert set(TrustLevel) == {
            TrustLevel.community,
            TrustLevel.domain_expert,
            TrustLevel.curator,
            TrustLevel.authority,
        }

    def test_contribution_channel(self) -> None:
        assert set(ContributionChannel) == {
            ContributionChannel.issue_tracker,
            ContributionChannel.personal_communication,
            ContributionChannel.direct_submission,
            ContributionChannel.public_forum,
        }

    def test_contribution_type(self) -> None:
        assert set(ContributionType) == {
            ContributionType.suggestion,
            ContributionType.review,
            ContributionType.decision,
            ContributionType.provision,
        }

    def test_synonym_scope(self) -> None:
        assert set(SynonymScope) == {
            SynonymScope.EXACT,
            SynonymScope.NARROW,
            SynonymScope.BROAD,
            SynonymScope.RELATED,
        }

    def test_statement_predicate(self) -> None:
        assert set(StatementPredicate) == {
            StatementPredicate.EXACT_SYNONYM,
            StatementPredicate.NARROW_SYNONYM,
            StatementPredicate.BROAD_SYNONYM,
            StatementPredicate.RELATED_SYNONYM,
            StatementPredicate.LABEL,
            StatementPredicate.DEFINITION,
            StatementPredicate.PARENT,
        }

    def test_recommended_action(self) -> None:
        assert set(RecommendedAction) == {
            RecommendedAction.ADD,
            RecommendedAction.CONFIRM,
            RecommendedAction.REJECT,
            RecommendedAction.REVIEW,
        }


# =============================================================================
# Roundtrip serialization tests
# =============================================================================


class TestRoundtripSerialization:
    """Classes roundtrip through model_dump(mode='json', exclude_none=True)."""

    def test_coding_roundtrip(self) -> None:
        coding = Coding(code="oio:hasExactSynonym", label="exact synonym", system="http://oio#")
        data = coding.model_dump(mode="json", exclude_none=True)
        assert Coding.model_validate(data) == coding

    def test_agent_roundtrip(self) -> None:
        agent = Agent(id="agent-1", type="Agent", name="Test Agent", subtype="AI")
        data = agent.model_dump(mode="json", exclude_none=True)
        assert Agent.model_validate(data) == agent

    def test_sieve_statement_roundtrip(self) -> None:
        stmt = SieveStatement(
            id="stmt-1",
            type="SieveStatement",
            subject="MONDO:0000001",
            predicate=Coding(code="oio:hasExactSynonym", label="exact synonym"),
            object="some disease",
            subject_label="Test Disease",
            object_label="some disease",
            statement_text="Test Disease has exact synonym 'some disease'",
        )
        data = stmt.model_dump(mode="json", exclude_none=True)
        result = SieveStatement.model_validate(data)
        assert result.subject == "MONDO:0000001"
        assert result.subject_label == "Test Disease"
        assert result.predicate is not None
        assert result.predicate.code == "oio:hasExactSynonym"

    def test_concordance_item_roundtrip(self) -> None:
        item = ConcordanceItem(
            id="conc-1",
            type="ConcordanceItem",
            source_name="Disease Ontology",
            source_id="DOID:12345",
            source_version="2024-01-01",
            source_subject="DOID:12345",
            source_subject_label="Some Disease",
            source_predicate="oio:hasExactSynonym",
            source_predicate_label="exact synonym",
            source_object="some disease name",
            source_object_label="some disease name",
            mapping_justification="semapv:LexicalMatching",
        )
        data = item.model_dump(mode="json", exclude_none=True)
        result = ConcordanceItem.model_validate(data)
        assert result.source_name == "Disease Ontology"
        assert result.source_subject == "DOID:12345"

    def test_agent_contribution_roundtrip(self) -> None:
        contrib = AgentContribution(
            id="ac-1",
            type="AgentContribution",
            contributor=Agent(id="person-1", type="Agent", name="Dr. Smith"),
            trust_level=TrustLevel.curator,
            channel=ContributionChannel.issue_tracker,
            contribution_type=ContributionType.review,
            reference="https://github.com/org/repo/issues/123",
            content="This synonym looks correct",
            date="2024-01-15",
        )
        data = contrib.model_dump(mode="json", exclude_none=True)
        result = AgentContribution.model_validate(data)
        assert result.contributor is not None
        assert result.contributor.name == "Dr. Smith"
        assert result.trust_level == "curator"

    def test_sieve_document_roundtrip(self) -> None:
        doc = SieveDocument(
            id="doc-1",
            type="SieveDocument",
            pmid="PMID:12345678",
            title="A Study on Diseases",
            quote="The disease is characterized by...",
            quote_location="Section 3, paragraph 2",
        )
        data = doc.model_dump(mode="json", exclude_none=True)
        result = SieveDocument.model_validate(data)
        assert result.pmid == "PMID:12345678"
        assert result.quote == "The disease is characterized by..."

    def test_computational_result_roundtrip(self) -> None:
        comp = ComputationalResult(
            id="comp-1",
            type="ComputationalResult",
            value="0.95",
            method_name="Semantic Similarity",
            method_id="OBI:0001234",
            parameters="threshold=0.8",
            software_version="1.2.3",
        )
        data = comp.model_dump(mode="json", exclude_none=True)
        result = ComputationalResult.model_validate(data)
        assert result.method_name == "Semantic Similarity"
        assert result.value == "0.95"

    def test_evidence_line_roundtrip(self) -> None:
        line = SieveEvidenceLine(
            id="el-1",
            type="SieveEvidenceLine",
            direction_of_evidence_provided="supports",
            strength_of_evidence_provided="strong",
            has_evidence_items=[
                ConcordanceItem(
                    id="conc-1",
                    type="ConcordanceItem",
                    source_name="DOID",
                    source_subject="DOID:1",
                    source_object="test",
                ),
            ],
        )
        data = line.model_dump(mode="json", exclude_none=True)
        result = SieveEvidenceLine.model_validate(data)
        assert result.direction_of_evidence_provided == "supports"
        assert result.has_evidence_items is not None
        assert len(result.has_evidence_items) == 1

    def test_evidence_synthesis_roundtrip(self) -> None:
        synth = EvidenceSynthesis(
            summary="Strong concordance from multiple sources.",
            score=Score(value=0.85, description="Weighted concordance"),
            direction=EvidenceDirection.supports,
            cited_evidence=["el-1", "el-2"],
        )
        data = synth.model_dump(mode="json", exclude_none=True)
        result = EvidenceSynthesis.model_validate(data)
        assert result.score is not None
        assert result.score.value == 0.85
        assert result.direction == "supports"

    def test_evidence_packet_roundtrip(self) -> None:
        """Full EvidencePacket with mixed evidence items roundtrips."""
        packet = EvidencePacket(
            id="pkt-test-001",
            statement=SieveStatement(
                id="stmt-1",
                type="SieveStatement",
                subject="MONDO:0000001",
                predicate=Coding(code="oio:hasExactSynonym"),
                object="test disease",
                subject_label="Mondo Disease",
                object_label="test disease",
            ),
            status=CurationStatus.UNREVIEWED,
            has_evidence_lines=[
                SieveEvidenceLine(
                    id="el-1",
                    type="SieveEvidenceLine",
                    direction_of_evidence_provided="supports",
                    has_evidence_items=[
                        ConcordanceItem(
                            id="conc-1",
                            type="ConcordanceItem",
                            source_name="DOID",
                            source_subject="DOID:1234",
                            source_object="test disease",
                        ),
                        AgentContribution(
                            id="ac-1",
                            type="AgentContribution",
                            contributor=Agent(id="a1", type="Agent", name="Curator"),
                            trust_level=TrustLevel.curator,
                            content="Confirmed",
                        ),
                        SieveDocument(
                            id="doc-1",
                            type="SieveDocument",
                            pmid="PMID:99999",
                            title="Relevant Paper",
                            quote="Evidence text",
                        ),
                    ],
                ),
            ],
            evidence_synthesis=EvidenceSynthesis(
                summary="Good concordance",
                score=Score(value=0.9),
                direction=EvidenceDirection.supports,
            ),
            created=date(2024, 1, 1),
            updated=date(2024, 6, 15),
        )
        data = packet.model_dump(mode="json", exclude_none=True)
        result = EvidencePacket.model_validate(data)
        assert result.id == "pkt-test-001"
        assert result.statement.subject_label == "Mondo Disease"
        assert result.has_evidence_lines is not None
        assert len(result.has_evidence_lines) == 1
        items = result.has_evidence_lines[0].has_evidence_items
        assert items is not None
        assert len(items) == 3
        assert result.evidence_synthesis is not None
        assert result.evidence_synthesis.score is not None
        assert result.evidence_synthesis.score.value == 0.9

    def test_resource_snapshot_roundtrip(self) -> None:
        snap = ResourceSnapshot(
            resource_id="doid",
            resource_name="Disease Ontology",
            version="2024-01-01",
            entity_count=10000,
            entities=[
                EntitySnapshot(
                    id="DOID:1234",
                    label="Test Disease",
                    definition="A test disease.",
                    synonyms=[
                        Synonym(value="test syn", scope=SynonymScope.EXACT),
                    ],
                ),
            ],
        )
        data = snap.model_dump(mode="json", exclude_none=True)
        result = ResourceSnapshot.model_validate(data)
        assert result.resource_id == "doid"
        assert result.entities is not None
        assert len(result.entities) == 1
        assert result.entities[0].synonyms is not None
        assert result.entities[0].synonyms[0].value == "test syn"

    def test_synonym_with_orcids_and_pmids(self) -> None:
        """Synonym.orcids and Synonym.pmids serialize correctly."""
        syn = Synonym(
            value="test synonym",
            scope=SynonymScope.EXACT,
            orcids=["0000-0001-2345-6789"],
            pmids=["PMID:12345"],
        )
        data = syn.model_dump(mode="json", exclude_none=True)
        result = Synonym.model_validate(data)
        assert result.orcids == ["0000-0001-2345-6789"]
        assert result.pmids == ["PMID:12345"]

    def test_entity_snapshot_definition_pmids(self) -> None:
        """EntitySnapshot.definition_pmids and definition_orcids serialize correctly."""
        entity = EntitySnapshot(
            id="MONDO:0000001",
            label="Test",
            definition_pmids=["PMID:11111", "PMID:22222"],
            definition_orcids=["0000-0001-1234-5678"],
        )
        data = entity.model_dump(mode="json", exclude_none=True)
        result = EntitySnapshot.model_validate(data)
        assert result.definition_pmids == ["PMID:11111", "PMID:22222"]
        assert result.definition_orcids == ["0000-0001-1234-5678"]


# =============================================================================
# Polymorphism / loader tests
# =============================================================================


class TestPolymorphism:
    """Evidence item polymorphism via _convert_evidence_items works."""

    def test_convert_concordance_item(self) -> None:
        data = {
            "id": "pkt-1",
            "statement": {"id": "s1", "type": "SieveStatement", "subject": "X", "object": "Y"},
            "status": "UNREVIEWED",
            "has_evidence_lines": [
                {
                    "id": "el-1",
                    "type": "SieveEvidenceLine",
                    "has_evidence_items": [
                        {
                            "id": "ci-1",
                            "type": "ConcordanceItem",
                            "source_name": "DOID",
                            "source_subject": "DOID:1",
                            "source_object": "test",
                        }
                    ],
                }
            ],
        }
        result = _convert_evidence_items(data)
        item = result["has_evidence_lines"][0]["has_evidence_items"][0]
        assert isinstance(item, ConcordanceItem)
        assert item.source_name == "DOID"

    def test_convert_mixed_evidence_items(self) -> None:
        data = {
            "has_evidence_lines": [
                {
                    "has_evidence_items": [
                        {"id": "ci-1", "type": "ConcordanceItem", "source_name": "DOID"},
                        {"id": "ac-1", "type": "AgentContribution", "content": "Looks good"},
                        {"id": "doc-1", "type": "SieveDocument", "title": "Paper"},
                    ]
                }
            ]
        }
        result = _convert_evidence_items(data)
        items = result["has_evidence_lines"][0]["has_evidence_items"]
        assert isinstance(items[0], ConcordanceItem)
        assert isinstance(items[1], AgentContribution)
        assert isinstance(items[2], SieveDocument)

    def test_evidence_item_types_mapping(self) -> None:
        """EVIDENCE_ITEM_TYPES contains expected entries including aliases."""
        assert "ConcordanceItem" in EVIDENCE_ITEM_TYPES
        assert "SieveDocument" in EVIDENCE_ITEM_TYPES
        assert "ComputationalResult" in EVIDENCE_ITEM_TYPES
        # Aliases
        assert "Document" in EVIDENCE_ITEM_TYPES
        assert EVIDENCE_ITEM_TYPES["Document"] is SieveDocument

    def test_convert_empty_evidence_lines(self) -> None:
        data = {"id": "pkt-1", "statement": {}}
        result = _convert_evidence_items(data)
        assert result == data  # unchanged


# =============================================================================
# Mapping utility tests
# =============================================================================


class TestMappings:
    """Mapping functions convert between predicates/directions correctly."""

    @pytest.mark.parametrize(
        "predicate,expected_code",
        [
            (StatementPredicate.EXACT_SYNONYM, "oio:hasExactSynonym"),
            (StatementPredicate.NARROW_SYNONYM, "oio:hasNarrowSynonym"),
            (StatementPredicate.BROAD_SYNONYM, "oio:hasBroadSynonym"),
            (StatementPredicate.RELATED_SYNONYM, "oio:hasRelatedSynonym"),
            (StatementPredicate.LABEL, "rdfs:label"),
            (StatementPredicate.DEFINITION, "IAO:0000115"),
            (StatementPredicate.PARENT, "rdfs:subClassOf"),
        ],
    )
    def test_predicate_to_coding(self, predicate: StatementPredicate, expected_code: str) -> None:
        coding = predicate_to_coding(predicate)
        assert coding.code == expected_code
        assert isinstance(coding, Coding)

    @pytest.mark.parametrize(
        "code,expected_predicate",
        [
            ("oio:hasExactSynonym", StatementPredicate.EXACT_SYNONYM),
            ("oio:hasNarrowSynonym", StatementPredicate.NARROW_SYNONYM),
            ("oio:hasBroadSynonym", StatementPredicate.BROAD_SYNONYM),
            ("oio:hasRelatedSynonym", StatementPredicate.RELATED_SYNONYM),
            ("rdfs:label", StatementPredicate.LABEL),
            ("IAO:0000115", StatementPredicate.DEFINITION),
            ("rdfs:subClassOf", StatementPredicate.PARENT),
        ],
    )
    def test_coding_to_predicate(self, code: str, expected_predicate: StatementPredicate) -> None:
        coding = Coding(code=code)
        result = coding_to_predicate(coding)
        assert result == expected_predicate

    def test_coding_to_predicate_unknown(self) -> None:
        coding = Coding(code="unknown:code")
        assert coding_to_predicate(coding) is None

    @pytest.mark.parametrize(
        "direction,expected",
        [
            (EvidenceDirection.supports, "supports"),
            (EvidenceDirection.disputes, "disputes"),
            (EvidenceDirection.neutral, "neutral"),
        ],
    )
    def test_direction_to_sieve(self, direction: EvidenceDirection, expected: str) -> None:
        assert direction_to_sieve(direction) == expected

    @pytest.mark.parametrize(
        "direction_str,expected",
        [
            ("supports", EvidenceDirection.supports),
            ("disputes", EvidenceDirection.disputes),
            ("neutral", EvidenceDirection.neutral),
        ],
    )
    def test_sieve_to_direction(self, direction_str: str, expected: EvidenceDirection) -> None:
        assert sieve_to_direction(direction_str) == expected

    def test_predicate_to_coding_from_string(self) -> None:
        """Can pass string value instead of enum."""
        coding = predicate_to_coding("EXACT_SYNONYM")
        assert coding.code == "oio:hasExactSynonym"


# =============================================================================
# Inheritance contract tests
# =============================================================================


class TestInheritance:
    """Key inheritance relationships are maintained."""

    def test_sieve_statement_is_statement(self) -> None:
        assert issubclass(SieveStatement, Statement)

    def test_sieve_evidence_line_is_evidence_line(self) -> None:
        assert issubclass(SieveEvidenceLine, EvidenceLine)

    def test_concordance_item_has_type_field(self) -> None:
        item = ConcordanceItem(id="ci-1", type="ConcordanceItem", source_name="DOID")
        assert item.type == "ConcordanceItem"

    def test_curation_activity_is_contribution(self) -> None:
        assert issubclass(CurationActivity, Contribution)

    def test_statement_is_information_entity(self) -> None:
        assert issubclass(Statement, InformationEntity)

    def test_information_entity_is_entity(self) -> None:
        assert issubclass(InformationEntity, Entity)

    def test_evidence_line_is_information_entity(self) -> None:
        assert issubclass(EvidenceLine, InformationEntity)

    def test_document_is_information_entity(self) -> None:
        assert issubclass(Document, InformationEntity)
