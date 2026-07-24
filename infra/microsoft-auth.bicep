targetScope = 'resourceGroup'

@description('Name of the existing Deepbox App Service web app.')
param webAppName string

@description('Microsoft Entra tenant ID whose users may sign in.')
param tenantId string

@description('Application (client) ID of the single-tenant Entra app registration.')
param clientId string

resource webApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: webAppName
}

// Easy Auth treats this reserved App Service setting as a user-assigned managed
// identity client ID and redeems the app registration's federated credential
// without a client secret or uploaded certificate.
var federatedCredentialSettingName = 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'

resource easyAuth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: webApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
      redirectToProvider: 'azureActiveDirectory'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: clientId
          clientSecretSettingName: federatedCredentialSettingName
          openIdIssuer: uri(
            environment().authentication.loginEndpoint,
            '${tenantId}/v2.0'
          )
        }
        validation: {
          allowedAudiences: [
            clientId
            'api://${clientId}'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output authSettingsResourceId string = easyAuth.id
