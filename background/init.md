
# MondoAI — Program Specification

## 0) Goal

Build **MondoAI**, an AI-assisted, evidence-driven **sync pipeline** for curating content into the **Mondo disease ontology**, replacing most of the existing `mondo-ingest` (see) sync logic with a cleaner architecture.

Core capabilities:
1. Ingest selected content from external sources (start with **OMIM, ORDO, DOID**) using a provided **SSSOM mapping** to Mondo.
2. Build **Evidence Packets** for candidate statements, with provenance and reliability.
3. Bucket packets into:
   - **NEW**: statement not present in Mondo
   - **CONFIRM**: statement already present in Mondo
4. Run an **agentic workflow** (pydantic-ai) that:
   - scores evidence deterministically,
   - optionally expands evidence via PubMed,
   - synthesizes a curated recommendation with citations,
   - assigns a curation status and stores results in an indexed store.

Focus: the AI agent must act as a **steward of evidence**, not just “an opinion engine”.

---

## 1) Inputs & Outputs

### Inputs
- `mondo.sssom.tsv` — SSSOM mapping file
- `config.yaml` — resources, content types, reliability weights, thresholds
- Resource OWL files: OMIM, ORDO, DOID
- Mondo OWL/OBO

### Outputs
- `evidence_packets/` — canonical packet store (YAML/JSONL)
- `packet_index/` — SQLite or DuckDB index
- `runs/<timestamp>/` — run metadata, summaries, agent artifacts

---

## 2) Architecture Overview

### A) Acquisition
- Download OWL files via `justfile`
- Cache with checksums and versions

### B) Extraction (schema-driven)
- Extract labels, synonyms, definitions, ancestors, mappings
- Produce **ResourceSnapshot** objects (LinkML)
- Use pyoxigraph or equivalent RDF tooling

### C) Mondo Snapshot
- Extract same structure for Mondo

### D) Evidence Packet Builder
- Apply SSSOM mapping → MONDO ID space
- Generate canonical statements
- Merge evidence across sources
- Bucket UNREVIEWED vs APPROVED (or whatever the evidencepaket model says)

### E) Scoring & Agentic Enrichment
- Deterministic scoring
- Optional PubMed enrichment
- AI synthesis using pydantic-ai

### F) Storage & Index
- Persist packets
- Index for fast retrieval

---

## 3) Data Models (LinkML-first)

### ResourceSnapshot
- resource_id
- version
- generated_at
- entities

### CanonicalStatement
- subject_mondo_id
- predicate
- object
- statement_id (hash)

### EvidenceItem, EvidencePacket etc

~/ws/sieve to see how to format evidence packets

---

## 4) Configuration (`config.yaml`)

Defines:
- resources and enabled content
- reliability weights
- scoring thresholds
- PubMed settings
- agent model + prompt version
- storage backend

---

## 5) Pipeline Steps

1. Download data
2. Extract snapshots
3. Read SSSOM mapping
4. Build MONDO-space statements
5. Build & merge evidence packets
6. Score evidence
7. Enrich with PubMed if needed
8. Agent synthesis
9. Persist results

---

## 6) Repo Layout

IMPORTANT: do not CHANGE the current layout, just add stuff.

```
mondoai/
  src/mondoai/
  tests/
  data/
  justfile
  pyproject.toml
```

Feel free to look at:

~/ws/mondo-ingest/src/ontology/ to see how the old pipeline worked
~/ws/dismech to see how to (roughly) design the pubmed evidence gathering
~/ws/sieve to see how to format evidence packets

But be careful these repos are huge and will pollute the context window, so best look at them selectively