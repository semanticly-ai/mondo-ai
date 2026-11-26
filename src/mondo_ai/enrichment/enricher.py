"""Attach PubMed literature to SIEVE profile packets.

PubMed hits for a packet's claim are attached as the claim's direct evidence
(``statement.has_evidence``): Publications that nobody has read as an argument yet. They
carry no direction and no strength, so enrichment leaves the score as it is. A reader (a
curator, or the synthesis agent in ``mondo_ai.agents``) turns them into an evidence line.

Claim types for which a Publication is not evidence (the profile's claim-evidence
matrix, e.g. MappingStatement) are not enriched.

Example:
    >>> from mondo_ai.enrichment import PacketEnricher
    >>> enricher = PacketEnricher()  # doctest: +SKIP
    >>> packet = enricher.enrich(packet)  # doctest: +SKIP
"""

from datetime import date as _date
from functools import lru_cache
from typing import Any, Mapping

from linkml_runtime.utils.schemaview import SchemaView
from sieve.packet_items import iter_items
from sieve.profile_checks import permitted_evidence
from sieve.traceability import SCHEMA

from mondo_ai.enrichment.pubmed import PubMedClient, PubMedPaper, build_search_query
from mondo_ai.packets.evidence import BASIS_UNKNOWN
from mondo_ai.packets.registry import PUBMED

PUBMED_RESOURCE: dict[str, Any] = {
    "id": PUBMED,
    "type": "InformationResource",
    "label": "PubMed",
    "curie_prefixes": ["PMID"],
    "default_role": "PRIMARY",
}


@lru_cache(maxsize=1)
def _profile_schema() -> SchemaView:
    """The SIEVE profile schema the profile checks read."""
    return SchemaView(str(SCHEMA))


@lru_cache(maxsize=None)
def publications_permitted(claim_type: str) -> bool:
    """Whether a Publication is evidence for the claim type (claim-evidence matrix).

    >>> publications_permitted("SynonymStatement"), publications_permitted("MappingStatement")
    (True, False)
    """
    return "Publication" in permitted_evidence(_profile_schema(), claim_type)


def paper_to_publication(paper: PubMedPaper, retrieval_date: str) -> dict[str, Any]:
    """A Publication item for a PubMed paper, as read from PubMed on ``retrieval_date``.

    ``generation_context`` holds the PubMed publication types as MeSH Codings (or
    ``sieve:basis_unknown`` when PubMed gives none); ``date_authored`` is set only for a
    full publication date.

    >>> paper = PubMedPaper("1", "A title", None, [], None, "2020", None)
    >>> paper_to_publication(paper, "2026-11-20")["generation_context"]
    [{'code': 'sieve:basis_unknown'}]
    """
    pub: dict[str, Any] = {
        "id": paper.reference_id,
        "type": "Publication",
        "resource": PUBMED,
    }
    if paper.title:
        pub["label"] = paper.title
    if paper.publication_date:
        pub["date_authored"] = paper.publication_date
    pub["record_metadata"] = {
        "retrieved_from": PUBMED,
        "retrieval_date": retrieval_date,
    }
    pub["generation_context"] = [
        {"code": f"MESH:{ui}", "label": name} for ui, name in paper.publication_types
    ] or [dict(BASIS_UNKNOWN)]
    return pub


