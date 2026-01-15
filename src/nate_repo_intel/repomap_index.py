# -*- coding: utf-8 -*-
"""
Created on 2026-01-15T16:43:17-05:00

@author: nate
"""
# -*- coding: utf-8 -*-
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from .git_utils import is_git_ignored
from .lsp_client import LspClient
from .py_lsp import (
    EnhancedSymbol,
    SymbolContainer,
    collect_document_symbols,
    collect_py_files,
    uri_to_path,
)

# -------------------------
# Tree-sitter identifier scan
# -------------------------

def _iter_identifier_positions(text: str) -> list[dict[str, int]]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    PY_LANGUAGE = Language(tspython.language())
    parser = Parser()
    parser.language = PY_LANGUAGE

    tree = parser.parse(text.encode("utf-8"))
    root = tree.root_node

    positions: list[dict[str, int]] = []
    stack = [root]

    while stack:
        node = stack.pop()
        if node.type == "identifier":
            line, col = node.start_point
            positions.append({"line": line, "character": col})
        # for-loops > list comps :)
        for child in node.children:
            stack.append(child)

    return positions


def _first_location_uri(result: Any) -> str | None:
    """
    Normalize LSP definition response into a target URI.
    Handles Location | Location[] | LocationLink[].
    """
    if result is None:
        return None

    loc: Any = None
    if isinstance(result, list):
        if not result:
            return None
        loc = result[0]
    elif isinstance(result, dict):
        loc = result
    else:
        return None

    if not isinstance(loc, dict):
        return None

    # Location: {"uri": "...", "range": {...}}
    # LocationLink: {"targetUri": "...", ...}
    uri = loc.get("uri") or loc.get("targetUri")
    if isinstance(uri, str) and uri:
        return uri
    return None


def _compute_outgoing_edges(
    client: LspClient,
    file_path: Path,
    *,
    root: Path,
) -> Counter[Path]:
    """
    Compute outgoing edges for one file: (ref_file -> def_file) counts.

    Strategy:
      - enumerate identifier positions in ref_file via tree-sitter
      - for each identifier, ask LSP for definition target
      - count definitions by target file
    """
    file_path = file_path.resolve()
    root = root.resolve()

    uri = client.ensure_open(file_path)
    text = file_path.read_text(encoding="utf-8")

    positions = _iter_identifier_positions(text)

    counts: Counter[Path] = Counter()
    for pos in positions:
        result = client.request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": pos},
        )
        target_uri = _first_location_uri(result)
        if not target_uri:
            continue

        def_path = uri_to_path(target_uri).resolve()
        try:
            def_rel = def_path.relative_to(root)
            ref_rel = file_path.relative_to(root)
        except ValueError:
            continue

        if def_rel == ref_rel:
            continue

        counts[def_rel] += 1

    return counts


# -------------------------
# RepoMapIndex
# -------------------------

