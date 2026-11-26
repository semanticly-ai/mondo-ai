# Mondo × SNOMED CT — seeded alignment and reconciliation

Status: designed, built and run 2026-11-06. Extends
[the agentic alignment design](2026-09-25-agentic-ontology-alignment-design.md); read that
first. This spec covers what a **seeded** surface adds — a pair where mapping sets already
exist — and records three defects in the shared pipeline that this surface exposed.

Run it: `just align-snomed-prepare` (inputs), `just align-snomed` (deterministic run),
`just align-reconciled` (consolidated pairs by status). Config:
`conf/align/mondo-snomed.yaml`. Code: `src/mondo_ai/align/omop.py`, `reconcile.py`.

## 1. Problem

Mondo × SNOMED CT is not a cold start. Two mapping sets exist:

| seed | rows | what it is |
|---|---|---|
| `mondo` | 9,134 SCTID pairs, 9,026 subjects | Mondo's released `mondo.sssom.tsv`; xrefs of mixed provenance (many imported with DOID and UMLS), no per-row confidence |
| `omop` | 10,442 pairs, one per subject | OMOP's Mondo `Maps to` rows (`background/concept_relationship_manual.xlsx`) joined to their `CONCEPT_RELATIONSHIP_METADATA` (`…_metadata_stage.csv`): 6,795 exact, 3,429 broad, 225 with no predicate; all "Machine-produced mappings with spot-curation", `AM-tool_U` |

6,323 pairs are identical between them, 2,811 only in Mondo, 4,119 only in OMOP; on 606
subjects they name disjoint objects. The deliverable is every pair anyone proposed, each with
its justification, plus whatever the run finds that neither seed has.

## 2. Method

### 2.1 Seeds never enter discovery

Discovery is the reference ladder, unchanged. Mondo's xrefs are its evaluation gold and stay
held out (`evaluation.hold_out`); OMOP's rows are never offered to the candidate generator.
Seeds meet the run in a separate stage, **after** discovery has settled
(`reconcile:` in the config). Consequence: when a string identity backs a seed pair, that
backing is independent of the seed, and the discovery numbers remain an honest measurement.

The alternative — seeds as `EV-005` proxy candidates — was rejected: it feeds the gold into
the input, voids the evaluation, and makes every seed pair look supported by itself.

### 2.2 Two seeds agreeing is one assertion

OMOP's Mondo mappings post-date Mondo's SNOMED xrefs, and 69% of Mondo's pairs reappear
verbatim in OMOP. Counting their agreement as corroboration would double-count one claim.
So only **lexical evidence** (or an adjudicating agent) corroborates; a pair both seeds assert
and no string supports is `seed_only`, `SPOT_CHECK`.

### 2.3 Per-pair evidence on the discovery code path

For each pair, the discovery `CandidateGenerator` is re-run for that subject over an index of
just that pair's objects (`reconcile.pair_candidates`), so a seed pair's lexical evidence —
rewrites, tiers, scope flags, the `SC-007` test — is computed exactly as a discovered pair's
would be, including against retired SNOMED concepts the discovery index never holds. Object
flags come from the detectors directly, not from the candidate, so a seed pair with no shared
string still carries its `RJ-005` or `SC-005`.

### 2.4 Reconciliation statuses

First problem found names the row (`reconcile._classify`):

| status | tier | condition |
|---|---|---|
| `inactive_subject` | REJECTED | subject obsolete or absent in this Mondo release |
| `unknown_target` | REJECTED | object not in the extracted vocabulary (`ST-003`) |
| `inactive_target` | REJECTED | object retired in SNOMED (`ST-004`); successor written to `replacement_object` |
| `predicate_conflict` | EXPERT_REVIEW | seeds (or the agent) assert different predicates |
| `rule_conflict` | EXPERT_REVIEW | an exact claim against a blocking flag |
| `undeclared_conflation` | EXPERT_REVIEW | a `CM-*` the config does not permit |
| `object_conflict` | EXPERT_REVIEW | subject claimed exact to two objects (`ST-001`) |
| `reverse_proxy_merge` | EXPERT_REVIEW | object claimed exact by two subjects (`ST-002`) |
| `predicate_vs_lexical` | EXPERT_REVIEW | non-exact claim, identical strings (tier ≤ 3) |
| `consistency_conflict` | EXPERT_REVIEW | Boomer overruled an otherwise clean row |
| `corroborated` | AUTO_ACCEPT / SPOT_CHECK | seed exact + unblocked string identity; SPOT_CHECK with a subject flag or permitted conflation |
| `seed_only` | SPOT_CHECK | seed claim, no independent backing, nothing against it |
| `discovery_only` | the run's tier | no seed asserts it: a candidate addition |

