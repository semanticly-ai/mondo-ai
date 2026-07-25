## Project-specific just recipes for mondo-ai

# ============== Configuration ==============

schema_path := "src/mondo_ai/schema/mondo_ai_sieve.yaml"
oak_config := "conf/oak_config.yaml"
evidence_packets_dir := "evidence_packets"
data_dir := "data"
snapshots_dir := "data/snapshots"

# OWL file URLs
doid_url := "http://purl.obolibrary.org/obo/doid.owl"
ordo_url := "http://www.orphadata.org/data/ORDO/ordo_orphanet.owl"
mondo_url := "http://purl.obolibrary.org/obo/mondo.owl"
# OMIM requires a license key, use local mondo-ingest mirror
omim_mirror := "/Users/matentzn/ws/mondo-ingest/src/ontology/tmp/mirror-omim.owl"
# SSSOM mapping file from mondo-ingest
sssom_mirror := "/Users/matentzn/ws/mondo-ingest/src/ontology/tmp/mondo.sssom.tsv"

# ============== Acquisition recipes ==============

# Create data directories
[group('acquisition')]
init-data:
    mkdir -p {{data_dir}}/owl
    mkdir -p {{snapshots_dir}}

# Download DOID OWL file
[group('acquisition')]
download-doid: init-data
    @echo "Downloading DOID..."
    curl -L -o {{data_dir}}/owl/doid.owl "{{doid_url}}"
    @echo "Checksum: $(shasum -a 256 {{data_dir}}/owl/doid.owl | cut -d' ' -f1)"

# Download ORDO OWL file
[group('acquisition')]
download-ordo: init-data
    @echo "Downloading ORDO..."
    curl -L -o {{data_dir}}/owl/ordo.owl "{{ordo_url}}"
    @echo "Checksum: $(shasum -a 256 {{data_dir}}/owl/ordo.owl | cut -d' ' -f1)"

# Download Mondo OWL file
[group('acquisition')]
download-mondo: init-data
    @echo "Downloading Mondo..."
    curl -L -o {{data_dir}}/owl/mondo.owl "{{mondo_url}}"
    @echo "Checksum: $(shasum -a 256 {{data_dir}}/owl/mondo.owl | cut -d' ' -f1)"

# Copy OMIM from mondo-ingest mirror (requires local mondo-ingest)
[group('acquisition')]
copy-omim: init-data
    @echo "Copying OMIM from mondo-ingest mirror..."
    @if [ -f "{{omim_mirror}}" ]; then \
        cp "{{omim_mirror}}" {{data_dir}}/owl/omim.owl; \
        echo "Checksum: $$(shasum -a 256 {{data_dir}}/owl/omim.owl | cut -d' ' -f1)"; \
    else \
        echo "Error: OMIM mirror not found at {{omim_mirror}}"; \
        echo "Please run mondo-ingest first or update the path"; \
        exit 1; \
    fi

# Copy SSSOM mapping file from mondo-ingest
[group('acquisition')]
copy-sssom: init-data
    @echo "Copying SSSOM mapping file..."
    @if [ -f "{{sssom_mirror}}" ]; then \
        cp "{{sssom_mirror}}" {{data_dir}}/mondo.sssom.tsv; \
        echo "Copied: {{data_dir}}/mondo.sssom.tsv"; \
        wc -l {{data_dir}}/mondo.sssom.tsv; \
    else \
        echo "Error: SSSOM file not found at {{sssom_mirror}}"; \
        echo "Please run mondo-ingest first or update the path"; \
        exit 1; \
    fi

# Download all OWL files and SSSOM
[group('acquisition')]
download-all: download-doid download-ordo download-mondo copy-omim copy-sssom
    @echo "All OWL files and SSSOM downloaded"

