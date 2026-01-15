# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T18:53:10-05:00

@author: nate
"""
from dataclasses import replace
from typing import Any, Iterable

import tiktoken
from loguru import logger

from .models import RagChunk


def _count_tokens(enc: Any, text: str) -> int:
    return len(enc.encode(text))

def _split_by_lines(
    *,
    enc: Any,
    file_header: str,
    body_lines: list[str],
    file_rel_path,
    start_line: int,
    token_limit: int,
) -> list[RagChunk]:
    """
    Split a multi-line chunk into smaller chunks along line boundaries.
    Partition of the chunk's lines is preserved (no overlap).
    """
    header_tokens = len(enc.encode(file_header + "\n"))
    budget = token_limit - header_tokens
    if budget <= 0:
        # Can't fit even the header; nothing meaningful to do
        budget = 1

    out: list[RagChunk] = []
    i = 0
    n = len(body_lines)

    while i < n:
        # Greedily extend j as far as possible
        j = i
        cur_tokens = 0
        while j < n:
            line_tokens = len(enc.encode(body_lines[j] + "\n"))
            if j == i and line_tokens > budget:
                # Single line too big -> handled by token-split fallback below
                break
            if cur_tokens + line_tokens > budget:
                break
            cur_tokens += line_tokens
            j += 1

        if j == i:
            # One absurd line: token-split it.
            parts = _split_line_by_tokens(
                enc=enc,
                file_header=file_header,
                line_text=body_lines[i],
                token_limit=token_limit,
            )
            for part in parts:
                out.append(
                    RagChunk(
                        file_rel_path=file_rel_path,
                        start_line=start_line + i,
                        end_line=start_line + i + 1,
                        text=f"{file_header}\n{part}",
                        break_depths=[-998],  # sentinel: post-split fragment
                    )
                )
            i += 1
            continue

        # Emit chunk for [i:j)
        chunk_body = "\n".join(body_lines[i:j])
        out.append(
            RagChunk(
                file_rel_path=file_rel_path,
                start_line=start_line + i,
                end_line=start_line + j,
                text=f"{file_header}\n{chunk_body}" if chunk_body else f"{file_header}\n",
                break_depths=[-997],  # sentinel: post-split window
            )
        )
        i = j

    return out


def _split_line_by_tokens(
    *,
    enc: Any,
    file_header: str,
    line_text: str,
    token_limit: int,
) -> list[str]:
    """
    Split a single absurd line into multiple fragments by token slices.
    This is the only place we split within a line.
    """
    header_tokens = len(enc.encode(file_header + "\n"))
    budget = token_limit - header_tokens
    if budget <= 0:
        budget = 1

    toks = enc.encode(line_text)
    out: list[str] = []

    i = 0
    n = len(toks)
    while i < n:
        j = min(n, i + budget)
        out.append(enc.decode(toks[i:j]))
        i = j

    return out


def postprocess_oversize_chunks(
    *,
    chunks: list[RagChunk],
    token_limit: int,
    encoding_name: str = "cl100k_base",
) -> list[RagChunk]:
    """
    Replace any oversize chunks with smaller chunks that fit token_limit.

    Strategy:
    - If chunk spans multiple lines: split on line boundaries (greedy windows).
    - If a single line is still oversize: split within that line via token slices.
    """
    enc = tiktoken.get_encoding(encoding_name)
    logger.debug('postprocessing...')
    out: list[RagChunk] = []
    n_exceeded = 0
    for chunk in chunks:
        if _count_tokens(enc, chunk.text) <= token_limit:
            out.append(chunk)
            continue
        n_exceeded += 1

        lines = chunk.text.splitlines()
        if not lines:
            out.append(chunk)
            continue

        file_header = lines[0]  # must be rel path
        body_lines = lines[1:]

        # If body_lines is empty, there's nothing we can do except keep it.
        if not body_lines:
            out.append(chunk)
            continue

        # Split this chunk's content into subchunks
        out.extend(
            _split_by_lines(
                enc=enc,
                file_header=file_header,
                body_lines=body_lines,
                file_rel_path=chunk.file_rel_path,
                start_line=chunk.start_line,
                token_limit=token_limit,
            )
        )
    logger.debug(f'Broke up {n_exceeded} chunks')
    return out
