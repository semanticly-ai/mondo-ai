"""Tests for the synthesis agent module."""

import pytest

from mondo_ai.agents.prompts import (
    SYSTEM_PROMPT,
    SYNTHESIS_PROMPT_TEMPLATE,
    build_synthesis_prompt,
    format_evidence_section,
)
from mondo_ai.agents.synthesis import SynthesisAgent, SynthesisResponse
from mondo_ai.datamodel import (
    ConcordanceItem,
    CurationStatus,
    EvidenceItemType,
    EvidencePacket,
    RecommendedAction,
    SieveDocument,
    SieveEvidenceLine,
    SieveStatement,
    StatementPredicate,
    predicate_to_coding,
)


class TestPromptTemplates:
    """Tests for prompt templates."""

    def test_system_prompt_exists(self):
        """Test that system prompt exists and has content."""
        assert SYSTEM_PROMPT
        assert "ontology curator" in SYSTEM_PROMPT.lower()
        assert "ADD" in SYSTEM_PROMPT
        assert "CONFIRM" in SYSTEM_PROMPT
        assert "REJECT" in SYSTEM_PROMPT
        assert "REVIEW" in SYSTEM_PROMPT

    def test_synthesis_prompt_template_has_placeholders(self):
        """Test that synthesis prompt has required placeholders."""
        assert "{subject_label}" in SYNTHESIS_PROMPT_TEMPLATE
        assert "{subject_id}" in SYNTHESIS_PROMPT_TEMPLATE
        assert "{predicate}" in SYNTHESIS_PROMPT_TEMPLATE
        assert "{object_value}" in SYNTHESIS_PROMPT_TEMPLATE
        assert "{evidence_section}" in SYNTHESIS_PROMPT_TEMPLATE
        assert "{evidence_score}" in SYNTHESIS_PROMPT_TEMPLATE


class TestFormatEvidenceSection:
    """Tests for format_evidence_section function."""

    def test_empty_evidence(self):
        """Test formatting empty evidence list."""
        result = format_evidence_section([])
        assert result == "*No evidence lines*"

    def test_concordance_evidence(self):
        """Test formatting concordance evidence."""
        item = ConcordanceItem(
            id="ev_001",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="OMIM",
            sourceSubject="OMIM:123456",
            description="Cross-reference from OMIM",
        )
        line = SieveEvidenceLine(
            id="el_001",
            type="SieveEvidenceLine",
            hasEvidenceItems=[item],
            directionOfEvidenceProvided="supports",
            strengthOfEvidenceProvided="strong",
            scoreOfEvidenceProvided=1.0,
        )
        result = format_evidence_section([line])
        assert "ev_001" in result
        assert "ConcordanceItem" in result
        assert "supports" in result
        assert "OMIM" in result

    def test_literature_evidence(self):
        """Test formatting literature evidence."""
        item = SieveDocument(
            id="ev_002",
            type="SieveDocument",
            pmid="12345678",
            title="Test Paper Title",
            quote="This is a quote from the paper.",
        )
        line = SieveEvidenceLine(
            id="el_002",
            type="SieveEvidenceLine",
            hasEvidenceItems=[item],
            directionOfEvidenceProvided="supports",
            strengthOfEvidenceProvided="moderate",
            scoreOfEvidenceProvided=0.5,
        )
        result = format_evidence_section([line])
        assert "ev_002" in result
        # Check that item type or Document shows up
        assert "12345678" in result
        assert "Test Paper Title" in result
        assert "This is a quote" in result

    def test_multiple_evidence_lines(self):
        """Test formatting multiple evidence lines."""
        item1 = ConcordanceItem(
            id="ev_001",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="OMIM",
            sourceSubject="OMIM:123456",
        )
        item2 = ConcordanceItem(
            id="ev_002",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="DOID",
            sourceSubject="DOID:789",
        )
        lines = [
            SieveEvidenceLine(
                id="el_001",
                type="SieveEvidenceLine",
                hasEvidenceItems=[item1],
                directionOfEvidenceProvided="supports",
            ),
            SieveEvidenceLine(
                id="el_002",
                type="SieveEvidenceLine",
                hasEvidenceItems=[item2],
                directionOfEvidenceProvided="disputes",
            ),
        ]
        result = format_evidence_section(lines)
        assert "ev_001" in result
        assert "ev_002" in result


