# Port mondo-ai to the SIEVE SEPIO profile and rebuild the packet stores

*Date: 2026-11-20*

## Problem

mondo-ai vendors the pre-profile SIEVE model (`ConcordanceItem`, `AgentContribution`,
`SieveDocument`, `EvidenceSynthesis`, `CurationStatus`, NER score). sieve (commit
`afc3a69`) replaced it with a profile of the canonical SEPIO core: one claim-type
statement per packet, nested evidence, an `InterpretationActivity` on every line, a
self-contained `reference_snapshot`, and the inclusion score S. The packet stores were
built from stale sources and cover synonyms (OMIM, ORDO, DOID) and DOID subclass only.

## Decisions

- **Schema.** Vendor `sepio_core.yaml` and `sieve.yaml` from sieve verbatim; a test
  compares them with `../sieve/schema/` when present. `mondo_ai_sieve.yaml` imports
  `sieve` and keeps only mondo-ai's extraction classes (`ResourceSnapshot`,
  `EntitySnapshot`, `Synonym`, `EntityMapping`, `SynonymScope`). Dropped:
  `TextMiningResult` (now `TextPassage`), `StatementPredicate` (now the claim types and
  their predicate value sets), `RecommendedAction` (an AI does not decide; it adds a line).
- **Packets are dicts.** Validating a nested profile packet through the generated
  Pydantic model costs 13-190 ms (34-way unions), hours at 200k packets. Builders, store,
  stats, scorer, web and synthesis work on the JSON dict, as sieve's own
  `profile_checks` and `inclusion_score` do. The generated model stays for typed access
  and is used to validate samples.
- **Reuse sieve, don't copy it.** `sieve` is already an editable dependency. Scoring is
  `sieve.inclusion_score.score_packet`; validation is `sieve.profile_checks.check_packet`
  plus `linkml-validate` on samples.
- **Sources** (2026-11-20): OMIM monarch release 2026-11-18 (OFN, converted to RDF/XML
  with ROBOT), ORDO 4.9, DOID 2026-10-29, ICD10CM from the Mondo sources PURL (2024ab),
  Mondo and `mondo.sssom.tsv` from their PURLs. Converted with a rewritten bulk-query
  `OWLExtractor` into `ResourceSnapshot` YAML. The extractor now also reads xrefs
  (`oboInOwl:hasDbXref`, `skos:*Match`, with ORDO's ECC precision codes), synonym types,
  and, for ORDO, `part_of` (BFO:0000050) classification parents.

## Packet shape

One packet per claim. The claim is in Mondo space; the evidence is the sources' own
claims in their own ids.

| Modality | Claim | Evidence line per source |
|---|---|---|
| synonym | `SynonymStatement` MONDO:x scope 'text' | prior `SynonymStatement` (source id, same scope) |
| subclass | `SubclassStatement` MONDO:x subClassOf MONDO:y | prior `SubclassStatement` (source child, source parent) |
| mapping | `MappingStatement` MONDO:x exactMatch/broadMatch/narrowMatch S:y | the `ExternalRecord` S:y (supports if live, disputes if obsolete); lexical match `ComputationalScore`; prior `MappingStatement`s from other sources' xrefs that agree |

- A prior claim is `reported_in` the source `ExternalRecord` (label, release, retrieval
  date) and carries `resolution_chains`: one `ResolutionChain` per source id resolved to Mondo,
  with the `mondo.sssom.tsv` exactMatch row it used (mapping set and version).
- Each line: `RULE_BASED_STRENGTH`, `computed_by_rule` = the claim type's
  `AggregationRule`, contributor = the mondo-ai concordance pipeline agent,
  `read_coverage: STRUCTURED_RECORD`, `independence_cluster` = the source resource,
  `score_of_evidence_provided` = the source's per-content weight from config.
- `generation_context` records the basis honestly: ORDO ECC code, synonym type
  (`mondo#GENERATED_FROM_LABEL` on ICD10CM), `part_of` for ORDO parents, untyped
  `hasDbXref` for DOID.
- `review_state`: NOT_VERIFIED if the claim is already in Mondo, PENDING_REVIEW if it is
  a candidate. `proposing_channel`: SOURCE_INGEST. No `assertion_decisions` (no human
  signed anything).
- `statement.score/strength/direction` = S, its band and sign, from the frozen rule.
- `reference_snapshot` = the registry entries the packet references (resources with
  quality ratings and `derives_content_from`, releases, agent, method, rule).
- Ids: `mondoai:pkt/<12 hex>` from subject/predicate/object; statement, line and prior
  claim ids derive from it. `ExternalRecord` ids are the source CURIE.
- **Raw store** (`packet_store/`): one packet per claim and source, id suffixed with the
  source. **Merged store** (`packet_store_merged/`): one packet per claim with all
  sources' lines.
- Upstream curators (ORCIDs on source axioms) are not recorded (D-055). PMIDs on source
  synonym axioms become `Publication`s in the prior claim's `reported_in`.

## Consumers

- **Enrichment** attaches PubMed hits as `has_evidence` (direct, uninterpreted).
- **Synthesis** adds one higher-order `LLM_JUDGEMENT` line: items = copies of the lines
  it read plus the `Publication`s, direction and strength from the model, `confidence`
  with basis MEASURED, `rationale`, `read_coverage: ABSTRACT_ONLY`. Re-scored after.
- **Store** writes in bulk (index and manifest saved once per batch, not per packet).
  Self-contained packets average ~4 KB (the reference snapshot is ~1.5 KB), so plain
  JSONL came to 1.1 GB for both stores. Files are gzip instead: each a series of members
  of 1,000 packets, indexed by (file, member offset, member length, line). Both stores
  together are 69 MB, and any gzip reader reads a file as JSONL.
- **Mondo provenance**: with `mondo.owl`, the ORCIDs and PMIDs on Mondo's own axioms
  are attached to claims already in Mondo, as `statement.contributions` (role CURATOR;
  not an AssertionDecision, which needs a date and outcome the annotation lacks) and
  `has_evidence` Publications. Mondo writes ORCIDs as `https://orcid.org/` URLs; skos
  triples carry no annotations, so mappings take the provenance of the matching xref axiom.
- **Publications are not mapping evidence** (the profile's claim-evidence matrix), so
  enrichment, synthesis and Mondo PMIDs skip MappingStatements.
- **Validation** is closed JSON Schema (LinkML) plus sieve's profile checks. An error
  inside a statement can be reported against the last claim type in the union.
- **Stats, index, web, validator, CLI** read the profile shape.
- `scripts/migrate_to_sieve.py` is deleted (migrates a format that no longer exists).

## Non-goals

- Calibrating rule parameters, weights or priors (illustrative, as in sieve's registry).
- Competing-claim lines (D-069), decisions, policies, batches.
- Definition, existence, attribute claim types.
