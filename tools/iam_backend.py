# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""The Portal IAM of this installation for the stand-ins: Keycloak (`IAM_URL` is set), otherwise the
in-memory IAM of `tools/test_iam.py`, which only serves the tests of the scaffold itself."""
from __future__ import annotations

import os


def uses_keycloak() -> bool:
    return bool(os.environ.get("IAM_URL"))


def backend(module: str):
    """Object with `find`, `person`, `bind`, `add_portal_roles`, `users_with_portal_role`,
    `create_technical_user` and `rotate_secret`."""
    if uses_keycloak():
        from tools.keycloak_admin import KeycloakAdmin
        return KeycloakAdmin(module)
    from tools import test_iam
    return test_iam.iam()
