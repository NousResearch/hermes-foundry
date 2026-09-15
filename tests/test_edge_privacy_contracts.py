import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class EdgePrivacyContractTests(unittest.TestCase):
    def test_activity_payload_and_runtime_session_are_not_returned_to_callers(self):
        source = (ROOT / "src/edge/AgentLogic/A365AgentApplication.cs").read_text()
        self.assertNotIn("Serialize(turnContext.Activity)", source)
        self.assertNotIn("FOUNDRY_AGENT_SESSION_ID", source)
        self.assertNotIn("$\"Exception:", source)

    def test_gateway_never_logs_reply_preview_or_returns_log_tail(self):
        source = (ROOT / "src/gateway/app.py").read_text()
        self.assertNotIn("preview={", source)
        self.assertNotIn("hermes_log_tail", source)
        self.assertNotIn("HERMES_LOG_PATH", source)

    def test_tool_telemetry_does_not_echo_results_or_exception_messages(self):
        source = (ROOT / "src/sidecar/agent365_mcp.py").read_text()
        self.assertNotIn("' :: ' + r[:300]", source)
        self.assertNotIn("traceback.print_exc", source)


if __name__ == "__main__":
    unittest.main()
