"""The Docker image installs only the declared dependencies, so every library the code needs must be declared."""
import ast
import re
import sys
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # Python 3.11+

ROOT = Path(__file__).resolve().parents[1]
IMPORT_NAME = {"PyYAML": "yaml", "python-multipart": "multipart"}
STDLIB = set(sys.stdlib_module_names)


def _declared() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    reqs = project["dependencies"] + project["optional-dependencies"]["server"]
    names = {re.split(r"[<>=\[ ]", r, maxsplit=1)[0] for r in reqs}
    return {IMPORT_NAME.get(n, n).lower() for n in names}


def test_every_third_party_import_is_declared():
    used = set()
    for path in (ROOT / "src" / "mqresolver").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                used |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                used.add(node.module.split(".")[0])
    third_party = {m.lower() for m in used if m not in STDLIB and m != "mqresolver"}
    assert third_party <= _declared(), third_party - _declared()


def test_scipy_declared_for_networkx_layouts():
    # graph.py calls nx.kamada_kawai_layout and nx.spring_layout, which import scipy lazily.
    assert "scipy" in _declared()
