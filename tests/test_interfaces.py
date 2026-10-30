# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Call convention between modules: every module has `interface.py` with a function for every step
and query of its openapi.yaml, and modules import each other only through `modules.<id>.interface`."""
import ast
import importlib
from pathlib import Path

import pytest

from portal_common import spec

MODULES = Path(__file__).resolve().parents[1] / "modules"


@pytest.mark.parametrize("mid", sorted(spec.modules()))
def test_interface_exposes_every_step_and_query(mid):
    ms = spec.module(mid)
    iface = importlib.import_module(f"modules.{ms.package}.interface")
    ops = [op for op in ms.operations if op.is_step or op.is_query]
    names = [op.function_name for op in ops]
    assert len(names) == len(set(names)), f"{mid}: two operations with the same function name"
    missing = [f"{op.method} {op.path} -> {op.function_name}" for op in ops
               if not callable(getattr(iface, op.function_name, None))]
    assert not missing, f"modules/{ms.package}/interface.py lacks: {missing}"


def test_function_names_follow_the_convention():
    c1_m2 = spec.module("c1-m2")
    assert c1_m2.find("GET", "/internal/applications/a1").function_name == "get_application_internal"
    assert c1_m2.find("POST", "/steps/complete-application").function_name == "complete_application"
    assert spec.snake_case("getBPNForCompany") == "get_bpn_for_company"


def _imported_names(source: str, package: str):
    """Full dotted name of everything a file in `modules/<package>/` imports."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:   # relative: resolve against modules.<package>
                base = ".".join(["modules", package][: max(0, 3 - node.level)] + ([base] if base else []))
            yield from (f"{base}.{a.name}" for a in node.names)


def _foreign(source: str, package: str) -> list[str]:
    """Imports of another module that do not go through `modules.<id>.interface`."""
    return [n for n in _imported_names(source, package)
            if n.split(".")[0] == "modules" and len(n.split(".")) > 1 and n.split(".")[1] != package
            and n.split(".")[2:3] != ["interface"]]


def test_import_check_itself():
    ok = "from modules.c1_m2.interface import get_application_internal\nfrom ..c1_m2 import interface\n" \
         "import modules.c1_m2.interface\nfrom . import router\nfrom modules.c3_m1 import router"
    assert _foreign(ok, "c3_m1") == []
    bad = "from modules.c1_m2 import router\nfrom modules.c1_m2.router import x\nfrom ..c1_m2.service import y\n" \
          "from modules import c1_m2\nimport modules.c1_m2"
    assert len(_foreign(bad, "c3_m1")) == 5


def test_modules_import_each_other_only_through_interface():
    wrong = []
    for file in sorted(MODULES.glob("*/**/*.py")):
        package = file.relative_to(MODULES).parts[0]
        wrong += [f"{file.relative_to(MODULES.parent)}: {n}" for n in _foreign(file.read_text(encoding="utf-8"), package)]
    assert not wrong, "import other modules only from modules.<id>.interface: " + "; ".join(wrong)
