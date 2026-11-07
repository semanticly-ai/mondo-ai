# Mapping curation rules — `maprule`

A registry of named, citable rules for curating and reviewing mappings.

You can never determine the correctness of a mapping in the abstract — only whether, *under the curation rules declared for the set*, subject and object relate the way the predicate claims. Writing the rules down is what makes a mapping set reviewable rather than merely asserted, and giving each one a stable id is what lets a mapping row cite it in a single cell.

**Namespace:** `maprule:` → `https://w3id.org/mapping-rules/`

In SSSOM, rules are cited in the URI-valued `curation_rule` slot, pipe-separated:

```
curation_rule    maprule:EV-002|maprule:SC-001|maprule:PR-002
```

That row says three things a bare `skos:narrowMatch` cannot: the labels agreed after normalisation, the object turned out to be a residual rubric, and the predicate was chosen because the object is the narrower concept.

## Reading the tables

Two columns carry machine-readable consequences rather than description:

- **blocks** — this rule, once it fires, blocks `skos:exactMatch`. The pipeline never downgrades such a match on its own authority; it routes the pair to adjudication.
- **rejects** — this rule is a terminal reason not to map at all.

## Generated artefacts

| File | For |
| --- | --- |
| `vocabulary/maprule.ttl` | SKOS concept scheme, so the CURIEs resolve to definitions |
| `vocabulary/maprule.yaml` | LinkML enum, to import and validate against |
| `docs/maprule.md` | this page |

All three are generated from `src/mondo_ai/align/rules.py` by `just align-valueset`. A test fails if they drift, so the registry the code cites and the registry the set publishes cannot disagree.

## Family `EV` — Evidence for sameness

What was actually observed that supports the mapping. Records evidence, not a verdict.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `EV-001` | exact match on primary label |  |  | Subject and object primary labels are identical. High confidence, very low cost. |
| `EV-002` | exact match on normalised label |  |  | Identical after non-semantic normalisation only (case, accents, punctuation, whitespace). Equivalent in strength to EV-001. |
| `EV-003` | exact match on exact synonym |  |  | One side's primary label equals the other's exact synonym. Medium-to-high confidence. |
| `EV-004` | match after declared lexical transformation |  |  | Matched only after a documented, bounded rewrite (possessive, word order, British/American spelling, disease/disorder). The rule is recorded in subject_preprocessing. |
| `EV-005` | proxy mapping via third resource |  |  | Both sides already map to the same concept in a trusted third resource. |
| `EV-006` | lexical similarity only |  |  | Low edit distance, or one label contained in the other. Low confidence — never sufficient alone for exactMatch. |
| `EV-007` | synonym similarity |  |  | Non-exact synonym overlap. Low confidence, not sufficient alone. |
| `EV-008` | hierarchical corroboration |  |  | Parents, children or linked features are comparable. Medium confidence. A failing hierarchical comparison is weak evidence against, because primary organising relationships rarely correspond across semantic spaces. |
| `EV-009` | definition comparison |  |  | Textual definitions were compared. Medium-to-high confidence, high cost. |
| `EV-010` | full expert review |  |  | An expert aggregated hierarchy, axioms, definitions and external sources. |
| `EV-011` | external source confirmation |  |  | The mapping is asserted by the source itself, via an explicit exact match. |
| `EV-012` | automated adjudication over retrieved candidates |  |  | Candidates were retrieved lexically and a model judged equivalence. Records the reasoning; does not by itself license a curated assertion. |

## Family `SC` — Scope, granularity and undefined subsets

Where the two concepts differ in extension rather than in kind.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `SC-001` | undefined residual subset ("Other X") | ✓ |  | Object is a semantically undefined remainder class. MUST NOT be an exactMatch, even when the subject disease is listed under it as an inclusion term. |
| `SC-002` | "unspecified" or "excluding X" rubric | ✓ |  | Object is scoped by exclusion; its extension is not stable. MUST NOT be an exactMatch. |
| `SC-003` | "related conditions" grouping | ✓ |  | Object is an open-ended association bucket. MUST be skos:relatedMatch. |
| `SC-004` | disjunctive rubric | ✓ |  | Object names two or more distinct diseases and is broader by construction. Not an exactMatch. |
| `SC-005` | target finer than subject | ✓ |  | Object is a site, laterality, encounter, severity or stage variant. Object is narrower. |
| `SC-006` | subtype under type-agnostic rubric | ✓ |  | Subject is a subtype and the object is the rubric that abstracts over it. Object is broader. Not an exactMatch unless SC-008 is declared. |
| `SC-007` | rubric coextensive with subject |  |  | The object's own subdivisions are all subtypes of the subject concept, so their extensions coincide. exactMatch is correct, including when the rubric's label carries 'unspecified'. Distinguishing this from SC-001 is the hardest judgement in this registry. |
| `SC-008` | granularity tolerance declared |  |  | The use case explicitly permits mapping up to the next best available concept. The predicate MUST still reflect the true relation. |

