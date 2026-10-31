#!/usr/bin/env python3
# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Get a token from the test Portal IAM and print the access token (C2-M5 14.4).

    get-token.py erika                  test person, browser flow with the form of the stand-in
    get-token.py gerd --realm CX-Central-B
    get-token.py svc-c3-m1              service account of a module
    get-token.py sa-div-callback        service account for callbacks
    get-token.py sa-csp                 client assertion with the test key (variant test only)
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools import keycloak_login as login  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="test person or client")
    parser.add_argument("--realm", default=os.environ.get("IAM_REALM", "CX-Central"))
    args = parser.parse_args()
    if args.name.startswith("svc-") or args.name in login.KEY_CLIENTS or args.name in login.SECRET_CLIENTS:
        status, tokens = login.client_token(args.name, args.realm)
        if status != 200:
            sys.exit(f"no token: HTTP {status} {tokens}")
        print(tokens["access_token"])
    else:
        print(login.person_token(args.name, args.realm)["access_token"])


if __name__ == "__main__":
    main()
