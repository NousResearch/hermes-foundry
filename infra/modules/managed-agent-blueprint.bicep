targetScope = 'resourceGroup'

@description('User-assigned identity resource ID used by the deployment script.')
param deploymentIdentityResourceId string

@description('Foundry project data-plane endpoint.')
param projectEndpoint string

@description('Stable name of the Managed Agent Identity Blueprint.')
param blueprintName string

@description('Hosted agent whose existing identity must be preserved on reinstall.')
param agentName string

resource createBlueprint 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'create-${blueprintName}'
  location: resourceGroup().location
  kind: 'AzurePowerShell'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${deploymentIdentityResourceId}': {}
    }
  }
  properties: {
    azPowerShellVersion: '11.5'
    timeout: 'PT15M'
    retentionInterval: 'P1D'
    arguments: '-ProjectEndpoint "${projectEndpoint}" -BlueprintName "${blueprintName}" -AgentName "${agentName}"'
    scriptContent: '''
param(
  [Parameter(Mandatory = $true)][string] $ProjectEndpoint,
  [Parameter(Mandatory = $true)][string] $BlueprintName,
  [Parameter(Mandatory = $true)][string] $AgentName
)
$ErrorActionPreference = "Stop"
Connect-AzAccount -Identity | Out-Null
$tokenResponse = Get-AzAccessToken -ResourceUrl "https://ai.azure.com"
$token = $tokenResponse.Token | ConvertFrom-SecureString -AsPlainText
# Reinstalls must retain the identity already bound to this exact hosted agent.
# Only a genuine 404 permits creating a blueprint; access/service failures must stop.
$agentUri = "$ProjectEndpoint/agents/$AgentName`?api-version=v1"
$existingAgent = $null
try {
  $existingAgent = Invoke-RestMethod -Uri $agentUri -Method Get -Headers @{
    Authorization = "Bearer $token"
    Accept = "application/json"
  }
} catch {
  if ($null -eq $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 404) {
    throw "Unable to verify the existing hosted agent. Refusing to create another identity blueprint."
  }
}
if ($null -ne $existingAgent) {
  $existingClientId = [string]$existingAgent.blueprint.client_id
  $parsedClientId = [guid]::Empty
  if (-not [guid]::TryParse($existingClientId, [ref]$parsedClientId) -or $parsedClientId -eq [guid]::Empty) {
    throw "Existing hosted agent has no valid identity blueprint client ID."
  }
  $DeploymentScriptOutputs = @{ blueprintClientId = $parsedClientId.ToString() }
  Write-Output "Reusing the existing hosted agent identity blueprint."
  return
}
$uri = "$ProjectEndpoint/managedagentidentityblueprints/$BlueprintName`?api-version=2025-11-15-preview"
$response = Invoke-RestMethod -Uri $uri -Method Put -Headers @{
  Authorization = "Bearer $token"
  Accept = "application/json"
  "Content-Type" = "application/json"
}
$clientId = $response.agentIdentityBlueprint.clientId
if ([string]::IsNullOrWhiteSpace($clientId)) {
  throw "Foundry returned no Agent Identity Blueprint client ID."
}
$DeploymentScriptOutputs = @{ blueprintClientId = $clientId }
'''
  }
}

output clientId string = createBlueprint.properties.outputs.blueprintClientId
