# Evidence Pipeline

This page walks through the mondo-ai evidence pipeline, from source ontologies to the
packet stores, using one real packet from `packet_store_merged/`.

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│   Sources    │ → │  Snapshots   │ → │   Packets    │ → │    Score     │ → │    Stores    │
│ OMIM, ORDO,  │   │ (local YAML) │   │ (SIEVE       │   │ (inclusion   │   │ raw + merged │
│ DOID, ICD10CM│   │              │   │  profile)    │   │  score S)    │   │ (gzip JSONL) │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
```

Packets follow **SIEVE**, a profile of the SEPIO core information model, maintained in
the [sieve](https://github.com/matentzn/sieve) repository. mondo-ai vendors its schema
(`src/mondo_ai/schema/sieve.yaml`, `sepio_core.yaml`) and uses its reference scorer and
profile checks.

## The example

> MONDO:0010590 *FG syndrome 1* `skos:exactMatch` Orphanet:93932 *FG syndrome type 1*

The mapping is in Mondo already (`review_state: NOT_VERIFIED`). The packet collects what
the sources say about it and scores it at S = 0.91 (strong).

## Step 1: acquire the sources

```bash
just download-all        # into data/owl/$SOURCE_DATE (default 2026-11-20)
```

| Source | File | Release read on 2026-11-20 |
|---|---|---|
| Disease Ontology | `http://purl.obolibrary.org/obo/doid.owl` | 2026-10-29 |
| Orphanet (ORDO) | `ORDO_en_4.9.owl` from orphadata.com | 4.9 |
| OMIM | monarch-initiative/omim release `omim.owl` | 2026-11-18 |
| ICD-10-CM | `http://purl.obolibrary.org/obo/mondo/sources/icd10cm.owl` | 2024ab |
| Mondo + mappings | `mondo.owl`, `mondo/mappings/mondo.sssom.tsv` | 2026-11-03 |

OMIM ships as OWL functional syntax; `just convert-omim` converts it to RDF/XML with ROBOT.
No OWL file comes from OMIM or CDC themselves: these are the Monarch and Mondo builds.

## Step 2: convert to the local schema

```bash
just extract-all         # data/snapshots/$SOURCE_DATE/<id>.yaml
```

