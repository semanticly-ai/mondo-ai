"""Canonical statement builder.

This module translates source ontology statements into Mondo-space
SIEVE statements using SSSOM mappings.

Example:
    >>> from mondo_ai.statements.builder import StatementBuilder
    >>> builder = StatementBuilder(sssom_reader)  # doctest: +SKIP
    >>> statements = builder.build_synonym_statements(entity_snapshot, "doid")  # doctest: +SKIP
"""

from dataclasses import dataclass
from hashlib import sha256

from mondo_ai.datamodel import (
    EntitySnapshot,
    SieveStatement,
    StatementPredicate,
    Synonym,
    SynonymScope,
    predicate_to_coding,
)
from mondo_ai.mapping.sssom_reader import SSSOMReader


@dataclass
class StatementWithMapping:
    """A statement paired with its source mapping metadata."""

    statement: "SieveStatement"
    source_entity_id: str
    source_entity_label: str
    mapping_set_id: str | None
    mapping_justification: str | None
    mapping_predicate: str | None


def _compute_statement_id(subject: str, predicate: str, obj: str) -> str:
    """Compute deterministic statement ID from triple.

    >>> _compute_statement_id("MONDO:0000001", "EXACT_SYNONYM", "disease")
    'stmt_750ae5c987ae'
    """
    content = f"{subject}|{predicate}|{obj}"
    hash_val = sha256(content.encode()).hexdigest()[:12]
    return f"stmt_{hash_val}"


def _scope_to_predicate(scope: SynonymScope | None) -> StatementPredicate:
    """Convert synonym scope to statement predicate."""
    if scope == SynonymScope.EXACT:
        return StatementPredicate.EXACT_SYNONYM
    if scope == SynonymScope.NARROW:
        return StatementPredicate.NARROW_SYNONYM
    if scope == SynonymScope.BROAD:
        return StatementPredicate.BROAD_SYNONYM
    if scope == SynonymScope.RELATED:
        return StatementPredicate.RELATED_SYNONYM
    return StatementPredicate.RELATED_SYNONYM


