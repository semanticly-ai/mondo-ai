## Alignment pipeline recipes.
##
## Everything deterministic lives here, including the downloads: nothing inside the Python
## fetches implicitly, so a run's inputs are always something you asked for and can checksum.

align_data := "data/align"
align_conf := "conf/align"
align_out := "alignments"

# ============== Acquisition ==============

# Mondo, from the canonical PURL. OBO rather than OWL: same content for alignment purposes,
# a third of the size, and no triple store needed to read it.
mondo_obo_url := "http://purl.obolibrary.org/obo/mondo.obo"
# ICD-10-CM, from the mondo-ingest component. This is the normalised build Mondo itself
# ingests (UMLS2RDF via BioPortal, then ODK normalisation), needs no API key, and carries all
# 97,903 codes. Do NOT substitute the icd10cm.ttl that circulates elsewhere: it is truncated
# to ~22k codes and is missing whole rubrics, Q87.4 Marfan syndrome among them.
icd10cm_owl_url := "http://purl.obolibrary.org/obo/mondo/sources/icd10cm.owl"

# Create the data and output directories
[group('align')]
align-init:
    mkdir -p {{align_data}} {{align_out}}

# Download Mondo
[group('align')]
align-download-mondo: align-init
    curl -sSL --retry 3 -o {{align_data}}/mondo.obo "{{mondo_obo_url}}"
    @echo "mondo.obo  $(shasum -a 256 {{align_data}}/mondo.obo | cut -c1-16)  $(grep -c '^\[Term\]' {{align_data}}/mondo.obo) terms"

# Download ICD-10-CM
[group('align')]
align-download-icd10cm: align-init
    curl -sSL --retry 3 -o {{align_data}}/icd10cm.owl "{{icd10cm_owl_url}}"
    @echo "icd10cm.owl  $(shasum -a 256 {{align_data}}/icd10cm.owl | cut -c1-16)  $(grep -c '<owl:Class rdf:about=\"http://purl.bioontology.org/ontology/ICD10CM/' {{align_data}}/icd10cm.owl) codes"

# Download every source the reference run needs
[group('align')]
align-download: align-download-mondo align-download-icd10cm

# --- SNOMED CT surface ---
#
# SNOMED CT is licensed and is not downloadable without an account, so its source is a local
# OMOP/Athena vocabulary zip rather than a URL; set OMOP_VOCAB_ZIP to use another one. The
# OMOP seed (Mondo "Maps to" rows plus their metadata) is the pair of files in background/.
mondo_sssom_url := "http://purl.obolibrary.org/obo/mondo/mappings/mondo.sssom.tsv"
omop_zip := env("OMOP_VOCAB_ZIP", "/Users/matentzn/ws/notes/niehs_standards/background/omop/vocabulary_download_v5_{d8dcb01e-532f-4d19-ae52-eb1044bec019}_1791811658579.zip")
omop_relationships := "background/concept_relationship_manual.xlsx"
omop_metadata := "background/concept_relationship_metadata_stage.csv"

# Download Mondo's released mapping set, whose SCTID rows are one of the SNOMED seeds
[group('align')]
align-download-mondo-sssom: align-init
    curl -sSL --retry 3 -o {{align_data}}/mondo.sssom.tsv "{{mondo_sssom_url}}"
    @echo "mondo.sssom.tsv  $(shasum -a 256 {{align_data}}/mondo.sssom.tsv | cut -c1-16)  $(grep -c 'SCTID:' {{align_data}}/mondo.sssom.tsv) SCTID rows"

# Extract SNOMED CT (Disorder, Clinical Finding, Morph Abnormality) from the OMOP zip. Minutes.
[group('align')]
align-snomed-extract zip=omop_zip: align-init
    @test -f "{{zip}}" || (echo "OMOP vocabulary zip not found: {{zip}} (set OMOP_VOCAB_ZIP)" && exit 1)
    uv run mondo-ai align extract-snomed "{{zip}}" -o {{align_data}}/snomed.tsv

# Convert the OMOP Mondo -> SNOMED mappings and their metadata into a seed SSSOM set
[group('align')]
align-snomed-seeds: align-init
    uv run mondo-ai align omop-seeds "{{omop_relationships}}" "{{omop_metadata}}" -o {{align_data}}/omop_mondo_snomed.sssom.tsv

# Everything the SNOMED run reads: latest Mondo, its mapping set, SNOMED, the OMOP seed
[group('align')]
align-snomed-prepare: align-download-mondo align-download-mondo-sssom align-snomed-extract align-snomed-seeds

