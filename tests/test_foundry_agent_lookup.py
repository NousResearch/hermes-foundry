import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest
import urllib.error


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts/foundry_agent_lookup.py"
SPEC = importlib.util.spec_from_file_location("foundry_agent_lookup", MODULE_PATH)
LOOKUP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LOOKUP)


URL = (
    "https://example.services.ai.azure.com/api/projects/example-project/"
    "agents/example-agent?api-version=v1"
)


class _Response:
    def __init__(self, body=b'{"name":"example-agent"}', status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def open(self, _request, timeout):
        assert timeout == 30
        if self.error is not None:
            raise self.error
        return self.response


def _http_error(code):
    return urllib.error.HTTPError(URL, code, "failure", {}, None)


class FoundryAgentLookupTests(unittest.TestCase):
    def test_accepts_only_exact_foundry_agent_endpoint(self):
        self.assertTrue(LOOKUP._valid_agent_url(URL))
        self.assertFalse(LOOKUP._valid_agent_url(URL.replace("https://", "http://")))
        self.assertFalse(LOOKUP._valid_agent_url(URL.replace("services.ai.azure.com", "example.com")))
        self.assertFalse(LOOKUP._valid_agent_url(URL.replace("example.services", "nested.example.services")))
        self.assertFalse(LOOKUP._valid_agent_url(URL.replace("api-version=v1", "api-version=v2")))
        self.assertFalse(LOOKUP._valid_agent_url(URL + "&redirect=https://example.com"))
        self.assertFalse(LOOKUP._valid_agent_url(URL.replace("azure.com", "azure.com:invalid")))

    def test_valid_json_200_is_found(self):
        status, body = LOOKUP._lookup(URL, "secret", opener=_Opener(response=_Response()))

        self.assertEqual(LOOKUP.FOUND, status)
        self.assertEqual("example-agent", json.loads(body)["name"])

    def test_only_404_is_absent(self):
        status, _ = LOOKUP._lookup(URL, "secret", opener=_Opener(error=_http_error(404)))
        self.assertEqual(LOOKUP.NOT_FOUND, status)

        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code):
                status, _ = LOOKUP._lookup(URL, "secret", opener=_Opener(error=_http_error(code)))
                self.assertEqual(LOOKUP.FAILED, status)

    def test_invalid_or_oversized_response_fails_closed(self):
        for body in (b"not-json", b"[]", b"x" * (LOOKUP.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(size=len(body)):
                status, _ = LOOKUP._lookup(URL, "secret", opener=_Opener(response=_Response(body)))
                self.assertEqual(LOOKUP.FAILED, status)

    def test_private_record_is_mode_600(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "agent.json"
            LOOKUP._write_private(target, b'{"name":"example-agent"}')

            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            self.assertEqual("example-agent", json.loads(target.read_text())["name"])

    def test_callers_branch_only_on_explicit_not_found(self):
        for relative in ("scripts/azd-post-provision.sh", "scripts/create-hosted-blueprint.sh"):
            text = (ROOT / relative).read_text()
            self.assertIn("foundry_agent_lookup.py", text)
            self.assertRegex(text, r'lookup_status.*\n.*\[\[ "\$lookup_status" -(?:eq|ne) 4 \]\]')

    def test_standalone_creator_stops_before_any_azure_call_on_lookup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            fake_python = temp / "python3"
            fake_python.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'python %s\\n' "$*" >> "$CALL_LOG"
                exit 1
            """))
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                exit 9
            """))
            for executable in (fake_python, fake_az):
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "ACCOUNT": "example",
                "PROJECT": "example-project",
                "AGENT": "example-agent",
                "PRIMARY_MODEL": "primary-model",
                "FALLBACK_MODEL": "fallback-model",
                "BLUEPRINT_ID": "11111111-1111-1111-1111-111111111111",
                "MAIB_NAME": "example-blueprint",
                "IMAGE": "example.azurecr.io/hermes@sha256:" + "a" * 64,
            })
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/create-hosted-blueprint.sh")],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("existence could not be verified", result.stderr)
            self.assertNotIn("az ", calls.read_text())


if __name__ == "__main__":
    unittest.main()
