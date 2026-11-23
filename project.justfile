## Project-specific just recipes for mondo-ai

# ============== Configuration ==============

schema_path := "src/mondo_ai/schema/mondo_ai_sieve.yaml"
oak_config := "conf/oak_config.yaml"
evidence_packets_dir := "evidence_packets"
data_dir := "data"

# Sources are downloaded into a dated directory; override with SOURCE_DATE=YYYY-MM-DD
source_date := env_var_or_default("SOURCE_DATE", "2026-11-20")
owl_dir := data_dir / "owl" / source_date
snapshots_dir := data_dir / "snapshots" / source_date

# Source URLs (the latest release behind each PURL)
doid_url := "http://purl.obolibrary.org/obo/doid.owl"
ordo_url := "https://www.orphadata.com/data/ontologies/ordo/last_version/ORDO_en_4.9.owl"
omim_url := "https://github.com/monarch-initiative/omim/releases/latest/download/omim.owl"
icd10cm_url := "http://purl.obolibrary.org/obo/mondo/sources/icd10cm.owl"
mondo_url := "http://purl.obolibrary.org/obo/mondo.owl"
sssom_url := "http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv"
odk_image := "obolibrary/odklite:v1.7"

# ============== Acquisition recipes ==============

# Download every source, Mondo and mondo.sssom.tsv into data/owl/SOURCE_DATE
[group('acquisition')]
download-all:
    mkdir -p {{owl_dir}}
    curl -sSL --retry 3 -o {{owl_dir}}/doid.owl "{{doid_url}}"
    curl -sSL --retry 3 -o {{owl_dir}}/ordo.owl "{{ordo_url}}"
    curl -sSL --retry 3 -o {{owl_dir}}/omim.owl "{{omim_url}}"
    curl -sSL --retry 3 -o {{owl_dir}}/icd10cm.owl "{{icd10cm_url}}"
    curl -sSL --retry 3 -o {{owl_dir}}/mondo.owl "{{mondo_url}}"
    curl -sSL --retry 3 -o {{owl_dir}}/mondo.sssom.tsv "{{sssom_url}}"
    just convert-omim
    just list-owl

# The OMIM release is OWL functional syntax; convert it to RDF/XML for pyoxigraph
[group('acquisition')]
convert-omim:
    docker run --rm -e ROBOT_JAVA_ARGS=-Xmx12G -v "$(pwd)/{{owl_dir}}:/work" -w /work {{odk_image}} robot convert -i omim.owl --format owl -o omim.rdf.owl

# List downloaded OWL files with checksums
[group('acquisition')]
list-owl:
    @ls -lh {{owl_dir}}
    @shasum -a 256 {{owl_dir}}/*.owl {{owl_dir}}/*.tsv

# ============== Extraction recipes ==============

# Extract one source into a ResourceSnapshot: just extract doid doid.owl DOID_
[group('extraction')]
extract id file filter:
    uv run mondo-ai extract-owl {{owl_dir}}/{{file}} -r {{id}} -o {{snapshots_dir}}/{{id}}.yaml --iri-filter "{{filter}}" --include-deprecated

# Extract every source and Mondo into data/snapshots/SOURCE_DATE
[group('extraction')]
extract-all:
    just extract doid doid.owl DOID_
    just extract ordo ordo.owl Orphanet_
    just extract omim omim.rdf.owl omim.org/entry/
    just extract icd10cm icd10cm.owl ontology/ICD10CM/
    just extract mondo mondo.owl MONDO_

# List extracted snapshots
[group('extraction')]
list-snapshots:
    @ls -lh {{snapshots_dir}}

# ============== Packet Building recipes ==============

# Rebuild packet_store/ and packet_store_merged/ (synonyms, subclass, mappings) from the snapshots
[group('packets')]
build-packets:
    uv run python run_full_pipeline.py {{snapshots_dir}} {{owl_dir}}

# Download, extract and rebuild everything
[group('packets')]
rebuild-all: download-all extract-all build-packets

# ============== Validation recipes ==============

# Validate one packet YAML/JSON file against the schema
[group('validation')]
validate-schema file:
    uv run linkml-validate --schema {{schema_path}} --target-class EvidencePacket {{file}}

# Validate reference citations in evidence packets using linkml-reference-validator
[group('validation')]
validate-references file:
    uv run linkml-reference-validator validate data {{file}} --schema {{schema_path}} --target-class EvidencePacket

# Cache a reference by PMID for faster validation
[group('validation')]
cache-reference pmid:
    uv run linkml-reference-validator cache reference {{pmid}}

# Validate ontology term references in an evidence packet using linkml-term-validator
[group('validation')]
validate-terms file:
    uv run linkml-term-validator validate-data {{file}} -s {{schema_path}} -t EvidencePacket --labels -c {{oak_config}}

# Validate schema term references (prefixes, etc.)
[group('validation')]
validate-schema-terms:
    uv run linkml-term-validator validate-schema {{schema_path}}

# ============== Scoring & Enrichment recipes ==============

# Re-score the merged store with the inclusion score
[group('scoring')]
score-packets:
    uv run mondo-ai score packet_store_merged

# Enrich packets with PubMed literature (all packets)
[group('enrichment')]
enrich-packets:
    uv run mondo-ai enrich packet_store_merged --max-papers 3

# Run AI synthesis on packets
[group('synthesis')]
synthesize-packets limit="10":
    uv run mondo-ai synthesize packet_store_merged --limit {{limit}}

# Run AI synthesis with OpenAI
[group('synthesis')]
synthesize-openai limit="10":
    uv run mondo-ai synthesize packet_store_merged --provider openai --limit {{limit}}

# ============== Storage & Index recipes ==============

# Show store statistics
[group('storage')]
index-stats:
    uv run mondo-ai stats packet_store_merged

# ============== Pipeline recipes ==============

# Generate pydantic models from the LinkML overlay (sieve's codegen fixes the date shadowing)
[group('pipeline')]
gen-pydantic:
    uv run python -m sieve.datamodel.codegen {{schema_path}} src/mondo_ai/datamodel/sieve_models.py

# ============== Web Application recipes ==============

# Generate the static evidence-browser data for the docs site
[group('web')]
gen-web-data:
    uv run mondo-ai export-web-data

# Serve the docs site (with a freshly generated evidence browser) locally
[group('web')]
serve-docs: gen-web-data
    uv run mkdocs serve

# Serve the web application (development)
[group('web')]
serve-web:
    uv run uvicorn mondo_ai.web.app:app --reload --host 0.0.0.0 --port 8000

# Serve the web application (production)
[group('web')]
serve-web-prod:
    uv run uvicorn mondo_ai.web.app:app --host 0.0.0.0 --port 8000
