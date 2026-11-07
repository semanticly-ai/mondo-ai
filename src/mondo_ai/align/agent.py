"""Agentic adjudication of everything the ladder could not settle.

Three commitments shape this module, and all three are about not trusting the model:

**It cannot invent an identifier.** The response is validated against the candidate ids that
were offered. A hallucinated id is not repaired or guessed at — the item is retried once and
then routed to expert review with ``RJ-002``. A crosswalk that contains an id nobody offered
is not a crosswalk.

**It cannot skip the rules.** The system prompt carries the citable rule registry, and the
output schema requires rule ids from it. A verdict citing a rule that does not exist is
rejected the same way a bad identifier is.

**Its output is a proposal, never an assertion.** Every row it produces is labelled
``semapv:CompositeMatching`` and Tier 3 under the Mondo tiering policy. The pipeline never
promotes it further on its own.

Caching is keyed on the rendered item payload plus the model id, so a rerun costs nothing
and a changed candidate pool invalidates exactly the items whose pool changed.

Example:
    >>> from mondo_ai.align.models import Candidate, Concept, Signal
    >>> subj = Concept(id="MONDO:1", label="Marfan syndrome", definition="A disorder.")
    >>> cands = [Candidate("ICD10CM:Q87.4", [Signal("lexical_variant", "EV-004", 0.97)])]
    >>> item = render_item(subj, cands, {}, {})
    >>> item["subject"]["label"], item["candidates"][0]["id"]
    ('Marfan syndrome', 'ICD10CM:Q87.4')
    >>> verdict_from_payload({"subject_id": "MONDO:1", "decision": "exact",
    ...                       "object_id": "ICD10CM:Q87.4", "confidence": 0.9,
    ...                       "rules": ["EV-004", "PR-001"], "justification": "Same disease.",
    ...                       "alternatives": [], "conflation": [], "needs_expert": False,
    ...                       "expert_question": ""}, {"ICD10CM:Q87.4"}).decision
    'exact'
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from pydantic import BaseModel, Field

from mondo_ai.align.models import Adjudication, Candidate, Concept
from mondo_ai.align.provenance import content_hash
from mondo_ai.align.rules import known, prompt_digest

#: Default model. Bulk adjudication over a whole vocabulary is the one place in this
#: pipeline where model choice is a real cost decision, so it is a config field; the
#: default is the most capable model rather than the cheapest.
DEFAULT_MODEL = "claude-opus-5"

DECISIONS = ("exact", "broad", "narrow", "close", "related", "none")


class ItemVerdict(BaseModel):
    """One adjudicated subject, as the model returns it."""

    subject_id: str = Field(description="The subject id exactly as given.")
    decision: str = Field(
        description="One of: exact, broad, narrow, close, related, none. "
        "'broad' means the OBJECT is broader than the subject; 'narrow' means the OBJECT "
        "is narrower. 'none' means no candidate denotes the subject."
    )
    object_id: str = Field(
        default="",
        description="The chosen candidate id, copied exactly from the candidate list. "
        "Empty string when decision is 'none'.",
    )
    confidence: float = Field(
        default=0.0,
        description="Your certainty in the DECISION, 0.0-1.0. Not your certainty about "
        "the disease.",
    )
    rules: list[str] = Field(
        default_factory=list,
        description="Curation rule ids from the registry that your verdict rests on, e.g. "
        "['EV-001', 'PR-001'] or ['SC-001', 'PR-002']. Cite the evidence rule and any "
        "scope or conflation rule you had to invoke.",
    )
    justification: str = Field(
        default="",
        description="One concrete sentence naming the object concept and why it does or "
        "does not denote the subject. When rejecting, say what the closest candidate was "
        "and what makes it wrong. 'No suitable match' is not acceptable.",
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description="Other candidate ids you seriously considered.",
    )
    conflation: list[str] = Field(
        default_factory=list,
        description="CM-* rule ids for any conceptual-model boundary you knowingly crossed.",
    )
    needs_expert: bool = Field(
        default=False,
        description="True when a human curator should decide this one.",
    )
    expert_question: str = Field(
        default="",
        description="If needs_expert, the specific question to put to the curator.",
    )


class BatchVerdict(BaseModel):
    """The model's response for one batch: one verdict per input item."""

    verdicts: list[ItemVerdict] = Field(
        description="Exactly one entry per input item, in the same order."
    )