class TestBuildSynthesisPrompt:
    """Tests for build_synthesis_prompt function."""

    def _make_packet(self) -> EvidencePacket:
        """Create a test packet."""
        item = ConcordanceItem(
            id="ev_001",
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName="OMIM",
            sourceSubject="OMIM:123456",
        )
        line = SieveEvidenceLine(
            id="el_001",
            type="SieveEvidenceLine",
            hasEvidenceItems=[item],
            directionOfEvidenceProvided="supports",
            scoreOfEvidenceProvided=0.75,
        )
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
            hasEvidenceLines=[line],
        )

    def test_builds_complete_prompt(self):
        """Test that build_synthesis_prompt creates a complete prompt."""
        packet = self._make_packet()
        prompt = build_synthesis_prompt(packet)

        # Check statement section
        assert "diabetes mellitus" in prompt
        assert "MONDO:0005015" in prompt
        assert "diabetes" in prompt
        # Predicate is now a label from Coding
        assert "exact synonym" in prompt.lower()

        # Check evidence section
        assert "ev_001" in prompt
        assert "OMIM" in prompt

        # Check score
        assert "0.75" in prompt

    def test_handles_missing_label(self):
        """Test handling of missing subject label."""
        packet = self._make_packet()
        packet.statement.subjectLabel = None
        prompt = build_synthesis_prompt(packet)
        assert "Unknown" in prompt

    def test_handles_empty_evidence(self):
        """Test handling of empty evidence list."""
        packet = self._make_packet()
        packet.hasEvidenceLines = []
        prompt = build_synthesis_prompt(packet)
        assert "*No evidence lines*" in prompt


class TestSynthesisResponse:
    """Tests for SynthesisResponse model."""

    def test_valid_response(self):
        """Test creating a valid response."""
        response = SynthesisResponse(
            action="ADD",
            confidence=0.9,
            summary="Strong concordance evidence supports this synonym.",
            key_evidence=["ev_001", "ev_002"],
        )
        assert response.action == "ADD"
        assert response.confidence == 0.9
        assert len(response.key_evidence) == 2

    def test_default_key_evidence(self):
        """Test that key_evidence defaults to empty list."""
        response = SynthesisResponse(
            action="REVIEW",
            confidence=0.5,
            summary="Needs human review.",
        )
        assert response.key_evidence == []


class TestSynthesisAgentInit:
    """Tests for SynthesisAgent initialization."""

    def test_anthropic_provider(self):
        """Test initialization with anthropic provider (without API key)."""
        with pytest.raises(ValueError, match="No API key found"):
            SynthesisAgent(provider="anthropic")

    def test_openai_provider(self):
        """Test initialization with openai provider (without API key)."""
        with pytest.raises(ValueError, match="No API key found"):
            SynthesisAgent(provider="openai")

    def test_unknown_provider(self):
        """Test initialization with unknown provider."""
        with pytest.raises(ValueError, match="Unknown provider"):
            SynthesisAgent(provider="unknown")

    def test_parse_action_valid(self):
        """Test parsing valid action strings."""
        from unittest.mock import patch

        # Mock the _initialize_agent method to avoid pydantic-ai initialization
        with patch.object(SynthesisAgent, "_initialize_agent"):
            import os
            old_key = os.environ.get("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = "test-key"
            try:
                agent = SynthesisAgent(provider="anthropic")
                assert agent._parse_action("ADD") == RecommendedAction.ADD
                assert agent._parse_action("add") == RecommendedAction.ADD
                assert agent._parse_action("CONFIRM") == RecommendedAction.CONFIRM
                assert agent._parse_action("REJECT") == RecommendedAction.REJECT
                assert agent._parse_action("REVIEW") == RecommendedAction.REVIEW
            finally:
                if old_key:
                    os.environ["ANTHROPIC_API_KEY"] = old_key
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_parse_action_unknown(self):
        """Test parsing unknown action defaults to REVIEW."""
        from unittest.mock import patch

        # Mock the _initialize_agent method to avoid pydantic-ai initialization
        with patch.object(SynthesisAgent, "_initialize_agent"):
            import os
            old_key = os.environ.get("ANTHROPIC_API_KEY")
            os.environ["ANTHROPIC_API_KEY"] = "test-key"
            try:
                agent = SynthesisAgent(provider="anthropic")
                assert agent._parse_action("UNKNOWN") == RecommendedAction.REVIEW
                assert agent._parse_action("") == RecommendedAction.REVIEW
            finally:
                if old_key:
                    os.environ["ANTHROPIC_API_KEY"] = old_key
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
