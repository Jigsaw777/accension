"""Pure dependency normalization shared by bounded plans and persistent graphs."""

from collections.abc import Iterable, Mapping


def normalize_dag(
    ids: Iterable[str],
    edges: Iterable[tuple[str, str]] = (),
    dependencies: Mapping[str, Iterable[str]] | None = None,
    *,
    reject_duplicate_edges: bool = False,
) -> dict[str, list[str]]:
    ids = list(ids)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate task ids")
    known = set(ids)
    normalized = {item: set((dependencies or {}).get(item, ())) for item in ids}
    if dependencies and not set(dependencies) <= known:
        raise ValueError("Unknown dependency")
    seen = set()
    for parent, child in edges:
        if parent not in known or child not in known:
            raise ValueError("Unknown dependency edge")
        if reject_duplicate_edges and (parent, child) in seen:
            raise ValueError("Duplicate dependency edge")
        seen.add((parent, child))
        normalized[child].add(parent)
    if any(not deps <= known for deps in normalized.values()):
        raise ValueError("Unknown dependency")
    pending = dict(normalized)
    while pending:
        ready = {item for item, deps in pending.items() if not deps}
        if not ready:
            raise ValueError("Task DAG contains a cycle")
        pending = {item: deps - ready for item, deps in pending.items() if item not in ready}
    return {item: sorted(deps) for item, deps in normalized.items()}
