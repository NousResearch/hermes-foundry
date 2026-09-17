import importlib.util
import pathlib
import stat
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "write-release-record.py"
SPEC = importlib.util.spec_from_file_location("write_release_record", SCRIPT)
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseRecordTests(unittest.TestCase):
    def test_version_creator_passes_only_the_digest_to_the_record(self):
        source = (ROOT / "new-version.sh").read_text()
        self.assertIn('--image-digest "$DIGEST"', source)
        self.assertNotIn('--image-digest "$IMAGE"', source)
        self.assertIn('deployment-output/release-records/', source)
        self.assertIn('SOURCE_STATE=dirty', source)

    def test_record_contains_only_portable_release_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "release.md"
            RELEASE.write_record(
                output=output,
                dockerfile=ROOT / "Dockerfile",
                source_commit="a" * 40,
                source_state="clean",
                image_digest="sha256:" + "b" * 64,
                previous_version="7",
                new_version="8",
            )

            text = output.read_text()
            self.assertIn("Source commit: `" + "a" * 40 + "`", text)
            self.assertIn("Combined image digest: `sha256:" + "b" * 64 + "`", text)
            self.assertIn("New hosted-agent version: `8`", text)
            self.assertIn("Rollback candidate: `7`", text)
            self.assertIn("nousresearch/hermes-agent@sha256:", text)
            for forbidden in (
                "onmicrosoft.com", "azurecr.io", "Tenant ID", "Agent User", "model deployment",
            ):
                self.assertNotIn(forbidden, text)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_unpinned_or_malformed_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("ARG HERMES_IMAGE=example.invalid/hermes:latest\n")
            with self.assertRaisesRegex(ValueError, "digest-pinned HERMES_IMAGE"):
                RELEASE.write_record(
                    output=root / "release.md",
                    dockerfile=dockerfile,
                    source_commit="a" * 40,
                    source_state="clean",
                    image_digest="sha256:" + "b" * 64,
                    previous_version="7",
                    new_version="8",
                )

            with self.assertRaisesRegex(ValueError, "image digest"):
                RELEASE.write_record(
                    output=root / "release.md",
                    dockerfile=ROOT / "Dockerfile",
                    source_commit="a" * 40,
                    source_state="clean",
                    image_digest="registry.invalid/image:mutable",
                    previous_version="7",
                    new_version="8",
                )


if __name__ == "__main__":
    unittest.main()
