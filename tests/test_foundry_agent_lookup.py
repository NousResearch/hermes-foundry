import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
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


class PostProvisionExistingAgentTests(unittest.TestCase):
    EXPECTED = "22222222-2222-2222-2222-222222222222"
    EXISTING = "33333333-3333-3333-3333-333333333333"

    def run_hook(self, blueprint=EXISTING, lookup_status=0, raw_fixture=None, expected=None):
        # Execute the real hook in an isolated checkout. All Azure and downstream write commands
        # are stubs; no developer credentials, local azd environment or production tenant is used.
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            scripts = temp / "scripts"
            scripts.mkdir()
            binaries = temp / "bin"
            binaries.mkdir()
            (binaries / "python3").symlink_to(sys.executable)
            hook = scripts / "azd-post-provision.sh"
            hook.write_text((ROOT / "scripts/azd-post-provision.sh").read_text())
            (temp / "fixture.json").write_text(raw_fixture if raw_fixture is not None else
                json.dumps({"blueprint": {"client_id": blueprint}}))
            (scripts / "foundry_agent_lookup.py").write_text(textwrap.dedent("""\
                import os, pathlib, sys
                code = int(os.environ['LOOKUP_STATUS'])
                if code == 0:
                    path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
                    path.write_text(pathlib.Path(os.environ['FIXTURE']).read_text())
                sys.exit(code)
            """))
            build = temp / "build.sh"
            build.write_text('#!/usr/bin/env bash\naz acr build --registry "$REG" --image "hermes-full-foundry:$1"\n')
            build.chmod(0o700)
            fake_az = binaries / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  account\\ set*|rest\\ --method\\ GET*|acr\\ build*) exit 0 ;;
                  *) exit 9 ;;
                esac
            """))
            fake_az.chmod(0o700)
            for name in ("create-hosted-blueprint.sh", "publish-autopilot-blueprint.sh",
                         "configure-blueprint-graph-permissions.sh", "install-agent-rbac-automation.sh"):
                stub = scripts / name
                stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "' + name + '" >> "$CALL_LOG"\n')
                stub.chmod(0o700)
            env = dict(os.environ)
            env.update({
                "PATH": f"{binaries}:{env['PATH']}", "CALL_LOG": str(temp / "calls.log"),
                "FIXTURE": str(temp / "fixture.json"), "LOOKUP_STATUS": str(lookup_status),
                "ACCOUNT": "example", "PROJECT": "example-project", "AGENT": "example-agent",
                "REG": "example", "TAG": "test", "PRIMARY_MODEL": "primary", "FALLBACK_MODEL": "fallback",
                "RESOURCE_GROUP": "example-group", "LOCATION": "eastus2", "SUBSCRIPTION_ID": self.EXPECTED,
                "TENANT_ID": self.EXPECTED, "BLUEPRINT_ID": expected or self.EXPECTED,
                "MAIB_NAME": "example-blueprint", "BOT_SERVICE_ID": "example-bot",
                "HERMES_INSTALL_ROUTINE_AUTOMATION": "true",
            })
            result = subprocess.run(["bash", str(hook)], env=env, capture_output=True,
                                    text=True, timeout=10, check=False)
            return result, (temp / "calls.log").read_text()

    def test_different_existing_identity_skips_cleanly_before_any_post_hook_writes(self):
        result, calls = self.run_hook()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Existing Hermes deployment found. Remaining setup skipped.", result.stdout)
        self.assertIn(self.EXISTING, result.stdout)
        self.assertIn(self.EXPECTED, result.stdout)
        self.assertIn("Azure infrastructure provisioning ran before this check", result.stdout)
        self.assertNotIn("Developer provisioning is complete", result.stdout)
        self.assertNotIn("resuming publication", result.stdout)
        self.assertEqual("", result.stderr)
        self.assertNotIn("acr build", calls)
        self.assertNotIn(".sh", calls)

    def test_same_identity_resumes_publication_without_rebuilding(self):
        result, calls = self.run_hook(blueprint=self.EXPECTED)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("expected identity", result.stdout)
        self.assertNotIn("acr build", calls)
        self.assertNotIn("create-hosted-blueprint.sh", calls)
        for step in ("publish-autopilot-blueprint.sh", "configure-blueprint-graph-permissions.sh",
                     "install-agent-rbac-automation.sh"):
            self.assertIn(step, calls)

    def test_identity_comparison_is_case_insensitive(self):
        identifier = "-".join(("a" * 8, "b" * 4, "c" * 4, "d" * 4, "e" * 12))
        result, calls = self.run_hook(blueprint=identifier.upper(), expected=identifier)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("publish-autopilot-blueprint.sh", calls)

    def test_absent_agent_still_builds_creates_and_publishes(self):
        result, calls = self.run_hook(lookup_status=4)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertLess(calls.index("acr build"), calls.index("create-hosted-blueprint.sh"))
        self.assertLess(calls.index("create-hosted-blueprint.sh"), calls.index("publish-autopilot-blueprint.sh"))
        self.assertIn("Developer provisioning is complete", result.stdout)

    def test_lookup_failure_remains_an_error_without_writes(self):
        result, calls = self.run_hook(lookup_status=1)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("existence could not be verified", result.stderr)
        self.assertNotIn("acr build", calls)
        self.assertNotIn(".sh", calls)

    def test_unknown_or_malformed_identity_is_not_treated_as_existing_success(self):
        for blueprint in (None, "", "-" * 36, "not-a-blueprint"):
            with self.subTest(blueprint=blueprint):
                result, calls = self.run_hook(blueprint=blueprint)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("acr build", calls)
                self.assertNotIn(".sh", calls)
        result, calls = self.run_hook(raw_fixture="invalid json")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("acr build", calls)
        self.assertNotIn(".sh", calls)


if __name__ == "__main__":
    unittest.main()
