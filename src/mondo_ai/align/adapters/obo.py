"""OBO-format adapter.

A hand-rolled line parser rather than ``pronto`` or ``oaklib``: Mondo's ``.obo`` release is
53 MB and this reads it in a few seconds with no object graph to hold afterwards, which
matters because the alignment run holds both resources in memory at once.

Only the stanza fields alignment needs are read. Everything else — including
``intersection_of``, ``relationship`` and the qualifier blocks on ``xref`` — is skipped,
except that xref *qualifiers* are preserved (see :func:`parse_xref`) because Mondo encodes
mapping precision there and a bare xref does not establish equivalence.

Example:
    >>> parse_xref('xref: ICD10CM:Q87.4 {source="MONDO:equivalentTo"}')
    ('ICD10CM:Q87.4', 'MONDO:equivalentTo')
    >>> parse_xref("xref: UMLS:C0024796")
    ('UMLS:C0024796', '')
"""

from __future__ import annotations

import re
from pathlib import Path

from mondo_ai.align.models import Concept, ResourceSet
from mondo_ai.align.provenance import file_provenance

_SYNONYM_RE = re.compile(r'^synonym: "((?:[^"\\]|\\.)*)"\s+(EXACT|BROAD|NARROW|RELATED)\b')
_DEF_RE = re.compile(r'^def: "((?:[^"\\]|\\.)*)"')
_XREF_RE = re.compile(r'^xref:\s+(\S+)(?:\s+\{(.*)\})?')
_SOURCE_RE = re.compile(r'source="([^"]+)"')


def parse_xref(line: str) -> tuple[str, str]:
    """Split an OBO ``xref:`` line into its identifier and its ``source`` qualifier.

    Args:
        line: A full ``xref:`` line.

    Returns:
        ``(curie, qualifier)``; the qualifier is the empty string when absent.

    Example:
        >>> parse_xref('xref: ICD10CM:E84 {source="MONDO:mondoIsBroaderThanSource"}')
        ('ICD10CM:E84', 'MONDO:mondoIsBroaderThanSource')
        >>> parse_xref("not an xref")
        ('', '')
    """
    m = _XREF_RE.match(line)
    if not m:
        return "", ""
    qual = ""
    if m.group(2):
        q = _SOURCE_RE.search(m.group(2))
        if q:
            qual = q.group(1)
    return m.group(1), qual


_IS_A_RE = re.compile(r"^is_a:\s*([^\s{!]+)")


def parse_is_a(line: str) -> str:
    """Extract the parent identifier from an OBO ``is_a:`` line.

    An ``is_a`` line may carry a trailing modifier block *and* a comment:
    ``is_a: MONDO:0024290 {source="NCIT:C118172"} ! adenoma``. Stripping only the comment
    leaves the brace block glued to the identifier, which then matches no concept and is
    silently dropped — so the hierarchy quietly loses those edges, and everything derived
    from it (descendant counts, the coextensive-rubric test, Boomer's hard constraints) is
    computed against a thinner graph than the file describes. Mondo's own release has 36,637
    such lines.

    Example:
        >>> parse_is_a("is_a: MONDO:0005015 ! diabetes mellitus")
        'MONDO:0005015'
        >>> parse_is_a('is_a: MONDO:0024290 {source="NCIT:C118172"} ! adenoma')
        'MONDO:0024290'
        >>> parse_is_a('is_a: CL:0000164 {gci_filler="UBERON:0000945"} ! cell')
        'CL:0000164'
        >>> parse_is_a("is_a: MONDO:0005015")
        'MONDO:0005015'
        >>> parse_is_a("is_a:")
        ''
    """
    match = _IS_A_RE.match(line)
    return match.group(1) if match else ""


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", " ").replace("\\\\", "\\")


