"""Candidate generation: independent signals, one bounded pool per subject.

Each generator contributes :class:`~mondo_ai.align.models.Signal` objects to a shared pool.
Generators are deliberately independent — a candidate supported by a lexical hit *and* a
proxy mapping *and* hierarchical corroboration is stronger than one supported three times
by the same mechanism, and keeping the mechanisms separate is what lets the ladder and the
report say so.

The pool is capped twice: globally, and per ICD-10-CM three-character category. The second
cap exists because laterality subcodes multiply — ``M08.011``, ``M08.012``, … — and without
it a general rubric like ``M08 Juvenile arthritis`` gets crowded out of its own candidate
list by its grandchildren. That failure was observed in the ``map_icd10cm`` run.

Example:
    >>> category_key("ICD10CM:M08.011")
    'M08'
    >>> category_key("MONDO:0007947")
    'MONDO:0007947'
"""

from __future__ import annotations

from collections import defaultdict

from mondo_ai.align.flags import object_flags, possibly_coextensive
from mondo_ai.align.index import LexicalIndex
from mondo_ai.align.models import Candidate, Concept, ResourceSet, Signal
from mondo_ai.align.normalize import (
    base_normalize,
    broad_variants,
    content_tokens,
    edits1,
    exact_variants,
)
from mondo_ai.align.normalize import RULE_CERTAINTY as RULE_CERTAINTY_DEFAULT


#: Lexical tiers, lower is stronger. 9 means "retrieval only, no string identity".
TIER_RAW = 1
TIER_NORMALIZED = 2
TIER_SYNONYM = 3
TIER_VARIANT = 4
TIER_RETRIEVAL = 9

#: Evidence rule for a hit, by the pair of fields it crossed.
_FIELD_RULE = {
    ("label", "label"): "EV-001",
    ("label", "exactSynonym"): "EV-003",
    ("exactSynonym", "label"): "EV-003",
    ("exactSynonym", "exactSynonym"): "EV-003",
}


def category_key(object_id: str) -> str:
    """Return the grouping key used by the per-category candidate cap.

    For ICD-shaped identifiers this is the three-character category; anything else is its
    own key, which makes the cap a no-op for vocabularies without that structure.

    Example:
        >>> category_key("ICD10CM:E76.210")
        'E76'
        >>> category_key("ICD10CM:E76")
        'E76'
        >>> category_key("OMIM:143100")
        'OMIM:143100'
    """
    local = object_id.split(":", 1)[-1]
    if len(local) >= 3 and local[0].isalpha() and local[1:3].isdigit():
        return local[:3]
    return object_id


def _field_rule(subject_field: str, object_field: str) -> str:
    """Return the ``EV-*`` rule for a hit crossing these two fields.

    Example:
        >>> _field_rule("label", "label")
        'EV-001'
        >>> _field_rule("label", "exactSynonym")
        'EV-003'
        >>> _field_rule("relatedSynonym", "label")
        'EV-007'
    """
    return _FIELD_RULE.get((subject_field, object_field), "EV-007")


