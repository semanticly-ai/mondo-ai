# Evidence Pipeline: A Complete Walkthrough

This guide walks through the mondo-ai evidence pipeline from start to finish, using a real example to illustrate how raw ontology data becomes a curated evidence packet with AI synthesis.

## Overview

The mondo-ai pipeline automates the collection and synthesis of evidence for Mondo disease ontology curation. It answers questions like:

- *"Should we add this synonym to Mondo?"*
- *"Is this parent-child relationship correct?"*
- *"What evidence supports or contradicts this assertion?"*

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  OWL Files   │ → │  Extraction  │ → │  Enrichment  │ → │  Synthesis   │
│ OMIM/ORDO/   │    │  & Mapping   │    │   (PubMed)   │    │    (AI)      │
│    DOID      │    │              │    │              │    │              │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                                                                   │
                                                                   ▼
                                                          ┌──────────────┐
                                                          │  Evidence    │
                                                          │   Packets    │
                                                          └──────────────┘
```

---

## The Example: Adding a Synonym to Mondo

Throughout this guide, we'll follow one specific assertion:

> **Statement**: "Reflex sympathetic dystrophy" should be an exact synonym for *complex regional pain syndrome type 1* (MONDO:0011441)

By the end, you'll see how the pipeline:

1. Discovers this synonym in Orphanet (ORDO)
2. Maps it to the correct Mondo term
3. Finds supporting literature in PubMed
4. Uses AI to synthesize a recommendation

---

## Step 1: Extraction from OWL Sources

### Goal
Extract entities (diseases), their synonyms, and hierarchical relationships from external ontologies.

### Tools & Frameworks

| Tool | Purpose |
|------|---------|
| **pyoxigraph** | RDF/OWL parsing and SPARQL queries |
| **curies** | CURIE ↔ IRI conversion via Bioregistry |

### How It Works

The `OWLExtractor` class loads OWL files and runs SPARQL queries to extract:

- **Labels** (`rdfs:label`)
- **Synonyms** (`oboInOwl:hasExactSynonym`, `hasRelatedSynonym`, etc.)
- **Definitions** (`IAO:0000115`)
- **Parent classes** (`rdfs:subClassOf`)
- **Deprecation status** (`owl:deprecated`)

```python
# src/mondo_ai/extraction/owl_extractor.py
from mondo_ai.extraction import OWLExtractor

extractor = OWLExtractor()
snapshot = extractor.extract(
    owl_path="mirror-ordo.owl",
    resource_id="ordo",
    resource_name="Orphanet (ORDO)",
    iri_filter="Orphanet_",      # Only extract Orphanet entities
    skip_deprecated=True,
)
```

### Our Example

From the ORDO OWL file, the extractor finds:

```yaml
Entity:
  id: Orphanet:29073
  label: "Complex regional pain syndrome type 1"
  synonyms:
    - value: "Reflex sympathetic dystrophy"
      scope: EXACT
    - value: "CRPS type I"
      scope: EXACT
```

### Implementation Detail

SPARQL queries are used for efficiency. Here's the synonym extraction query:

```sparql
SELECT ?entity ?synonym ?type WHERE {
    ?entity oboInOwl:hasExactSynonym ?synonym .
    OPTIONAL { ?synonym oboInOwl:hasSynonymType ?type }
}
```

---

## Step 2: Mapping to Mondo IDs

### Goal
Connect external ontology terms (OMIM, ORDO, DOID) to their Mondo equivalents using SSSOM mappings.

### Tools & Frameworks

| Tool | Purpose |
|------|---------|
| **sssom** | SSSOM TSV file parsing |
| **mondo.sssom.tsv** | Official Mondo mapping file |

### How It Works

The `SSSOMReader` loads the Mondo SSSOM file and provides bidirectional lookups:

```python
# src/mondo_ai/mapping/sssom_reader.py
from mondo_ai.mapping import SSSOMReader

sssom = SSSOMReader("mondo.sssom.tsv")

# External → Mondo
mondo_id = sssom.get_mondo_id("Orphanet:29073")
# Returns: "MONDO:0011441"

