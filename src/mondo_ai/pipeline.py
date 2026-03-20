"""MondoAI Pipeline Runner.

This module orchestrates the full pipeline for generating SIEVE evidence packets.

Example:
    >>> from mondo_ai.pipeline import run_pipeline
    >>> packets = run_pipeline(config)
"""

from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import yaml

from mondo_ai.datamodel import (
    ConcordanceItem,
    CurationStatus,
    EvidenceDirection,
    EvidenceItemType,
    EvidencePacket,
    EvidenceStrength,
    SieveEvidenceLine,
    SieveStatement,
    StatementPredicate,
    SynonymScope,
    predicate_to_coding,
)
from mondo_ai.extraction.owl_extractor import OWLExtractor
from mondo_ai.mapping.sssom_reader import SSSOMReader
from mondo_ai.packets.builder import PacketBuilder, PacketMerger
from mondo_ai.scoring.scorer import Scorer
from mondo_ai.statements.builder import StatementBuilder


def _generate_packet_id(counter: int) -> str:
    """Generate deterministic packet ID."""
    return f"pkt_{counter:06d}"


def _generate_evidence_line_id(counter: int, source: str) -> str:
    """Generate deterministic evidence line ID."""
    return f"el_{source}_{counter:04d}"


def _generate_evidence_item_id(counter: int, source: str) -> str:
    """Generate deterministic evidence item ID."""
    return f"ev_{source}_{counter:04d}"


def _reliability_to_strength(reliability: float) -> str:
    """Convert numeric reliability to SIEVE strength string."""
    if reliability >= 0.8:
        return EvidenceStrength.strong.value
    if reliability >= 0.5:
        return EvidenceStrength.moderate.value
    return EvidenceStrength.weak.value