SYSTEM_PROMPT = """\
You adjudicate candidate mappings between two disease vocabularies. For each subject term \
you decide which, if any, candidate object concept denotes the same disease, and you record \
the curation rules your decision rests on.

What reaches you is the residue a deterministic lexical matcher could not settle, so **most \
items have no exact match and the expected answer is often "none"**. Do not manufacture \
matches to be useful. A wrong mapping silently pulls the wrong patient cohort, which is \
worse than a recorded absence.

## Direction convention

In `subject predicate object`: `narrow` means the OBJECT is narrower than the subject; \
`broad` means the OBJECT is broader. Get this backwards and the mapping is wrong even though \
the concepts are related.

## Accept `exact` only when

The two denote the same disease, allowing for house style: word order, possessives, \
British/American spelling, `disease`/`disorder`, singular/plural, and eponym-vs-descriptive \
naming of the same entity.

- `Marfan syndrome` = `Q87.4 Marfan syndrome` — same concept.
- `cystic fibrosis` = `E84 Cystic fibrosis` — the rubric denotes the disease itself.

## Reject `exact` when

- **The object is a disjunction of two diseases.** `G71.01 Duchenne or Becker muscular \
dystrophy` covers Becker too, so it is broader (SC-004).
- **The object is a grouping or chapter rubric** broader than the subject (SC-006 / PR-003).
- **The object is narrower**: a subtype, stage, site, laterality or complication variant \
(SC-005 / PR-002).
- **`unspecified` / `other` / `NOS` / `NEC` rubrics** are residual buckets, not the disease \
(SC-001 / SC-002). A specific disease listed *under* one as an inclusion term is coded there, \
but the rubric is not that disease.
- **Same organ, different disease.** Token overlap is not equivalence; several candidates are \
retrieval noise and share only a word (RJ-004).
- **The subject is a susceptibility locus** (CM-006) or a **gene-level entity** (CM-007) with \
no disease counterpart.

## The one exception that matters

A rubric whose own subdivisions are all subtypes of the subject is **coextensive** with it, \
and `exact` is correct even when its label says "unspecified" (SC-007). `E76.21 Morquio \
mucopolysaccharidoses` divides only into Morquio A, Morquio B and unspecified, and is \
therefore exactly MPS IV. Distinguishing this from a genuine residual (SC-001) is the hardest \
judgement here, which is why the candidate's children are listed for you. Use them.

## Conflation is a decision, not an error

Where the two resources hold different models of what a disease is — a sign versus a disease, \
a finding versus a disorder, cause-based versus presentation-based individuation — you may \
still map, but you must cite the `CM-*` rule for the boundary you crossed. Silently crossing \
it makes the row unreviewable. `CM-003`, `CM-006`, `CM-007` and `CM-009` are rejection \
grounds, not permissions.

## Discipline

- Use **only** ids from that item's candidate list. Never invent or recall a code.
- If two or more candidates are equally defensible, answer `none` and cite `PR-008`. \
Ambiguity is not a match.
- Cite rule ids from the registry below and nothing else.
- Set `needs_expert` when the call genuinely turns on clinical knowledge you do not have, and \
say what you would ask.
- Emit exactly one verdict per input item, in the input order.

## Curation rule registry

{rules}
"""


def build_system_prompt() -> str:
    """Return the system prompt with the rule registry interpolated.

    Example:
        >>> p = build_system_prompt()
        >>> "SC-007" in p and "EV-001" in p and "CM-006" in p
        True
        >>> "{rules}" in p
        False
    """
    return SYSTEM_PROMPT.replace("{rules}", prompt_digest())


