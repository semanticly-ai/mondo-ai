"""Inclusion score S for evidence packets.

S is computed by sieve's reference implementation (``sieve.inclusion_score``, D-063):
every parameter comes from the claim type's AggregationRule frozen in the packet's
``reference_snapshot``, so the same packet version always scores the same. Per valid
line w = d * s * q * c, the strongest line per independence cluster counts,
L = L0 + k * sum(w), and S = tanh(L / 2) in [-1, 1].

The score is written onto the statement in the profile's "proposition assessment mode":
``score`` = S, ``strength`` = its band, ``direction`` = its sign. It is not a decision:
nothing here accepts or rejects a claim.

Example:
    >>> from mondo_ai.scoring.scorer import score_packet
    >>> packet = score_packet(packet)  # doctest: +SKIP
    >>> packet["statement"]["score"]  # doctest: +SKIP
    0.9085
"""

from typing import Any, Iterable

from sieve.inclusion_score import ScoreBreakdown
from sieve.inclusion_score import score_packet as _reference_score


def explain(packet: dict[str, Any]) -> ScoreBreakdown:
    """Compute S and every step that produced it, without changing the packet."""
    return _reference_score(packet)


def score_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Set the statement's score, strength and direction from S; return the packet.

    >>> rule = {"id": "r", "claim_type": "SynonymStatement", "scaling_factor": 2.0,
    ...         "strength_bands": ["strong >= 0.8", "moderate >= 0.4", "weak < 0.4"]}
    >>> line = {"id": "l", "type": "SieveEvidenceLine", "direction_of_evidence_provided": "supports",
    ...         "score_of_evidence_provided": 0.9, "has_evidence_items": [],
    ...         "contributions": [{"method_type": "RULE_BASED_STRENGTH"}]}
    >>> p = {"statement": {"type": "SynonymStatement", "has_evidence_lines": [line]},
    ...      "reference_snapshot": {"aggregation_rules": [rule]}}
    >>> score_packet(p)["statement"]["score"], p["statement"]["strength"], p["statement"]["direction"]
    (0.4219, 'moderate', 'supports')
    """
    breakdown = _reference_score(packet)
    statement = packet["statement"]
    statement["score"] = round(breakdown.s, 4)
    for key in ("strength", "direction"):
        statement.pop(key, None)
    if breakdown.band:
        statement["strength"] = breakdown.band
    if breakdown.s:
        statement["direction"] = "supports" if breakdown.s > 0 else "disputes"
    return packet


def score_packets(packets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score many packets in place; return them as a list."""
    return [score_packet(p) for p in packets]
