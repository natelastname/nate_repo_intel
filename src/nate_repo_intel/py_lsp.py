# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T16:00:52-05:00

@author: nate
"""
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

from loguru import logger

from .git_utils import is_git_ignored
from .lsp_client import KIND_LABELS, LspClient

# ---------------- Models ----------------

@dataclass(frozen=True)
class EnhancedSymbol:
    name: str
    kind: int | None
    kind_label: str
    dotpath: str
    location: dict[str, Any]
    children: list["EnhancedSymbol"]


@dataclass(frozen=True)
class SymbolContainer:
    """Tree of symbols for a single document."""

    roots: list[EnhancedSymbol]

    def walk(self) -> Iterable[EnhancedSymbol]:
        """Depth-first traversal over all symbols (roots + descendants)."""
        stack: list[EnhancedSymbol] = []
        for sym in reversed(self.roots):
            stack.append(sym)

        while stack:
            cur = stack.pop()
            yield cur
            for child in reversed(cur.children):
                stack.append(child)


@dataclass(frozen=True)
class RepoMapAnalysis:
    root: Path
    py_files: list[Path]
    file_symbols: dict[Path, SymbolContainer]
    edges: dict[Path, dict[Path, int]]


# ---------------- Repo map logic ----------------

def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


def find_identifier_pos(sym: EnhancedSymbol) -> dict[str, int]:
    """Return a position dict {line, character} on the actual identifier."""
    loc = sym.location
    rng = loc["range"]
    line_no = rng["start"]["line"]

    file_path = uri_to_path(loc["uri"])
    text = file_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    line = lines[line_no]
    idx = line.find(sym.name)
    if idx == -1:
        idx = rng["start"]["character"]

    return {"line": line_no, "character": idx}


def collect_py_files(root: Path) -> list[Path]:
    py_files: list[Path] = []
    for path in root.rglob("*.py"):
        ref_rel = path.relative_to(root)
        if is_git_ignored(root, ref_rel):
            continue
        py_files.append(path)
    return py_files


def _kind_label(kind: int | None) -> str:
    if kind is None:
        return "kind=?"
    return KIND_LABELS.get(kind, f"kind={kind}")


def _enhance_document_symbol_node(
    item: dict[str, Any],
    *,
    parents: list[str],
    file_uri: str,
) -> EnhancedSymbol:
    """
    Build a single EnhancedSymbol (with children) from one DocumentSymbol node.
    """
    name = item.get("name") or "<unknown>"
    kind = item.get("kind")
    dotpath = ".".join([*parents, name]) if parents else name

    selection_range = item.get("selectionRange") or item.get("range")
    if selection_range is None:
        # Defensive: should not happen with pyright, but avoid crashing the whole file.
        selection_range = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}

    loc = {"uri": file_uri, "range": selection_range}

    children_items = item.get("children")
    children: list[EnhancedSymbol] = []
    if isinstance(children_items, list) and children_items:
        for child in children_items:
            if not isinstance(child, dict):
                continue
            children.append(
                _enhance_document_symbol_node(
                    child,
                    parents=[*parents, name],
                    file_uri=file_uri,
                )
            )

    return EnhancedSymbol(
        name=name,
        kind=kind,
        kind_label=_kind_label(kind),
        dotpath=dotpath,
        location=loc,
        children=children,
    )


def _enhance_from_document_symbol_tree(
    items: list[dict[str, Any]],
    *,
    file_uri: str,
) -> SymbolContainer:
    roots: list[EnhancedSymbol] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        roots.append(_enhance_document_symbol_node(item, parents=[], file_uri=file_uri))
    return SymbolContainer(roots=roots)


def _enhance_from_symbol_information_list(
    items: list[dict[str, Any]],
) -> SymbolContainer:
    """
    SymbolInformation is flat. We can’t reliably rebuild hierarchy,
    so we store them as roots with empty children.
    """
    roots: list[EnhancedSymbol] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        name = item.get("name") or "<unknown>"
        kind = item.get("kind")
        container = item.get("containerName")
        dotpath = f"{container}.{name}" if container else name
        loc = item.get("location")
        if not isinstance(loc, dict):
            continue

        roots.append(
            EnhancedSymbol(
                name=name,
                kind=kind,
                kind_label=_kind_label(kind),
                dotpath=dotpath,
                location=loc,
                children=[],
            )
        )
    return SymbolContainer(roots=roots)


def collect_document_symbols(client: LspClient, file_path: Path) -> SymbolContainer:
    uri = client.open_document(file_path)
    result = client.request(
        "textDocument/documentSymbol",
        {"textDocument": {"uri": uri}},
    )

    if not isinstance(result, list) or not result:
        return SymbolContainer(roots=[])

    first = result[0] if isinstance(result[0], dict) else {}
    if "selectionRange" in first or "children" in first or "range" in first:
        # DocumentSymbol[]
        return _enhance_from_document_symbol_tree(result, file_uri=uri)

    if "location" in first:
        # SymbolInformation[]
        return _enhance_from_symbol_information_list(result)

    return SymbolContainer(roots=[])


def collect_reference_edges(
    client: LspClient,
    root: Path,
    file_symbols: dict[Path, SymbolContainer],
) -> dict[Path, dict[Path, int]]:
    edges: dict[Path, dict[Path, int]] = {}
    root = root.resolve()

    for def_path, container in file_symbols.items():
        def_uri = client.open_document(def_path)

        for sym in container.walk():
            refs = client.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": def_uri},
                    "position": find_identifier_pos(sym),
                    "context": {"includeDeclaration": False},
                },
            )
            if not refs:
                continue

            for loc in refs:
                ref_path = uri_to_path(loc["uri"]).resolve()
                try:
                    ref_rel = ref_path.relative_to(root)
                    def_rel = def_path.relative_to(root)
                except ValueError:
                    continue

                if ref_rel == def_rel:
                    continue

                if ref_rel not in edges:
                    edges[ref_rel] = {}
                edges[ref_rel][def_rel] = edges[ref_rel].get(def_rel, 0) + 1

    return edges


def run_repomap_analysis(
    root: Path,
    lsp_cmd: Optional[list[str]] = None,
) -> RepoMapAnalysis:
    if lsp_cmd is None:
        lsp_cmd = ["pyright-langserver", "--stdio"]

    root = root.resolve()
    client = LspClient(lsp_cmd, root)

    try:
        py_files = collect_py_files(root)

        logger.info("1) Collect symbols for all files")
        file_symbols: dict[Path, SymbolContainer] = {}
        for path in py_files:
            file_symbols[path] = collect_document_symbols(client, path)

        logger.info("2) Build reference graph")
        edges = collect_reference_edges(client, root, file_symbols)

        return RepoMapAnalysis(
            root=root,
            py_files=py_files,
            file_symbols=file_symbols,
            edges=edges,
        )
    finally:
        client.close()


def _render_symbol_tree(sym: EnhancedSymbol, *, level: int, out: list[str]) -> None:
    indent = "  " * (1 + level)
    out.append(f"{indent}{sym.kind_label} {sym.dotpath}")
    for child in sym.children:
        _render_symbol_tree(child, level=level + 1, out=out)


def render_repomap_text(analysis: RepoMapAnalysis) -> str:
    root = analysis.root
    lines: list[str] = []

    logger.info("3) Render repomap text")
    for path in analysis.py_files:
        rel_path = path.resolve().relative_to(root)
        container = analysis.file_symbols.get(path, SymbolContainer(roots=[]))

        lines.append(f"{rel_path}:")

        if not container.roots:
            lines.append("  (no symbols found)")
            lines.append("")
            continue

        for sym in container.roots:
            _render_symbol_tree(sym, level=0, out=lines)

        lines.append("")

    return "\n".join(lines)


def build_repomap_text(
    root: Path,
    lsp_cmd: Optional[list[str]] = None,
) -> str:
    analysis = run_repomap_analysis(root, lsp_cmd=lsp_cmd)
    return render_repomap_text(analysis)
