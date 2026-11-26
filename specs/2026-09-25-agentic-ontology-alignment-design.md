# Agentic ontology alignment — design spec

Status: design as written 2026-09-25, **then built and run the same day**. Sections 1–7 are the
design as designed; [section 8](#8-as-built-deltas) records the four places running it proved
the design wrong, [section 9](#9-reference-run) the measured results, and
[section 10](#10-second-round-rules-as-a-value-set-and-consistency-conditioning) a second round
of changes: rules cited by CURIE against a published value set, and consistency conditioning
with Boomer. Where sections conflict, the later one wins — the tier table in §3 has six entries
in the code, not five; the ladder has nine rungs, not eight; and rows carry `curation_rule`
CURIEs, not `curation_rule_text` prose.

Implemented as `src/mondo_ai/align/`, driven by `just align-*` recipes and a Claude Code skill
`/align-ontologies`. Documented for users in `docs/alignment.md`.

## 1. Problem

Producing a mapping set between two disease resources (here: Mondo × ICD-10-CM) is not a
retrieval problem. Retrieval is the cheap part. The expensive part is **deciding whether two
concepts denote the same thing**, and the expensive-and-usually-skipped part is **recording why**
so the decision is reviewable and so a curator's time goes only where it changes an outcome.

Three things the system must therefore do that a lexical matcher does not:

1. Treat *ambiguity as a first-class outcome*. `sssom:NoTermFound` with a reason beats a
   plausible-looking wrong row (`PR-008`, `RJ-002`).
2. Attach **named rules** to every decision, not free text. A mapping is only meaningful
   relative to the curation rules it was made under — the OBO Academy framing. The rules already
   exist as a registry (`maprule:EV-*`, `SC-*`, `PR-*`, `ST-*`, `RJ-*`, `CM-*`); this system
   emits them as data.
3. Produce a **triage table** whose ordering is trustworthy: the top of the list must be where
   an expert's hour buys the most correctness.

## 2. Inputs, outputs, non-goals

**Inputs.** A YAML run config naming two or more *resources*. A resource is anything that can be
reduced to a set of concepts with identifiers, labels, synonyms, definitions and parents:
OWL/RDF, OBO, OBO-JSON, SSSOM TSV, plain TSV/CSV, or YAML. Format is declared, not sniffed.

**Outputs**, all under `alignments/<run_id>/`:

| Artifact | Purpose |
|---|---|
| `mappings.sssom.tsv` | The mapping set. Standard SSSOM plus declared extension slots. |
| `records.jsonl` | One rich `MappingRecord` per subject/object pair considered — every signal, feature, flag, rung, agent verdict and alternative. The audit trail. |
| `system_table.tsv` | The triage table. One row per subject, ranked by `review_priority`. |
| `alignment.duckdb` | Same data, queryable. |
| `report.md` | Run summary: counts by tier, rule histogram, structural defects, evaluation. |
| `run.yaml` | Config, resource checksums/versions, tool version, timings. Reproducibility. |

**Non-goals.** No ontology editing. No PR creation. No probabilistic global consistency solving
(that is Boomer's job, and a downstream consumer of this output). No attempt to be a general
OAEI-style matcher: this is tuned for biomedical terminologies with rich synonymy and residual
rubrics.

## 3. Pipeline

Seven stages. Stages 1–5 and 7 are deterministic and reproducible; stage 6 is not, and is
fenced off, cached, and separately labelled in the output.

```
  1 acquire  →  2 index  →  3 candidates  →  4 features  →  5 deterministic ladder
                                                                      │
                                                          settled ────┤──── unsettled
                                                                      │           │
                                                                      │     6 agentic adjudication
                                                                      │           │
                                                                      └─────┬─────┘
                                                                            ▼
                                                              7 triage + structural checks + emit
```

### Stage 1 — acquire

`ResourceAdapter` protocol → `Concept(id, label, definition, exact_synonyms, other_synonyms,
parents, children, xrefs, obsolete, extra)`. Adapters: `obo`, `owl` (pyoxigraph, streaming),
`obojson`, `sssom`, `tsv`, `yaml`.

Downloads are content-addressed: URL → `data/align/<name>.<ext>` with a recorded sha256 and
retrieval date. `just align-download-*` recipes own this; nothing in Python fetches implicitly.

The OWL adapter reads `rdfs:label`, `skos:prefLabel`, `skos:altLabel`, `oboInOwl:hasExactSynonym`,
`oboInOwl:hasRelatedSynonym`, `IAO:0000115`, `rdfs:subClassOf` (named superclasses only),
`oboInOwl:hasDbXref`, `owl:deprecated`. ICD-10-CM from mondo-ingest
(`purl.obolibrary.org/obo/mondo/sources/icd10cm.owl`, UMLS2RDF 2024ab, 97,903 codes) uses
`rdfs:label` + `oboInOwl:hasExactSynonym`, so no source-specific branch is needed.

### Stage 2 — index

Object-side string index with four lookup surfaces:

- **raw** — whitespace-collapsed literal.
- **normalized** — NFKD, diacritics stripped, dashes/quotes unified, bracketed segments removed,
  casefolded, whitespace collapsed. Non-semantic only.
- **variant** — normalized string after one *exact-preserving* rewrite from a named, bounded rule
  table (possessive add/strip, `disease`↔`disorder`, `syndrome`↔`disease`, arabic↔roman type
  numbers, British↔American spelling, `T-cell`↔`T cell`, comma inversion, `deficiency of X`↔
  `X deficiency`, `X, type N`→`X type N`). Each rewrite carries a rule id and a certainty.
  Ported from MeDIC's lexical grounder and the `map_icd10cm` port of it.
- **tokens** — IDF-weighted inverted index over content tokens (stopwords and the
  non-discriminative heads `disease`/`disorder`/`syndrome`/`unspecified`/`other` removed) for
  candidate retrieval.

Every surface records *which field* the string came from (`label` / `exactSynonym` /
`relatedSynonym`), because evidence strength differs (`EV-001` vs `EV-003` vs `EV-007`).

### Stage 3 — candidate generation

Per subject concept, a bounded candidate pool from independent generators, each contributing a
typed `Signal(kind, rule_id, certainty, detail)`:

| Generator | Signal | Notes |
|---|---|---|
| `lexical_exact` | `EV-001`/`EV-002`/`EV-003` | raw / normalized / synonym-crossing hits |
| `lexical_variant` | `EV-004` | records the rewrite rule in `subject_preprocessing` |
| `token_retrieval` | `EV-006` | IDF-weighted overlap, top-k |
| `fuzzy_edit1` | `EV-006` | edit-distance-1 neighbours of the normalized label |
| `broad_variant` | `EV-006` | qualifier-strip / `other X`→`X`; widens the pool only |
| `proxy` | `EV-005` | both sides already mapped to a shared third concept |
| `structural` | `EV-008` | parents/children of concepts already anchored in this run |

Candidates are **capped per 3-character ICD category** so that laterality subcodes
(`M08.011`, `M08.012`, …) cannot crowd the general rubric out of the pool — a lesson from the
`map_icd10cm` run.

### Stage 4 — features

Deterministic, per (subject, object) pair. These are what both the ladder and the agent see.

*Lexical*: best similarity per surface; whether the winning hit was label↔label; the set of
rewrite rules that fired; token Jaccard; token containment in each direction (an asymmetry
signal for broad vs narrow).

*Scope flags*, each mapped to a citable rule:

| Flag | Detector | Rule |
|---|---|---|
| `residual_rubric` | object label matches `other` / `unspecified` / `NEC` / `NOS` / `not elsewhere classified` | `SC-001`, `SC-002` |
| `disjunctive` | object label joins two disease heads with `or` / `and` | `SC-004` |
| `finer_variant` | object carries laterality, site, stage, encounter or `with <complication>` | `SC-005` |
| `numbered_subtype` | subject ends in a type number, roman numeral, or `complementation group X` | `SC-006` |
| `possibly_coextensive` | object is residual-labelled **but** every child's label contains the subject's head | `SC-007` |
| `susceptibility` | subject label says susceptibility / predisposition | `CM-006` |
| `gene_level` | subject is `<GENE>-related` or a molecular defect | `CM-007` |
| `chapter_range` | object id is a range (`E00-E90`) or foreign-vocabulary shaped | `ST-005` |
| `off_category` | object sits in ICD chapter R, S–T, V–Y, Z, O or P | `CM-002`/`CM-003`/`CM-009` |

`possibly_coextensive` matters disproportionately: distinguishing `SC-007` from `SC-001` is
named in the rule registry as the single hardest judgement, and it is exactly the case where an
automatic reject is wrong. It is never auto-decided; it is routed to the agent with the object's
children listed, and it raises `review_priority`.

*Ambiguity*: how many distinct object ids tie at the winning tier, and the margin to the runner-up.

*Structural*: whether accepting this pair would create a proxy merge (`ST-001`) or a reverse
proxy merge (`ST-002`) against decisions already taken in this run.

### Stage 5 — deterministic ladder

First rung that fires decides. Rungs `D0`–`D7`; the rung id is emitted as `decision_path`, so
every row says which rule settled it.

| Rung | Condition | Result |
|---|---|---|
| `D0` | `chapter_range` or obsolete object | reject, `ST-005`/`ST-004` |
| `D1` | unique raw label↔label hit, no blocking flag | `skos:exactMatch`, 0.99, `EV-001`+`PR-001` |
| `D2` | unique normalized label↔label hit, no blocking flag | `skos:exactMatch`, 0.95, `EV-002` |
| `D3` | unique hit crossing into an exact synonym, no blocking flag | `skos:exactMatch`, 0.90, `EV-003` |
| `D4` | unique exact-preserving variant hit, no blocking flag | `skos:exactMatch`, 0.85, `EV-004` |
| `D5` | a D1–D4 hit **blocked** by a scope flag | proposal only → agent |
| `D6` | ≥2 distinct object ids at the winning tier | → agent, `PR-008`/`RJ-002` carried |
| `D7` | retrieval-only candidates, or none | → agent, or `sssom:NoTermFound` + `RJ-001` |

(A ninth rung, `D8`, was added after measurement — see [§8.3](#83-rung-d8--token-overlap-is-a-verdict-not-a-question).)

Blocking flags are `residual_rubric`, `disjunctive`, `finer_variant`, `off_category`,
`susceptibility`, `gene_level`. They never silently downgrade a match; they move the decision to
a stage that can explain itself.

### Stage 6 — agentic adjudication

A `pydantic-ai` agent with structured output, batched, cached and bounded.

*Prompt.* System prompt carries the curation-rule registry in compact form, the Mondo tiering
policy, and the accept/reject worked examples from the ICD-10-CM review guide. Per item: the
subject with its definition, exact synonyms, parents and child count; each candidate with label,
synonyms, parent label, a sample of children, and its computed flags.

*Output* per item: `decision` (`exact`/`broad`/`narrow`/`close`/`related`/`none`), `object_id`,
`confidence`, `rules` (ids from the registry), `justification` (one concrete sentence naming the
object and why), `alternatives`, `conflation` (the `CM-*` ids it knowingly crossed),
`needs_expert` and `expert_question`.

*Discipline.* Only candidate ids offered may be returned; a hallucinated id fails validation and
the item is retried once, then routed to review with `RJ-002`. Optional self-consistency: `k`
samples, majority decision, `agent_agreement = majority_share`. An optional critic pass re-reads
accepted rows against the rules and can downgrade.

*Caching.* Keyed on sha256 of the rendered item payload plus model id, so reruns cost nothing
and a changed candidate pool invalidates exactly the items it changed.

*Labelling.* Every agent-derived row carries `semapv:CompositeMatching`, `mapping_tool`,
`mapping_tool_version`, and is Tier 3 under the Mondo guide — an AI proposal, not a curated
assertion. The output never pretends otherwise.

### Stage 7 — triage, structural checks, emit

Set-level checks first, because they are invisible row by row: proxy merges (`ST-001`), reverse
proxy merges (`ST-002`), duplicate pairs, object ids absent from the declared object version
(`ST-003`), obsolete objects (`ST-004`).

Then a **review tier** per subject:

| Tier | Meaning |
|---|---|
| `AUTO_ACCEPT` | Deterministic rung D1–D3, no flags, no ambiguity, agent (if consulted) agrees. |
| `SPOT_CHECK` | Defensible but resting on a weaker rung or a single agent verdict. Sample it. |
| `EXPERT_REVIEW` | A human decides. Anything below is not shipped as an assertion. |
| `REJECTED` | Actively decided against, with a rule. Still a row, still a finding. |
| `NO_MATCH` | `sssom:NoTermFound` with `RJ-001`. Distinguishes "none exists" from "nobody looked". |

(A sixth tier, `QUEUED`, was added after measurement — see [§8.2](#82-queued--one-tier-was-doing-two-incompatible-jobs).)

`review_priority` ∈ [0,1], the ranking key of the system table, is a transparent weighted sum —
not a learned score — so a curator can read why a row is near the top:

| Component | Weight | Rationale |
|---|---|---|
| ladder/agent disagreement | 0.25 | The two independent methods conflict. Highest-yield review. |
| agent uncertainty (1 − confidence, or 1 − self-consistency) | 0.20 | The model says it is unsure. |
| `possibly_coextensive` | 0.15 | The registry's hardest judgement (`SC-007` vs `SC-001`). |
| ambiguity (candidates tied at the winning tier) | 0.12 | `PR-008`: assert nothing, ask. |
| structural defect (`ST-001`/`ST-002`) | 0.12 | Set-level corruption; must be resolved by a curator. |
| subject impact (log descendant count, normalised) | 0.08 | A wrong grouping mapping propagates. |
| declared conflation (`CM-*` invoked) | 0.08 | A boundary was knowingly crossed. |

Rows with `needs_expert` from the agent are floored at 0.6. `AUTO_ACCEPT` rows are capped at 0.2
so they never displace a genuine question.

**SSSOM emission.** Standard slots: `subject_id`, `subject_label`, `predicate_id`, `object_id`,
`object_label`, `mapping_justification`, `confidence`, `subject_preprocessing`,
`object_preprocessing`, `mapping_tool`, `mapping_tool_version`, `author_id`, `comment`,
`curation_rule_text`, `similarity_score`, `mapping_date`. Non-standard slots are declared in the
header's `extension_definitions` rather than smuggled in:

`signals`, `decision_path`, `review_tier`, `review_priority`, `ambiguity_count`, `risk_flags`,
`agent_confidence`, `agent_agreement`, `agent_alternatives`, `conflation_rules`,
`deterministic_confidence`, `expert_question`, `mapping_tier`.

The set-level `comment` carries the review protocol: versions, curation rules in force,
use case, and **every conflation the set knowingly permits** — a set that crosses the
disease/phenotype boundary silently is not reviewable however good its rows.

## 4. Evaluation

Mondo asserts its own ICD-10-CM cross-references with precision qualifiers
(`MONDO:equivalentTo`, `MONDO:mondoIsBroaderThanSource`, …). These are held out entirely from
the run — they are not seeded as anchors, not used as proxy evidence — and then used as a gold
standard by `just align-eval`.

(This section's scoring was wrong and was replaced — see
[§8.1](#81-evaluation-scope--precision-was-measuring-mondos-coverage).)

Reported: precision / recall / F1 on `skos:exactMatch`, broken down by tier and by rung, plus a
confusion table of predicted vs asserted predicate, plus the rows where the pipeline disagrees
with Mondo (which are themselves a curation finding, not only an error signal). A run that
cannot report these numbers is not finished.

## 5. Skill

`.claude/skills/align-ontologies/SKILL.md`, invoked as `/align-ontologies`. It does not
reimplement the pipeline: it selects or writes the run config, runs the `just` recipes in order,
reads `report.md`, and then does the thing an agent is actually better at than a script — reading
the top of the system table and telling the curator what the open questions *are*. Reference
files carry the curation rule registry and the disease conflation pack so the adjudication
prompt and the skill cite the same ids.

## 6. Testing

Per repo policy, tests first, doctests as documentation, no mocks standing in for real logic.

- `normalize` / variant rules: table-driven, including the cases that motivated them
  (`Marfan syndrome` ↔ `Marfan's syndrome`).
- adapters: small real fixtures in each format; the OWL adapter against a hand-built RDF/XML file.
- index: lookup surfaces, field provenance, IDF ranking order.
- features: one fixture per scope flag, including a `SC-007` coextensive rubric and a
  `SC-001` residual that must not be confused with it.
- ladder: one test per rung, plus the blocking-flag path.
- triage: priority ordering is a total order with the documented components; monotonic in each.
- structural: proxy merge and reverse proxy merge are detected on a constructed set.
- emit: round-trips through `sssom-py`; extension definitions survive.
- agent: a `FakeModel` exercises batching, id validation, retry and caching without network.
  Real-API tests are marked `integration`.

## 7. Known risks

- **The agent is the weakest link and the most expensive.** Bounded by `max_items`, cached, and
  never allowed to invent an id. Its rows are Tier 3 and labelled as such.
- **ICD-10-CM residual rubrics are the dominant failure mode** in both directions: reject a
  genuine `SC-007` and recall drops; accept a `SC-001` and the crosswalk pulls wrong cohorts. The
  design deliberately refuses to auto-decide either and pays for it in review volume.
- **Coverage will look low.** Mondo is dominated by gene-level and numbered OMIM subtypes with no
  ICD-10-CM counterpart; `map_icd10cm` measured ~13.5% on a rare-disease list. `NO_MATCH` rows
  with `RJ-001` are the correct output there, and the report must not present them as failure.

---

## 8. As-built deltas

Four things the design got wrong, found by running it. Each is recorded here rather than
silently patched into the sections above, because the reasoning is the useful part.

### 8.1 Evaluation scope — precision was measuring Mondo's coverage

The design said a predicted exact match with no asserted counterpart is a false positive: "the
strict reading". Measured, that reported **0.20 precision**, and every single one of the 52
"errors" was a plausible new mapping (`salmonellosis` → `A02.0`, `transposition of the great
arteries` → `Q20.3`). Mondo cross-references only ~2% of its terms to ICD-10-CM, so the strict
reading made precision a function of Mondo's coverage rather than of this pipeline's
correctness.

Fixed by scoping: a subject is evaluated only if it carries at least one *qualified*
cross-reference to the object vocabulary — there, the source has looked, and a disagreement is
a real disagreement. Out-of-scope predictions are counted as `novel_mapping` and folded into
precision in neither direction. Same run, same code: **0.987 precision, 0.845 recall**.

A third disagreement kind fell out of this and is the most interesting one:
`declined_by_source`, where the source cross-referenced the term and declined to call it
equivalent. Somebody looked, and the pipeline disagreed anyway.

### 8.2 `QUEUED` — one tier was doing two incompatible jobs

The design had five tiers, with items the adjudication budget never reached landing in
`EXPERT_REVIEW`. On the full run that made `EXPERT_REVIEW` **89% of the set**, which is true of
neither the rows nor any curator's worklist, and which destroyed the only signal the triage
table carries.

`QUEUED` is now a sixth tier. `EXPERT_REVIEW` means an adjudicator examined the pair and a
human has to decide; `QUEUED` means nobody has looked yet. The same run then reports 726 rows
needing a human (2.3%) and 23,538 waiting for budget. Unexamined rows are also excluded from
the `needs_expert` priority floor, so they cannot outrank questions somebody actually formed.

The general lesson, and the reason this is recorded rather than quietly fixed: "we decided
against this", "we could not decide this", and "nobody has looked at this" are three different
claims, and a pipeline that collapses any two of them lies to its reader.

### 8.3 Rung `D8` — token overlap is a verdict, not a question

Token retrieval returns something for almost any subject, so `D7` swallowed 28,660 subjects,
most of them noise sharing a single word. The design had no way to say "retrieval found things
and none of them is a candidate".

Rung `D8` settles those as `sssom:NoTermFound` with `RJ-004` (token overlap only) when no
retrieval-only candidate covers at least `min_retrieval_score` of the subject's discriminative
IDF mass. This is a deterministic, reproducible judgement with the rejected candidates retained
in `records.jsonl`, not an expert question.

The floor was chosen by measurement, not taste. On the full run, against the in-scope gold:

| floor | queue removed | recoverable gold exacts lost |
|---|---|---|
| 0.3 | 4,343 (13%) | 1 in scope (7 of 63 overall) |
| 0.5 | ~15,500 (54%) | 22 of 63 |
| 0.7 | ~23,500 (82%) | 37 of 63 |

0.3 is the default: queue length is cheap and recall is expensive to rebuild.

### 8.4 `author_id` — a slot type silently voided the whole file

`author_id` was filled with `"mondo-ai align (deterministic)"`. It is a CURIE-valued slot, so
`sssom-py` dropped **every row**: the file parsed, reported zero mappings, and nothing said
why. The Mondo curation guide wants Tier 3 provenance in `mapping_tool` /
`mapping_tool_version` anyway, and inventing an ORCID for a pipeline would be worse than
leaving the slot out. Which adjudicator produced a verdict now lives in the declared
`verdict_source` extension slot, and a round-trip through `sssom-py` is a regression test.

A related bug in the same area: the set-level `curie_map` appended the resource prefixes last
and so clobbered the real Mondo and ICD-10-CM namespaces with placeholders.

## 9. Reference run

Mondo `2026-09-01` (32,109 active terms) × ICD-10-CM `2024ab` from mondo-ingest (97,903
codes), agent budget 1,200 items on `claude-opus-5`, evaluation held out:

| | |
|---|---|
| deterministic stages | 78s total |
| agent stage | 117s, 39 API calls at concurrency 12, 0 validation failures |
| cost | $8.27 / 1,200 items = $0.0069 per item; ~$162 to clear the remaining queue |
| exactMatch precision / recall / F1, in scope | **0.987 / 0.845 / 0.910** |
| `AUTO_ACCEPT` precision | **0.989** (462 TP, 5 FP) |
| `EXPERT_REVIEW` | 726 rows (2.3%): 457 structural, 221 disagreement, 48 `SC-007` |
| `QUEUED` | 23,538 |
| novel mapping candidates | 2,061 |
| reverse proxy merges (`ST-002`) | 419 |

Recall is bounded by the agent budget rather than by the method: 90 of the 96 in-scope misses
sit in `QUEUED`.

Two findings that vindicate the conflation vocabulary:

- Mondo carries explicitly **veterinary** terms — "polydactyly, domestic cat", "persistent
  right aortic arch, puma", "chronic active hepatitis, non-human animal". ICD-10-CM is a human
  classification, and the adjudicator cited `CM-008` (cross-species analog) 596 times,
  rejecting almost all of them. A matcher without a conflation vocabulary cannot express this,
  let alone act on it.
- 419 **reverse proxy merges**: `A30.1` claimed as an exact match by both *tuberculoid leprosy*
  and *paucibacillary leprosy*; `A48.0` by both *gas gangrene* and *infection due to
  Clostridium perfringens*. No individual row reveals these, which is why the check is
  set-level.

---

## 10. Second round: rules as a value set, and consistency conditioning

Three changes after the first reference run, in response to review.

### 10.1 Rules cited by CURIE against a published value set

`curation_rule_text` carried each rule's full title in every row, which made a 32,109-row file
mostly repeated English and gave no machine anything to join on. Rows now use the URI-valued
`curation_rule` slot:

```
curation_rule    maprule:EV-002|maprule:SC-001|maprule:PR-002
```

The titles live once, in a value set generated from `mondo_ai.align.rules` by
`just align-valueset` into three shapes:

| Artefact | For |
|---|---|
| `vocabulary/maprule.ttl` | SKOS concept scheme — a `skos:Concept` per rule with `notation`, `prefLabel`, `definition`, grouped into per-family collections, so `maprule:SC-002` resolves |
| `vocabulary/maprule.yaml` | LinkML enum (`MappingCurationRule`) plus a `CurationRuleAnnotation` class, to import and validate against |
| `docs/maprule.md` | the human registry, one table per family |

Two properties travel with each concept, because they are the two facts a downstream tool
needs in order to *act* rather than merely display: `maprule:blocksExactMatch` and
`maprule:isRejectionGround`. A test fails if any artefact drifts from the registry the code
cites, so the rules the pipeline applies and the rules the set publishes cannot disagree.

The prose form survives in exactly one place — the set-level review protocol in the SSSOM
header — because that paragraph is read once by a human, not once per row.

### 10.2 Boomer: `boomer_prior` and `boomer_posterior`

Everything the pipeline scored up to this point was **local**: one subject, its candidates, its
flags. Nothing in it can see that accepting a row makes another row impossible. Boomer can, so
the asserted mappings now go through it: each as a hypothesis with the pipeline's confidence as
its prior, both sources' subclass axioms over the mapped signature as hard constraints.

The two columns, defined precisely because "prior" and "posterior" are easy to print and easy to
misread:

- **`boomer_prior`** — the probability this pipeline put on the relation it asserted, before
  Boomer saw it. Exactly the ptable cell that went in. Per mapping, exact.
- **`boomer_posterior`** — that relation's probability after conditioning, as a
  **max-marginal**: for each relation the mapping could take, the score of the best consistent
  assignment of its whole clique under that choice, normalised across the choices.

A max-marginal is not a sum-marginal, and the distinction is not pedantry: Boomer 0.2 reports
MAP assignments and subsequent solution *scores*, not per-hypothesis sums, so a max-marginal is
what its output supports. Calling it a marginal posterior without that qualification would
overstate what was computed.

Getting it took a second pass and one piece of arithmetic worth writing down. For each mapping
and each relation it has prior mass on, a renamed copy of its clique pins that choice; all
copies go into a single invocation, because Boomer partitions by connectivity and renamed
copies are disconnected by construction. Pinning necessarily replaces the mapping's own prior
with near-certainty, so the pin's mass is subtracted back out in log space and the real prior
added in its place:

```
score(r) = clique_score(variant) − log PIN_MASS + log prior(m = r)
```

The first implementation omitted that correction and every unconstrained mapping came out
uniform at 0.25 — the prior had been thrown away. With it, **a mapping nothing constrains has
posterior exactly equal to its prior**, which is both the correct answer and the standing test
that the arithmetic is right: it holds for all 1,956 unconstrained mappings in the reference
run.

Two honesty constraints on the column:

- A posterior normalised over an **incomplete** option set is not comparable to its prior.
  Boomer sometimes partitions a pinned variant into its singleton bag, leaving that relation
  unscored, so coverage is tracked and a posterior below `MIN_POSTERIOR_COVERAGE` (0.9 of the
  prior mass) is suppressed rather than emitted.
- An empty cell means Boomer did not condition that row. It is not a posterior of zero, and
  `boomer_constrained` says which case a row is in.

`boomer_conflict` — the probability consistency took off our claim — became a triage component
at weight 0.15, with the other seven rescaled. A mapping the pipeline was confident about and
the constraints will not allow is the most specific reason to review a row in the whole set.

What it found on the reference run, in the first minute of being wired up: **231 mappings
overruled**, among them `MONDO:0009050 Cushing disease due to pituitary adenoma` asserted
`exactMatch` to `ICD10CM:E24 Cushing's syndrome` at prior 0.88, left at posterior 0.017 and
reassigned `broadMatch`. That is the `SC-006` example the rule registry is written around, and
the pipeline had shipped it as an exact match — Boomer caught it only because another Mondo term
also claimed E24. Also `Q05 Spina bifida` claimed exactly by *spina bifida cystica*,
*myelomeningocele* and *meningocele*, and `E23.0 Hypopituitarism` by six Mondo terms.

### 10.3 The `is_a` modifier bug

Found while building Boomer's hard constraints, and the most consequential defect in the
project so far. Mondo's OBO release writes

```
is_a: MONDO:0024290 {source="NCIT:C118172"} ! adenoma
```

The parser stripped the `!` comment but not the trailing modifier block, leaving
`MONDO:0024290 {source="NCIT:C118172"}` — an identifier matching no concept, silently dropped.
36,637 lines in Mondo carry such a block. Everything derived from the hierarchy had been
computed against a graph thinner than the file describes: descendant counts (the triage impact
term), the `SC-007` coextensive test, and now Boomer's constraints. After the fix the loaded
hierarchy has 46,582 parent edges and 4 rootless active terms.

The lesson is about where to look for this class of bug. It produced no error, no warning and
no obviously wrong output — only slightly weaker signals everywhere. It surfaced because Boomer
choked on a malformed IRI, i.e. because a *second* consumer with stricter input requirements
was pointed at the same data.
