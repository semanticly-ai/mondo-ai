"""Evidence packet builder.

This module builds EvidencePackets from SieveStatements,
adding ConcordanceItems as evidence items wrapped in SieveEvidenceLines.

ID Generation Contract:
    All IDs are deterministic and content-addressed:
    - Packet ID: hash(subject + predicate_code + object)
    - Statement ID: hash(subject + predicate_code + object)
    - Evidence Line ID: hash(packet_id + source_id)
    - Evidence Item ID: hash(line_id + source_id)

    This ensures:
    1. Re-running the pipeline produces the same IDs for the same content
    2. IDs can be used for stable URLs and cross-run tracking
    3. Changes to evidence don't change the packet ID (only statement content matters)

    The ID generation is robust to schema changes because it only depends on
    the semantic content (subject, predicate code, object), not on field names
    or schema structure.

Example:
    >>> from mondo_ai.packets.builder import PacketBuilder
    >>> builder = PacketBuilder()
    >>> packet = builder.build_packet(statement, source_id, source_name, weight=0.85)  # doctest: +SKIP
"""

from __future__ import annotations

import hashlib
from datetime import date

from mondo_ai.datamodel import (
    Agent,
    AgentContribution,
    Coding,
    ConcordanceItem,
    ContributionType,
    CurationStatus,
    EvidenceDirection,
    EvidenceItemType,
    EvidencePacket,
    EvidenceStrength,
    SieveDocument,
    SieveEvidenceLine,
    SieveStatement,
    TrustLevel,
)


def _hash_content(*parts: str) -> str:
    """Generate deterministic hash from content parts.

    Args:
        *parts: String parts to hash together.

    Returns:
        Hex digest of the hash.
    """
    content = "|".join(str(p) for p in parts)
    return hashlib.sha256(content.encode()).hexdigest()


def _generate_packet_id(subject: str, predicate: str, obj: str) -> str:
    """Generate deterministic packet ID from statement content.

    The same statement will always produce the same packet ID.

    Args:
        subject: Statement subject (e.g., MONDO:0005015).
        predicate: Statement predicate code.
        obj: Statement object value.

    Returns:
        Deterministic packet ID.
    """
    h = _hash_content(subject, predicate, obj)
    return f"pkt_{h[:12]}"


def _generate_statement_id(subject: str, predicate: str, obj: str) -> str:
    """Generate deterministic statement ID from content.

    Args:
        subject: Statement subject.
        predicate: Statement predicate code.
        obj: Statement object value.

    Returns:
        Deterministic statement ID.
    """
    h = _hash_content(subject, predicate, obj)
    return f"stmt_{h[:12]}"


def _generate_evidence_line_id(packet_id: str, source_id: str) -> str:
    """Generate deterministic evidence line ID.

    Args:
        packet_id: Parent packet ID.
        source_id: Source identifier.

    Returns:
        Deterministic evidence line ID.
    """
    h = _hash_content(packet_id, source_id)
    return f"el_{h[:8]}"


def _generate_evidence_item_id(line_id: str, source_id: str) -> str:
    """Generate deterministic evidence item ID.

    Args:
        line_id: Parent evidence line ID.
        source_id: Source identifier.

    Returns:
        Deterministic evidence item ID.
    """
    h = _hash_content(line_id, source_id)
    return f"ev_{h[:8]}"


def _score_to_strength(score: float) -> str:
    """Convert numeric score to SIEVE strength string.

    Args:
        score: Evidence score between 0 and 1.

    Returns:
        SIEVE strength string: 'strong', 'moderate', or 'weak'.
    """
    if score >= 0.8:
        return EvidenceStrength.strong.value
    if score >= 0.5:
        return EvidenceStrength.moderate.value
    return EvidenceStrength.weak.value


# Default weight for unknown sources/predicates
DEFAULT_WEIGHT = 0.7

# Evidence scores for provenance types
ORCID_EXPERT_SCORE = 0.8
PMID_CANDIDATE_SCORE = 0.4
CONSORTIUM_DECISION_SCORE = 0.6

# Mondo consortium agent (reused from mondo_extractor)
MONDO_CONSORTIUM = Agent(
    id="mondo:consortium",
    type="Agent",
    name="Mondo Disease Ontology Consortium",
    subtype="organization",
)