def claim_terms(
    statement: dict[str, Any], labels: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """The subject label and object term of a claim, for a literature search.

    Labels passed in win. Otherwise the subject label is the part of ``statement_text``
    before the predicate label, and the object is the synonym text, or the text after the
    predicate label (with a mapped record's label taken from its parentheses).

    >>> st = {"type": "SubclassStatement", "subject": "MONDO:1", "object": "MONDO:2",
    ...       "predicate": {"code": "rdfs:subClassOf", "label": "subclass of"},
    ...       "statement_text": "cystic fibrosis subclass of lung disease"}
    >>> claim_terms(st)
    ('cystic fibrosis', 'lung disease')
    >>> claim_terms(st, {"MONDO:1": "CF"})
    ('CF', 'lung disease')
    """
    labels = labels or {}
    subject, obj = statement["subject"], str(statement.get("object") or "")
    text = statement.get("statement_text") or ""
    predicate_label = (statement.get("predicate") or {}).get("label") or ""
    before, found, after = (
        text.partition(f" {predicate_label} ") if predicate_label else ("", "", "")
    )
    subject_label = labels.get(subject) or (before if found else subject)
    if obj in labels:
        return subject_label, labels[obj]
    if statement["type"] == "SynonymStatement" or not found:
        return subject_label, obj
    if after.startswith(f"{obj} (") and after.endswith(")"):
        return subject_label, after[len(obj) + 2 : -1]
    return subject_label, after


def _line_items(statement: dict[str, Any]) -> list[dict[str, Any]]:
    """The statement's direct evidence and its lines' items (into higher-order lines)."""
    out = list(statement.get("has_evidence") or [])
    todo = list(statement.get("has_evidence_lines") or [])
    while todo:
        line = todo.pop()
        for item in line.get("has_evidence_items") or []:
            out.append(item)
            if item.get("type") == "SieveEvidenceLine":
                todo.append(item)
    return out


def has_literature_evidence(packet: dict[str, Any]) -> bool:
    """Whether a Publication is attached to the claim, directly or as an item of a line.

    A Publication nested in a prior claim (a source citing a paper for its own claim) is
    that source's evidence and does not count.

    >>> has_literature_evidence({"statement": {"has_evidence": [{"id": "PMID:1", "type": "Publication"}]}})
    True
    >>> has_literature_evidence({"statement": {"has_evidence_lines": []}})
    False
    """
    return any(
        item.get("type") == "Publication" for item in _line_items(packet["statement"])
    )


def add_registry_entry(packet: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    """Put a registry entry into the packet's reference_snapshot list ``key``, replacing one with the same id."""
    entries = packet["reference_snapshot"].setdefault(key, [])
    entries[:] = [e for e in entries if e["id"] != entry["id"]] + [entry]


class PacketEnricher:
    """Attach PubMed literature to packets as direct evidence."""

    def __init__(
        self,
        pubmed_client: PubMedClient | None = None,
        max_papers: int = 3,
        min_score_threshold: float | None = None,
        skip_enriched: bool = True,
        labels: Mapping[str, str] | None = None,
        date: str | None = None,
    ) -> None:
        """Initialize the enricher.

        Args:
            pubmed_client: PubMed client instance (creates default if None).
            max_papers: Maximum papers to search for per packet.
            min_score_threshold: Only enrich packets whose score S is below this (None = all).
            skip_enriched: Skip packets that already have literature evidence.
            labels: Term labels by CURIE, used for the search instead of ``statement_text``.
            date: Retrieval date (ISO) recorded on the Publications; defaults to today.
        """
        self.pubmed = pubmed_client or PubMedClient()
        self.max_papers = max_papers
        self.min_score_threshold = min_score_threshold
        self.skip_enriched = skip_enriched
        self.labels = labels
        self.date = date or _date.today().isoformat()

    def should_enrich(self, packet: dict[str, Any]) -> bool:
        """Whether the packet should be enriched.

        No for claim types a Publication is not evidence for, for packets that already
        have literature (when ``skip_enriched``) and for packets scoring at or above
        ``min_score_threshold``.
        """
        statement = packet["statement"]
        if not publications_permitted(statement["type"]):
            return False
        if self.skip_enriched and has_literature_evidence(packet):
            return False
        if (
            self.min_score_threshold is not None
            and statement.get("score", 0.0) >= self.min_score_threshold
        ):
            return False
        return True

    def enrich(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Search PubMed for the claim and attach the hits as ``statement.has_evidence``.

        A PMID the packet already carries anywhere is not attached again. PubMed is added
        to the packet's reference_snapshot when a Publication is attached.

        Args:
            packet: The packet to enrich (modified in place).

        Returns:
            The packet.
        """
        if not self.should_enrich(packet):
            return packet
        statement = packet["statement"]
        subject_label, obj = claim_terms(statement, self.labels)
        papers = self.pubmed.search_and_fetch(
            build_search_query(subject_label, obj), max_results=self.max_papers
        )

        present = {item["id"] for item in iter_items(packet)}
        new = []
        for paper in papers:
            pub = paper_to_publication(paper, self.date)
            if pub["id"] not in present:
                present.add(pub["id"])
                new.append(pub)
        if new:
            statement.setdefault("has_evidence", []).extend(new)
            if not any(
                r["id"] == PUBMED
                for r in packet["reference_snapshot"].get("resources") or []
            ):
                add_registry_entry(packet, "resources", dict(PUBMED_RESOURCE))
        return packet

    def enrich_packets(self, packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich many packets in place; return them as a list."""
        return [self.enrich(packet) for packet in packets]
