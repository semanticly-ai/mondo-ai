"""The mondo-ai SieveRegistry and per-packet reference snapshots.

The registry is the reference data every packet points at by id: information resources
(with quality rating, role and dependence), the releases read, the pipeline agent, the
methods it follows and one AggregationRule per claim type. A packet must be
self-contained (D-051), so each one carries a frozen copy of the entries it references
in its ``reference_snapshot``.

Example:
    >>> from mondo_ai.config import get_default_config
    >>> reg = build_registry(get_default_config(), {"doid": "2026-10-29"}, "2026-11-20")
    >>> sorted(r["id"] for r in reg["aggregation_rules"])[:2]
    ['mondoai:rule/mapping-v1.0', 'mondoai:rule/subclass-v1.0']
"""

import re
from pathlib import Path
from typing import Any, Iterator

from mondo_ai import __version__
from mondo_ai.config import PipelineConfig

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "mondo_ai_sieve.yaml"

AGENT = "mondoai:agent/concordance-pipeline"
CONCORDANCE_METHOD = "mondoai:method/source-concordance"
NORMALIZATION_METHOD = "mondoai:method/sssom-normalization"
LEXICAL_METHOD = "mondoai:method/lexical-match"
PUBMED = "infores:pubmed"
MONDO_RESOURCE = "infores:mondo"

CLAIM_TYPES = {
    "SynonymStatement": "synonym",
    "SubclassStatement": "subclass",
    "MappingStatement": "mapping",
}

EXCLUSIVITY = {
    "SynonymStatement": "An exact synonym belongs to one live term per branch; abbreviations are exempt.",
    "MappingStatement": "An exact match is 1:1 into a source unless marked as one of several preferred siblings.",
}


def rule_id(claim_type: str, version: str) -> str:
    """The id of the AggregationRule for a claim type.

    >>> rule_id("SynonymStatement", "1.0")
    'mondoai:rule/synonym-v1.0'
    """
    return f"mondoai:rule/{CLAIM_TYPES[claim_type]}-v{version}"


def release_version(version_iri: str) -> str:
    """A short release version from an ontology version IRI or version string.

    >>> release_version("http://purl.obolibrary.org/obo/doid/releases/2026-10-29/doid.owl")
    '2026-10-29'
    >>> release_version("https://www.orphadata.com/data/ontologies/ordo/last_version/ORDO_en_4.9.owl")
    '4.9'
    >>> release_version("2024ab")
    '2024ab'
    """
    if m := re.search(r"\d{4}-\d{2}-\d{2}", version_iri):
        return m.group(0)
    if m := re.search(r"_(\d+(?:\.\d+)+)\.owl$", version_iri):
        return m.group(1)
    return version_iri


def release_id(source_id: str, version: str) -> str:
    """The id of a SourceRelease.

    >>> release_id("doid", "2026-10-29")
    'mondoai:release/doid-2026-10-29'
    """
    return f"mondoai:release/{source_id}-{version}"


