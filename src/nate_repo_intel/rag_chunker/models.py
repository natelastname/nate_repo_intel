# -*- coding: utf-8 -*-
"""
Created on 2026-01-14T18:57:05-05:00

@author: nate
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RagChunk:
    file_rel_path: Path
    start_line: int
    end_line: int
    text: str
    break_depths: list[int]

    def summary(self, *, head: int = 5, tail: int = 5) -> str:
        lines = self.text.splitlines()
        header = lines[0] if lines else str(self.file_rel_path)
        body = lines[1:]

        total = len(body)
        start_ln = self.start_line + 1

        out: list[str] = []
        first_line = f"=== {header} ==="
        out.append(first_line)

        if total == 0:
            out.append("(empty chunk)")
            return "\n".join(out)

        show_all = total <= head + tail + 1

        def fmt(i: int, line: str) -> str:
            return f"{start_ln + i:>6} | {line}"

        if show_all:
            for i, line in enumerate(body):
                out.append(fmt(i, line))
            return "\n".join(out)

        for i in range(min(head, total)):
            out.append(fmt(i, body[i]))

        out.append("   ⋮")

        tail_start = total - tail
        for i in range(tail_start, total):
            out.append(fmt(i, body[i]))

        out.append("=" * len(first_line))
        return "\n".join(out)
