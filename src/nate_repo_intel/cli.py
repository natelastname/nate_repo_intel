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
from .py_utils import get_entrypoints_from_pyproject


def repo_tree(in_path: Path, fmt_json: bool = False):
    in_path = git_root_for(in_path)
    tree = build_repo_tree(in_path)
    if fmt_json:
        print(json.dumps(tree, indent=2))
        return 0

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

def cli():
    parser = argh.ArghParser()
    parser.add_commands([
        repo_tree,
        py_entrypoints
    ])
    parser.dispatch()

    # Only one entrypoint
    #argh.dispatch_command(main)
