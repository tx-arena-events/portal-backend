# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C11-M1 stand-in. The spec has no steps or queries for C11-M1 today."""
from __future__ import annotations

from portal_common import standin

MODULE = "c11-m1"

__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
