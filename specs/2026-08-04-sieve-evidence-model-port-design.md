# Port sieve's current evidence model into mondo-ai

*Date: 2026-08-04*

Status: implemented (2026-08-04).

## Problem

mondo-ai vendors an **old copy** of the sieve evidence model in
`src/mondo_ai/schema/sieve_sepio.yaml` (+ a camelCase `sepio_classes.yaml`),
generates `datamodel/sieve_models.py` from it, and uses **camelCase** field
names throughout (`hasEvidenceLines`, `directionOfEvidenceProvided`,
`evidenceItemType`, …) across ~18 modules.

sieve has since **changed massively** and now owns the canonical model. Its
current `schema/sieve.yaml` + `schema/sepio_classes.yaml` are **snake_case**, add
several curation-workflow features, and differ structurally from mondo-ai's copy.
mondo-ai must adopt sieve's *current* full curation model.

sieve is itself **mid-migration**: a new lean "minimal microschema"
(`schema/minimal.yaml`, the "A/C" evidence kernel with UPPERCASE
`Direction`/`Strength`/`EvidenceSource` enums and real SEPIO/ECO URIs) exists
standalone but nothing in sieve imports or uses it yet; `sieve.yaml` has **not**
been re-based onto it, and the kernel does not yet contain the "C" shape
(`TextMiningResult`).

## Decisions (locked in brainstorming, 2026-08-04)

- **Adopt the full curation model, exactly as-is.** Vendor sieve's current
  `sieve.yaml` + `sepio_classes.yaml` **verbatim** — snake_case, `EvidencePacket`
  + the rich evidence items, the lowercase `EvidenceDirection`/`EvidenceStrength`
  enums, and the `example.org/sepio-model` base URIs. **No** re-basing onto the
  minimal kernel, **no** URI cleanup, **no** enum upper-casing.
- **Add `TextMiningResult` (the "C" shape) in mondo-ai's own overlay**, not in the
  vendored files. This keeps the vendored copies pristine while giving the
  extraction pipeline a proper home for text-mining/LLM output. It is expressed in
  the full model's idiom: `is_a SieveDataItem` + `CuratedEvidence` (so it inherits
  `value`, `rating`, `eco_code`, `eco_label`) with the spec's C slots
  (`extraction_score`, `document_section`, `text_location`, `extraction_method`) —
  all **optional**, so a bare `value` instance is still valid ("A stays valid").
- **mondo-ai extends, never modifies, sieve's model** — the same pattern it already
  uses for `ResourceSnapshot`/`EntitySnapshot`/`Synonym`/`EntityMapping`.

## The delta being migrated

sieve's model differs from mondo-ai's vendored copy in three ways:

1. **camelCase → snake_case** on every SIEVE-layer *and* SEPIO-base slot
   (`hasEvidenceLines`→`has_evidence_lines`, `directionOfEvidenceProvided`→
   `direction_of_evidence_provided`, `evidenceItemType`→`evidence_item_type`,
   `sourceName`/`sourceId`/`sourceSubject`/…→`source_*`, `trustLevel`→`trust_level`,
   `contributionType`→`contribution_type`, `quoteLocation`→`quote_location`,
   `methodName`→`method_name`, `mappingJustification`→`mapping_justification`, …).
   This is the bulk of the work.
2. **New classes/slots** sieve gained: the `CuratedEvidence` mixin
   (`rating`/`eco_code`/`eco_label`) on all six evidence-item types;
   `CurationDecision` + `DecisionType`; `pull_request`/`issue`/`created_with` on
   `CurationActivity`; `mapping_set` on `ConcordanceItem`.
3. **`AgentContribution.date`** range flips `date` → `string` (sieve's
   gen-pydantic / Python 3.10 guard).

## Target schema layout

```
src/mondo_ai/schema/
  sepio_classes.yaml     ← REPLACED verbatim with sieve's snake_case copy
  sieve.yaml             ← NEW, verbatim from sieve (replaces sieve_sepio.yaml)
  mondo_ai_sieve.yaml    ← overlay: imports sieve.yaml; keeps mondo classes
                            (ResourceSnapshot / EntitySnapshot / Synonym /
                             EntityMapping + StatementPredicate / RecommendedAction /
                             SynonymScope); ADDS TextMiningResult
```

- `sieve_sepio.yaml`, the camelCase `sepio_classes.yaml`, and
  `sepio_classes_full.yaml` are removed.
- `datamodel/sieve_models.py` is regenerated from `mondo_ai_sieve.yaml` via the
  **existing `just gen-pydantic` recipe** (`project.justfile`:
  `uv run gen-pydantic {{schema_path}} > src/mondo_ai/datamodel/sieve_models.py`,
  where `schema_path = src/mondo_ai/schema/mondo_ai_sieve.yaml`). The generated
  file stays mypy/ruff-excluded, mirroring sieve.

## Migration inventory (Python surface)

Modules referencing the model (`grep` of `sieve_models`/`EvidencePacket`/
`SieveEvidenceLine`/`EvidenceDirection`): `cli.py`, `stats.py`, `pipeline.py`,
`web/app.py`, `agents/synthesis.py`, `agents/prompts.py`,
`storage/jsonl_store.py`, `storage/index.py`, `packets/builder.py`,
`scoring/scorer.py`, `enrichment/enricher.py`, `datamodel/__init__.py`,
`datamodel/mappings.py`, `datamodel/loaders.py`, `extraction/mondo_extractor.py`,
`validation/validator.py`. Plus test files and YAML fixtures under `tests/`.

## TDD phasing — each phase ends with `just test` green

Per the repo's TDD rule: tests first, and **no relaxing a test to make it pass**.

1. **Schema swap + regenerate.** Drop in the vendored `sieve.yaml` +
   `sepio_classes.yaml`; repoint the overlay's `imports` to `sieve`; add the
   `just gen-pydantic` recipe; regenerate `sieve_models.py`; fix
   `datamodel/__init__.py` + `mappings.py` exports so the package imports cleanly.
   (Downstream code still broken — expected.)
2. **Migrate producers & consumers** camelCase → snake_case across all modules in
   the inventory. No behaviour change intended. Update `loaders.py` polymorphic
   dispatch to the new names.
3. **Add `TextMiningResult` (C)** to the overlay + loader dispatch; regenerate.
   Tests: an A-shaped item and a C-shaped item both validate inside an
   `EvidencePacket`; the loader round-trips C.
4. **Fixtures, doctests & docs.** Flip YAML test fixtures to snake_case, fix
   doctests, update datamodel docstrings.

## Non-goals

- Not re-basing onto the minimal kernel; not fixing the `example.org` base URIs;
  not upper-casing the direction/strength enums (all per "exactly as-is").
- Not wiring the *new* capabilities (`CurationDecision`, per-item
  `rating`/`eco_code`, GitHub provenance) into the pipeline beyond making them
  available. That is follow-up feature work, not this port.
- No data migration (regeneration is deterministic; IDs are content-addressed).

## Risks / open items

- **Slot-URI reads.** The rename is safe for Python *attribute* access but would
  break any code that looks up SEPIO *slot URIs* as strings. sieve flagged the same
  (their B4). Grep-verify in phase 1 before migrating.
- **Regeneration.** The `gen-pydantic` recipe already exists; confirm the
  regenerated model matches what the pipeline expects (LinkML/pydantic version
  drift can change output).
- **`TextMiningResult` and `EvidenceItemType`.** The vendored `EvidenceItemType`
  enum has no `TextMiningResult` value. Loader dispatch keys on the `type`
  (class-name) field, so the new class is registered there; the vendored enum is
  left untouched to preserve "as-is."
