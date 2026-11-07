"""Review mode: an existing SSSOM file in, the same rows out with confidence columns.

Alignment answers "what should this subject map to?". Review answers a different question
about a mapping set somebody already has: *how much should we believe each row?* The output
holds exactly the input's rows, in the input's order, with every input cell untouched; what
review adds are columns, three independent lines of evidence and a verdict built from them:

``lexical_confidence``
    The strongest string identity between subject and object, computed by the discovery code
    path (:func:`~mondo_ai.align.reconcile.pair_candidates`), so a reviewed row's lexical
    evidence means exactly what a discovered row's does.

``boomer_posterior_confidence``
    The max-marginal probability of the row's own predicate after Boomer conditions the whole
    set on both hierarchies (see :mod:`~mondo_ai.align.boomer` for what a max-marginal is and
    is not). ``boomer_prior`` is what went in.

``reviewer_confidence``
    A model's probability that the row, predicate included, is correct — one fenced judgement
    per row, rules required, budget-capped. A row the budget did not reach is *empty*, never
    zero: "nobody looked" and "looked and disbelieved" are different claims.

``review_status`` / ``review_tier``
    The reconciliation statuses and tiers (:mod:`~mondo_ai.align.reconcile`), with the input
    file as the only seed. ``consistency_conflict`` is a row Boomer overruled.

**Scaling Boomer: boxes.** Review draws boxes around the hierarchies on both sides and solves
each box on its own (:func:`make_boxes`):

1. A box is a **cycle component** (:func:`cycle_components`). Boomer's only constraint over
   subclass axioms is that two classes of one namespace may not collapse into an
   equivalence, and a collapse is a cycle: up one hierarchy, across a mapping, along the
   other hierarchy and back. Hypotheses that cannot share a cycle cannot constrain each
   other, so solving their components separately is *exact*. Agreeing hierarchies never
   close a cycle, which keeps the components tiny: on Mondo × SNOMED CT, 8,606 hypotheses
   make 8,503 components, the largest of 3, and the boxed posteriors equal one global
   Boomer run's on every row (spec §4.4).
2. A component of one hypothesis is ``isolated``: nothing can move it, so its posterior is
   its prior, by construction rather than by solver call.
3. Only a component larger than ``max_box`` is cut — at its most generic hubs first, then
   by chunking — and only a cut box gets a halo of neighbouring hypotheses solved alongside
   it and never read back. Every row records its box, the box size and any cut.
4. Boxes are renamed apart (``MONDO:1`` → ``MONDO:1__boxb7``) and packed into bins, one Boomer
   invocation per bin, bins in parallel. Renamed boxes cannot interact, so a bin is as exact
   as running each box alone, without a JVM start per box.

Two earlier rules are worth not repeating. Joining hypotheses along every subclass edge, and
then joining ancestor/descendant pairs, both percolated into one component of nearly the whole
set; cutting that at hubs lost real inversions, and halos traded those losses for tie
artefacts at 15× the cost.

Example:
    >>> box_curie("MONDO:0000001", "b7")
    'MONDO:0000001__boxb7'
    >>> unbox("MONDO:0000001__boxb7")
    'MONDO:0000001'
    >>> unbox("MONDO:0000001")
    'MONDO:0000001'
"""

from __future__ import annotations

import csv
import dataclasses
import json
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from mondo_ai.align import boomer as bm
from mondo_ai.align import reconcile as rc
from mondo_ai.align.agent import (
    DECISIONS,
    AnthropicCompleter,
    ResponseCache,
    batched,
    estimate_cost,
    render_item,
)
from mondo_ai.align.candidates import CandidateGenerator
from mondo_ai.align.config import AlignmentConfig
from mondo_ai.align.index import build_index
from mondo_ai.align.models import Candidate, Concept, ResourceSet
from mondo_ai.align.provenance import content_hash
from mondo_ai.align.rules import known, prompt_digest, render_rule_curies

#: Ceiling on a row's prior; a prior of exactly 1.0 is a hard constraint (see the SNOMED spec
#: §4.3), and two contradicting ones leave Boomer no solution for the whole bin.
MAX_PRIOR = 0.99

#: Separator for box renaming. Distinct from :data:`~mondo_ai.align.boomer.VARIANT_SEP`, so a
#: pinned variant of a boxed entity (``MONDO:1__boxb7__maic0v0``) strips back one level at a
#: time.
BOX_SEP = "__box"


def box_curie(curie: str, box_id: str) -> str:
    """Suffix a CURIE's local part with a box id."""
    prefix, _, local = curie.partition(":")
    return f"{prefix}:{local}{BOX_SEP}{box_id}"


def unbox(curie: str) -> str:
    """Strip a box suffix (and anything after it), returning the original CURIE."""
    head, sep, _ = curie.partition(BOX_SEP)
    return head if sep else curie


# --- SSSOM in and out --------------------------------------------------------------------


@dataclass
class SSSOMTable:
    """An SSSOM TSV held as read: metadata, column order, and every cell as a string.

    Nothing is parsed into types, so writing the table back reproduces every input cell
    byte for byte; only the YAML header is re-serialised.
    """

    metadata: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, str]]


def read_sssom(path: str | Path) -> SSSOMTable:
    """Read an SSSOM TSV without interpreting its cells.

    Example:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mkdtemp()) / "x.sssom.tsv"
        >>> _ = p.write_text("# mapping_set_id: x\\n"
        ...     "subject_id\\tpredicate_id\\tobject_id\\tconfidence\\n"
        ...     "MONDO:1\\tskos:exactMatch\\tSCTID:9\\t\\n")
        >>> t = read_sssom(p)
        >>> t.metadata, t.columns, t.rows[0]["confidence"]
        ({'mapping_set_id': 'x'}, ['subject_id', 'predicate_id', 'object_id', 'confidence'], '')
    """
    import yaml

    header: list[str] = []
    body: list[str] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for line in fh:
            (header if line.startswith("#") and not body else body).append(line)
    metadata = yaml.safe_load("".join(h[1:] for h in header)) or {}
    reader = csv.DictReader(body, delimiter="\t")
    rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
    return SSSOMTable(metadata=metadata, columns=list(reader.fieldnames or []), rows=rows)


def write_sssom(path: str | Path, table: SSSOMTable) -> int:
    """Write a table back as SSSOM TSV under its YAML header."""
    return rc.write_tsv(Path(path), table.rows, table.metadata, table.columns)


def subset_sssom(
    source: str | Path,
    out: str | Path,
    subject_prefix: str,
    object_prefix: str,
) -> int:
    """Write the rows of an SSSOM file between two prefixes, keeping its metadata.

    Example:
        >>> import tempfile, pathlib
        >>> d = pathlib.Path(tempfile.mkdtemp())
        >>> _ = (d / "a.tsv").write_text("# mapping_set_id: x\\n"
        ...     "subject_id\\tpredicate_id\\tobject_id\\n"
        ...     "MONDO:1\\tskos:exactMatch\\tSCTID:9\\n"
        ...     "MONDO:1\\tskos:exactMatch\\tDOID:9\\n")
        >>> subset_sssom(d / "a.tsv", d / "b.tsv", "MONDO", "SCTID")
        1
    """
    table = read_sssom(source)
    table.rows = [
        r for r in table.rows
        if r.get("subject_id", "").startswith(f"{subject_prefix}:")
        and r.get("object_id", "").startswith(f"{object_prefix}:")
    ]
    table.metadata = dict(table.metadata)
    table.metadata["mapping_set_description"] = (
        f"The {subject_prefix} -> {object_prefix} rows of "
        f"{table.metadata.get('mapping_set_id', Path(source).name)}, unchanged.")
    return write_sssom(out, table)


# --- boxes -------------------------------------------------------------------------------


