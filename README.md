# nate\_repo\_intel

Some miscellaneous tools to list information about a git repo. Possible applications include:

- Gathering information for helpdesk tickets
- Generating context for AI

## Installation

```
poetry install
poetry run nate_repo_intel repo-tree ./ 
```

## Usage

List entrypoints of a python package:

```
(base) nate@nate-Kudu:~/nate_repo_intel$ poetry run nate_repo_intel py-entrypoints ./ 
{
  "script_name": "nate_repo_intel",
  "target": "nate_repo_intel.cli:cli",
  "module": "nate_repo_intel.cli",
  "rel_path": "src/nate_repo_intel/cli.py"
}
```

Generate a human-readable tree of given git repository, obeying `.gitignore`.

```
(base) nate@nate-Kudu:~/nate_repo_intel$ poetry run nate_repo_intel repo-tree ./

📦 Repository: /home/nate/nate_repo_intel

├── 📁 .venv
├── 📁 src
│   └── 📁 nate_repo_intel
│       ├── 📄 __init__.py
│       ├── 📄 cli.py
│       ├── 📄 git_utils.py
│       └── 📄 py_utils.py
├── 📁 tests
│   └── 📄 test_util.py
├── 📄 .gitignore
├── 📄 LICENSE
├── 📄 poetry.lock
├── 📄 pyproject.toml
└── 📄 README.md
```


Generate a tree in JSON format:

```
(base) nate@nate-Kudu:~/nate_repo_intel$ poetry run nate_repo_intel repo-tree ./ --fmt-json
2026-01-07 17:47:25.561 | INFO     | nate_repo_intel.cli:repo_tree:18 - nate_repo_intel.cli
{
  "type": "dir",
  "name": "/home/nate/nate_repo_intel",
  "rel": "",
  "children": [
    {
      "type": "dir",
      "name": ".venv",
      "rel": ".venv",
      "children": []
    },
    {
      "type": "dir",
      "name": "src",
      "rel": "src",
      "children": [
        {
          "type": "dir",
          "name": "nate_repo_intel",
          "rel": "src/nate_repo_intel",
          "children": [
            {
              "type": "file",
              "name": "__init__.py",
              "rel": "src/nate_repo_intel/__init__.py"
            },
            {
              "type": "file",
              "name": "cli.py",
              "rel": "src/nate_repo_intel/cli.py"
            },
            {
              "type": "file",
              "name": "git_utils.py",
              "rel": "src/nate_repo_intel/git_utils.py"
            },
            {
              "type": "file",
              "name": "py_utils.py",
              "rel": "src/nate_repo_intel/py_utils.py"
            }
          ]
        }
      ]
    },
    {
      "type": "dir",
      "name": "tests",
      "rel": "tests",
      "children": [
        {
          "type": "file",
          "name": "test_util.py",
          "rel": "tests/test_util.py"
        }
      ]
    },
    {
      "type": "file",
      "name": ".gitignore",
      "rel": ".gitignore"
    },
    {
      "type": "file",
      "name": "LICENSE",
      "rel": "LICENSE"
    },
    {
      "type": "file",
      "name": "poetry.lock",
      "rel": "poetry.lock"
    },
    {
      "type": "file",
      "name": "pyproject.toml",
      "rel": "pyproject.toml"
    },
    {
      "type": "file",
      "name": "README.md",
      "rel": "README.md"
    }
  ]
}
```

## License

MIT / Expat
