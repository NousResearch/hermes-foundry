"""Execute the actual ARM deployment script with mocked Azure HTTP responses."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = '33333333-3333-3333-3333-333333333333'


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell required for deployment-script behavior tests')
class BlueprintIdentityReuseTests(unittest.TestCase):
    def execute(self, scenario):
        source = (ROOT / 'infra/modules/managed-agent-blueprint.bicep').read_text()
        script = source.split("scriptContent: '''", 1)[1].split("'''", 1)[0]
        harness = r'''
param([string]$ScriptPath, [string]$Scenario)
$global:calls = @()
class MockHttpException : System.Exception {
  [object] $Response
  MockHttpException([int]$status) { $this.Response = @{StatusCode = $status} }
}
function Connect-AzAccount { }
function Get-AzAccessToken { return @{Token = (ConvertTo-SecureString 'synthetic' -AsPlainText -Force)} }
function Invoke-RestMethod {
  param($Uri, $Method, $Headers)
  $global:calls += @{method = [string]$Method; uri = $Uri}
  if ($Method -eq 'Get') {
    switch ($Scenario) {
      'existing' { return @{blueprint = @{client_id = '33333333-3333-3333-3333-333333333333'}} }
      'invalid' { return @{blueprint = @{client_id = 'invalid'}} }
      '404' { throw [MockHttpException]::new(404) }
      '403' { throw [MockHttpException]::new(403) }
      '500' { throw [MockHttpException]::new(500) }
      'network' { throw 'Connection failed' }
    }
  }
  return @{agentIdentityBlueprint = @{clientId = '11111111-1111-1111-1111-111111111111'}}
}
$errorMessage = $null
try {
  . $ScriptPath -ProjectEndpoint 'https://example.services.ai.azure.com/api/projects/project' -BlueprintName 'planned-maib' -AgentName 'hermes-agent'
} catch { $errorMessage = $_.Exception.Message }
@{calls = $global:calls; outputs = $DeploymentScriptOutputs; error = $errorMessage} | ConvertTo-Json -Depth 8 -Compress
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'script.ps1').write_text(script)
            (path / 'harness.ps1').write_text(harness)
            result = subprocess.run(['pwsh', '-NoProfile', '-File', str(path / 'harness.ps1'),
                                     str(path / 'script.ps1'), scenario], capture_output=True, text=True, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_existing_agent_reuses_identity_without_blueprint_write(self):
        result = self.execute('existing')
        self.assertIsNone(result['error'])
        self.assertEqual(result['outputs']['blueprintClientId'], BLUEPRINT)
        self.assertEqual([c['method'] for c in result['calls']], ['Get'])
        self.assertTrue(result['calls'][0]['uri'].endswith('/agents/hermes-agent?api-version=v1'))

    def test_only_404_creates_blueprint(self):
        result = self.execute('404')
        self.assertIsNone(result['error'])
        self.assertEqual([c['method'] for c in result['calls']], ['Get', 'Put'])
        self.assertEqual(result['outputs']['blueprintClientId'], '11111111-1111-1111-1111-111111111111')

    def test_invalid_identity_and_lookup_failures_never_create_blueprint(self):
        for scenario in ['invalid', '403', '500', 'network']:
            with self.subTest(scenario=scenario):
                result = self.execute(scenario)
                self.assertIsNotNone(result['error'])
                self.assertIsNone(result['outputs'])
                self.assertEqual([c['method'] for c in result['calls']], ['Get'])