@dataclass
class Box:
    """A set of hypotheses Boomer solves together.

    Attributes:
        id: ``b0001``…, assigned in a stable order.
        pairs: ``(subject, object)`` pairs, sorted.
        cut: ``""`` for a box that is a whole connected component of the background,
            ``"hub"`` when a stricter hub limit was needed to bring it under ``max_box``,
            ``"chunk"`` when it had to be split by brute force.
    """

    id: str
    pairs: list[tuple[str, str]]
    cut: str = ""
    #: Hypotheses from neighbouring boxes solved alongside the core, never read back.
    halo: list[tuple[str, str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.pairs) + len(self.halo)


def _lookup(cid: str, subjects: ResourceSet, objects: ResourceSet) -> Concept | None:
    return subjects.concepts.get(cid) or objects.concepts.get(cid)


def _background(
    pairs: list[tuple[str, str]],
    subjects: ResourceSet,
    objects: ResourceSet,
) -> set[str]:
    """Return the signature plus one hop, exactly what Boomer's background would declare."""
    wanted: set[str] = set()
    for pair in pairs:
        for cid in pair:
            concept = _lookup(cid, subjects, objects)
            wanted.add(cid)
            if concept is not None:
                wanted.update(concept.parents)
                wanted.update(concept.children)
    return wanted


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Smaller id wins, so component roots — and with them box order — are stable.
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _mapped_ancestors(
    pairs: list[tuple[str, str]],
    subjects: ResourceSet,
    objects: ResourceSet,
) -> dict[str, set[str]]:
    """Return, per mapped concept, its *nearest* mapped ancestors in Boomer's background.

    Upward reachability only through background classes, because a path Boomer's background
    does not contain is a constraint Boomer cannot see either. The search stops at the first
    mapped concept on each path: union is transitive, so nearest ancestors already connect
    every chain, and a concept's hub-ness is then its number of *direct* mapped children
    rather than everything beneath it — counting the latter made ``encephalitis`` a hub and
    cut it off from its own mapped parent, losing exactly the inversions Boomer exists for.
    """
    wanted = _background(pairs, subjects, objects)
    mapped = {cid for pair in pairs for cid in pair}
    out: dict[str, set[str]] = {}
    for cid in sorted(mapped):
        seen: set[str] = set()
        nearest: set[str] = set()
        stack = [cid]
        while stack:
            concept = _lookup(stack.pop(), subjects, objects)
            if concept is None:
                continue
            for parent in concept.parents:
                if parent in wanted and parent not in seen:
                    seen.add(parent)
                    if parent in mapped:
                        nearest.add(parent)
                    else:
                        stack.append(parent)
        out[cid] = nearest - {cid}
    return out


def cycle_components(
    pairs: list[tuple[str, str]],
    subjects: ResourceSet,
    objects: ResourceSet,
) -> list[list[tuple[str, str]]]:
    """Group hypotheses by the strongly connected component they could close a cycle in.

    Boomer's only constraint over a background of subclass axioms is that no two classes of
    one namespace may collapse into an equivalence, and a collapse is a cycle. Give every
    hypothesis its most permissive reading — equivalence, an edge both ways; broad and narrow
    are each one direction of it — and every subclass axiom of the one-hop background its own
    direction. Any cycle any assignment could create then lies inside one strongly connected
    component, so hypotheses in different components cannot constrain each other, and solving
    the components separately is exact.

    Hierarchies that agree (``s1 ⊑ s2`` and ``o1 ⊑ o2``) never close a cycle, which is why
    these components are small where the comparability components percolate.

    Example:
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:A": Concept(id="M:A", children=["M:B"]),
        ...     "M:B": Concept(id="M:B", parents=["M:A"])})
        >>> agree = ResourceSet(name="s", prefix="S", concepts={
        ...     "S:A": Concept(id="S:A", children=["S:B"]),
        ...     "S:B": Concept(id="S:B", parents=["S:A"])})
        >>> pairs = [("M:A", "S:A"), ("M:B", "S:B")]
        >>> cycle_components(pairs, s, agree)
        [[('M:A', 'S:A')], [('M:B', 'S:B')]]
        >>> invert = ResourceSet(name="s", prefix="S", concepts={
        ...     "S:B": Concept(id="S:B", children=["S:A"]),
        ...     "S:A": Concept(id="S:A", parents=["S:B"])})
        >>> cycle_components(pairs, s, invert)
        [[('M:A', 'S:A'), ('M:B', 'S:B')]]
    """
    wanted = _background(pairs, subjects, objects)
    succ: dict[str, list[str]] = defaultdict(list)
    for cid in wanted:
        concept = _lookup(cid, subjects, objects)
        if concept is not None:
            succ[cid].extend(p for p in concept.parents if p in wanted)
    for s, o in pairs:
        succ[s].append(o)
        succ[o].append(s)

    # Iterative Tarjan: the background has tens of thousands of nodes, and a recursive
    # version overflows Python's stack on SNOMED's deeper chains.
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp_of: dict[str, int] = {}
    counter = 0
    for root in sorted(succ):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            edges = succ.get(node, [])
            if i < len(edges):
                work.append((node, i + 1))
                nxt = edges[i]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    comp_of[member] = index[node]
                    if member == node:
                        break
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    groups: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for pair in pairs:
        groups[comp_of[pair[0]]].append(pair)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _nearest_children(ancestors: dict[str, set[str]]) -> Counter[str]:
    """Count, per mapped concept, the mapped concepts it is the nearest mapped ancestor of."""
    return Counter(a for anc in ancestors.values() for a in anc)


def _components(
    pairs: list[tuple[str, str]],
    subjects: ResourceSet,
    objects: ResourceSet,
    hub_children: int,
    hubs: frozenset[str] = frozenset(),
) -> list[list[tuple[str, str]]]:
    """Group hypotheses into the sets that can constrain one another, cut at hubs.

    Boomer finds a conflict only along a cycle: up or down one hierarchy, across a mapping,
    along the other hierarchy and back. Every vertical leg of such a cycle joins two *mapped*
    concepts that are ancestor and descendant, so hypotheses are joined when they share a
    concept or when their concepts are comparable on the same side. Siblings never are:
    two mappings under one parent cannot contradict each other, which is why joining along
    every subclass edge put the whole SNOMED set in one component.

    A mapped concept that is the nearest mapped ancestor of more than ``hub_children`` mapped
    concepts is a hub, as is every concept in ``hubs``: links down to it are not joined, and
    the hub's own hypothesis is solved without them. Its links upward still are.
    """
    ancestors = _mapped_ancestors(pairs, subjects, objects)
    below = _nearest_children(ancestors)
    uf = _UnionFind()
    for s, o in pairs:
        uf.union(s, o)
    for cid, anc in ancestors.items():
        for a in anc:
            if below[a] <= hub_children and a not in hubs:
                uf.union(cid, a)
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pair in pairs:
        groups[uf.find(pair[0])].append(pair)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def make_boxes(
    pairs: list[tuple[str, str]],
    subjects: ResourceSet,
    objects: ResourceSet,
    *,
    hub_children: int = 40,
    max_box: int = 150,
) -> list[Box]:
    """Partition hypotheses into boxes of at most ``max_box``.

    A box is a cycle component (:func:`cycle_components`) — exact, nothing given up. Only a
    component larger than ``max_box`` is cut: at its most generic hubs first, by brute-force
    chunking if no hub separates it, and the cut is recorded on the box.

    Example:
        Mondo and SNOMED agree that ``1`` and ``2`` sit under ``p``; they disagree about
        ``3`` and ``p`` (an inversion), so only those two mappings share a box:

        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:p": Concept(id="M:p", children=["M:1", "M:2", "M:3"]),
        ...     "M:1": Concept(id="M:1", parents=["M:p"]),
        ...     "M:2": Concept(id="M:2", parents=["M:p"]),
        ...     "M:3": Concept(id="M:3", parents=["M:p"])})
        >>> o = ResourceSet(name="s", prefix="S", concepts={
        ...     "S:3": Concept(id="S:3", children=["S:p"]),
        ...     "S:p": Concept(id="S:p", parents=["S:3"], children=["S:1", "S:2"]),
        ...     "S:1": Concept(id="S:1", parents=["S:p"]),
        ...     "S:2": Concept(id="S:2", parents=["S:p"])})
        >>> pairs = [("M:p", "S:p"), ("M:1", "S:1"), ("M:2", "S:2"), ("M:3", "S:3")]
        >>> [b.pairs for b in make_boxes(pairs, s, o)]
        [[('M:1', 'S:1')], [('M:2', 'S:2')], [('M:3', 'S:3'), ('M:p', 'S:p')]]

        Over the size cap the inversion is cut, and the boxes say so:

        >>> [(b.pairs, b.cut) for b in make_boxes(pairs, s, o, max_box=1)][-2:]
        [([('M:3', 'S:3')], 'chunk'), ([('M:p', 'S:p')], 'chunk')]
    """
    def sibling_key(pair: tuple[str, str]) -> tuple[str, str, str]:
        concept = _lookup(pair[0], subjects, objects)
        first = concept.parents[0] if concept is not None and concept.parents else ""
        return (first, pair[0], pair[1])

    pieces: list[tuple[list[tuple[str, str]], str]] = []
    work = [(group, frozenset(), "")
            for group in cycle_components(sorted(set(pairs)), subjects, objects)]
    while work:
        group, hubs, cut = work.pop()
        if len(group) <= max_box:
            pieces.append((group, cut))
            continue
        # Cut the most generic hubs first — those that are nearest mapped ancestor of the most
        # mapped concepts — doubling how many until the largest piece is clearly smaller.
        # Halving a hub limit across the whole box instead cut mid-level links like
        # encephalomyelitis -> encephalitis, which are where Boomer's inversions are; and
        # removing one hub at a time only chips single hypotheses off a percolating box.
        below = _nearest_children(_mapped_ancestors(group, subjects, objects))
        order = [c for c, n in sorted(below.items(), key=lambda kv: (-kv[1], kv[0]))
                 if c not in hubs and n > 1]
        target = max(max_box, int(0.75 * len(group)))
        parts: list[list[tuple[str, str]]] = []
        k = 1
        while order:
            banned = hubs | frozenset(order[:k])
            parts = _components(group, subjects, objects, hub_children, banned)
            if max(len(p) for p in parts) <= target or k >= len(order):
                break
            k *= 2
        if len(parts) > 1:
            work.extend((part, banned, "hub") for part in parts)
            continue
        # No hub separates it: chunk, keeping siblings together by ordering on the subject's
        # first parent.
        ordered = sorted(group, key=sibling_key)
        pieces.extend((sorted(ordered[i: i + max_box]), "chunk")
                      for i in range(0, len(ordered), max_box))
    pieces.sort(key=lambda p: p[0][0])
    width = max(4, len(str(len(pieces))))
    return [Box(id=f"b{i:0{width}d}", pairs=group, cut=cut)
            for i, (group, cut) in enumerate(pieces, start=1)]


def add_halos(
    boxes: list[Box],
    subjects: ResourceSet,
    objects: ResourceSet,
    *,
    depth: int = 1,
    hub_children: int = 40,
    pairs: list[tuple[str, str]] | None = None,
) -> None:
    """Give every box a halo: the hypotheses within ``depth`` links of its core, in place.

    Any partition of a set whose constraint graph percolates cuts real constraints — on Mondo
    × SNOMED CT the lossless components are one box of 8,597. A halo puts each core
    hypothesis's neighbours into its box (shared concept, or nearest mapped ancestor or
    child), so a core row is solved with its immediate constraints present even when they
    belong to another box. Halo rows are solved but never read back.

    Hubs do not fan out: a halo reaches a hub's own hypothesis, never all of its children.

    Example:
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:p": Concept(id="M:p", children=["M:1"]),
        ...     "M:1": Concept(id="M:1", parents=["M:p"])})
        >>> o = ResourceSet(name="s", prefix="S", concepts={
        ...     "S:p": Concept(id="S:p"), "S:1": Concept(id="S:1")})
        >>> bs = [Box("b1", [("M:1", "S:1")]), Box("b2", [("M:p", "S:p")])]
        >>> add_halos(bs, s, o)
        >>> bs[0].halo, bs[1].halo
        ([('M:p', 'S:p')], [('M:1', 'S:1')])
    """
    pairs = sorted(set(pairs) if pairs is not None else {p for b in boxes for p in b.pairs})
    ancestors = _mapped_ancestors(pairs, subjects, objects)
    below = _nearest_children(ancestors)
    by_entity: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for pair in pairs:
        for cid in pair:
            by_entity[cid].add(pair)
    linked: dict[str, set[str]] = defaultdict(set)
    for cid, anc in ancestors.items():
        for a in anc:
            linked[cid].add(a)
            if below[a] <= hub_children:
                linked[a].add(cid)

    def neighbours(pair: tuple[str, str]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for cid in pair:
            out |= by_entity[cid]
            for other in linked[cid]:
                out |= by_entity[other]
        return out

    for box in boxes:
        core = set(box.pairs)
        halo: set[tuple[str, str]] = set()
        frontier = core
        for _ in range(depth):
            nxt = set().union(*(neighbours(p) for p in frontier)) - core - halo
            halo |= nxt
            frontier = nxt
        box.halo = sorted(halo)


def pack_bins(boxes: list[Box], capacity: int) -> list[list[Box]]:
    """Pack boxes into bins of at most ``capacity`` hypotheses, first fit decreasing.

    Example:
        >>> bs = [Box(f"b{i}", [("M:%d" % j, "S:1") for j in range(n)])
        ...       for i, n in enumerate([5, 3, 3, 1])]
        >>> [[b.id for b in bin_] for bin_ in pack_bins(bs, 6)]
        [['b0', 'b3'], ['b1', 'b2']]
    """
    bins: list[list[Box]] = []
    loads: list[int] = []
    for box in sorted(boxes, key=lambda b: (-b.size, b.id)):
        for i, load in enumerate(loads):
            if load + box.size <= capacity:
                bins[i].append(box)
                loads[i] += box.size
                break
        else:
            bins.append([box])
            loads.append(box.size)
    return bins


def bin_resources(
    boxes: list[Box],
    subjects: ResourceSet,
    objects: ResourceSet,
) -> tuple[ResourceSet, ResourceSet]:
    """Build renamed resource views holding each box's one-hop background, apart.

    Every Boomer helper looks concepts up by id, so a view whose ids are box-renamed makes
    the existing background, variant and marginal code work per box unchanged.

    Example:
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:p": Concept(id="M:p", children=["M:1"]),
        ...     "M:1": Concept(id="M:1", label="a", parents=["M:p"])})
        >>> o = ResourceSet(name="s", prefix="S", concepts={"S:1": Concept(id="S:1")})
        >>> vs, vo = bin_resources([Box("b1", [("M:1", "S:1")])], s, o)
        >>> sorted(vs.concepts), vs.concepts["M:1__boxb1"].parents
        (['M:1__boxb1', 'M:p__boxb1'], ['M:p__boxb1'])
    """
    views = {
        subjects.prefix: ResourceSet(name=subjects.name, prefix=subjects.prefix),
        objects.prefix: ResourceSet(name=objects.name, prefix=objects.prefix),
    }
    for box in boxes:
        wanted = _background(box.pairs + box.halo, subjects, objects)
        for cid in wanted:
            concept = _lookup(cid, subjects, objects)
            view = views.get(cid.partition(":")[0])
            if concept is None or view is None:
                continue
            renamed = box_curie(cid, box.id)
            view.concepts[renamed] = Concept(
                id=renamed, label=concept.label, obsolete=concept.obsolete,
                parents=[box_curie(p, box.id) for p in concept.parents if p in wanted],
                children=[box_curie(k, box.id) for k in concept.children if k in wanted],
            )
    return views[subjects.prefix], views[objects.prefix]


# --- solving -----------------------------------------------------------------------------


@dataclass
class BinResult:
    """What one Boomer bin produced, keyed by the original (un-renamed) pairs."""

    cells: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    verdicts: dict[tuple[str, str], bm.BoomerVerdict] = field(default_factory=dict)
    failed: dict[tuple[str, str], str] = field(default_factory=dict)
    seconds: float = 0.0
    invocations: int = 0


def solve_bin(
    work: Path,
    boxes: list[Box],
    priors: dict[tuple[str, str], tuple[float, ...]],
    predicates: dict[tuple[str, str], str],
    subjects: ResourceSet,
    objects: ResourceSet,
    *,
    subject_iri: str,
    object_iri: str,
    cfg: Any,
    timeout: int,
) -> BinResult:
    """Run Boomer over one bin of renamed boxes: MAP pass, then the max-marginal pass."""
    start = time.time()
    out = BinResult()
    view_s, view_o = bin_resources(boxes, subjects, objects)
    rows: list[tuple[str, str, tuple[float, ...]]] = []
    rpriors: dict[tuple[str, str], tuple[float, ...]] = {}
    back: dict[tuple[str, str], tuple[tuple[str, str], str]] = {}
    for box in boxes:
        core = set(box.pairs)
        for s, o in box.pairs + box.halo:
            key = (box_curie(s, box.id), box_curie(o, box.id))
            rows.append((key[0], key[1], priors[(s, o)]))
            rpriors[key] = priors[(s, o)]
            if (s, o) in core:
                back[key] = ((s, o), box.id)

    common = dict(
        subject_iri=subject_iri, object_iri=object_iri, executable=cfg.executable,
        window_count=cfg.window_count, runs=cfg.runs,
        exhaustive_search_limit=cfg.exhaustive_search_limit,
        subsequent_solutions=cfg.subsequent_solutions, timeout=timeout,
    )
    run = bm.run_boomer(work / "pass1", rows, view_s, view_o, **common)
    out.invocations += 1
    if not run.ok:
        reason = run.stderr_tail.splitlines()[0] if run.stderr_tail else "boomer failed"
        out.failed = {pair: reason for pair, _ in back.values()}
        out.seconds = time.time() - start
        return out

    posteriors: dict[tuple[str, str], dict[str, float]] = {}
    coverage: dict[tuple[str, str], float] = {}
    # Halo-only cliques are context, not results: pinning them would buy posteriors nobody
    # reads back.
    interacting = [c for c in run.interacting_cliques()
                   if any((v.subject_id, v.object_id) in back for v in c.verdicts)]
    if cfg.marginals and interacting:
        vrows, index = bm.build_marginal_inputs(
            interacting, rpriors, max_clique_size=cfg.max_marginal_clique_size)
        if vrows:
            pass2 = work / "pass2"
            pass2.mkdir(parents=True, exist_ok=True)
            onto = pass2 / "variants.owl"
            bm.write_variant_ontology(onto, vrows, view_s, view_o, subject_iri, object_iri)
            run2 = bm.run_boomer(pass2, vrows, view_s, view_o, ontology_path=onto, **common)
            out.invocations += 1
            if run2.ok:
                posteriors, coverage = bm.marginals_from_variants(run2.cliques, index, rpriors)

    for key, verdict in run.verdicts.items():
        if key not in back:
            continue
        pair, box_id = back[key]
        cells = bm.verdict_cells(verdict, predicates[pair], rpriors[key],
                                 posteriors.get(key, {}), coverage.get(key))
        cells["box"] = box_id
        out.cells[pair] = cells
        out.verdicts[pair] = dataclasses.replace(
            verdict, subject_id=pair[0], object_id=pair[1])
    for key, (pair, _) in back.items():
        if pair not in out.cells:
            out.failed[pair] = "boomer reported no verdict for this hypothesis"
    out.seconds = time.time() - start
    return out


def isolated_cells(predicate: str, prior_row: tuple[float, ...], box_id: str) -> dict[str, Any]:
    """Cells for a hypothesis alone in its box: nothing constrains it, so posterior = prior.

    Not a shortcut standing in for a solver call: with no other hypothesis in its background,
    Boomer's MAP is the prior's argmax and the max-marginal is the prior, by construction.

    Example:
        >>> c = isolated_cells("skos:exactMatch", (0.0167, 0.0167, 0.95, 0.0166), "b1")
        >>> c["prior"], c["posterior"], c["method"], c["constrained"]
        (0.95, 0.95, 'isolated', False)
    """
    relation = bm.PREDICATE_RELATION[predicate]
    prior = prior_row[bm.RELATIONS.index(relation)]
    return {
        "prior": round(prior, 6), "posterior": round(prior, 6), "relation": relation,
        "predicate": predicate, "agrees_with_pipeline": True, "overruled": False,
        "clique_id": "", "clique_size": 1, "clique_probability": 1.0,
        "clique_confidence": 1.0, "method": "isolated",
        "marginal": {r: round(p, 6) for r, p in zip(bm.RELATIONS, prior_row)},
        "posterior_coverage": 1.0, "constrained": False, "box": box_id,
    }


def condition(
    work: Path,
    priors: dict[tuple[str, str], tuple[float, ...]],
    predicates: dict[tuple[str, str], str],
    subjects: ResourceSet,
    objects: ResourceSet,
    cfg: AlignmentConfig,
    *,
    subject_iri: str,
    object_iri: str,
    log: Callable[[str], None] = print,
) -> tuple[BinResult, list[Box], dict[str, Any]]:
    """Box, bin and solve every hypothesis; return merged results, the boxes, and stats."""
    rcfg, bcfg = cfg.review, cfg.boomer
    boxes = make_boxes(list(priors), subjects, objects,
                       hub_children=rcfg.hub_children, max_box=rcfg.max_box)
    cut = [b for b in boxes if b.cut]
    if rcfg.halo_depth and cut:
        # Only a cut box has lost constraints; an uncut box is a whole cycle component, exact.
        add_halos(cut, subjects, objects, depth=rcfg.halo_depth,
                  hub_children=rcfg.hub_children, pairs=list(priors))
    lone = [b for b in boxes if b.size == 1]
    shared = [b for b in boxes if b.size > 1]
    bins = pack_bins(shared, rcfg.bin_hypotheses)
    stats: dict[str, Any] = {
        "hypotheses": len(priors), "boxes": len(boxes), "isolated": len(lone),
        "largest_box": max((len(b.pairs) for b in boxes), default=0),
        "largest_box_with_halo": max((b.size for b in boxes), default=0),
        "halo_depth": rcfg.halo_depth,
        "halo_rows": sum(len(b.halo) for b in boxes),
        "cut_boxes": dict(Counter(b.cut for b in boxes if b.cut)),
        "bins": len(bins), "workers": rcfg.workers,
    }
    log(f"    {len(boxes):,} boxes ({len(lone):,} isolated, largest {stats['largest_box']} "
        f"core / {stats['largest_box_with_halo']} with halo), {len(bins):,} bins over "
        f"{rcfg.workers} workers")

    merged = BinResult()
    for box in lone:
        pair = box.pairs[0]
        merged.cells[pair] = isolated_cells(predicates[pair], priors[pair], box.id)
    if not bins:
        return merged, boxes, stats
    if bm.find_boomer(bcfg.executable) is None:
        reason = "boomer executable not found"
        for b in shared:
            for pair in b.pairs:
                merged.failed[pair] = reason
        stats["error"] = reason
        return merged, boxes, stats

    lock = threading.Lock()
    done = {"n": 0}

    def run_one(i: int, members: list[Box]) -> BinResult:
        kw = dict(subject_iri=subject_iri, object_iri=object_iri, cfg=bcfg,
                  timeout=rcfg.bin_timeout_seconds)
        result = solve_bin(work / f"bin{i:04d}", members, priors, predicates, subjects,
                           objects, **kw)
        if result.failed and len(members) > 1:
            # One unsolvable or slow box should not take its bin-mates down with it.
            retry = BinResult(seconds=result.seconds, invocations=result.invocations)
            for box in members:
                one = solve_bin(work / f"bin{i:04d}" / box.id, [box], priors, predicates,
                                subjects, objects, **kw)
                retry.cells.update(one.cells)
                retry.verdicts.update(one.verdicts)
                retry.failed.update(one.failed)
                retry.seconds += one.seconds
                retry.invocations += one.invocations
            result = retry
        with lock:
            done["n"] += 1
            if done["n"] % 10 == 0 or done["n"] == len(bins):
                log(f"    bins {done['n']:,}/{len(bins):,}")
        return result

    with ThreadPoolExecutor(max_workers=rcfg.workers) as pool:
        for result in pool.map(lambda a: run_one(*a), enumerate(bins)):
            merged.cells.update(result.cells)
            merged.verdicts.update(result.verdicts)
            merged.failed.update(result.failed)
            merged.seconds += result.seconds
            merged.invocations += result.invocations
    stats.update({
        "solved": len(merged.cells) - len(lone), "failed": len(merged.failed),
        "boomer_invocations": merged.invocations,
        "boomer_cpu_seconds": round(merged.seconds, 1),
        "overruled": sum(1 for v in merged.verdicts.values() if v.overruled),
        "posteriors": sum(1 for c in merged.cells.values() if c["posterior"] is not None),
    })
    return merged, boxes, stats


# --- the reviewing model ------------------------------------------------------------------


class ReviewVerdict(BaseModel):
    """One reviewed mapping, as the model returns it."""

    mapping_id: str = Field(description="The mapping id exactly as given.")
    relation: str = Field(
        description="The relation that actually holds between subject and object: exact, "
        "broad, narrow, close, related, or none. 'broad' means the OBJECT is broader than the "
        "subject; 'narrow' means the OBJECT is narrower.")
    confidence: float = Field(
        description="Probability, 0.0-1.0, that the mapping AS ASSERTED — this object, this "
        "predicate — is correct. A correct pair with the wrong predicate is not correct.")
    rules: list[str] = Field(
        default_factory=list,
        description="Curation rule ids from the registry that your judgement rests on.")
    justification: str = Field(
        default="",
        description="One concrete sentence: what the object denotes, and why the asserted "
        "mapping is or is not right.")
    needs_expert: bool = Field(
        default=False, description="True when the call turns on clinical knowledge you lack.")


class ReviewBatch(BaseModel):
    """The model's response for one batch: one verdict per input mapping."""

    verdicts: list[ReviewVerdict] = Field(
        description="Exactly one entry per input mapping, in the same order.")


REVIEW_PROMPT = """\
You review mappings between two disease vocabularies that someone has already asserted. For \
each mapping you judge which relation really holds between the subject and the object, and \
how probable it is that the mapping, exactly as asserted, is correct.

These are curated or semi-curated mappings, and most are right. Do not invent problems to \
seem rigorous; do not rubber-stamp either. A wrong mapping silently pulls the wrong patient \
cohort, so a confident wrong "yes" is the costly error.

## Direction convention

In `subject predicate object`: `narrow` means the OBJECT is narrower than the subject; \
`broad` means the OBJECT is broader. A broad mapping asserted as narrow is wrong.

## What makes an asserted `exactMatch` wrong

- The object is broader: a grouping, a disjunction of two diseases, or a residual \
`other`/`unspecified`/`NOS` bucket (SC-001, SC-002, SC-004, SC-006, PR-003).
- The object is narrower: a subtype, stage, site, laterality or complication variant \
(SC-005, PR-002).
- Same organ, different disease; shared words are not equivalence (RJ-004).
- The object is a morphology, a procedure, or anything that is not a condition (RJ-005).
- A conceptual-model boundary crossed without a permitted conflation rule, such as a finding \
mapped to a disorder (CM-004) or a susceptibility locus mapped to a disease (CM-006).

House-style differences are not errors: word order, possessives, spelling variants, \
`disease`/`disorder`, singular/plural, eponym versus descriptive name of the same entity.

## Calibration

`confidence` is a probability, and it is scored as one. Above 0.9 means you would bet on it; \
0.5 means you cannot tell. When the asserted predicate is wrong but a different one holds, \
`relation` says which, and `confidence` is low.

## Discipline

- Judge only the subject and object given. Never introduce another identifier.
- Cite rule ids from the registry below and nothing else.
- Emit exactly one verdict per input mapping, in the input order.

## Curation rule registry

{rules}
"""


def mapping_id(subject_id: str, object_id: str) -> str:
    """Return the id a mapping is presented to the model under."""
    return f"{subject_id} {object_id}"


def render_review_item(
    m: rc.ConsolidatedMapping,
    predicate: str,
    subjects: ResourceSet,
    objects: ResourceSet,
) -> dict[str, Any]:
    """Render one asserted mapping as the payload the reviewing model sees.

    Lexical evidence and scope flags are facts about the pair and go in; this pipeline's own
    status and tier do not, because a reviewer shown the verdict it is meant to check tends
    to agree with it.

    Example:
        >>> s = ResourceSet(name="m", prefix="M", concepts={
        ...     "M:1": Concept(id="M:1", label="Marfan syndrome")})
        >>> o = ResourceSet(name="s", prefix="S", concepts={
        ...     "S:1": Concept(id="S:1", label="Marfan's syndrome")})
        >>> item = render_review_item(rc.ConsolidatedMapping("M:1", "S:1"),
        ...                           "skos:exactMatch", s, o)
        >>> item["mapping_id"], item["asserted_predicate"], item["object"]["label"]
        ('M:1 S:1', 'skos:exactMatch', "Marfan's syndrome")
    """
    subject = subjects.concepts.get(m.subject_id) or Concept(id=m.subject_id)
    candidate = Candidate(
        m.object_id,
        signals=list(m.candidate.signals) if m.candidate is not None else [],
        flags=list(m.object_flags),
    )
    rendered = render_item(subject, [candidate], subjects.concepts, objects.concepts)
    obj = rendered["candidates"][0]
    if m.lexical is not None:
        obj["string_match"] = {
            "match": m.lexical.detail, "subject_field": m.lexical.subject_field,
            "object_field": m.lexical.object_field, "rule": m.lexical.rule_id}
    return {
        "mapping_id": mapping_id(m.subject_id, m.object_id),
        "asserted_predicate": predicate,
        "subject": rendered["subject"],
        "object": obj,
    }


def verdict_from_review(payload: dict[str, Any]) -> ReviewVerdict:
    """Validate a raw review verdict.

    Raises:
        ValueError: On an unknown relation or a confidence outside [0, 1].

    Example:
        >>> v = verdict_from_review({"mapping_id": "M:1 S:1", "relation": "exact",
        ...     "confidence": 0.9, "rules": ["EV-001", "NOPE-1"], "justification": "Same."})
        >>> v.rules, "NOPE-1" in v.justification
        (['EV-001'], True)
        >>> verdict_from_review({"mapping_id": "x", "relation": "maybe", "confidence": 0.5})
        Traceback (most recent call last):
        ...
        ValueError: review of x has unknown relation 'maybe'
    """
    v = ReviewVerdict.model_validate(payload)
    if v.relation not in DECISIONS:
        raise ValueError(f"review of {v.mapping_id} has unknown relation {v.relation!r}")
    if not 0.0 <= v.confidence <= 1.0:
        raise ValueError(f"review of {v.mapping_id} has confidence {v.confidence}")
    bad = [r for r in v.rules if not known(r)]
    v.rules = [r.split(":")[-1] for r in v.rules if known(r)]
    if bad:
        v.justification = (f"{v.justification} [pipeline note: dropped unknown rule ids "
                           f"{', '.join(bad)}]").strip()
    return v


RELATION_PREDICATE = {
    "exact": "skos:exactMatch", "broad": "skos:broadMatch", "narrow": "skos:narrowMatch",
    "close": "skos:closeMatch", "related": "skos:relatedMatch", "none": "sssom:NoTermFound",
}


@dataclass
class Reviewer:
    """Batches, calls, validates, retries and caches row-level review.

    The same discipline as :class:`~mondo_ai.align.agent.AgentAdjudicator`: a batch that fails
    validation is retried item by item, and an item that still fails is recorded as failed,
    never as a judgement.
    """

    completer: Any
    model: str
    batch_size: int = 15
    concurrency: int = 8
    cache_dir: str | Path | None = None
    on_progress: Callable[[int, int], None] | None = None

    def __post_init__(self) -> None:
        self.cache = ResponseCache(self.cache_dir)
        self.system = REVIEW_PROMPT.replace("{rules}", prompt_digest())
        self.calls = 0
        self.cache_hits = 0
        self.failures: list[str] = []
        self._lock = threading.Lock()

    def cache_key(self, item: dict[str, Any]) -> str:
        return content_hash("review", self.model, json.dumps(item, sort_keys=True))

    def review(self, items: list[dict[str, Any]]) -> dict[str, ReviewVerdict]:
        """Review rendered items, returning mapping id -> verdict.

        Example:
            >>> def fake(system, user):
            ...     items = json.loads(user[user.index("["):])
            ...     return ReviewBatch(verdicts=[ReviewVerdict(mapping_id=i["mapping_id"],
            ...         relation="exact", confidence=0.8) for i in items])
            >>> r = Reviewer(completer=fake, model="m")
            >>> out = r.review([{"mapping_id": "M:1 S:1"}])
            >>> out["M:1 S:1"].confidence, r.calls
            (0.8, 1)
        """
        results: dict[str, ReviewVerdict] = {}
        pending: list[dict[str, Any]] = []
        for item in items:
            cached = self.cache.get(self.cache_key(item))
            if cached is not None:
                try:
                    results[item["mapping_id"]] = verdict_from_review(cached)
                    self.cache_hits += 1
                    continue
                except ValueError:
                    pass
            pending.append(item)
        batches = list(batched(pending, self.batch_size))
        state = {"done": 0}

        def process(batch: list[dict[str, Any]]) -> dict[str, ReviewVerdict]:
            out = self._run_batch(batch)
            if out is None:
                out = {}
                for single in batch:
                    one = self._run_batch([single])
                    if one is None:
                        with self._lock:
                            self.failures.append(single["mapping_id"])
                    else:
                        out.update(one)
            with self._lock:
                state["done"] += len(batch)
                if self.on_progress:
                    self.on_progress(state["done"], len(pending))
            return out

        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as pool:
            for out in pool.map(process, batches):
                results.update(out)
        return results

    def _run_batch(self, batch: list[dict[str, Any]]) -> dict[str, ReviewVerdict] | None:
        user = (f"Review these {len(batch)} mappings. Return exactly {len(batch)} verdicts, "
                f"one per mapping, in this order.\n\n"
                + json.dumps(batch, indent=1, sort_keys=True))
        try:
            response = self.completer(self.system, user)
        except Exception:  # transport failure: the caller retries item-wise
            return None
        with self._lock:
            self.calls += 1
        by_id = {item["mapping_id"]: item for item in batch}
        out: dict[str, ReviewVerdict] = {}
        for raw in response.verdicts:
            payload = raw.model_dump()
            item = by_id.get(payload.get("mapping_id", ""))
            if item is None:
                return None
            try:
                out[payload["mapping_id"]] = verdict_from_review(payload)
            except ValueError:
                return None
            self.cache.put(self.cache_key(item), payload)
        return out if set(out) == set(by_id) else None


# --- the run -----------------------------------------------------------------------------

#: Columns review adds, with their SSSOM extension definitions. A re-reviewed file has them
#: already; their values are replaced, every other input cell is left alone.
REVIEW_EXTENSIONS: dict[str, tuple[str, str]] = {
    "review_status": ("http://w3id.org/mondo-ai/align/reviewStatus", "xsd:string"),
    "review_tier": ("http://w3id.org/mondo-ai/align/reviewTier", "xsd:string"),
    "review_rules": ("http://w3id.org/mondo-ai/align/reviewRules", "xsd:string"),
    "review_comment": ("http://w3id.org/mondo-ai/align/reviewComment", "xsd:string"),
    "risk_flags": ("http://w3id.org/mondo-ai/align/riskFlags", "xsd:string"),
    "structural_flags": ("http://w3id.org/mondo-ai/align/structuralFlags", "xsd:string"),
    "lexical_confidence": ("http://w3id.org/mondo-ai/align/lexicalConfidence", "xsd:double"),
    "lexical_match": ("http://w3id.org/mondo-ai/align/lexicalMatch", "xsd:string"),
    "boomer_prior": ("http://w3id.org/mondo-ai/align/boomerPrior", "xsd:double"),
    "boomer_posterior_confidence": (
        "http://w3id.org/mondo-ai/align/boomerPosterior", "xsd:double"),
    "boomer_predicate": ("http://w3id.org/mondo-ai/align/boomerPredicate", "xsd:string"),
    "boomer_agrees": ("http://w3id.org/mondo-ai/align/boomerAgrees", "xsd:boolean"),
    "boomer_status": ("http://w3id.org/mondo-ai/align/boomerStatus", "xsd:string"),
    "boomer_box": ("http://w3id.org/mondo-ai/align/boomerBox", "xsd:string"),
    "boomer_box_size": ("http://w3id.org/mondo-ai/align/boomerBoxSize", "xsd:integer"),
    "boomer_box_cut": ("http://w3id.org/mondo-ai/align/boomerBoxCut", "xsd:string"),
    "boomer_clique_size": ("http://w3id.org/mondo-ai/align/boomerCliqueSize", "xsd:integer"),
    "reviewer_confidence": ("http://w3id.org/mondo-ai/align/reviewerConfidence", "xsd:double"),
    "reviewer_predicate": ("http://w3id.org/mondo-ai/align/reviewerPredicate", "xsd:string"),
    "reviewer_rules": ("http://w3id.org/mondo-ai/align/reviewerRules", "xsd:string"),
    "reviewer_justification": (
        "http://w3id.org/mondo-ai/align/reviewerJustification", "xsd:string"),
    "reviewer_needs_expert": (
        "http://w3id.org/mondo-ai/align/reviewerNeedsExpert", "xsd:boolean"),
    "reviewer_model": ("http://w3id.org/mondo-ai/align/reviewerModel", "xsd:string"),
}

_TIER_ORDER = {"EXPERT_REVIEW": 0, "SPOT_CHECK": 1, "AUTO_ACCEPT": 2}


@dataclass
class ReviewResult:
    """Everything a review run produced."""

    table: SSSOMTable
    mappings: list[rc.ConsolidatedMapping]
    stats: dict[str, Any]
    paths: dict[str, Path]


def _float_cell(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class ReviewRunner:
    """Reviews one SSSOM file against the resources a run config names.

    Args:
        config: An alignment config; its ``subject``/``object`` must cover the file's prefixes.
        input_path: The SSSOM file to review.
        out_dir: Where the reviewed file, report and Boomer work go.
        completer: Transport override for the reviewing model, for tests.
    """

    def __init__(
        self,
        config: AlignmentConfig,
        input_path: Path,
        out_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        completer: Any = None,
    ) -> None:
        self.config = config
        self.input_path = Path(input_path)
        self.out_dir = Path(out_dir)
        self.log = log if log is not None else print
        self.completer = completer
        self.timings: dict[str, float] = {}

    def _timed(self, name: str, fn: Callable[[], Any]) -> Any:
        start = time.time()
        out = fn()
        self.timings[name] = round(time.time() - start, 1)
        self.log(f"  {name}: {self.timings[name]:.1f}s")
        return out

    def run(self) -> ReviewResult:
        """Review the file and write every output."""
        from mondo_ai.align.pipeline import AlignmentRunner, _obo_iri

        cfg = self.config
        self.out_dir.mkdir(parents=True, exist_ok=True)
        table = read_sssom(self.input_path)
        self.log(f"[1/5] {len(table.rows):,} rows in {self.input_path}")
        loader = AlignmentRunner(cfg, log=self.log)
        subjects, objects = loader.load()
        self.timings.update({k: round(v, 1) for k, v in loader.timings.items()})

        self.log("[2/5] per-row evidence: lexical match, scope flags, structure")
        seeds, scope = self._seed_rows(table)
        generator = CandidateGenerator(
            build_index([]), objects, object_prefix=cfg.object.prefix,
            max_candidates=cfg.candidates.max_candidates,
            max_per_category=cfg.candidates.max_per_category,
            retrieval_limit=cfg.candidates.retrieval_limit)
        mappings = self._timed("evidence", lambda: rc.reconcile(
            [], [seeds], subjects, objects, generator,
            permitted_conflations=cfg.conflations_permitted))
        by_pair = {(m.subject_id, m.object_id): m for m in mappings}
        stats: dict[str, Any] = {"rows": len(table.rows), "pairs": len(mappings),
                                 "out_of_scope_rows": sum(1 for s in scope if s != "in")}

        self.log("[3/5] boomer: boxes around both hierarchies")
        cells: dict[tuple[str, str], dict[str, Any]] = {}
        failed: dict[tuple[str, str], str] = {}
        boxes_by_pair: dict[tuple[str, str], Box] = {}
        if cfg.boomer.enabled:
            priors = {
                (m.subject_id, m.object_id): bm.priors_for(
                    m.predicate_id, min(m.confidence, MAX_PRIOR))
                for m in mappings
                if m.review_tier.value != "REJECTED" and m.predicate_id in bm.PREDICATE_RELATION
            }
            predicates = {k: by_pair[k].predicate_id for k in priors}
            result, boxes, bstats = self._timed("boomer", lambda: condition(
                self.out_dir / "boomer", priors, predicates, subjects, objects, cfg,
                subject_iri=cfg.subject.iri_base or _obo_iri(subjects.prefix),
                object_iri=cfg.object.iri_base or _obo_iri(objects.prefix), log=self.log))
            cells, failed = result.cells, result.failed
            boxes_by_pair = {p: b for b in boxes for p in b.pairs}
            bstats["escalated"] = rc.apply_boomer(mappings, result.verdicts)
            stats["boomer"] = bstats
        else:
            stats["boomer"] = {"enabled": False}

        self.log("[4/5] reviewing model")
        verdicts = self._timed("reviewer", lambda: self._review(
            mappings, cells, subjects, objects, stats))

        self.log("[5/5] writing")
        self._annotate(table, scope, by_pair, cells, failed, boxes_by_pair, verdicts)
        stats["timings"] = self.timings
        paths = self._write(table, mappings, stats)
        return ReviewResult(table=table, mappings=mappings, stats=stats, paths=paths)

    # --- stages ---------------------------------------------------------------------

    def _seed_rows(self, table: SSSOMTable) -> tuple[list[rc.SeedRow], list[str]]:
        """Turn the input rows into one seed; return it and each row's scope."""
        cfg = self.config
        seeds: list[rc.SeedRow] = []
        seen: set[tuple[str, str]] = set()
        scope: list[str] = []
        for row in table.rows:
            s, o, p = row.get("subject_id", ""), row.get("object_id", ""), row.get(
                "predicate_id", "")
            if not (s.startswith(f"{cfg.subject.prefix}:")
                    and o.startswith(f"{cfg.object.prefix}:")):
                scope.append("prefix")
                continue
            if p not in rc._MAPPING_PREDICATES:
                scope.append("predicate")
                continue
            scope.append("in")
            if (s, o) in seen:
                continue
            seen.add((s, o))
            conf = _float_cell(row.get("confidence", ""))
            seeds.append(rc.SeedRow(
                seed="input", subject_id=s, predicate_id=p, object_id=o,
                confidence=conf if conf is not None else cfg.review.default_confidence,
                justification=row.get("mapping_justification", "") or
                "semapv:UnspecifiedMatching",
                mapping_source=str(self.input_path)))
        return seeds, scope

    def _review(
        self,
        mappings: list[rc.ConsolidatedMapping],
        cells: dict[tuple[str, str], dict[str, Any]],
        subjects: ResourceSet,
        objects: ResourceSet,
        stats: dict[str, Any],
    ) -> dict[str, ReviewVerdict]:
        """Send the most contested rows to the reviewing model, up to the budget."""
        rcfg, acfg = self.config.review, self.config.agent
        eligible = [m for m in mappings if m.review_tier.value in _TIER_ORDER]

        def urgency(m: rc.ConsolidatedMapping) -> tuple:
            c = cells.get((m.subject_id, m.object_id)) or {}
            post = c.get("posterior")
            return (_TIER_ORDER[m.review_tier.value],
                    0 if c and not c.get("agrees_with_pipeline", True) else 1,
                    post if post is not None else 1.0, m.subject_id, m.object_id)

        eligible.sort(key=urgency)
        selected = eligible if rcfg.max_items == 0 else eligible[: rcfg.max_items]
        agent_stats: dict[str, Any] = {"enabled": rcfg.agent, "model": acfg.model,
                                       "eligible": len(eligible)}
        stats["reviewer"] = agent_stats
        if not rcfg.agent or not selected:
            agent_stats["sent"] = 0
            return {}
        items = [render_review_item(m, m.predicate_id, subjects, objects) for m in selected]
        completer = self.completer or AnthropicCompleter(
            model=acfg.model, max_tokens=acfg.max_tokens, effort=acfg.effort,
            output_format=ReviewBatch)
        reviewer = Reviewer(
            completer=completer, model=acfg.model, batch_size=acfg.batch_size,
            concurrency=acfg.concurrency, cache_dir=acfg.cache_dir or None,
            on_progress=lambda done, total: (
                self.log(f"    {done:,}/{total:,}")
                if done % (acfg.batch_size * 5) == 0 or done == total else None))
        verdicts = reviewer.review(items)
        agent_stats.update({"sent": len(items), "api_calls": reviewer.calls,
                            "cache_hits": reviewer.cache_hits,
                            "failures": len(reviewer.failures),
                            "not_reached": len(eligible) - len(selected)})
        usage = getattr(completer, "usage", None)
        if usage:
            cost = estimate_cost(acfg.model, usage)
            agent_stats.update({f"tokens_{k}": v for k, v in usage.items()})
            agent_stats["cost_usd"] = round(cost, 4) if cost is not None else "unpriced model"
            fresh = len(items) - reviewer.cache_hits
            if cost is not None and fresh > 0:
                per = cost / fresh
                agent_stats["cost_usd_per_row"] = round(per, 5)
                agent_stats["cost_usd_to_review_rest"] = round(
                    per * agent_stats["not_reached"], 2)
        return verdicts

    def _annotate(
        self,
        table: SSSOMTable,
        scope: list[str],
        by_pair: dict[tuple[str, str], rc.ConsolidatedMapping],
        cells: dict[tuple[str, str], dict[str, Any]],
        failed: dict[tuple[str, str], str],
        boxes: dict[tuple[str, str], Box],
        verdicts: dict[str, ReviewVerdict],
    ) -> None:
        """Add the review columns to every row, in place."""
        model = self.config.agent.model
        boomer_on = self.config.boomer.enabled
        for column in REVIEW_EXTENSIONS:
            if column not in table.columns:
                table.columns.append(column)
        for row, where in zip(table.rows, scope):
            for column in REVIEW_EXTENSIONS:
                row[column] = ""
            if where != "in":
                row["review_status"] = f"out_of_scope_{where}"
                continue
            pair = (row["subject_id"], row["object_id"])
            m = by_pair[pair]
            predicate = row["predicate_id"]
            row.update({
                "review_status": m.status, "review_tier": m.review_tier.value,
                "review_rules": render_rule_curies(m.rule_ids), "review_comment": m.comment,
                "risk_flags": "|".join(sorted(set(m.object_flags) | set(m.subject_flags))),
                "structural_flags": "|".join(m.structural_flags),
            })
            if m.lexical is not None:
                row["lexical_confidence"] = _fmt(round(m.lexical.certainty, 4))
                row["lexical_match"] = m.lexical.detail
            if predicate != m.predicate_id:
                # A second row for the same pair with another predicate: the evidence is the
                # pair's, the predicate under review is this row's own.
                row["review_comment"] = (f"duplicate pair: reviewed as {m.predicate_id} on an "
                                         f"earlier row. {m.comment}")
            c = cells.get(pair)
            if c is not None:
                relation = bm.PREDICATE_RELATION.get(predicate)
                posterior = c["posterior"] if predicate == m.predicate_id else (
                    (c.get("marginal") or {}).get(relation))
                box = boxes.get(pair)
                row.update({
                    "boomer_prior": _fmt(c["prior"]) if predicate == m.predicate_id else "",
                    "boomer_posterior_confidence": _fmt(posterior),
                    "boomer_predicate": c["predicate"],
                    "boomer_agrees": _fmt(c["predicate"] == predicate),
                    "boomer_status": c["method"] if c["method"] == "isolated" else "solved",
                    "boomer_box": c.get("box", ""),
                    "boomer_box_size": _fmt(len(box.pairs)) if box else "",
                    "boomer_box_cut": box.cut if box else "",
                    "boomer_clique_size": _fmt(c["clique_size"]),
                })
            elif pair in failed:
                row["boomer_status"] = f"failed: {failed[pair]}"
            elif boomer_on:
                row["boomer_status"] = "not_submitted"
            else:
                row["boomer_status"] = "disabled"
            v = verdicts.get(mapping_id(*pair))
            if v is not None:
                reviewer_predicate = RELATION_PREDICATE[v.relation]
                confidence = v.confidence
                if predicate != m.predicate_id:
                    # The model judged the pair's first predicate; for another predicate on
                    # the same pair only its relation call carries over, not its probability.
                    confidence = None
                row.update({
                    "reviewer_confidence": _fmt(confidence),
                    "reviewer_predicate": reviewer_predicate,
                    "reviewer_rules": render_rule_curies(v.rules),
                    "reviewer_justification": v.justification,
                    "reviewer_needs_expert": _fmt(v.needs_expert),
                    "reviewer_model": model,
                })

    def _write(
        self,
        table: SSSOMTable,
        mappings: list[rc.ConsolidatedMapping],
        stats: dict[str, Any],
    ) -> dict[str, Path]:
        """Write the reviewed SSSOM, the report and the stats."""
        from mondo_ai import __version__ as version

        md = dict(table.metadata)
        existing = [d for d in md.get("extension_definitions") or []
                    if d.get("slot_name") not in REVIEW_EXTENSIONS]
        md["extension_definitions"] = existing + [
            {"slot_name": s, "property": p, "type_hint": t}
            for s, (p, t) in REVIEW_EXTENSIONS.items()]
        note = (f"Reviewed by mondo-ai align review {version} on {date.today().isoformat()}: "
                f"rows and their cells are the input's; the review_*, lexical_*, boomer_* and "
                f"reviewer_* columns are added.")
        md["comment"] = f"{md['comment']} {note}" if md.get("comment") else note
        table.metadata = md

        stem = self.input_path.name.removesuffix(".tsv").removesuffix(".sssom")
        reviewed = self.out_dir / f"{stem}.reviewed.sssom.tsv"
        write_sssom(reviewed, table)
        report = self.out_dir / "review_report.md"
        report.write_text(review_report(table, stats, self.input_path, reviewed),
                          encoding="utf-8")
        stats_path = self.out_dir / "review_stats.json"
        stats_path.write_text(json.dumps(stats, indent=1, sort_keys=True, default=str),
                              encoding="utf-8")
        self.log(f"  wrote {reviewed}")
        return {"reviewed": reviewed, "report": report, "stats": stats_path}


# --- report ------------------------------------------------------------------------------


def review_report(table: SSSOMTable, stats: dict[str, Any], source: Path, out: Path) -> str:
    """Return the markdown review report."""
    from mondo_ai.align.report import md_table

    rows = table.rows
    lines = [
        f"# Review of `{source.name}`",
        "",
        f"{len(rows):,} rows in, {len(rows):,} rows out, in input order: `{out.name}`. Every "
        f"input cell is unchanged; review adds columns.",
        "",
        "## Status and tier",
        "",
    ]
    st = Counter((r["review_status"], r["review_tier"]) for r in rows)
    lines += [md_table([["status", "tier", "rows"]] + [
        [s, t, f"{n:,}"] for (s, t), n in sorted(st.items(), key=lambda kv: -kv[1])]), ""]

    b = stats.get("boomer", {})
    lines += ["## Boomer, boxed", ""]
    if b.get("enabled") is False:
        lines += ["Disabled.", ""]
    else:
        lines += [
            f"{b.get('hypotheses', 0):,} hypotheses in {b.get('boxes', 0):,} boxes "
            f"({b.get('isolated', 0):,} isolated, largest {b.get('largest_box', 0)}); "
            f"cut boxes {b.get('cut_boxes') or 'none'}. {b.get('bins', 0):,} bins, "
            f"{b.get('boomer_invocations', 0):,} Boomer invocations, "
            f"{b.get('boomer_cpu_seconds', 0)} s solver time over {b.get('workers')} workers.",
            "",
            f"Solved {b.get('solved', 0):,}, failed {b.get('failed', 0):,}; "
            f"{b.get('overruled', 0):,} overruled by consistency, "
            f"{b.get('escalated', 0):,} escalated to expert review; "
            f"{b.get('posteriors', 0):,} rows with a posterior.",
            "",
        ]
        status = Counter(r["boomer_status"] for r in rows)
        lines += [md_table([["boomer_status", "rows"]] + [
            [s or "(none)", f"{n:,}"] for s, n in status.most_common()]), ""]
        drops = sorted(
            (r for r in rows if r["boomer_prior"] and r["boomer_posterior_confidence"]),
            key=lambda r: float(r["boomer_posterior_confidence"]) - float(r["boomer_prior"]))
        drops = [r for r in drops
                 if float(r["boomer_prior"]) - float(r["boomer_posterior_confidence"]) > 0.05]
        if drops:
            lines += [f"### Largest drops from prior to posterior ({len(drops):,} rows > 0.05)",
                      ""]
            lines += [md_table([["prior", "posterior", "subject", "asserted", "object",
                                 "boomer", "box"]] + [
                [r["boomer_prior"], r["boomer_posterior_confidence"],
                 f"{r['subject_id']} {r.get('subject_label', '')[:40]}", r["predicate_id"],
                 f"{r['object_id']} {r.get('object_label', '')[:32]}", r["boomer_predicate"],
                 f"{r['boomer_box']} ({r['boomer_box_size']})"] for r in drops[:25]]), ""]

    a = stats.get("reviewer", {})
    lines += ["## Reviewing model", ""]
    if not a.get("enabled"):
        lines += ["Disabled; `reviewer_confidence` is empty throughout.", ""]
    else:
        lines += [
            f"Model `{a.get('model')}`: {a.get('sent', 0):,} of {a.get('eligible', 0):,} "
            f"eligible rows reviewed, most contested first ({a.get('cache_hits', 0):,} from "
            f"cache, {a.get('failures', 0):,} failed validation). "
            f"{a.get('not_reached', 0):,} not reached; their `reviewer_confidence` is empty.",
            "",
        ]
        if "cost_usd" in a:
            lines += [f"Spend: ${a['cost_usd']}"
                      + (f", ${a['cost_usd_per_row']} per row; reviewing the rest would cost "
                         f"about ${a['cost_usd_to_review_rest']}."
                         if "cost_usd_per_row" in a else "."), ""]
        reviewed = [r for r in rows if r["reviewer_predicate"]]
        agree = Counter(("agrees" if r["reviewer_predicate"] == r["predicate_id"]
                         else f"says {r['reviewer_predicate']}") for r in reviewed)
        if reviewed:
            lines += [md_table([["reviewer vs asserted", "rows"]] + [
                [k, f"{n:,}"] for k, n in agree.most_common()]), ""]
            low = sorted((r for r in reviewed if r["reviewer_confidence"]),
                         key=lambda r: float(r["reviewer_confidence"]))[:20]
            lines += ["### Least believed by the reviewer", ""]
            lines += [md_table([["reviewer", "subject", "asserted", "object", "says", "why"]]
                               + [[r["reviewer_confidence"],
                                   f"{r['subject_id']} {r.get('subject_label', '')[:36]}",
                                   r["predicate_id"],
                                   f"{r['object_id']} {r.get('object_label', '')[:30]}",
                                   r["reviewer_predicate"],
                                   r["reviewer_justification"][:140]] for r in low]), ""]

    lines += ["## Timings", "", md_table([["stage", "seconds"]] + [
        [k, str(v)] for k, v in stats.get("timings", {}).items()]), ""]
    return "\n".join(lines)