Predicate proposal: seeds in config (trust) order, else the discovery verdict. Confidence: the
strongest line supporting that predicate. Boomer then conditions the consolidated set, with
priors capped at 0.99 (§4.3).

### 2.5 Justification, lexmatch-style

Two files, two shapes of the same evidence:

- `consolidated.sssom.tsv` — one row per pair. `mapping_justification` is
  `semapv:LexicalMatching` (string only), `semapv:CompositeMatching` (two or more lines), or the
  seed's own (seed only). The winning string identity is in the standard slots `match_string`,
  `subject_match_field`, `object_match_field` (`rdfs:label` / `oio:hasExactSynonym` /
  `oio:hasRelatedSynonym`) and `subject_preprocessing` (the rewrite). Declared extensions:
  `reconciliation_status`, `review_tier`, `evidence_sources`, `seed_predicates`,
  `discovery_predicate`, `discovery_path`, `risk_flags`, `structural_flags`, `object_status`,
  `replacement_object`, `boomer_predicate`, `boomer_agrees`. Rules by CURIE in `curation_rule`.
- `evidence.sssom.tsv` — the ledger: one row per pair per line of evidence (`lexical`, each
  seed with its `mapping_source`, `agent`), plus a lexical row for every string-identity
  candidate discovery considered — a complete lexmatch over the run, the shape OAK writes.

### 2.6 SNOMED from OMOP

`omop.extract_snomed` reduces the Athena zip (5 GB uncompressed) to `data/align/snomed.tsv`
in ~40 s, byte-for-byte reproducibly, with a provenance sidecar (zip sha256, SNOMED editions).

- Classes `Disorder`, `Clinical Finding`, `Morph Abnormality`. Morphology is kept only so seeds
  into it resolve and get `RJ-005`; 228 seed targets are morphologies.
- Inactive concepts kept, obsolete, with the strongest successor (`Concept replaced by` >
  `same_as` > `poss_eq` > `was_a` > `alt_to`). 393 seed targets are inactive.
- English descriptions as **exact** synonyms, FSN semantic tags stripped into `semantic_tag`.
  SNOMED descriptions of a concept are synonymous by construction; this is the one place the
  pipeline trusts a source's synonymy unqualified, and the sidecar says so.
- Hierarchy from valid `Is a` rows only.

SNOMED flags (`flags.snomed_class`, `flags.snomed_laterality`): `Clinical Finding` →
`CM-004` (permitted); any other class → `RJ-005` (blocking); "of left knee" / "right-sided" →
`SC-005`. `RJ-005` now blocks `exactMatch` in the registry (it already was a rejection ground).

OMOP seed conversion (`omop.omop_seed_sssom`) follows the niehs_standards OMOP→SSSOM
transform: `AM-tool` → `semapv:UnspecifiedMatching`, raw fields in `comment`. 225 rows whose
metadata has no predicate are recorded as `skos:broadMatch` — OMOP's `Maps to` means "same or
broader", and the weaker half is the one that is certainly claimed. Excel stores numeric codes
as floats; `_cell` renders `64572001.0` as `64572001`.

## 3. Results, first full run

Mondo release of 2026-11 (63,391 terms, 32,137 active), SNOMED CT 2026-02-01 International /
2026-03-01 US edition via OMOP (223,588 concepts, 145,392 valid). Deterministic, no agent,
5 minutes.

**Discovery**, against Mondo's held-out xrefs: exactMatch precision **0.957**, recall 0.678;
`AUTO_ACCEPT` precision 0.960 (4,553 TP, 192 FP). 2,882 novel exact candidates. Recall is
bounded by the unadjudicated queue: 2,005 of 2,220 misses are `QUEUED`.

Of 205 `different_object` disagreements, 94 are granularity (gold is parent or child of the
prediction), 11 are SNOMED's finding/disorder split, and reading the rest shows mostly
Mondo-side errors (*agnosia* → gold *Apraxia*, *cleft palate* → *Cleft hard and soft palate*)
with a minority of real pipeline errors entering through synonyms.