def build_registry(config: PipelineConfig, releases: dict[str, str], date: str) -> dict[str, Any]:
    """Build the registry for one pipeline run.

    Args:
        config: Pipeline configuration (resources, weights, rule parameters).
        releases: Source id -> release version read in this run.
        date: Run date (ISO), used as quality-rating and registry version date.

    Returns:
        A SieveRegistry as a dict.
    """
    by_id = {r.id: r for r in config.resources}
    resources: list[dict[str, Any]] = []
    for r in config.resources:
        entry: dict[str, Any] = {
            "id": r.infores,
            "type": "InformationResource",
            "label": r.name,
            "curie_prefixes": r.curie_prefixes,
            "default_role": r.default_role,
        }
        if r.derives_content_from:
            entry["derives_content_from"] = [by_id[d].infores for d in r.derives_content_from]
        if r.quality is not None:
            entry["quality_ratings"] = [{"level": r.quality, "valid_from": "2026-01-01"}]
        resources.append(entry)
    resources.append(
        {"id": PUBMED, "type": "InformationResource", "label": "PubMed", "curie_prefixes": ["PMID"], "default_role": "PRIMARY"}
    )
    resources.append(
        {"id": MONDO_RESOURCE, "type": "InformationResource", "label": "Mondo", "curie_prefixes": ["MONDO"], "default_role": "PRIMARY"}
    )

    release_entries = [
        {"id": release_id(sid, version), "type": "SourceRelease", "of_resource": by_id[sid].infores, "version": version}
        for sid, version in releases.items()
        if sid in by_id
    ]

    scoring = config.scoring
    rules = []
    for claim_type in CLAIM_TYPES:
        rule: dict[str, Any] = {
            "id": rule_id(claim_type, scoring.rule_version),
            "type": "AggregationRule",
            "version": scoring.rule_version,
            "claim_type": claim_type,
            "strength_bands": scoring.strength_bands,
            "confidence_floor": scoring.confidence_floor,
            "scaling_factor": scoring.scaling_factor,
            "default_quality": scoring.default_quality,
            "stacked_lines": "CAP_AND_ADD",
            "claim_priors": [{"channel": "SOURCE_INGEST", "log_odds": scoring.source_ingest_prior}],
        }
        if claim_type in EXCLUSIVITY:
            rule["exclusive"] = True
            rule["exclusivity_rule"] = EXCLUSIVITY[claim_type]
        rules.append(rule)

    return {
        "id": "mondoai:registry",
        "type": "SieveRegistry",
        "version": date,
        "resources": resources,
        "releases": release_entries,
        "agents": [
            {
                "id": AGENT,
                "type": "SieveAgent",
                "subtype": "software",
                "name": "mondo-ai source concordance pipeline",
                "software_version": __version__,
            }
        ],
        "methods": [
            {
                "id": CONCORDANCE_METHOD,
                "type": "SieveMethod",
                "label": "Source concordance: a source's own claim, normalized to Mondo ids, read as evidence",
                "version": "2.0",
            },
            {
                "id": NORMALIZATION_METHOD,
                "type": "SieveMethod",
                "label": "Normalization of source ids to Mondo ids by the skos:exactMatch rows of mondo.sssom.tsv",
                "version": "1.0",
            },
            {
                "id": LEXICAL_METHOD,
                "type": "SieveMethod",
                "label": "Case- and punctuation-insensitive match of Mondo and source labels and exact synonyms",
                "version": "1.0",
            },
        ],
        "aggregation_rules": rules,
    }


SNAPSHOT_LISTS = ("resources", "releases", "agents", "methods", "aggregation_rules", "policies", "batches")


def registry_index(registry: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Index registry entries by id: id -> (list name, entry)."""
    return {entry["id"]: (key, entry) for key in SNAPSHOT_LISTS for entry in registry.get(key) or []}


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def reference_snapshot(
    packet: dict[str, Any], index: dict[str, tuple[str, dict[str, Any]]], version: str
) -> dict[str, Any]:
    """The frozen registry entries a packet references, closed over references between entries.

    Args:
        packet: The packet (statement and lines), without a reference_snapshot.
        index: ``registry_index`` of the run's registry.
        version: Packet version.

    Returns:
        A SieveRegistry dict holding only the referenced entries, in registry order.
    """
    found: set[str] = set()
    todo = [s for s in _strings(packet["statement"]) if s in index]
    while todo:
        ref = todo.pop()
        if ref in found:
            continue
        found.add(ref)
        todo += [s for s in _strings(index[ref][1]) if s in index and s not in found]
    snapshot: dict[str, Any] = {"id": f"{packet['id']}#references", "type": "SieveRegistry", "version": version}
    for ref, (key, entry) in index.items():
        if ref in found:
            snapshot.setdefault(key, []).append(entry)
    return snapshot
