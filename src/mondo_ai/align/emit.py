"""Output writers: SSSOM, the record store, the system table, DuckDB and the report.

Rules are cited by CURIE, not by prose. ``curation_rule`` is a URI-valued SSSOM slot, so a
row says ``maprule:SC-001|maprule:PR-002`` in one cell and the reader resolves it against the
published value set (``vocabulary/maprule.ttl``, ``docs/maprule.md``). The earlier version of
this module inlined each rule's full title into every row, which made a 32,109-row file
mostly repeated English and gave no machine anything to join on.

Non-standard columns are **declared** in the SSSOM header's ``extension_definitions`` rather
than smuggled in as extra columns. An undeclared column is a column the next tool silently
drops, and these columns are the point of the pipeline.

The set-level ``comment`` carries the review protocol: source versions and checksums, the
curation rules in force, the use case, and every conflation the set knowingly permits. A set
that crosses the disease/phenotype boundary without saying so is not reviewable however good
its rows.

Example:
    >>> sorted(EXTENSION_SLOTS)[:4]
    ['agent_agreement', 'agent_alternatives', 'agent_confidence', 'ambiguity_count']
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from mondo_ai.align.config import AlignmentConfig
from mondo_ai.align.models import (
    NO_TERM_FOUND,
    MappingRecord,
    ResourceSet,
    ReviewTier,
)
from mondo_ai.align.rules import (
    MAPRULE_URI_BASE,
    render_rule_curies,
    render_rule_text,
)
from mondo_ai.align.triage import WEIGHTS

MAPPING_TOOL = "mondo-ai align"

#: Standard SSSOM slots emitted, in column order.
STANDARD_SLOTS = [
    "subject_id", "subject_label", "predicate_id", "object_id", "object_label",
    "mapping_justification", "confidence", "subject_preprocessing", "mapping_tool",
    "mapping_tool_version", "mapping_date", "curation_rule",
    "similarity_score", "comment",
]

#: Non-standard slots, declared in ``extension_definitions``. Each maps to a property URI
#: under the ``maprule`` namespace so the declaration is resolvable in principle.
EXTENSION_SLOTS: dict[str, tuple[str, str]] = {
    "signals": ("http://w3id.org/mondo-ai/align/signals", "xsd:string"),
    "decision_path": ("http://w3id.org/mondo-ai/align/decisionPath", "xsd:string"),
    "review_tier": ("http://w3id.org/mondo-ai/align/reviewTier", "xsd:string"),
    "review_priority": ("http://w3id.org/mondo-ai/align/reviewPriority", "xsd:double"),
    "ambiguity_count": ("http://w3id.org/mondo-ai/align/ambiguityCount", "xsd:integer"),
    "risk_flags": ("http://w3id.org/mondo-ai/align/riskFlags", "xsd:string"),
    "agent_confidence": ("http://w3id.org/mondo-ai/align/agentConfidence", "xsd:double"),
    "agent_agreement": ("http://w3id.org/mondo-ai/align/agentAgreement", "xsd:double"),
    "agent_alternatives": ("http://w3id.org/mondo-ai/align/agentAlternatives", "xsd:string"),
    "conflation_rules": ("http://w3id.org/mondo-ai/align/conflationRules", "xsd:string"),
    "deterministic_confidence": (
        "http://w3id.org/mondo-ai/align/deterministicConfidence", "xsd:double"),
    "expert_question": ("http://w3id.org/mondo-ai/align/expertQuestion", "xsd:string"),
    "mapping_tier": ("http://w3id.org/mondo-ai/align/mappingTier", "xsd:integer"),
    "structural_flags": ("http://w3id.org/mondo-ai/align/structuralFlags", "xsd:string"),
    "verdict_source": ("http://w3id.org/mondo-ai/align/verdictSource", "xsd:string"),
    # Boomer: consistency conditioning over the whole set, not this row alone.
    "boomer_prior": ("http://w3id.org/mondo-ai/align/boomerPrior", "xsd:double"),
    "boomer_posterior": ("http://w3id.org/mondo-ai/align/boomerPosterior", "xsd:double"),
    "boomer_predicate": ("http://w3id.org/mondo-ai/align/boomerPredicate", "xsd:string"),
    "boomer_agrees": ("http://w3id.org/mondo-ai/align/boomerAgrees", "xsd:boolean"),
    "boomer_clique_size": ("http://w3id.org/mondo-ai/align/boomerCliqueSize", "xsd:integer"),
    "boomer_clique_confidence": (
        "http://w3id.org/mondo-ai/align/boomerCliqueConfidence", "xsd:double"),
    "boomer_method": ("http://w3id.org/mondo-ai/align/boomerMethod", "xsd:string"),
    "boomer_constrained": (
        "http://w3id.org/mondo-ai/align/boomerConstrained", "xsd:boolean"),
}

ALL_SLOTS = STANDARD_SLOTS + list(EXTENSION_SLOTS)

#: Mapping justification by verdict source, per semapv.
JUSTIFICATION = {
    "deterministic": "semapv:LexicalMatching",
    "agent": "semapv:CompositeMatching",
}


def _pipe(values: Iterable[str]) -> str:
    """Join values for a single SSSOM cell.

    Example:
        >>> _pipe(["a", "b"])
        'a|b'
        >>> _pipe([])
        ''
    """
    return "|".join(v for v in values if v)


def record_to_row(record: MappingRecord, version: str) -> dict[str, Any]:
    """Project a :class:`MappingRecord` into an SSSOM row.

    Rows with no object are emitted as ``sssom:NoTermFound``, keeping the reason: that is
    what distinguishes "no code exists" from "nobody looked".

    Example:
        >>> from mondo_ai.align.models import Adjudication, Candidate
        >>> rec = MappingRecord("MONDO:1", "Cholera", object_label="Cholera",
        ...     final=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99,
        ...                        rule_ids=["EV-001", "PR-001"], decision_path="D1",
        ...                        justification="Identical labels."),
        ...     candidates=[Candidate("ICD10CM:A00")],
        ...     review_tier=ReviewTier.AUTO_ACCEPT, review_priority=0.0)
        >>> row = record_to_row(rec, "0.1.0")
        >>> row["predicate_id"], row["mapping_justification"], row["mapping_tier"]
        ('skos:exactMatch', 'semapv:LexicalMatching', 3)
        >>> row["curation_rule"]
        'maprule:EV-001|maprule:PR-001'
        >>> row["review_tier"], row["confidence"], row["verdict_source"]
        ('AUTO_ACCEPT', 0.99, 'deterministic')
        >>> "author_id" in row
        False

        A no-match row still carries its reason:

        >>> rec2 = MappingRecord("MONDO:2", "X",
        ...     final=Adjudication("deterministic", "none", rule_ids=["RJ-001"],
        ...                        decision_path="D7", justification="Nothing denotes it."),
        ...     review_tier=ReviewTier.NO_MATCH)
        >>> row2 = record_to_row(rec2, "0.1.0")
        >>> row2["predicate_id"], row2["object_id"]
        ('sssom:NoTermFound', 'sssom:NoTermFound')
        >>> row2["curation_rule"]
        'maprule:RJ-001'
        >>> row2["boomer_prior"]
        ''
    """
    final = record.final
    assert final is not None, "triage() must run before emission"

    winner = record.candidate(final.object_id) if final.object_id else None
    transforms = winner.transforms() if winner else []
    signals = sorted({s.kind for c in record.candidates for s in c.signals})
    risk_flags = sorted({f for c in record.candidates for f in c.flags})
    similarity = round(winner.best_certainty(), 4) if winner else ""

    return {
        "subject_id": record.subject_id,
        "subject_label": record.subject_label,
        "predicate_id": final.predicate,
        "object_id": final.object_id or NO_TERM_FOUND,
        "object_label": record.object_label,
        "mapping_justification": JUSTIFICATION.get(final.source, "semapv:CompositeMatching"),
        "confidence": round(final.confidence, 4),
        "subject_preprocessing": _pipe(transforms),
        "mapping_tool": MAPPING_TOOL,
        "mapping_tool_version": version,
        "mapping_date": date.today().isoformat(),
        # No author_id. The Mondo curation guide requires Tier 3 provenance to be
        # sssom:mapping_tool_id / mapping_tool_version, and author_id is a CURIE-valued slot
        # for ORCIDs: sssom-py drops any row whose author_id is not resolvable, and inventing
        # an ORCID for a pipeline is worse than leaving the slot empty. Which adjudicator
        # produced the verdict is recorded in the declared verdict_source extension.
        "curation_rule": render_rule_curies(final.rule_ids),
        "similarity_score": similarity,
        "comment": final.justification,
        "signals": _pipe(signals),
        "decision_path": final.decision_path,
        "review_tier": record.review_tier.value,
        "review_priority": record.review_priority,
        "ambiguity_count": record.ambiguity_count,
        "risk_flags": _pipe(risk_flags),
        "agent_confidence": round(record.agent.confidence, 4) if record.agent else "",
        "agent_agreement": round(record.agent.agreement, 4) if record.agent else "",
        "agent_alternatives": _pipe(final.alternatives),
        "conflation_rules": _pipe(final.conflation),
        "deterministic_confidence": (
            round(record.deterministic.confidence, 4) if record.deterministic else ""),
        "expert_question": final.expert_question,
        "mapping_tier": 3,
        "structural_flags": _pipe(record.structural_flags),
        "verdict_source": final.source,
        **_boomer_cells(record),
    }


def _boomer_cells(record: MappingRecord) -> dict[str, Any]:
    """Project Boomer's findings into cells, leaving them empty when it did not see the row.

    Empty rather than zero throughout: a mapping Boomer never conditioned has no posterior,
    and writing ``0.0`` would read downstream as "consistency ruled this out".

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("M:1", final=Adjudication("agent", "exact", "I:A"))
        >>> _boomer_cells(rec)["boomer_prior"]
        ''
        >>> rec.boomer = {"prior": 0.7, "posterior": 0.0329,
        ...               "predicate": "skos:narrowMatch", "agrees_with_pipeline": False,
        ...               "clique_size": 3, "clique_confidence": 0.923,
        ...               "method": "exhaustive search"}
        >>> cells = _boomer_cells(rec)
        >>> cells["boomer_prior"], cells["boomer_posterior"], cells["boomer_agrees"]
        (0.7, 0.0329, False)
        >>> cells["boomer_clique_size"], cells["boomer_method"]
        (3, 'exhaustive search')
        >>> _boomer_cells(rec)["boomer_constrained"] == ""
        True
    """
    data = record.boomer
    if not data:
        return {
            "boomer_prior": "", "boomer_posterior": "", "boomer_predicate": "",
            "boomer_agrees": "", "boomer_clique_size": "",
            "boomer_clique_confidence": "", "boomer_method": "",
            "boomer_constrained": "",
        }
    posterior = data.get("posterior")
    return {
        "boomer_prior": data.get("prior", ""),
        "boomer_posterior": "" if posterior is None else posterior,
        "boomer_predicate": data.get("predicate", ""),
        "boomer_agrees": data.get("agrees_with_pipeline", ""),
        "boomer_clique_size": data.get("clique_size", ""),
        "boomer_clique_confidence": data.get("clique_confidence", ""),
        "boomer_method": data.get("method", ""),
        "boomer_constrained": data.get("constrained", ""),
    }


#: Namespaces for prefixes this pipeline meets in practice. A prefix not listed here gets a
#: placeholder so that the set still declares one — an undeclared prefix makes the whole file
#: unparseable, and a wrong-but-declared namespace is a fixable metadata error.
KNOWN_NAMESPACES = {
    "MONDO": "http://purl.obolibrary.org/obo/MONDO_",
    "ICD10CM": "http://purl.bioontology.org/ontology/ICD10CM/",
    "ICD10WHO": "http://purl.bioontology.org/ontology/ICD10/",
    "DOID": "http://purl.obolibrary.org/obo/DOID_",
    "NCIT": "http://purl.obolibrary.org/obo/NCIT_",
    "HP": "http://purl.obolibrary.org/obo/HP_",
    "Orphanet": "http://www.orpha.net/ORDO/Orphanet_",
    "OMIM": "https://omim.org/entry/",
    "UMLS": "http://linkedlifedata.com/resource/umls/id/",
    "MEDGEN": "http://identifiers.org/medgen/",
    "MESH": "http://id.nlm.nih.gov/mesh/",
    "SNOMEDCT": "http://snomed.info/id/",
    "SCTID": "http://snomed.info/id/",
}


def _curie_map(subject_prefix: str, object_prefix: str) -> dict[str, str]:
    """Build the set's CURIE map, declaring both resource prefixes.

    The vocabulary prefixes are added **without overwriting** a known namespace: an earlier
    version of this function appended the resource prefixes last and clobbered the real
    Mondo and ICD-10-CM namespaces with placeholders, which sssom-py then rejected.

    Example:
        >>> m = _curie_map("MONDO", "ICD10CM")
        >>> m["MONDO"]
        'http://purl.obolibrary.org/obo/MONDO_'
        >>> m["ICD10CM"]
        'http://purl.bioontology.org/ontology/ICD10CM/'
        >>> _curie_map("LOCAL", "OTHER")["LOCAL"]
        'https://w3id.org/mondo-ai/align/ns/LOCAL_'
        >>> sorted(set(m) & {"skos", "semapv", "sssom", "maprule"})
        ['maprule', 'semapv', 'skos', 'sssom']
    """
    out = {
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "semapv": "https://w3id.org/semapv/vocab/",
        "sssom": "https://w3id.org/sssom/",
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "maprule": MAPRULE_URI_BASE,
    }
    for prefix in (subject_prefix, object_prefix):
        if not prefix:
            continue
        out.setdefault(
            prefix,
            KNOWN_NAMESPACES.get(
                prefix, f"https://w3id.org/mondo-ai/align/ns/{prefix}_"),
        )
    for prefix, ns in KNOWN_NAMESPACES.items():
        if prefix in (subject_prefix, object_prefix):
            out[prefix] = ns
    return out


def set_metadata(
    config: AlignmentConfig,
    subjects: ResourceSet,
    objects: ResourceSet,
    version: str,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Build the SSSOM set-level metadata block.

    Example:
        >>> from mondo_ai.align.config import ResourceSpec
        >>> cfg = AlignmentConfig(run_id="r",
        ...     subject=ResourceSpec(name="mondo", prefix="MONDO", format="obo", path="m"),
        ...     object=ResourceSpec(name="icd", prefix="ICD10CM", format="obo", path="i"))
        >>> s = ResourceSet(name="mondo", prefix="MONDO", sha256="aaa", version="v1")
        >>> o = ResourceSet(name="icd", prefix="ICD10CM", sha256="bbb", version="v2")
        >>> md = set_metadata(cfg, s, o, "0.1.0", {"AUTO_ACCEPT": 1})
        >>> md["mapping_set_id"]
        'https://w3id.org/mondo-ai/align/r'
        >>> md["subject_source_version"], md["object_source_version"]
        ('v1', 'v2')
        >>> "maprule" in md["curie_map"]
        True
        >>> len(md["extension_definitions"]) == len(EXTENSION_SLOTS)
        True
        >>> "CM-001" in md["comment"]
        True
    """
    conflations = render_rule_text(config.conflations_permitted)
    protocol = "\n".join([
        f"Produced by {MAPPING_TOOL} {version} on {date.today().isoformat()}.",
        "",
        "REVIEW PROTOCOL",
        f"Use case: {config.use_case}",
        f"Subject source: {subjects.name} {subjects.version or '(no version)'} "
        f"sha256={subjects.sha256[:16]} retrieved={subjects.retrieved} "
        f"url={subjects.source_url or subjects.source_path}",
        f"Object source: {objects.name} {objects.version or '(no version)'} "
        f"sha256={objects.sha256[:16]} retrieved={objects.retrieved} "
        f"url={objects.source_url or objects.source_path}",
        "Curation rules in force: the maprule registry families EV (evidence), SC (scope), "
        "PR (predicate), ST (structural integrity), RJ (rejection grounds) and CM "
        "(disease/phenotype conceptual-model conflation). Every row cites them by CURIE in "
        "the URI-valued curation_rule slot; resolve them against the published value set "
        "(vocabulary/maprule.ttl as SKOS, vocabulary/maprule.yaml as a LinkML enum, "
        "docs/maprule.md for humans), whose namespace is declared as maprule: in curie_map.",
        f"Conflations knowingly permitted: {conflations or 'none declared'}.",
        "Tiering: every row is Tier 3 under the Mondo Mapping Curation Guide (AI-curated, "
        "precise, not reviewed by the Mondo core team). Rows are not curated assertions. "
        "review_tier says which need a human: AUTO_ACCEPT (settled by a strong "
        "deterministic rung with nothing against it), SPOT_CHECK (defensible, sample it), "
        "EXPERT_REVIEW (a human decides), REJECTED (actively decided against, with a rule), "
        "NO_MATCH (recorded absence, sssom:NoTermFound with RJ-001).",
        f"review_priority is a transparent weighted sum of: "
        f"{', '.join(f'{k} {v}' for k, v in WEIGHTS.items())}. Higher means an expert hour "
        f"buys more correctness there.",
        f"Agent adjudication: {'model ' + config.agent.model if config.agent.enabled else 'disabled'}"
        f"; rows it produced carry semapv:CompositeMatching and are proposals for review.",
        "Row counts by review tier: "
        + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) + ".",
        "",
        "Boomer: boomer_prior is the probability this pipeline put on the relation it "
        "asserted; boomer_posterior is that relation's max-marginal after conditioning on "
        "consistency with the two sources' subclass axioms and with every other mapping in "
        "its clique. They coincide for mappings nothing constrains. Where the posterior is "
        "far below the prior, consistency argues against a row the pipeline liked, and that "
        "is reflected in review_priority. An empty cell means Boomer did not condition that "
        "row, which is not the same as a posterior of zero.",
        "",
        "NOTE: sssom-py may warn about and drop unknown columns from its dataframe. The "
        "columns survive on disk, and every non-standard one is declared in "
        "extension_definitions.",
    ])

    return {
        "mapping_set_id": f"https://w3id.org/mondo-ai/align/{config.run_id}",
        "mapping_set_title": f"{subjects.name} to {objects.name} alignment ({config.run_id})",
        "mapping_set_description": (
            f"Agentic alignment of {subjects.name} against {objects.name}, with per-row "
            f"curation-rule citations and a review-priority triage."
        ),
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "mapping_date": date.today().isoformat(),
        "mapping_tool": MAPPING_TOOL,
        "mapping_tool_version": version,
        "subject_source": subjects.name,
        "subject_source_version": subjects.version,
        "object_source": objects.name,
        "object_source_version": objects.version,
        "curie_map": _curie_map(subjects.prefix, objects.prefix),
        "extension_definitions": [
            {"slot_name": slot, "property": prop, "type_hint": hint}
            for slot, (prop, hint) in EXTENSION_SLOTS.items()
        ],
        "comment": protocol,
    }