**Reconciliation**: 13,695 pairs; **3,248 need a human**.

| status | tier | pairs |
|---|---|---|
| corroborated | AUTO_ACCEPT | 6,582 |
| seed_only | SPOT_CHECK | 2,246 |
| predicate_conflict | EXPERT_REVIEW | 1,392 |
| object_conflict | EXPERT_REVIEW | 743 |
| corroborated | SPOT_CHECK | 644 |
| predicate_vs_lexical | EXPERT_REVIEW | 401 |
| inactive_target | REJECTED | 383 |
| rule_conflict | EXPERT_REVIEW | 342 |
| inactive_subject | REJECTED | 282 |
| reverse_proxy_merge | EXPERT_REVIEW | 278 |
| discovery_only | AUTO_ACCEPT / SPOT_CHECK | 264 / 41 |
| consistency_conflict | EXPERT_REVIEW | 84 |
| undeclared_conflation | EXPERT_REVIEW | 8 |
| unknown_target | REJECTED | 5 |

Most `predicate_conflict` rows are Mondo-exact versus OMOP-broad on pairs whose strings are
identical (*auriculocondylar syndrome* → *Auriculo-condylar syndrome*): OMOP's machine
predicate is the likelier error, and the queue is the place to decide it, not a default.
282 OMOP rows point at Mondo terms obsoleted since OMOP's snapshot; 383 Mondo xrefs point at
retired SNOMED concepts, each with a named successor.

## 4. Defects this surface exposed in the shared pipeline

### 4.1 Related-synonym hits settled as exact

`CandidateGenerator.generate` gave any synonym crossing tier 3, so a hit through a Mondo
*related* synonym settled at `D3` as `exactMatch` — contradicting `EV-007` ("not sufficient
alone") and mondo-ingest's own lexmatch rules (related → `closeMatch`, weight 0). In the ICD
reference run this produced 126 exact matches resting only on a related synonym, **50 of them
`AUTO_ACCEPT`**: *alopecia* → *Alopecia areata*, *cerebrovascular disorder* → *Cerebral
infarction*. Fixed: such hits are strong retrieval candidates (tier 9, full score) and go to
adjudication. The ICD reference numbers in the 2026-09-25 spec predate the fix.

### 4.2 Gold xrefs to retired concepts were scored

A Mondo xref to a retired SNOMED concept made the live successor a false positive. On the
first smoke run that was 27 of 70 "false positives". `evaluate(objects=...)` now sets retired
gold targets aside; subjects with only retired gold are `gold_target_retired` (301 on the full
run), scored in neither direction — the same scoping argument as §8.1 of the parent spec.

### 4.3 Hard priors made Boomer unsolvable

OMOP writes confidence 1 on half its rows; `priors_for` turned that into a prior of exactly
1.0, a hard constraint. Two contradicting ones gave "No possible resolution of perplexity" for
the whole set. Consolidated priors are capped at 0.99, the ladder's own ceiling.

### 4.4 Two runs on the same inputs wrote different bytes

Diffing two full runs found Python's string-hash randomisation leaking into the output twice:
the index's posting *sets* were iterated directly, so signal order (and with it
`subject_preprocessing` and `curation_rule`) varied; and retrieval summed IDF weights over a
token set, so equal-on-paper scores differed in their last bit and ties reordered. Both are
now iterated sorted, and a test runs the generator and retrieval under eight hash seeds. Two
full runs now produce identical SSSOM, system table, records, ledger and disagreements;
`report.md` differs only in its timings, and every file carries the run date.

## 5. Not done, deliberately

- **Agent adjudication** is off in the config: the run is meant to be deterministic. 23,634
  discovery items are `QUEUED`. `just align-snomed-agent` runs it; budget is the user's call.
- **SNOMED labels** are in the outputs. SNOMED CT is licensed; Mondo's own set leaves them
  blank. Outputs stay under the gitignored `alignments/`; a publishable variant needs a
  redaction step.
- **Successor retargeting**: `inactive_target` rows name the successor but do not propose
  the retargeted pair. SNOMED's `REPLACED BY` is not always `SAME AS`, so that is a curation
  step, not a chaining rule.
