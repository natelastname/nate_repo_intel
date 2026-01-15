# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T18:35:55-05:00

@author: nate
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import tiktoken
from loguru import logger

from ..py_lsp import RepoMapAnalysis, SymbolContainer
from .models import RagChunk
from .postprocess import postprocess_oversize_chunks
from .preprocess import BreakpointPlan, preprocess_breakpoints
from .solver import SolveResult, TokenIndex, solve_fewest_chunks, window_partition


def _build_token_index(lines: list[str], file_rel_path: Path, *, encoding_name: str) -> TokenIndex:
    enc = tiktoken.get_encoding(encoding_name)
    prefix: list[int] = [0]
    running = 0
    for line in lines:
        running += len(enc.encode(line + "\n"))
        prefix.append(running)
    path_prefix_tokens = len(enc.encode(str(file_rel_path) + "\n"))
    return TokenIndex(enc=enc, line_tokens_prefix=prefix, path_prefix_tokens=path_prefix_tokens)


def _chunk_text(rel_path: Path, lines: list[str], start: int, end: int) -> str:
    body = "\n".join(lines[start:end])
    if body:
        return f"{rel_path}\n{body}"
    return f"{rel_path}\n"


def _solve_interval(
    *,
    token_index: TokenIndex,
    plan: BreakpointPlan,
    start: int,
    end: int,
    token_limit: int,
    max_symbol_depth: int,
) -> Optional[SolveResult]:
    """
    Solve partition on [start:end] using plan breakpoints, respecting depth preference.
    """
    # Candidate breakpoints constrained to interval
    interval_bps_all = [b for b in plan.breakpoints if start <= b <= end]
    if interval_bps_all[0] != start:
        interval_bps_all.insert(0, start)
    if interval_bps_all[-1] != end:
        interval_bps_all.append(end)

    interval_bps_all = sorted(set(interval_bps_all))

    # Iterative deepening on depth
    for d in range(0, max_symbol_depth + 1):
        allowed: list[int] = []
        for b in interval_bps_all:
            if b in (start, end):
                allowed.append(b)
                continue
            if b in plan.mandatory:
                allowed.append(b)
                continue
            depth = plan.depth_by_break.get(b, 10**9)
            if depth != -1 and depth <= d:
                allowed.append(b)

        allowed = sorted(set(allowed))
        res = solve_fewest_chunks(
            token_index=token_index,
            bps=allowed,
            depth_of_line=plan.depth_by_break,
            token_limit=token_limit,
        )
        if res is not None:
            return res

    # If that failed, allow *all* breakpoints in the interval (including soft)
    res = solve_fewest_chunks(
        token_index=token_index,
        bps=interval_bps_all,
        depth_of_line=plan.depth_by_break,
        token_limit=token_limit,
    )
    return res


def chunk_python_file_partitioned(
    *,
    root: Path,
    file_path: Path,
    symbols: SymbolContainer,
    token_limit: int,
    encoding_name: str = "cl100k_base",
    max_symbol_depth: int = 10,
) -> list[RagChunk]:
    root = root.resolve()
    file_path = file_path.resolve()
    rel_path = file_path.relative_to(root)

    raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
    lines = raw_text.splitlines()
    n = len(lines)

    token_index = _build_token_index(lines, rel_path, encoding_name=encoding_name)

    # Ideal: whole file fits
    if token_index.segment_tokens(0, n) <= token_limit:
        return [
            RagChunk(
                file_rel_path=rel_path,
                start_line=0,
                end_line=n,
                text=_chunk_text(rel_path, lines, 0, n),
                break_depths=[],
            )
        ]

    plan = preprocess_breakpoints(
        file_rel_path=rel_path,
        lines=lines,
        symbols=symbols,
        token_limit=token_limit,
        encoding_name=encoding_name,
        snap_window_lines=8,
        enable_blankline_soft_breaks=True,
    )

    oversize_set = set(plan.oversize_spans)

    # Process mandatory-separated intervals; oversize spans are forced chunks.
    mand_sorted = sorted(plan.mandatory)
    chunks: list[RagChunk] = []

    for i in range(len(mand_sorted) - 1):
        a = mand_sorted[i]
        b = mand_sorted[i + 1]
        if a == b:
            continue

        if (a, b) in oversize_set:
            # Forced oversize chunk: may exceed token_limit by design.
            chunks.append(
                RagChunk(
                    file_rel_path=rel_path,
                    start_line=a,
                    end_line=b,
                    text=_chunk_text(rel_path, lines, a, b),
                    break_depths=[-999],  # sentinel meaning "forced oversize"
                )
            )
            continue

        # Normal interval solve
        res = _solve_interval(
            token_index=token_index,
            plan=plan,
            start=a,
            end=b,
            token_limit=token_limit,
            max_symbol_depth=max_symbol_depth,
        )

        if res is None:
            logger.warning(f"Falling back to window partition for {rel_path} [{a},{b})")
            breaks = window_partition(token_index=token_index, start=a, end=b, token_limit=token_limit)
            # window_partition returns [a, ..., b]
            for k in range(len(breaks) - 1):
                s = breaks[k]
                e = breaks[k + 1]
                chunks.append(
                    RagChunk(
                        file_rel_path=rel_path,
                        start_line=s,
                        end_line=e,
                        text=_chunk_text(rel_path, lines, s, e),
                        break_depths=[],
                    )
                )
            continue

        # Emit chunks for solution breaks
        for k in range(len(res.breaks) - 1):
            s = res.breaks[k]
            e = res.breaks[k + 1]
            # Depths used inside the interval; for debug, keep the start depth if present
            start_depth = plan.depth_by_break.get(s, -1)
            chunks.append(
                RagChunk(
                    file_rel_path=rel_path,
                    start_line=s,
                    end_line=e,
                    text=_chunk_text(rel_path, lines, s, e),
                    break_depths=[start_depth] if s not in (0, n) else [],
                )
            )

    # Sanity: ensure partition covers [0,n) in order
    # (optional in production, useful while iterating)
    if chunks:
        if chunks[0].start_line != 0 or chunks[-1].end_line != n:
            logger.warning(f"Partition sanity check failed for {rel_path}")

    chunks = postprocess_oversize_chunks(
        chunks=chunks,
        token_limit=token_limit,
        encoding_name=encoding_name,
    )

    return chunks

def chunk_repomap_analysis(
    analysis: RepoMapAnalysis,
    *,
    token_limit: int = 1000,
    encoding_name: str = "cl100k_base",
) -> dict[Path, list[RagChunk]]:
    out: dict[Path, list[RagChunk]] = {}
    root = analysis.root

    for file_path in analysis.py_files:
        container = analysis.file_symbols.get(file_path, SymbolContainer(roots=[]))
        chunks = chunk_python_file_partitioned(
            root=root,
            file_path=file_path,
            symbols=container,
            token_limit=token_limit,
            encoding_name=encoding_name,
        )
        out[file_path.resolve().relative_to(root)] = chunks

    return out
