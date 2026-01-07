# -*- coding: utf-8 -*-
"""
Created on 2026-01-07T17:04:19-05:00

@author: nate
"""
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import networkx as nx
from glom import Coalesce, glom
from pydantic import BaseModel, ConfigDict


class EntrypointInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    script_name: str
    target: str          # e.g. "pkg.mod:main"
    module: str          # e.g. "pkg.mod"
    rel_path: Path       # e.g. Path("pkg/mod.py")

def _load_pyproject_data(root: Path) -> Optional[dict]:
    """
    Return parsed pyproject.toml dict, or None if missing/unreadable.
    """
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:
        # If it's present but malformed, you can either raise or treat as absent.
        # For 'print_md' robustness, treat as absent.
        return None


def load_pyproject_scripts(root: Path) -> dict[str, str]:
    """
    Return scripts mapping {name: "module:callable"} from pyproject.toml.
    If pyproject.toml is missing, return {}.
    """
    data = _load_pyproject_data(root)
    if not data:
        return {}

    scripts: dict[str, str] = glom(
        data,
        Coalesce("project.scripts", "tool.poetry.scripts", default={}),
    )
    if scripts is None:
        return {}

    if not isinstance(scripts, dict):
        return {}

    out: dict[str, str] = {}
    for k, v in scripts.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def _import_bases_from_pyproject(root: Path) -> list[Path]:
    """
    Return candidate import-base directories (absolute Paths) for module resolution.

    If pyproject.toml is missing, fall back to:
      - ./src if it exists
      - repo root
    """
    bases: list[Path] = []
    root = root.resolve()

    data = _load_pyproject_data(root)  # may be None

    # 1) Poetry packages with `from` (e.g. from="src")
    if data:
        packages = glom(data, Coalesce("tool.poetry.packages", default=[]))
        if isinstance(packages, list):
            for pkg in packages:
                if not isinstance(pkg, dict):
                    continue
                from_dir = pkg.get("from")
                if isinstance(from_dir, str) and from_dir.strip():
                    base = (root / from_dir.strip()).resolve()
                    if base.exists() and base.is_dir() and base not in bases:
                        bases.append(base)

    # 2) If ./src exists, include it
    src_dir = (root / "src").resolve()
    if src_dir.exists() and src_dir.is_dir() and src_dir not in bases:
        bases.append(src_dir)

    # 3) Always include repo root as fallback
    if root not in bases:
        bases.append(root)

    return bases


def _module_to_relpath(root: Path, module: str) -> Optional[Path]:
    """
    Resolve 'pkg.mod' -> (base)/pkg/mod.py or (base)/pkg/mod/__init__.py.

    Returns a path *relative to repo root* (so it matches your graph node keys),
    or None if not found.
    """
    root = root.resolve()
    parts = [p for p in module.split(".") if p]
    rel_mod = Path(*parts)

    for base in _import_bases_from_pyproject(root):
        cand1 = base / (rel_mod.as_posix() + ".py")
        if cand1.exists():
            return cand1.resolve().relative_to(root)

        cand2 = base / rel_mod / "__init__.py"
        if cand2.exists():
            return cand2.resolve().relative_to(root)

    return None


def get_entrypoints_from_pyproject(root: Path) -> list[EntrypointInfo]:
    """
    Parse scripts from pyproject.toml and resolve to repo-relative file paths.
    """
    scripts = load_pyproject_scripts(root)

    infos: list[EntrypointInfo] = []
    for name, target in scripts.items():
        # Use glom to normalize/clean the target string (trim whitespace)
        cleaned_target: str = glom({"t": target}, "t", default="").strip()

        module = cleaned_target.split(":", 1)[0].strip()
        rel_path = _module_to_relpath(root, module)
        if rel_path is None:
            continue

        infos.append(
            EntrypointInfo(
                script_name=name,
                target=cleaned_target,
                module=module,
                rel_path=rel_path,
            )
        )

    # stable order: by script name
    infos.sort(key=lambda x: x.script_name)
    return infos


def _weighted_in_from_set(G: nx.DiGraph, node: Path, allowed_sources: set[Path]) -> int:
    w = 0
    for u, _, data in G.in_edges(node, data=True):
        if u in allowed_sources:
            w += int(data.get("weight", 1))
    return w


def _multi_source_bfs_dist(G: nx.DiGraph, sources: list[Path]) -> dict[Path, int]:
    dist: dict[Path, int] = {}
    q: deque[Path] = deque()

    for s in sources:
        if s in G and s not in dist:
            dist[s] = 0
            q.append(s)

    while q:
        u = q.popleft()
        du = dist[u]

        for v in G.successors(u):
            if v in dist:
                continue
            dist[v] = du + 1
            q.append(v)

    return dist

