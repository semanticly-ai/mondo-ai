---
name: align-ontologies
description: Use when asked to align, map or crosswalk two resources into an SSSOM mapping set — ontologies, terminologies, code lists, or TSV/YAML concept files. Triggers on "/align-ontologies", "align X with Y", "map Mondo to ICD-10-CM", "build a crosswalk between", "produce SSSOM mappings for". For reviewing a mapping set that already exists, use review-sssom-mappings instead.
---

# Aligning two resources into a reviewable mapping set

## What this produces

A run of this pipeline produces **five artefacts, not a report**:

| File | What it is |
|---|---|
| `mappings.sssom.tsv` | the mapping set; non-standard columns declared in `extension_definitions` |
| `system_table.tsv` | every subject, ranked by `review_priority` — the curation queue |
| `records.jsonl` | full audit trail: every candidate, signal, flag and verdict |
| `report.md` | counts by tier, rules cited, evaluation, top of the queue |
| `run.yaml` | config plus source checksums, so the run can be reproduced |

The deliverable is those files. A prose summary is secondary and never a substitute.

**Your job is the last mile.** The pipeline is a program: it does the matching, the rule
citation and the triage. What it cannot do is read the top of the queue and tell the curator
what the open questions *are*. That is what you add.

## Run it

Everything deterministic is a `just` recipe. Do not reimplement any of it in Python.

```bash
just align-download                      # both sources, with checksums
just align-icd10cm                       # the reference run: Mondo x ICD-10-CM
just align-icd10cm-deterministic         # no model calls, no cost, fully reproducible
just align-smoke 300                     # 300 subjects, 20 agent items — seconds
just align-report                        # read the run report
just align-tiers                         # row counts by review tier
just align-queue mondo-icd10cm 30        # top 30 rows needing an expert
just align-explain MONDO:0009654         # why did this subject come out that way
just align-eval                          # re-score against held-out asserted mappings
just align-validate                      # confirm the set still parses as SSSOM
just align-rules SC                      # the citable curation rule registry
just align-valueset                      # regenerate the published maprule value set
just align-boomer-conflicts              # rows consistency conditioning argues against
just align-test                          # tests + doctests for the align package
```

`just align-sql "select ... from system_table"` for anything the recipes do not cover.

### Mondo × SNOMED CT (seeded)

```bash
just align-snomed-prepare                # latest Mondo + its SSSOM, SNOMED from the OMOP zip, OMOP seed
just align-snomed                        # deterministic: discovery, then reconciliation with the seeds
just align-reconciled                    # consolidated pairs by reconciliation status x tier
just align-reconciled-status predicate_conflict   # the pairs behind one status
just align-snomed-agent                  # optional: agent adjudication (a cost decision)
```

Seeded surfaces add a **reconciliation** stage (`reconcile:` in the config). Seeds are never
inputs to discovery, so evaluation stays honest and a string identity backing a seed pair is
independent. Two extra artefacts: `consolidated.sssom.tsv` (one row per pair anyone proposed,
with `reconciliation_status`, `evidence_sources`, `seed_predicates` and lexmatch-style
`match_string` / `subject_match_field` / `object_match_field`) and `evidence.sssom.tsv` (one
row per pair per line of evidence). Report the consolidated set by status, and never count
two seeds agreeing as corroboration — OMOP's Mondo mappings repeat Mondo's own xrefs.

## Aligning a new pair

1. **Write a config** in `conf/align/<subject>-<object>.yaml`. Copy
   `conf/align/mondo-icd10cm.yaml` and change the two resource blocks. Formats: `obo`,
   `obojson`, `owl` (needs `iri_base`), `sssom`, `tsv`, `yaml`. Format is **declared, never
   sniffed** — guessing wrong on a 100 MB file silently half-loads the index.
2. **Declare the use case and the conflations.** `use_case` and `conflations_permitted` are
   not decoration: a mapping decision is only meaningful relative to the rules it was made
   under, and a set that crosses the disease/phenotype boundary without saying so is not
   reviewable however good its rows. Any `CM-*` the adjudicator invokes that is *not* in
   `conflations_permitted` escalates that row to `EXPERT_REVIEW`.
3. **Add a download recipe** to `align.just` if the source is not already there. Nothing
   inside the Python fetches implicitly.
4. **Smoke it first**: `just align-run conf/align/<new>.yaml` with `--limit 300
   --max-agent-items 20`. Check `report.md` before spending a full budget.
5. **Then run it whole.**

## Reading the output — what to tell the curator

Read `report.md` and the top of `system_table.tsv`. Then report, in this order:

1. **How much needs a human.** The `EXPERT_REVIEW` count and what share of the set that is.
2. **Whether the rest is safe.** `AUTO_ACCEPT` precision from the evaluation section. If that
   number is not there, say so — it is the only evidence that the auto-accepted rows can ship.