# Show what is downloaded, with checksums
[group('align')]
align-sources:
    @ls -lh {{align_data}} 2>/dev/null || echo "nothing downloaded; run: just align-download"
    @shasum -a 256 {{align_data}}/* 2>/dev/null || true

# ============== Running ==============

# Uses whatever is already in data/align; `just align-download` refreshes it. Deliberately not
# a dependency: re-fetching 150 MB on every iteration is waste, and run.yaml records the
# checksum of whatever was actually used.
#
# Full reference run: Mondo x ICD-10-CM, ladder plus agent adjudication
[group('align')]
align-icd10cm:
    @test -f {{align_data}}/mondo.obo -a -f {{align_data}}/icd10cm.owl         || (echo "sources missing; run: just align-download" && exit 1)
    uv run mondo-ai align run -c {{align_conf}}/mondo-icd10cm.yaml

# Refresh both sources, then run
[group('align')]
align-icd10cm-fresh: align-download align-icd10cm

# No model calls, no cost, fully reproducible; the unsettled queue becomes EXPERT_REVIEW
[group('align')]
align-icd10cm-deterministic:
    uv run mondo-ai align run -c {{align_conf}}/mondo-icd10cm.yaml --no-agent

# Smoke run over the first N subjects, to check a change end to end in seconds
[group('align')]
align-smoke n="300":
    uv run mondo-ai align run -c {{align_conf}}/mondo-icd10cm.yaml \
        --limit {{n}} --max-agent-items 20 --run-id smoke

# Run an arbitrary config
[group('align')]
align-run config:
    uv run mondo-ai align run -c {{config}}

# Mondo x SNOMED CT: discovery with seeds held out, then pair-level reconciliation. No model
# calls: deterministic and repeatable from the inputs `just align-snomed-prepare` writes
[group('align')]
align-snomed:
    @test -f {{align_data}}/mondo.obo -a -f {{align_data}}/mondo.sssom.tsv -a -f {{align_data}}/snomed.tsv -a -f {{align_data}}/omop_mondo_snomed.sssom.tsv || (echo "inputs missing; run: just align-snomed-prepare" && exit 1)
    uv run mondo-ai align run -c {{align_conf}}/mondo-snomed.yaml --no-agent

# Prepare every input from scratch, then run
[group('align')]
align-snomed-all: align-snomed-prepare align-snomed

# Same, with agent adjudication of the unsettled queue (costs money; see the config's cap)
[group('align')]
align-snomed-agent:
    uv run mondo-ai align run -c {{align_conf}}/mondo-snomed.yaml

# SNOMED smoke run over the first N subjects
[group('align')]
align-snomed-smoke n="2000":
    uv run mondo-ai align run -c {{align_conf}}/mondo-snomed.yaml --no-agent \
        --limit {{n}} --run-id mondo-snomed-smoke

# Consolidated pairs by reconciliation status and review tier
[group('align')]
align-reconciled run="mondo-snomed":
    @awk -F'\t' '/^#/{next} !h{for(i=1;i<=NF;i++)k[$i]=i; h=1; next} \
        {n[$k["reconciliation_status"] "\t" $k["review_tier"]]++} \
        END {for (s in n) printf "%-44s %8d\n", s, n[s]}' \
        {{align_out}}/{{run}}/consolidated.sssom.tsv | sort -k3 -rn

# Consolidated pairs with one status, e.g. `just align-reconciled-status predicate_conflict`
[group('align')]
align-reconciled-status status run="mondo-snomed" n="30":
    @awk -F'\t' -v s={{status}} -v n={{n}} '/^#/{next} !h{for(i=1;i<=NF;i++)k[$i]=i; h=1; next} \
        $k["reconciliation_status"]==s && shown<n { \
        printf "%s %s\t%s\t%s %s\t%s\t%s\n", $k["subject_id"], \
        substr($k["subject_label"],1,36), $k["predicate_id"], $k["object_id"], \
        substr($k["object_label"],1,36), $k["seed_predicates"], $k["risk_flags"]; shown++}' \
        {{align_out}}/{{run}}/consolidated.sssom.tsv

# ============== Inspecting ==============

# Why did this subject come out that way? Deterministic stages for one term, no model call
[group('align')]
align-explain id config=(align_conf + "/mondo-icd10cm.yaml"):
    uv run mondo-ai align explain -c {{config}} {{id}}

# Same, as the exact JSON payload the adjudicating model would see.
[group('align')]
align-explain-json id config=(align_conf + "/mondo-icd10cm.yaml"):
    uv run mondo-ai align explain -c {{config}} {{id}} --json

# Regenerate the published curation-rule value set (SKOS + LinkML + docs) from the registry
[group('align')]
align-valueset:
    uv run mondo-ai align valueset

# Fail if the checked-in value set has drifted from the registry
[group('align')]
align-valueset-check:
    uv run mondo-ai align valueset --check

# Print the citable curation rule registry
[group('align')]
align-rules family="":
    uv run mondo-ai align rules {{ if family != "" { "--family " + family } else { "" } }}

# Re-score an existing run against the held-out asserted mappings
[group('align')]
align-eval config=(align_conf + "/mondo-icd10cm.yaml"):
    uv run mondo-ai align evaluate -c {{config}}

# Every view below resolves columns by NAME from the header row, never by position. The
# system table gains columns as the pipeline grows — it went from 16 to 21 when the Boomer
# columns landed — and a hardcoded field number silently prints the wrong column rather than
# failing, which is the worst way for an inspection tool to be wrong.

# The top of the review queue: rows an adjudicator examined and could not settle
[group('align')]
align-queue run="mondo-icd10cm" n="30":
    @awk -F'\t' -v n={{n}} 'NR==1{for(i=1;i<=NF;i++)c[$i]=i; \
        print "priority\ttier\tsubject\tpredicate\tobject\twhy"; next} \
        $c["review_tier"]=="EXPERT_REVIEW" && shown<n { \
        printf "%s\t%s\t%s %s\t%s\t%s %s\t%s\n", $c["review_priority"], \
        $c["review_tier"], $c["subject_id"], substr($c["subject_label"],1,40), \
        $c["predicate_id"], $c["object_id"], substr($c["object_label"],1,28), \
        $c["top_priority_reason"]; shown++}' \
        {{align_out}}/{{run}}/system_table.tsv

# Row counts by review tier
[group('align')]
align-tiers run="mondo-icd10cm":
    @awk -F'\t' 'NR==1{for(i=1;i<=NF;i++)c[$i]=i; next} {n[$c["review_tier"]]++} \
        END {for (t in n) printf "%-14s %8d\n", t, n[t]}' \
        {{align_out}}/{{run}}/system_table.tsv | sort -k2 -rn

# The run report
[group('align')]
align-report run="mondo-icd10cm":
    @cat {{align_out}}/{{run}}/report.md

# Ad-hoc SQL over the run
[group('align')]
align-sql query run="mondo-icd10cm":
    uv run python -c "import duckdb,sys; con=duckdb.connect('{{align_out}}/{{run}}/alignment.duckdb'); [print(*r, sep='\t') for r in con.execute(sys.argv[1]).fetchall()]" "{{query}}"

# Confirm the emitted set still parses as SSSOM (sssom-py warnings about dev slots are expected)
[group('align')]
align-validate run="mondo-icd10cm":
    uv run python -c "from sssom.parsers import parse_sssom_table; \
        msdf = parse_sssom_table('{{align_out}}/{{run}}/mappings.sssom.tsv'); \
        print(f'parsed {len(msdf.df):,} rows, {len(msdf.df.columns)} columns kept by sssom-py'); \
        print('prefixes:', sorted(msdf.prefix_map)[:8])"

# Clear the agent response cache (only needed after a prompt change)
[group('align')]
align-clear-cache:
    rm -rf .align_cache
    @echo "agent response cache cleared"

# Where consistency conditioning argues against a row the pipeline was confident about
[group('align')]
align-boomer-conflicts run="mondo-icd10cm" n="30":
    @printf 'prior\tpost\tdrop\tsubject\tours\tobject\tboomer\n'
    @awk -F'\t' 'NR==1{for(i=1;i<=NF;i++)c[$i]=i; next} \
        $c["boomer_prior"]!="" && $c["boomer_posterior"]!="" && \
        ($c["boomer_prior"]-$c["boomer_posterior"])>0.05 { \
        printf "%.3f\t%.3f\t%.3f\t%s %s\t%s\t%s %s\t%s\n", \
        $c["boomer_prior"], $c["boomer_posterior"], \
        $c["boomer_prior"]-$c["boomer_posterior"], \
        $c["subject_id"], substr($c["subject_label"],1,34), \
        $c["predicate_id"], $c["object_id"], substr($c["object_label"],1,24), \
        $c["boomer_predicate"]}' \
        {{align_out}}/{{run}}/system_table.tsv | sort -k3 -rn | head -{{n}}

# Alignment tests only (Boomer stays behind the integration marker: it is an external
# Java binary, and a JVM start per test made the suite slow and intermittently flaky)
[group('align')]
align-test:
    uv run pytest tests/align -q
    uv run pytest --doctest-modules src/mondo_ai/align -q

# The tests that shell out to the real Boomer binary; skipped if it is not installed
[group('align')]
align-test-boomer:
    uv run pytest tests/align -q -m integration
