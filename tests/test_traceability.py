"""Every acceptance case and functional requirement is covered by at least one test."""

from __future__ import annotations

import ast
import json
from pathlib import Path

TESTS = Path(__file__).parent
FRS = [f"FR{i:02d}" for i in range(1, 26)]


def _markers() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in TESTS.glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "acceptance" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant):
                    found.setdefault(arg.value, set()).add(path.name)
    return found


def test_every_acceptance_case_has_a_marked_test():
    inventory = json.loads((TESTS / "acceptance_cases.json").read_text())
    markers = _markers()
    for case in inventory["cases"]:
        assert case["id"] in markers, f"{case['id']} has no test marked acceptance('{case['id']}')"
        assert Path(case["tests"]).name in markers[case["id"]], case


def test_every_functional_requirement_is_traced():
    inventory = json.loads((TESTS / "acceptance_cases.json").read_text())
    covered = {r for c in inventory["cases"] for r in c["requirements"]}
    assert covered == set(FRS), sorted(set(FRS) - covered)