def load_obo(
    path: str | Path,
    prefix: str,
    name: str = "",
    *,
    source_url: str = "",
    keep_xref_prefixes: tuple[str, ...] = (),
) -> ResourceSet:
    """Load an OBO file into a :class:`ResourceSet`.

    Args:
        path: Path to the ``.obo`` file.
        prefix: Identifier prefix to keep, e.g. ``MONDO``. Stanzas with any other prefix
            are skipped, which is how imported foreign terms are excluded.
        name: Resource name; defaults to the lower-cased prefix.
        source_url: Recorded for provenance.
        keep_xref_prefixes: Only xrefs with these prefixes are retained, to avoid holding
            Mondo's full cross-reference set when only one target vocabulary matters.
            Empty keeps all.

    Returns:
        The resource, with ``children`` and ``n_descendants`` filled in.

    Example:
        >>> import tempfile, pathlib
        >>> obo = '''[Term]
        ... id: X:1
        ... name: root disease
        ... [Term]
        ... id: X:2
        ... name: leaf disease
        ... def: "A leaf." [PMID:1]
        ... synonym: "leaf dz" EXACT []
        ... synonym: "leafy" RELATED []
        ... is_a: X:1 ! root disease
        ... xref: ICD10CM:A00 {source="MONDO:equivalentTo"}
        ... [Term]
        ... id: X:3
        ... name: gone
        ... is_obsolete: true
        ... '''
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "t.obo"
        >>> _ = p.write_text(obo)
        >>> rs = load_obo(p, "X")
        >>> len(rs), [c.id for c in rs.active()]
        (3, ['X:1', 'X:2'])
        >>> rs.concepts["X:2"].exact_synonyms, rs.concepts["X:2"].other_synonyms
        (['leaf dz'], ['leafy'])
        >>> rs.concepts["X:2"].xrefs
        ['ICD10CM:A00']
        >>> rs.concepts["X:2"].extra["xref_ICD10CM:A00"]
        'MONDO:equivalentTo'
        >>> rs.concepts["X:1"].children, rs.concepts["X:1"].n_descendants
        (['X:2'], 1)
        >>> rs.concepts["X:3"].obsolete
        True
    """
    path = Path(path)
    concepts: dict[str, Concept] = {}
    cur: Concept | None = None
    in_term = False
    version = ""

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("data-version:"):
                version = line.split(":", 1)[1].strip()
                continue
            if line.startswith("["):
                if cur is not None:
                    concepts[cur.id] = cur
                cur = None
                in_term = line == "[Term]"
                continue
            if not in_term or not line:
                continue
            if line.startswith("id: "):
                cid = line[4:].strip()
                cur = Concept(id=cid) if cid.startswith(f"{prefix}:") else None
                continue
            if cur is None:
                continue
            if line.startswith("name: "):
                cur.label = line[6:].strip()
            elif line.startswith("is_obsolete: true"):
                cur.obsolete = True
            elif line.startswith("def: "):
                m = _DEF_RE.match(line)
                if m:
                    cur.definition = _unescape(m.group(1))
            elif line.startswith("synonym: "):
                m = _SYNONYM_RE.match(line)
                if m:
                    text = _unescape(m.group(1))
                    if m.group(2) == "EXACT":
                        cur.exact_synonyms.append(text)
                    else:
                        cur.other_synonyms.append(text)
            elif line.startswith("is_a: "):
                parent = parse_is_a(line)
                if parent.startswith(f"{prefix}:"):
                    cur.parents.append(parent)
            elif line.startswith("xref: "):
                curie, qual = parse_xref(line)
                if not curie:
                    continue
                if keep_xref_prefixes and not curie.startswith(
                    tuple(f"{p}:" for p in keep_xref_prefixes)
                ):
                    continue
                cur.xrefs.append(curie)
                if qual:
                    cur.extra[f"xref_{curie}"] = qual

    if cur is not None:
        concepts[cur.id] = cur

    rs = ResourceSet(
        name=name or prefix.lower(),
        prefix=prefix,
        concepts=concepts,
        source_path=str(path),
        source_url=source_url,
        version=version,
        **file_provenance(path),
    )
    link_hierarchy(rs)
    return rs


def link_hierarchy(rs: ResourceSet) -> None:
    """Fill ``children`` and ``n_descendants`` from the ``parents`` already loaded.

    Descendant counts are computed by memoised depth-first walk with cycle protection —
    real terminologies do contain cycles, and a naive recursion over ICD-10-CM's 98k
    classes hits the interpreter's stack limit.

    Example:
        >>> rs = ResourceSet(name="r", prefix="P", concepts={
        ...     "P:1": Concept(id="P:1", label="a"),
        ...     "P:2": Concept(id="P:2", label="b", parents=["P:1"]),
        ...     "P:3": Concept(id="P:3", label="c", parents=["P:2"])})
        >>> link_hierarchy(rs)
        >>> rs.concepts["P:1"].n_descendants, rs.concepts["P:2"].n_descendants
        (2, 1)
    """
    for c in rs.concepts.values():
        c.children = []
    for c in rs.concepts.values():
        for p in c.parents:
            parent = rs.concepts.get(p)
            if parent is not None:
                parent.children.append(c.id)

    memo: dict[str, set[str]] = {}

    def descendants(cid: str, seen: frozenset[str]) -> set[str]:
        if cid in memo:
            return memo[cid]
        if cid in seen:
            return set()
        acc: set[str] = set()
        for ch in rs.concepts[cid].children:
            if ch in rs.concepts:
                acc.add(ch)
                acc |= descendants(ch, seen | {cid})
        memo[cid] = acc
        return acc

    import sys

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 100000))
    try:
        for cid, c in rs.concepts.items():
            c.n_descendants = len(descendants(cid, frozenset()))
    finally:
        sys.setrecursionlimit(old_limit)
