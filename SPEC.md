# mondo-ai — System Specification

Status: as-built, reflecting the repository after the port to the SIEVE SEPIO profile
(design: `specs/2026-11-20-sieve-profile-port-design.md`). This document mirrors what the
code does today. Gaps are listed in [Known limitations](#known-limitations).

## 0. Scope of this document

This specifies the **evidence pipeline**. The repository also contains an independent
**ontology alignment** subsystem (`src/mondo_ai/align/`, `just align-*`,
`/align-ontologies`), specified separately in
[`specs/2026-09-25-agentic-ontology-alignment-design.md`](specs/2026-09-25-agentic-ontology-alignment-design.md)
and documented in [`docs/alignment.md`](docs/alignment.md). The two share the repository,
the tooling and the `mondo-ai` CLI, and nothing else.

## 1. Purpose

mondo-ai collects the evidence that source ontologies give for or against Mondo claims.
It reads what OMIM, Orphanet, the Disease Ontology and ICD-10-CM say (synonyms, parents,
mappings), normalizes the ids in those statements to Mondo ids through mondo.sssom.tsv, and packages
each Mondo claim with that evidence, its provenance and an inclusion score as an
**evidence packet** for curator review. Literature enrichment and AI synthesis add
further evidence.

Packets follow **SIEVE**, a profile of the SEPIO core information model, owned by the
sibling [`../sieve`](../sieve) repository. mondo-ai vendors its schema verbatim and uses
its reference scorer and profile checks (sieve is an editable path dependency).

## 2. Technology stack

- **Python** ≥ 3.10, < 4.0 (pinned to 3.11 via `.python-version`)
- **uv** for dependency management, **just** as the command runner
- **LinkML** + `linkml-runtime` for the schema; sieve's codegen generates the models
- **sieve** for the inclusion score (`sieve.inclusion_score`) and profile checks (`sieve.profile_checks`)
- **pyoxigraph** for RDF/OWL parsing; **ROBOT** (ODK docker image) to convert OMIM's OWL functional syntax
- **Typer** for the CLI; **FastAPI** + **Jinja2** + **uvicorn** for the web browser
- **pydantic-ai** for AI synthesis (Anthropic or OpenAI)
- **SQLite** (stdlib) for the queryable packet index
- **pytest** / **mypy** / **ruff** for testing, typing, and linting

## 3. Package layout

```
src/mondo_ai/
├── schema/          sieve.yaml + sepio_core.yaml (vendored from sieve) and the mondo_ai_sieve.yaml overlay
├── datamodel/       Generated Pydantic models; packet loaders
├── config.py        PipelineConfig: resources, weights, rule parameters, enrichment, agent
├── extraction/      OWL → ResourceSnapshot (bulk SPARQL); snapshot I/O; Mondo axiom provenance
├── mapping/         SSSOM reader (normalizing source ids to Mondo ids)
├── packets/         Packet builder, evidence constructors, registry, validation, Mondo provenance
├── scoring/         Inclusion score S (wraps sieve.inclusion_score)
├── enrichment/      PubMed literature attached as direct evidence
├── agents/          AI synthesis: one LLM_JUDGEMENT higher-order line per packet
├── validation/      Packet / store validation reports
├── storage/         Gzip JSONL packet store (block-compressed, sealed files) + SQLite index
├── stats.py         Aggregate statistics + markdown dashboard
├── pipeline.py      PipelineRunner: snapshots → raw and merged stores
├── cli.py           Typer CLI
└── web/             FastAPI browser over a store; static export for the docs site
```

## 4. Data model

`schema/sieve.yaml` and `schema/sepio_core.yaml` are byte-identical to sieve's
(`tests/test_datamodel_contract.py` compares them when `../sieve` is checked out).
`schema/mondo_ai_sieve.yaml` imports `sieve` and adds only the local form of a source
ontology: `ResourceSnapshot`, `EntitySnapshot` (label, synonyms with scope/type/PMIDs,
definition, parents, classification parents, mappings with precision, deprecation),
`Synonym`, `EntityMapping`. `just gen-pydantic` regenerates
`datamodel/sieve_models.py` with sieve's codegen.

**Packets are plain dicts** in the shape of the profile, everywhere (builder, store,
scorer, stats, web). Validating a nested profile packet through the generated Pydantic
model costs tens of milliseconds; `datamodel.packet_model` does it for single packets.

### Packet shape

```
EvidencePacket  id mondoai:pkt/<12 hex> (raw store: ...-<source>), version (build date)
├── statement: SynonymStatement | SubclassStatement | MappingStatement
│   ├── subject (MONDO), predicate {code, label}, object (text or CURIE), statement_text
│   ├── review_state      NOT_VERIFIED (already in Mondo) | PENDING_REVIEW (candidate)
│   ├── proposing_channel SOURCE_INGEST
│   ├── score / strength / direction    inclusion score S, its band and sign
│   ├── has_evidence_lines: [SieveEvidenceLine]
│   │   ├── has_evidence_items      nested in full:
│   │   │     prior claim (the source's own SynonymStatement / SubclassStatement /
│   │   │       MappingStatement in its own ids) reported_in its ExternalRecord, with
│   │   │       resolution_chains (ResolutionChain, one NORMALIZATION step via the mondo.sssom.tsv exactMatch row)
│   │   │     ExternalRecord, ComputationalScore (lexical match), Publication,
│   │   │     or other lines (the AI synthesis line)
│   │   ├── direction_of_evidence_provided, score_of_evidence_provided, strength
│   │   ├── computed_by_rule, independence_cluster, date_attached, last_confirmed(_against)
│   │   └── contributions: [InterpretationActivity]   who interpreted it, how, when
│   ├── has_evidence      Publications attached without interpretation (enrichment, Mondo PMIDs)
│   └── contributions     curators (ORCIDs) on Mondo's own axiom, role CURATOR
└── reference_snapshot: SieveRegistry   frozen resources, releases, agents, methods, rules
```

## 5. Pipeline

1. **Acquire** (`just download-all`): the latest DOID, ORDO, OMIM (monarch-initiative/omim
   release), ICD-10-CM (Mondo sources PURL), Mondo and mondo.sssom.tsv into
   `data/owl/<date>/`; OMIM is converted to RDF/XML with ROBOT.
2. **Extract** (`just extract-all`): `OWLExtractor` reads each file with one SPARQL query
   per property into a `ResourceSnapshot` (`data/snapshots/<date>/<id>.yaml`), keeping
   deprecated classes flagged.
3. **Build** (`just build-packets` / `run_full_pipeline.py`): `PacketBuilder` normalizes every
   source id through Mondo's exactMatch mappings and builds three modalities:
   - **synonyms**: each live source record's synonyms (scope = predicate), except one equal
     to the Mondo label; case variants merge; PMIDs on the source axiom become Publications.
   - **subclass**: each source parent (ORDO also its part_of classification parents)
     where both ends normalize to Mondo ids; in Mondo if Mondo entails it.
   - **mappings**: every Mondo mapping into a configured source, plus candidates implied by
     a source's own xrefs (not where Mondo maps the target exactly elsewhere). Evidence:
     the object record (obsolete: disputes), a lexical match score, and each source's xref
     or skos match, supporting at the same precision and disputing at another (ORDO ECC
     codes; untyped DOID xrefs read as exact).
   Lines from a resource that derives content from another resource with a line on the
   same claim share that resource's independence cluster (DOID → OMIM).
4. **Provenance**: with `mondo.owl`, the ORCIDs and PMIDs on Mondo's own axioms are
   attached to claims already in Mondo (`packets.mondo_provenance`).
5. **Score, validate, store**: S on every packet; a sample is checked against the LinkML
   schema and sieve's profile checks; both stores are replaced, each with `stats.md`.

## 6. Scoring, enrichment and synthesis

**Scoring** (`scoring/scorer.py`) is sieve's reference implementation (D-063). Per valid
line w = d · s · q · c (direction, line strength, resource quality or interpreter trust,
confidence); the strongest line per independence cluster counts; L = L0 + k · Σw;
S = tanh(L/2) ∈ [-1, 1]. Parameters (k, bands, default quality, confidence floor, L0 per
channel) live in one AggregationRule per claim type, built from `ScoringConfig` and frozen
in each packet. Line strengths come from each resource's `ContentWeights`. S is not a
decision: nothing in mondo-ai accepts or rejects a claim.

**Enrichment** (`enrichment/`) attaches PubMed hits as Publications in
`statement.has_evidence` (not for mappings: the profile does not permit Publications as
mapping evidence).

**Synthesis** (`agents/synthesis.py`) adds one higher-order `LLM_JUDGEMENT` line: copies
of the lines it read plus the Publications, the model's direction and strength,
`confidence` with basis MEASURED, a rationale, `read_coverage`. The packet is re-scored.

## 7. Storage

**JSONL store** (`storage/jsonl_store.py`): packets as JSON lines in
`packets_NNN.jsonl.gz`, each file a series of gzip members of up to 1,000 packets.
`index.json` maps packet id → (file, member offset, member length, line), so a lookup
decompresses one block; any gzip reader reads a file as plain JSONL. A file is sealed at
50 MB compressed. `manifest.json` tracks files.

**SQLite index** (`storage/index.py`): `PacketIndex.build_from_store` indexes claim type,
subject, predicate, object, review state, score, strength, direction, line count and
sources for filtering.

## 8. Configuration

`config.py` / `config.yaml`: `ResourceConfig` per source (url, infores id, CURIE prefixes,
IRI filter, quality rating, default role, `derives_content_from`, `ContentWeights`),
`ScoringConfig` (rule parameters), `EnrichmentConfig`, `AgentConfig`, storage settings.
Commands default to `get_default_config()` (OMIM, ORDO, DOID, ICD-10-CM).

## 9. CLI

`mondo-ai <command>`:

| Group | Commands |
|-------|----------|
| Pipeline | `download`, `extract`, `extract-owl`, `build-packets`, `run` |
| Scoring / enrichment / AI | `score`, `enrich`, `synthesize` |
| Index & query | `index`, `query`, `index-stats` |
| Show / export / validate | `show`, `export`, `validate`, `stats` |
| Store | `jsonl-stats`, `jsonl-rebuild-index`, `jsonl-browse` |
| Web / setup | `export-web-data`, `init-config` |
| Alignment | `align …` |

## 10. Web browser

`web/app.py` (`just serve-web`): dashboard, filterable packet list (claim type, review
state, source, S range) and packet detail with every line and item, routed by the part of
the id after `mondoai:pkt/`. `web/static_export.py` writes `docs/evidence/data.json` for the
docs site's evidence browser.

## 11. Testing and tooling

`just test` runs pytest, doctests, mypy and ruff. Tests build packets from small synthetic
snapshots and require every packet to pass sieve's profile checks and the LinkML schema.
Network (PubMed) and LLM tests skip without credentials.

**CI** (`.github/workflows/`). `main.yaml` runs `just test` on Python 3.10 to 3.13 for every
push to main and every PR; `deploy-docs.yaml` exports `docs/evidence/data.json` from the
committed `packet_store_merged/` and deploys the mkdocs site on push to main.

- sieve is an editable path dependency (`../sieve` in `[tool.uv.sources]`), so every
  workflow that runs `uv sync` first uses `.github/actions/setup-sieve`, which checks out
  matentzn/sieve next to the repository at a pinned commit. The pin is the sieve commit the
  vendored `schema/sieve.yaml` is copied from: `tests/test_datamodel_contract.py` compares
  the two byte for byte, so CI fails until the pin is bumped with each re-vendoring.
- The AI workflows (`claude.yml`, `claude-code-review.yml`, `claude-issue-*.yml`,
  `dragon-ai.yml`) run only by `workflow_dispatch`: semanticly-ai/mondo-ai has none of
  their secrets (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `PAT_FOR_PR`). Each file
  keeps its original triggers in a comment. Run with a secret missing, the Claude and
  issue workflows skip with a notice, and Dragon AI's mention check falls back to the
  default token; answering a mention still needs `PAT_FOR_PR`.

## 12. Known limitations

- Rule parameters, source weights and quality ratings are illustrative (as in sieve's
  example registry); calibrating them is the D-063 task.
- No competing-claim lines (D-069): an exact synonym or exact mapping that another Mondo
  term also holds is not yet scored down.
- Packet versions are the build date; a rebuild replaces the stores and does not record
  `previous_version`.
- OMIM and ICD-10-CM are the Monarch/Mondo builds of those sources (no OWL comes from OMIM
  or CDC); ICD-10-CM synonyms typed `mondo#GENERATED_FROM_LABEL` are labels mondo-ingest
  derived, recorded as such in `generation_context`.
- mondo-ai cannot be published to PyPI as is: the wheel declares a bare `sieve`
  dependency, and `sieve` on PyPI is an unrelated package (XML comparison utils), so
  `pip install mondo-ai` would install the wrong one. `pypi-publish.yaml` builds, but the
  result does not install correctly (semanticly-ai/mondo-ai#7).
