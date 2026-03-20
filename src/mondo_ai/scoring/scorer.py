"""Evidence scoring module.

This module calculates the Net Evidence Ratio (NER) for evidence packets
based on their SIEVE evidence lines.

The NER formula is:
    NER = (S+ - S-) / (S+ + S- + S?)

Where:
    S+ = sum of scores for evidence lines that support
    S- = sum of scores for evidence lines that dispute
    S? = sum of scores for evidence lines that are neutral

NER ranges from -1 (all disputing) to +1 (all supporting).

Example:
    >>> from mondo_ai.scoring.scorer import Scorer
    >>> scorer = Scorer()
    >>> score = scorer.calculate_ner(packet)
"""

from mondo_ai.datamodel import (
    EvidenceDirection,
    EvidencePacket,
    Score,
)


class Scorer:
    """Calculate evidence scores for packets."""

    def __init__(
        self,
        accept_threshold: float = 0.7,
        reject_threshold: float = -0.3,
    ) -> None:
        """Initialize scorer with thresholds.

        Args:
            accept_threshold: Minimum NER to auto-accept.
            reject_threshold: Maximum NER to auto-reject.
        """
        self.accept_threshold = accept_threshold
        self.reject_threshold = reject_threshold

    def calculate_ner(self, packet: EvidencePacket) -> float:
        """Calculate Net Evidence Ratio for a packet.

        Args:
            packet: Evidence packet with evidence lines.

        Returns:
            NER score between -1 and 1.

        Example:
            >>> from mondo_ai.datamodel import (
            ...     EvidencePacket, SieveStatement, SieveEvidenceLine,
            ...     ConcordanceItem, CurationStatus, EvidenceDirection,
            ...     Coding, EvidenceItemType
            ... )
            >>> from mondo_ai.datamodel.mappings import predicate_to_coding
            >>> from mondo_ai.datamodel import StatementPredicate
            >>> stmt = SieveStatement(
            ...     id="test",
            ...     type="SieveStatement",
            ...     subject="MONDO:0000001",
            ...     predicate=predicate_to_coding(StatementPredicate.EXACT_SYNONYM),
            ...     object="test synonym"
            ... )
            >>> item = ConcordanceItem(
            ...     id="ev1",
            ...     type="ConcordanceItem",
            ...     sourceName="test",
            ... )
            >>> line = SieveEvidenceLine(
            ...     id="el1",
            ...     type="SieveEvidenceLine",
            ...     hasEvidenceItems=[item],
            ...     directionOfEvidenceProvided="supports",
            ...     scoreOfEvidenceProvided=0.9,
            ... )
            >>> packet = EvidencePacket(
            ...     id="pkt1",
            ...     statement=stmt,
            ...     hasEvidenceLines=[line]
            ... )
            >>> scorer = Scorer()
            >>> score = scorer.calculate_ner(packet)
            >>> score == 1.0
            True
        """
        if not packet.hasEvidenceLines:
            return 0.0

        s_plus = 0.0  # Supporting
        s_minus = 0.0  # Disputing
        s_uncertain = 0.0  # Neutral

        for line in packet.hasEvidenceLines:
            # Get the score for this evidence line
            score = line.scoreOfEvidenceProvided or 1.0
            direction = line.directionOfEvidenceProvided or "neutral"

            if direction == EvidenceDirection.supports.value or direction == "supports":
                s_plus += score
            elif direction == EvidenceDirection.disputes.value or direction == "disputes":
                s_minus += score
            else:  # neutral
                s_uncertain += score

        total = s_plus + s_minus + s_uncertain
        if total == 0:
            return 0.0

        return (s_plus - s_minus) / total

    def score_packet(self, packet: EvidencePacket) -> EvidencePacket:
        """Calculate and set the evidence score on a packet's synthesis.

        Args:
            packet: Evidence packet to score.

        Returns:
            Packet with evidence_synthesis.score set.
        """
        ner = self.calculate_ner(packet)

        # Create or update synthesis with score
        if packet.evidence_synthesis is None:
            from mondo_ai.datamodel import EvidenceSynthesis
            packet.evidence_synthesis = EvidenceSynthesis(
                summary="Scored by deterministic evidence scorer",
                score=Score(value=ner, description="Net Evidence Ratio (NER)"),
            )
        else:
            packet.evidence_synthesis.score = Score(
                value=ner, description="Net Evidence Ratio (NER)"
            )

        return packet

    def score_packets(self, packets: list[EvidencePacket]) -> list[EvidencePacket]:
        """Score multiple packets.

        Args:
            packets: List of packets to score.

        Returns:
            Scored packets.
        """
        return [self.score_packet(p) for p in packets]

    def get_recommendation(self, score: float) -> str:
        """Get recommendation based on score.

        Args:
            score: NER score.

        Returns:
            "ACCEPT", "REJECT", or "REVIEW".
        """
        if score >= self.accept_threshold:
            return "ACCEPT"
        if score <= self.reject_threshold:
            return "REJECT"
        return "REVIEW"