`OWLExtractor` reads each file with pyoxigraph, one SPARQL query per property, into a
`ResourceSnapshot` (`schema/mondo_ai_sieve.yaml`). Each `EntitySnapshot` holds the label,
synonyms with scope, type and PMIDs, the definition, named parents, part_of
classification parents (ORDO), mappings (`oboInOwl:hasDbXref` and `skos:*Match`, with
ORDO's ECC precision code, prefixes normalised, e.g. `MIM:` → `OMIM:`) and deprecation.
Deprecated classes are kept and flagged: an obsolete mapping target is evidence.

| Snapshot | Entities | Synonyms | Parents | Mappings |
|---|---:|---:|---:|---:|
| doid | 14,939 | 23,058 | 17,635 | 41,743 |
| ordo | 16,295 | 29,433 | 25,998 + 16,664 part_of | 79,153 |
| omim | 28,506 | 107,643 | 23,192 | 110,703 |
| icd10cm | 97,903 | 113,818 | 97,902 | 0 |
| mondo | 36,094 | 95,492 | 46,774 | 259,340 |

## Step 3: build the packets

```bash
just build-packets       # or: uv run mondo-ai build-packets --mondo-owl data/owl/2026-11-20/mondo.owl
```

A packet is one Mondo claim. Its evidence is what the sources say, **in their own ids**:
each source statement becomes a *prior claim* (typed by claim type), reported in the
`ExternalRecord` it was read from, and normalized to Mondo ids by a `ResolutionChain` (one NORMALIZATION step) that
freezes the mondo.sssom.tsv exactMatch row it used.

- **Synonyms**: every synonym of a live source record whose id normalizes to a Mondo id.
- **Subclass**: every source parent (and ORDO part_of parent) where both ends normalize to Mondo ids.
- **Mappings**: every Mondo mapping into OMIM, Orphanet, DOID or ICD-10-CM, plus candidate
  mappings that a source's own xrefs imply.

For the example, three lines:

| Line | Items | Direction | s |
|---|---|---|---:|
| lexical | ExternalRecord Orphanet:93932 + ComputationalScore (Mondo exact synonym = ORDO label) | supports | 0.4 |
| ordo | ORDO's own claim Orphanet:93932 exactMatch MONDO:0010590 (ECC `E`) | supports | 0.8 |
| omim | OMIM's claim OMIM:305450 exactMatch Orphanet:93932, OMIM:305450 normalized to MONDO:0010590 | supports | 0.5 |

The ORDO line as stored:

```yaml
id: mondoai:el/0e83f9be54fc-ordo
type: SieveEvidenceLine
has_evidence_items:
- id: mondoai:pc/c6ffbe26e0e1
  type: MappingStatement            # ORDO's own claim, in ORDO's ids
  subject: Orphanet:93932
  predicate: {code: skos:exactMatch, label: exact match}
  object: MONDO:0010590
  resource: infores:orphanet
  generation_context: [{code: "ECC:E", label: ORDO mapping precision}]
  reported_in:
  - id: Orphanet:93932
    type: ExternalRecord
    label: FG syndrome type 1
    record_metadata: {retrieved_from: infores:orphanet, source_release: mondoai:release/ordo-4.9, retrieval_date: "2026-11-20"}
direction_of_evidence_provided: supports
score_of_evidence_provided: 0.8
strength_of_evidence_provided: strong
computed_by_rule: mondoai:rule/mapping-v1.0
independence_cluster: infores:orphanet
contributions:
- type: InterpretationActivity
  contributor: mondoai:agent/concordance-pipeline
  method_type: RULE_BASED_STRENGTH
  specified_by: [mondoai:method/source-concordance]
  read_coverage: STRUCTURED_RECORD
```

A source that gives another precision disputes (ORDO `NTBT` against a Mondo exactMatch),
and an obsolete target record disputes. When `mondo.owl` is given, the curators (ORCIDs)
and PMIDs on Mondo's own axiom are attached to claims already in Mondo, as
`contributions` (role CURATOR) and `has_evidence` Publications.

Every packet carries a `reference_snapshot`: frozen copies of the resources (with quality
rating and `derives_content_from`), releases, agent, methods and the AggregationRule it
references, so it reads the same however the registry changes later.

## Step 4: score

S is sieve's inclusion score (`sieve.inclusion_score`, D-063): per line
w = d · s · q · c; the strongest line per independence cluster counts;
L = L0 + k · Σw; S = tanh(L/2). For the example, L0 = 0, k = 2 and all three lines rest
on 0.9-rated resources (Orphanet for the lexical line), so
L = 2 · (0.4·0.9 + 0.8·0.9 + 0.5·0.9) = 3.06 and S = tanh(1.53) = 0.91. A DOID line on the
same claim as an OMIM line shares OMIM's cluster (DOID derives content from OMIM), so the
echo is not counted twice. `mondo-ai score STORE --explain PACKET_ID` prints the breakdown.

S ranks claims for review. It is not a decision: humans sign off.

## Step 5: enrich and synthesise (optional)

```bash
uv run mondo-ai enrich packet_store_merged --max-papers 3 --claim-type SynonymStatement --limit 50
uv run mondo-ai synthesize packet_store_merged --limit 10
```

Enrichment attaches PubMed hits as `has_evidence` Publications (synonym and subclass
claims only; the profile does not admit Publications as mapping evidence). Synthesis adds
one `LLM_JUDGEMENT` line that nests copies of the lines it read and the Publications,
with the model's direction, strength, confidence (basis MEASURED) and rationale; the
packet is then re-scored.

## Step 6: the stores

| Store | Packets | Content |
|---|---:|---|
| `packet_store/` | 136,397 | one packet per claim and source (`mondoai:pkt/<hash>-<source>`) |
| `packet_store_merged/` | 109,541 | one packet per claim, every source's lines |

| Claim type | Merged packets | In Mondo | Candidates |
|---|---:|---:|---:|
| SynonymStatement | 48,071 | 19,129 | 28,942 |
| SubclassStatement | 28,824 | 22,447 | 6,377 |
| MappingStatement | 32,646 | 30,135 | 2,511 |

Files are `packets_NNN.jsonl.gz`: gzip members of 1,000 packets, readable with
`gzip -dc`, with an index for random access. `stats.md` in each store has the full
breakdown.

```bash
uv run mondo-ai stats packet_store_merged
uv run mondo-ai index packet_store_merged
uv run mondo-ai query --claim-type MappingStatement --review-state PENDING_REVIEW --min-score 0.8
uv run mondo-ai show mondoai:pkt/0e83f9be54fc --store packet_store_merged
uv run mondo-ai validate packet_store_merged --sample 2000
just serve-web
```