def order_nodes_from_entrypoints(
    G: nx.DiGraph,
    entrypoints: list[Path],
) -> list[Path]:
    """
    Deterministic importance order:

      1) entrypoint(s) first
      2) reachable nodes next, by BFS distance from entrypoints (0,1,2,...)
         - within each layer: higher weighted fan-in from reachable nodes first
      3) unreachable nodes last, grouped by weakly connected component
         - each component ordered by BFS from its "local hub" (max fan-in inside component)

    Assumes G nodes are Paths (relative-to-root).
    """
    # Ensure only entrypoints that exist in graph
    eps: list[Path] = []
    for ep in entrypoints:
        if ep in G:
            eps.append(ep)

    # Reachability + distance from entrypoints
    dist = _multi_source_bfs_dist(G, eps)
    reachable = set(dist.keys())

    ordered: list[Path] = []

    # 1) Entrypoints first (guaranteed)
    for ep in eps:
        if ep not in ordered:
            ordered.append(ep)

    # 2) Reachable nodes by layer (excluding already-added entrypoints)
    max_d = 0
    for d in dist.values():
        if d > max_d:
            max_d = d

    for d in range(0, max_d + 1):
        layer: list[Path] = []
        for n, nd in dist.items():
            if nd != d:
                continue
            if n in ordered:
                continue
            layer.append(n)

        # rank within layer by fan-in from reachable
        layer.sort(
            key=lambda n: (-_weighted_in_from_set(G, n, reachable), str(n)),
        )
        for n in layer:
            ordered.append(n)

    # 3) Unreachable nodes grouped by weakly connected components
    unreachable: list[Path] = []
    for n in G.nodes():
        if n not in reachable:
            unreachable.append(n)

    if not unreachable:
        return ordered

    U = G.subgraph(unreachable).copy()
    components = list(nx.weakly_connected_components(U))

    # Sort components by their "hub" score so the biggest/most-referenced modules come first
    comp_infos: list[tuple[int, str, set[Path]]] = []
    for comp in components:
        comp_set = set(comp)

        # hub = node with max in-weight inside component
        hub = None
        hub_score = -1
        for n in comp_set:
            s = _weighted_in_from_set(U, n, comp_set)
            if s > hub_score:
                hub_score = s
                hub = n

        hub_name = str(hub) if hub is not None else ""
        comp_infos.append((hub_score, hub_name, comp_set))

    comp_infos.sort(key=lambda x: (-x[0], x[1]))

    for _, _, comp_set in comp_infos:
        sub = U.subgraph(comp_set).copy()

        # choose hub again within sub (stable)
        hub = None
        hub_score = -1
        for n in comp_set:
            s = _weighted_in_from_set(sub, n, comp_set)
            if s > hub_score:
                hub_score = s
                hub = n

        if hub is None:
            # degenerate, just append sorted
            rest = list(comp_set)
            rest.sort(key=lambda n: str(n))
            for n in rest:
                if n not in ordered:
                    ordered.append(n)
            continue

        # BFS from hub within component
        comp_dist = _multi_source_bfs_dist(sub, [hub])

        max_cd = 0
        for d in comp_dist.values():
            if d > max_cd:
                max_cd = d

        for d in range(0, max_cd + 1):
            layer: list[Path] = []
            for n, nd in comp_dist.items():
                if nd != d:
                    continue
                if n in ordered:
                    continue
                layer.append(n)

            layer.sort(
                key=lambda n: (-_weighted_in_from_set(sub, n, comp_set), str(n)),
            )
            for n in layer:
                ordered.append(n)

        # any leftover nodes (if subgraph BFS missed due to directionality)
        leftovers: list[Path] = []
        for n in comp_set:
            if n not in ordered:
                leftovers.append(n)
        leftovers.sort(key=lambda n: str(n))
        for n in leftovers:
            ordered.append(n)

    return ordered

def order_from_pyproject_entrypoints(
    root: Path,
    G: nx.DiGraph,
) -> tuple[list[EntrypointInfo], list[Path]]:
    """
    Convenience: extract entrypoints from pyproject.toml and return (entrypoints, ordered_nodes).
    """
    eps = get_entrypoints_from_pyproject(root)
    ep_paths: list[Path] = []
    for ep in eps:
        ep_paths.append(ep.rel_path)

    ordered = order_nodes_from_entrypoints(G, ep_paths)
    return eps, ordered
