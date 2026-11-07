"""Object-side lexical index with four lookup surfaces.

The index answers "which object concepts carry this string, and how did they carry it".
Field provenance is kept on every posting because evidence strength differs by field: a
hit on a primary label is ``EV-001``, on an exact synonym ``EV-003``, on anything else
``EV-007``.

Four surfaces, in descending strength:

``raw``
    Whitespace-collapsed literal. Two labels equal here are the same characters.
``normalized``
    After :func:`~mondo_ai.align.normalize.base_normalize`, i.e. case/diacritic/punctuation
    differences removed. Still a claim of string identity.
``variant``
    After one named meaning-preserving rewrite. Backs ``EV-004``, and the rewrite is
    recorded so the row says which one.
``tokens``
    IDF-weighted inverted index, for retrieval when no string surface hits. Backs
    ``EV-006`` only — retrieval is a way to find candidates, never a reason to believe one.

Example:
    >>> from mondo_ai.align.models import Concept
    >>> idx = LexicalIndex()
    >>> idx.add(Concept(id="I:A00", label="Cholera"))
    >>> idx.add(Concept(id="I:Q87.4", label="Marfan's syndrome"))
    >>> idx.finalize()
    >>> sorted(idx.lookup_raw("Cholera"))
    [('I:A00', 'label')]
    >>> sorted(idx.lookup_normalized("cholera"))
    [('I:A00', 'label')]
    >>> sorted(idx.lookup_normalized("marfan syndrome"))
    []
    >>> sorted(idx.lookup_variant("marfan syndrome"))
    [('I:Q87.4', 'label', 'strip_possessive')]
"""

from __future__ import annotations

import math
from collections import defaultdict

from mondo_ai.align.models import Concept
from mondo_ai.align.normalize import (
    base_normalize,
    content_tokens,
    exact_variants,
)

#: Postings are kept as (concept_id, field) or (concept_id, field, transform).
Posting = tuple[str, str]
VariantPosting = tuple[str, str, str]


