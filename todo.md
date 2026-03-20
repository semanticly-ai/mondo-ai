Update system design:

- [x] mappingJustification in ConcordanceItem now uses the `mapping_set_id` from SSSOM metadata (e.g., `http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv`)
- [x] In config.yaml individual content_weights for each content type in each source. For example, `exact_synonym: 0.95` in omim.
- [x] If a jsonl file increases beyond 50MB it is ok (soft limit) - but throws error if size exceeds 90MB (hard limit)
- [x] Removed "predicate_weights" from scoring config - weights are now per-source in content_weights

Incremental pipeline support:

- [x] Extract versionIRI from OWL ontologies for ResourceSnapshot provenance
- [x] Added `has_literature_evidence()` function to detect already-enriched packets
- [x] Updated `PacketEnricher` with `skip_enriched` parameter (default True)
- [x] Added `--force` flag to CLI `enrich` command to override incremental behavior
- [x] Added tests for incremental enrichment behavior

Mondo evidence types:

- [x] Created `MondoExtractor` class for Mondo-specific extraction with provenance
- [x] Added `AgentContribution` for organizational decision (Mondo consortium presence)
  - Uses `TrustLevel.authority` and `ContributionType.decision`
  - Agent is "mondo:consortium" (organization subtype)
- [x] Added `AgentContribution` for expert determination (ORCID in source)
  - Uses `TrustLevel.curator` and `ContributionType.decision`
  - Agent ID is the ORCID (person subtype)
- [x] Added `SieveDocument` with candidate subtype for PMIDs in source
  - Uses `subtype` Coding with `code="candidate_reference"`
  - Weak strength (0.4) indicates unverified status
- [x] Added functions to extract ORCIDs and PMIDs from annotation sources
- [x] Added tests for all Mondo evidence extraction (23 tests)

### Next things to tackle

- Investigate low subclass axiom counts: DOID has 16,564 subClassOf packets but should have many more. Check if `_extract_hierarchy` is filtering too aggressively or if the IRI filter / SSSOM mapping coverage is limiting results.
- Figure out if there is a way to score evidence based on some rational metrics about the journal involved? Maybe.. Impact factor? Any other idea?