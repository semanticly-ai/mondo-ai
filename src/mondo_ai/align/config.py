"""Run configuration for the alignment pipeline.

Pydantic here rather than dataclasses: this is where a human-edited YAML file crosses into
the program, and a typo in a resource format should fail loudly at load time, not three
minutes into a 100 MB parse.

Example:
    >>> cfg = AlignmentConfig(
    ...     run_id="demo",
    ...     subject=ResourceSpec(name="mondo", prefix="MONDO", format="obo",
    ...                          path="data/align/mondo.obo"),
    ...     object=ResourceSpec(name="icd10cm", prefix="ICD10CM", format="owl",
    ...                         path="data/align/icd10cm.owl",
    ...                         iri_base="http://purl.bioontology.org/ontology/ICD10CM/"))
    >>> cfg.agent.model
    'claude-opus-5'
    >>> cfg.output_dir
    PosixPath('alignments/demo')
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from mondo_ai.align.agent import DEFAULT_MODEL

ResourceFormat = Literal["obo", "obojson", "owl", "sssom", "tsv", "yaml"]


class ResourceSpec(BaseModel):
    """How to load one resource.

    Attributes:
        name: Short name used in filenames and the report.
        prefix: CURIE prefix of the concepts to keep.
        format: Declared format; never sniffed.
        path: Local file path.
        url: Canonical download URL, recorded in provenance and used by the ``just``
            download recipes.
        iri_base: Required for ``owl``: the IRI prefix that ``prefix`` abbreviates.
        options: Extra keyword arguments passed straight to the adapter, for the tabular
            adapters' column names and similar.
    """

    name: str
    prefix: str
    format: ResourceFormat
    path: Path
    url: str = ""
    iri_base: str = ""
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _owl_needs_iri_base(self) -> ResourceSpec:
        """Reject an OWL spec with no IRI base.

        Example:
            >>> ResourceSpec(name="x", prefix="X", format="owl", path="x.owl")
            Traceback (most recent call last):
            ...
            pydantic_core._pydantic_core.ValidationError: ...
        """
        if self.format == "owl" and not self.iri_base:
            raise ValueError(
                f"resource {self.name!r} has format 'owl' but no iri_base; without it no "
                f"concept identifier can be contracted to a CURIE"
            )
        return self

    def adapter_kwargs(self) -> dict[str, Any]:
        """Return the keyword arguments for :func:`~mondo_ai.align.adapters.load_resource`.

        Example:
            >>> spec = ResourceSpec(name="m", prefix="MONDO", format="obo", path="m.obo",
            ...                     options={"keep_xref_prefixes": ["ICD10CM"]})
            >>> sorted(spec.adapter_kwargs())
            ['keep_xref_prefixes', 'name', 'path', 'prefix', 'source_url']
        """
        kwargs: dict[str, Any] = {
            "path": self.path,
            "prefix": self.prefix,
            "name": self.name,
            "source_url": self.url,
        }
        if self.format == "owl":
            kwargs["iri_base"] = self.iri_base
        kwargs.update(self.options)
        return kwargs


class CandidateConfig(BaseModel):
    """Candidate-pool shaping.

    Attributes:
        max_candidates: Global cap on the pool per subject.
        max_per_category: Cap per ICD three-character category, so laterality subcodes cannot
            crowd a general rubric out of its own candidate list.
        retrieval_limit: Token-retrieval hits considered before capping.
        enable_fuzzy: Generate edit-distance-1 neighbours. Costly on long labels.
        min_retrieval_score: Share of the subject's discriminative IDF mass a retrieval-only
            candidate must cover to be worth adjudicating. Below it the subject settles at
            rung ``D8`` with ``RJ-004`` (token overlap only) instead of joining the queue.
            Measured on the full Mondo x ICD-10-CM run, 0.3 removes 13% of the queue and
            costs 7 of 63 recoverable gold exact matches; 0.5 removes 54% and costs 22.
    """

    max_candidates: int = Field(default=12, ge=1)
    max_per_category: int = Field(default=3, ge=1)
    retrieval_limit: int = Field(default=25, ge=1)
    enable_fuzzy: bool = False
    min_retrieval_score: float = Field(default=0.3, ge=0.0, le=1.0)


class AgentConfig(BaseModel):
    """Agentic adjudication settings.

    Attributes:
        enabled: Whether to call a model at all. A run with this off is fully deterministic
            and still produces a complete system table, with everything unsettled queued as
            ``EXPERT_REVIEW``.
        model: Model id. This is the one real cost lever in the pipeline; a full
            Mondo × ICD-10-CM run queues tens of thousands of items.
        batch_size: Items per request.
        max_items: Cap on items sent to the model, highest-priority first. 0 means no cap.
        max_tokens: Output cap per request.
        effort: ``output_config.effort``.
        cache_dir: Response cache directory; empty disables caching.
        concurrency: Batches in flight at once. Adjudication is entirely latency-bound —
            each batch is one independent request — so this is the difference between a run
            that finishes in an hour and one that takes a day.
    """

    enabled: bool = True
    model: str = DEFAULT_MODEL
    batch_size: int = Field(default=15, ge=1)
    concurrency: int = Field(default=8, ge=1, le=32)
    max_items: int = Field(default=400, ge=0)
    max_tokens: int = Field(default=16000, ge=1024)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    cache_dir: str = ".align_cache"


class BoomerConfig(BaseModel):
    """Consistency conditioning with Boomer.

    Attributes:
        enabled: Whether to run Boomer at all. Off degrades to no Boomer columns, never to a
            failed run: it is an external tool and its absence is not an alignment error.
        executable: Path to the boomer binary. Empty searches PATH, then the conventional
            ``~/tools/boomer/bin/boomer``. A configured path that does not exist is an error
            rather than a silent fall back to a different binary.
        window_count: Boomer's ``--window-count``.
        runs: Boomer's ``--runs``; shuffled greedy restarts per clique.
        exhaustive_search_limit: Cliques at or below this size are searched exhaustively, so
            their scores — and therefore the max-marginals — are exact.
        subsequent_solutions: How many next-best solution scores to report per clique; this
            is what the per-clique confidence is computed from.
        marginals: Whether to run the second, pinned-variant pass that produces
            ``boomer_posterior``. Without it only priors and the MAP verdict are emitted.
        max_marginal_clique_size: Cliques larger than this are skipped by the marginal pass;
            its cost is quadratic in clique size.
        timeout_seconds: Per-invocation wall clock cap.
    """

    enabled: bool = True
    executable: str = ""
    window_count: int = Field(default=5, ge=1)
    runs: int = Field(default=3, ge=1)
    exhaustive_search_limit: int = Field(default=12, ge=2)
    subsequent_solutions: int = Field(default=3, ge=1)
    marginals: bool = True
    max_marginal_clique_size: int = Field(default=8, ge=2)
    timeout_seconds: int = Field(default=3600, ge=30)


class EvalConfig(BaseModel):
    """Evaluation against the subject resource's own asserted cross-references.

    Attributes:
        enabled: Whether to evaluate.
        gold_qualifier: The xref ``source`` qualifier that marks an asserted exact match.
        hold_out: Whether to withhold the gold set from candidate generation. Must stay
            True for an honest evaluation; exposed only so a production run can use the
            asserted mappings as proxy evidence.
    """

    enabled: bool = True
    gold_qualifier: str = "MONDO:equivalentTo"
    hold_out: bool = True


class SeedSpec(BaseModel):
    """One existing mapping set to reconcile against the run.

    Attributes:
        name: Short name, used in the ``evidence_sources`` and ``seed_predicates`` columns.
        path: An SSSOM TSV. Rows outside the run's subject/object prefixes are ignored.
        mapping_source: The seed set's identifier, written to ``mapping_source`` on its
            evidence-ledger rows.
        default_confidence: Used for rows that carry no ``confidence`` of their own.
    """

    name: str
    path: Path
    mapping_source: str = ""
    default_confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class ReconcileConfig(BaseModel):
    """Reconciliation of the run against seed mapping sets.

    Seeds are deliberately **not** inputs to discovery: the ladder and the evaluation never
    see them, so a seed pair the lexical evidence supports is supported independently. They
    meet the run only here, after it has settled, and every seed pair comes out with a
    status, a tier and its evidence.

    Attributes:
        enabled: Whether to reconcile at all.
        seeds: In trust order. When seeds disagree on a pair's predicate, the first one's
            predicate is proposed and the row goes to expert review.
        boomer: Condition the consolidated set on consistency as well, when Boomer itself is
            enabled. Rows Boomer overrules go to expert review.
    """

    enabled: bool = False
    seeds: list[SeedSpec] = Field(default_factory=list)
    boomer: bool = True


class ReviewConfig(BaseModel):
    """Review mode: an existing SSSOM file in, the same rows out with confidence columns.

    The run's ``subject``/``object`` resources supply labels and hierarchies, its
    ``agent`` block the model settings and its ``boomer`` block the solver settings; only
    what review adds lives here.

    Attributes:
        default_confidence: Prior for rows that carry no ``confidence`` of their own.
        agent: Whether the reviewing model runs at all.
        max_items: Rows sent to the reviewing model, most contested first. 0 means all.
            This is the cost lever: every row is one model judgement.
        hub_children: Only used when a cycle component exceeds ``max_box`` and has to be cut:
            a mapped concept that is the nearest mapped ancestor of more than this many
            mapped concepts does not fan out into halos.
        max_box: Hypotheses per box. Boxes are cycle components, which are exact; one
            larger than this is cut at its most generic hubs, then chunked, and every row
            says which box it was solved in and whether it was cut.
        halo_depth: For cut boxes only: links of neighbouring hypotheses solved alongside
            the core and never read back. 0 makes a cut a hard cut.
        bin_hypotheses: Boxes are renamed apart and packed into one Boomer invocation up to
            this many hypotheses, so a thousand small boxes are not a thousand JVM starts.
        workers: Boomer invocations in flight at once.
        bin_timeout_seconds: Wall clock per invocation. A bin that times out is retried box
            by box before its rows are given up as unsolved.
    """

    default_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    agent: bool = True
    max_items: int = Field(default=400, ge=0)
    hub_children: int = Field(default=40, ge=2)
    max_box: int = Field(default=150, ge=2)
    halo_depth: int = Field(default=1, ge=0, le=3)
    bin_hypotheses: int = Field(default=1500, ge=2)
    workers: int = Field(default=6, ge=1, le=32)
    bin_timeout_seconds: int = Field(default=900, ge=30)


class AlignmentConfig(BaseModel):
    """A complete alignment run.

    Attributes:
        run_id: Output directory name under ``alignments/``.
        subject: The resource whose terms are being mapped.
        object: The resource being mapped to.
        use_case: Recorded in the SSSOM set metadata. The use case determines what evidence
            is sufficient, so a set that does not state it is not reviewable.
        conflations_permitted: ``CM-*`` ids this run knowingly allows, declared up front.
        limit_subjects: Cap on subjects processed, for smoke runs. 0 means all.
        subject_id_file: Optional file of subject CURIEs, one per line, to restrict the run.
    """

    run_id: str
    subject: ResourceSpec
    object: ResourceSpec
    use_case: str = (
        "Knowledge-graph integration of disease concepts: exact matches must denote the "
        "same disease; broader and narrower relations are recorded with their true "
        "predicate rather than promoted."
    )
    conflations_permitted: list[str] = Field(default_factory=lambda: ["CM-001", "CM-002"])
    candidates: CandidateConfig = Field(default_factory=CandidateConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    boomer: BoomerConfig = Field(default_factory=BoomerConfig)
    evaluation: EvalConfig = Field(default_factory=EvalConfig)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    limit_subjects: int = Field(default=0, ge=0)
    subject_id_file: Path | None = None
    output_root: Path = Path("alignments")

    @field_validator("conflations_permitted")
    @classmethod
    def _known_conflations(cls, value: list[str]) -> list[str]:
        """Reject conflation ids that are not in the registry.

        Example:
            >>> AlignmentConfig(run_id="r", conflations_permitted=["CM-999"],
            ...     subject=ResourceSpec(name="a", prefix="A", format="obo", path="a"),
            ...     object=ResourceSpec(name="b", prefix="B", format="obo", path="b"))
            Traceback (most recent call last):
            ...
            pydantic_core._pydantic_core.ValidationError: ...
        """
        from mondo_ai.align.rules import known

        bad = [v for v in value if not known(v)]
        if bad:
            raise ValueError(f"unknown curation rule ids in conflations_permitted: {bad}")
        return value

    @property
    def output_dir(self) -> Path:
        """Return the run's output directory.

        Example:
            >>> AlignmentConfig(run_id="r",
            ...     subject=ResourceSpec(name="a", prefix="A", format="obo", path="a"),
            ...     object=ResourceSpec(name="b", prefix="B", format="obo", path="b")
            ... ).output_dir
            PosixPath('alignments/r')
        """
        return self.output_root / self.run_id

    @classmethod
    def from_yaml(cls, path: str | Path) -> AlignmentConfig:
        """Load a config from YAML.

        Example:
            >>> import tempfile, pathlib
            >>> y = '''run_id: demo
            ... subject: {name: mondo, prefix: MONDO, format: obo, path: m.obo}
            ... object: {name: icd, prefix: ICD10CM, format: owl, path: i.owl,
            ...          iri_base: "http://x/"}
            ... agent: {max_items: 10}
            ... '''
            >>> p = pathlib.Path(tempfile.mkdtemp()) / "c.yaml"
            >>> _ = p.write_text(y)
            >>> cfg = AlignmentConfig.from_yaml(p)
            >>> cfg.run_id, cfg.agent.max_items, cfg.object.iri_base
            ('demo', 10, 'http://x/')
        """
        with open(path, encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})

    def to_yaml(self) -> str:
        """Serialise the config, for the reproducibility record.

        Example:
            >>> cfg = AlignmentConfig(run_id="r",
            ...     subject=ResourceSpec(name="a", prefix="A", format="obo", path="a"),
            ...     object=ResourceSpec(name="b", prefix="B", format="obo", path="b"))
            >>> "run_id: r" in cfg.to_yaml()
            True
        """
        return yaml.safe_dump(
            yaml.safe_load(self.model_dump_json()), sort_keys=False, default_flow_style=False
        )
