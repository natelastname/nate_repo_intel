# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T18:34:53-05:00

@author: nate
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

from ..py_lsp import EnhancedSymbol, SymbolContainer


@dataclass(frozen=True)
class BreakpointPlan:
    # All candidate breakpoints (sorted unique) in [0..N]
    breakpoints: list[int]

    # line -> depth (lower is preferred). Soft breaks get large depth.
    depth_by_break: dict[int, int]

    # Hard constraints: these must appear in any solution (0/N always included).
    mandatory: set[int]

    # Lines that cannot fit even as a one-line chunk; we force them into their own chunks.
    # Spans are in original line indices: (i, i+1).
    oversize_spans: list[tuple[int, int]]


def _sym_start_line(sym: EnhancedSymbol) -> int:
    rng = sym.location["range"]
    return int(rng["start"]["line"])


def _collect_symbol_break_depths(container: SymbolContainer) -> dict[int, int]:
    out: dict[int, int] = {}

    def walk(node: EnhancedSymbol, depth: int) -> None:
        line = _sym_start_line(node)
        prev = out.get(line)
        if prev is None or depth < prev:
            out[line] = depth
        for child in node.children:
            walk(child, depth + 1)

    for root in container.roots:
        walk(root, 0)

    return out


def _collect_blankline_breaks(lines: list[str]) -> set[int]:
    out: set[int] = set()
    for i, line in enumerate(lines):
        if not line.strip():
            out.add(i + 1)  # break starts after blank line
    return out


def _nearest_before(target: int, candidates: set[int], window: int, lo: int, hi: int) -> int:
    start = max(lo, target - window)
    end = min(hi, target)
    for b in range(end, start - 1, -1):
        if b in candidates:
            return b
    return target


def preprocess_breakpoints(
    *,
    file_rel_path: Path,
    lines: list[str],
    symbols: SymbolContainer,
    token_limit: int,
    encoding_name: str = "cl100k_base",
    snap_window_lines: int = 8,
    enable_blankline_soft_breaks: bool = True,
    soft_break_depth: int = 10**6,
) -> BreakpointPlan:
    """
    Pre-solver pass:
    - Symbol start-line breakpoints with depth.
    - Optional blank-line soft breakpoints (low priority).
    - Snap symbol breakpoints backward to nearby blank-line boundaries.
    - Detect oversize single lines and make their boundaries mandatory.

    Oversize policy here:
      if one line can't fit even alone, we force it to be a standalone chunk (may exceed token_limit).
      This preserves exact partitioning without splitting lines.
    """
    n = len(lines)
    enc = tiktoken.get_encoding(encoding_name)

    path_prefix_tokens = len(enc.encode(str(file_rel_path) + "\n"))
    budget = token_limit - path_prefix_tokens
    if budget <= 0:
        raise ValueError("token_limit is too small to include the required '<relpath>\\n' prefix")

    per_line_tokens: list[int] = []
    for line in lines:
        per_line_tokens.append(len(enc.encode(line + "\n")))

    depth_by_break = _collect_symbol_break_depths(symbols)

    mandatory: set[int] = {0, n}
    oversize_spans: list[tuple[int, int]] = []
    for i, t in enumerate(per_line_tokens):
        if t > budget:
            oversize_spans.append((i, i + 1))
            mandatory.add(i)
            mandatory.add(i + 1)

    soft_breaks: set[int] = set()
    if enable_blankline_soft_breaks:
        soft_breaks = _collect_blankline_breaks(lines)
        soft_breaks = {b for b in soft_breaks if 0 <= b <= n}

    # Ensure mandatory exist in depth map
    for b in mandatory:
        depth_by_break.setdefault(b, -1)

    # Snap symbol breaks within mandatory-separated intervals
    snapped: dict[int, int] = dict(depth_by_break)

    if enable_blankline_soft_breaks and soft_breaks and snap_window_lines > 0:
        snap_candidates = set(soft_breaks) | set(mandatory)
        mand_sorted = sorted(mandatory)

        intervals: list[tuple[int, int]] = []
        for i in range(len(mand_sorted) - 1):
            intervals.append((mand_sorted[i], mand_sorted[i + 1]))

        def interval_for(line: int) -> tuple[int, int]:
            for a, b in intervals:
                if a <= line <= b:
                    return a, b
            return 0, n

        # Only snap non-mandatory, non-boundary symbol breaks
        for line, depth in list(depth_by_break.items()):
            if line in mandatory or line in (0, n):
                continue
            lo, hi = interval_for(line)
            moved = _nearest_before(
                target=line,
                candidates=snap_candidates,
                window=snap_window_lines,
                lo=lo,
                hi=hi,
            )
            if moved != line:
                prev = snapped.get(moved)
                if prev is None or (prev >= 0 and depth < prev):
                    snapped[moved] = depth
                # Remove old if it isn't mandatory (it isn't) and we moved it
                snapped.pop(line, None)

    # Add soft breaks as low-priority candidates
    if enable_blankline_soft_breaks:
        for b in soft_breaks:
            if b in (0, n):
                continue
            snapped.setdefault(b, soft_break_depth)

    bps = sorted({*snapped.keys(), 0, n})
    return BreakpointPlan(
        breakpoints=bps,
        depth_by_break=snapped,
        mandatory=mandatory,
        oversize_spans=oversize_spans,
    )