class CandidateGenerator:
    """Generate the candidate pool for subjects against one indexed object resource.

    Args:
        index: Finalised index over the object resource.
        objects: The object resource, for label and hierarchy lookups.
        object_prefix: Prefix used by flag detectors that are vocabulary-specific.
        max_candidates: Global cap on pool size per subject.
        max_per_category: Cap per ICD three-character category.
        retrieval_limit: How many token-retrieval hits to consider before capping.
        enable_fuzzy: Whether to generate edit-distance-1 neighbours. Costly on long
            labels; off by default for full-vocabulary runs.
        seed_mappings: Optional subject-id -> object-ids map providing ``EV-005`` proxy
            evidence. Deliberately empty for evaluation runs, so that held-out gold
            mappings cannot leak in as input.
    """

    def __init__(
        self,
        index: LexicalIndex,
        objects: ResourceSet,
        *,
        object_prefix: str = "",
        max_candidates: int = 12,
        max_per_category: int = 3,
        retrieval_limit: int = 25,
        enable_fuzzy: bool = False,
        seed_mappings: dict[str, list[str]] | None = None,
    ) -> None:
        self.index = index
        self.objects = objects
        self.object_prefix = object_prefix or objects.prefix
        self.max_candidates = max_candidates
        self.max_per_category = max_per_category
        self.retrieval_limit = retrieval_limit
        self.enable_fuzzy = enable_fuzzy
        self.seed_mappings = seed_mappings or {}
        self._flag_cache: dict[str, list[str]] = {}

    def flags_for(self, object_id: str) -> list[str]:
        """Return the object's flags, memoised across subjects.

        Objects recur across thousands of subjects, and the detectors are pure, so this
        cache is the difference between seconds and minutes on a full run.

        Example:
            >>> from mondo_ai.align.index import build_index
            >>> objs = ResourceSet(name="i", prefix="ICD10CM", concepts={
            ...     "ICD10CM:E80.29": Concept(id="ICD10CM:E80.29", label="Other porphyria")})
            >>> g = CandidateGenerator(build_index(list(objs.concepts.values())), objs)
            >>> g.flags_for("ICD10CM:E80.29")
            ['SC-001']
            >>> g.flags_for("ICD10CM:missing")
            []
        """
        cached = self._flag_cache.get(object_id)
        if cached is None:
            obj = self.objects.concepts.get(object_id)
            cached = object_flags(obj, self.object_prefix) if obj else []
            self._flag_cache[object_id] = cached
        return cached

    def generate(self, subject: Concept) -> list[Candidate]:
        """Return the capped candidate pool for one subject.

        Candidates are ordered by tier then by retrieval score, so the first entry is the
        strongest available evidence.

        Example:
            >>> from mondo_ai.align.index import build_index
            >>> objs = ResourceSet(name="i", prefix="ICD10CM", concepts={
            ...   "ICD10CM:Q87.4": Concept(id="ICD10CM:Q87.4", label="Marfan's syndrome"),
            ...   "ICD10CM:A00": Concept(id="ICD10CM:A00", label="Cholera")})
            >>> g = CandidateGenerator(build_index(list(objs.concepts.values())), objs)
            >>> pool = g.generate(Concept(id="MONDO:1", label="Marfan syndrome"))
            >>> [(c.object_id, c.tier) for c in pool]
            [('ICD10CM:Q87.4', 4)]
            >>> sorted(set(s.rule_id for s in pool[0].signals))
            ['EV-004', 'EV-006']
            >>> pool[0].transforms()
            ['add_possessive', 'strip_possessive']
        """
        # Every index lookup is iterated sorted: the index holds sets, and set order varies
        # between processes with string-hash randomisation, which made signal order — and so
        # subject_preprocessing and curation_rule — differ between two runs on the same inputs.
        pool: dict[str, Candidate] = {}

        def add(object_id: str, signal: Signal, tier: int, score: float = 0.0) -> None:
            if object_id not in self.objects.concepts:
                return
            cand = pool.get(object_id)
            if cand is None:
                cand = Candidate(object_id=object_id, flags=list(self.flags_for(object_id)))
                pool[object_id] = cand
            cand.signals.append(signal)
            cand.tier = min(cand.tier, tier)
            cand.score = max(cand.score, score)

        subject_strings = subject.all_strings()

        def related(s_field: str, o_field: str) -> bool:
            # EV-007: a string identity through a related (or broad, narrow, unqualified)
            # synonym. The registry says it is never sufficient alone, and mondo-ingest's
            # lexmatch rules score it closeMatch at weight 0, so it widens the pool as a
            # strong retrieval candidate rather than entering the string-identity tiers the
            # ladder settles on. Full score keeps it above the D8 viability floor.
            return _field_rule(s_field, o_field) == "EV-007"

        # --- tier 1-3: string identity -----------------------------------------------
        for value, s_field in subject_strings:
            for oid, o_field in sorted(self.index.lookup_raw(value)):
                if related(s_field, o_field):
                    add(oid, Signal("lexical_exact", "EV-007", 0.6, detail=value,
                                    subject_field=s_field, object_field=o_field),
                        TIER_RETRIEVAL, 1.0)
                    continue
                tier = TIER_RAW if (s_field == "label" and o_field == "label") else TIER_SYNONYM
                add(oid, Signal("lexical_exact", _field_rule(s_field, o_field), 1.0,
                                detail=value, subject_field=s_field, object_field=o_field),
                    tier)
            norm = base_normalize(value)
            if not norm:
                continue
            for oid, o_field in sorted(self.index.lookup_normalized(norm)):
                if related(s_field, o_field):
                    add(oid, Signal("lexical_exact", "EV-007", 0.6, detail=norm,
                                    subject_field=s_field, object_field=o_field),
                        TIER_RETRIEVAL, 1.0)
                    continue
                if s_field == "label" and o_field == "label":
                    tier, cert, rule_id = TIER_NORMALIZED, 0.97, "EV-002"
                else:
                    tier, cert, rule_id = TIER_SYNONYM, 0.92, _field_rule(s_field, o_field)
                add(oid, Signal("lexical_exact", rule_id, cert, detail=norm,
                                subject_field=s_field, object_field=o_field), tier)

        # --- tier 4: one declared meaning-preserving rewrite, either direction --------
        def variant(oid: str, detail: str, s_field: str, o_field: str, transform: str) -> None:
            if related(s_field, o_field):
                add(oid, Signal("lexical_variant", "EV-007", 0.5, detail=detail,
                                subject_field=s_field, object_field=o_field,
                                transform=transform), TIER_RETRIEVAL, 1.0)
                return
            add(oid, Signal("lexical_variant", "EV-004",
                            RULE_CERTAINTY_DEFAULT.get(transform, 0.85),
                            detail=detail, subject_field=s_field,
                            object_field=o_field, transform=transform),
                TIER_VARIANT)

        for value, s_field in subject_strings:
            norm = base_normalize(value)
            if not norm:
                continue
            for produced, transform in exact_variants(norm):
                for oid, o_field in sorted(self.index.lookup_normalized(produced)):
                    variant(oid, produced, s_field, o_field, transform)
            for oid, o_field, transform in sorted(self.index.lookup_variant(norm)):
                variant(oid, norm, s_field, o_field, transform)

        # --- pool widening: scope-changing rewrites, never evidence of sameness -------
        label_norm = base_normalize(subject.label)
        for produced, transform in broad_variants(label_norm):
            for oid, o_field in sorted(self.index.lookup_normalized(produced)):
                add(oid, Signal("broad_variant", "EV-006",
                                RULE_CERTAINTY_DEFAULT.get(transform, 0.7),
                                detail=produced, object_field=o_field,
                                transform=transform), TIER_RETRIEVAL)

        # --- proxy evidence via a third resource -------------------------------------
        for oid in self.seed_mappings.get(subject.id, []):
            add(oid, Signal("proxy", "EV-005", 0.9, detail=f"seed mapping to {oid}"),
                TIER_RETRIEVAL)

        # --- token retrieval ---------------------------------------------------------
        tokens = content_tokens(label_norm)
        for value, _ in subject_strings[1:]:
            tokens |= content_tokens(base_normalize(value))
        for oid, score in self.index.retrieve(tokens, limit=self.retrieval_limit):
            add(oid, Signal("token_retrieval", "EV-006", min(0.6, score),
                            detail=f"idf overlap {score:.2f}"), TIER_RETRIEVAL, score)

        # --- edit-distance-1 neighbours ----------------------------------------------
        if self.enable_fuzzy and label_norm and len(label_norm) <= 40:
            for neighbour in sorted(edits1(label_norm)):
                if not self.index.has_normalized(neighbour):
                    continue
                for oid, o_field in sorted(self.index.lookup_normalized(neighbour)):
                    add(oid, Signal("fuzzy_edit1", "EV-006", 0.5, detail=neighbour,
                                    object_field=o_field, transform="fuzzy_edit1_unique"),
                        TIER_RETRIEVAL)

        return self._cap(sorted(pool.values(), key=lambda c: (c.tier, -c.score, c.object_id)))

    def _cap(self, ordered: list[Candidate]) -> list[Candidate]:
        """Apply the global and per-category caps to an ordered pool.

        Example:
            >>> from mondo_ai.align.index import build_index
            >>> objs = ResourceSet(name="i", prefix="ICD10CM", concepts={})
            >>> g = CandidateGenerator(build_index([]), objs, max_candidates=2,
            ...                        max_per_category=1)
            >>> pool = [Candidate("ICD10CM:M08.011", tier=2),
            ...         Candidate("ICD10CM:M08.012", tier=2),
            ...         Candidate("ICD10CM:E84", tier=3)]
            >>> [c.object_id for c in g._cap(pool)]
            ['ICD10CM:M08.011', 'ICD10CM:E84']
        """
        per_cat: dict[str, int] = defaultdict(int)
        out: list[Candidate] = []
        for cand in ordered:
            key = category_key(cand.object_id)
            if per_cat[key] >= self.max_per_category:
                continue
            per_cat[key] += 1
            out.append(cand)
            if len(out) >= self.max_candidates:
                break
        return out

    def annotate_coextensive(self, subject: Concept, candidates: list[Candidate]) -> None:
        """Add the ``SC-007`` hint to residual candidates whose children cohere.

        Mutates the candidates in place: ``SC-007`` is appended alongside — never instead
        of — the residual flag, because the two are competing readings of the same object
        and the reviewer needs to see both.

        Example:
            >>> from mondo_ai.align.index import build_index
            >>> kids = {
            ...   "ICD10CM:E76.210": Concept(id="ICD10CM:E76.210",
            ...       label="Morquio A mucopolysaccharidoses"),
            ...   "ICD10CM:E76.219": Concept(id="ICD10CM:E76.219",
            ...       label="Morquio mucopolysaccharidoses, unspecified")}
            >>> rubric = Concept(id="ICD10CM:E76.21", parents=["ICD10CM:E76.2"],
            ...     label="Morquio mucopolysaccharidoses, unspecified",
            ...     children=list(kids))
            >>> sib = Concept(id="ICD10CM:E76.22", label="Sly mucopolysaccharidoses",
            ...               parents=["ICD10CM:E76.2"])
            >>> par = Concept(id="ICD10CM:E76.2", label="Other mucopolysaccharidoses",
            ...               children=["ICD10CM:E76.21", "ICD10CM:E76.22"])
            >>> objs = ResourceSet(name="i", prefix="ICD10CM", concepts={
            ...     **kids, rubric.id: rubric, sib.id: sib, par.id: par})
            >>> g = CandidateGenerator(build_index(list(objs.concepts.values())), objs)
            >>> cands = [Candidate("ICD10CM:E76.21", flags=["SC-002"])]
            >>> g.annotate_coextensive(
            ...     Concept(id="MONDO:1", label="mucopolysaccharidosis type IV"), cands)
            >>> cands[0].flags
            ['SC-002', 'SC-007']

            A candidate with no residual flag is left alone; there is nothing to reconsider.

            >>> clean = [Candidate("ICD10CM:E76.22", flags=[])]
            >>> g.annotate_coextensive(Concept(id="MONDO:1", label="Sly syndrome"), clean)
            >>> clean[0].flags
            []
        """
        for cand in candidates:
            if not any(f in ("SC-001", "SC-002") for f in cand.flags):
                continue
            obj = self.objects.concepts.get(cand.object_id)
            if obj is None:
                continue
            if possibly_coextensive(subject.label, obj, self.objects.concepts):
                if "SC-007" not in cand.flags:
                    cand.flags.append("SC-007")