def render_item(
    subject: Concept,
    candidates: list[Candidate],
    subject_concepts: dict[str, Concept],
    object_concepts: dict[str, Concept],
    *,
    max_children: int = 6,
) -> dict[str, Any]:
    """Render one subject and its candidates as the JSON payload the model sees.

    The rendering is the cache key, so it must be deterministic and must contain everything
    the decision depends on — notably each candidate's children, without which ``SC-007``
    cannot be told from ``SC-001``.

    Example:
        >>> from mondo_ai.align.models import Candidate, Concept
        >>> objs = {
        ...   "I:E76.21": Concept(id="I:E76.21", label="Morquio mucopolysaccharidoses",
        ...                       children=["I:E76.210"], parents=["I:E76.2"]),
        ...   "I:E76.210": Concept(id="I:E76.210", label="Morquio A mucopolysaccharidoses"),
        ...   "I:E76.2": Concept(id="I:E76.2", label="Other mucopolysaccharidoses")}
        >>> item = render_item(Concept(id="M:1", label="MPS IV"),
        ...                    [Candidate("I:E76.21", flags=["SC-001", "SC-007"])], {}, objs)
        >>> item["candidates"][0]["children"]
        ['I:E76.210 Morquio A mucopolysaccharidoses']
        >>> item["candidates"][0]["parent"]
        'I:E76.2 Other mucopolysaccharidoses'
        >>> item["candidates"][0]["flags"]
        ['SC-001', 'SC-007']
    """
    subj_payload: dict[str, Any] = {
        "id": subject.id,
        "label": subject.label,
    }
    if subject.definition:
        subj_payload["definition"] = subject.definition[:600]
    if subject.exact_synonyms:
        subj_payload["exact_synonyms"] = subject.exact_synonyms[:8]
    parents = [
        f"{p} {subject_concepts[p].label}" for p in subject.parents if p in subject_concepts
    ]
    if parents:
        subj_payload["parents"] = parents[:5]
    if subject.n_descendants:
        subj_payload["n_descendants"] = subject.n_descendants

    cand_payloads = []
    for cand in candidates:
        obj = object_concepts.get(cand.object_id)
        payload: dict[str, Any] = {"id": cand.object_id, "label": obj.label if obj else ""}
        if obj is not None:
            if obj.exact_synonyms:
                payload["synonyms"] = obj.exact_synonyms[:6]
            if obj.definition:
                payload["definition"] = obj.definition[:400]
            for p in obj.parents:
                if p in object_concepts:
                    payload["parent"] = f"{p} {object_concepts[p].label}"
                    break
            kids = [
                f"{k} {object_concepts[k].label}"
                for k in obj.children[:max_children]
                if k in object_concepts
            ]
            if kids:
                payload["children"] = kids
                if len(obj.children) > max_children:
                    payload["n_children"] = len(obj.children)
        if cand.flags:
            payload["flags"] = cand.flags
        payload["evidence"] = sorted({s.rule_id for s in cand.signals})
        cand_payloads.append(payload)

    return {"subject": subj_payload, "candidates": cand_payloads}


def verdict_from_payload(payload: dict[str, Any], allowed_ids: set[str]) -> ItemVerdict:
    """Validate a raw verdict against the offered ids and the rule registry.

    Invalid rule ids are dropped with a note appended to the justification rather than
    silently kept: an audit trail that cites a nonexistent rule is worse than one that
    admits the model reached for one.

    Raises:
        ValueError: If the decision is unknown, or the object id was not offered.

    Example:
        >>> v = verdict_from_payload({"subject_id": "M:1", "decision": "exact",
        ...     "object_id": "I:A00", "confidence": 0.8, "rules": ["EV-001", "BOGUS-9"],
        ...     "justification": "Same."}, {"I:A00"})
        >>> v.rules
        ['EV-001']
        >>> "BOGUS-9" in v.justification
        True
        >>> verdict_from_payload({"subject_id": "M:1", "decision": "exact",
        ...     "object_id": "I:ZZZ"}, {"I:A00"})
        Traceback (most recent call last):
        ...
        ValueError: verdict for M:1 names object 'I:ZZZ', which was not offered
        >>> verdict_from_payload({"subject_id": "M:1", "decision": "maybe"}, set())
        Traceback (most recent call last):
        ...
        ValueError: verdict for M:1 has unknown decision 'maybe'
    """
    v = ItemVerdict.model_validate(payload)
    if v.decision not in DECISIONS:
        raise ValueError(f"verdict for {v.subject_id} has unknown decision {v.decision!r}")
    if v.decision == "none":
        v.object_id = ""
    elif v.object_id not in allowed_ids:
        raise ValueError(
            f"verdict for {v.subject_id} names object {v.object_id!r}, "
            f"which was not offered"
        )
    bad = [r for r in v.rules if not known(r)]
    v.rules = [r.split(":")[-1] for r in v.rules if known(r)]
    v.conflation = [r.split(":")[-1] for r in v.conflation if known(r)]
    v.alternatives = [a for a in v.alternatives if a in allowed_ids]
    if bad:
        v.justification = (
            f"{v.justification} [pipeline note: dropped unknown rule ids "
            f"{', '.join(bad)}]"
        ).strip()
    return v


