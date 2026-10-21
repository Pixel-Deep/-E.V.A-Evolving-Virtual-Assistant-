"""Small, deterministic algorithm library used by EVA; functions are unit-tested."""
from __future__ import annotations
import heapq
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from typing import TypeVar

T = TypeVar("T")
V = TypeVar("V", bound=Hashable)

def binary_search(items: Sequence[T], target: T) -> int:
    """Return target index in ascending sorted items, or -1 if absent. O(log n)."""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(items) and items[lo] == target else -1

def merge_sort(items: Iterable[T], *, key: Callable[[T], object] | None = None) -> list[T]:
    """Stable merge sort returning a new list; O(n log n) time and O(n) memory."""
    values = list(items)
    key_fn = key or (lambda x: x)
    def sort(seq: list[T]) -> list[T]:
        if len(seq) < 2:
            return seq
        mid = len(seq) // 2
        left, right = sort(seq[:mid]), sort(seq[mid:])
        out: list[T] = []
        i = j = 0
        while i < len(left) and j < len(right):
            if key_fn(left[i]) <= key_fn(right[j]):
                out.append(left[i]); i += 1
            else:
                out.append(right[j]); j += 1
        out.extend(left[i:]); out.extend(right[j:])
        return out
    return sort(values)

def breadth_first_search(graph: Mapping[V, Iterable[V]], start: V) -> list[V]:
    """Return BFS visitation order; neighbors absent as keys are treated as leaves."""
    visited = {start}
    queue = deque([start])
    order: list[V] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbor in graph.get(node, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return order

def dijkstra(graph: Mapping[V, Iterable[tuple[V, float]]], start: V) -> dict[V, float]:
    """Shortest distances for non-negative weighted graphs; raises on negative weights."""
    distances: dict[V, float] = {start: 0.0}
    heap: list[tuple[float, int, V]] = [(0.0, 0, start)]
    serial = 1
    while heap:
        distance, _, node = heapq.heappop(heap)
        if distance != distances.get(node):
            continue
        for neighbor, weight in graph.get(node, ()):
            if weight < 0:
                raise ValueError("Dijkstra requires non-negative edge weights.")
            candidate = distance + weight
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                heapq.heappush(heap, (candidate, serial, neighbor))
                serial += 1
    return distances

ALGORITHM_METADATA = {
    "binary_search": {"complexity": "O(log n) time, O(1) auxiliary space", "preconditions": ["sorted ascending sequence"], "description": "Index of target in sorted sequence, or -1"},
    "merge_sort": {"complexity": "O(n log n) time, O(n) auxiliary space", "preconditions": [], "description": "Stable sorting; returns a new list"},
    "breadth_first_search": {"complexity": "O(V+E)", "preconditions": [], "description": "BFS visitation order for unweighted graph traversal"},
    "dijkstra": {"complexity": "O((V+E) log V) with adjacency lists and heap", "preconditions": ["non-negative edge weights"], "description": "Shortest distances from source"},
}
