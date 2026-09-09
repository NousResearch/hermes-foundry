targetScope = 'resourceGroup'

@description('User-assigned identity resource ID used by the deployment script.')
param deploymentIdentityResourceId string

@description('Foundry project data-plane endpoint.')
param projectEndpoint string

@description('Stable name of the Managed Agent Identity Blueprint.')
param blueprintName string

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
    arguments: '-ProjectEndpoint "${projectEndpoint}" -BlueprintName "${blueprintName}"'
    scriptContent: '''
param(
  [Parameter(Mandatory = $true)][string] $ProjectEndpoint,
  [Parameter(Mandatory = $true)][string] $BlueprintName
)
$ErrorActionPreference = "Stop"
Connect-AzAccount -Identity | Out-Null
$tokenResponse = Get-AzAccessToken -ResourceUrl "https://ai.azure.com"
$token = $tokenResponse.Token | ConvertFrom-SecureString -AsPlainText
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
