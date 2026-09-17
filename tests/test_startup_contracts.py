import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class StartupContractTests(unittest.TestCase):
    def test_pid_one_forwards_termination_to_every_supervised_service(self):
        source = (ROOT / "start.sh").read_text()
        self.assertIn("trap shutdown TERM INT", source)
        self.assertIn("SUPERVISOR_PIDS", source)
        self.assertIn("trap stop_supervisor TERM INT", source)
        self.assertIn('kill -TERM "$child_pid"', source)
        self.assertIn('wait "$child_pid"', source)
        for service in ("sidecar", "gateway", "edge", "hermes"):
            self.assertIn(f"supervise {service} ", source)

    def test_shutdown_does_not_return_runtime_log_content_to_callers(self):
        source = (ROOT / "start.sh").read_text()
        self.assertNotIn("tee -a /tmp/hermes.log", source)

    def test_dependency_monitor_uses_edge_deep_health(self):
        source = (ROOT / "start.sh").read_text()
        self.assertIn('urlopen("http://127.0.0.1:8088/healthz"', source)
        self.assertNotIn('urlopen("http://127.0.0.1:8080/healthz"', source)

    def test_dotnet_container_inputs_are_digest_pinned_and_multiarch(self):
        source = (ROOT / "Dockerfile").read_text()
        digest = r"sha256:[0-9a-f]{64}"
        self.assertRegex(
            source,
            rf"ARG DOTNET_SDK_IMAGE=mcr\.microsoft\.com/dotnet/sdk:9\.0@{digest}",
        )
        self.assertRegex(
            source,
            rf"ARG DOTNET_ASPNET_IMAGE=mcr\.microsoft\.com/dotnet/aspnet:9\.0@{digest}",
        )
        self.assertIn("FROM ${DOTNET_SDK_IMAGE} AS edge-build", source)
        self.assertIn("FROM ${DOTNET_ASPNET_IMAGE} AS aspnet-runtime", source)
        self.assertIn(
            "COPY --from=aspnet-runtime /usr/share/dotnet /usr/share/dotnet",
            source,
        )
        self.assertNotIn("FROM mcr.microsoft.com/dotnet/sdk:9.0", source)
        self.assertNotIn("--from=mcr.microsoft.com/dotnet/aspnet:9.0", source)

    def test_direct_dotnet_packages_do_not_use_wildcard_versions(self):
        source = (ROOT / "src/edge/HelloWorldA365Agent.csproj").read_text()
        package_versions = re.findall(
            r'<PackageReference\s+Include="[^"]+"\s+Version="([^"]+)"', source
        )
        self.assertGreater(len(package_versions), 0)
        self.assertFalse(
            [version for version in package_versions if "*" in version],
            "Direct NuGet dependencies must resolve identically in every release build",
        )

    def test_durable_state_whitelist_is_seeded_without_overwriting_owner_changes(self):
        source = (ROOT / "start.sh").read_text()
        self.assertIn('if [ ! -f "$HERMES_STATE/state-sync.json" ]', source)
        self.assertIn(
            'cp /opt/state-sync.defaults.json "$HERMES_STATE/state-sync.json"', source)
        self.assertIn("COPY config/state-sync.defaults.json /opt/state-sync.defaults.json",
                      (ROOT / "Dockerfile").read_text())

    def test_releases_identify_state_writer_and_keep_legacy_bridge(self):
        updater = (ROOT / "new-version.sh").read_text()
        creator = (ROOT / "scripts" / "create-hosted-blueprint.sh").read_text()
        for source in (updater, creator):
            self.assertIn('"HERMES_RELEASE_ID"', source)
            self.assertIn('"STATE_WRITER_PROTOCOL"', source)
            self.assertIn('"dual-v1-v2"', source)


if __name__ == "__main__":
    unittest.main()
