"""Evidence packet enrichment with PubMed literature.

This module adds SieveDocument evidence items to packets by searching PubMed
for relevant papers about the disease and assertion.

Example:
    >>> from mondo_ai.enrichment import PacketEnricher
    >>> enricher = PacketEnricher()
    >>> enriched_packet = enricher.enrich(packet)  # doctest: +SKIP
"""

from datetime import date
from uuid import uuid4

from mondo_ai.datamodel import (
    EvidenceDirection,
    EvidenceItemType,
    EvidencePacket,
    EvidenceStrength,
    SieveDocument,
    SieveEvidenceLine,
)
from mondo_ai.enrichment.pubmed import PubMedClient, PubMedPaper, build_search_query


def _generate_evidence_line_id() -> str:
    """Generate unique evidence line ID."""
    return f"el_lit_{uuid4().hex[:8]}"


def _generate_evidence_item_id() -> str:
    """Generate unique evidence item ID."""
    return f"ev_lit_{uuid4().hex[:8]}"


def has_literature_evidence(packet: EvidencePacket) -> bool:
    """Check if a packet already has literature evidence.

    Args:
        packet: The evidence packet to check.

    Returns:
        True if the packet contains at least one SieveDocument evidence item.

    Example:
        >>> from mondo_ai.datamodel import EvidencePacket, SieveStatement
        >>> packet = EvidencePacket(id="pkt1", statement=SieveStatement(id="s1"))  # doctest: +SKIP
        >>> has_literature_evidence(packet)  # doctest: +SKIP
        False
    """
    if not packet.has_evidence_lines:
        return False

    for line in packet.has_evidence_lines:
        if not line.has_evidence_items:
            continue
        for item in line.has_evidence_items:
            # Check by type string (SieveDocument) or evidence_item_type enum (Document)
            if getattr(item, "type", None) == "SieveDocument":
                return True
            if getattr(item, "evidence_item_type", None) == EvidenceItemType.Document:
                return True
    return False


class PacketEnricher:
    """Enriches evidence packets with PubMed literature evidence."""

    def __init__(
        self,
        pubmed_client: PubMedClient | None = None,
        max_papers: int = 3,
        min_score_threshold: float | None = None,
        skip_enriched: bool = True,
    ) -> None:
        """Initialize the enricher.

        Args:
            pubmed_client: PubMed client instance (creates default if None).
            max_papers: Maximum papers to add per packet.
            min_score_threshold: Only enrich packets below this score (None = enrich all).
            skip_enriched: Skip packets that already have literature evidence.
        """
        self.pubmed = pubmed_client or PubMedClient()
        self.max_papers = max_papers
        self.min_score_threshold = min_score_threshold
        self.skip_enriched = skip_enriched

    def _paper_to_evidence_line(self, paper: PubMedPaper) -> SieveEvidenceLine:
        """Convert a PubMed paper to SieveEvidenceLine containing SieveDocument.

        Args:
            paper: The PubMed paper.

        Returns:
            SieveEvidenceLine containing a SieveDocument.
        """
        # Extract a snippet from the abstract if available
        quote = None
        if paper.abstract:
            # Take first 500 characters as quote
            quote = paper.abstract[:500]
            if len(paper.abstract) > 500:
                quote += "..."

        # Build URL if PMID available
        urls = []
        if paper.pmid:
            urls.append(f"https://pubmed.ncbi.nlm.nih.gov/{paper.pmid}/")

        doc = SieveDocument(
            id=_generate_evidence_item_id(),
            type="SieveDocument",
            title=paper.title,
            pmid=paper.pmid,
            quote=quote,
            quote_location="Abstract" if quote else None,
            urls=urls if urls else None,
            description=f"Literature from {paper.journal or 'PubMed'}: {paper.title[:100]}",
        )

        return SieveEvidenceLine(
            id=_generate_evidence_line_id(),
            type="SieveEvidenceLine",
            has_evidence_items=[doc],
            direction_of_evidence_provided=EvidenceDirection.supports.value,
            strength_of_evidence_provided=EvidenceStrength.moderate.value,
            score_of_evidence_provided=0.5,  # Moderate default score for literature
        )

    def _get_current_score(self, packet: EvidencePacket) -> float:
        """Get the current evidence score from a packet.

        Args:
            packet: The evidence packet.

        Returns:
            Current score (calculated from evidence lines if no synthesis).
        """
        if packet.evidence_synthesis and packet.evidence_synthesis.score:
            return packet.evidence_synthesis.score.value or 0
        # Calculate from evidence lines if no synthesis
        if not packet.has_evidence_lines:
            return 0
        total_score = sum(
            line.score_of_evidence_provided or 0 for line in packet.has_evidence_lines
        )
        return total_score / len(packet.has_evidence_lines) if packet.has_evidence_lines else 0

    def should_enrich(self, packet: EvidencePacket) -> bool:
        """Check if a packet should be enriched.

        Skips packets that already have literature evidence (when skip_enriched=True).
        Also skips packets above the score threshold (when min_score_threshold is set).

        Args:
            packet: The evidence packet.

        Returns:
            True if the packet should be enriched.
        """
        # Skip if already has literature evidence (incremental mode)
        if self.skip_enriched and has_literature_evidence(packet):
            return False

        # Skip if above score threshold
        if self.min_score_threshold is not None:
            score = self._get_current_score(packet)
            if score >= self.min_score_threshold:
                return False

        return True

    def enrich(self, packet: EvidencePacket) -> EvidencePacket:
        """Enrich a packet with PubMed literature evidence.

        Args:
            packet: The evidence packet to enrich.

        Returns:
            The enriched packet (modified in place).
        """
        if not self.should_enrich(packet):
            return packet

        # Build search query from the statement
        disease_label = packet.statement.subject_label or ""
        object_value = packet.statement.object or ""

        # For synonyms, search for both the disease and the synonym
        query = build_search_query(disease_label, object_value)

        # Search and fetch papers
        papers = self.pubmed.search_and_fetch(query, max_results=self.max_papers)

        # Convert to evidence lines and add to packet
        for paper in papers:
            evidence_line = self._paper_to_evidence_line(paper)
            if packet.has_evidence_lines is None:
                packet.has_evidence_lines = []
            packet.has_evidence_lines.append(evidence_line)

        packet.updated = date.today()
        return packet

    def enrich_packets(self, packets: list[EvidencePacket]) -> list[EvidencePacket]:
        """Enrich multiple packets with literature evidence.

        Args:
            packets: List of evidence packets.

        Returns:
            List of enriched packets.
        """
        enriched = []
        for packet in packets:
            enriched_packet = self.enrich(packet)
            enriched.append(enriched_packet)
        return enriched
