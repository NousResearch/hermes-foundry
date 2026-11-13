targetScope = 'resourceGroup'

// Tenant-neutral Azure infrastructure for the private Hermes Autopilot blueprint. This follows the
// resource shape in Microsoft's Foundry Autopilot azd sample, while the post-provision hook builds
// the Hermes image and invokes this repository's guarded create/publish scripts.

@minLength(1)
@maxLength(32)
@description('Azure Developer CLI environment name.')
param environmentName string

@description('Azure region that supports Foundry hosted agents and the selected model.')
param location string

@description('Object ID of the tenant deployment administrator who must call the Foundry data plane during post-provisioning.')
param deploymentPrincipalId string

@description('Initial model deployed for Hermes. More compatible project deployments are discovered at runtime.')
param modelName string = 'gpt-6.1-sol'

@description('Version of the initial model deployment.')
param modelVersion string = '2026-09-29'

@minValue(1)
@maxValue(1000)
@description('Initial Global Standard model capacity in thousands of tokens per minute.')
param modelCapacity int = 5

@description('Tags applied to the resources created by this template.')
param tags object = {
  application: 'Hermes Agent on Foundry'
  managedBy: 'azd'
}

var uniqueSuffix = uniqueString(subscription().subscriptionId, resourceGroup().id, environmentName)
var accountName = 'hermes${uniqueSuffix}ai'
var projectName = 'hermes-${uniqueSuffix}-project'
var registryName = 'hermes${uniqueSuffix}acr'
var agentName = 'hermes-agent-on-foundry'
var blueprintName = '${agentName}-maib'
var botName = 'hermes-${uniqueSuffix}-bot'

resource account 'Microsoft.CognitiveServices/accounts@2025-09-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
  tags: tags
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2026-03-01' = {
  parent: account
  // Cognitive Services serializes child writes on the parent account. Wait for the
  // model deployment before creating/updating the project to avoid RequestConflict.
  dependsOn: [
    initialModel
  ]
  name: projectName
  location: location
  #disable-next-line BCP187
  kind: 'AIServices'
  #disable-next-line BCP187
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Hermes Agent on Foundry'
  }
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

resource initialModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

var foundryProjectEndpoint = project.properties.endpoints['AI Foundry API']

var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var cognitiveServicesUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
)
var foundryUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)

resource projectAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, project.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource projectModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, project.id, cognitiveServicesUserRole)
  scope: account
  properties: {
    roleDefinitionId: cognitiveServicesUserRole
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Azure Owner/Contributor is a management-plane role and does not grant the Foundry data actions
// used by the post-provision scripts. Assign Foundry User to the selected deployment administrator
// at this project only, matching Microsoft's hosted-agent prerequisite.
resource deploymentOperatorFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, deploymentPrincipalId, foundryUserRole)
  scope: project
  properties: {
    roleDefinitionId: foundryUserRole
    principalId: deploymentPrincipalId
    principalType: 'User'
  }
}

// Match the proven hfull construction: create the MAIB explicitly before the hosted-agent version,
// rather than relying on an implicitly generated blueprint.
module deploymentScriptIdentity 'modules/deployment-script-identity.bicep' = {
  name: 'agent-blueprint-deployment-identity'
  params: {
    identityName: 'hermes-${uniqueSuffix}-blueprint-deployer'
  }
}

module managedAgentBlueprint 'modules/managed-agent-blueprint.bicep' = {
  name: 'managed-agent-identity-blueprint'
  params: {
    deploymentIdentityResourceId: deploymentScriptIdentity.outputs.resourceId
    projectEndpoint: foundryProjectEndpoint
    blueprintName: blueprintName
    agentName: agentName
  }
  dependsOn: [
    deploymentOperatorFoundryUser
    projectModelAccess
  ]
}

module botService 'modules/bot-service.bicep' = {
  name: 'hermes-bot-service'
  params: {
    botName: botName
    displayName: 'Hermes Agent on Foundry'
    blueprintClientId: managedAgentBlueprint.outputs.clientId
    endpoint: '${foundryProjectEndpoint}/agents/${agentName}/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview'
  }
}

output ACCOUNT string = account.name
output PROJECT string = project.name
output AGENT string = agentName
output REG string = registry.name
output TAG string = 'v1'
output PRIMARY_MODEL string = initialModel.name
output FALLBACK_MODEL string = initialModel.name
output AZURE_AI_PROJECT_ENDPOINT string = foundryProjectEndpoint
output RESOURCE_GROUP string = resourceGroup().name
output SUBSCRIPTION_ID string = subscription().subscriptionId
output TENANT_ID string = tenant().tenantId
output LOCATION string = location
output BLUEPRINT_ID string = managedAgentBlueprint.outputs.clientId
output MAIB_NAME string = blueprintName
output BOT_SERVICE_ID string = botService.outputs.resourceId