class PipelineRunner:
    """Run the MondoAI evidence pipeline."""

    def __init__(
        self,
        sssom_path: str | Path,
        omim_owl_path: str | Path | None = None,
        ordo_owl_path: str | Path | None = None,
        doid_owl_path: str | Path | None = None,
        mondo_owl_path: str | Path | None = None,
    ) -> None:
        """Initialize pipeline runner.

        Args:
            sssom_path: Path to mondo.sssom.tsv.
            omim_owl_path: Path to OMIM OWL file.
            ordo_owl_path: Path to ORDO OWL file.
            doid_owl_path: Path to DOID OWL file.
            mondo_owl_path: Path to Mondo OWL file.
        """
        self.sssom_path = Path(sssom_path)
        self.omim_path = Path(omim_owl_path) if omim_owl_path else None
        self.ordo_path = Path(ordo_owl_path) if ordo_owl_path else None
        self.doid_path = Path(doid_owl_path) if doid_owl_path else None
        self.mondo_path = Path(mondo_owl_path) if mondo_owl_path else None

        self.sssom = SSSOMReader(self.sssom_path)
        self.extractor = OWLExtractor()
        self.packet_builder = PacketBuilder()
        self.scorer = Scorer()

        # Reliability weights
        self._reliability_weights = {
            "omim": 0.95,
            "ordo": 0.90,
            "doid": 0.85,
        }

    def run(
        self,
        omim_synonym_count: int = 20,
        ordo_synonym_count: int = 20,
        doid_synonym_count: int = 20,
        doid_hierarchy_count: int = 40,
    ) -> list[EvidencePacket]:
        """Run the pipeline and generate evidence packets.

        Args:
            omim_synonym_count: Number of OMIM synonyms to extract.
            ordo_synonym_count: Number of ORDO synonyms to extract.
            doid_synonym_count: Number of DOID synonyms to extract.
            doid_hierarchy_count: Number of DOID subClassOf relations.

        Returns:
            List of evidence packets.
        """
        packets: list[EvidencePacket] = []
        packet_counter = 1
        evidence_counter = 1

        # Process OMIM synonyms (filter by omim.org/entry/)
        if self.omim_path and self.omim_path.exists():
            omim_packets, packet_counter, evidence_counter = self._extract_synonyms(
                self.omim_path,
                "omim",
                "OMIM",
                omim_synonym_count,
                packet_counter,
                evidence_counter,
                iri_filter="omim.org/entry/",
            )
            packets.extend(omim_packets)

        # Process ORDO synonyms (filter by Orphanet_)
        if self.ordo_path and self.ordo_path.exists():
            ordo_packets, packet_counter, evidence_counter = self._extract_synonyms(
                self.ordo_path,
                "ordo",
                "Orphanet (ORDO)",
                ordo_synonym_count,
                packet_counter,
                evidence_counter,
                iri_filter="Orphanet_",
            )
            packets.extend(ordo_packets)

        # Process DOID synonyms (filter by DOID_)
        if self.doid_path and self.doid_path.exists():
            doid_syn_packets, packet_counter, evidence_counter = self._extract_synonyms(
                self.doid_path,
                "doid",
                "Disease Ontology",
                doid_synonym_count,
                packet_counter,
                evidence_counter,
                iri_filter="DOID_",
            )
            packets.extend(doid_syn_packets)

            # Process DOID hierarchy (filter by DOID_)
            doid_hier_packets, packet_counter, evidence_counter = self._extract_hierarchy(
                self.doid_path,
                "doid",
                "Disease Ontology",
                doid_hierarchy_count,
                packet_counter,
                evidence_counter,
                iri_filter="DOID_",
            )
            packets.extend(doid_hier_packets)

        # Score all packets
        packets = self.scorer.score_packets(packets)

        return packets

    def _extract_synonyms(
        self,
        owl_path: Path,
        source_id: str,
        source_name: str,
        count: int,
        packet_counter: int,
        evidence_counter: int,
        iri_filter: str | None = None,
    ) -> tuple[list[EvidencePacket], int, int]:
        """Extract synonym evidence packets from an OWL file."""
        packets: list[EvidencePacket] = []

        # Extract entities with optional IRI filter
        snapshot = self.extractor.extract(
            owl_path,
            source_id,
            source_name,
            limit=count * 10,
            iri_filter=iri_filter,
            skip_deprecated=True,
        )

        for entity in snapshot.entities or []:
            if not entity.synonyms:
                continue

            # Get Mondo mapping
            mondo_id = self.sssom.get_mondo_id(entity.id)
            if not mondo_id:
                continue

            mapping = self.sssom.get_mondo_mapping(entity.id)
            mondo_label = mapping.subject_label if mapping else None

            for syn in entity.synonyms:
                if syn.scope != SynonymScope.EXACT:
                    continue

                # Determine predicate
                predicate = StatementPredicate.EXACT_SYNONYM

                # Create statement ID
                stmt_content = f"{mondo_id}|{predicate.value}|{syn.value}"
                stmt_id = f"stmt_{sha256(stmt_content.encode()).hexdigest()[:12]}"

                # Create SIEVE statement
                statement = SieveStatement(
                    id=stmt_id,
                    type="SieveStatement",
                    subject=mondo_id,
                    subjectLabel=mondo_label,
                    predicate=predicate_to_coding(predicate),
                    object=syn.value,
                    objectLabel=syn.value,
                    statementText=f"{mondo_label or mondo_id} has exact synonym '{syn.value}'",
                )

                # Get reliability for this source
                reliability = self._reliability_weights.get(source_id, 0.9)

                # Create ConcordanceItem evidence
                concordance_item = ConcordanceItem(
                    id=_generate_evidence_item_id(evidence_counter, source_id),
                    type="ConcordanceItem",
                    evidenceItemType=EvidenceItemType.ConcordanceItem,
                    sourceName=source_name,
                    sourceId=source_id,
                    sourceSubject=entity.id,
                    sourceSubjectLabel=entity.label,
                    mappingJustification="skos:exactMatch",
                    description=f"Synonym from {source_name}: '{syn.value}'",
                )
                evidence_counter += 1

                # Wrap in SieveEvidenceLine
                evidence_line = SieveEvidenceLine(
                    id=_generate_evidence_line_id(packet_counter, source_id),
                    type="SieveEvidenceLine",
                    hasEvidenceItems=[concordance_item],
                    directionOfEvidenceProvided=EvidenceDirection.supports.value,
                    strengthOfEvidenceProvided=_reliability_to_strength(reliability),
                    scoreOfEvidenceProvided=reliability,
                )

                # Create packet
                packet = EvidencePacket(
                    id=_generate_packet_id(packet_counter),
                    statement=statement,
                    status=CurationStatus.UNREVIEWED,
                    hasEvidenceLines=[evidence_line],
                    created=date.today(),
                )
                packet_counter += 1

                packets.append(packet)

                if len(packets) >= count:
                    return packets, packet_counter, evidence_counter

        return packets, packet_counter, evidence_counter

    def _extract_hierarchy(
        self,
        owl_path: Path,
        source_id: str,
        source_name: str,
        count: int,
        packet_counter: int,
        evidence_counter: int,
        iri_filter: str | None = None,
    ) -> tuple[list[EvidencePacket], int, int]:
        """Extract hierarchy (subClassOf) evidence packets from an OWL file."""
        packets: list[EvidencePacket] = []

        # Extract entities with optional IRI filter
        snapshot = self.extractor.extract(
            owl_path,
            source_id,
            source_name,
            limit=count * 5,
            iri_filter=iri_filter,
            skip_deprecated=True,
        )

        # Build label lookup
        labels = {e.id: e.label or e.id for e in (snapshot.entities or [])}

        for entity in snapshot.entities or []:
            if not entity.parents:
                continue

            # Get Mondo mapping for child
            mondo_child = self.sssom.get_mondo_id(entity.id)
            if not mondo_child:
                continue

            child_mapping = self.sssom.get_mondo_mapping(entity.id)
            mondo_child_label = child_mapping.subject_label if child_mapping else None

            for parent_id in entity.parents:
                # Get Mondo mapping for parent
                mondo_parent = self.sssom.get_mondo_id(parent_id)
                if not mondo_parent:
                    continue

                parent_mapping = self.sssom.get_mondo_mapping(parent_id)
                mondo_parent_label = parent_mapping.subject_label if parent_mapping else None

                # Create statement ID
                stmt_content = f"{mondo_child}|{StatementPredicate.PARENT.value}|{mondo_parent}"
                stmt_id = f"stmt_{sha256(stmt_content.encode()).hexdigest()[:12]}"

                # Create SIEVE statement
                statement = SieveStatement(
                    id=stmt_id,
                    type="SieveStatement",
                    subject=mondo_child,
                    subjectLabel=mondo_child_label,
                    predicate=predicate_to_coding(StatementPredicate.PARENT),
                    object=mondo_parent,
                    objectLabel=mondo_parent_label or mondo_parent,
                    statementText=f"{mondo_child_label or mondo_child} is a subclass of {mondo_parent_label or mondo_parent}",
                )

                # Get reliability for this source
                reliability = self._reliability_weights.get(source_id, 0.9)

                # Create ConcordanceItem evidence
                concordance_item = ConcordanceItem(
                    id=_generate_evidence_item_id(evidence_counter, source_id),
                    type="ConcordanceItem",
                    evidenceItemType=EvidenceItemType.ConcordanceItem,
                    sourceName=source_name,
                    sourceId=source_id,
                    sourceSubject=entity.id,
                    sourceSubjectLabel=entity.label,
                    mappingJustification="skos:exactMatch",
                    description=f"SubClassOf from {source_name}: {entity.label} -> {labels.get(parent_id, parent_id)}",
                )
                evidence_counter += 1

                # Wrap in SieveEvidenceLine
                evidence_line = SieveEvidenceLine(
                    id=_generate_evidence_line_id(packet_counter, source_id),
                    type="SieveEvidenceLine",
                    hasEvidenceItems=[concordance_item],
                    directionOfEvidenceProvided=EvidenceDirection.supports.value,
                    strengthOfEvidenceProvided=_reliability_to_strength(reliability),
                    scoreOfEvidenceProvided=reliability,
                )

                # Create packet
                packet = EvidencePacket(
                    id=_generate_packet_id(packet_counter),
                    statement=statement,
                    status=CurationStatus.UNREVIEWED,
                    hasEvidenceLines=[evidence_line],
                    created=date.today(),
                )
                packet_counter += 1

                packets.append(packet)

                if len(packets) >= count:
                    return packets, packet_counter, evidence_counter

        return packets, packet_counter, evidence_counter


