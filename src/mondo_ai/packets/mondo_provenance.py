"""Attach Mondo's curator and publication provenance to the packets of claims in Mondo.

Mondo's own assertion is not evidence for itself, but the axiom annotations behind it
say who curated it (ORCIDs) and what it cites (PMIDs). For a packet whose claim is
already in Mondo (``review_state: NOT_VERIFIED``):

- each ORCID becomes a ``SieveContribution`` with role ``CURATOR`` on the statement, and
  a person ``SieveAgent`` in the packet's reference snapshot. It is not an
  ``AssertionDecision``: that needs a date and an outcome the annotation does not give.
- each PMID becomes a ``Publication`` in the statement's ``has_evidence`` (evidence with
  no interpretation), with ``infores:pubmed`` in the snapshot, for the claim types the
  claim-evidence matrix allows publications on (synonyms and subclass axioms, not
  mappings).

Neither carries a strength, so the packet's score does not change.

Example:
    >>> from mondo_ai.extraction.mondo_extractor import extract_mondo_provenance  # doctest: +SKIP
    >>> provenance = extract_mondo_provenance("mondo.owl")  # doctest: +SKIP
    >>> packets = [apply_mondo_provenance(p, provenance) for p in packets]  # doctest: +SKIP
"""

from functools import lru_cache
from typing import Any, Mapping

from linkml_runtime.utils.schemaview import SchemaView
from sieve.profile_checks import permitted_evidence

from mondo_ai.extraction.mondo_extractor import AxiomKey, AxiomProvenance
from mondo_ai.packets.evidence import publication
from mondo_ai.packets.registry import PUBMED, SCHEMA_PATH

PUBMED_RESOURCE: dict[str, Any] = {
    "id": PUBMED,
    "type": "InformationResource",
    "label": "PubMed",
    "curie_prefixes": ["PMID"],
    "default_role": "PRIMARY",
}


@lru_cache(maxsize=None)
def _publications_permitted(claim_type: str) -> bool:
    """Whether the claim-evidence matrix allows a Publication as evidence for a claim type.

    >>> _publications_permitted("SynonymStatement"), _publications_permitted("MappingStatement")
    (True, False)
    """
    return "Publication" in permitted_evidence(SchemaView(str(SCHEMA_PATH)), claim_type)


def provenance_key(statement: dict[str, Any]) -> AxiomKey:
    """The key of a statement's axiom in the Mondo provenance map; synonym text is casefolded.

    >>> provenance_key({"type": "SynonymStatement", "subject": "MONDO:1", "predicate": {"code": "oboInOwl:hasExactSynonym"}, "object": "CF"})
    ('MONDO:1', 'oboInOwl:hasExactSynonym', 'cf')
    """
    obj = statement["object"]
    if statement["type"] == "SynonymStatement":
        obj = obj.casefold()
    return statement["subject"], statement["predicate"]["code"], obj


def _append_new(items: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items followed by those of ``new`` whose id is not already there."""
    seen = {i["id"] for i in items}
    return items + [n for n in new if n["id"] not in seen]


def apply_mondo_provenance(packet: dict[str, Any], provenance: Mapping[AxiomKey, AxiomProvenance]) -> dict[str, Any]:
    """The packet with the curators and publications Mondo records on its claim's axiom.

    Args:
        packet: A profile packet (dict).
        provenance: The Mondo provenance map
            (:func:`mondo_ai.extraction.mondo_extractor.extract_mondo_provenance`).

    Returns:
        The same packet object if its claim is not in Mondo or has no provenance;
        otherwise a copy with contributions, publications and the snapshot entries they
        reference added (no duplicates, so applying twice changes nothing). The input is
        not modified.
    """
    statement = packet["statement"]
    if statement.get("review_state") != "NOT_VERIFIED":
        return packet
    prov = provenance.get(provenance_key(statement))
    if prov is None:
        return packet
    pmids = prov.pmids if _publications_permitted(statement["type"]) else ()
    if not (prov.orcids or pmids):
        return packet

    statement = dict(statement)
    snapshot = dict(packet["reference_snapshot"])
    if prov.orcids:
        contributions = [
            {
                "id": f"{statement['id']}#curator-{orcid}",
                "type": "SieveContribution",
                "contributor": f"orcid:{orcid}",
                "role": "CURATOR",
            }
            for orcid in prov.orcids
        ]
        agents = [{"id": f"orcid:{orcid}", "type": "SieveAgent", "subtype": "person"} for orcid in prov.orcids]
        statement["contributions"] = _append_new(statement.get("contributions") or [], contributions)
        snapshot["agents"] = _append_new(snapshot.get("agents") or [], agents)
    if pmids:
        statement["has_evidence"] = _append_new(statement.get("has_evidence") or [], [publication(p) for p in pmids])
        snapshot["resources"] = _append_new(snapshot.get("resources") or [], [PUBMED_RESOURCE])
    return {**packet, "statement": statement, "reference_snapshot": snapshot}