## Family `PR` — Predicate selection

Which predicate the relation found actually licenses. Direction convention: in `subject predicate object`, `skos:narrowMatch` means the OBJECT is narrower.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `PR-001` | same real-world concept |  |  | Subject and object denote the same disease. |
| `PR-002` | object is a subclass |  |  | No target concept equals the subject; the object is narrower → skos:narrowMatch. |
| `PR-003` | object is a superclass |  |  | No target concept equals the subject; the object is broader → skos:broadMatch. |
| `PR-004` | object is a sibling |  |  | → skos:closeMatch. |
| `PR-005` | conceptually related only |  |  | No exact, broad, narrow or close fit → skos:relatedMatch. Should be rare. |
| `PR-006` | exactMatch is not OWL equivalence |  |  | skos:exactMatch asserts referential sameness for integration purposes, not owl:equivalentClass. |
| `PR-007` | prefer exact or broad |  |  | closeMatch and relatedMatch are hard to use analytically; an exclusion reason MUST be given where the external concept is out of scope. |
| `PR-008` | ambiguity is not a match | ✓ |  | Two or more equally defensible candidates → assert nothing. |

## Family `ST` — Structural integrity

Checks over the set rather than the row. Violations are set-level defects.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `ST-001` | proxy merge |  |  | Two object concepts from one resource both exactMatch a single subject. Disallowed for precise mappings; a core curator must resolve it. |
| `ST-002` | reverse proxy merge |  |  | Two subject concepts both exactMatch a single object id. Disallowed at every tier. |
| `ST-003` | object identifier must exist | ✓ | ✓ | The object id must be a real, current identifier in the target vocabulary at the declared version. |
| `ST-004` | object must not be obsolete | ✓ | ✓ | Deprecated or obsoleted target concepts must not carry exact matches. |
| `ST-005` | identifier is not a code | ✓ | ✓ | The object id is a chapter range or belongs to a different vocabulary than its prefix claims. Invalid regardless of the label's plausibility. |
| `ST-006` | subject must be in scope | ✓ |  | The subject must be a disease, not a phenotypic feature or a gene. |

## Family `RJ` — Rejection grounds

Terminal outcomes. A rejection is a finding and is recorded, not dropped.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `RJ-001` | no candidate found |  | ✓ | Nothing in the target vocabulary denotes this concept. |
| `RJ-002` | ambiguous candidates |  | ✓ | Several defensible candidates, none clearly best. |
| `RJ-003` | homonym |  | ✓ | Labels agree but the concepts differ — the classic failure mode of label-only evidence. |
| `RJ-004` | token overlap only |  | ✓ | Candidate shares words but denotes a different disease. Retrieval noise. |
| `RJ-005` | target out of scope | ✓ | ✓ | The target concept is not a disease under the subject resource's model. |

## Family `CM` — Conceptual model conflation (disease / phenotype)

The two resources hold genuinely different models of what an entity is, and the curator crossed that boundary knowingly. Conflation is a decision, not an error.

| rule | title | blocks | rejects | meaning |
| --- | --- | --- | --- | --- |
| `CM-001` | disease/disorder conflation |  |  | Subject and object are technically distinct as disease vs disorder; conflated as acceptable. |
| `CM-002` | disease/phenotype conflation, murky case |  |  | The object is modelled as a sign, symptom or clinical finding while the subject is a disease, and the distinction is genuinely unclear. Conflated to avoid losing associations. Acceptable. |
| `CM-003` | disease/phenotype distinction clear-cut | ✓ | ✓ | Same shape as CM-002 but the distinction is unambiguous. Rejection ground. |
| `CM-004` | finding-vs-disorder conflation |  |  | Target separates clinical finding from disorder, as SNOMED does. Conflated deliberately. |
| `CM-005` | etiological vs phenomenological divergence |  |  | The same clinical picture is individuated by cause in one resource and by presentation in the other. Conflated where the use case does not distinguish them. |
| `CM-006` | susceptibility vs disease | ✓ | ✓ | Subject is a susceptibility or predisposition locus; object is the clinical disease. Rejection ground — these are not the same entity. |
| `CM-007` | gene-centric vs disease-centric | ✓ | ✓ | One side denotes a gene or molecular defect, the other a disease. Rejection ground. |
| `CM-008` | cross-species analog |  |  | Entities are analogous across organisms rather than identical. Conflate only if the use case explicitly permits it. |
| `CM-009` | injury/event vs inherited condition | ✓ | ✓ | Target rubric denotes an acute event or procedural complication where the subject denotes an inherited trait. Rejection ground. |