class LexicalIndex:
    """Build-then-query index over one resource's strings.

    ``finalize`` must be called before any token retrieval; the string surfaces work
    immediately. Calling ``add`` after ``finalize`` is an error rather than a silent
    inconsistency, because a half-built IDF table produces plausible but wrong rankings.
    """

    def __init__(self) -> None:
        self._raw: dict[str, set[Posting]] = defaultdict(set)
        self._norm: dict[str, set[Posting]] = defaultdict(set)
        self._variant: dict[str, set[VariantPosting]] = defaultdict(set)
        self._postings: dict[str, set[str]] = defaultdict(set)
        self._concept_tokens: dict[str, frozenset[str]] = {}
        self._idf: dict[str, float] = {}
        self._n = 0
        self._final = False

    def add(self, concept: Concept) -> None:
        """Index every string of a concept.

        Raises:
            RuntimeError: If called after :meth:`finalize`.

        Example:
            >>> idx = LexicalIndex(); idx.finalize()
            >>> idx.add(Concept(id="X:1", label="a"))
            Traceback (most recent call last):
            ...
            RuntimeError: cannot add to a finalized LexicalIndex
        """
        if self._final:
            raise RuntimeError("cannot add to a finalized LexicalIndex")
        self._n += 1
        toks: set[str] = set()
        for value, field in concept.all_strings():
            raw = " ".join(value.split())
            if raw:
                self._raw[raw].add((concept.id, field))
            norm = base_normalize(value)
            if not norm:
                continue
            self._norm[norm].add((concept.id, field))
            for produced, transform in exact_variants(norm):
                self._variant[produced].add((concept.id, field, transform))
            toks |= content_tokens(norm)
            self._concept_tokens.setdefault(concept.id, frozenset())
        if toks:
            self._concept_tokens[concept.id] = frozenset(toks)
            for t in toks:
                self._postings[t].add(concept.id)

    def finalize(self) -> None:
        """Compute IDF weights and freeze the index.

        Idempotent, so a caller that is unsure whether the index was finalised may call it
        again.

        Example:
            >>> idx = LexicalIndex()
            >>> idx.add(Concept(id="X:1", label="cholera fever"))
            >>> idx.add(Concept(id="X:2", label="cholera"))
            >>> idx.finalize(); idx.finalize()
            >>> idx.idf("fever") > idx.idf("cholera")
            True
        """
        if self._final:
            return
        n = max(self._n, 1)
        self._idf = {
            t: math.log(1.0 + n / (1.0 + len(ids))) for t, ids in self._postings.items()
        }
        self._final = True

    # --- string surfaces ---------------------------------------------------------------

    def lookup_raw(self, value: str) -> set[Posting]:
        """Return postings for the whitespace-collapsed literal.

        Example:
            >>> idx = LexicalIndex(); idx.add(Concept(id="X:1", label="A  B"))
            >>> sorted(idx.lookup_raw("A B"))
            [('X:1', 'label')]
        """
        return self._raw.get(" ".join(value.split()), set())

    def lookup_normalized(self, normalized: str) -> set[Posting]:
        """Return postings for an already-normalised string.

        Example:
            >>> idx = LexicalIndex(); idx.add(Concept(id="X:1", label="Cholera"))
            >>> sorted(idx.lookup_normalized("cholera"))
            [('X:1', 'label')]
        """
        return self._norm.get(normalized, set())

    def lookup_variant(self, normalized: str) -> set[VariantPosting]:
        """Return postings whose *rewritten* form equals ``normalized``.

        Example:
            >>> idx = LexicalIndex()
            >>> idx.add(Concept(id="X:1", label="haemophilia A"))
            >>> sorted(idx.lookup_variant("hemophilia a"))
            [('X:1', 'label', 'brit_to_am')]
        """
        return self._variant.get(normalized, set())

    def has_normalized(self, normalized: str) -> bool:
        """Return whether any concept carries this normalised string.

        Example:
            >>> idx = LexicalIndex(); idx.add(Concept(id="X:1", label="Cholera"))
            >>> idx.has_normalized("cholera"), idx.has_normalized("typhoid")
            (True, False)
        """
        return normalized in self._norm

    # --- token retrieval ---------------------------------------------------------------

    def idf(self, token: str) -> float:
        """Return the inverse document frequency of a token.

        Unseen tokens score 0: they cannot discriminate between candidates that, by
        construction, do not contain them.

        Example:
            >>> idx = LexicalIndex(); idx.add(Concept(id="X:1", label="cholera"))
            >>> idx.finalize(); idx.idf("nothing")
            0.0
        """
        return self._idf.get(token, 0.0)

    def concept_tokens(self, concept_id: str) -> frozenset[str]:
        """Return the indexed content tokens of a concept.

        Example:
            >>> idx = LexicalIndex(); idx.add(Concept(id="X:1", label="cholera fever"))
            >>> sorted(idx.concept_tokens("X:1"))
            ['cholera', 'fever']
        """
        return self._concept_tokens.get(concept_id, frozenset())

    def retrieve(
        self,
        tokens: frozenset[str],
        limit: int = 20,
        max_postings: int = 20000,
    ) -> list[tuple[str, float]]:
        """Return the best-scoring concepts for a query token set.

        Scoring is the IDF mass shared by query and candidate, normalised by the query's
        own IDF mass, so a candidate can reach 1.0 only by covering every discriminative
        query token. Very common tokens (postings above ``max_postings``) are skipped
        entirely: they add nothing to the ranking and turn retrieval into a full scan.

        Args:
            tokens: Query content tokens.
            limit: Maximum candidates to return.
            max_postings: Skip tokens appearing in more concepts than this.

        Returns:
            ``(concept_id, score)`` pairs, best first, with ties broken by id so the
            output is reproducible.

        Example:
            >>> idx = LexicalIndex()
            >>> idx.add(Concept(id="X:1", label="marfan syndrome"))
            >>> idx.add(Concept(id="X:2", label="marfan syndrome type 2"))
            >>> idx.add(Concept(id="X:3", label="cholera"))
            >>> idx.finalize()
            >>> [i for i, _ in idx.retrieve(frozenset({"marfan"}))]
            ['X:1', 'X:2']
            >>> idx.retrieve(frozenset({"zzz"}))
            []
            >>> idx.retrieve(frozenset())
            []
        """
        if not self._final:
            raise RuntimeError("call finalize() before retrieve()")
        if not tokens:
            return []
        # Sorted, because tokens is a set and float addition is not associative: summing in
        # set order let the process's hash seed move a score's last bits and so reorder ties.
        ordered = sorted(tokens)
        query_mass = sum(self._idf.get(t, 0.0) for t in ordered)
        if query_mass <= 0.0:
            return []
        acc: dict[str, float] = defaultdict(float)
        for t in ordered:
            ids = self._postings.get(t)
            if not ids or len(ids) > max_postings:
                continue
            w = self._idf[t]
            for cid in ids:
                acc[cid] += w
        if not acc:
            return []
        scored = [(cid, mass / query_mass) for cid, mass in acc.items()]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored[:limit]


def build_index(concepts: list[Concept]) -> LexicalIndex:
    """Build and finalise an index over a list of concepts.

    Example:
        >>> idx = build_index([Concept(id="X:1", label="cholera")])
        >>> sorted(idx.lookup_normalized("cholera"))
        [('X:1', 'label')]
    """
    idx = LexicalIndex()
    for c in concepts:
        idx.add(c)
    idx.finalize()
    return idx