class StatementBuilder:
    """Build SIEVE statements in Mondo space."""

    def __init__(self, sssom_reader: SSSOMReader) -> None:
        """Initialize with SSSOM reader for ID translation.

        Args:
            sssom_reader: SSSOM reader with Mondo mappings.
        """
        self.sssom = sssom_reader

    def build_synonym_statement(
        self,
        source_entity_id: str,
        source_entity_label: str,
        synonym: Synonym,
    ) -> SieveStatement | None:
        """Build a synonym statement in Mondo space.

        Args:
            source_entity_id: Source entity CURIE (e.g., "DOID:4").
            source_entity_label: Label of the source entity.
            synonym: Synonym object.

        Returns:
            SieveStatement if mapping exists, None otherwise.
        """
        mondo_id = self.sssom.get_mondo_id(source_entity_id)
        if not mondo_id:
            return None

        mapping = self.sssom.get_mondo_mapping(source_entity_id)
        mondo_label = mapping.subject_label if mapping else None

        predicate = _scope_to_predicate(synonym.scope)
        statement_id = _compute_statement_id(
            mondo_id, predicate.value, synonym.value
        )

        return SieveStatement(
            id=statement_id,
            type="SieveStatement",
            subject=mondo_id,
            subject_label=mondo_label,
            predicate=predicate_to_coding(predicate),
            object=synonym.value,
            object_label=synonym.value,
            statement_text=f"{mondo_label or mondo_id} has {predicate.value.lower().replace('_', ' ')} '{synonym.value}'",
        )

    def build_parent_statement(
        self,
        source_child_id: str,
        source_child_label: str,
        source_parent_id: str,
        source_parent_label: str,
    ) -> SieveStatement | None:
        """Build a subClassOf statement in Mondo space.

        Args:
            source_child_id: Source child entity CURIE.
            source_child_label: Label of the child entity.
            source_parent_id: Source parent entity CURIE.
            source_parent_label: Label of the parent entity.

        Returns:
            SieveStatement if both mappings exist, None otherwise.
        """
        mondo_child = self.sssom.get_mondo_id(source_child_id)
        mondo_parent = self.sssom.get_mondo_id(source_parent_id)

        if not mondo_child or not mondo_parent:
            return None

        child_mapping = self.sssom.get_mondo_mapping(source_child_id)
        parent_mapping = self.sssom.get_mondo_mapping(source_parent_id)

        statement_id = _compute_statement_id(
            mondo_child, StatementPredicate.PARENT.value, mondo_parent
        )

        child_label = child_mapping.subject_label if child_mapping else None
        parent_label = parent_mapping.subject_label if parent_mapping else mondo_parent

        return SieveStatement(
            id=statement_id,
            type="SieveStatement",
            subject=mondo_child,
            subject_label=child_label,
            predicate=predicate_to_coding(StatementPredicate.PARENT),
            object=mondo_parent,
            object_label=parent_label,
            statement_text=f"{child_label or mondo_child} is a subclass of {parent_label}",
        )

    def build_statements_from_snapshot(
        self,
        snapshot: EntitySnapshot,
        include_synonyms: bool = True,
        include_hierarchy: bool = True,
    ) -> list[SieveStatement]:
        """Build all statements from an entity snapshot.

        Args:
            snapshot: Entity snapshot from OWL extraction.
            include_synonyms: Include synonym statements.
            include_hierarchy: Include subClassOf statements.

        Returns:
            List of SIEVE statements.
        """
        statements: list[SieveStatement] = []

        if include_synonyms and snapshot.synonyms:
            for syn in snapshot.synonyms:
                stmt = self.build_synonym_statement(
                    snapshot.id,
                    snapshot.label or "",
                    syn,
                )
                if stmt:
                    statements.append(stmt)

        if include_hierarchy and snapshot.parents:
            for parent_id in snapshot.parents:
                stmt = self.build_parent_statement(
                    snapshot.id,
                    snapshot.label or "",
                    parent_id,
                    parent_id,  # We don't have parent labels in snapshot
                )
                if stmt:
                    statements.append(stmt)

        return statements

    def build_statements_with_mapping(
        self,
        snapshot: EntitySnapshot,
        include_synonyms: bool = True,
        include_hierarchy: bool = True,
    ) -> list[StatementWithMapping]:
        """Build statements with their SSSOM mapping metadata.

        This method returns statements paired with the mapping_set_id
        from the SSSOM file, which identifies the provenance of the mapping.

        Args:
            snapshot: Entity snapshot from OWL extraction.
            include_synonyms: Include synonym statements.
            include_hierarchy: Include subClassOf statements.

        Returns:
            List of StatementWithMapping objects containing statements
            and their SSSOM mapping metadata.
        """
        results: list[StatementWithMapping] = []

        # Get the mapping for this entity once
        mapping = self.sssom.get_mondo_mapping(snapshot.id)
        if not mapping:
            return results

        # Get file-level mapping_set_id and per-mapping metadata
        mapping_set_id = self.sssom.mapping_set_id
        mapping_justification = mapping.mapping_justification
        mapping_predicate = mapping.predicate_id

        if include_synonyms and snapshot.synonyms:
            for syn in snapshot.synonyms:
                stmt = self.build_synonym_statement(
                    snapshot.id,
                    snapshot.label or "",
                    syn,
                )
                if stmt:
                    results.append(StatementWithMapping(
                        statement=stmt,
                        source_entity_id=snapshot.id,
                        source_entity_label=snapshot.label or "",
                        mapping_set_id=mapping_set_id,
                        mapping_justification=mapping_justification,
                        mapping_predicate=mapping_predicate,
                    ))

        if include_hierarchy and snapshot.parents:
            for parent_id in snapshot.parents:
                stmt = self.build_parent_statement(
                    snapshot.id,
                    snapshot.label or "",
                    parent_id,
                    parent_id,
                )
                if stmt:
                    results.append(StatementWithMapping(
                        statement=stmt,
                        source_entity_id=snapshot.id,
                        source_entity_label=snapshot.label or "",
                        mapping_set_id=mapping_set_id,
                        mapping_justification=mapping_justification,
                        mapping_predicate=mapping_predicate,
                    ))

        return results