# List downloaded OWL files with checksums
[group('acquisition')]
list-owl:
    @echo "Downloaded OWL files:"
    @ls -lh {{data_dir}}/owl/*.owl 2>/dev/null || echo "No OWL files found"
    @echo ""
    @echo "Checksums:"
    @shasum -a 256 {{data_dir}}/owl/*.owl 2>/dev/null || true

# ============== Extraction recipes ==============

# Extract snapshot from DOID
[group('extraction')]
extract-doid:
    uv run mondo-ai extract-owl {{data_dir}}/owl/doid.owl -r doid -o {{snapshots_dir}}/doid_snapshot.yaml --iri-filter "DOID_"

# Extract snapshot from ORDO
[group('extraction')]
extract-ordo:
    uv run mondo-ai extract-owl {{data_dir}}/owl/ordo.owl -r ordo -o {{snapshots_dir}}/ordo_snapshot.yaml --iri-filter "Orphanet_"

# Extract snapshot from OMIM
[group('extraction')]
extract-omim:
    uv run mondo-ai extract-owl {{data_dir}}/owl/omim.owl -r omim -o {{snapshots_dir}}/omim_snapshot.yaml --iri-filter "omim.org/entry"

# Extract snapshot from Mondo
[group('extraction')]
extract-mondo:
    uv run mondo-ai extract-owl {{data_dir}}/owl/mondo.owl -r mondo -o {{snapshots_dir}}/mondo_snapshot.yaml --iri-filter "MONDO_"

# Extract all snapshots
[group('extraction')]
extract-all: extract-doid extract-ordo extract-omim extract-mondo
    @echo "All snapshots extracted"

# List extracted snapshots
[group('extraction')]
list-snapshots:
    @echo "Extracted snapshots:"
    @ls -lh {{snapshots_dir}}/*.yaml 2>/dev/null || echo "No snapshots found"

# ============== Validation recipes ==============

# Validate all evidence packets against the schema
[group('validation')]
validate-schema file="":
    @if [ -n "{{file}}" ]; then \
        uv run linkml-validate --schema {{schema_path}} --target-class EvidencePacket {{file}}; \
    else \
        for f in {{evidence_packets_dir}}/pkt_*.yaml; do \
            echo "Validating $$f..."; \
            uv run linkml-validate --schema {{schema_path}} --target-class EvidencePacket "$$f"; \
        done; \
    fi

# Validate reference citations in evidence packets using linkml-reference-validator
[group('validation')]
validate-references file="":
    @if [ -n "{{file}}" ]; then \
        uv run linkml-reference-validator validate data {{file}} --schema {{schema_path}} --target-class EvidencePacket; \
    else \
        for f in {{evidence_packets_dir}}/pkt_*.yaml; do \
            echo "Validating references in $$f..."; \
            uv run linkml-reference-validator validate data "$$f" --schema {{schema_path}} --target-class EvidencePacket; \
        done; \
    fi

# Cache a reference by PMID for faster validation
[group('validation')]
cache-reference pmid:
    uv run linkml-reference-validator cache reference {{pmid}}

# Validate ontology term references in evidence packets using linkml-term-validator
[group('validation')]
validate-terms file="":
    @if [ -n "{{file}}" ]; then \
        uv run linkml-term-validator validate-data {{file}} -s {{schema_path}} -t EvidencePacket --labels -c {{oak_config}}; \
    else \
        for f in {{evidence_packets_dir}}/pkt_*.yaml; do \
            echo "Validating terms in $$f..."; \
            uv run linkml-term-validator validate-data "$$f" -s {{schema_path}} -t EvidencePacket --labels -c {{oak_config}}; \
        done; \
    fi

# Validate schema term references (prefixes, etc.)
[group('validation')]
validate-schema-terms:
    uv run linkml-term-validator validate-schema {{schema_path}}

# Run all validations on evidence packets
[group('validation')]
validate-all: validate-schema validate-terms
    @echo "All validations complete"

# ============== Packet Building recipes ==============

# Build evidence packets from a single snapshot
[group('packets')]
build-packets-doid:
    uv run mondo-ai build-packets {{snapshots_dir}}/doid_snapshot.yaml --sssom {{data_dir}}/mondo.sssom.tsv --source-id doid --source-name "Disease Ontology" -o {{evidence_packets_dir}}

[group('packets')]
build-packets-ordo:
    uv run mondo-ai build-packets {{snapshots_dir}}/ordo_snapshot.yaml --sssom {{data_dir}}/mondo.sssom.tsv --source-id ordo --source-name "Orphanet" -o {{evidence_packets_dir}}

[group('packets')]
build-packets-omim:
    uv run mondo-ai build-packets {{snapshots_dir}}/omim_snapshot.yaml --sssom {{data_dir}}/mondo.sssom.tsv --source-id omim --source-name "OMIM" -o {{evidence_packets_dir}}

# Build packets from all sources and merge
[group('packets')]
build-packets-all:
    uv run mondo-ai build-packets-multi \
        --snapshot {{snapshots_dir}}/doid_snapshot.yaml --source-id doid --source-name "Disease Ontology" \
        --snapshot {{snapshots_dir}}/ordo_snapshot.yaml --source-id ordo --source-name "Orphanet" \
        --snapshot {{snapshots_dir}}/omim_snapshot.yaml --source-id omim --source-name "OMIM" \
        --sssom {{data_dir}}/mondo.sssom.tsv \
        -o {{evidence_packets_dir}}

# List generated evidence packets
[group('packets')]
list-packets:
    @echo "Generated evidence packets:"
    @ls -lh {{evidence_packets_dir}}/pkt_*.yaml 2>/dev/null | head -20 || echo "No packets found"
    @echo ""
    @echo "Total packets: $$(ls {{evidence_packets_dir}}/pkt_*.yaml 2>/dev/null | wc -l || echo 0)"

# ============== Scoring & Enrichment recipes ==============

# Score all evidence packets
[group('scoring')]
score-packets:
    uv run mondo-ai score {{evidence_packets_dir}}

# Score packets with custom thresholds
[group('scoring')]
score-packets-strict:
    uv run mondo-ai score {{evidence_packets_dir}} --accept 0.8 --reject -0.2

# Enrich packets with PubMed literature (all packets)
[group('enrichment')]
enrich-packets:
    uv run mondo-ai enrich {{evidence_packets_dir}} --max-papers 3

# Enrich only low-scoring packets
[group('enrichment')]
enrich-low-score:
    uv run mondo-ai enrich {{evidence_packets_dir}} --max-papers 3 --min-score 0.5

# Run AI synthesis on packets
[group('synthesis')]
synthesize-packets limit="10":
    uv run mondo-ai synthesize {{evidence_packets_dir}} --limit {{limit}}

# Run AI synthesis with OpenAI
[group('synthesis')]
synthesize-openai limit="10":
    uv run mondo-ai synthesize {{evidence_packets_dir}} --provider openai --limit {{limit}}

# ============== Storage & Index recipes ==============

# Build packet index from evidence_packets directory
[group('storage')]
build-index:
    uv run mondo-ai index {{evidence_packets_dir}}

# Show index statistics
[group('storage')]
index-stats:
    uv run mondo-ai stats

# Query packets by score range
[group('storage')]
query-high-score:
    uv run mondo-ai query --min-score 0.9 --limit 20

# Query packets needing review
[group('storage')]
query-review:
    uv run mondo-ai query --min-score 0.3 --max-score 0.7 --limit 20

# ============== Pipeline recipes ==============

# Run the full pipeline to generate evidence packets
[group('pipeline')]
run-pipeline:
    uv run python run_full_pipeline.py

# Generate pydantic models from LinkML schema (SIEVE-based)
[group('pipeline')]
gen-pydantic:
    uv run gen-pydantic {{schema_path}} > src/mondo_ai/datamodel/sieve_models.py

# Migrate existing evidence packets to SIEVE format
[group('pipeline')]
migrate-packets:
    uv run python scripts/migrate_to_sieve.py {{evidence_packets_dir}}/

# ============== Web Application recipes ==============

# Serve the web application (development)
[group('web')]
serve-web:
    uv run uvicorn mondo_ai.web.app:app --reload --host 0.0.0.0 --port 8000

# Serve the web application (production)
[group('web')]
serve-web-prod:
    uv run uvicorn mondo_ai.web.app:app --host 0.0.0.0 --port 8000
