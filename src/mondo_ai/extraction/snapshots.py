"""Read and write ResourceSnapshot YAML files.

A snapshot is the local, versioned form of a source ontology (``mondo_ai_sieve.yaml``).
Snapshots of large sources (Mondo, ICD-10-CM) run to tens of megabytes, so the YAML is
read and written with libyaml's C loader and dumper when available.

Example:
    >>> save_snapshot(snapshot, "data/snapshots/doid.yaml")  # doctest: +SKIP
    >>> snapshot = load_snapshot("data/snapshots/doid.yaml")  # doctest: +SKIP
"""

from pathlib import Path

import yaml

from mondo_ai.datamodel import ResourceSnapshot

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def save_snapshot(snapshot: ResourceSnapshot, path: str | Path) -> Path:
    """Write a snapshot as YAML, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(
            snapshot.model_dump(mode="json", exclude_none=True),
            f,
            Dumper=_Dumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    return path


def load_snapshot(path: str | Path) -> ResourceSnapshot:
    """Read a snapshot YAML file."""
    with open(path) as f:
        data = yaml.load(f, Loader=_Loader)
    return ResourceSnapshot.model_validate(data)
