# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Test environment: installation OSP A at http://osp-a.test, everything in one process."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://osp-a.test"
os.environ.setdefault("SPEC_DIR", str(ROOT / "spec"))
os.environ["SETTINGS_FILE"] = str(ROOT / "config" / "osp-a" / "installation.json")
os.environ["BASE_URL"] = BASE
os.environ.pop("IAM_URL", None)
os.environ.pop("MODULE_URLS", None)
os.environ["MODULE_URLS_DEFAULT"] = "in-process"

from portal_common import events, http, settings, tokens  # noqa: E402
from tools import test_iam  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    settings.reset()
    tokens.reset()
    test_iam.reset()
    events.RELAY.reset()
    yield
    http._local.clear()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def person_token(key: str) -> str:
    iam = test_iam.iam()
    return iam.person_token(iam.find(key))
