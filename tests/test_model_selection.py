import pathlib
import unittest


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).parents[1]
        self.policy = (
            self.root / "src" / "edge" / "Services" / "ModelPolicyService.cs"
        ).read_text()
        self.router = (
            self.root
            / "src"
            / "edge"
            / "AgentLogic"
            / "HermesRouter"
            / "HermesRouterAgentLogicService.cs"
        ).read_text()
        self.routines = (
            self.root
            / "src"
            / "edge"
            / "AgentLogic"
            / "ResponsesProtocol"
            / "HermesResponseHandler.cs"
        ).read_text()

    def test_discovery_uses_documented_project_api_and_audience(self):
        self.assertIn('"https://ai.azure.com/.default"', self.policy)
        self.assertIn('deployments?api-version=v1', self.policy)
        self.assertIn('TryGetProperty("nextLink"', self.policy)

    def test_only_chat_model_deployments_are_selectable(self):
        self.assertIn('type.Equals("ModelDeployment"', self.policy)
        self.assertIn('IsTruthy(capabilities, "chat_completion")', self.policy)
        self.assertIn('IsTruthy(capabilities, "responses")', self.policy)
        self.assertIn("ApplyAllowList(discovered)", self.policy)

    def test_command_choices_are_checked_against_inventory(self):
        self.assertIn('text.StartsWith("/model "', self.policy)
        self.assertIn("inventory.Models.FirstOrDefault", self.policy)
        self.assertIn("SetPreferenceAsync(preferenceKey, requested", self.policy)
        self.assertIn('argument.Equals("auto"', self.policy)

    def test_teams_command_is_handled_before_hermes_turn(self):
        command = self.router.index("HandleCommandAsync(")
        invoke = self.router.index("var stagedFiles = new List<StagedInputFile>()")
        self.assertLess(command, invoke)
        self.assertIn("ReplyToControlCommandAsync", self.router)

    def test_turn_and_routine_send_explicit_provider_and_model(self):
        self.assertIn("model = modelSelection?.Model", self.router)
        self.assertIn('provider = modelSelection?.Provider', self.router)
        self.assertIn("model = modelSelection.Model", self.routines)
        self.assertIn('provider = modelSelection.Provider', self.routines)

    def test_release_captures_inventory_and_priority(self):
        release = (self.root / "new-version.sh").read_text()
        self.assertIn('deployments?api-version=v1', release)
        self.assertIn('env["MODEL_DEPLOYMENTS_SNAPSHOT"]', release)
        self.assertIn('env["MODEL_PRIORITY"]', release)
        self.assertIn('MODEL_ALLOWED_DEPLOYMENTS', release)

    def test_empty_admin_inventory_fails_closed(self):
        self.assertIn('return available.FirstOrDefault() ?? "";', self.policy)
        self.assertIn("if (!modelSelection.IsAvailable)", self.router)
        self.assertIn("if (!modelSelection.IsAvailable)", self.routines)


if __name__ == "__main__":
    unittest.main()
