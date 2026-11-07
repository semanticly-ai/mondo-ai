# Review mode — an existing SSSOM file in, the same rows out with confidence columns

Status: designed, built and run 2026-11-07. Extends
[the agentic alignment design](2026-09-25-agentic-ontology-alignment-design.md) and reuses
[the SNOMED surface's reconciliation](2026-11-06-mondo-snomed-alignment-design.md); read those
first. Recipe: the agentic-alignment artifact (<https://claude.ai/artifact/FJujQgNSwQoBdkrLLyochv>).
Cross-project record: `~/ws/notes/mapping-strategy/decisions/review-mode-in-mondo-ai.md`.

Run it: `just review-mapping <file>.sssom.tsv [config]` (default config
`conf/align/mondo-snomed.yaml`), `just review-mapping-deterministic <file>` (no model calls).
Code: `src/mondo_ai/align/review.py`; config block `review:` (`ReviewConfig`).
First target: `just review-mondo-snomed-input` writes Mondo's 9,134 SCTID rows to
`data/align/mondo_snomed.sssom.tsv`.

## 1. Problem

Alignment answers "what should this subject map to?". Most mapping sets Monarch holds already
exist — Mondo's xrefs, OMOP's `Maps to`, uPheno's external mappings, anything a curator or a
lexmatch run produced — and the question about them is *how much should we believe each row?*

Requested (Nico, 2026-11-02): a pipeline that takes a candidate SSSOM file and outputs a
reviewed file "which contains the exact same mappings as the inputs, but with various confidence
scores such as `reviewer_confidence`, `boomer_posterior_confidence`", run as
`just review-mapping test.sssom.tsv`; "the hardest part will be to make the boomer pipeline
scalable — probably by drawing relatively small boxes around the hierarchies on both sides."

## 2. Contract

- **Same rows, same order, same cells.** The input is read without interpreting any cell
  (`read_sssom`); review only appends columns. Re-reviewing a reviewed file replaces the review
  columns and nothing else. Rows outside the config's two prefixes, or with a non-mapping
  predicate, come back with `review_status = out_of_scope_prefix | out_of_scope_predicate`.
- **Empty is not zero.** A row no reviewer reached has an empty `reviewer_confidence`; a row
  Boomer could not solve has an empty posterior and `boomer_status = failed: <reason>`.
- **The file stays SSSOM.** Every added column is declared in `extension_definitions`; the
  output round-trips through sssom-py with every row (test).

## 3. Columns

| column | meaning |
|---|---|
| `review_status`, `review_tier` | reconciliation status and tier with the input file as the only seed (SNOMED spec §2.4) |
| `review_rules`, `risk_flags`, `structural_flags`, `review_comment` | rules cited, scope flags, ST-001/ST-002, one sentence |
| `lexical_confidence`, `lexical_match` | strongest string identity, computed on the discovery code path |
| `boomer_prior` | the row's prior for its own predicate: input `confidence` (else `review.default_confidence`, 0.95), lifted to the lexical certainty when that supports it, capped at 0.99 |
| `boomer_posterior_confidence` | max-marginal of the row's own predicate after conditioning (see `boomer.py`) |
| `boomer_predicate`, `boomer_agrees` | Boomer's MAP relation; whether it is the asserted one |
| `boomer_status`, `boomer_box`, `boomer_box_size`, `boomer_box_cut`, `boomer_clique_size` | how the row was solved — `isolated` rows have posterior = prior by construction |
| `reviewer_confidence` | the model's probability that the row *as asserted* (object and predicate) is correct |
| `reviewer_predicate`, `reviewer_rules`, `reviewer_justification`, `reviewer_needs_expert`, `reviewer_model` | what the model thinks holds, and why |

No combined score. Three independent lines and a tier are reviewable; a weighted blend of them
would be one more number to calibrate and would hide which line moved. Left to a later
decision with outcome data (§7).

## 4. Boomer at scale: boxes are cycle components

### 4.1 What can constrain what

Over a background of subclass axioms, Boomer's only constraint is that two classes of one
namespace may not collapse into an equivalence — and a collapse is a **cycle**: up one
hierarchy, across a mapping, along the other hierarchy and back. Give every hypothesis its most
permissive reading (equivalence: an edge both ways; broad and narrow are each one direction of
it) and every subclass axiom of the one-hop background its direction. Any cycle any assignment
could create lies inside one strongly connected component of that graph. Hypotheses in
different components cannot constrain each other, so **solving the components separately is
exact** — not an approximation that happens to work (`review.cycle_components`).

Hierarchies that agree (`s1 ⊑ s2`, `o1 ⊑ o2`) never close a cycle. Only inversions and shared
concepts do, and those are rare, so the components are tiny.

### 4.2 Packing and running

Boxes are renamed apart (`MONDO:1__boxb0007`) and packed into bins of `review.bin_hypotheses`
(1,500) rows: one Boomer invocation per bin plus one max-marginal pass, `review.workers` (6)
bins in parallel. Renamed boxes cannot interact, so a bin is as exact as one box per JVM. A bin
that fails is retried box by box. A component of one hypothesis is `isolated` and not sent to
Boomer at all: nothing can move it, so its posterior is its prior.

### 4.3 Oversized components: cut, with a halo

A component larger than `review.max_box` (150) is cut at its most generic hubs (concepts that
are the nearest mapped ancestor of the most mapped concepts), then chunked if no hub separates
it, and only a cut box is solved with a one-link **halo** of neighbouring hypotheses that is
never read back. Every row records `boomer_box`, `boomer_box_size` and `boomer_box_cut`. Not
triggered on Mondo × SNOMED CT (largest component: 3).

### 4.4 Measured against one global run

Mondo's 9,134 SNOMED CT rows, 8,606 hypotheses (rejected rows are not submitted). Reference:
all hypotheses solved as one box, which is one global Boomer invocation.

| boxes | wall | Boomer calls | posterior \|Δ\| > 0.05 | max \|Δ\| |
|---|---|---|---|---|
| one global box (reference) | 58 s | 2 | — | — |
| **cycle components** (built) | **5.9 s** | 2 | **0 of 8,511** | **0.0000** |
| comparability, hub-cut, `max_box` 150–1000, no halo | 14 s | 8 | 3 | 0.96 |
| comparability, hub-cut, halo depth 1 | 207–211 s | 50–52 | 2 | 0.48 |

8,503 components, 8,409 of them single hypotheses, the largest 3; none of the global run's 94
Boomer cliques is split across components. The 52 rows where the two runs' MAP *predicate*
differs are all exhaustive-search ties — two mappings with equal priors, one of which must
yield — so `boomer_predicate` is arbitrary on a tied clique and the posterior (≈ 0.49 for each,
in both runs) is the number to read. All 8,409 isolated rows kept their prior's argmax in the
global run too.

What was tried first, and why it is not the design. Joining hypotheses along every subclass
edge, and then joining any ancestor/descendant pair of mapped concepts, both percolate: one
component of ~8,600 at every hub limit. Cutting that at hubs lost real inversions
(*encephalomyelitis* above *myelitis* and *encephalitis* in Mondo, below them in SNOMED CT;
*superficial* vs *cutaneous mycosis*), and halos bought them back only partly, as tie artefacts
of a clique seen half, at 15× the cost. The "small boxes around the hierarchies" are right, but
the boxes have to follow cycles, not adjacency.

## 5. The reviewing model

One verdict per row (`ReviewVerdict`): the relation that actually holds, the probability that
the row as asserted is correct, rule ids from the registry, one sentence, `needs_expert`. Its
own prompt (`REVIEW_PROMPT`), not the adjudication one: adjudication is told "the expected
answer is often none", which is the wrong prior for a curated set. The payload carries the
subject, the object (with parent, children, synonyms), scope flags and the string match — and
deliberately not the pipeline's own status or tier, which a reviewer tends to agree with.

Validation as in the adjudicator: unknown relation or out-of-range confidence fails the batch,
which is retried item by item; an item that still fails gets no verdict. Unknown rule ids are
dropped and noted. Responses are cached on the rendered item plus model id.

Budget: `review.max_items` (400) rows, most contested first — `EXPERT_REVIEW`, then
`SPOT_CHECK`, then `AUTO_ACCEPT`; within a tier, Boomer disagreement first, then lowest
posterior. `REJECTED` rows are not reviewed. The report states spend, cost per row and the cost
of reviewing the rest.

## 6. Results, first run

`just review-mapping data/align/mondo_snomed.sssom.tsv`, 2026-11-07: Mondo 2026-11, SNOMED CT
via OMOP (SNOMED spec §2.6). 9,134 rows in and out, no input cell changed, 23 columns added;
sssom-py parses all 9,134 rows (it ignores the extension columns, as for the alignment output).
Report: `alignments/review/mondo_snomed/review_report.md`.

| status | tier | rows |
|---|---|---|
| corroborated | AUTO_ACCEPT | 6,465 |
| seed_only | SPOT_CHECK | 1,245 |
| corroborated | SPOT_CHECK | 548 |
| inactive_target | REJECTED | 383 |
| rule_conflict | EXPERT_REVIEW | 178 |
| object_conflict | EXPERT_REVIEW | 153 |
| inactive_subject | REJECTED | 144 |
| consistency_conflict | EXPERT_REVIEW | 15 |
| other | | 3 |

- **Boomer**: 7 s. 197 hypotheses in interacting components, 94 overruled, 86 with a posterior
  more than 0.05 below the prior, 15 clean rows escalated to `consistency_conflict`.
- **Reviewer**: `claude-opus-5`, 400 rows, 138 s, **$4.74 ($0.012 per row)**; reviewing the
  remaining 8,206 would cost about $97. Of the 400 (the most contested): 321 agree with the
  asserted predicate, 36 say the object is narrower, 29 broader, 12 close/related, 2 no match.
  The least believed are recognisable errors — *trachea leiomyoma* → "Neoplasm of trachea",
  *esophageal disorder* → "Ulcer of esophagus", *primary ovarian failure* → "Primary
  hypogonadism".
- **Lines of evidence agree where they overlap.** *Trachea leiomyoma*, *primary ovarian
  failure*, *neoplasm of mature B-cells*: Boomer posterior 0.36, reviewer 0.02.

## 7. Not done, deliberately

- **No combined confidence** (§3).
- **No calibration of `reviewer_confidence`.** It is the model's stated probability. Calibrating
  it needs curator outcomes on a sample, which is the same open question the parent spec has for
  the 726 expert-review rows.
- **Cut boxes are approximate.** Only a cycle component above `max_box` is cut, and what the
  cut gives up is marked per row (`boomer_box_cut`), not recovered. A set whose hierarchies
  disagree wholesale would show it here first.
- **Ties are not broken.** On a tied clique `boomer_predicate` is whichever solution Boomer
  found first; the posterior is the stable number (§4.4).
- **SNOMED labels** come from the licensed extract and stay under the gitignored `alignments/`.
