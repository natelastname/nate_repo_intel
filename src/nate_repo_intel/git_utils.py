# -*- coding: utf-8 -*-
"""
Created on 2026-01-07T17:02:22-05:00

@author: nate
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def is_git_ignored(working_dir: str, path: str) -> bool:
    """
    Return True if `path` should be treated as ignored.

    - Treat .git and anything under it as ignored (git doesn't necessarily
      report .git via check-ignore since it's a special directory).
    - Otherwise defer to `git check-ignore`.
    """
    p = Path(path)

    # Normalize: if absolute, make it relative to working_dir when possible
    wd = Path(working_dir).resolve()
    if p.is_absolute():
        try:
            p = p.relative_to(wd)
        except ValueError:
            # Path isn't inside the repo root; treat as ignored for our purposes
            return True

    # .git is always excluded from traversal
    if p.parts and p.parts[0] == ".git":
        return True

    full = str(wd / p)

    result = subprocess.run(
        ["git", "-C", str(wd), "check-ignore", "-q", full],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0


class NotAGitRepoError(RuntimeError):
    pass

def git_root_for(path: str | Path) -> Path:
    """Return the root directory of the git repo containing `path`.

    Raises NotAGitRepoError if `path` is not inside a git repository.
    """
    if Path(path).is_file():
        path = Path(path).parent

    path = Path(path).resolve()

    # Run git in that directory
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    if result.returncode != 0:
        raise NotAGitRepoError(f"{path} is not inside a git repository")

    return Path(result.stdout.strip())


def build_repo_tree(repo_root: Path) -> dict[str, Any]:
    """
    Build a JSON-serializable tree for repo_root, skipping gitignored paths.

    Node schema:
      - type: "dir" | "file"
      - name: str
      - rel: repo-relative POSIX path ("" for root)
      - children: list[nodes]  (dirs only)
    """
    repo_root = repo_root.resolve()

    def walk(dir_path: Path) -> dict[str, Any]:
        rel_dir = dir_path.relative_to(repo_root).as_posix()
        node: dict[str, Any] = {
            "type": "dir",
            "name": dir_path.name if dir_path != repo_root else dir_path.as_posix(),
            "rel": "" if dir_path == repo_root else rel_dir,
            "children": [],
        }

        entries: list[Path] = []
        for p in dir_path.iterdir():
            rel = p.relative_to(repo_root).as_posix()
            if is_git_ignored(str(repo_root), rel):
                continue
            entries.append(p)

        # dirs first, then files; case-insensitive name sort
        entries.sort(key=lambda p: (p.is_file(), p.name.lower()))

        for entry in entries:
            rel = entry.relative_to(repo_root).as_posix()
            if entry.is_dir():
                node["children"].append(walk(entry))
            else:
                node["children"].append({
                    "type": "file",
                    "name": entry.name,
                    "rel": rel,
                })

        return node

    return walk(repo_root)


def format_repo_tree(tree: dict[str, Any]) -> str:
    """
    Format the JSON tree from build_repo_tree() into a human-readable string.
    """
    lines: list[str] = []

    root_display = tree.get("name", "<repo>")
    lines.append("")
    lines.append(f"📦 Repository: {root_display}")
    lines.append("")

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        children: list[dict[str, Any]] = node.get("children", [])

        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            connector = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")

            if child["type"] == "dir":
                lines.append(f"{prefix}{connector}📁 {child['name']}")
                walk(child, next_prefix)
            else:
                # Keep your old "📄" status marker (you can swap later for ✅/❌)
                lines.append(f"{prefix}{connector}📄 {child['name']}")

    walk(tree)
    return "\n".join(lines)
