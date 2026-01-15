# -*- coding: utf-8 -*-
"""
Created on 2026-01-07T17:02:22-05:00

@author: nate
"""
import json
from pathlib import Path

import argh
from loguru import logger

from .git_utils import build_repo_tree, format_repo_tree, git_root_for
from .py_lsp import render_repomap_text, run_repomap_analysis
from .py_utils import get_entrypoints_from_pyproject


def repo_tree(in_path: Path, fmt_json: bool = False):
    in_path = git_root_for(in_path)
    tree = build_repo_tree(in_path)
    if fmt_json:
        print(json.dumps(tree, indent=2))
        return

    out = format_repo_tree(tree)
    print(out)
    return

def py_entrypoints(in_path: Path):
    in_path = git_root_for(in_path)
    eps = get_entrypoints_from_pyproject(in_path)
    for info in eps:
        blob = info.model_dump_json(indent=2)
        print(blob)
    return

def get_lsp_symbols(root_path: Path):
    logger.info(__name__)
    root = Path(root_path).resolve()
    if not root.exists():
        print(f"Path does not exist: {root}", file=sys.stderr)
        raise SystemExit(1)

    in_path = git_root_for(root)
    rma = run_repomap_analysis(in_path)

    import tiktoken

    from .rag_chunker import chunk_python_file_partitioned

    for file_path in rma.py_files:
        if file_path.name != "chunker.py":
            continue
        container = rma.file_symbols[file_path]
        chunks = chunk_python_file_partitioned(
            root=root,
            file_path=file_path,
            symbols=container,
            token_limit=250,
            encoding_name=tiktoken.encoding_name_for_model('gpt-4o'),
        )
        for chunk in chunks:
            print(chunk.summary())

        breakpoint()

    #res = chunk_repomap_analysis(analysis, token_limit=250)

    #text = render_repomap_text(rma)
    #print(text)
    return


def cli():
    parser = argh.ArghParser()
    parser.add_commands([
        repo_tree,
        py_entrypoints,
        get_lsp_symbols
    ])
    parser.dispatch()

    # Only one entrypoint
    #argh.dispatch_command(main)
