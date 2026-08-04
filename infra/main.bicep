// Deepbox — Azure App Service (Linux) infrastructure.
//
// Provisions a single-instance Linux App Service (B1) running the Python 3.12
// control-plane server. TLS is terminated by the App Service front end, which
// forwards to the container's published port. SQLite + DVR recordings live on
// the persistent /home volume (WEBSITES_ENABLE_APP_SERVICE_STORAGE=true).
//
// Secrets are NEVER hardcoded here. DEEPBOX_SECRET is passed as a secure
// parameter (generate at deploy time or source from Key Vault) and stored as
// an app setting. Nothing in this file is committed with a real secret value.
//
// Inbound access is restricted to Microsoft corporate network service tags.
// The site is not reachable from the public internet: the default action is
// Deny and SCM/Kudu inherits the same rules. This satisfies the corp-tenant
// "no internet-exposed web apps" requirement enforced by Azure Policy.

@description('Globally-unique web app name (also the default *.azurewebsites.net host).')
param webAppName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('App Service Plan name.')
param appServicePlanName string = '${webAppName}-plan'

@description('Session-signing secret. Generate at deploy time; do not commit.')
@secure()
param deepboxSecret string

@description('Comma-separated allowed browser origins (must be HTTPS in production).')
param allowedOrigins string = 'https://${webAppName}.azurewebsites.net'

@description('Public URL browsers use to reach the app.')
param publicUrl string = 'https://${webAppName}.azurewebsites.net'

@description('Allow self-service registration. Keep false for production.')
param registrationEnabled bool = false

@description('Persistent data directory on the /home volume.')
param dataDir string = '/home/deepbox'

@description('Source Git commit embedded in this deployment for build provenance.')
param gitCommit string = 'unknown'

@description('Service tags allowed to reach the site. Public internet is always denied.')
param allowedServiceTags array = [
  'CorpNetPublic'
  'CorpNetSAW'
]

var linuxFxVersion = 'PYTHON|3.12'

// One Allow rule per approved service tag, then an explicit catch-all Deny.
var ipRules = [for (tag, i) in allowedServiceTags: {
  ipAddress: tag
  action: 'Allow'
  tag: 'ServiceTag'
  priority: 100 + (i * 10)
  name: 'Allow-${tag}'
}]

var denyAllRule = {
  ipAddress: 'Any'
  action: 'Deny'
  priority: 2147483647
  name: 'Deny-all'
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'B1'
    tier: 'Basic'
    capacity: 1
  }
  kind: 'linux'
  properties: {
    reserved: true // required for Linux
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: linuxFxVersion
      alwaysOn: true
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      webSocketsEnabled: true
      numberOfWorkers: 1
      healthCheckPath: '/api/ready'
      publicNetworkAccess: 'Enabled'
      ipSecurityRestrictions: concat(ipRules, [denyAllRule])
      ipSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictionsUseMain: true
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      // Oryx build during zip deploy installs the root requirements.txt.
      appCommandLine: 'bash $(ls -t /tmp/*/azure-startup.sh | head -n 1)'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'DEEPBOX_ENV', value: 'production' }
        { name: 'DEEPBOX_PLATFORM', value: 'azure-app-service' }
        { name: 'DEEPBOX_HOST', value: '0.0.0.0' }
        { name: 'DEEPBOX_PORT', value: '8000' }
        { name: 'DEEPBOX_FORWARDED_ALLOW_IPS', value: '*' }
        { name: 'DEEPBOX_SECRET', value: deepboxSecret }
        { name: 'DEEPBOX_DATABASE_URL', value: 'sqlite:///${dataDir}/deepbox.db' }
        { name: 'DEEPBOX_DATA_DIR', value: dataDir }
        { name: 'DEEPBOX_PUBLIC_URL', value: publicUrl }
        { name: 'DEEPBOX_ALLOWED_ORIGINS', value: allowedOrigins }
        { name: 'DEEPBOX_COOKIE_SECURE', value: 'true' }
        { name: 'DEEPBOX_COOKIE_SAMESITE', value: 'lax' }
        { name: 'DEEPBOX_REGISTRATION_ENABLED', value: string(registrationEnabled) }
        { name: 'DEEPBOX_GIT_COMMIT', value: gitCommit }
      ]
    }
  }
}

output defaultHostName string = site.properties.defaultHostName
output appServiceName string = site.name