3. **The actual open questions.** Group the top of the queue by `top_priority_reason` and
   describe the *kinds* of decision waiting, with two or three named examples each. Not a row
   dump: a curator wants "there are 40 cases where a residual ICD rubric may in fact be
   coextensive with the Mondo term (`SC-007` vs `SC-001`); here are three", not 40 lines.
4. **Where consistency disagrees**, from the report's Boomer section. A row whose
   `boomer_posterior` collapsed below its `boomer_prior` is a mapping the pipeline was
   confident about and the constraints reject — name two or three with their numbers.
5. **Disagreements with the source's own xrefs**, from `disagreements.tsv`. A
   `declined_by_source` row is the strongest finding in the file — the source looked at that
   pair and declined to call it equivalent, and the pipeline disagreed anyway.

Never present `NO_MATCH` counts as failure. Mondo is dominated by gene-level and numbered
OMIM subtypes for which ICD-10-CM has no counterpart; a recorded absence with `RJ-001` is the
correct output there, and the alternative — a plausible wrong code — pulls the wrong patient
cohort.

## What the tiers mean

| Tier | Meaning | What to do |
|---|---|---|
| `AUTO_ACCEPT` | settled by a strong deterministic rung, no flag, no ambiguity, no conflation | ship it |
| `SPOT_CHECK` | defensible, but resting on a weaker rung, a single agent verdict, or a permitted conflation | sample it |
| `EXPERT_REVIEW` | an adjudicator looked and **a human decides** | work the queue in priority order |
| `QUEUED` | nobody has looked yet; the budget did not reach it | raise `agent.max_items` to convert these into verdicts |
| `REJECTED` | actively decided against, with a rule | a finding; keep the row |
| `NO_MATCH` | recorded absence, `sssom:NoTermFound` + `RJ-001`/`RJ-004` | distinguishes "none exists" from "nobody looked" |

**Never report `QUEUED` as work needing an expert.** It is the absence of a verdict, not a
question anyone has formed. Report the two numbers separately: "N rows need a human; M are
waiting for adjudication budget, which costs roughly $X to clear" — the report states that
figure.

Every row is **Tier 3** under the Mondo Mapping Curation Guide: AI-curated, precise, not
reviewed by the Mondo core team. `AUTO_ACCEPT` means "no expert hour would change this", not
"curated". Do not describe the output as curated mappings.

## Cost, and the one decision that is the user's

The agent stage is the only cost in the pipeline. `agent.max_items` caps it, **highest
priority first**; everything beyond the cap is queued as `EXPERT_REVIEW` with the reason
recorded, never as a silent no-match.

A full Mondo × ICD-10-CM run leaves roughly 20,000 unsettled items. At `claude-opus-5` that is
a materially expensive run and takes hours. The shipped config caps it well below that on
purpose. **Raising `max_items` to 0 (no cap), or changing `agent.model`, is the user's
decision — ask, with the estimate, rather than deciding for them.** Responses are cached by
item content, so a rerun after a cap increase only pays for the new items.

If the user wants coverage cheaply, the honest options are, in order: raise `max_items` on the
same model; lower `agent.effort`; or move to `claude-sonnet-5`. Say which you used.

## How it decides, in one screen

Seven stages. 1–5 and 7 are deterministic; 6 is not and is labelled separately in the output.

**Deterministic ladder.** First rung that fires decides, and the rung id is emitted as
`decision_path`:

| Rung | Condition | Result |
|---|---|---|
| `D0` | object id is a chapter range or foreign-shaped | reject, `ST-005` |
| `D1` | unique identical primary labels | `exactMatch` 0.99, `EV-001` |
| `D2` | unique identical after non-semantic normalisation | `exactMatch` 0.97, `EV-002` |
| `D3` | unique hit crossing into an exact synonym | `exactMatch` 0.92, `EV-003` |
| `D4` | unique hit after one declared meaning-preserving rewrite | `exactMatch`, `EV-004` |
| `D5` | a D1–D4 hit **blocked** by a scope flag | proposal only → adjudication |
| `D6` | two or more objects tie at the winning tier | → adjudication, `PR-008` |
| `D7` | retrieval-only candidates above the viability floor | → adjudication |
| `D8` | retrieval returned token-overlap noise only | `NoTermFound` + `RJ-004` |

**The ladder never downgrades a blocked hit on its own authority.** A lexical match against
"Other porphyria" is real evidence about strings and none about extension, so it goes to a
stage that can reason about the rubric's children. Emitting `broadMatch` there would look
like progress and would put an unreviewed guess in the file.

**Consistency conditioning (Boomer).** Everything the ladder and the agent do is *local* — one
subject, its candidates, its flags. Boomer sees the set: each asserted mapping becomes a
hypothesis with our confidence as its prior, both sources' subclass axioms become hard
constraints, and it returns the most probable *consistent* assignment.

| Column | Means |
|---|---|
| `boomer_prior` | the probability we put on the relation we asserted, before Boomer saw it |
| `boomer_posterior` | that relation's **max-marginal** after conditioning on consistency |
| `boomer_predicate` / `boomer_agrees` | what Boomer chose instead, and whether it agreed |
| `boomer_constrained` | whether anything in the set constrains this row at all |

