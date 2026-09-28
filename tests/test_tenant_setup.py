import copy
import http.client
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tenant_setup", ROOT / "scripts/tenant_setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
TENANT = "11111111-1111-1111-1111-111111111111"
BLUEPRINT = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"
APP = "44444444-4444-4444-4444-444444444444"


def state(phase="awaiting_approval"):
    return dict(schema=1, tenant=TENANT, subscription=TENANT, run_id=USER,
                environment="example", phase=phase)


def config(kind="apiBased"):
    return dict(isOverridableAtAgentIdLevel=False,
                endpointConfiguration={"configurationType": kind, "apiBased":
                    {"callbackUri": "https://example.com/messages"} if kind == "apiBased" else None,
                    "botBased": {"botId": BLUEPRINT} if kind == "botBased" else None},
                teamworkConfiguration={"oneOnOneChatConfiguration": {"messageNotificationMode": "allMessages"}})


class TenantSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "setup-state.json"
        self.patcher = patch.object(setup, "STATE", self.state_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_checkpoint_private_and_roundtrips(self):
        setup.save(state())
        self.assertEqual(setup.load(), state())
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("token", self.state_path.read_text())

    def test_missing_checkpoint_actionable(self):
        with self.assertRaisesRegex(RuntimeError, "No saved"):
            setup.load()

    def test_bad_checkpoint_fail_closed(self):
        for key, value in [("schema", 2), ("phase", "other"), ("tenant", "bad"), ("environment", "../../other")]:
            data = state(); data[key] = value; setup.save(data)
            with self.subTest(key=key), self.assertRaises((RuntimeError, ValueError)):
                setup.load()

    def test_eof_and_interrupt_pause_safely(self):
        for error in (EOFError, KeyboardInterrupt):
            with patch("builtins.input", side_effect=error), self.assertRaisesRegex(RuntimeError, "Paused safely"):
                setup.pause("Continue?")

    def test_pending_approval_does_not_provision_or_configure(self):
        data = state()
        with patch.object(setup, "pause"), patch.object(setup, "deployment", return_value={}), \
             patch.object(setup, "setup_client") as client, patch.object(setup.subprocess, "run") as process:
            with self.assertRaisesRegex(RuntimeError, "not reported approval"):
                setup.run(data)
        client.assert_not_called(); process.assert_not_called()
        self.assertEqual(data["phase"], "awaiting_approval")

    def test_provision_failure_retains_phase(self):
        data = state("provisioning")
        with patch.object(setup.subprocess, "run", return_value=Mock(returncode=1)), \
             self.assertRaisesRegex(RuntimeError, "Provisioning stopped"):
            setup.run(data)
        self.assertEqual(data["phase"], "provisioning")

    def test_post_hook_mismatch_does_not_advance(self):
        data = state("provisioning")
        with patch.object(setup.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(setup, "deployment", side_effect=RuntimeError("different blueprint")), \
             self.assertRaisesRegex(RuntimeError, "different blueprint"):
            setup.run(data)
        self.assertEqual(data["phase"], "provisioning")

    def test_approval_lost_returns_to_wait(self):
        data = state("configure_bot")
        with patch.object(setup, "deployment", return_value={}), self.assertRaisesRegex(RuntimeError, "no longer"):
            setup.run(data)
        self.assertEqual(setup.load()["phase"], "awaiting_approval")

    def test_completed_resume_has_no_writes(self):
        data = state("complete"); data.update(runtime_verified=True, operator_checks={"chat": True})
        agent = {"state": "enabled", "agent_endpoint": {"publish_approval_status": "approved"}}
        with patch.object(setup.subprocess, "run") as run, patch.object(setup, "request") as request, \
             patch.object(setup, "deployment", return_value=agent), patch.object(setup, "check_route"):
            setup.run(data)
        run.assert_not_called(); request.assert_not_called()

    def test_resume_configuration_skips_provision_and_finishes(self):
        data = state("configure_bot"); data["target"] = {"BLUEPRINT_ID": BLUEPRINT}
        with patch.object(setup, "deployment", return_value={"agent_endpoint": {"publish_approval_status": "approved"}}), \
             patch.object(setup, "pause"), patch.object(setup, "ensure_msal"), \
             patch.object(setup, "token", return_value="admin"), \
             patch.object(setup, "request", return_value={"id": USER}), \
             patch.object(setup, "setup_client", return_value=APP), \
             patch.object(setup, "browser_token", return_value="delegated"), \
             patch.object(setup, "enable_runtime") as enable, \
             patch.object(setup, "bind_bot") as bind, patch.object(setup.subprocess, "run") as process:
            setup.run(data)
        process.assert_not_called(); bind.assert_called_once_with(BLUEPRINT, BLUEPRINT, "delegated")
        enable.assert_called_once_with(data)
        self.assertEqual(setup.load()["phase"], "enable_runtime")

    def runtime_state(self):
        data = state("enable_runtime")
        data["target"] = dict(ACCOUNT="example", PROJECT="example-project", AGENT="example-agent")
        return data

    def runtime_agent(self, status="disabled"):
        return {"state": status, "agent_endpoint": {"publish_approval_status": "approved"}}

    def test_disabled_runtime_enabled_once_and_read_back(self):
        data = self.runtime_state()
        with patch.object(setup, "deployment", side_effect=[self.runtime_agent(), self.runtime_agent("enabled")]), \
             patch.object(setup, "check_route", return_value="3"), patch.object(setup, "confirm"), \
             patch.object(setup, "token", return_value="private"), patch.object(setup, "request", return_value={}) as req:
            setup.enable_runtime(data)
        self.assertEqual(req.call_count, 1)
        self.assertTrue(req.call_args.args[0].endswith("/example-agent:enable?api-version=v1"))
        self.assertEqual(req.call_args.args[2], "POST")
        self.assertEqual(data["phase"], "verify_instance")

    def test_enabled_resume_does_not_post(self):
        with patch.object(setup, "deployment", return_value=self.runtime_agent("enabled")), \
             patch.object(setup, "check_route", return_value="3"), patch.object(setup, "request") as req:
            setup.enable_runtime(self.runtime_state())
        req.assert_not_called()

    def test_activation_requires_explicit_confirmation(self):
        with patch.object(setup, "deployment", return_value=self.runtime_agent()), \
             patch.object(setup, "check_route", return_value="3"), patch("builtins.input", return_value="no"), \
             patch.object(setup, "request") as req, self.assertRaisesRegex(RuntimeError, "Not confirmed"):
            setup.enable_runtime(self.runtime_state())
        req.assert_not_called()

    def test_enable_forbidden_retains_resume_phase(self):
        data = self.runtime_state()
        with patch.object(setup, "deployment", return_value=self.runtime_agent()), \
             patch.object(setup, "check_route", return_value="3"), patch.object(setup, "confirm"), \
             patch.object(setup, "token", return_value="private"), \
             patch.object(setup, "request", side_effect=setup.ApiError(403, "forbidden")) as req, \
             self.assertRaises(setup.ApiError):
            setup.enable_runtime(data)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(data["phase"], "enable_runtime")

    def test_activation_polling_bounded(self):
        data = self.runtime_state()
        with patch.object(setup, "deployment", return_value=self.runtime_agent()) as get, \
             patch.object(setup, "check_route", return_value="3"), patch.object(setup, "confirm"), \
             patch.object(setup, "token", return_value="private"), patch.object(setup, "request") as req, \
             patch.object(setup.time, "sleep"), self.assertRaisesRegex(RuntimeError, "not verified"):
            setup.enable_runtime(data)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(get.call_count, 13)
        self.assertEqual(data["phase"], "enable_runtime")

    def test_old_complete_checkpoint_migrates_without_reprovision(self):
        data = state("complete")
        with patch.object(setup, "enable_runtime") as enable, patch.object(setup.subprocess, "run") as run:
            setup.run(data)
        enable.assert_called_once_with(data); run.assert_not_called()
        self.assertEqual(setup.load()["phase"], "enable_runtime")

    def test_complete_but_later_disabled_never_silently_reenabled(self):
        data = state("complete"); data.update(runtime_verified=True, operator_checks={"chat": True})
        with patch.object(setup, "deployment", return_value=self.runtime_agent()), \
             patch.object(setup, "request") as req, self.assertRaisesRegex(RuntimeError, "No automatic"):
            setup.run(data)
        req.assert_not_called()

    def test_prerequisite_decline_prevents_provision(self):
        data = state("prerequisites")
        with patch("builtins.input", return_value="no"), patch.object(setup.subprocess, "run") as run, \
             self.assertRaisesRegex(RuntimeError, "Not confirmed"):
            setup.run(data)
        run.assert_not_called(); self.assertEqual(data["phase"], "prerequisites")

    def test_guided_new_tenant_runs_all_phases_in_order(self):
        data = self.runtime_state(); data["phase"] = "prerequisites"
        data["target"]["BLUEPRINT_ID"] = BLUEPRINT
        with patch.object(setup.subprocess, "run", return_value=Mock(returncode=0)) as provision, \
             patch.object(setup, "deployment", return_value=self.runtime_agent("enabled")), \
             patch.object(setup, "pause"), patch.object(setup, "confirm"), \
             patch.object(setup, "ensure_msal"), patch.object(setup, "token", return_value="private"), \
             patch.object(setup, "request", return_value={"id": USER}), \
             patch.object(setup, "setup_client", return_value=APP), \
             patch.object(setup, "browser_token", return_value="delegated"), \
             patch.object(setup, "bind_bot") as bind, \
             patch.object(setup, "check_route", return_value="3"), \
             patch.object(setup, "verify_instance_identity"), patch("builtins.input", return_value="tested"):
            setup.run(data)
        provision.assert_called_once()
        bind.assert_called_once()
        self.assertTrue(data["prerequisites_confirmed"])
        self.assertTrue(data["runtime_verified"])
        self.assertEqual(data["routine_check"], "tested")
        self.assertEqual(setup.load()["phase"], "complete")

    def test_acceptance_only_completes_after_operator_checks(self):
        data = self.runtime_state(); data["phase"] = "verify_instance"
        with patch.object(setup, "deployment", return_value=self.runtime_agent("enabled")), \
             patch.object(setup, "check_route", return_value="3"), patch.object(setup, "confirm"), \
             patch.object(setup, "verify_instance_identity"), \
             patch("builtins.input", return_value="tested"):
            setup.verify_instance(data)
        self.assertEqual(setup.load()["phase"], "complete")
        self.assertEqual(data["operator_checks"], dict(chat=True, mail=True, onedrive=True))
        self.assertEqual(data["routine_check"], "tested")

    def test_failed_delivery_keeps_installation_incomplete(self):
        data = self.runtime_state(); data["phase"] = "verify_instance"
        with patch.object(setup, "deployment", return_value=self.runtime_agent("enabled")), \
             patch.object(setup, "check_route", return_value="3"), \
             patch.object(setup, "verify_instance_identity"), \
             patch.object(setup, "confirm", side_effect=[None, RuntimeError("Not confirmed")]), \
             self.assertRaisesRegex(RuntimeError, "Not confirmed"):
            setup.verify_instance(data)
        self.assertEqual(data["phase"], "verify_instance")

    def test_instance_must_belong_to_installation(self):
        for blueprint in (BLUEPRINT, USER):
            data = self.runtime_state(); data["target"]["BLUEPRINT_ID"] = BLUEPRINT
            with patch("builtins.input", return_value="test-agent@example.com"), \
                 patch.object(setup, "token", return_value="private"), \
                 patch.object(setup, "request", side_effect=[
                     {"identityParentId": APP, "accountEnabled": True},
                     {"agentIdentityBlueprintId": blueprint, "accountEnabled": True}]) as req:
                if blueprint == BLUEPRINT:
                    setup.verify_instance_identity(data)
                    self.assertEqual(setup.load()["instance_upn"], "test-agent@example.com")
                else:
                    with self.assertRaisesRegex(RuntimeError, "different blueprint"):
                        setup.verify_instance_identity(data)
                self.assertTrue(all(len(c.args) == 2 for c in req.call_args_list))

    def test_serving_route_validation(self):
        agent = {"agent_endpoint": {"protocols": ["activity", "responses"],
            "authorization_schemes": [{"type": "BotServiceTenant"}],
            "version_selector": {"version_selection_rules": [{"agent_version": "3", "traffic_percentage": 100}]}}}
        version = {"status": "active", "definition": {"kind": "hosted",
            "environment_variables": {"AgentLogic": "HermesRouter"},
            "container_protocol_versions": [{"protocol": "activity_protocol"}, {"protocol": "responses"}]}}
        with patch.object(setup, "token", return_value="private"), patch.object(setup, "request", return_value=version):
            self.assertEqual(setup.check_route(self.runtime_state(), agent), "3")
            version["status"] = "failed"
            with self.assertRaisesRegex(RuntimeError, "not active"):
                setup.check_route(self.runtime_state(), agent)
            version["status"] = "active"
            version["definition"]["environment_variables"]["AgentLogic"] = "ResponsesApi"
            with self.assertRaisesRegex(RuntimeError, "expected Hermes runtime"):
                setup.check_route(self.runtime_state(), agent)
            agent["agent_endpoint"]["authorization_schemes"] = []
            with self.assertRaisesRegex(RuntimeError, "authorization"):
                setup.check_route(self.runtime_state(), agent)

    def test_empty_enable_response_accepted(self):
        response = Mock(); response.read.return_value = b""
        context = Mock(); context.__enter__ = Mock(return_value=response); context.__exit__ = Mock(return_value=False)
        with patch.object(setup.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = context
            self.assertEqual(setup.request("https://example.services.ai.azure.com/agents/test:enable", "private", "POST"), {})

    def test_binding_preserves_settings_and_uses_documented_api(self):
        before = config(); after = config("botBased")
        with patch.object(setup, "request", side_effect=[before, after, after]) as req:
            setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")
        write = req.call_args_list[1]
        self.assertIn("/beta/applications/" + BLUEPRINT + "/microsoft.graph.agentIdentityBlueprint/communicationConfiguration", write.args[0])
        self.assertEqual(write.args[2], "PUT")
        self.assertEqual(write.args[3]["teamworkConfiguration"], before["teamworkConfiguration"])
        self.assertFalse(write.args[3]["isOverridableAtAgentIdLevel"])
        self.assertIsNone(write.args[3]["endpointConfiguration"]["apiBased"])
        self.assertEqual(before, config())

    def test_already_bound_no_put(self):
        with patch.object(setup, "request", side_effect=[config("botBased"), config("botBased")]) as req:
            setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")
        self.assertEqual(len(req.call_args_list), 2)
        self.assertTrue(all(len(call.args) == 2 for call in req.call_args_list))

    def test_unknown_fields_prevent_destructive_replace(self):
        before = config(); before["futureSetting"] = True
        with patch.object(setup, "request", return_value=before) as req, self.assertRaisesRegex(RuntimeError, "Unknown"):
            setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")
        self.assertEqual(req.call_count, 1)

    def test_readback_mismatch_not_success(self):
        with patch.object(setup, "request", side_effect=[config(), config("botBased"), config()]), \
             self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")

    def test_notification_change_not_success(self):
        after = config("botBased"); after["teamworkConfiguration"] = {}
        with patch.object(setup, "request", side_effect=[config(), after, after]), \
             self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")

    def test_http_failures_do_not_retry_writes(self):
        for code in (401, 403, 404, 409, 429, 500):
            with self.subTest(code=code), patch.object(setup, "request", side_effect=setup.ApiError(code, "failure")) as req:
                with self.assertRaises(setup.ApiError):
                    setup.bind_bot(BLUEPRINT, BLUEPRINT, "private-token")
                self.assertEqual(req.call_count, 1)

    def test_env_parser_never_executes_values(self):
        output = 'ACCOUNT="example"\nPROJECT="example-project"\nSECRET="do not keep"\nAGENT="$(touch ignored)"\n'
        with patch.object(setup, "command", return_value=output):
            values = setup.env_values("example")
        self.assertEqual(values["AGENT"], "$(touch ignored)")
        self.assertNotIn("SECRET", values)

    def test_deployment_rejects_changed_target_before_api(self):
        data = state(); data["target"] = {"AGENT": "different"}
        values = dict(ACCOUNT="example", PROJECT="example-project", AGENT="example-agent",
                      BLUEPRINT_ID=BLUEPRINT, BOT_SERVICE_ID="example", TENANT_ID=TENANT, SUBSCRIPTION_ID=TENANT)
        with patch.object(setup, "env_values", return_value=values), patch.object(setup, "request") as req, \
             self.assertRaisesRegex(RuntimeError, "IDs changed"):
            setup.deployment(data)
        req.assert_not_called()

    def test_wrong_tenant_rejected_before_api(self):
        with patch.object(setup, "env_values", return_value={"TENANT_ID": USER}), \
             patch.object(setup, "request") as req, self.assertRaisesRegex(RuntimeError, "do not match"):
            setup.deployment(state())
        req.assert_not_called()

    def test_adopt_existing_resolves_live_bot_without_writes(self):
        data = state()
        values = dict(ACCOUNT="example", PROJECT="example-project", AGENT="example-agent",
                      BLUEPRINT_ID=USER, BOT_SERVICE_ID="old", TENANT_ID=TENANT, SUBSCRIPTION_ID=TENANT)
        bot_id = f"/subscriptions/{TENANT}/resourceGroups/example/providers/Microsoft.BotService/botServices/example"
        bot = {"id": bot_id, "properties": {"msaAppId": BLUEPRINT, "msaAppTenantId": TENANT,
               "endpoint": "https://example.services.ai.azure.com/api/projects/example-project/agents/example-agent/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"}}
        with patch.object(setup, "env_values", return_value=values), \
             patch.object(setup, "token", return_value="private"), patch.object(setup, "confirm"), \
             patch.object(setup, "request", return_value={"blueprint": {"client_id": BLUEPRINT}}) as req, \
             patch.object(setup, "command", side_effect=[json.dumps([{"id": bot_id}]), json.dumps(bot)]) as command:
            setup.adopt_existing(data)
        self.assertEqual(data["phase"], "awaiting_approval")
        self.assertTrue(data["existing_deployment"])
        self.assertEqual(data["target"]["BLUEPRINT_ID"], BLUEPRINT)
        self.assertEqual(data["target"]["BOT_SERVICE_ID"], bot_id)
        self.assertTrue(all(len(c.args) == 2 for c in req.call_args_list))
        self.assertTrue(all(c.args[2] in {"list", "show"} for c in command.call_args_list))

    def test_existing_adoption_rejects_wrong_tenant(self):
        with patch.object(setup, "env_values", return_value={"TENANT_ID": USER}), \
             patch.object(setup, "request") as req, self.assertRaisesRegex(RuntimeError, "does not match"):
            setup.adopt_existing(state())
        req.assert_not_called()

    def test_bot_endpoint_must_match_hosted_agent(self):
        values = dict(ACCOUNT="example", PROJECT="example-project", AGENT="example-agent",
                      BLUEPRINT_ID=BLUEPRINT, BOT_SERVICE_ID="example", TENANT_ID=TENANT, SUBSCRIPTION_ID=TENANT)
        endpoint = ("https://example.services.ai.azure.com/api/projects/example-project/agents/"
                    "example-agent/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview")
        bot = {"type": "Microsoft.BotService/botServices", "id": f"/subscriptions/{TENANT}/resourceGroups/example",
               "properties": {"msaAppId": BLUEPRINT, "msaAppTenantId": TENANT, "endpoint": endpoint}}
        for actual in (endpoint, endpoint.replace("example-agent", "different-agent"),
                       endpoint.replace("https://", "http://"), endpoint + "&api-version=other", ""):
            with self.subTest(endpoint=actual):
                bot["properties"]["endpoint"] = actual
                with patch.object(setup, "env_values", return_value=values), \
                     patch.object(setup, "token", return_value="admin"), \
                     patch.object(setup, "request", return_value={"blueprint": {"client_id": BLUEPRINT}}), \
                     patch.object(setup, "command", return_value=json.dumps(bot)):
                    if actual == endpoint:
                        setup.deployment(state())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "endpoint does not target"):
                            setup.deployment(state())

    def test_untrusted_url_refuses_token(self):
        for url in ("http://graph.microsoft.com", "https://example.com", "https://graph.microsoft.com@example.com"):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, "Unexpected API"):
                setup.request(url, "private-token")

    def client_fixture(self):
        graph = {"id": TENANT, "oauth2PermissionScopes": [{"id": BLUEPRINT, "value": setup.SCOPE, "isEnabled": True}]}
        app = {"id": APP, "appId": APP, "signInAudience": "AzureADMyOrg", "isFallbackPublicClient": True,
               "publicClient": {"redirectUris": ["http://localhost"]}, "requiredResourceAccess":
               [{"resourceAppId": setup.GRAPH_APP, "resourceAccess": [{"id": BLUEPRINT, "type": "Scope"}]}]}
        principal = {"id": APP, "accountEnabled": True, "appRoleAssignmentRequired": False}
        grant = {"id": "grant", "consentType": "Principal", "principalId": USER,
                 "resourceId": TENANT, "scope": setup.SCOPE}
        return graph, app, principal, grant

    def test_existing_setup_client_reused_without_writes(self):
        graph, app, principal, grant = self.client_fixture()
        with patch.object(setup, "request", side_effect=[{"value": [v]} for v in (graph, app, principal, grant)]) as req:
            self.assertEqual(setup.setup_client(state(), "admin", USER), APP)
        self.assertTrue(all(c.args[2] == "GET" for c in req.call_args_list))

    def test_new_client_only_delegated_single_admin_consent(self):
        graph, app, principal, grant = self.client_fixture()
        answers = [{"value": [graph]}, {"value": []}, app, {"value": []}, principal, {"value": []}, grant]
        with patch.object(setup, "request", side_effect=answers) as req:
            setup.setup_client(state(), "admin", USER)
        posts = [c for c in req.call_args_list if c.args[2] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0].args[3]["requiredResourceAccess"][0]["resourceAccess"][0]["type"], "Scope")
        self.assertEqual(posts[-1].args[3]["consentType"], "Principal")
        self.assertEqual(posts[-1].args[3]["principalId"], USER)
        self.assertEqual(posts[-1].args[3]["scope"], setup.SCOPE)

    def test_known_creation_rejection_can_resume(self):
        graph, *_ = self.client_fixture(); data = state()
        with patch.object(setup, "request", side_effect=[{"value": [graph]}, {"value": []}, setup.ApiError(403, "denied")]), \
             self.assertRaises(setup.ApiError):
            setup.setup_client(data, "admin", USER)
        self.assertNotIn("client_creation_started", setup.load())

    def test_unknown_creation_outcome_never_duplicates(self):
        graph, *_ = self.client_fixture(); data = state(); data["client_creation_started"] = True
        with patch.object(setup, "request", side_effect=[{"value": [graph]}, {"value": []}]) as req, \
             self.assertRaisesRegex(RuntimeError, "unknown outcome"):
            setup.setup_client(data, "admin", USER)
        self.assertEqual(req.call_count, 2)

    def test_setup_help_documents_continue_without_azure(self):
        result = subprocess.run(["bash", str(ROOT / "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--continue", result.stdout)


class BrowserAuthenticationTests(unittest.TestCase):
    """Exercise the real loopback HTTP handler; Entra/MSAL exchange is mocked, never contacted."""

    def sign_in(self, result, *, invalid_first=False, exchange_error=False, full_pipeline=False):
        app = Mock()
        app.initiate_auth_code_flow.return_value = {
            "state": "expected-state", "auth_uri": "https://login.microsoftonline.com/example/authorize"}
        app.acquire_token_by_auth_code_flow.return_value = result
        if exchange_error:
            app.acquire_token_by_auth_code_flow.side_effect = ValueError("private-callback-do-not-print")
        msal = types.SimpleNamespace(PublicClientApplication=Mock(return_value=app))
        self.app, self.msal = app, msal
        responses, errors, threads = [], [], []
        original_server = setup.http.server.HTTPServer
        clock = time.monotonic

        def make_server(address, handler):
            server = original_server(address, handler)
            self.assertEqual(address, ("127.0.0.1", 0))

            def browser():
                paths = ["/start"]
                if invalid_first:
                    paths += ["/?state=wrong&code=not-valid", "/?state=expected-state&code=one&code=two",
                              "/?state=expected-state&code=one&error=access_denied"]
                paths.append("/?state=expected-state&code=simulated-code")
                try:
                    for path in paths:
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                        try:
                            connection.request("GET", path)
                            response = connection.getresponse()
                            responses.append((response.status, response.getheader("Location")))
                            response.read()
                        finally:
                            connection.close()
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=browser, daemon=True)
            threads.append(thread)
            thread.start()
            return server

        self.output = io.StringIO()
        with patch.dict("sys.modules", {"msal": msal}), \
             patch.object(setup.http.server, "HTTPServer", side_effect=make_server), \
             patch.object(setup.time, "monotonic", side_effect=lambda: clock() * 100), \
             patch("sys.stdout", self.output), patch("sys.stderr", self.output):
            try:
                if full_pipeline:
                    data = state("configure_bot"); data["target"] = {"BLUEPRINT_ID": BLUEPRINT}
                    with tempfile.TemporaryDirectory() as folder, \
                         patch.object(setup, "STATE", Path(folder) / "checkpoint.json"), \
                         patch.object(setup, "deployment", return_value={"agent_endpoint": {"publish_approval_status": "approved"}}), \
                         patch.object(setup, "ensure_msal"), patch.object(setup, "pause"), \
                         patch.object(setup, "token", return_value="admin"), \
                         patch.object(setup, "request", return_value={"id": USER}), \
                         patch.object(setup, "setup_client", return_value=APP), \
                         patch.object(setup, "bind_bot") as bind:
                        try:
                            setup.run(data)
                        finally:
                            bind.assert_not_called()
                            self.assertEqual(data["phase"], "configure_bot")
                else:
                    return setup.browser_token(TENANT, APP, USER)
            finally:
                for thread in threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                self.assertFalse(errors, errors)
                self.assertEqual(responses[0], (302, app.initiate_auth_code_flow.return_value["auth_uri"]))
                self.assertEqual([r[0] for r in responses[1:]], [400, 400, 400, 200] if invalid_first else [200])

    def test_browser_callback_scope_authority_and_no_persistent_cache(self):
        result = {"access_token": "simulated-access", "id_token_claims": {"tid": TENANT, "oid": USER}}
        self.assertEqual(self.sign_in(result, invalid_first=True), "simulated-access")
        self.msal.PublicClientApplication.assert_called_once_with(
            APP, authority=f"https://login.microsoftonline.com/{TENANT}", exclude_scopes=["offline_access"])
        call = self.app.initiate_auth_code_flow.call_args
        self.assertEqual(call.args[0], [setup.GRAPH + "/" + setup.SCOPE])
        self.assertTrue(call.kwargs["redirect_uri"].startswith("http://localhost:"))
        self.assertEqual(call.kwargs["prompt"], "select_account")
        self.app.acquire_token_by_auth_code_flow.assert_called_once_with(
            self.app.initiate_auth_code_flow.return_value, {"state": "expected-state", "code": "simulated-code"})
        self.app.initiate_device_flow.assert_not_called()
        self.assertNotIn("simulated-access", self.output.getvalue())
        self.assertNotIn("simulated-code", self.output.getvalue())

    def test_wrong_tenant_user_or_missing_claims_never_reaches_binding(self):
        for claims in ({"tid": APP, "oid": USER}, {"tid": TENANT, "oid": APP}, {}):
            with self.subTest(claims=claims), self.assertRaisesRegex(RuntimeError, "different tenant/administrator"):
                self.sign_in({"access_token": "simulated-access", "id_token_claims": claims}, full_pipeline=True)

    def test_auth_failure_safe_diagnostics_and_no_binding(self):
        with self.assertRaisesRegex(RuntimeError, "530035") as error:
            self.sign_in({"error": "access_denied", "error_codes": [530035], "correlation_id": APP,
                          "error_description": "sensitive-description"}, full_pipeline=True)
        self.assertNotIn("sensitive-description", str(error.exception))
        self.assertIn(APP, str(error.exception))

    def test_msal_validation_error_never_reaches_binding_or_logs_callback(self):
        with self.assertRaisesRegex(RuntimeError, "response validation") as error:
            self.sign_in({}, exchange_error=True, full_pipeline=True)
        self.assertNotIn("private-callback", str(error.exception))

    def test_expired_browser_link_does_not_exchange_token(self):
        app = Mock()
        app.initiate_auth_code_flow.return_value = {"state": "state", "auth_uri": "https://example.com"}
        with patch.dict("sys.modules", {"msal": types.SimpleNamespace(PublicClientApplication=Mock(return_value=app))}), \
             patch.object(setup.time, "monotonic", side_effect=[0, 901]), patch("sys.stdout", io.StringIO()), \
             self.assertRaisesRegex(RuntimeError, "Sign-in timed out"):
            setup.browser_token(TENANT, APP, USER)
        app.acquire_token_by_auth_code_flow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
