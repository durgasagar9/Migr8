"""
Dependency analysis stage.

Turns the flat resource inventory into a directed dependency graph
(edge A -> B means "A depends on B", i.e. B must exist/be migrated first)
and computes a wave-based topological ordering: resources with no
un-migrated dependencies go in wave 1, resources that only depend on
wave-1 resources go in wave 2, and so on. Cycles are detected and
reported rather than silently breaking the plan.
"""
from __future__ import annotations

from .models import Inventory


class CycleError(Exception):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"Dependency cycle detected: {' -> '.join(cycle)}")


class DependencyGraph:
    def __init__(self, inventory: Inventory):
        self.inventory = inventory
        self.edges: dict[str, set[str]] = {
            r.id: set(r.depends_on) for r in inventory.resources
        }
        self._validate_references()

    def _validate_references(self) -> None:
        known = set(self.edges.keys())
        for rid, deps in self.edges.items():
            unknown = deps - known
            if unknown:
                raise ValueError(
                    f"Resource '{rid}' depends on unknown resource(s): {sorted(unknown)}"
                )

    def detect_cycle(self) -> list[str] | None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self.edges}
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            path.append(node)
            for dep in self.edges[node]:
                if color[dep] == GRAY:
                    idx = path.index(dep)
                    return path[idx:] + [dep]
                if color[dep] == WHITE:
                    result = dfs(dep)
                    if result:
                        return result
            path.pop()
            color[node] = BLACK
            return None

        for node in self.edges:
            if color[node] == WHITE:
                cyc = dfs(node)
                if cyc:
                    return cyc
        return None

    def compute_waves(self) -> list[list[str]]:
        """Kahn's algorithm, batched by depth so independent resources
        that unlock at the same time land in the same wave (and can be
        migrated in parallel)."""
        cycle = self.detect_cycle()
        if cycle:
            raise CycleError(cycle)

        remaining = {n: set(deps) for n, deps in self.edges.items()}
        migrated: set[str] = set()
        waves: list[list[str]] = []

        while remaining:
            ready = sorted([n for n, deps in remaining.items() if deps <= migrated])
            if not ready:
                # Shouldn't happen since we already checked for cycles,
                # but guard against pathological input.
                raise CycleError(list(remaining.keys()))
            waves.append(ready)
            migrated.update(ready)
            for n in ready:
                del remaining[n]

        return waves

    def dependents_of(self, resource_id: str) -> list[str]:
        """Resources that would break if `resource_id` disappeared --
        used later to size the blast radius of a rollback."""
        return sorted([n for n, deps in self.edges.items() if resource_id in deps])