def _create_orcid_agent(orcid: str) -> Agent:
    """Create an Agent from an ORCID identifier.

    Args:
        orcid: ORCID identifier (with or without prefix).

    Returns:
        Agent representing the person.
    """
    orcid_id = orcid if orcid.startswith("ORCID:") else f"ORCID:{orcid}"
    return Agent(
        id=orcid_id,
        type="Agent",
        subtype="person",
    )


class PacketBuilder:
    """Build evidence packets from statements.

    This builder is stateless - weights are passed directly when building packets.
    """

    def _create_concordance_evidence_line(
        self,
        packet_id: str,
        source_id: str,
        source_name: str,
        source_entity_id: str,
        source_entity_label: str,
        mapping_justification: str,
        evidence_score: float,
    ) -> SieveEvidenceLine:
        """Create a SieveEvidenceLine containing a ConcordanceItem.

        Args:
            packet_id: Parent packet ID (for deterministic line ID).
            source_id: Source identifier (e.g., "doid").
            source_name: Human-readable source name.
            source_entity_id: Source entity CURIE.
            source_entity_label: Source entity label.
            mapping_justification: SSSOM mapping_set_id for provenance.
            evidence_score: Pre-computed evidence score (from source's content_weights).

        Returns:
            SieveEvidenceLine containing a ConcordanceItem.
        """
        line_id = _generate_evidence_line_id(packet_id, source_id)
        item_id = _generate_evidence_item_id(line_id, source_id)

        concordance_item = ConcordanceItem(
            id=item_id,
            type="ConcordanceItem",
            evidenceItemType=EvidenceItemType.ConcordanceItem,
            sourceName=source_name,
            sourceId=source_id,
            sourceSubject=source_entity_id,
            sourceSubjectLabel=source_entity_label,
            mappingJustification=mapping_justification,
            description=f"Concordance evidence from {source_name}",
        )

        return SieveEvidenceLine(
            id=line_id,
            type="SieveEvidenceLine",
            hasEvidenceItems=[concordance_item],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=_score_to_strength(evidence_score),
            scoreOfEvidenceProvided=evidence_score,
        )

    def _create_orcid_evidence_line(
        self,
        packet_id: str,
        orcid: str,
        annotation_type: str = "annotation",
    ) -> SieveEvidenceLine:
        """Create a SieveEvidenceLine for ORCID expert contribution.

        Args:
            packet_id: Parent packet ID (for deterministic line ID).
            orcid: ORCID identifier of the expert.
            annotation_type: Type of annotation (e.g., "synonym", "definition").

        Returns:
            SieveEvidenceLine containing an AgentContribution.
        """
        line_id = _generate_evidence_line_id(packet_id, f"orcid:{orcid}")
        item_id = _generate_evidence_item_id(line_id, f"orcid:{orcid}")

        agent = _create_orcid_agent(orcid)
        contribution = AgentContribution(
            id=item_id,
            type="AgentContribution",
            evidenceItemType=EvidenceItemType.AgentContribution,
            contributor=agent,
            trustLevel=TrustLevel.curator,
            contributionType=ContributionType.decision,
            description=f"Expert {annotation_type} contribution by {orcid}",
        )

        return SieveEvidenceLine(
            id=line_id,
            type="SieveEvidenceLine",
            hasEvidenceItems=[contribution],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=_score_to_strength(ORCID_EXPERT_SCORE),
            scoreOfEvidenceProvided=ORCID_EXPERT_SCORE,
        )

    def _create_pmid_evidence_line(
        self,
        packet_id: str,
        pmid: str,
    ) -> SieveEvidenceLine:
        """Create a SieveEvidenceLine for PMID candidate literature.

        The document is marked as a candidate reference (unverified)
        using a subtype Coding.

        Args:
            packet_id: Parent packet ID (for deterministic line ID).
            pmid: PubMed identifier.

        Returns:
            SieveEvidenceLine containing a SieveDocument with candidate subtype.
        """
        line_id = _generate_evidence_line_id(packet_id, f"pmid:{pmid}")
        item_id = _generate_evidence_item_id(line_id, f"pmid:{pmid}")

        candidate_subtype = Coding(
            code="candidate_reference",
            label="Candidate Literature Reference",
            system="mondo:evidence_status",
        )

        document = SieveDocument(
            id=item_id,
            type="SieveDocument",
            pmid=pmid,
            subtype=candidate_subtype,
            description=f"Candidate literature reference PMID:{pmid} (unverified)",
        )

        return SieveEvidenceLine(
            id=line_id,
            type="SieveEvidenceLine",
            hasEvidenceItems=[document],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=_score_to_strength(PMID_CANDIDATE_SCORE),
            scoreOfEvidenceProvided=PMID_CANDIDATE_SCORE,
        )

    def _create_consortium_evidence_line(
        self,
        packet_id: str,
        source_version: str | None = None,
    ) -> SieveEvidenceLine:
        """Create a SieveEvidenceLine for Mondo consortium decision.

        This represents the organizational decision that the statement
        is included in the Mondo ontology.

        Args:
            packet_id: Parent packet ID (for deterministic line ID).
            source_version: Version of the Mondo ontology.

        Returns:
            SieveEvidenceLine containing an AgentContribution from consortium.
        """
        line_id = _generate_evidence_line_id(packet_id, "mondo:consortium")
        item_id = _generate_evidence_item_id(line_id, "mondo:consortium")

        version_info = f" (version: {source_version})" if source_version else ""
        contribution = AgentContribution(
            id=item_id,
            type="AgentContribution",
            evidenceItemType=EvidenceItemType.AgentContribution,
            contributor=MONDO_CONSORTIUM,
            trustLevel=TrustLevel.authority,
            contributionType=ContributionType.decision,
            description=f"Mondo consortium inclusion decision{version_info}",
        )

        return SieveEvidenceLine(
            id=line_id,
            type="SieveEvidenceLine",
            hasEvidenceItems=[contribution],
            directionOfEvidenceProvided=EvidenceDirection.supports.value,
            strengthOfEvidenceProvided=_score_to_strength(CONSORTIUM_DECISION_SCORE),
            scoreOfEvidenceProvided=CONSORTIUM_DECISION_SCORE,
        )

    def build_packet(
        self,
        statement: SieveStatement,
        source_id: str,
        source_name: str,
        source_entity_id: str,
        source_entity_label: str,
        mapping_predicate: str = "unknown",
        weight: float = DEFAULT_WEIGHT,
        orcids: list[str] | None = None,
        pmids: list[str] | None = None,
        is_mondo_source: bool = False,
        source_version: str | None = None,
    ) -> EvidencePacket:
        """Build an evidence packet for a statement.

        The packet ID is deterministic based on the statement content,
        so the same statement will always produce the same packet ID.

        For non-Mondo sources, creates ConcordanceItem evidence.
        For Mondo source, creates consortium decision evidence instead.
        Additionally creates expert evidence lines for ORCIDs and
        candidate literature evidence lines for PMIDs.

        Args:
            statement: The SIEVE statement.
            source_id: Source identifier (e.g., "doid").
            source_name: Human-readable source name.
            source_entity_id: Source entity CURIE.
            source_entity_label: Source entity label.
            mapping_predicate: SSSOM mapping_set_id for provenance.
            weight: Evidence weight for this source+predicate combination.
            orcids: List of ORCID identifiers found in annotation sources.
            pmids: List of PMID identifiers found in annotation sources.
            is_mondo_source: True if this is from Mondo (uses consortium decision).
            source_version: Version of the source ontology.

        Returns:
            EvidencePacket with appropriate evidence lines.
        """
        # Generate deterministic packet ID from statement content
        predicate_code = ""
        if statement.predicate:
            predicate_code = statement.predicate.code or ""
        packet_id = _generate_packet_id(
            statement.subject or "",
            predicate_code,
            statement.object or "",
        )

        evidence_lines: list[SieveEvidenceLine] = []

        # For Mondo source: consortium decision instead of concordance
        # For external sources: concordance evidence
        if is_mondo_source:
            evidence_lines.append(
                self._create_consortium_evidence_line(
                    packet_id=packet_id,
                    source_version=source_version,
                )
            )
        else:
            evidence_lines.append(
                self._create_concordance_evidence_line(
                    packet_id=packet_id,
                    source_id=source_id,
                    source_name=source_name,
                    source_entity_id=source_entity_id,
                    source_entity_label=source_entity_label,
                    mapping_justification=mapping_predicate,
                    evidence_score=weight,
                )
            )

        # Add ORCID expert evidence lines
        if orcids:
            for orcid in orcids:
                evidence_lines.append(
                    self._create_orcid_evidence_line(
                        packet_id=packet_id,
                        orcid=orcid,
                    )
                )

        # Add PMID candidate literature evidence lines
        if pmids:
            for pmid in pmids:
                evidence_lines.append(
                    self._create_pmid_evidence_line(
                        packet_id=packet_id,
                        pmid=pmid,
                    )
                )

        return EvidencePacket(
            id=packet_id,
            statement=statement,
            status=CurationStatus.UNREVIEWED,
            hasEvidenceLines=evidence_lines,
            created=date.today(),
        )

    def add_evidence_to_packet(
        self,
        packet: EvidencePacket,
        source_id: str,
        source_name: str,
        source_entity_id: str,
        source_entity_label: str,
        mapping_predicate: str = "unknown",
        weight: float = DEFAULT_WEIGHT,
        orcids: list[str] | None = None,
        pmids: list[str] | None = None,
        is_mondo_source: bool = False,
        source_version: str | None = None,
    ) -> EvidencePacket:
        """Add evidence from another source to existing packet.

        For non-Mondo sources, adds ConcordanceItem evidence.
        For Mondo source, adds consortium decision evidence.
        Additionally adds expert evidence lines for ORCIDs and
        candidate literature evidence lines for PMIDs.

        Args:
            packet: Existing evidence packet.
            source_id: Source identifier.
            source_name: Human-readable source name.
            source_entity_id: Source entity CURIE.
            source_entity_label: Source entity label.
            mapping_predicate: SSSOM mapping_set_id for provenance.
            weight: Evidence weight for this source+predicate combination.
            orcids: List of ORCID identifiers found in annotation sources.
            pmids: List of PMID identifiers found in annotation sources.
            is_mondo_source: True if this is from Mondo (uses consortium decision).
            source_version: Version of the source ontology.

        Returns:
            Updated packet with additional evidence.
        """
        if packet.hasEvidenceLines is None:
            packet.hasEvidenceLines = []

        # For Mondo source: consortium decision instead of concordance
        # For external sources: concordance evidence
        if is_mondo_source:
            packet.hasEvidenceLines.append(
                self._create_consortium_evidence_line(
                    packet_id=packet.id,
                    source_version=source_version,
                )
            )
        else:
            packet.hasEvidenceLines.append(
                self._create_concordance_evidence_line(
                    packet_id=packet.id,
                    source_id=source_id,
                    source_name=source_name,
                    source_entity_id=source_entity_id,
                    source_entity_label=source_entity_label,
                    mapping_justification=mapping_predicate,
                    evidence_score=weight,
                )
            )

        # Add ORCID expert evidence lines
        if orcids:
            for orcid in orcids:
                packet.hasEvidenceLines.append(
                    self._create_orcid_evidence_line(
                        packet_id=packet.id,
                        orcid=orcid,
                    )
                )

        # Add PMID candidate literature evidence lines
        if pmids:
            for pmid in pmids:
                packet.hasEvidenceLines.append(
                    self._create_pmid_evidence_line(
                        packet_id=packet.id,
                        pmid=pmid,
                    )
                )

        packet.updated = date.today()
        return packet


class PacketMerger:
    """Merge packets with the same statement from different sources."""

    def __init__(self) -> None:
        """Initialize merger."""
        self._packets_by_statement: dict[str, EvidencePacket] = {}

    def add_packet(self, packet: EvidencePacket) -> None:
        """Add packet, merging if statement already exists."""
        stmt_id = packet.statement.id
        if stmt_id in self._packets_by_statement:
            existing = self._packets_by_statement[stmt_id]
            if packet.hasEvidenceLines:
                if existing.hasEvidenceLines is None:
                    existing.hasEvidenceLines = []
                existing.hasEvidenceLines.extend(packet.hasEvidenceLines)
                existing.updated = date.today()
        else:
            self._packets_by_statement[stmt_id] = packet

    def get_packets(self) -> list[EvidencePacket]:
        """Get all merged packets."""
        return list(self._packets_by_statement.values())