def export_packets_yaml(packets: list[EvidencePacket], output_dir: str | Path) -> None:
    """Export packets to YAML files.

    Args:
        packets: List of evidence packets.
        output_dir: Output directory.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for packet in packets:
        filename = f"{packet.id}.yaml"
        filepath = output_dir / filename

        # Convert to dict for YAML
        packet_dict = packet.model_dump(mode="json", exclude_none=True)

        with open(filepath, "w") as f:
            yaml.dump(packet_dict, f, default_flow_style=False, sort_keys=False)


def export_packets_summary(packets: list[EvidencePacket], output_path: str | Path) -> None:
    """Export packets summary to a single YAML file.

    Args:
        packets: List of evidence packets.
        output_path: Output file path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Create summary
    summary = {
        "total_packets": len(packets),
        "by_status": {},
        "by_predicate": {},
        "score_distribution": {
            "high (>=0.7)": 0,
            "medium (0.3-0.7)": 0,
            "low (<0.3)": 0,
        },
        "packets": [],
    }

    for packet in packets:
        # Count by status
        status_val = packet.status
        if hasattr(status_val, "value"):
            status_str = status_val.value
        else:
            status_str = str(status_val) if status_val else "UNREVIEWED"
        summary["by_status"][status_str] = summary["by_status"].get(status_str, 0) + 1

        # Count by predicate
        pred = packet.statement.predicate
        if pred:
            if hasattr(pred, "label") and pred.label:
                pred_str = pred.label
            elif hasattr(pred, "code") and pred.code:
                pred_str = pred.code
            else:
                pred_str = str(pred)
        else:
            pred_str = "unknown"
        summary["by_predicate"][pred_str] = summary["by_predicate"].get(pred_str, 0) + 1

        # Get score from synthesis if available
        score = 0
        if packet.evidence_synthesis and packet.evidence_synthesis.score:
            score = packet.evidence_synthesis.score.value or 0

        if score >= 0.7:
            summary["score_distribution"]["high (>=0.7)"] += 1
        elif score >= 0.3:
            summary["score_distribution"]["medium (0.3-0.7)"] += 1
        else:
            summary["score_distribution"]["low (<0.3)"] += 1

        # Add packet summary
        packet_summary = {
            "id": packet.id,
            "status": status_str,
            "score": round(score, 3) if score else None,
            "predicate": pred_str,
            "mondo_id": packet.statement.subject,
            "mondo_label": packet.statement.subjectLabel,
            "object": packet.statement.object,
            "evidence_line_count": len(packet.hasEvidenceLines) if packet.hasEvidenceLines else 0,
        }
        summary["packets"].append(packet_summary)

    with open(output_path, "w") as f:
        yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
