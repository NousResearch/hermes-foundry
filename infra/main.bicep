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

@description('Initial model deployed for Hermes. More compatible project deployments are discovered at runtime.')
param modelName string = 'gpt-5-mini'

@description('Version of the initial model deployment.')
param modelVersion string = '2025-08-07'

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

var acrPullRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var cognitiveServicesUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
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

output ACCOUNT string = account.name
output PROJECT string = project.name
output AGENT string = agentName
output REG string = registry.name
output TAG string = 'v1'
output PRIMARY_MODEL string = initialModel.name
output FALLBACK_MODEL string = initialModel.name
output AZURE_AI_PROJECT_ENDPOINT string = project.properties.endpoints['AI Foundry API']
output RESOURCE_GROUP string = resourceGroup().name
output SUBSCRIPTION_ID string = subscription().subscriptionId
output TENANT_ID string = tenant().tenantId
output LOCATION string = location
