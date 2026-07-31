"""Locating differences between two normalized documents.

Both real-lab comparisons — the two bundles' observable contract and the pre/post
stable-state snapshot — reduce their subject to a plain JSON-shaped document and
then ask the same question: *where* do these two differ?  A whole-document
equality check answers "somewhere", which is not an answer an operator can act
on, so this walk reports one line per differing leaf with both values truncated
to something a report can carry.

The lines are the only thing that reaches a report, so they are bounded here
rather than trusted to be short.
"""

from __future__ import annotations


DIFFERENCE_VALUE_LIMIT = 200
DIFFERENCE_LIMIT = 50


def describe_differences(
    reference: object, candidate: object, path: str = "", *, limit: int = DIFFERENCE_LIMIT
) -> tuple[str, ...]:
    """Name every leaf where `candidate` differs from `reference`.

    `limit` bounds the report, not the verdict: any non-empty result is a
    failure, so truncating a very large difference list costs nothing but noise.
    The truncation is announced rather than silent.
    """

    found = list(_walk(reference, candidate, path))
    if len(found) <= limit:
        return tuple(found)
    return tuple(found[:limit]) + (
        f"… and {len(found) - limit} further difference(s) not listed",
    )


def _walk(reference: object, candidate: object, path: str) -> list[str]:
    location = path or "<document>"
    if type(reference) is not type(candidate):
        return [
            f"{location}: {type(reference).__name__} != {type(candidate).__name__}"
        ]
    if isinstance(reference, dict):
        assert isinstance(candidate, dict)
        differences: list[str] = []
        for key in sorted(set(reference) | set(candidate), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in reference:
                differences.append(f"{child}: only the candidate has it")
            elif key not in candidate:
                differences.append(f"{child}: only the reference has it")
            else:
                differences += _walk(reference[key], candidate[key], child)
        return differences
    if isinstance(reference, list):
        assert isinstance(candidate, list)
        if len(reference) != len(candidate):
            only_reference = [item for item in reference if item not in candidate]
            only_candidate = [item for item in candidate if item not in reference]
            return [
                f"{location}: {len(reference)} entries != {len(candidate)}; "
                f"reference-only={short(only_reference)} "
                f"candidate-only={short(only_candidate)}"
            ]
        return [
            difference
            for index, (left, right) in enumerate(zip(reference, candidate))
            for difference in _walk(left, right, f"{path}[{index}]")
        ]
    if reference != candidate:
        return [f"{location}: {short(reference)} != {short(candidate)}"]
    return []


def short(value: object, limit: int = DIFFERENCE_VALUE_LIMIT) -> str:
    """Render one value small enough for a report line."""

    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"
