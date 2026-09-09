@description('Globally unique Azure Bot Service resource name.')
param botName string

@description('User-facing bot display name.')
param displayName string

@description('Entra client ID of the Managed Agent Identity Blueprint.')
param blueprintClientId string

@description('Foundry hosted-agent Activity Protocol endpoint.')
param endpoint string

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  kind: 'azurebot'
  location: 'global'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: displayName
    endpoint: endpoint
    msaAppId: blueprintClientId
    msaAppTenantId: tenant().tenantId
    msaAppType: 'SingleTenant'
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2021-03-01' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
  }
}

output resourceId string = bot.id