# Get full mapping with metadata
mapping = sssom.get_mondo_mapping("Orphanet:29073")
# Returns: Mapping(subject_id="MONDO:0011441",
#                  subject_label="complex regional pain syndrome type 1",
#                  predicate_id="skos:exactMatch", ...)
```

### Our Example

The SSSOM lookup reveals:

| External ID | Mondo ID | Mapping Predicate |
|-------------|----------|-------------------|
| Orphanet:29073 | MONDO:0011441 | skos:exactMatch |

This tells us that the synonym found in ORDO applies to Mondo term `MONDO:0011441`.

---

## Step 3: Building the Canonical Statement

### Goal
Create a standardized statement representing the assertion we want to evaluate.

### Data Model

```python
# src/mondo_ai/datamodel/
class CanonicalStatement(BaseModel):
    statement_id: str          # SHA256 hash for deduplication
    subject_mondo_id: str      # The Mondo term (MONDO:0011441)
    subject_label: str         # "complex regional pain syndrome type 1"
    predicate: StatementPredicate  # EXACT_SYNONYM
    object_value: str          # "Reflex sympathetic dystrophy"
    object_id: str | None      # For PARENT predicates: target Mondo ID
```

### Statement Predicates

| Predicate | Use Case |
|-----------|----------|
| `EXACT_SYNONYM` | Synonyms with identical meaning |
| `NARROW_SYNONYM` | More specific terms |
| `BROAD_SYNONYM` | More general terms |
| `RELATED_SYNONYM` | Associated but not equivalent |
| `PARENT` | Hierarchical (subClassOf) |
| `DEFINITION` | Text definitions |

### Our Example

```yaml
statement:
  statement_id: stmt_b00b341a8849
  subject_mondo_id: MONDO:0011441
  subject_label: complex regional pain syndrome type 1
  predicate: EXACT_SYNONYM
  object_value: Reflex sympathetic dystrophy
```

---

## Step 4: Creating Concordance Evidence

### Goal
Create evidence items from cross-ontology agreement (concordance).

### What is Concordance Evidence?

When multiple authoritative sources agree on an assertion, that agreement is evidence. If ORDO says "Reflex sympathetic dystrophy" is an exact synonym, that's concordance evidence from ORDO.

### Data Model

```python
class ConcordanceEvidence(Evidence):
    source_resource: str         # "ordo"
    source_resource_name: str    # "Orphanet (ORDO)"
    source_entity_id: str        # "Orphanet:29073"
    source_entity_label: str     # "Complex regional pain syndrome type 1"
    mapping_predicate: str       # "skos:exactMatch"
    reliability_weight: float    # Source reliability (0.9 for ORDO)
```

### Source Reliability Weights

Different sources have different reliability based on curation quality:

| Source | Weight | Reasoning |
|--------|--------|-----------|
| OMIM | 0.95 | Expert-curated, high quality |
| ORDO | 0.90 | Expert-curated rare disease focus |
| DOID | 0.85 | Community-curated, good coverage |
| MeSH | 0.80 | Vocabulary focus, less disease expertise |

### Our Example

```yaml
evidence:
  - id: ev_ordo_0001
    evidence_type: CONCORDANCE
    direction: SUPPORTS
    evidence_strength: 0.9
    description: "Synonym from Orphanet (ORDO): 'Reflex sympathetic dystrophy'"
    source_resource: ordo
    source_resource_name: Orphanet (ORDO)
    source_entity_id: Orphanet:29073
```

---

## Step 5: Enrichment with Literature

### Goal
Find published research that supports or contradicts the assertion.

### Tools & Frameworks

| Tool | Purpose |
|------|---------|
| **httpx** | Async HTTP client for NCBI E-utilities |
| **NCBI E-utilities** | PubMed search and fetch APIs |

### How It Works

The `PacketEnricher` class:

1. Builds a PubMed search query from the statement
2. Searches for relevant papers
3. Fetches paper metadata (title, abstract, authors)
4. Creates `LiteratureEvidence` items

```python
# src/mondo_ai/enrichment/enricher.py
from mondo_ai.enrichment import PacketEnricher, PubMedClient

client = PubMedClient(cache_dir="references_cache")
enricher = PacketEnricher(pubmed_client=client, max_papers=2)

# Enriches packet with literature evidence
enriched_packet = enricher.enrich(packet)
```

### Search Query Construction

The query combines the disease name with the proposed synonym:

```python
# Generated query for our example:
("complex regional pain syndrome type 1"[Title/Abstract] OR
 "Reflex sympathetic dystrophy"[Title/Abstract])
```

### Reference Caching

Papers are cached as markdown files with YAML frontmatter to avoid repeated API calls:

```markdown
---
pmid: "36397914"
title: "Bilateral Upper and Lower Limb Complex Regional Pain Syndrome..."
authors: ["Singh J", "Naik P", "Patel K"]
journal: "Cureus"
year: 2022
doi: "10.7759/cureus.31120"
---

## Abstract