Read them as a pair. They are **equal by definition** for a mapping nothing constrains, so a
row where the posterior sits far below the prior is the most specific finding in the file: the
pipeline was confident and the constraints would not allow it. `just align-boomer-conflicts`
lists them. An **empty** posterior means Boomer did not condition that row — not a posterior of
zero, and never to be reported as one.

Two things not to say about these columns: a max-marginal is not a sum-marginal (Boomer 0.2
reports MAP assignments and solution scores, not per-hypothesis sums), and Boomer being absent
or timing out is not an alignment failure — the columns are simply empty and the report says
why.

**`review_priority`** is a published weighted sum, not a learned score — ladder/agent
disagreement 0.22, agent uncertainty 0.17, Boomer conflict 0.15, `possibly_coextensive` 0.13,
ambiguity 0.10, structural defect 0.10, subject impact 0.07, declared conflation 0.06. Every
row carries its own component breakdown and `top_priority_reason`, so a curator who disagrees
with the ranking can see exactly which term put a row where it is.

## The rules are the reasoning

Rows cite rules **by CURIE, in the URI-valued `curation_rule` slot**, pipe-separated:

```
curation_rule    maprule:EV-002|maprule:SC-001|maprule:PR-002
```

That says three things a bare `skos:narrowMatch` cannot. Never put rule prose in a row — the
titles live once, in the value set generated by `just align-valueset` into
`vocabulary/maprule.ttl` (SKOS), `vocabulary/maprule.yaml` (LinkML enum) and `docs/maprule.md`.
**If you add or change a rule in `rules.py`, run `just align-valueset`**; a test fails
otherwise. The prose form survives in exactly one place, the set-level review protocol in the
SSSOM header, because that paragraph is read once by a human rather than once per row.

The families: `EV` evidence, `SC` scope, `PR` predicate choice, `ST` structural integrity,
`RJ` rejection grounds, `CM` conceptual-model conflation — the same registry the
`review-sssom-mappings` skill reviews against.

Three you will meet constantly:

- **`SC-001` / `SC-002`** — residual rubrics ("Other X", "X, unspecified"). Never an
  `exactMatch`, even when the subject disease is listed under one as an inclusion term.
- **`SC-007`** — the exception, and the hardest judgement in the registry: a rubric whose own
  subdivisions are all subtypes of the subject *is* coextensive with it, so `exactMatch` is
  right even though the label says "unspecified". `E76.21 Morquio mucopolysaccharidoses`
  divides only into Morquio A, Morquio B and unspecified, and is therefore exactly MPS IV.
  The pipeline never auto-decides this; it routes the pair to adjudication with the rubric's
  children listed and raises the priority.
- **`PR-008`** — ambiguity is not a match. Two equally defensible candidates means assert
  nothing.

## Extending it

- **A new rule** goes in `src/mondo_ai/align/rules.py`, then `just align-valueset` to
  regenerate the published SKOS/LinkML/docs artefacts. A rule the code cites but the value set
  does not publish is an unresolvable URI in a URI-valued slot.
- **A new scope or conflation detector** goes in `src/mondo_ai/align/flags.py`, returns a rule
  id from the registry, and gets a test in `tests/align/test_flags_and_ladder.py` naming that
  rule. A detector that returns a private label instead of a registry id is a regression: the
  flag becomes uncitable.
- **A new lexical rewrite** goes in `src/mondo_ai/align/normalize.py` with an entry in
  `RULE_PREDICATE` and `RULE_CERTAINTY`. Only `skos:exactMatch` rewrites may back `EV-004`;
  scope-changing rewrites live in `broad_variants` and widen the candidate pool only.
- **A new resource format** is an adapter in `src/mondo_ai/align/adapters/` plus one line in
  `ADAPTERS`, and a real fixture file in `tests/align/conftest.py` — not a mock.
- Tests first, per the repo's `CLAUDE.md`. Doctests are documentation and tests both; run
  `just align-test`.

## Common mistakes

- **Reimplementing a stage in an ad-hoc script** instead of adding a recipe or a config.
- **Reading `report.md` and stopping.** The queue is the point; read the top of it.
- **Calling the output curated.** It is Tier 3, AI-curated, unreviewed.
- **Spending the agent budget before smoking the config.** `just align-smoke` first.
- **Treating low coverage as a bug.** It is a property of the vocabulary pair.
- **Turning off `evaluation.hold_out`.** That feeds the gold standard in as proxy evidence and
  makes every reported number meaningless. It exists for production runs only, and a run with
  it off must never have its precision quoted.

## Reference

- `specs/2026-09-25-agentic-ontology-alignment-design.md` — the design and its rationale.
- `just align-rules` — the rule registry as the pipeline sees it.
- Background: the OBO Academy guide on
  [whether two entities are the same](https://oboacademy.github.io/obook/howto/are-two-entities-the-same/),
  and the Mondo Mapping Curation Guide's four-tier policy in `background/`.