def write_sssom(
    path: Path,
    records: list[MappingRecord],
    metadata: dict[str, Any],
    version: str,
) -> int:
    """Write the SSSOM TSV, header block first.

    Returns:
        The number of data rows written.

    Example:
        >>> import tempfile, pathlib
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("MONDO:1", "Cholera", object_label="Cholera",
        ...     final=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99,
        ...                        rule_ids=["EV-001"], decision_path="D1"),
        ...     review_tier=ReviewTier.AUTO_ACCEPT)
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "m.sssom.tsv"
        >>> write_sssom(p, [rec], {"mapping_set_id": "x", "curie_map": {"a": "b"}}, "0.1.0")
        1
        >>> text = p.read_text()
        >>> text.startswith("#mapping_set_id: x")
        True
        >>> [ln for ln in text.splitlines() if not ln.startswith("#")][0].split("\\t")[:3]
        ['subject_id', 'subject_label', 'predicate_id']
    """
    import yaml

    header = yaml.safe_dump(metadata, sort_keys=False, default_flow_style=False, width=100)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        for line in header.splitlines():
            fh.write(f"#{line}\n")
        writer = csv.DictWriter(fh, fieldnames=ALL_SLOTS, delimiter="\t",
                                lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        n = 0
        for record in records:
            writer.writerow(record_to_row(record, version))
            n += 1
    return n


def _jsonable(value: Any) -> Any:
    """Convert dataclasses, enums and paths into JSON-serialisable values.

    Example:
        >>> from mondo_ai.align.models import Signal
        >>> _jsonable(Signal("a", "EV-001", 1.0))["rule_id"]
        'EV-001'
        >>> _jsonable(ReviewTier.AUTO_ACCEPT)
        'AUTO_ACCEPT'
        >>> _jsonable([1, {"a": Path("x")}])
        [1, {'a': 'x'}]
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, ReviewTier):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def write_records(path: Path, records: list[MappingRecord]) -> int:
    """Write the full audit trail as JSONL, one record per line.

    Example:
        >>> import tempfile, pathlib, json
        >>> from mondo_ai.align.models import Adjudication, Candidate, Signal
        >>> rec = MappingRecord("MONDO:1", "Cholera",
        ...     candidates=[Candidate("ICD10CM:A00", [Signal("lexical_exact", "EV-001", 1.0)])],
        ...     final=Adjudication("deterministic", "exact", "ICD10CM:A00", 0.99))
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "r.jsonl"
        >>> write_records(p, [rec])
        1
        >>> doc = json.loads(p.read_text())
        >>> doc["candidates"][0]["signals"][0]["rule_id"], doc["review_tier"]
        ('EV-001', 'NO_MATCH')
    """
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(_jsonable(record), sort_keys=True) + "\n")
    return len(records)


#: The system table's columns. Narrower than the SSSOM row on purpose: this is the file a
#: curator opens, ranked by the one number that matters.
SYSTEM_TABLE_COLUMNS = [
    "review_priority", "review_tier", "subject_id", "subject_label", "predicate_id",
    "object_id", "object_label", "confidence", "boomer_prior", "boomer_posterior",
    "boomer_predicate", "boomer_agrees", "boomer_constrained", "decision_path", "ambiguity_count",
    "risk_flags", "structural_flags", "curation_rule", "justification",
    "expert_question", "top_priority_reason",
]


def top_reason(components: dict[str, float]) -> str:
    """Return the component contributing most to a row's priority.

    Example:
        >>> top_reason({"disagreement": 1.0, "ambiguity": 1.0})
        'disagreement'
        >>> top_reason({k: 0.0 for k in WEIGHTS})
        'none'
        >>> top_reason({})
        'none'
    """
    weighted = {k: WEIGHTS[k] * v for k, v in components.items() if k in WEIGHTS and v > 0}
    if not weighted:
        return "none"
    return max(weighted.items(), key=lambda kv: (kv[1], kv[0]))[0]


def write_system_table(path: Path, records: list[MappingRecord]) -> int:
    """Write the ranked triage table.

    Example:
        >>> import tempfile, pathlib
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("MONDO:1", "Cholera",
        ...     final=Adjudication("agent", "exact", "ICD10CM:A00", 0.6,
        ...                        rule_ids=["EV-012"], justification="Same."),
        ...     review_tier=ReviewTier.SPOT_CHECK, review_priority=0.08,
        ...     priority_components={"agent_uncertainty": 0.4})
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.tsv"
        >>> write_system_table(p, [rec])
        1
        >>> rows = list(csv.DictReader(p.read_text().splitlines(), delimiter="\\t"))
        >>> rows[0]["review_tier"], rows[0]["top_priority_reason"]
        ('SPOT_CHECK', 'agent_uncertainty')
        >>> rows[0]["boomer_prior"], rows[0]["curation_rule"]
        ('', 'maprule:EV-012')
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SYSTEM_TABLE_COLUMNS, delimiter="\t",
                                lineterminator="\n")
        writer.writeheader()
        for r in records:
            final = r.final
            assert final is not None
            boomer = _boomer_cells(r)
            writer.writerow({
                "review_priority": r.review_priority,
                "review_tier": r.review_tier.value,
                "subject_id": r.subject_id,
                "subject_label": r.subject_label,
                "predicate_id": final.predicate,
                "object_id": final.object_id or NO_TERM_FOUND,
                "object_label": r.object_label,
                "confidence": round(final.confidence, 4),
                "boomer_prior": boomer.get("boomer_prior", ""),
                "boomer_posterior": boomer.get("boomer_posterior", ""),
                "boomer_predicate": boomer.get("boomer_predicate", ""),
                "boomer_agrees": boomer.get("boomer_agrees", ""),
                "boomer_constrained": boomer.get("boomer_constrained", ""),
                "decision_path": final.decision_path,
                "ambiguity_count": r.ambiguity_count,
                "risk_flags": _pipe(sorted({f for c in r.candidates for f in c.flags})),
                "structural_flags": _pipe(r.structural_flags),
                "curation_rule": render_rule_curies(final.rule_ids),
                "justification": final.justification,
                "expert_question": final.expert_question,
                "top_priority_reason": top_reason(r.priority_components),
            })
    return len(records)


