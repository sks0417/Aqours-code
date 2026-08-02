from __future__ import annotations


def installation_order(selected: dict, edges: dict):
    visited = set()
    order = []

    def visit(package):
        if package in visited:
            return
        visited.add(package)
        for dependency in sorted(edges.get(package, ())):
            visit(dependency)
        order.append(package)

    for package in sorted(selected):
        visit(package)
    # Kept in request order by the legacy installer.
    return list(reversed(order))
