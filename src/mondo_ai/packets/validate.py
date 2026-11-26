"""Validate packets against the profile: the LinkML schema and sieve's profile checks.

``linkml-validate`` covers ranges, enums, cardinality and the class rules;
``sieve.profile_checks`` adds what the schema cannot express (predicate value sets, the
claim-evidence matrix, identical copies, self-containment, ...).

Example:
    >>> problems = validate_packet(packet)  # doctest: +SKIP
"""

from functools import lru_cache
from typing import Any

from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin
from linkml_runtime.utils.schemaview import SchemaView
from sieve.profile_checks import Issue, check_packet

from mondo_ai.packets.registry import SCHEMA_PATH


@lru_cache(maxsize=1)
def _validator() -> Validator:
    """A validator over the overlay schema with its imports merged in.

    The LinkML validator resolves imports against the working directory, so the vendored
    ``sieve`` and ``sepio_core`` imports are merged before validating. The JSON Schema is
    closed, so unknown slots are errors too. The statement slot is a union of the claim
    types, so an error inside a statement may be reported against the last claim type
    the validator tried rather than the packet's own.
    """
    sv = SchemaView(str(SCHEMA_PATH))
    sv.merge_imports()
    return Validator(sv.schema, validation_plugins=[JsonschemaValidationPlugin(closed=True)])


def schema_errors(packet: dict[str, Any]) -> list[str]:
    """LinkML schema validation messages for one packet (empty if valid)."""
    return [r.message for r in _validator().validate(packet, "EvidencePacket").results]


def profile_issues(packet: dict[str, Any], severity: str = "ERROR") -> list[Issue]:
    """sieve profile-check issues of one packet at the given severity."""
    return [i for i in check_packet(packet) if i.severity == severity]


def validate_packet(packet: dict[str, Any]) -> list[str]:
    """Every schema error and profile ERROR of one packet, as messages."""
    return schema_errors(packet) + [f"{i.code}: {i.message}" for i in profile_issues(packet)]
