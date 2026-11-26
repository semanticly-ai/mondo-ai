"""Validation reports for SIEVE profile packets.

A thin reporting layer over :mod:`mondo_ai.packets.validate`: each packet is checked
against the LinkML schema and sieve's profile checks; schema errors and profile ERRORs
make it invalid, profile WARNINGs (e.g. ``READ_COVERAGE_NOT_RECORDED``) are reported
but do not. Packets come from a packet file (YAML, JSON or JSONL; one packet or a list),
a directory of packet files, or a :class:`~mondo_ai.storage.jsonl_store.JSONLPacketStore`.

Example:
    >>> from mondo_ai.validation import validate_store
    >>> result = validate_store(Path("packet_store"), sample=1000)  # doctest: +SKIP
    >>> print(result.summary())  # doctest: +SKIP
"""

import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from mondo_ai.packets.validate import profile_issues, schema_errors
from mondo_ai.storage.jsonl_store import JSONLPacketStore

PACKET_SUFFIXES = (".yaml", ".yml", ".json", ".jsonl")
NON_PACKET_FILES = {"summary.yaml", "curation_records.yaml", "manifest.json", "index.json"}


@dataclass
class ValidationError:
    """One problem found in a packet.

    Attributes:
        packet_id: Id of the packet (or the file stem when the file did not parse).
        code: ``SCHEMA`` for LinkML schema errors, ``PARSE`` for unreadable files,
            otherwise the sieve profile-check code (e.g. ``UNRESOLVED_REFERENCE``).
        message: Human-readable message.
        severity: ``ERROR`` or ``WARNING``.

    >>> ValidationError("mondoai:pkt/x", "SCHEMA", "bad").field
    'SCHEMA'
    """

    packet_id: str
    code: str
    message: str
    severity: str = "ERROR"

    @property
    def field(self) -> str:
        """The error code, under the name older callers used."""
        return self.code


@dataclass
class ValidationResult:
    """Outcome of validating one or more packets.

    >>> r = ValidationResult(valid_count=2)
    >>> r.is_valid
    True
    >>> r.merge(ValidationResult(invalid_count=1, errors=[ValidationError("p", "SCHEMA", "bad")]))
    >>> (r.valid_count, r.invalid_count, r.is_valid)
    (2, 1, False)
    """

    valid_count: int = 0
    invalid_count: int = 0
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    checked_ids: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if no packet had an error."""
        return self.invalid_count == 0

    def merge(self, other: "ValidationResult") -> None:
        """Add another result's counts and issues to this one."""
        self.valid_count += other.valid_count
        self.invalid_count += other.invalid_count
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.checked_ids.extend(other.checked_ids)

    def summary(self) -> str:
        """Counts of valid and invalid packets, errors and warnings, each by code.

        >>> print(ValidationResult(valid_count=1).summary())
        Validation Summary:
          Valid packets: 1
          Invalid packets: 0
          Errors: 0
          Warnings: 0
        """
        lines = [
            "Validation Summary:",
            f"  Valid packets: {self.valid_count}",
            f"  Invalid packets: {self.invalid_count}",
            f"  Errors: {len(self.errors)}",
        ]
        lines += [f"    - {code}: {n}" for code, n in sorted(Counter(e.code for e in self.errors).items())]
        lines.append(f"  Warnings: {len(self.warnings)}")
        lines += [f"    - {code}: {n}" for code, n in sorted(Counter(w.code for w in self.warnings).items())]
        return "\n".join(lines)


def validate_packet_data(packet: dict[str, Any]) -> ValidationResult:
    """Validate one packet dict: schema errors, profile ERRORs and profile WARNINGs."""
    packet_id = str(packet.get("id", "unknown"))
    errors = [ValidationError(packet_id, "SCHEMA", message) for message in schema_errors(packet)]
    errors += [ValidationError(packet_id, i.code, i.message) for i in profile_issues(packet, "ERROR")]
    warnings = [ValidationError(packet_id, i.code, i.message, "WARNING") for i in profile_issues(packet, "WARNING")]
    return ValidationResult(
        valid_count=0 if errors else 1,
        invalid_count=1 if errors else 0,
        errors=errors,
        warnings=warnings,
        checked_ids=[packet_id],
    )


def validate_packets(packets: Iterable[dict[str, Any]]) -> ValidationResult:
    """Validate packet dicts and combine the results."""
    result = ValidationResult()
    for packet in packets:
        result.merge(validate_packet_data(packet))
    return result


def _parse_error(path: Path, message: str) -> ValidationResult:
    return ValidationResult(invalid_count=1, errors=[ValidationError(path.stem, "PARSE", message)])


def _load(path: Path) -> Any:
    """The parsed content of a packet file: a dict, a list, or anything else YAML yields."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def validate_packet_file(path: Path) -> ValidationResult:
    """Validate the packet(s) in a YAML, JSON or JSONL file.

    Args:
        path: A file holding one packet, a list of packets, or (``.jsonl``) one packet per line.

    Returns:
        The combined result; a file that does not parse, or holds something other than
        packets, counts as one invalid packet with a ``PARSE`` error.
    """
    try:
        content = _load(path)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        return _parse_error(path, f"could not parse {path.name}: {e}")
    packets = content if isinstance(content, list) else [content]
    if not all(isinstance(p, dict) for p in packets):
        return _parse_error(path, f"{path.name} does not hold a packet or a list of packets")
    return validate_packets(packets)


def validate_packet_directory(path: Path) -> ValidationResult:
    """Validate every packet file (``*.yaml``, ``*.yml``, ``*.json``, ``*.jsonl``) in a directory."""
    result = ValidationResult()
    for file in sorted(path.iterdir()):
        if file.is_file() and file.suffix in PACKET_SUFFIXES and file.name not in NON_PACKET_FILES:
            result.merge(validate_packet_file(file))
    return result


def _reservoir(items: Iterator[dict[str, Any]], k: int, rng: random.Random) -> list[dict[str, Any]]:
    """A uniform random sample of k items from a stream (all of them if there are fewer).

    >>> len(_reservoir(iter([{"id": i} for i in range(10)]), 3, random.Random(0)))
    3
    >>> len(_reservoir(iter([{"id": 1}]), 3, random.Random(0)))
    1
    """
    sample: list[dict[str, Any]] = []
    for n, item in enumerate(items):
        if n < k:
            sample.append(item)
        else:
            j = rng.randrange(n + 1)
            if j < k:
                sample[j] = item
    return sample


def validate_store(store_dir: Path, sample: int | None = None, seed: int = 0) -> ValidationResult:
    """Validate the packets of a JSONL packet store, or a random sample of them.

    Args:
        store_dir: Directory of a :class:`JSONLPacketStore` (holding ``manifest.json``).
        sample: Validate only this many packets, drawn uniformly at random.
        seed: Seed of the sample, so the same store and seed check the same packets.

    Returns:
        The combined result over the packets checked.

    Raises:
        FileNotFoundError: If ``store_dir`` does not exist.
    """
    if not Path(store_dir).is_dir():
        raise FileNotFoundError(f"No packet store at {store_dir}")
    packets = JSONLPacketStore(store_dir).iter_packets()
    if sample is None:
        return validate_packets(packets)
    return validate_packets(_reservoir(packets, sample, random.Random(seed)))
