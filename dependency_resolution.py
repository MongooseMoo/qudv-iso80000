"""Required dependency ordering and ordered, grounded inference.

These are different operations: a required edge delays a node, while an
unavailable inference alternative does not prevent trying the next rule.
None means unresolved; falsey values such as dimension one ({}) are facts.
"""

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from typing import Optional, TypeVar

Value = TypeVar('Value')
Dependencies = Mapping[str, Iterable[str]]
Alternatives = Mapping[str, Sequence[Sequence[str]]]


def resolve_dependencies(
    dependencies: Dependencies,
    evaluate: Callable[[str, Mapping[str, Optional[Value]]], Optional[Value]],
) -> dict[str, Optional[Value]]:
    """Evaluate the acyclic portion of a graph of required dependencies.

    Missing dependencies and nodes blocked by cycles remain None. Evaluators
    receive only previously processed values, including unresolved inputs.
    """
    sorter = TopologicalSorter({node: sorted(parents)
                                for node, parents in sorted(dependencies.items())})
    try:
        sorter.prepare()
    except CycleError:
        pass  # Python still exposes the independent acyclic portion.
    values: dict[str, Optional[Value]] = {}
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        if not ready:
            break
        for node in ready:
            values[node] = evaluate(node, values) if node in dependencies else None
        sorter.done(*ready)
    return {node: values.get(node) for node in dependencies}


def infer_alternatives(
    alternatives: Alternatives,
    evaluate: Callable[[str, int, Mapping[str, Value]], Value],
) -> dict[str, Optional[Value]]:
    """Infer facts from ordered alternative rules using a dependency work queue.

    Each rule is a sequence of required input IDs. An empty sequence is a
    source fact. evaluate(node, rule_index, values) must return a value once
    those inputs are known. Nodes without a usable rule remain unresolved.

    A fallback is provisional: a preferred rule can replace it when grounded
    inputs arrive. The selected rules always form an acyclic proof, so a node
    cannot justify itself through a downstream result. Rules only move toward
    higher priority; fixed proofs propagate along a DAG. Both operations are
    finite, without a retry limit or recursive calls.
    """
    subscribers: defaultdict[str, set[str]] = defaultdict(set)
    for node, rules in alternatives.items():
        for parents in rules:
            for parent in parents:
                subscribers[parent].add(node)

    values: dict[str, Value] = {}
    selected: dict[str, int] = {}
    queue = deque(sorted(alternatives))
    queued = set(queue)

    def enqueue(nodes: Iterable[str]) -> None:
        for node in sorted(nodes):
            if node not in queued:
                queue.append(node)
                queued.add(node)

    def would_cycle(node: str, parents: Sequence[str]) -> bool:
        pending = list(parents)
        visited: set[str] = set()
        while pending:
            parent = pending.pop()
            if parent == node:
                return True
            if parent in visited:
                continue
            visited.add(parent)
            if parent in selected:
                pending.extend(alternatives[parent][selected[parent]])
        return False

    while queue:
        node = queue.popleft()
        queued.remove(node)
        previous = selected.get(node)
        for index, parents in enumerate(alternatives[node]):
            if previous is not None and index > previous:
                break
            if any(parent not in values for parent in parents):
                continue
            # A first proof cannot reach an unresolved node. Only replacing
            # an existing proof can introduce a back edge.
            if previous is not None and index < previous and would_cycle(node, parents):
                continue
            value = evaluate(node, index, values)
            if value is None:
                raise ValueError('an inference rule with known inputs returned None')
            changed = node not in values or value != values[node]
            selected[node] = index
            values[node] = value
            if changed or index != previous:
                enqueue(subscribers[node])
            if previous is not None and index < previous:
                # Replacing a proof can make a previously circular preferred
                # alternative independent, even if its numeric value is equal.
                enqueue(n for n, choice in selected.items() if choice > 0)
            break

    return {node: values.get(node) for node in alternatives}


def unresolved_cycles(dependencies: Dependencies, values: Mapping[str, object]) -> list[list[str]]:
    """Return every cyclic component of the unresolved graph, deterministically.

    Two iterative DFS passes (Kosaraju) distinguish cycle members from nodes
    merely downstream. Overlapping cycles belong to one component.
    """
    pending = {node for node in dependencies if values.get(node) is None}
    graph = {node: sorted(parent for parent in dependencies[node] if parent in pending)
             for node in sorted(pending)}
    visited: set[str] = set()
    finished: list[str] = []
    for root in graph:
        walk = [(root, False)]
        while walk:
            node, leaving = walk.pop()
            if leaving:
                finished.append(node)
            elif node not in visited:
                visited.add(node)
                walk.append((node, True))
                walk.extend((parent, False) for parent in reversed(graph[node]))

    reverse: defaultdict[str, list[str]] = defaultdict(list)
    for node, parents in graph.items():
        for parent in parents:
            reverse[parent].append(node)
    visited = set()
    components: list[list[str]] = []
    for root in reversed(finished):
        if root in visited:
            continue
        component: list[str] = []
        stack = [root]
        visited.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for child in reverse[node]:
                if child not in visited:
                    visited.add(child)
                    stack.append(child)
        if len(component) > 1 or root in graph[root]:
            components.append(sorted(component))
    return sorted(components)
