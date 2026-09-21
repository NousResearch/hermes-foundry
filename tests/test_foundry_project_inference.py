import importlib.util
import json
import pathlib
import sys
import types
import tempfile
import unittest
import urllib.parse
from unittest import mock


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        return lambda fn: fn


class _TokenResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"access_token": "test-token"}).encode()


def _load_sidecar():
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = _FakeFastMCP
    server = types.ModuleType("mcp.server")
    server.fastmcp = fastmcp
    mcp = types.ModuleType("mcp")
    mcp.server = server
    with mock.patch.dict(sys.modules, {
        "mcp": mcp,
        "mcp.server": server,
        "mcp.server.fastmcp": fastmcp,
    }):
        path = pathlib.Path(__file__).parents[1] / "src" / "sidecar" / "agent365_mcp.py"
        spec = importlib.util.spec_from_file_location("hfull_foundry_project_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class FoundryProjectInferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).parents[1]
        self.sidecar = _load_sidecar()

    def _token_request_resource(self, base_url):
        env = {
            "IDENTITY_ENDPOINT": "http://identity/token",
            "IDENTITY_HEADER": "header-secret",
            "AZURE_FOUNDRY_BASE_URL": base_url,
        }
        with mock.patch.dict(self.sidecar.os.environ, env, clear=False), \
             mock.patch.object(self.sidecar.urllib.request, "urlopen", return_value=_TokenResponse()) as open_url:
            self.assertEqual(self.sidecar._foundry_token(), "test-token")
        request = open_url.call_args.args[0]
        return urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)["resource"][0]

    def test_project_model_endpoint_uses_ai_azure_token_audience(self):
        resource = self._token_request_resource(
            "https://example.services.ai.azure.com/api/projects/project/openai/v1"
        )
        self.assertEqual(resource, "https://ai.azure.com")

    def test_portable_account_endpoint_retains_cognitive_services_audience(self):
        resource = self._token_request_resource(
            "https://example.cognitiveservices.azure.com/openai/v1"
        )
        self.assertEqual(resource, "https://cognitiveservices.azure.com")

    def test_primary_and_fallback_are_both_entra_authenticated(self):
        start = (self.root / "start.sh").read_text()
        self.assertIn('auth_mode: "entra_id"', start)
        self.assertIn(
            'FALLBACK_PROVIDER_YAML+="    auth_mode: \\"entra_id\\""',
            start,
        )

        dockerfile = (self.root / "Dockerfile").read_text()
        self.assertIn("apply_fallback_entra_patch.py", dockerfile)
        self.assertIn("apply_foundry_annotations_patch.py", dockerfile)

        patcher = (
            self.root / "src" / "hermes-patches" / "apply_fallback_entra_patch.py"
        ).read_text()
        self.assertIn("fallback_auth_mode=", patcher)
        self.assertIn("fb.get", patcher)
        self.assertIn("model_cfg[", patcher)
        self.assertIn("auth_mode", patcher)
        self.assertIn("preserving the active Entra token provider", patcher)

    def test_replayed_assistant_text_gets_required_annotations(self):
        patcher = (
            self.root / "src" / "hermes-patches" / "apply_foundry_annotations_patch.py"
        ).read_text()
        self.assertIn("codex_responses_adapter.py", patcher)
        self.assertIn('"annotations": []', patcher)
        self.assertIn('text_type == "output_text"', patcher)
        self.assertIn("expected one source match", patcher)

    def test_foundry_reasoning_id_survives_history_and_preflight(self):
        path = self.root / "src/hermes-patches/apply_foundry_annotations_patch.py"
        spec = importlib.util.spec_from_file_location("reasoning_patch", path)
        patcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patcher)
        source = '''def history(ri):
    return {k: v for k, v in ri.items()
                                if k not in ("id", "_issuer_kind")}
def preflight(items):
    result = []
    for item in items:
        if item["type"] == "reasoning":
            if item.get("encrypted_content"):
                item_id = item.get("id")
                reasoning_item = {"type": "reasoning", "encrypted_content": item["encrypted_content"]}
                summary = item.get("summary")
                reasoning_item["summary"] = summary or []
                result.append(reasoning_item)
    return result
'''
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "adapter.py"
            target.write_text(source)
            patcher.patch_reasoning(target)
            first = target.read_text()
            patcher.patch_reasoning(target)
            self.assertEqual(first, target.read_text())
            namespace = {}
            exec(compile(first, str(target), "exec"), namespace)
        original = {"type": "reasoning", "id": "rs_original", "encrypted_content": "opaque",
                    "summary": [], "_issuer_kind": "internal"}
        history = namespace["history"](original)
        self.assertNotIn("_issuer_kind", history)
        self.assertEqual("rs_original", namespace["preflight"]([history])[0]["id"])
        self.assertEqual([], namespace["preflight"]([{k:v for k,v in original.items() if k != "id"}]))

    def test_release_defaults_to_project_scoped_model_endpoint(self):
        release = (self.root / "new-version.sh").read_text()
        self.assertIn('PROJECT_MODEL_BASE_URL="${PROJECT_MODEL_BASE_URL:-${BASE}/openai/v1}"', release)
        self.assertIn('env["AZURE_FOUNDRY_BASE_URL"] = sys.argv[5]', release)

    def test_routine_requests_include_required_foundry_api_version(self):
        source = (
            self.root / "src" / "edge" / "Services" / "RoutineBindingService.cs"
        ).read_text()
        self.assertIn('private const string ApiVersion = "v1";', source)
        self.assertIn("api-version={Uri.EscapeDataString(ApiVersion)}", source)
        self.assertIn('TryAddWithoutValidation("Foundry-Features", PreviewHeader)', source)
        self.assertIn('root.TryGetProperty("data", out var data)', source)

    def test_routine_requests_forward_foundry_call_authority(self):
        source = (
            self.root / "src" / "edge" / "Services" / "RoutineBindingService.cs"
        ).read_text()
        self.assertIn('request?.Headers["x-agent-foundry-call-id"]', source)
        self.assertIn(
            'TryAddWithoutValidation("x-agent-foundry-call-id", foundryCallId)',
            source,
        )

    def test_routine_diagnostics_hash_identity_claims_and_never_log_bearer_token(self):
        source = (
            self.root / "src" / "edge" / "Services" / "RoutineBindingService.cs"
        ).read_text()
        self.assertIn("LogFoundryCaller(token.Token, foundryCallId)", source)
        self.assertIn('ClaimTag(claims, "oid")', source)
        self.assertIn('ClaimTag(claims, "appid")', source)
        self.assertIn('ClaimTag(claims, "azp")', source)
        self.assertNotIn("logger.LogInformation(accessToken", source)
        self.assertIn(
            '[JsonPropertyName("foundry_call_id")] public string FoundryCallId',
            source,
        )

    def test_code_progress_never_echoes_source(self):
        source = (
            self.root
            / "src"
            / "edge"
            / "AgentLogic"
            / "HermesRouter"
            / "HermesRouterAgentLogicService.cs"
        ).read_text()
        self.assertIn(
            '"run_code" or "execute_code" or "terminal" => "Running code"',
            source,
        )
        self.assertNotIn(
            '"run_code" or "execute_code" or "terminal" => WithPreview("Running code")',
            source,
        )

    def test_fired_routine_is_wrapped_as_immediate_execution(self):
        source = (
            self.root
            / "src"
            / "edge"
            / "AgentLogic"
            / "ResponsesProtocol"
            / "HermesResponseHandler.cs"
        ).read_text()
        self.assertIn("BuildRoutineExecutionPrompt(binding.Task)", source)
        self.assertIn("TRUSTED FOUNDRY ROUTINE EXECUTION", source)
        self.assertIn("Execute exactly one occurrence of the task NOW", source)
        self.assertIn("Do not call create_routine", source)


if __name__ == "__main__":
    unittest.main()
