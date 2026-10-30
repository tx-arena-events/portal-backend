# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of C12-M1 Process Orchestration for other modules (README 9). C12-M1 has no steps."""
from __future__ import annotations

from portal_common import standin
from portal_common.problems import Problem

from . import engine as eng

MODULE = eng.MODULE
SUBJECT_TYPES = {"APPLICATION", "COMPANY", "TECHNICAL_USER", "OFFER", "SUBSCRIPTION", "CONNECTOR"}


def find_processes_of_subject(*, subjectType: str, subjectId: str) -> list:
    """`GET /internal/processes`: the processes of one subject, newest first."""
    if subjectType not in SUBJECT_TYPES:
        raise Problem(MODULE, 400, "validation", detail="subjectType")
    items = [eng.summary(p) for p in list(eng.get_engine().processes.values())
             if p["subjectType"] == subjectType and p["subjectId"] == subjectId]
    return sorted(items, key=lambda x: x["createdAt"], reverse=True)


__getattr__ = standin.missing_interface(MODULE)