def write_duckdb(path: Path, system_table: Path, sssom: Path) -> None:
    """Load the emitted TSVs into a DuckDB file for ad-hoc querying.

    DuckDB reads the TSVs directly rather than re-serialising the records, so the database
    and the files cannot disagree.

    Example:
        >>> import tempfile, pathlib
        >>> d = pathlib.Path(tempfile.mkdtemp())
        >>> _ = (d / "t.tsv").write_text("review_priority\\tsubject_id\\n0.5\\tM:1\\n")
        >>> _ = (d / "m.tsv").write_text("subject_id\\tpredicate_id\\nM:1\\tskos:exactMatch\\n")
        >>> write_duckdb(d / "a.duckdb", d / "t.tsv", d / "m.tsv")
        >>> import duckdb
        >>> con = duckdb.connect(str(d / "a.duckdb"))
        >>> con.execute("select count(*) from system_table").fetchone()[0]
        1
        >>> con.close()
    """
    import duckdb

    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        for table, source in (("system_table", system_table), ("mappings", sssom)):
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto("
                f"'{source}', delim='\t', header=true, all_varchar=true, "
                f"comment='#', ignore_errors=true)"
            )
    finally:
        con.close()


def tier_counts(records: list[MappingRecord]) -> dict[str, int]:
    """Count records per review tier.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rs = [MappingRecord("M:1", review_tier=ReviewTier.AUTO_ACCEPT),
        ...       MappingRecord("M:2", review_tier=ReviewTier.AUTO_ACCEPT),
        ...       MappingRecord("M:3", review_tier=ReviewTier.NO_MATCH)]
        >>> tier_counts(rs)
        {'AUTO_ACCEPT': 2, 'NO_MATCH': 1}
    """
    counter = Counter(r.review_tier.value for r in records)
    return dict(sorted(counter.items(), key=lambda kv: -kv[1]))