A 57-year-old male, a survivor of lung cancer...
```

### Our Example

```yaml
- id: ev_lit_0001
  evidence_type: LITERATURE
  direction: SUPPORTS
  evidence_strength: 0.5
  description: "Literature from Cureus: Bilateral Upper and Lower Limb
               Complex Regional Pain Syndrome (CRPS) in a Lung Cancer
               Survivor: Diag..."
  publication_id: "PMID:36397914"
  publication_title: "Bilateral Upper and Lower Limb Complex Regional
                      Pain Syndrome..."
```

---

## Step 6: Scoring with Net Evidence Ratio

### Goal
Calculate a numerical score representing the balance of evidence.

### The NER Formula

```
NER = (S+ - S-) / (S+ + S- + S?)

Where:
  S+ = sum of evidence_strength for SUPPORTS items
  S- = sum of evidence_strength for CONTRADICTS items
  S? = sum of evidence_strength for UNCERTAIN items

Range: -1.0 (all contradicting) to +1.0 (all supporting)
```

### Our Example

| Evidence | Direction | Strength |
|----------|-----------|----------|
| ev_ordo_0001 | SUPPORTS | 0.9 |
| ev_lit_0001 | SUPPORTS | 0.5 |

```
NER = (0.9 + 0.5 - 0) / (0.9 + 0.5 + 0)
    = 1.4 / 1.4
    = 1.0  (maximum support)
```

### Score Interpretation

| Score Range | Interpretation |
|-------------|----------------|
| 0.7 to 1.0 | Strong support → likely ACCEPT |
| 0.3 to 0.7 | Moderate → needs REVIEW |
| -0.3 to 0.3 | Weak/mixed → needs REVIEW |
| -1.0 to -0.3 | Contradicting → likely REJECT |

---

## Step 7: AI Synthesis

### Goal
Generate a human-readable recommendation with reasoning using AI.

### Tools & Frameworks

| Tool | Purpose |
|------|---------|
| **pydantic-ai** | Structured LLM outputs with type safety |
| **Claude** / **GPT-4** | AI model providers |

### How It Works

The `SynthesisAgent` uses pydantic-ai to get structured outputs:

```python
# src/mondo_ai/agents/synthesis.py
from mondo_ai.agents import SynthesisAgent

agent = SynthesisAgent(provider="anthropic")  # or "openai"
synthesis = agent.synthesize(packet)

# synthesis.action: RecommendedAction (ADD/CONFIRM/REJECT/REVIEW)
# synthesis.confidence: float (0-1)
# synthesis.summary: str (reasoning)
# synthesis.cited_evidence: list[str] (evidence IDs used)
```

### The System Prompt

The AI is prompted as an expert disease ontology curator:

```
You are an expert disease ontology curator reviewing evidence for
proposed changes to the Mondo Disease Ontology. Your task is to
analyze the evidence and provide a recommendation.

Consider:
- Type and strength of each evidence item
- Whether evidence supports or contradicts
- Source reliability
- Overall evidence balance
```

### Structured Output

pydantic-ai ensures the response matches our schema:

```python
class SynthesisResponse(BaseModel):
    action: str              # ADD, CONFIRM, REJECT, or REVIEW
    confidence: float        # 0.0 to 1.0
    summary: str             # Reasoning explanation
    key_evidence: list[str]  # Evidence IDs cited
```

### Our Example

```yaml
evidence_synthesis:
  action: ADD
  confidence: 0.85
  summary: >
    The evidence strongly supports adding "Reflex sympathetic dystrophy"
    as an exact synonym for complex regional pain syndrome type 1.
    The primary evidence comes from Orphanet (ORDO), a major disease
    ontology, which provides high-strength concordance support (0.9).
    This is reinforced by literature evidence from a peer-reviewed
    medical publication that uses both terms interchangeably when
    describing CRPS Type I. "Reflex sympathetic dystrophy" is a
    well-established historical term for what is now more commonly
    called Complex Regional Pain Syndrome Type I, making this synonym
    addition both medically accurate and beneficial for users searching
    with older terminology.
  cited_evidence:
    - ev_ordo_0001
    - ev_lit_0001
  model_id: claude-sonnet-4-20250514
  prompt_version: v1.0
  generated_at: "2026-01-07T06:42:49.548301Z"
```

---

## The Final Evidence Packet

After all steps, we have a complete `EvidencePacket`:

```yaml
id: pkt_000001
statement:
  statement_id: stmt_b00b341a8849
  subject_mondo_id: MONDO:0011441
  subject_label: complex regional pain syndrome type 1
  predicate: EXACT_SYNONYM
  object_value: Reflex sympathetic dystrophy
bucket: NEW
provenance:
  generated_at: '2026-01-07'
  source_version: Orphanet (ORDO)
