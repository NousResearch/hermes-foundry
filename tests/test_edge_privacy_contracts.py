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
        self.assertNotIn("[web_search] query={", source)
        self.assertNotIn('-> {path}', source)
        self.assertNotIn("{str(exc)[:300]}", source)
        self.assertNotIn('"session": os.environ.get("FOUNDRY_AGENT_SESSION_ID"', source)
        self.assertIn('"session_tag": session_tag', source)

    def test_anonymous_bot_channels_are_not_treated_as_tenant_colleagues(self):
        source = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        self.assertNotIn('|| string.Equals(ch, "directline"', source)
        self.assertNotIn('|| string.Equals(ch, "webchat"', source)
        self.assertIn("activityTenantId == _agent.TenantId", source)

    def test_structural_turn_logs_exclude_person_and_conversation_fields(self):
        source = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        self.assertNotIn('$"fromId=', source)
        self.assertNotIn('$"fromName=', source)
        self.assertNotIn('$"recipId=', source)
        self.assertNotIn('$"conv=', source)
        self.assertNotIn('EdgeLog("agentic.ids"', source)
        self.assertNotIn("ex.Message", "\n".join(
            line for line in source.splitlines() if "EdgeLog(" in line
        ))

    def test_foundry_telemetry_does_not_export_genai_content_or_connection_strings(self):
        source = (ROOT / "src/edge/Program.cs").read_text()
        router = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        routine = (
            ROOT / "src/edge/AgentLogic/ResponsesProtocol/HermesResponseHandler.cs"
        ).read_text()
        self.assertIn(
            'SetSwitch("Azure.Experimental.TraceGenAIMessageContent", false)', source
        )
        self.assertNotIn('AI ConnectionString:', source)
        self.assertNotIn('Azure Key Vault configured: {keyVaultUri}', source)
        self.assertNotIn('serviceUrl={G("serviceUrl")}', source)
        self.assertNotIn("                    session,", source)
        self.assertIn("session_tag = sessionTag", source)
        self.assertNotIn("                    session,", router)
        self.assertNotIn("                            session,", routine)
        self.assertIn("session_tag = sessionTag", router)
        self.assertIn("session_tag = sessionTag", routine)

    def test_attachment_and_progress_failures_do_not_log_exception_messages(self):
        edge = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        gateway = (ROOT / "src/gateway/app.py").read_text()
        self.assertNotIn('upEx.Message', edge)
        self.assertNotIn('$"attempt={progress.FailedUpdates} {ex.GetType().Name}: {ex.Message}"', edge)
        self.assertNotIn('head.Substring', edge)
        self.assertNotIn('attach.bad | {type(e).__name__}: {e}', gateway)
        self.assertNotIn('outside {OUTBOX}', gateway)

    def test_compatibility_mode_logs_no_agent_ids_or_service_response_bodies(self):
        factory = (
            ROOT / "src/edge/AgentLogic/ResponsesApi/ResponsesApiAgentLogicServiceFactory.cs"
        ).read_text()
        service = (
            ROOT / "src/edge/AgentLogic/ResponsesApi/ResponsesApiAgentLogicService.cs"
        ).read_text()
        self.assertNotIn("AGENTIC-IDS", factory)
        self.assertNotIn("Response: {Response}", factory)
        self.assertNotIn("({Url})", factory)
        self.assertNotIn("Process environment variables", service)
        self.assertNotIn("Responses API request: {Request}", service)
        self.assertNotIn("Responses API response: {Response}", service)
        self.assertNotIn("{IncomingText}", service)
        self.assertNotIn("{ResponseId}", service)
        self.assertNotIn("{ConversationId}", service)

    def test_identity_and_routine_logs_use_no_raw_directory_identifiers(self):
        router = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        routine = (ROOT / "src/edge/Services/RoutineBindingService.cs").read_text()
        obo = (ROOT / "src/edge/Services/AgentUserAuthorizationService.cs").read_text()
        self.assertNotIn('EdgeLog("agentic.ids"', router)
        self.assertNotIn("{AgentIdentityId}", routine)
        self.assertNotIn("{AgentUserId}", routine)
        self.assertNotIn("{AgentIdentityId}", obo)
        self.assertNotIn("{AgentUserId}", obo)
        self.assertIn("oid_tag={ObjectIdTag}", routine)

    def test_activity_and_routine_paths_forward_safe_correlation(self):
        activity = (
            ROOT / "src/edge/AgentLogic/HermesRouter/HermesRouterAgentLogicService.cs"
        ).read_text()
        routine = (
            ROOT / "src/edge/AgentLogic/ResponsesProtocol/HermesResponseHandler.cs"
        ).read_text()
        self.assertIn('"X-Hermes-Correlation-Id", CurrentCorrelationId()', activity)
        self.assertIn('"X-Hermes-Correlation-Id", CurrentCorrelationId()', routine)

    def test_production_edge_does_not_return_detailed_framework_errors(self):
        settings = (ROOT / "src/edge/appsettings.json").read_text()
        self.assertIn('"DetailedErrors": false', settings)
        self.assertNotIn('"DetailedErrors": true', settings)


if __name__ == "__main__":
    unittest.main()