def verdict_to_adjudication(v: ItemVerdict, *, path: str = "A1") -> Adjudication:
    """Convert a validated verdict into the pipeline's :class:`Adjudication`.

    ``EV-012`` is always added: the row was produced by automated adjudication over
    retrieved candidates, and the Mondo tiering policy turns on that fact.

    Example:
        >>> v = ItemVerdict(subject_id="M:1", decision="broad", object_id="I:E83.31",
        ...                 confidence=0.7, rules=["SC-006"], justification="Broader.")
        >>> a = verdict_to_adjudication(v)
        >>> a.predicate, a.rule_ids, a.decision_path
        ('skos:broadMatch', ['SC-006', 'EV-012'], 'A1')
    """
    rule_ids = list(v.rules)
    for extra in list(v.conflation) + ["EV-012"]:
        if extra not in rule_ids:
            rule_ids.append(extra)
    return Adjudication(
        source="agent",
        decision=v.decision,
        object_id=v.object_id,
        confidence=max(0.0, min(1.0, v.confidence)),
        rule_ids=rule_ids,
        justification=v.justification,
        decision_path=path,
        alternatives=v.alternatives,
        conflation=v.conflation,
        needs_expert=v.needs_expert,
        expert_question=v.expert_question,
    )


# --- transport -------------------------------------------------------------------------


class Completer(Protocol):
    """A callable that turns a system prompt and a user message into parsed verdicts.

    Isolating the transport behind this protocol is what makes the batching, validation,
    retry and caching logic testable without a network call.
    """

    def __call__(self, system: str, user: str) -> BatchVerdict:  # pragma: no cover
        ...


@dataclass
class AnthropicCompleter:
    """Structured-output completer backed by the Anthropic Messages API.

    Attributes:
        model: Model id.
        max_tokens: Output cap. Batches of 20 verdicts with justifications fit comfortably
            in 16k; a truncated response is retried by the caller.
        effort: ``output_config.effort``. Adjudication is a judgement task, so the default
            is high.
    """

    model: str = DEFAULT_MODEL
    max_tokens: int = 16000
    effort: str = "high"
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
    )
    _client: Any = field(default=None, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def _get_client(self) -> Any:
        # One client shared across worker threads: the SDK is thread-safe and pools
        # connections, and a client per thread would multiply TLS handshakes for nothing.
        with self._lock:
            if self._client is None:
                import anthropic

                if not os.environ.get("ANTHROPIC_API_KEY"):
                    from dotenv import load_dotenv

                    load_dotenv()
                self._client = anthropic.Anthropic(max_retries=4)
            return self._client

    def __call__(self, system: str, user: str) -> BatchVerdict:
        client = self._get_client()
        response = client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            output_format=BatchVerdict,
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": user}],
        )
        with self._lock:
            # Recorded so the run report can state what it actually spent, which is the
            # number the user needs in order to decide whether to lift the item cap.
            for key in self.usage:
                self.usage[key] += getattr(response.usage, key, 0) or 0
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError(
                f"model returned no parsed output (stop_reason={response.stop_reason})"
            )
        return parsed


class ResponseCache:
    """Content-addressed JSON cache under a directory.

    Example:
        >>> import tempfile
        >>> c = ResponseCache(tempfile.mkdtemp())
        >>> c.get("k") is None
        True
        >>> c.put("k", {"a": 1}); c.get("k")
        {'a': 1}
        >>> ResponseCache(None).get("k") is None
        True
        >>> ResponseCache(None).put("k", {"a": 1})
    """

    def __init__(self, directory: str | Path | None) -> None:
        self.directory = Path(directory) if directory else None
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path | None:
        return None if self.directory is None else self.directory / f"{key}.json"

    def get(self, key: str) -> Any | None:
        """Return the cached value, or None."""
        p = self._path(key)
        if p is None or not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry is a cache miss, not a run failure.
            return None

    def put(self, key: str, value: Any) -> None:
        """Store a value under a key."""
        p = self._path(key)
        if p is None:
            return
        p.write_text(json.dumps(value, sort_keys=True))


def batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    """Split a list into consecutive batches.

    Example:
        >>> [len(b) for b in batched([1, 2, 3, 4, 5], 2)]
        [2, 2, 1]
        >>> list(batched([], 3))
        []
    """
    for i in range(0, len(items), size):
        yield items[i: i + size]


@dataclass
class AgentAdjudicator:
    """Batches, calls, validates, retries and caches agent adjudication.

    Args:
        completer: The transport. Injected so tests exercise the real logic.
        model: Model id, part of the cache key.
        batch_size: Items per request.
        cache_dir: Where to cache responses; None disables caching.
        on_progress: Optional callback receiving ``(done, total)`` after each batch.
    """

    completer: Completer
    model: str = DEFAULT_MODEL
    batch_size: int = 15
    cache_dir: str | Path | None = None
    on_progress: Callable[[int, int], None] | None = None
    concurrency: int = 1

    def __post_init__(self) -> None:
        self.cache = ResponseCache(self.cache_dir)
        self.system = build_system_prompt()
        self.calls = 0
        self.cache_hits = 0
        self.failures: list[str] = []
        self._lock = threading.Lock()

    def cache_key(self, item: dict[str, Any]) -> str:
        """Return the cache key for one rendered item.

        Example:
            >>> a = AgentAdjudicator(completer=lambda s, u: BatchVerdict(verdicts=[]))
            >>> a.cache_key({"subject": {"id": "M:1"}}) == a.cache_key({"subject": {"id": "M:1"}})
            True
            >>> a.cache_key({"subject": {"id": "M:1"}}) == a.cache_key({"subject": {"id": "M:2"}})
            False
        """
        return content_hash(self.model, json.dumps(item, sort_keys=True))

    def adjudicate(self, items: list[dict[str, Any]]) -> dict[str, Adjudication]:
        """Adjudicate rendered items, returning subject id -> verdict.

        Cached items never reach the transport. Uncached items are batched; a batch whose
        response fails validation is retried once as single-item requests, which isolates
        the bad item instead of discarding its neighbours. Items that still fail are
        returned as ``needs_expert`` with ``RJ-002`` rather than dropped — a silent
        omission would read downstream as "nobody looked".

        Example:
            >>> def fake(system, user):
            ...     payload = json.loads(user[user.index("["):])
            ...     return BatchVerdict(verdicts=[
            ...         ItemVerdict(subject_id=i["subject"]["id"], decision="none",
            ...                     justification="Closest was noise.") for i in payload])
            >>> subj = {"subject": {"id": "M:1", "label": "x"}, "candidates": []}
            >>> a = AgentAdjudicator(completer=fake, batch_size=2)
            >>> out = a.adjudicate([subj])
            >>> out["M:1"].decision, out["M:1"].source, a.calls
            ('none', 'agent', 1)

            A response naming an id that was not offered is isolated, not trusted:

            >>> def liar(system, user):
            ...     payload = json.loads(user[user.index("["):])
            ...     return BatchVerdict(verdicts=[
            ...         ItemVerdict(subject_id=i["subject"]["id"], decision="exact",
            ...                     object_id="I:INVENTED") for i in payload])
            >>> b = AgentAdjudicator(completer=liar, batch_size=2)
            >>> out = b.adjudicate([subj])
            >>> out["M:1"].decision, out["M:1"].needs_expert, out["M:1"].rule_ids
            ('none', True, ['RJ-002'])
            >>> b.failures
            ['M:1']
        """
        results: dict[str, Adjudication] = {}
        pending: list[dict[str, Any]] = []
        for item in items:
            cached = self.cache.get(self.cache_key(item))
            if cached is not None:
                self.cache_hits += 1
                try:
                    v = verdict_from_payload(cached, self._allowed(item))
                    results[item["subject"]["id"]] = verdict_to_adjudication(
                        v, path="A1-cached")
                    continue
                except ValueError:
                    pass  # a stale cache entry is a miss
            pending.append(item)

        total = len(pending)
        batches = list(batched(pending, self.batch_size))
        if not batches:
            return results

        state = {"done": 0}

        def process(batch: list[dict[str, Any]]) -> dict[str, Adjudication]:
            out = self._run_batch(batch)
            if out is None:
                # Retry item-wise so one bad verdict cannot discard its neighbours.
                out = {}
                for single in batch:
                    single_out = self._run_batch([single])
                    if single_out is None:
                        sid = single["subject"]["id"]
                        with self._lock:
                            self.failures.append(sid)
                        out[sid] = _failed_adjudication()
                    else:
                        out.update(single_out)
            with self._lock:
                state["done"] += len(batch)
                if self.on_progress:
                    self.on_progress(state["done"], total)
            return out

        if self.concurrency <= 1:
            for batch in batches:
                results.update(process(batch))
            return results

        # Adjudication is embarrassingly parallel and entirely latency-bound: each batch is
        # one independent request. Serially, a full-vocabulary queue takes days.
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for out in pool.map(process, batches):
                results.update(out)
        return results

    @staticmethod
    def _allowed(item: dict[str, Any]) -> set[str]:
        """Return the candidate ids offered for one item.

        Example:
            >>> AgentAdjudicator._allowed({"candidates": [{"id": "I:A00"}]})
            {'I:A00'}
        """
        return {c["id"] for c in item.get("candidates", [])}

    def _run_batch(self, batch: list[dict[str, Any]]) -> dict[str, Adjudication] | None:
        """Call the transport for one batch, returning None if anything fails validation."""
        user = (
            f"Adjudicate these {len(batch)} items. Return exactly {len(batch)} verdicts, "
            f"one per item, in this order.\n\n"
            + json.dumps(batch, indent=1, sort_keys=True)
        )
        try:
            response = self.completer(self.system, user)
        except Exception:  # transport failure: the caller retries this batch item-wise
            return None
        with self._lock:
            self.calls += 1

        by_id = {item["subject"]["id"]: item for item in batch}
        out: dict[str, Adjudication] = {}
        seen: set[str] = set()
        for raw in response.verdicts:
            payload = raw.model_dump()
            item = by_id.get(payload.get("subject_id", ""))
            if item is None:
                return None
            try:
                v = verdict_from_payload(payload, self._allowed(item))
            except ValueError:
                return None
            seen.add(v.subject_id)
            out[v.subject_id] = verdict_to_adjudication(v)
            self.cache.put(self.cache_key(item), payload)
        if seen != set(by_id):
            return None
        return out


