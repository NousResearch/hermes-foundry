#!/usr/bin/env python3
"""Read one Foundry hosted-agent record without confusing access failures with absence."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


FOUND = 0
FAILED = 1
NOT_FOUND = 4
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RESOURCE = "https://ai.azure.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _valid_agent_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not re.fullmatch(r"[a-z0-9-]+\.services\.ai\.azure\.com", hostname)
        or parsed.fragment
    ):
        return False
    if not re.fullmatch(r"/api/projects/[A-Za-z0-9._-]+/agents/[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", parsed.path):
        return False
    return query == {"api-version": ["v1"]}


def _azure_token() -> str:
    completed = subprocess.run(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            RESOURCE,
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    token = completed.stdout.strip()
    if completed.returncode != 0 or not token:
        raise RuntimeError("Azure authentication failed")
    return token


def _lookup(url: str, token: str, *, opener=None) -> tuple[int, bytes | None]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=30) as response:
            if response.status != 200:
                return FAILED, None
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return FAILED, None
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                return FAILED, None
            return FOUND, body
    except urllib.error.HTTPError as error:
        error.close()
        return (NOT_FOUND, None) if error.code == 404 else (FAILED, None)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return FAILED, None


def _write_private(path: pathlib.Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    if not _valid_agent_url(args.url):
        print("ERROR: refused an invalid Foundry hosted-agent lookup URL.", file=sys.stderr)
        return FAILED

    try:
        token = _azure_token()
        status, body = _lookup(args.url, token)
    except (RuntimeError, OSError):
        print("ERROR: could not authenticate the Foundry hosted-agent lookup.", file=sys.stderr)
        return FAILED

    try:
        args.output.unlink(missing_ok=True)
    except OSError:
        print("ERROR: could not prepare the private hosted-agent lookup record.", file=sys.stderr)
        return FAILED

    if status == FOUND and body is not None:
        try:
            _write_private(args.output, body)
        except OSError:
            print("ERROR: could not save the private hosted-agent lookup record.", file=sys.stderr)
            return FAILED
        return FOUND
    if status == NOT_FOUND:
        return NOT_FOUND

    print(
        "ERROR: the hosted-agent lookup failed. No create operation was attempted. "
        "Verify Foundry access and service availability, then retry.",
        file=sys.stderr,
    )
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
