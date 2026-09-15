import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"


class ResourceProviderHookTests(unittest.TestCase):
    def run_hook(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            calls = temp / "calls.log"
            fake_az = temp / "az"
            fake_az.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                printf 'az %s\\n' "$*" >> "$CALL_LOG"
                case "$*" in
                  account\\ set*) exit 0 ;;
                  provider\\ show*) printf '%s\\n' NotRegistered ;;
                  provider\\ register*) exit 0 ;;
                  *) printf 'unexpected az call: %s\\n' "$*" >&2; exit 9 ;;
                esac
            """))
            fake_az.chmod(fake_az.stat().st_mode | stat.S_IXUSR)
            environment = dict(os.environ)
            environment.update({
                "PATH": f"{temp}:{environment['PATH']}",
                "CALL_LOG": str(calls),
                "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
                "HERMES_PROVISION_MODE": mode,
            })
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/azd-register-resource-providers.sh")],
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            return result, calls.read_text()

    def test_preview_reports_missing_providers_without_registering(self):
        result, calls = self.run_hook("preview")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("preview only; no change made", result.stdout)
        self.assertNotIn("provider register", calls)

    def test_apply_registers_missing_providers(self):
        result, calls = self.run_hook("apply")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, calls.count("provider register"))


if __name__ == "__main__":
    unittest.main()
