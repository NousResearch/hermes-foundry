targetScope = 'resourceGroup'

@description('User-assigned identity used only to create the Foundry Managed Agent Identity Blueprint.')
param identityName string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: resourceGroup().location
}

var contributorRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b24988ac-6180-42a0-ab88-20f7382dd24c'
)
var cognitiveServicesUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
)

resource contributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, contributorRole)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: contributorRole
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryDataAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, identity.id, cognitiveServicesUserRole)
  scope: resourceGroup()
  properties: {
    roleDefinitionId: cognitiveServicesUserRole
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output resourceId string = identity.id