@dataclass
class RepoMapIndex:
    """
    Incremental repo index:
      - file_symbols: Path(abs) -> SymbolContainer
      - edges_from:  ref_rel -> Counter(def_rel -> count)
      - rev_deps:    def_rel -> set(ref_rel)

    All graph keys are stored as *relative-to-root* Paths for stability.
    """
    root: Path
    client: LspClient

    py_files: list[Path] = field(default_factory=list)  # absolute Paths
    file_symbols: dict[Path, SymbolContainer] = field(default_factory=dict)  # abs -> symbols

    edges_from: dict[Path, Counter[Path]] = field(default_factory=dict)  # rel -> Counter(rel->count)
    rev_deps: dict[Path, set[Path]] = field(default_factory=lambda: defaultdict(set))  # rel(def) -> set(rel(ref))

    def build(self) -> None:
        """Initial full build. After this, call update_file* for incremental updates."""
        logger.debug('Building index...')
        self.root = self.root.resolve()

        self.py_files = collect_py_files(self.root)
        logger.debug(f"RepoMapIndex.build(): {len(self.py_files)} python files")

        for path in self.py_files:
            self._index_one_file(path)

    def update_file(self, path: Path) -> None:
        """
        Update symbols + outgoing edges for a single file.
        Safe to call on create/modify events.
        """
        path = path.resolve()
        if path.suffix != ".py":
            return

        if not self._should_track(path):
            return

        if path not in self.file_symbols:
            self.py_files.append(path)

        self.client.update_document(path)
        self._index_one_file(path)

    def update_file_and_dependents(self, path: Path, *, max_hops: int = 1) -> None:
        """
        Update a file, then update its dependents (reverse deps) up to N hops.
        This handles the common reality that changes to defs can change resolution in callers.
        """
        path = path.resolve()
        if path.suffix != ".py":
            return

        if not self._should_track(path):
            return

        root = self.root.resolve()
        start_rel = self._to_rel(path)

        to_update: set[Path] = set()
        to_update.add(path)

        frontier: set[Path] = set()
        frontier.add(start_rel)

        hops = 0
        while hops < max_hops:
            next_frontier: set[Path] = set()

            for def_rel in frontier:
                for ref_rel in self.rev_deps.get(def_rel, set()):
                    to_update.add(root / ref_rel)
                    next_frontier.add(ref_rel)

            frontier = next_frontier
            hops += 1

        for f in to_update:
            self.update_file(f)

    def remove_file(self, path: Path) -> None:
        """
        Handle delete events:
          - remove from py_files + file_symbols
          - remove outgoing edges + reverse deps entries
          - close LSP doc if open
        """
        path = path.resolve()
        if path.suffix != ".py":
            return

        if path in self.file_symbols:
            self.file_symbols.pop(path, None)

        self.py_files = [p for p in self.py_files if p != path]

        rel = self._to_rel(path)

        # Remove outgoing edges for this file
        old_edges = self.edges_from.pop(rel, Counter())

        # Remove reverse links created by its outgoing edges
        for def_rel in old_edges:
            s = self.rev_deps.get(def_rel)
            if s is not None:
                s.discard(rel)
                if not s:
                    self.rev_deps.pop(def_rel, None)

        # Remove any reverse-deps *into* this file (i.e., callers -> this file)
        inbound = self.rev_deps.pop(rel, None)
        if inbound is not None:
            for ref_rel in inbound:
                counter = self.edges_from.get(ref_rel)
                if counter is None:
                    continue
                if rel in counter:
                    del counter[rel]
                if not counter:
                    self.edges_from.pop(ref_rel, None)

        try:
            self.client.close_document(path)
        except Exception:
            pass

    # -------------------------
    # Rendering (optional)
    # -------------------------

    def render_repomap_text(self) -> str:
        """Render symbol trees like your current output (uses cached symbols)."""
        root = self.root.resolve()
        lines: list[str] = []

        for path in self.py_files:
            if path not in self.file_symbols:
                continue

            rel_path = path.resolve().relative_to(root)
            container = self.file_symbols.get(path, SymbolContainer(roots=[]))

            lines.append(f"{rel_path}:")

            if not container.roots:
                lines.append("  (no symbols found)")
                lines.append("")
                continue

            for sym in container.roots:
                self._render_symbol_tree(sym, level=0, out=lines)

            lines.append("")

        return "\n".join(lines)

    def render_deps_text(self, *, top_k: int = 8) -> str:
        """
        Render a quick dependency view:
          file.py -> (top referenced files by count)
        """
        lines: list[str] = []
        for ref_rel, counter in sorted(self.edges_from.items()):
            lines.append(f"{ref_rel}:")
            if not counter:
                lines.append("  (no outgoing deps)")
                lines.append("")
                continue

            for def_rel, n in counter.most_common(top_k):
                lines.append(f"  {n:4d} -> {def_rel}")
            lines.append("")

        return "\n".join(lines)

    # -------------------------
    # Internals
    # -------------------------

    def _should_track(self, path: Path) -> bool:
        root = self.root.resolve()
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:
            return False
        if is_git_ignored(root, rel):
            return False
        return True

    def _to_rel(self, path: Path) -> Path:
        return path.resolve().relative_to(self.root.resolve())

    def _index_one_file(self, path: Path) -> None:
        """
        Recompute symbols + outgoing edges for a file and update reverse deps consistently.
        """
        root = self.root.resolve()
        path = path.resolve()

        if not self._should_track(path):
            return

        self.client.ensure_open(path)

        # Symbols
        symbols = collect_document_symbols(self.client, path)
        self.file_symbols[path] = symbols

        # Outgoing edges
        new_edges = _compute_outgoing_edges(self.client, path, root=root)
        self._set_edges_for_file(path, new_edges)

    def _set_edges_for_file(self, ref_file_abs: Path, new_edges: Counter[Path]) -> None:
        """
        Replace outgoing edges for ref_file, maintaining rev_deps.
        Keys are stored as relative-to-root paths.
        """
        ref_rel = self._to_rel(ref_file_abs)

        old_edges = self.edges_from.get(ref_rel, Counter())

        # Remove old reverse links
        for def_rel in old_edges:
            s = self.rev_deps.get(def_rel)
            if s is not None:
                s.discard(ref_rel)
                if not s:
                    self.rev_deps.pop(def_rel, None)

        # Install new outgoing edges
        self.edges_from[ref_rel] = new_edges

        # Add new reverse links
        for def_rel in new_edges:
            self.rev_deps[def_rel].add(ref_rel)

    def _render_symbol_tree(self, sym: EnhancedSymbol, *, level: int, out: list[str]) -> None:
        indent = "  " * (1 + level)
        out.append(f"{indent}{sym.kind_label} {sym.dotpath}")
        for child in sym.children:
            self._render_symbol_tree(child, level=level + 1, out=out)
