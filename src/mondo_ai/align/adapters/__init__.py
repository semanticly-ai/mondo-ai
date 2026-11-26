"""Resource adapters: anything reducible to labelled concepts can be aligned.

An adapter turns one file into a :class:`~mondo_ai.align.models.ResourceSet`. Format is
declared in the run config, never sniffed: guessing wrong on a 100 MB file wastes minutes
and silently produces a half-loaded index, which is worse than an error.

Example:
    >>> sorted(ADAPTERS)
    ['obo', 'obojson', 'owl', 'sssom', 'tsv', 'yaml']
"""

from __future__ import annotations

from typing import Callable

from mondo_ai.align.adapters.obo import load_obo
from mondo_ai.align.adapters.obojson import load_obojson
from mondo_ai.align.adapters.owl import load_owl
from mondo_ai.align.adapters.tabular import load_sssom, load_tsv, load_yaml
from mondo_ai.align.models import ResourceSet

#: Declared format name -> loader.
ADAPTERS: dict[str, Callable[..., ResourceSet]] = {
    "obo": load_obo,
    "obojson": load_obojson,
    "owl": load_owl,
    "sssom": load_sssom,
    "tsv": load_tsv,
    "yaml": load_yaml,
}


def load_resource(fmt: str, **kwargs) -> ResourceSet:
    """Dispatch to the adapter for a declared format.

    Args:
        fmt: One of :data:`ADAPTERS`.
        **kwargs: Passed through to the adapter.

    Returns:
        The loaded resource.

    Raises:
        ValueError: On an unknown format.

    Example:
        >>> load_resource("nope")
        Traceback (most recent call last):
        ...
        ValueError: unknown resource format 'nope'; known: obo, obojson, owl, sssom, tsv, yaml
    """
    if fmt not in ADAPTERS:
        raise ValueError(
            f"unknown resource format {fmt!r}; known: {', '.join(sorted(ADAPTERS))}"
        )
    return ADAPTERS[fmt](**kwargs)


__all__ = ["ADAPTERS", "load_resource", "load_obo", "load_obojson", "load_owl",
           "load_sssom", "load_tsv", "load_yaml"]
