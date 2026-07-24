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

// The client secret is deliberately not an IaC parameter. Provision it as the
// MICROSOFT_PROVIDER_AUTHENTICATION_SECRET App Service setting before enabling
// Microsoft authentication in the Deepbox runtime configuration.
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
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
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