def _failed_adjudication() -> Adjudication:
    """Return the verdict for an item whose adjudication could not be validated.

    A failure is recorded as an escalation, never as a "no match": downstream those read as
    opposite claims, and a silent omission would say "we looked and there is nothing".

    Example:
        >>> a = _failed_adjudication()
        >>> a.decision, a.needs_expert, a.rule_ids, a.decision_path
        ('none', True, ['RJ-002'], 'A1-failed')
    """
    return Adjudication(
        source="agent",
        decision="none",
        rule_ids=["RJ-002"],
        justification=(
            "Adjudication could not be validated (the model named an identifier that was "
            "not offered, or returned an unparseable verdict); routed to expert review."
        ),
        decision_path="A1-failed",
        needs_expert=True,
        expert_question="Which object concept, if any, denotes this term?",
    )


#: Published per-million-token prices, for reporting spend. Cache reads are 0.1x input and
#: cache writes 1.25x, per the API's pricing model.
MODEL_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


def estimate_cost(model: str, usage: dict[str, int]) -> float | None:
    """Return the USD cost of a usage tally, or None for an unpriced model.

    Returns None rather than 0.0 for an unknown model: a confident zero would be worse than
    an admitted gap when the number is going into a spending decision.

    Example:
        >>> estimate_cost("claude-opus-5", {"input_tokens": 1_000_000,
        ...                                 "output_tokens": 1_000_000,
        ...                                 "cache_read_input_tokens": 0,
        ...                                 "cache_creation_input_tokens": 0})
        30.0
        >>> estimate_cost("claude-opus-5", {"input_tokens": 0, "output_tokens": 0,
        ...     "cache_read_input_tokens": 1_000_000,
        ...     "cache_creation_input_tokens": 0})
        0.5
        >>> estimate_cost("some-future-model", {"input_tokens": 1}) is None
        True
    """
    price = MODEL_PRICES.get(model)
    if price is None:
        return None
    inp, out = price
    per_m = 1_000_000
    total = (
        usage.get("input_tokens", 0) * inp
        + usage.get("output_tokens", 0) * out
        + usage.get("cache_read_input_tokens", 0) * inp * 0.1
        + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
    ) / per_m
    return round(total, 4)
