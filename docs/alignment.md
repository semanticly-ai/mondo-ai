# Ontology alignment

An end-to-end pipeline that turns two resources into an SSSOM mapping set where **every row
names the curation rules its verdict rests on**, and a triage table that says which rows need
a human.

Design and rationale: [`specs/2026-09-25-agentic-ontology-alignment-design.md`](
https://github.com/monarch-initiative/mondo-ai/blob/main/specs/2026-09-25-agentic-ontology-alignment-design.md).

Reviewing the output is a separate skill with its own audience: see the
[Mondo Mapping Review guide](mapping-review-guide.md), written for domain experts with no
ontology background, whose verdicts are the gold standard this pipeline's precision is measured
against.

## Why the rules matter more than the matching

Retrieval is the cheap part of building a crosswalk. The expensive part is deciding whether two
concepts denote the same thing, and the expensive-and-usually-skipped part is recording *why*,
so that the decision is reviewable and a curator's time goes only where it changes an outcome.

As the OBO Academy puts it: you can never determine the correctness of a mapping in the
abstract — only whether, under the curation rules declared for the set, subject and object
relate the way the predicate claims. So this pipeline treats the rule registry as data. Every
verdict cites ids from it, the adjudicating model is given it, and the report counts it.

Run `mondo-ai align rules` (or `just align-rules`) to see the registry. Six families:

| Family | What it judges |
|---|---|
| `EV` | evidence for sameness — what was actually observed |
| `SC` | scope and granularity — where extensions differ |
| `PR` | predicate selection |
| `ST` | structural integrity of the set |
| `RJ` | rejection grounds |
| `CM` | conceptual-model conflation (disease vs phenotype vs susceptibility vs gene) |

## Quick start

```bash
just align-download                 # Mondo + ICD-10-CM, with checksums
just align-smoke 300                # 300 subjects end to end, in seconds
just align-icd10cm                  # the full reference run
just align-report                   # read the report
just align-queue mondo-icd10cm 30   # the top of the review queue
```

## The pipeline

Seven stages. One through five and seven are deterministic and reproducible; six is not, and
is fenced off, cached, and separately labelled in the output.

```
 1 acquire → 2 index → 3 candidates → 4 features → 5 deterministic ladder
                                                            │
                                                settled ────┤──── unsettled
                                                            │          │
                                                            │    6 agentic adjudication
                                                            │          │
                                                            └─────┬────┘
                                                                  ▼
                                            7 triage + structural checks + emit
```

### 1 · Acquire

A resource is anything reducible to concepts with identifiers, labels, synonyms, definitions
and parents. Adapters: `obo`, `obojson`, `owl` (streaming, via pyoxigraph), `sssom`, `tsv`,
`yaml`. Format is **declared in the config, never sniffed**. Downloads are `just` recipes with
recorded checksums; nothing in the Python fetches implicitly.

### 2 · Index

Four lookup surfaces over the object resource, in descending strength: the raw literal, the
non-semantically normalised string, the string after one *named meaning-preserving rewrite*
(possessive add/strip, `disease`↔`disorder`, arabic↔roman type numbers, British↔American
spelling, comma inversion, `deficiency of X`↔`X deficiency`, …), and an IDF-weighted token
index for retrieval. Every posting records which field it came from, because a hit on a primary
label is `EV-001` and a hit on a synonym is `EV-003`.

### 3 · Candidates

Independent generators each contribute typed signals: exact lexical hits, rewrite hits, token
retrieval, edit-1 neighbours, scope-widening rewrites, proxy mappings via a third resource, and
hierarchical corroboration. The pool is capped globally *and per ICD three-character category*,
so laterality subcodes (`M08.011`, `M08.012`, …) cannot crowd `M08 Juvenile arthritis` out of
its own candidate list.

### 4 · Features and flags

Each detector returns a citable rule id, not a private label:

| Flag | Detects | Rule |
|---|---|---|
| residual rubric | "Other X", "X, unspecified", "NEC", "NOS" | `SC-001`, `SC-002` |
| disjunction | "Duchenne or Becker muscular dystrophy" | `SC-004` |
| finer variant | laterality, site, stage, encounter | `SC-005` |
| numbered subtype | "immunodeficiency 112", "complementation group C" | `SC-006` |
| **possibly coextensive** | a residual rubric whose children are all subtypes of the subject | `SC-007` |
| susceptibility | "susceptibility to", "predisposition" | `CM-006` |
| gene-level | "`GENE`-related", "caused by mutation in" | `CM-007` |
| chapter range | `E00-E90`, or a foreign-shaped id | `ST-005` |
| off-category chapter | ICD chapters R, S–T, V–Y, Z, O, P | `CM-002`/`CM-003`/`CM-009` |

`SC-007` is the one that earns its keep. `E76.21 Morquio mucopolysaccharidoses` divides only
into Morquio A, Morquio B and unspecified, so it **is** MPS IV — an `exactMatch`, despite the
residual wording. `A41 Other sepsis` also subdivides into sepsis variants, and yet it is a
genuine remainder, because its sibling `A40 Streptococcal sepsis` takes part of the same
subject matter. Three conditions separate them: the subject must share a content token with
the rubric's stem; every child must carry the whole stem; and no sibling may carry it. Passing
never decides anything — it routes the pair to adjudication with the children listed, and
raises the review priority.

### 5 · The deterministic ladder

First rung that fires decides. The rung id travels with the row as `decision_path`.

| Rung | Condition | Result |
|---|---|---|
| `D0` | object id is not a pullable code | reject, `ST-005` |
| `D1` | unique identical primary labels | `exactMatch` 0.99, `EV-001` |
| `D2` | unique identical after non-semantic normalisation | `exactMatch` 0.97, `EV-002` |
| `D3` | unique hit crossing into an exact synonym | `exactMatch` 0.92, `EV-003` |
| `D4` | unique hit after one declared rewrite | `exactMatch`, `EV-004` |
| `D5` | a D1–D4 hit **blocked** by a scope flag | proposal only → adjudication |
| `D6` | two or more objects tie at the winning tier | → adjudication, `PR-008` |
| `D7` | retrieval-only candidates above the viability floor | → adjudication |
| `D8` | retrieval returned token-overlap noise only | `NoTermFound` + `RJ-004` |

The ladder's defining property is what it refuses to do: **it never downgrades a blocked hit to
a weaker predicate on its own authority.** A lexical match against "Other porphyria" is real
evidence about strings and no evidence about extension. Emitting `broadMatch` there would look
like progress and would put an unreviewed guess in the file.

### 6 · Agentic adjudication

Everything unsettled, highest priority first, in batches, through the Anthropic API with
structured output. Three commitments, all about not trusting the model:

- **It cannot invent an identifier.** Responses are validated against the ids that were
  offered. A hallucinated id is retried once in isolation, then routed to expert review with
  `RJ-002` — never repaired or guessed at.
- **It cannot skip the rules.** The registry is in the system prompt and the schema requires
  ids from it. An unknown rule id is dropped and the drop is noted in the justification.
- **Its output is a proposal.** Every agent row carries `semapv:CompositeMatching` and is
  Tier 3 under the Mondo Mapping Curation Guide.

Responses are cached by item content plus model id, so a rerun costs nothing and a changed
candidate pool invalidates exactly the items whose pool changed.

### 6b · Consistency conditioning (Boomer)

Everything above scores a row **locally**: one subject, its candidates, its flags. What that
cannot see is that accepting this row makes another impossible — that two Mondo terms cannot
both be exactly `A30.1`, or that a mapping into a child code contradicts a mapping into its
parent.

[Boomer](https://github.com/INCATools/boomer) can. Every asserted mapping goes in as a
*hypothesis* with the pipeline's confidence as its prior; both sources' subclass axioms over
the mapped signature go in as *hard constraints*; Boomer partitions the hypotheses into
cliques and returns the most probable assignment that is actually consistent.

Two columns come back, and their definitions are worth reading carefully because "prior" and
"posterior" are easy to print and easy to misread:

`boomer_prior`
: The probability this pipeline put on the relation it asserted, before Boomer saw it —
  exactly the ptable cell that went in. Per mapping, exact.

`boomer_posterior`
: That same relation's probability after conditioning, as a **max-marginal**. For each
  relation the mapping could take, the score of the best consistent assignment of its whole
  clique under that choice, normalised across the choices.

A max-marginal is not a sum-marginal: it asks "how much mass does the best world containing
this claim carry, against the best worlds containing its rivals", not "summed over all
worlds". Boomer 0.2 reports MAP assignments and subsequent solution *scores*, not
per-hypothesis sums, so the max-marginal is what its output actually supports — and calling it
a marginal posterior without that qualification would overstate it.

Mechanically, the posterior takes a second Boomer pass. For each mapping and each relation it
has prior mass on, a renamed copy of its clique pins that choice; all copies go into one
invocation, because Boomer partitions by connectivity and renamed copies are disconnected by
construction. The pin's own mass is then subtracted back out in log space and the real prior
added in its place, so the pin acts purely as a constraint:

```
score(r) = clique_score(variant) − log PIN_MASS + log prior(m = r)
```

The consequence is also the test that the arithmetic is right: **for a mapping nothing
constrains, every completion scores the same and the posterior comes out equal to the prior.**
On the reference run that holds exactly for all 1,956 unconstrained mappings.

Where the two diverge, the row is a finding. `boomer_conflict` — the probability consistency
took off our claim — is a triage component in its own right, weighted 0.15.

Failure is not fatal: a missing binary, a timeout or an unsolvable input degrades to empty
Boomer columns and a recorded reason. Conditioning is additional evidence, not a prerequisite.

### 7 · Triage

Set-level checks first, because they are invisible row by row: reverse proxy merges (`ST-002` —
two subjects claiming the same code as an exact match), proxy merges (`ST-001`), and
conflations the run did not declare.

Then a tier per subject:

| Tier | Meaning |
|---|---|
| `AUTO_ACCEPT` | strong deterministic rung, no flag, no ambiguity, no conflation |
| `SPOT_CHECK` | defensible, but weaker rung, single agent verdict, or a permitted conflation |
| `EXPERT_REVIEW` | an adjudicator looked and a human has to decide |
| `QUEUED` | nobody has looked yet; the adjudication budget did not reach it |
| `REJECTED` | actively decided against, with a rule |
| `NO_MATCH` | recorded absence — `sssom:NoTermFound` + `RJ-001` / `RJ-004` |

`AUTO_ACCEPT` means "no expert hour would change this", **not** "curated".

`QUEUED` and `EXPERT_REVIEW` are separate, and keeping them separate is what makes the table
worth reading. With them collapsed, the first full Mondo run reported 89% `EXPERT_REVIEW`,
which was true of neither the rows nor any curator's actual worklist. Separated, the same run
reports 726 rows needing a human — 2.3% — and 23,538 waiting for budget.

And a `review_priority` in [0,1] — a published weighted sum, not a learned score, so a curator
can read why a row is near the top:

| Component | Weight | Rationale |
|---|---|---|
| ladder/agent disagreement | 0.22 | two independent methods conflict; highest-yield review |
| agent uncertainty | 0.17 | the model says it is unsure |
| Boomer conflict | 0.15 | consistency took probability off the relation we asserted |
| `possibly_coextensive` | 0.13 | the registry's hardest judgement |
| ambiguity | 0.10 | `PR-008`: assert nothing, ask |
| structural defect | 0.10 | set-level corruption a curator must resolve |
| subject impact | 0.07 | a wrong grouping mapping propagates to descendants |
| declared conflation | 0.06 | a conceptual-model boundary was crossed |

## Output

| File | Contents |
|---|---|
| `mappings.sssom.tsv` | the mapping set; non-standard columns declared in `extension_definitions` |
| `system_table.tsv` | every subject, ranked by `review_priority` — the curation queue |
| `records.jsonl` | full audit trail: every candidate, signal, flag and verdict |
| `alignment.duckdb` | both tables, queryable |
| `report.md` | counts by tier, rules cited, evaluation, top of the queue |
| `disagreements.tsv` | where the run contradicts the source's own cross-references |
| `run.yaml` | config plus source checksums and timings |

Rules are cited **by CURIE, not by prose**. `curation_rule` is a URI-valued SSSOM slot, so a
row says

```
curation_rule    maprule:EV-002|maprule:SC-001|maprule:PR-002
```

and the reader resolves it against the published value set. That row states three things a
bare `skos:narrowMatch` cannot: the labels agreed after normalisation, the object turned out to
be a residual rubric, and the predicate was chosen because the object is the narrower concept.
The value set is generated from `src/mondo_ai/align/rules.py` by `just align-valueset` into
three shapes — `vocabulary/maprule.ttl` (SKOS, so the CURIEs resolve),
`vocabulary/maprule.yaml` (a LinkML enum) and [`docs/maprule.md`](maprule.md) — and a test
fails if any of them drifts from the registry the code cites.

Non-standard SSSOM columns are **declared**, not smuggled: `signals`, `decision_path`,
`review_tier`, `review_priority`, `ambiguity_count`, `risk_flags`, `agent_confidence`,
`agent_agreement`, `agent_alternatives`, `conflation_rules`, `deterministic_confidence`,
`expert_question`, `mapping_tier`, `structural_flags`, `verdict_source`, and the Boomer set
`boomer_prior`, `boomer_posterior`, `boomer_predicate`, `boomer_agrees`,
`boomer_constrained`, `boomer_clique_size`, `boomer_clique_confidence`, `boomer_method`. An
undeclared column is a column the next tool silently drops.

The set-level `comment` carries the review protocol: source versions and checksums, the rules
in force, the use case, and every conflation the set knowingly permits.

## Evaluation

Mondo asserts its own ICD-10-CM cross-references with precision qualifiers
(`MONDO:equivalentTo`, `MONDO:mondoIsBroaderThanSource`, …). Those are **held out entirely** —
not seeded as anchors, not used as proxy evidence — and then used as a gold standard.

Scope is the whole design of the evaluation. Mondo cross-references only a small fraction of
its terms to ICD-10-CM, and the absence of a cross-reference is not a claim that no code
exists — usually it means nobody has curated that term against ICD-10-CM yet. So a subject is
**in scope** only if it carries at least one qualified cross-reference to the object
vocabulary. Predictions outside that scope are recorded as `novel_mapping` candidate additions
and are not folded into precision in either direction. (Scoring them as errors was the first
thing this pipeline did, and it reported 0.20 precision of which every single "error" was a
plausible new mapping.)

Reported per slice, per review tier and per rung, so "`AUTO_ACCEPT` is safe to ship" is a
measured claim rather than a design intention. Plus a predicate confusion table and the
disagreements, which are a curation finding in their own right:

| kind | meaning |
|---|---|
| `novel_mapping` | out of scope; a candidate addition to the source |
| `different_object` | both assert an exact match, and they disagree |
| `declined_by_source` | the source cross-referenced this term and declined to call it equivalent — the strongest contradiction, because somebody looked |
| `missed` | an asserted exact match this pipeline did not find |

## Measured on the reference run

Mondo `2026-09-01` (32,109 active terms) × ICD-10-CM `2024ab` from mondo-ingest (97,903
codes), agent budget 1,200 items on `claude-opus-5`:

| | |
|---|---|
| deterministic stages | ~80s (load 4s, index 14s, candidates + ladder 64s) |
| agent stage | 339s, 1,200 items at concurrency 12, 0 validation failures |
| Boomer | 11s — 2,602 hypotheses, 249 interacting cliques, two passes |
| cost | $8.27 for 1,200 agent items — $0.0069 per item |
| **exactMatch precision / recall / F1** | **0.987 / 0.845 / 0.910** in scope |
| `AUTO_ACCEPT` precision | **0.989** (462 TP, 5 FP) |
| needs a human (`EXPERT_REVIEW`) | 731 rows, 2.3% |
| waiting for budget (`QUEUED`) | 23,538 rows — finishing costs ~$162 |
| novel mapping candidates | 2,061 |
| reverse proxy merges found (`ST-002`) | 419 |
| **mappings overruled by consistency** | **231**, largest clique 6 |

Recall is limited by the agent budget, not by the method: 90 of the 96 in-scope misses sit in
`QUEUED`. The worklist breaks down as 457 structural defects, 221 ladder/agent disagreements
and 48 `SC-007`-vs-`SC-001` judgements.

Two findings worth naming, because they show the rules earning their keep:

- Mondo carries explicitly **veterinary** terms — "polydactyly, domestic cat", "persistent
  right aortic arch, puma". ICD-10-CM is a human classification, so the adjudicator cited
  `CM-008` (cross-species analog) 596 times and rejected almost all of them. Correct, and
  invisible to any matcher without a conflation vocabulary.
- 419 **reverse proxy merges** (`ST-002`): two Mondo terms claiming one ICD code as an exact
  match. `A30.1` is claimed by both *tuberculoid leprosy* and *paucibacillary leprosy*;
  `A48.0` by both *gas gangrene* and *infection due to Clostridium perfringens*. No single row
  shows this, which is why the check is set-level and every subject involved is escalated.
- Consistency conditioning caught `MONDO:0009050 Cushing disease due to pituitary adenoma`
  shipped as an `exactMatch` to `ICD10CM:E24 Cushing's syndrome` at confidence 0.88 — posterior
  0.017, reassigned `broadMatch`. That is the `SC-006` example the rule registry is written
  around, and the local scorers had accepted it. Also `Q05 Spina bifida` claimed exactly by
  *spina bifida cystica*, *myelomeningocele* **and** *meningocele*, and `E23.0 Hypopituitarism`
  by six Mondo terms.

## Cost

The agent stage is the only cost. `agent.max_items` caps it, highest priority first, and
everything beyond the cap is queued as `EXPERT_REVIEW` with the reason recorded — never as a
silent no-match. The report states the tokens spent, the cost per item, and the extrapolated
cost of finishing the remaining queue, so raising the cap is an informed decision.

## Seeded pairs: reconciliation (Mondo × SNOMED CT)

Some pairs are not a cold start: mapping sets already exist, and the deliverable is every pair
anyone has proposed, each with its evidence. Mondo × SNOMED CT has two such seeds, Mondo's own
SCTID xrefs and OMOP's Mondo `Maps to` rows.

```bash
just align-snomed-prepare     # latest Mondo + mondo.sssom.tsv, SNOMED from the OMOP zip, OMOP seed
just align-snomed             # deterministic: discovery, then reconciliation
just align-reconciled         # consolidated pairs by status x tier
just align-reconciled-status predicate_conflict
```

**Seeds never enter discovery.** The ladder runs with both held out, so its evaluation stays
honest and a string identity that backs a seed pair is independent of it. Seeds meet the run
in a reconciliation stage afterwards (`reconcile:` in the config), which recomputes each
pair's lexical evidence on the discovery code path and gives it a status:
`corroborated`, `seed_only`, `discovery_only`, or one of the conflicts — `predicate_conflict`,
`object_conflict` (`ST-001`), `reverse_proxy_merge` (`ST-002`), `rule_conflict`,
`predicate_vs_lexical`, `consistency_conflict` (Boomer) — or a dead end: `inactive_target`
(retired in SNOMED, successor named), `inactive_subject`, `unknown_target`.

**Two seeds agreeing is not corroboration.** OMOP's Mondo mappings repeat 69% of Mondo's own
pairs verbatim; only lexical evidence or an adjudicator corroborates.

Two extra files: `consolidated.sssom.tsv`, one row per pair, with lexmatch-style
`match_string` / `subject_match_field` / `object_match_field` / `subject_preprocessing`; and
`evidence.sssom.tsv`, one row per pair per line of evidence, each with its own
`mapping_justification` and `mapping_source`. Design and first-run numbers:
`specs/2026-11-06-mondo-snomed-alignment-design.md`.

SNOMED CT is licensed. Its labels are in these outputs, which stay under the gitignored
`alignments/` directory.

## Adding a new pair

1. Copy `conf/align/mondo-icd10cm.yaml` and change the two resource blocks.
2. State the `use_case` and list `conflations_permitted`. A `CM-*` the adjudicator invokes that
   is not on that list escalates the row to `EXPERT_REVIEW`.
3. Add a download recipe to `align.just`.
4. `just align-run conf/align/<new>.yaml` with `--limit 300 --max-agent-items 20` first.
5. Then run it whole.
