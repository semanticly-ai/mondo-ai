"""Build SIEVE profile evidence packets from source snapshots.

A packet is one claim in Mondo space with the evidence for and against it. The evidence
is what the sources themselves say, in their own ids: a DOID synonym, an Orphanet
classification parent, an OMIM xref. Each such statement becomes a prior claim, nested
in an evidence line, reported in the ExternalRecord it was read from and normalized into
Mondo through the skos:exactMatch rows of mondo.sssom.tsv.

Three modalities are built:

- ``synonyms``: SynonymStatement, from each source's synonyms (scope = predicate).
- ``subclass``: SubclassStatement, from each source's named parents (and ORDO's
  part_of classification parents) where both ends normalize to Mondo.
- ``mappings``: MappingStatement, for every Mondo mapping into a configured source
  (from mondo.sssom.tsv) and for candidate mappings a source's own xrefs imply. The
  evidence is the object record (obsolete: disputes), a lexical match score, and every
  source's xref or skos match that agrees (supports) or gives another precision
  (disputes).

Two views come out of one build: raw packets, one per claim and source, and merged
packets, one per claim with every source's lines. Packets are plain dicts in the shape of
the profile; the statement's score is sieve's inclusion score S.

Example:
    >>> builder = PacketBuilder(sources, mondo_snapshot, sssom, config, "2026-11-20")  # doctest: +SKIP
    >>> raw, merged = builder.build(["synonyms", "subclass", "mappings"])  # doctest: +SKIP
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from mondo_ai import __version__
from mondo_ai.config import PipelineConfig, ResourceConfig
from mondo_ai.datamodel import EntitySnapshot, ResourceSnapshot, SynonymScope
from mondo_ai.mapping.sssom_reader import SSSOMReader
from mondo_ai.packets.evidence import (
    PREDICATE_LABELS,
    Normalization,
    Release,
    coding,
    computational_score,
    digest,
    evidence_line,
    external_record,
    prior_claim,
    publication,
    resolution_chain,
)
from mondo_ai.scoring.scorer import score_packet
from mondo_ai.packets.registry import (
    build_registry,
    reference_snapshot,
    registry_index,
    release_id,
    release_version,
)

MODALITIES = ("synonyms", "subclass", "mappings")
MONDO_SSSOM = "http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv"

SCOPE_PREDICATES = {
    SynonymScope.EXACT: "oboInOwl:hasExactSynonym",
    SynonymScope.RELATED: "oboInOwl:hasRelatedSynonym",
    SynonymScope.NARROW: "oboInOwl:hasNarrowSynonym",
    SynonymScope.BROAD: "oboInOwl:hasBroadSynonym",
}

MAPPING_PREDICATES = ("skos:exactMatch", "skos:broadMatch", "skos:narrowMatch", "skos:relatedMatch")
PRECISE = {"skos:exactMatch", "skos:broadMatch", "skos:narrowMatch"}
INVERSE = {
    "skos:exactMatch": "skos:exactMatch",
    "skos:broadMatch": "skos:narrowMatch",
    "skos:narrowMatch": "skos:broadMatch",
    "skos:relatedMatch": "skos:relatedMatch",
}
# ORDO's ECC precision codes. NTBT: the ORPHA concept is narrower than the target, so
# Orphanet:x skos:broadMatch target; BTNT the reverse. Other codes (ND, W, -) are skipped.
ECC_PREDICATES = {"E": "skos:exactMatch", "NTBT": "skos:broadMatch", "BTNT": "skos:narrowMatch"}

# Sources whose MONDO xrefs are generated from Mondo itself (the monarch OMIM build reads
# them from mondo-ingest), so they are not independent evidence for a Mondo mapping.
MONDO_DERIVED_XREFS = {"omim"}

OBSOLETE_RECORD_STRENGTH = 0.9
LEXICAL_STRENGTH = {1.0: 0.6, 0.8: 0.4}
LEXICAL_CLUSTER = "mondoai:lexical"


def packet_id(claim_type: str, subject: str, predicate: str, obj: str) -> str:
    """Deterministic packet id for a claim; synonym text is compared case-insensitively.

    >>> packet_id("SynonymStatement", "MONDO:1", "oboInOwl:hasExactSynonym", "CF")
    'mondoai:pkt/9ec61863ea85'
    """
    key = obj.casefold() if claim_type == "SynonymStatement" else obj
    return f"mondoai:pkt/{digest(claim_type, subject, predicate, key)}"


def normalize_name(name: str) -> str:
    """Lower-case a name and drop punctuation, for lexical matching.

    >>> normalize_name("Cystic fibrosis, type 1")
    'cystic fibrosis type 1'
    """
    return " ".join(re.sub(r"[^\w]+", " ", name.casefold()).split())


@dataclass
class Source:
    """A configured source and its snapshot."""

    config: ResourceConfig
    snapshot: ResourceSnapshot
    entities: dict[str, EntitySnapshot] = field(init=False)

    def __post_init__(self) -> None:
        self.entities = {e.id: e for e in self.snapshot.entities or []}

    @property
    def id(self) -> str:
        return self.config.id


@dataclass
class Claim:
    """A claim being assembled, with its lines per source."""

    claim_type: str
    subject: str
    predicate: str
    obj: str
    review_state: str
    text: str
    extra: dict[str, Any] = field(default_factory=dict)
    lines: dict[str, dict[str, dict[str, Any]]] = field(default_factory=lambda: defaultdict(dict))


class PacketBuilder:
    """Build raw and merged evidence packets from source snapshots."""

    def __init__(
        self,
        sources: list[Source],
        mondo: ResourceSnapshot,
        sssom: SSSOMReader,
        config: PipelineConfig,
        date: str,
        mapping_set_version: str | None = None,
    ) -> None:
        """Set up the run.

        Args:
            sources: The sources to read evidence from, in config order.
            mondo: Mondo snapshot (labels, synonyms, parents, obsoletion).
            sssom: mondo.sssom.tsv, for normalizing source ids and for Mondo's own mappings.
            config: Pipeline configuration (weights, rule parameters, resources).
            date: Run date (ISO); packet version and retrieval date.
            mapping_set_version: Version of mondo.sssom.tsv; defaults to the Mondo release.
        """
        self.sources = sources
        self.sssom = sssom
        self.date = date
        self.mondo = {e.id: e for e in mondo.entities or [] if not e.deprecated}
        self.registry = build_registry(
            config, {s.id: release_version(s.snapshot.version or "unknown") for s in sources}, date
        )
        self.index = registry_index(self.registry)
        self.rules = {r["claim_type"]: r for r in self.registry["aggregation_rules"]}
        self.releases = {
            s.id: Release(
                source_id=s.id,
                resource=s.config.infores,
                release=release_id(s.id, release_version(s.snapshot.version or "unknown")),
                version=s.snapshot.version or "unknown",
                retrieval_date=date,
            )
            for s in sources
        }
        self.mapping_set = sssom.mapping_set_id or MONDO_SSSOM
        self.mapping_set_version = mapping_set_version or release_version(mondo.version or "unknown")
        self.by_prefix = {p: s for s in sources for p in s.config.curie_prefixes}
        derives = {r["id"]: set(r.get("derives_content_from") or []) for r in self.registry["resources"]}
        self.derives = derives
        self.claims: dict[str, Claim] = {}
        self._ancestors: dict[str, set[str]] = {}
        self._mondo_synonyms = {
            e.id: {(SCOPE_PREDICATES[s.scope or SynonymScope.RELATED], s.value.casefold()): s.value for s in e.synonyms or []}
            for e in self.mondo.values()
        }

    # ---- helpers ------------------------------------------------------------------

    def _normalize(self, curie: str) -> Normalization | None:
        m = self.sssom.get_exact_mondo_mapping(curie)
        if m is None or m.subject_id not in self.mondo:
            return None
        return Normalization(curie, m.subject_id, m.mapping_justification or None, self.mapping_set, self.mapping_set_version)

    def _chain(self, slot: str, g: Normalization) -> dict[str, Any]:
        return resolution_chain(slot, g, self.date)

    def _label(self, mondo_id: str) -> str:
        entity = self.mondo.get(mondo_id)
        return (entity.label if entity else None) or mondo_id

    def _ancestors_of(self, mondo_id: str) -> set[str]:
        if mondo_id not in self._ancestors:
            self._ancestors[mondo_id] = set()
            out: set[str] = set()
            for parent in self.mondo[mondo_id].parents or [] if mondo_id in self.mondo else []:
                if parent.startswith("MONDO:"):
                    out |= {parent} | self._ancestors_of(parent)
            self._ancestors[mondo_id] = out
        return self._ancestors[mondo_id]

    def _record(self, source: Source, entity: EntitySnapshot) -> dict[str, Any]:
        return external_record(self.releases[source.id], entity.id, entity.label, bool(entity.deprecated))

    def _claim(self, claim_type: str, subject: str, predicate: str, obj: str, review_state: str, text: str) -> Claim:
        pid = packet_id(claim_type, subject, predicate, obj)
        if pid not in self.claims:
            self.claims[pid] = Claim(claim_type, subject, predicate, obj, review_state, text)
        return self.claims[pid]

    def _add(
        self,
        claim: Claim,
        source: Source,
        suffix: str,
        items: list[dict[str, Any]],
        direction: str,
        score: float,
        cluster: str,
        description: str,
    ) -> None:
        """Add items to the claim's line for (source, suffix), creating the line if new."""
        pid = packet_id(claim.claim_type, claim.subject, claim.predicate, claim.obj)
        line_id = f"mondoai:el/{pid.rsplit('/', 1)[-1]}-{suffix}"
        lines = claim.lines[source.id]
        if line_id in lines:
            known = {i["id"] for i in lines[line_id]["has_evidence_items"]}
            lines[line_id]["has_evidence_items"] += [i for i in items if i["id"] not in known]
            return
        lines[line_id] = evidence_line(
            line_id,
            items,
            direction,
            score,
            self.rules[claim.claim_type],
            cluster,
            self.date,
            description,
            self.releases[source.id].release,
        )

    # ---- synonyms -----------------------------------------------------------------

    def add_synonyms(self) -> None:
        """Synonym claims from every source's synonyms on live entities that normalize to Mondo."""
        for source in self.sources:
            rel = self.releases[source.id]
            for entity in source.entities.values():
                if entity.deprecated or not entity.synonyms:
                    continue
                g = self._normalize(entity.id)
                if g is None:
                    continue
                mondo_label = self._label(g.mondo_id)
                record = self._record(source, entity)
                for syn in entity.synonyms:
                    if syn.value.casefold() == mondo_label.casefold():
                        continue
                    predicate = SCOPE_PREDICATES[syn.scope or SynonymScope.RELATED]
                    in_mondo = self._mondo_synonyms[g.mondo_id].get((predicate, syn.value.casefold()))
                    obj = in_mondo or syn.value
                    claim = self._claim(
                        "SynonymStatement",
                        g.mondo_id,
                        predicate,
                        obj,
                        "NOT_VERIFIED" if in_mondo else "PENDING_REVIEW",
                        f"{mondo_label} {PREDICATE_LABELS[predicate]} '{obj}'",
                    )
                    context = [{"code": syn.type, "label": "synonym type"}] if syn.type else None
                    item = prior_claim(
                        "SynonymStatement",
                        rel,
                        entity.id,
                        predicate,
                        syn.value,
                        [record, *(publication(p) for p in syn.pmids or [])],
                        [self._chain("subject", g)],
                        context,
                    )
                    self._add(
                        claim,
                        source,
                        source.id,
                        [item],
                        "supports",
                        source.config.get_weight(predicate),
                        rel.resource,
                        f"{source.config.name} carries the synonym on a record Mondo maps exactly to this term.",
                    )

    # ---- subclass -----------------------------------------------------------------

    def add_subclass(self) -> None:
        """Subclass claims from every source's parents where both ends normalize to Mondo."""
        for source in self.sources:
            rel = self.releases[source.id]
            for entity in source.entities.values():
                if entity.deprecated:
                    continue
                parents = [(p, False) for p in entity.parents or []]
                parents += [(p, True) for p in entity.classification_parents or []]
                if not parents:
                    continue
                g = self._normalize(entity.id)
                if g is None:
                    continue
                record = self._record(source, entity)
                for parent, classification in parents:
                    gp = self._normalize(parent)
                    if gp is None or gp.mondo_id == g.mondo_id:
                        continue
                    in_mondo = gp.mondo_id in self._ancestors_of(g.mondo_id)
                    claim = self._claim(
                        "SubclassStatement",
                        g.mondo_id,
                        "rdfs:subClassOf",
                        gp.mondo_id,
                        "NOT_VERIFIED" if in_mondo else "PENDING_REVIEW",
                        f"{self._label(g.mondo_id)} subclass of {self._label(gp.mondo_id)}",
                    )
                    context = (
                        [{"code": "BFO:0000050", "label": "part_of classification parent"}] if classification else None
                    )
                    item = prior_claim(
                        "SubclassStatement",
                        rel,
                        entity.id,
                        "rdfs:subClassOf",
                        parent,
                        [record],
                        [self._chain("subject", g), self._chain("object", gp)],
                        context,
                    )
                    self._add(
                        claim,
                        source,
                        source.id,
                        [item],
                        "supports",
                        source.config.get_weight("rdfs:subClassOf"),
                        rel.resource,
                        f"{source.config.name} places the record under a parent; both normalize to Mondo.",
                    )

    # ---- mappings -----------------------------------------------------------------

    def add_mappings(self) -> None:
        """Mapping claims: Mondo's own mappings into the sources, and candidates from source xrefs."""
        mondo_pairs: dict[tuple[str, str], str] = {}
        exact_targets: dict[str, set[str]] = defaultdict(set)
        for m in self.sssom.iter_mappings():
            if m.subject_id not in self.mondo or m.predicate_id not in MAPPING_PREDICATES:
                continue
            target = self.by_prefix.get(m.object_id.split(":", 1)[0])
            if target is None:
                continue
            if m.predicate_id == "skos:exactMatch":
                exact_targets[m.object_id].add(m.subject_id)
            claim = self._mapping_claim(m.subject_id, m.predicate_id, m.object_id, "NOT_VERIFIED")
            mondo_pairs[(m.subject_id, m.object_id)] = m.predicate_id
            self._object_evidence(claim, target)

        for source in self.sources:
            uses_ecc = any(m.mapping_source for e in source.entities.values() for m in e.mappings or [])
            rel = self.releases[source.id]
            for entity in source.entities.values():
                if entity.deprecated or not entity.mappings:
                    continue
                g: Normalization | None = None
                normalized = False
                for xref in entity.mappings:
                    asserted = self._asserted_predicate(xref.predicate_id, xref.mapping_source, uses_ecc)
                    if asserted is None:
                        continue
                    target_id = xref.object_id
                    if target_id.startswith("MONDO:"):
                        if source.id in MONDO_DERIVED_XREFS or target_id not in self.mondo:
                            continue
                        # The source maps its own record to Mondo: the reverse of a Mondo mapping.
                        subject, predicate, obj, resolution_chains = target_id, INVERSE[asserted], entity.id, []
                    else:
                        target = self.by_prefix.get(target_id.split(":", 1)[0])
                        if target is None or target.id == source.id:
                            continue
                        if not normalized:
                            g, normalized = self._normalize(entity.id), True
                        if g is None:
                            continue
                        subject, predicate, obj = g.mondo_id, asserted, target_id
                        resolution_chains = [self._chain("subject", g)]
                    claimed = mondo_pairs.get((subject, obj))
                    if claimed is None:
                        if exact_targets.get(obj, set()) - {subject} or predicate == "skos:relatedMatch":
                            continue  # Mondo maps the target exactly elsewhere; not a candidate
                        claim = self._mapping_claim(subject, predicate, obj, "PENDING_REVIEW")
                        mondo_pairs[(subject, obj)] = predicate
                        target_source = self.by_prefix.get(obj.split(":", 1)[0])
                        if target_source is not None:
                            self._object_evidence(claim, target_source)
                        claimed = predicate
                    else:
                        claim = self.claims[packet_id("MappingStatement", subject, claimed, obj)]
                    if claimed == predicate:
                        direction = "supports"
                    elif claimed in PRECISE and predicate in PRECISE:
                        direction = "disputes"
                    else:
                        continue
                    item = prior_claim(
                        "MappingStatement",
                        rel,
                        entity.id,
                        asserted,
                        target_id,
                        [self._record(source, entity)],
                        resolution_chains,
                        self._mapping_context(xref.predicate_id, xref.mapping_source),
                    )
                    self._add(
                        claim,
                        source,
                        source.id,
                        [item],
                        direction,
                        source.config.content_weights.mapping,
                        rel.resource,
                        f"{source.config.name} maps its record to the target"
                        + (" with the same precision." if direction == "supports" else " with another precision."),
                    )

    @staticmethod
    def _asserted_predicate(predicate_id: str | None, ecc: str | None, uses_ecc: bool) -> str | None:
        """The SKOS predicate a source's xref or match asserts, or None to skip it."""
        if predicate_id in MAPPING_PREDICATES:
            return predicate_id
        if predicate_id == "oboInOwl:hasDbXref":
            if ecc:
                return ECC_PREDICATES.get(ecc)
            return None if uses_ecc else "skos:exactMatch"
        return None

    @staticmethod
    def _mapping_context(predicate_id: str | None, ecc: str | None) -> list[dict[str, str]] | None:
        if ecc:
            return [{"code": f"ECC:{ecc}", "label": "ORDO mapping precision"}]
        if predicate_id == "oboInOwl:hasDbXref":
            return [{"code": "oboInOwl:hasDbXref", "label": "untyped cross-reference"}]
        return None

    def _mapping_claim(self, subject: str, predicate: str, obj: str, review_state: str) -> Claim:
        target = self.by_prefix.get(obj.split(":", 1)[0])
        entity = target.entities.get(obj) if target else None
        obj_label = f" ({entity.label})" if entity and entity.label else ""
        return self._claim(
            "MappingStatement",
            subject,
            predicate,
            obj,
            review_state,
            f"{self._label(subject)} {PREDICATE_LABELS[predicate]} {obj}{obj_label}",
        )

    def _object_evidence(self, claim: Claim, target: Source) -> None:
        """Evidence from the object record: obsolete (disputes) or a lexical match (supports)."""
        entity = target.entities.get(claim.obj)
        if entity is None:
            return
        record = self._record(target, entity)
        if entity.deprecated:
            claim.extra["object_obsolete_in_source_version"] = target.snapshot.version
            self._add(
                claim,
                target,
                "obsolete",
                [record],
                "disputes",
                OBSOLETE_RECORD_STRENGTH,
                self.releases[target.id].resource,
                f"The object is obsolete in {target.config.name} {self.releases[target.id].version}.",
            )
            return
        match, kind = self._lexical(claim.subject, entity)
        if match is None:
            return
        score = computational_score(
            match,
            f"subject={claim.subject};object={claim.obj};match={kind};normalize=casefold,punctuation",
            __version__,
        )
        self._add(
            claim,
            target,
            "lexical",
            [record, score],
            "supports",
            LEXICAL_STRENGTH[match],
            LEXICAL_CLUSTER,
            f"The Mondo term and the {target.config.name} record share a name ({kind}).",
        )

    def _lexical(self, mondo_id: str, entity: EntitySnapshot) -> tuple[float | None, str]:
        mondo = self.mondo[mondo_id]
        m_label = normalize_name(mondo.label or "")
        s_label = normalize_name(entity.label or "")
        if m_label and m_label == s_label:
            return 1.0, "label_label"
        m_names = {m_label} | {normalize_name(s.value) for s in mondo.synonyms or [] if s.scope == SynonymScope.EXACT}
        s_names = {s_label} | {normalize_name(s.value) for s in entity.synonyms or [] if s.scope == SynonymScope.EXACT}
        if (m_names & s_names) - {""}:
            return 0.8, "exact_synonym"
        return None, ""

    # ---- assembly -----------------------------------------------------------------

    def build(self, modalities: Iterable[str] = MODALITIES) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the raw and merged packets for the requested modalities.

        Returns:
            (raw packets, merged packets), each sorted by packet id.
        """
        modalities = set(modalities)
        if unknown := modalities - set(MODALITIES):
            raise ValueError(f"unknown modalities {sorted(unknown)}; choose from {MODALITIES}")
        if "synonyms" in modalities:
            self.add_synonyms()
        if "subclass" in modalities:
            self.add_subclass()
        if "mappings" in modalities:
            self.add_mappings()

        raw: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        for pid in sorted(self.claims):
            claim = self.claims[pid]
            if not claim.lines:
                continue
            for source_id in sorted(claim.lines):
                raw.append(self._packet(claim, f"{pid}-{source_id}", list(claim.lines[source_id].values())))
            merged.append(self._packet(claim, pid, [li for src in claim.lines.values() for li in src.values()]))
        return raw, merged

    def _packet(self, claim: Claim, pid: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        statement: dict[str, Any] = {
            "id": f"mondoai:st/{packet_id(claim.claim_type, claim.subject, claim.predicate, claim.obj).rsplit('/', 1)[-1]}",
            "type": claim.claim_type,
            "subject": claim.subject,
            "predicate": coding(claim.predicate),
            "object": claim.obj,
            "statement_text": claim.text,
            "review_state": claim.review_state,
            "proposing_channel": "SOURCE_INGEST",
            **claim.extra,
            "has_evidence_lines": self._clustered(lines),
        }
        packet: dict[str, Any] = {"id": pid, "type": "EvidencePacket", "version": self.date, "statement": statement}
        packet["reference_snapshot"] = reference_snapshot(packet, self.index, self.date)
        return score_packet(packet)

    def _clustered(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy the lines, putting a line from a resource that derives content from another
        resource with a line on the same claim into that resource's cluster (D-046)."""
        clusters = {li["independence_cluster"] for li in lines}
        out = []
        for li in lines:
            upstream = sorted(self.derives.get(li["independence_cluster"], set()) & clusters)
            out.append(dict(li, independence_cluster=upstream[0]) if upstream else li)
        return out
