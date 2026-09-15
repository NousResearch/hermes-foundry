import pathlib
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


if __name__ == "__main__":
    unittest.main()
