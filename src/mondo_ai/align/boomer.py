"""Boomer integration: consistency-conditioned probabilities for the mapping set.

The pipeline's own confidence is **local**. It looks at one subject, its candidates and its
flags, and says how sure it is. What it cannot see is that accepting this row makes another
row impossible — that two Mondo terms cannot both be exactly `A30.1`, or that a mapping into
a child code contradicts a mapping into its parent. Boomer can: it takes the mappings as
*hypotheses* with priors, the two ontologies' subclass axioms as *hard constraints*, and
returns the most probable assignment that is actually consistent.

So this module adds two columns whose meanings are worth stating precisely, because "prior"
and "posterior" are easy to print and easy to misread:

``boomer_prior``
    The probability this pipeline put on the relation it asserted, *before* Boomer saw it.
    Exactly the ptable cell that went in. Per mapping, exact.

``boomer_posterior``
    The probability of that same relation *after* conditioning on consistency, as a
    **max-marginal**: for each competing relation the mapping could take, the score of the
    best consistent assignment of its whole clique under that choice, normalised across the
    choices. Per mapping, exact for cliques Boomer searched exhaustively.

A max-marginal is not a sum-marginal. It answers "how much probability mass does the best
world containing this claim carry, against the best worlds containing its rivals" rather than
"summed over all worlds". Boomer 0.2 reports the MAP assignment and the scores of subsequent
solutions, not per-hypothesis sums, so the max-marginal is what its output actually supports;
calling it a marginal posterior without qualification would overstate it.

The interesting rows are the ones where the two disagree. A mapping whose prior was 0.85 and
whose posterior is 0.11 is one the pipeline liked and consistency rejected, and that is a
curation finding no amount of local confidence would have produced.

Example:
    >>> priors_for("skos:exactMatch", 0.9)
    (0.0333, 0.0333, 0.9, 0.0334)
    >>> round(sum(priors_for("skos:broadMatch", 0.6)), 6)
    1.0
    >>> priors_for("sssom:NoTermFound", 0.0)
    (0.0, 0.0, 0.0, 1.0)
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mondo_ai.align.models import (
    BROAD_MATCH,
    CLOSE_MATCH,
    EXACT_MATCH,
    NARROW_MATCH,
    RELATED_MATCH,
    MappingRecord,
    ResourceSet,
)

#: Boomer's ptable relation columns, in file order.
RELATIONS = ("subclass", "superclass", "equivalent", "other")

#: Boomer's markdown relation names -> the ptable column they correspond to. Direction:
#: Boomer writes `A ProperSubClassOf B` with the ptable's subject first, so `subclass` means
#: the SUBJECT is narrower, which is SSSOM's `skos:broadMatch` (the object is broader).
BOOMER_RELATION = {
    "ProperSubClassOf": "subclass",
    "ProperSuperClassOf": "superclass",
    "EquivalentTo": "equivalent",
    "SiblingOf": "other",
}

#: SSSOM predicate -> the ptable column that predicate asserts.
PREDICATE_RELATION = {
    EXACT_MATCH: "equivalent",
    BROAD_MATCH: "subclass",     # object is broader => subject ⊂ object
    NARROW_MATCH: "superclass",  # object is narrower => object ⊂ subject
    CLOSE_MATCH: "other",
    RELATED_MATCH: "other",
}

#: The ptable column -> the SSSOM predicate it licenses, for reading Boomer's verdict back.
RELATION_PREDICATE = {
    "equivalent": EXACT_MATCH,
    "subclass": BROAD_MATCH,
    "superclass": NARROW_MATCH,
    "other": CLOSE_MATCH,
}


def priors_for(predicate: str, confidence: float) -> tuple[float, float, float, float]:
    """Turn one asserted predicate and confidence into a ptable prior distribution.

    The asserted relation takes the confidence; the remainder is split evenly over the other
    three. Even splitting is deliberate: the pipeline has an opinion about *which* relation
    holds and how sure it is, and no opinion at all about the ranking of the alternatives it
    rejected. Inventing one would put structure into the prior that no evidence supports, and
    Boomer would faithfully propagate it.

    Args:
        predicate: The SSSOM predicate asserted, or ``sssom:NoTermFound``.
        confidence: Confidence in [0,1].

    Returns:
        ``(p_subclass, p_superclass, p_equivalent, p_other)``, summing to 1.0.

    Example:
        >>> priors_for("skos:exactMatch", 0.99)
        (0.0033, 0.0033, 0.99, 0.0034)
        >>> priors_for("skos:narrowMatch", 0.7)
        (0.1, 0.7, 0.1, 0.1)
        >>> priors_for("skos:broadMatch", 0.7)
        (0.7, 0.1, 0.1, 0.1)

        A recorded absence puts all its mass on "no relation", which is what lets Boomer
        treat a `NoTermFound` row as evidence rather than as a gap:

        >>> priors_for("sssom:NoTermFound", 0.5)
        (0.0, 0.0, 0.0, 1.0)
    """
    relation = PREDICATE_RELATION.get(predicate)
    if relation is None:
        return (0.0, 0.0, 0.0, 1.0)
    confidence = max(0.0, min(1.0, confidence))
    rest = round((1.0 - confidence) / 3.0, 4)
    values = {r: rest for r in RELATIONS}
    values[relation] = confidence
    # Absorb the rounding residue into "other" so the row sums to exactly 1.0; a ptable row
    # that does not sum to 1 is silently renormalised by Boomer, which hides the error.
    drift = round(1.0 - sum(values.values()), 6)
    values["other"] = round(values["other"] + drift, 6)
    return (values["subclass"], values["superclass"], values["equivalent"], values["other"])


@dataclass(slots=True)
class BoomerVerdict:
    """What Boomer concluded about one mapping.

    Attributes:
        subject_id: Subject CURIE.
        object_id: Object CURIE.
        relation: The ptable relation Boomer chose.
        prior: The prior of that relation, as supplied.
        posterior: Max-marginal posterior of that relation, or None when not computed.
        was_most_probable: Whether the chosen relation was also the prior-maximal one. False
            means consistency overrode the pipeline's preference, which is the signal.
        clique_id: Identifier of the clique it was resolved in.
        clique_probability: Boomer's estimated probability of the clique's MAP solution.
        clique_confidence: ``P(best) / (P(best) + P(next best))`` for the clique.
        clique_size: Number of mappings in the clique.
        method: ``exhaustive search``, ``greedy search`` or ``singletons``.
    """

    subject_id: str
    object_id: str
    relation: str
    prior: float
    posterior: float | None = None
    was_most_probable: bool = True
    clique_id: str = ""
    clique_probability: float = 1.0
    clique_confidence: float = 1.0
    clique_size: int = 1
    method: str = "singletons"

    @property
    def predicate(self) -> str:
        """The SSSOM predicate Boomer's chosen relation licenses.

        Example:
            >>> BoomerVerdict("M:1", "I:1", "subclass", 0.7).predicate
            'skos:broadMatch'
            >>> BoomerVerdict("M:1", "I:1", "equivalent", 0.7).predicate
            'skos:exactMatch'
        """
        return RELATION_PREDICATE.get(self.relation, CLOSE_MATCH)

    @property
    def overruled(self) -> bool:
        """Whether consistency conditioning moved this mapping off its prior-best relation.

        Example:
            >>> BoomerVerdict("M:1", "I:1", "subclass", 0.1, was_most_probable=False).overruled
            True
        """
        return not self.was_most_probable


# --- inputs ------------------------------------------------------------------------------


def write_prefixes(path: Path, prefixes: dict[str, str]) -> None:
    """Write Boomer's prefix YAML.

    Boomer also uses these namespaces to detect *new within-namespace equivalences*, which is
    how it notices that a solution would merge two Mondo terms. Getting them right is what
    makes the reverse-proxy-merge constraint work at all.

    Example:
        >>> import tempfile, pathlib, yaml
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "p.yaml"
        >>> write_prefixes(p, {"MONDO": "http://x/MONDO_"})
        >>> yaml.safe_load(p.read_text())
        {'MONDO': 'http://x/MONDO_'}
    """
    import yaml

    path.write_text(yaml.safe_dump(prefixes, sort_keys=True), encoding="utf-8")


def write_ptable(path: Path, rows: list[tuple[str, str, tuple[float, ...]]]) -> int:
    """Write the ptable Boomer reads.

    Returns:
        Rows written.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "pt.tsv"
        >>> write_ptable(p, [("M:1", "I:A00", (0.03, 0.03, 0.9, 0.04))])
        1
        >>> p.read_text().strip().split("\\t")
        ['M:1', 'I:A00', '0.03', '0.03', '0.9', '0.04']
    """
    with open(path, "w", encoding="utf-8") as fh:
        for subject, obj, probs in rows:
            fh.write(f"{subject}\t{obj}\t" + "\t".join(str(p) for p in probs) + "\n")
    return len(rows)


def write_background_ontology(
    path: Path,
    subjects: ResourceSet,
    objects: ResourceSet,
    subject_iri: str,
    object_iri: str,
    signature: set[str],
) -> int:
    """Write the background OWL: the subclass axioms that make the constraints bite.

    Restricted to the signature actually mapped plus one ancestor hop, because Boomer only
    needs the axioms that can interact with a hypothesis, and handing it all 97,903
    ICD-10-CM classes would make every clique the whole vocabulary.

    Args:
        signature: CURIEs appearing in the ptable.

    Returns:
        Number of classes declared.

    Example:
        >>> import tempfile, pathlib
        >>> from mondo_ai.align.models import Concept
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:1": Concept(id="M:1", label="a"),
        ...     "M:2": Concept(id="M:2", label="b", parents=["M:1"])})
        >>> o = ResourceSet(name="i", prefix="I", concepts={
        ...     "I:A": Concept(id="I:A", label="c"),
        ...     "I:A.0": Concept(id="I:A.0", label="d", parents=["I:A"])})
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "bg.owl"
        >>> write_background_ontology(p, s, o, "http://x/M_", "http://y/I/",
        ...                           {"M:2", "I:A.0"})
        4
        >>> "http://x/M_2" in p.read_text() and "http://y/I/A" in p.read_text()
        True
    """
    wanted: set[str] = set()
    for rs in (subjects, objects):
        for cid in signature:
            concept = rs.concepts.get(cid)
            if concept is None:
                continue
            wanted.add(cid)
            wanted.update(concept.parents)
            wanted.update(concept.children)

    def iri(cid: str) -> str | None:
        prefix, _, local = cid.partition(":")
        if prefix == subjects.prefix:
            return f"{subject_iri}{local}"
        if prefix == objects.prefix:
            return f"{object_iri}{local}"
        return None

    lines = [
        '<?xml version="1.0"?>',
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"',
        '  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"',
        '  xmlns:owl="http://www.w3.org/2002/07/owl#"',
        '  xml:base="https://w3id.org/mondo-ai/align/boomer-background">',
        '  <owl:Ontology rdf:about='
        '"https://w3id.org/mondo-ai/align/boomer-background"/>',
    ]
    declared = 0
    for rs in (subjects, objects):
        for cid in sorted(wanted):
            concept = rs.concepts.get(cid)
            if concept is None:
                continue
            uri = iri(cid)
            if uri is None:
                continue
            parents = [p for p in concept.parents if p in wanted and iri(p)]
            if parents:
                lines.append(f'  <owl:Class rdf:about="{uri}">')
                for p in parents:
                    lines.append(
                        f'    <rdfs:subClassOf rdf:resource="{iri(p)}"/>')
                lines.append("  </owl:Class>")
            else:
                lines.append(f'  <owl:Class rdf:about="{uri}"/>')
            declared += 1
    lines.append("</rdf:RDF>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return declared


# --- output parsing ----------------------------------------------------------------------

_CLIQUE_HEADER = re.compile(r"^## (.+)$")
_FIELD = re.compile(r"^(Method|Score|Estimated probability|Confidence|Subsequent scores[^:]*):"
                    r"\s*(.*)$")
_MAPPING_LINE = re.compile(
    r"^- \[(?P<subject>[^\]]+)\]\([^)]*\)\s+(?P<relation>\w+)\s+"
    r"\[(?P<object>[^\]]+)\]\([^)]*\)\s*(?P<marker>\(most probable\))?\s*"
    r"(?P<probability>[0-9.eE+-]+)\s*$"
)


@dataclass(slots=True)
class Clique:
    """One resolved clique as Boomer reported it.

    Attributes:
        id: The clique heading, with markdown links reduced to CURIEs.
        method: Search method used.
        score: Log score of the MAP solution.
        probability: Boomer's estimated probability of the MAP solution.
        confidence: ``P(best) / (P(best) + P(next best))``.
        subsequent_scores: Log scores of the next-best distinct solutions.
        verdicts: One per mapping in the clique.
    """

    id: str
    method: str = ""
    score: float = 0.0
    probability: float = 1.0
    confidence: float = 1.0
    subsequent_scores: list[float] = field(default_factory=list)
    verdicts: list[BoomerVerdict] = field(default_factory=list)

    @property
    def is_singleton_group(self) -> bool:
        """Whether this is Boomer's catch-all group of non-interacting mappings.

        Example:
            >>> Clique(id="SINGLETONS").is_singleton_group
            True
            >>> Clique(id="MONDO:1 ICD10CM:A00").is_singleton_group
            False
        """
        return self.id.strip().upper() == "SINGLETONS"


def _curies(heading: str) -> str:
    """Reduce a markdown clique heading to a space-separated CURIE list.

    Example:
        >>> _curies("[ICD10CM:B00](http://x) [MONDO:0000001](http://y)")
        'ICD10CM:B00 MONDO:0000001'
        >>> _curies("SINGLETONS")
        'SINGLETONS'
    """
    found = re.findall(r"\[([^\]]+)\]\([^)]*\)", heading)
    return " ".join(found) if found else heading.strip()


def parse_markdown(text: str) -> list[Clique]:
    """Parse Boomer's markdown report into cliques and per-mapping verdicts.

    Example:
        >>> md = '''## [MONDO:0000001](http://a) [ICD10CM:B00](http://b)
        ... Method: exhaustive search
        ... Score: -2.688
        ... Estimated probability: 0.8
        ... Confidence: 0.888
        ... Subsequent scores (max 3): -4.76, -5.46
        ...
        ... - [MONDO:0000001](http://a) ProperSuperClassOf [ICD10CM:B00.1](http://c)\t\t0.1
        ... - [MONDO:0000002](http://d) EquivalentTo [ICD10CM:B00.1](http://c)\t(most probable)\t0.8
        ...
        ... ## SINGLETONS
        ... Method: singletons
        ... Score: 0.0
        ... Estimated probability: 1.0
        ... Confidence: 1.0
        ... Subsequent scores (max 3):
        ... '''
        >>> cliques = parse_markdown(md)
        >>> len(cliques), cliques[0].method, cliques[0].probability
        (2, 'exhaustive search', 0.8)
        >>> cliques[0].subsequent_scores
        [-4.76, -5.46]
        >>> v = cliques[0].verdicts[0]
        >>> v.subject_id, v.relation, v.prior, v.was_most_probable
        ('MONDO:0000001', 'superclass', 0.1, False)
        >>> cliques[0].verdicts[1].was_most_probable
        True
        >>> cliques[1].is_singleton_group
        True
    """
    cliques: list[Clique] = []
    current: Clique | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        header = _CLIQUE_HEADER.match(line)
        if header:
            current = Clique(id=_curies(header.group(1)))
            cliques.append(current)
            continue
        if current is None:
            continue
        field_match = _FIELD.match(line)
        if field_match:
            key, value = field_match.group(1), field_match.group(2).strip()
            if key == "Method":
                current.method = value
            elif key == "Score":
                current.score = _float(value, 0.0)
            elif key == "Estimated probability":
                current.probability = _float(value, 1.0)
            elif key == "Confidence":
                current.confidence = _float(value, 1.0)
            elif key.startswith("Subsequent scores"):
                current.subsequent_scores = [
                    _float(v.strip(), 0.0) for v in value.split(",") if v.strip()
                ]
            continue
        mapping = _MAPPING_LINE.match(line)
        if mapping:
            current.verdicts.append(BoomerVerdict(
                subject_id=mapping.group("subject"),
                object_id=mapping.group("object"),
                relation=BOOMER_RELATION.get(mapping.group("relation"), "other"),
                prior=_float(mapping.group("probability"), 0.0),
                was_most_probable=bool(mapping.group("marker")),
                clique_id=current.id,
                clique_probability=current.probability,
                clique_confidence=current.confidence,
                method=current.method,
            ))
    for clique in cliques:
        # SINGLETONS is Boomer's bag of mappings that interact with nothing, not a clique of
        # 1,956 members. Reporting its length as a clique size would make every unconstrained
        # row look like it sat in the largest clique in the run.
        size = 1 if clique.is_singleton_group else len(clique.verdicts)
        for verdict in clique.verdicts:
            verdict.clique_size = size
    return cliques


def _float(value: str, default: float) -> float:
    """Parse a float, falling back rather than failing on Boomer's formatting.

    Example:
        >>> _float("0.8", 1.0), _float("", 1.0), _float("NaN", 0.0)
        (0.8, 1.0, 0.0)
    """
    try:
        out = float(value)
    except ValueError:
        return default
    return default if math.isnan(out) else out


# --- running -----------------------------------------------------------------------------

DEFAULT_BOOMER = "boomer"


def find_boomer(explicit: str = "") -> str | None:
    """Locate the Boomer executable.

    An explicitly configured path is used or nothing is: silently substituting a different
    binary for the one the config named would make the run's provenance a lie.

    Example:
        >>> find_boomer("definitely-not-on-path-12345") is None
        True
        >>> find_boomer("sh") is not None
        True
    """
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        return str(Path(explicit)) if Path(explicit).is_file() else None
    found = shutil.which(DEFAULT_BOOMER)
    if found:
        return found
    # Conventional install location used by the ODK/Monarch tooling.
    guess = Path.home() / "tools" / "boomer" / "bin" / "boomer"
    return str(guess) if guess.is_file() else None


@dataclass
class BoomerRun:
    """One Boomer invocation and its parsed result.

    Attributes:
        cliques: Parsed cliques.
        verdicts: ``(subject_id, object_id)`` -> verdict.
        command: The command line used.
        returncode: Boomer's exit status.
        stderr_tail: Last lines of Boomer's output, for diagnosing a failure.
        n_ptable_rows: Hypotheses submitted.
        n_classes: Background classes declared.
    """

    cliques: list[Clique] = field(default_factory=list)
    verdicts: dict[tuple[str, str], BoomerVerdict] = field(default_factory=dict)
    command: str = ""
    returncode: int = 0
    stderr_tail: str = ""
    n_ptable_rows: int = 0
    n_classes: int = 0

    @property
    def ok(self) -> bool:
        """Whether Boomer ran and produced verdicts.

        Example:
            >>> BoomerRun().ok
            False
            >>> BoomerRun(verdicts={("M:1", "I:1"): BoomerVerdict("M:1", "I:1", "equivalent",
            ...                                                   0.9)}).ok
            True
        """
        return self.returncode == 0 and bool(self.verdicts)

    def interacting_cliques(self) -> list[Clique]:
        """Return the cliques where mappings actually constrain one another.

        Singletons are where prior and posterior coincide by construction, so they are not
        worth a second solver pass.

        Example:
            >>> r = BoomerRun(cliques=[Clique(id="SINGLETONS"),
            ...                        Clique(id="A B", verdicts=[
            ...                            BoomerVerdict("M:1", "I:1", "equivalent", 0.9),
            ...                            BoomerVerdict("M:2", "I:1", "equivalent", 0.8)])])
            >>> [c.id for c in r.interacting_cliques()]
            ['A B']
        """
        return [
            c for c in self.cliques
            if not c.is_singleton_group and len(c.verdicts) > 1
        ]


def run_boomer(
    work_dir: Path,
    ptable_rows: list[tuple[str, str, tuple[float, ...]]],
    subjects: ResourceSet,
    objects: ResourceSet,
    *,
    subject_iri: str,
    object_iri: str,
    ontology_path: Path | None = None,
    executable: str = "",
    window_count: int = 5,
    runs: int = 3,
    exhaustive_search_limit: int = 12,
    subsequent_solutions: int = 3,
    timeout: int = 3600,
    log=None,
) -> BoomerRun:
    """Write Boomer's inputs, invoke it, and parse its report.

    Args:
        ontology_path: A background ontology to use as-is. Supplied by the max-marginal pass,
            whose renamed variant entities do not exist in either resource and so cannot be
            derived from them.

    Returns a :class:`BoomerRun` with ``returncode`` set even on failure: Boomer is an
    external tool and a missing binary or an unsolvable input must degrade the run to
    "no Boomer columns", never abort an alignment that is otherwise complete.
    """
    # Absolute, because Boomer runs with cwd set to the work directory and would otherwise
    # resolve these relative paths a second time against it.
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    binary = find_boomer(executable)
    if binary is None:
        return BoomerRun(
            returncode=127,
            stderr_tail=(
                "boomer executable not found. Install it and put it on PATH, or set "
                "boomer.executable in the run config. Boomer columns are omitted."
            ),
        )

    ptable = work_dir / "mappings.ptable.tsv"
    prefixes = work_dir / "prefixes.yaml"
    background = work_dir / "background.owl"
    out_base = work_dir / "boomer"

    n_rows = write_ptable(ptable, ptable_rows)
    write_prefixes(prefixes, {subjects.prefix: subject_iri, objects.prefix: object_iri})
    if ontology_path is not None:
        background = ontology_path.resolve()
        n_classes = background.read_text(encoding="utf-8").count("<owl:Class ")
    else:
        signature = {s for s, _, _ in ptable_rows} | {o for _, o, _ in ptable_rows}
        n_classes = write_background_ontology(
            background, subjects, objects, subject_iri, object_iri, signature)

    command = [
        binary,
        "--ptable", str(ptable),
        "--ontology", str(background),
        "--prefixes", str(prefixes),
        "--output", str(out_base),
        "--window-count", str(window_count),
        "--runs", str(runs),
        "--exhaustive-search-limit", str(exhaustive_search_limit),
        "--subsequent-solutions", str(subsequent_solutions),
    ]
    if log:
        log(f"    boomer: {n_rows:,} hypotheses, {n_classes:,} background classes")

    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, cwd=str(work_dir)
        )
    except subprocess.TimeoutExpired:
        return BoomerRun(
            command=" ".join(command), returncode=124, n_ptable_rows=n_rows,
            n_classes=n_classes,
            stderr_tail=f"boomer timed out after {timeout}s; Boomer columns are omitted.",
        )

    report = out_base.with_suffix(".md")
    if proc.returncode != 0 or not report.exists():
        return BoomerRun(
            command=" ".join(command), returncode=proc.returncode or 1,
            n_ptable_rows=n_rows, n_classes=n_classes,
            stderr_tail="\n".join(
                (proc.stderr or proc.stdout or "no output").splitlines()[-12:]) or
            f"boomer exited {proc.returncode} and wrote no report at {report}",
        )

    cliques = parse_markdown(report.read_text(encoding="utf-8"))
    verdicts = {(v.subject_id, v.object_id): v for c in cliques for v in c.verdicts}
    return BoomerRun(
        cliques=cliques, verdicts=verdicts, command=" ".join(command),
        returncode=0, n_ptable_rows=n_rows, n_classes=n_classes,
    )


# --- posteriors --------------------------------------------------------------------------


def max_marginal(scores: dict[str, float]) -> dict[str, float]:
    """Normalise per-relation best-solution log scores into a max-marginal distribution.

    Args:
        scores: Relation -> log score of the best consistent assignment under that choice.
            A relation that admits no consistent assignment is omitted, not given -inf.

    Returns:
        Relation -> probability, summing to 1.0 over the relations offered.

    Example:
        >>> out = max_marginal({"equivalent": math.log(0.8), "subclass": math.log(0.2)})
        >>> round(out["equivalent"], 4), round(out["subclass"], 4)
        (0.8, 0.2)
        >>> max_marginal({"equivalent": -1.0})
        {'equivalent': 1.0}
        >>> max_marginal({})
        {}

        Log scores are shifted before exponentiating, so a clique whose scores are all very
        negative does not underflow to a uniform distribution:

        >>> out = max_marginal({"a": -800.0, "b": -801.0})
        >>> round(out["a"], 3)
        0.731
    """
    if not scores:
        return {}
    top = max(scores.values())
    weights = {r: math.exp(s - top) for r, s in scores.items()}
    total = sum(weights.values())
    if total <= 0:
        return {r: 1.0 / len(scores) for r in scores}
    return {r: w / total for r, w in weights.items()}


def pinned_variants(
    clique: Clique,
    priors: dict[tuple[str, str], tuple[float, ...]],
) -> list[tuple[str, tuple[str, str], str]]:
    """Enumerate the pinned re-solves needed for a clique's max-marginals.

    For each mapping and each relation it has non-negligible prior mass on, one variant pins
    that mapping to that relation and leaves the rest of the clique free. Relations the
    pipeline gave no mass are skipped: re-solving to discover that an impossible option stays
    impossible is a wasted solver call.

    Returns:
        ``(variant_tag, (subject, object), relation)`` triples.

    Example:
        >>> c = Clique(id="x", verdicts=[BoomerVerdict("M:1", "I:1", "equivalent", 0.9)])
        >>> pr = {("M:1", "I:1"): (0.0333, 0.0333, 0.9, 0.0334)}
        >>> [(t, r) for t, _, r in pinned_variants(c, pr)]
        [('v0', 'subclass'), ('v1', 'superclass'), ('v2', 'equivalent'), ('v3', 'other')]
        >>> pr2 = {("M:1", "I:1"): (0.0, 0.0, 1.0, 0.0)}
        >>> [(t, r) for t, _, r in pinned_variants(c, pr2)]
        [('v0', 'equivalent')]
    """
    out: list[tuple[str, tuple[str, str], str]] = []
    n = 0
    for verdict in clique.verdicts:
        key = (verdict.subject_id, verdict.object_id)
        row = priors.get(key)
        if row is None:
            continue
        for index, relation in enumerate(RELATIONS):
            if row[index] <= 0.0:
                continue
            out.append((f"v{n}", key, relation))
            n += 1
    return out


#: Minimum share of a mapping's prior mass that must have been scored before a posterior is
#: emitted. Below this, normalising over the surviving relations yields a number that is not
#: comparable to the prior, and an incomparable posterior in a column called
#: ``boomer_posterior`` is worse than an empty cell.
MIN_POSTERIOR_COVERAGE = 0.9


def attach_to_records(
    records: list[MappingRecord],
    run: BoomerRun,
    priors: dict[tuple[str, str], tuple[float, ...]],
    posteriors: dict[tuple[str, str], dict[str, float]] | None = None,
    coverage: dict[tuple[str, str], float] | None = None,
    *,
    min_coverage: float = MIN_POSTERIOR_COVERAGE,
) -> int:
    """Write Boomer's findings onto the records, returning how many were annotated.

    A record whose mapping Boomer did not see is left untouched rather than given a zero: an
    absent probability and a probability of zero are different claims.

    Example:
        >>> from mondo_ai.align.models import Adjudication
        >>> rec = MappingRecord("M:1", final=Adjudication(
        ...     "agent", "exact", "I:A00", 0.9, decision_path="A1"))
        >>> v = BoomerVerdict("M:1", "I:A00", "subclass", 0.0333, was_most_probable=False,
        ...                   clique_id="c1", clique_probability=0.7, clique_confidence=0.6,
        ...                   clique_size=3, method="exhaustive search")
        >>> run = BoomerRun(returncode=0, verdicts={("M:1", "I:A00"): v})
        >>> priors = {("M:1", "I:A00"): (0.0333, 0.0333, 0.9, 0.0334)}
        >>> post = {("M:1", "I:A00"): {"equivalent": 0.11, "subclass": 0.89}}
        >>> attach_to_records([rec], run, priors, post, {("M:1", "I:A00"): 1.0})
        1
        >>> b = rec.boomer
        >>> b["prior"], b["posterior"], b["overruled"]
        (0.9, 0.11, True)
        >>> b["relation"], b["predicate"], b["clique_size"], b["constrained"]
        ('subclass', 'skos:broadMatch', 3, True)

        Thin coverage suppresses the posterior rather than reporting an incomparable one:

        >>> _ = attach_to_records([rec], run, priors, post, {("M:1", "I:A00"): 0.5})
        >>> rec.boomer["posterior"], rec.boomer["posterior_coverage"]
        (None, 0.5)

        An unconstrained mapping's posterior is its prior, and is labelled as unconstrained:

        >>> free = BoomerVerdict("M:2", "I:A01", "equivalent", 0.9, clique_size=1,
        ...                      method="singletons")
        >>> rec2 = MappingRecord("M:2", final=Adjudication(
        ...     "deterministic", "exact", "I:A01", 0.9, decision_path="D2"))
        >>> _ = attach_to_records([rec2], BoomerRun(verdicts={("M:2", "I:A01"): free}),
        ...                       {("M:2", "I:A01"): (0.0333, 0.0333, 0.9, 0.0334)})
        >>> rec2.boomer["prior"], rec2.boomer["posterior"], rec2.boomer["constrained"]
        (0.9, 0.9, False)
    """
    posteriors = posteriors or {}
    annotated = 0
    for record in records:
        final = record.final
        if final is None or not final.object_id:
            continue
        key = (record.subject_id, final.object_id)
        verdict = run.verdicts.get(key)
        if verdict is None:
            continue
        prior_row = priors.get(key)
        asserted = PREDICATE_RELATION.get(final.predicate)
        # The prior reported is the one for the relation the pipeline asserted, not the one
        # Boomer chose: the column's job is to show what conditioning did to our own claim.
        prior = (
            prior_row[RELATIONS.index(asserted)]
            if prior_row is not None and asserted in RELATIONS
            else verdict.prior
        )
        marginal = posteriors.get(key, {})
        scored = (coverage or {}).get(key, 1.0 if marginal else 0.0)
        if scored < min_coverage:
            marginal = {}
        posterior = marginal.get(asserted) if asserted else None

        # A mapping Boomer placed in its singleton group interacts with nothing, so no
        # consistent assignment of anything else can change it and its posterior *is* its
        # prior. Saying so is the definition, not a stand-in for a computation that did not
        # run — and leaving 92% of the column empty would make it look broken instead of
        # making the unconstrained majority visible.
        unconstrained = verdict.clique_size <= 1 and verdict.method == "singletons"
        if posterior is None and unconstrained and asserted:
            posterior = prior
            scored = 1.0
        record.boomer = {
            "prior": round(prior, 6),
            "posterior": round(posterior, 6) if posterior is not None else None,
            "relation": verdict.relation,
            "predicate": verdict.predicate,
            "agrees_with_pipeline": verdict.predicate == final.predicate,
            "overruled": verdict.overruled,
            "clique_id": verdict.clique_id,
            "clique_size": verdict.clique_size,
            "clique_probability": round(verdict.clique_probability, 6),
            "clique_confidence": round(verdict.clique_confidence, 6),
            "method": verdict.method,
            "marginal": {k: round(v, 6) for k, v in sorted(marginal.items())} or None,
            "posterior_coverage": round(scored, 6),
            "constrained": not unconstrained,
        }
        annotated += 1
    return annotated


#: Entities in a pinned variant get this suffix so variants cannot interact with each other
#: or with the original clique. It stays inside the local part of the CURIE, so the prefix map
#: still expands it and Boomer's within-namespace equivalence check still applies.
VARIANT_SEP = "__mai"

#: Cliques larger than this are skipped by the marginal pass. The pinned-variant cost is
#: quadratic in clique size, and a 30-mapping clique is a curation problem to look at directly
#: rather than a distribution to estimate.
MAX_MARGINAL_CLIQUE_SIZE = 8


def variant_curie(curie: str, tag: str) -> str:
    """Suffix a CURIE's local part for a pinned variant.

    Example:
        >>> variant_curie("MONDO:0000001", "v3")
        'MONDO:0000001__maiv3'
        >>> variant_curie("ICD10CM:A00.0", "v0")
        'ICD10CM:A00.0__maiv0'
    """
    prefix, _, local = curie.partition(":")
    return f"{prefix}:{local}{VARIANT_SEP}{tag}"


def base_curie(curie: str) -> str:
    """Strip a variant suffix, returning the original CURIE.

    Example:
        >>> base_curie("MONDO:0000001__maiv3")
        'MONDO:0000001'
        >>> base_curie("MONDO:0000001")
        'MONDO:0000001'
    """
    head, sep, _ = curie.partition(VARIANT_SEP)
    return head if sep else curie


#: Prior mass a pinned variant puts on the forced relation. Its log is subtracted back out
#: when the marginal is computed, so the pin acts purely as a constraint and contributes no
#: probability of its own.
PIN_MASS = 0.9997


def pinned_priors(relation: str) -> tuple[float, float, float, float]:
    """Return the prior row that pins a mapping to one relation.

    Not a hard 1.0/0.0: Boomer works in log space, and a zero prior on the alternatives makes
    the whole assignment's score -inf as soon as consistency forces one of them, which loses
    the ranking this pass exists to produce. A near-certain pin preserves the ordering while
    leaving the arithmetic finite.

    Example:
        >>> pinned_priors("equivalent")
        (0.0001, 0.0001, 0.9997, 0.0001)
        >>> round(sum(pinned_priors("subclass")), 6)
        1.0
        >>> pinned_priors("subclass")[0]
        0.9997
    """
    values = {r: 0.0001 for r in RELATIONS}
    values[relation] = PIN_MASS
    return (values["subclass"], values["superclass"], values["equivalent"], values["other"])


def build_marginal_inputs(
    cliques: list[Clique],
    priors: dict[tuple[str, str], tuple[float, ...]],
    *,
    max_clique_size: int = MAX_MARGINAL_CLIQUE_SIZE,
) -> tuple[list[tuple[str, str, tuple[float, ...]]], dict[str, tuple[tuple[str, str], str]]]:
    """Build the pinned-variant ptable for the max-marginal pass.

    Each variant is a renamed copy of one clique with exactly one mapping pinned to one
    relation. Because every copy uses distinct entity IRIs, Boomer partitions them into
    independent cliques and one invocation scores them all.

    Returns:
        ``(ptable_rows, index)`` where ``index`` maps a variant tag to the
        ``((subject, object), relation)`` it pinned.

    Example:
        >>> c = Clique(id="x", verdicts=[
        ...     BoomerVerdict("M:1", "I:A", "equivalent", 0.9),
        ...     BoomerVerdict("M:2", "I:A", "equivalent", 0.8)])
        >>> pr = {("M:1", "I:A"): (0.0, 0.0, 1.0, 0.0),
        ...       ("M:2", "I:A"): (0.0, 0.0, 1.0, 0.0)}
        >>> rows, index = build_marginal_inputs([c], pr)
        >>> len(rows), len(index)
        (4, 2)
        >>> sorted(index.values())
        [(('M:1', 'I:A'), 'equivalent'), (('M:2', 'I:A'), 'equivalent')]

        Every row in a variant is renamed with that variant's tag, so nothing leaks between
        variants:

        >>> sorted({r[0].split("__mai")[1] for r in rows})
        ['c0v0', 'c0v1']

        An oversized clique is skipped rather than estimated:

        >>> big = Clique(id="big", verdicts=[
        ...     BoomerVerdict(f"M:{i}", "I:A", "equivalent", 0.9) for i in range(20)])
        >>> build_marginal_inputs([big], {}, max_clique_size=8)
        ([], {})
    """
    rows: list[tuple[str, str, tuple[float, ...]]] = []
    index: dict[str, tuple[tuple[str, str], str]] = {}
    for c_no, clique in enumerate(cliques):
        if not 1 < len(clique.verdicts) <= max_clique_size:
            continue
        keys = [(v.subject_id, v.object_id) for v in clique.verdicts]
        if not all(k in priors for k in keys):
            continue
        v_no = 0
        for target in keys:
            for position, relation in enumerate(RELATIONS):
                if priors[target][position] <= 0.0:
                    continue
                tag = f"c{c_no}v{v_no}"
                v_no += 1
                index[tag] = (target, relation)
                for key in keys:
                    probs = pinned_priors(relation) if key == target else priors[key]
                    rows.append((variant_curie(key[0], tag),
                                 variant_curie(key[1], tag), probs))
    return rows, index


def write_variant_ontology(
    path: Path,
    ptable_rows: list[tuple[str, str, tuple[float, ...]]],
    subjects: ResourceSet,
    objects: ResourceSet,
    subject_iri: str,
    object_iri: str,
) -> int:
    """Write the background ontology for the pinned variants.

    The subclass structure is copied per variant from the *base* entities, so each variant
    faces the same hard constraints the original clique did.

    Example:
        >>> import tempfile, pathlib
        >>> from mondo_ai.align.models import Concept
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:1": Concept(id="M:1", label="a"),
        ...     "M:2": Concept(id="M:2", label="b", parents=["M:1"])})
        >>> o = ResourceSet(name="i", prefix="I", concepts={"I:A": Concept(id="I:A")})
        >>> rows = [("M:1__maic0v0", "I:A__maic0v0", (0.0, 0.0, 1.0, 0.0)),
        ...         ("M:2__maic0v0", "I:A__maic0v0", (0.0, 0.0, 1.0, 0.0))]
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "v.owl"
        >>> write_variant_ontology(p, rows, s, o, "http://x/M_", "http://y/I/")
        3
        >>> "M_2__maic0v0" in p.read_text() and "subClassOf" in p.read_text()
        True
    """
    by_variant: dict[str, set[str]] = {}
    for subject, obj, _ in ptable_rows:
        tag = subject.partition(VARIANT_SEP)[2]
        by_variant.setdefault(tag, set()).update({subject, obj})

    def iri(curie: str) -> str | None:
        prefix, _, local = curie.partition(":")
        if prefix == subjects.prefix:
            return f"{subject_iri}{local}"
        if prefix == objects.prefix:
            return f"{object_iri}{local}"
        return None

    lines = [
        '<?xml version="1.0"?>',
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"',
        '  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"',
        '  xmlns:owl="http://www.w3.org/2002/07/owl#"',
        '  xml:base="https://w3id.org/mondo-ai/align/boomer-variants">',
        '  <owl:Ontology rdf:about='
        '"https://w3id.org/mondo-ai/align/boomer-variants"/>',
    ]
    declared = 0
    for tag, members in sorted(by_variant.items()):
        present = {base_curie(m): m for m in members}
        for base, variant in sorted(present.items()):
            uri = iri(variant)
            if uri is None:
                continue
            concept = subjects.concepts.get(base) or objects.concepts.get(base)
            parents = [
                present[p] for p in (concept.parents if concept else []) if p in present
            ]
            if parents:
                lines.append(f'  <owl:Class rdf:about="{uri}">')
                for parent in parents:
                    lines.append(
                        f'    <rdfs:subClassOf rdf:resource="{iri(parent)}"/>')
                lines.append("  </owl:Class>")
            else:
                lines.append(f'  <owl:Class rdf:about="{uri}"/>')
            declared += 1
    lines.append("</rdf:RDF>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return declared


def marginals_from_variants(
    cliques: list[Clique],
    index: dict[str, tuple[tuple[str, str], str]],
    priors: dict[tuple[str, str], tuple[float, ...]],
) -> tuple[dict[tuple[str, str], dict[str, float]], dict[tuple[str, str], float]]:
    """Turn the pinned-variant cliques' scores into per-mapping max-marginals.

    The arithmetic matters, so it is written out. A variant that pins mapping *m* to relation
    *r* scores ``log PIN_MASS + log P(best consistent completion of the rest | m = r)``. The
    pin's own mass is an artefact of how the constraint had to be expressed, so it is
    subtracted back out and the mapping's *real* prior for *r* added in its place:

        ``score(r) = clique_score(variant) - log PIN_MASS + log prior(m = r)``

    Normalising ``exp`` over the relations gives the max-marginal. The consequence worth
    noting: for a mapping nothing in the clique constrains, every completion scores the same
    and the posterior comes out equal to the prior — which is the correct answer, and is also
    the sanity check that the arithmetic is right.

    Args:
        cliques: Cliques from the variant Boomer run.
        index: Variant tag -> the ``((subject, object), relation)`` it pinned.
        priors: The mappings' original prior rows.

    Not every pinned variant comes back: Boomer may partition a variant into its singleton
    bag, in which case that relation gets no score. Normalising over the survivors would then
    produce a number that is not comparable to the prior, so the second return value reports
    how much of each mapping's prior mass was actually scored, and the caller refuses to emit
    a posterior below a coverage threshold.

    Returns:
        ``(marginals, coverage)`` where marginals is ``(subject, object)`` -> relation ->
        probability, and coverage is ``(subject, object)`` -> the prior mass that was scored.

    Example:
        An unconstrained mapping's posterior reproduces its prior:

        >>> idx = {"c0v0": (("M:1", "I:A"), "equivalent"),
        ...        "c0v1": (("M:1", "I:A"), "subclass")}
        >>> pr = {("M:1", "I:A"): (0.2, 0.0, 0.8, 0.0)}
        >>> same = math.log(PIN_MASS) + math.log(0.5)
        >>> cs = [Clique(id="M:1__maic0v0 I:A__maic0v0", score=same),
        ...       Clique(id="M:1__maic0v1 I:A__maic0v1", score=same)]
        >>> out, cov = marginals_from_variants(cs, idx, pr)
        >>> {k: round(v, 4) for k, v in out[("M:1", "I:A")].items()}
        {'equivalent': 0.8, 'subclass': 0.2}
        >>> cov[("M:1", "I:A")]
        1.0

        A constraint that makes the prior-favoured relation impossible flips the ranking:

        >>> cs2 = [Clique(id="M:1__maic0v0 I:A__maic0v0",
        ...               score=math.log(PIN_MASS) + math.log(0.001)),
        ...        Clique(id="M:1__maic0v1 I:A__maic0v1",
        ...               score=math.log(PIN_MASS) + math.log(0.9))]
        >>> out2, _ = marginals_from_variants(cs2, idx, pr)
        >>> round(out2[("M:1", "I:A")]["subclass"], 4)
        0.9956

        A variant Boomer never reported contributes nothing, rather than a fabricated zero:

        A variant Boomer never reported leaves its relation unscored, and the coverage says
        so rather than the posterior pretending to be complete:

        >>> partial, cov2 = marginals_from_variants([cs[0]], idx, pr)
        >>> partial[("M:1", "I:A")], cov2[("M:1", "I:A")]
        ({'equivalent': 1.0}, 0.8)
    """
    log_pin = math.log(PIN_MASS)
    scores: dict[tuple[str, str], dict[str, float]] = {}
    for clique in cliques:
        if clique.is_singleton_group:
            continue
        tags = {
            token.partition(VARIANT_SEP)[2]
            for token in clique.id.split()
            if VARIANT_SEP in token
        }
        if len(tags) != 1:
            # A clique spanning two variants would mean the renaming failed to isolate them;
            # dropping it is safer than attributing its score to one of the pins.
            continue
        entry = index.get(tags.pop())
        if entry is None:
            continue
        key, relation = entry
        row = priors.get(key)
        if row is None:
            continue
        prior = row[RELATIONS.index(relation)]
        if prior <= 0.0:
            continue
        scores.setdefault(key, {})[relation] = (
            clique.score - log_pin + math.log(prior)
        )
    marginals = {key: max_marginal(by_relation) for key, by_relation in scores.items()}
    coverage = {
        key: round(sum(priors[key][RELATIONS.index(r)] for r in by_relation), 6)
        for key, by_relation in scores.items()
    }
    return marginals, coverage
