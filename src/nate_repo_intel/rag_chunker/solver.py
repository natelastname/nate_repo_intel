# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T18:35:25-05:00

@author: nate
"""

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class TokenIndex:
    enc: Any
    line_tokens_prefix: list[int]
    path_prefix_tokens: int

    def segment_tokens(self, start: int, end: int) -> int:
        return self.path_prefix_tokens + (self.line_tokens_prefix[end] - self.line_tokens_prefix[start])


@dataclass(frozen=True)
class SolveResult:
    breaks: list[int]       # includes segment start and end
    break_depths: list[int] # depth for each break line (start/end typically -1)


def reconstruct_path(prev: list[int], end_idx: int, bps: list[int]) -> list[int]:
    path_indices: list[int] = []
    cur = end_idx
    while cur != -1:
        path_indices.append(cur)
        cur = prev[cur]
    path_indices.reverse()
    return [bps[i] for i in path_indices]


def solve_fewest_chunks(
    *,
    token_index: TokenIndex,
    bps: list[int],
    depth_of_line: dict[int, int],
    token_limit: int,
    max_back_steps: int = 32,
) -> Optional[SolveResult]:
    """
    Shortest-path in a forward DAG induced by 'fits' (token limit).
    Avoids O(M^2) edge construction via two-pointer furthest reach + bounded back-steps.
    """
    m = len(bps)
    if m < 2:
        return None

    dist = [-1] * m
    prev = [-1] * m
    dist[0] = 0

    q: deque[int] = deque([0])

    # Two-pointer furthest reach
    furthest: list[int] = [0] * m
    j = 0
    for i in range(m):
        if j < i:
            j = i
        while j + 1 < m and token_index.segment_tokens(bps[i], bps[j + 1]) <= token_limit:
            j += 1
        furthest[i] = j

    def neighbors(i: int) -> list[int]:
        j_max = furthest[i]
        if j_max <= i:
            return []

        cands: list[int] = [j_max]
        step = 1
        while step <= max_back_steps:
            j2 = j_max - step
            if j2 <= i:
                break
            cands.append(j2)
            step += 1

        # Dedup and sort by preference: longer jump, then shallower breakpoint
        out = sorted(
            set(cands),
            key=lambda j_idx: (
                -(bps[j_idx]),
                depth_of_line.get(bps[j_idx], 10**9),
            ),
        )
        return out

    while q:
        i = q.popleft()
        if i == m - 1:
            break

        for j_idx in neighbors(i):
            if dist[j_idx] != -1:
                continue
            dist[j_idx] = dist[i] + 1
            prev[j_idx] = i
            q.append(j_idx)

    if dist[m - 1] == -1:
        return None

    breaks = reconstruct_path(prev, m - 1, bps)
    break_depths: list[int] = []
    for line in breaks:
        if line == bps[0] or line == bps[-1]:
            break_depths.append(-1)
        else:
            break_depths.append(depth_of_line.get(line, 10**9))

    return SolveResult(breaks=breaks, break_depths=break_depths)


def window_partition(
    *,
    token_index: TokenIndex,
    start: int,
    end: int,
    token_limit: int,
) -> list[int]:
    """
    Always partitions [start:end] into windows of lines (no overlap).
    Assumes every single line fits within limit (oversize lines handled elsewhere).
    """
    breaks: list[int] = [start]
    i = start
    while i < end:
        j = i
        while j < end and token_index.segment_tokens(i, j + 1) <= token_limit:
            j += 1
        if j == i:
            j = min(end, i + 1)
        breaks.append(j)
        i = j
    return breaks