evidence:
  - id: ev_ordo_0001
    evidence_type: CONCORDANCE
    direction: SUPPORTS
    evidence_strength: 0.9
    description: "Synonym from Orphanet (ORDO): 'Reflex sympathetic dystrophy'"
  - id: ev_lit_0001
    evidence_type: LITERATURE
    direction: SUPPORTS
    evidence_strength: 0.5
    description: "Literature from Cureus: Bilateral Upper and Lower Limb..."
evidence_score: 1.0
evidence_synthesis:
  action: ADD
  confidence: 0.85
  summary: "The evidence strongly supports adding..."
  cited_evidence: [ev_ordo_0001, ev_lit_0001]
  model_id: claude-sonnet-4-20250514
status: UNREVIEWED
created_at: '2026-01-07T06:42:39.084131Z'
```

### Packet Structure Explained

| Field | Purpose |
|-------|---------|
| `id` | Unique packet identifier |
| `statement` | The assertion being evaluated |
| `bucket` | NEW (proposed addition) or CONFIRM (verification) |
| `provenance` | Source metadata and generation date |
| `evidence` | List of supporting/contradicting evidence items |
| `evidence_score` | Net Evidence Ratio (-1 to 1) |
| `evidence_synthesis` | AI-generated recommendation |
| `status` | Curation state (UNREVIEWED → ACCEPTED/REJECTED) |

---

## Running the Pipeline

### Quick Start

```bash
# Set up environment variables
export ANTHROPIC_API_KEY=your_key_here
export NCBI_API_KEY=your_ncbi_key  # optional

# Run the pipeline
just run-pipeline
```

### Programmatic Usage

```python
from mondo_ai.pipeline import PipelineRunner, export_packets_yaml
from mondo_ai.enrichment import PacketEnricher, PubMedClient
from mondo_ai.agents import SynthesisAgent

# Step 1: Initialize and run extraction
runner = PipelineRunner(
    sssom_path="mondo.sssom.tsv",
    omim_owl_path="mirror-omim.owl",
    ordo_owl_path="mirror-ordo.owl",
)
packets = runner.run(omim_synonym_count=10, ordo_synonym_count=10)

# Step 2: Enrich with literature
client = PubMedClient(cache_dir="references_cache")
enricher = PacketEnricher(pubmed_client=client, max_papers=2)
for packet in packets:
    enricher.enrich(packet)

# Step 3: AI synthesis
agent = SynthesisAgent(provider="anthropic")
for packet in packets:
    agent.synthesize(packet)

# Step 4: Export
export_packets_yaml(packets, "evidence_packets/")
```

### Validation

```bash
# Validate against LinkML schema
just validate-schema

# Validate ontology term references
just validate-terms

# Validate literature citations
just validate-references
```

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                         mondo-ai Pipeline                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐ │
│  │   OWLExtractor   │   │   SSSOMReader    │   │  StatementBuilder│ │
│  │   (pyoxigraph)   │   │    (sssom)       │   │                  │ │
│  └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘ │
│           │                      │                      │           │
│           └──────────────────────┼──────────────────────┘           │
│                                  │                                  │
│                                  ▼                                  │
│                    ┌─────────────────────────┐                      │
│                    │     PacketBuilder       │                      │
│                    │ (EvidencePacket model)  │                      │
│                    └───────────┬─────────────┘                      │
│                                │                                    │
│           ┌────────────────────┼────────────────────┐               │
│           │                    │                    │               │
│           ▼                    ▼                    ▼               │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐    │
│  │     Scorer       │ │  PacketEnricher  │ │  SynthesisAgent  │    │
│  │ (Net Evidence    │ │   (PubMed via    │ │  (pydantic-ai    │    │
│  │     Ratio)       │ │     httpx)       │ │   + Claude/GPT)  │    │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘    │
│                                                                      │
├─────────────────────────────────────────────────────────────────────┤
│  Key Frameworks:                                                     │
│  • LinkML + Pydantic: Data models and validation                    │
│  • pyoxigraph: RDF/OWL parsing with SPARQL                          │
│  • pydantic-ai: Structured AI outputs                                │
│  • httpx: Async HTTP for PubMed API                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Next Steps for Developers

1. **Explore the schema**: `src/mondo_ai/schema/mondo_ai.yaml`
2. **Run tests**: `just test`
3. **Try the CLI**: `uv run mondo-ai --help`
4. **Add a new evidence source**: Extend `Evidence` base class
5. **Customize AI prompts**: Edit `src/mondo_ai/agents/prompts.py`

For questions or contributions, see the [GitHub repository](https://github.com/monarch-initiative/mondo-ai).
