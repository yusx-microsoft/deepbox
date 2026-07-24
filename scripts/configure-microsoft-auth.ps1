<#
.SYNOPSIS
  Configure secretless, tenant-restricted Microsoft sign-in for an existing Deepbox web app.

.DESCRIPTION
  Creates or reuses the app registration's home-tenant service principal and a
  user-assigned managed identity, assigns the identity to the web app, creates
  the Entra federated identity credential, writes the reserved Easy Auth
  managed-identity setting, and deploys authsettingsV2.

  No client secret or certificate is created. The script deliberately does not
  change DEEPBOX_AUTH_MODE; keep local mode until the first interactive sign-in.

.EXAMPLE
  ./scripts/configure-microsoft-auth.ps1 `
      -SubscriptionId 00000000-0000-0000-0000-000000000000 `
      -ResourceGroup deepbox-rg `
      -WebAppName my-deepbox `
      -TenantId 00000000-0000-0000-0000-000000000000 `
      -ClientId 00000000-0000-0000-0000-000000000000 `
      -ApplicationObjectId 00000000-0000-0000-0000-000000000000
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][guid]$SubscriptionId,
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$WebAppName,
    [Parameter(Mandatory = $true)][guid]$TenantId,
    [Parameter(Mandatory = $true)][guid]$ClientId,
    [Parameter(Mandatory = $true)][guid]$ApplicationObjectId,
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$ManagedIdentityName,
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$FederatedCredentialName = 'deepbox-easy-auth-uami-fic'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$managedIdentitySettingName = 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'

if (-not $ManagedIdentityName) {
    $ManagedIdentityName = "$WebAppName-auth"
}

function Assert-AzSucceeded([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed (Azure CLI exit code $LASTEXITCODE)."
    }
}

Write-Host "Selecting subscription '$SubscriptionId'..."
& az account set --subscription $SubscriptionId
Assert-AzSucceeded 'Selecting the Azure subscription'

$account = & az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json |
    ConvertFrom-Json
Assert-AzSucceeded 'Reading the Azure account'
if ([guid]$account.tenantId -ne $TenantId) {
    throw "Azure CLI is signed in to tenant '$($account.tenantId)', expected '$TenantId'."
}

$webApp = & az webapp show `
    --resource-group $ResourceGroup `
    --name $WebAppName `
    --subscription $SubscriptionId `
    --query '{id:id,location:location}' `
    --output json | ConvertFrom-Json
Assert-AzSucceeded 'Reading the App Service'

$application = & az ad app show `
    --id $ApplicationObjectId `
    --query '{appId:appId,signInAudience:signInAudience}' `
    --output json | ConvertFrom-Json
Assert-AzSucceeded 'Reading the Entra app registration'
if ([guid]$application.appId -ne $ClientId) {
    throw "App registration object '$ApplicationObjectId' has client ID '$($application.appId)', expected '$ClientId'."
}
if ($application.signInAudience -ne 'AzureADMyOrg') {
    throw "App registration must be single-tenant (AzureADMyOrg), found '$($application.signInAudience)'."
}

$servicePrincipalJson = & az ad sp show --id $ClientId --output json 2>$null
if ($LASTEXITCODE -eq 0) {
    $servicePrincipal = $servicePrincipalJson | ConvertFrom-Json
    Write-Host "Reusing home-tenant service principal '$($servicePrincipal.id)'."
}
else {
    Write-Host 'Creating the app registration home-tenant service principal...'
    $servicePrincipal = & az ad sp create --id $ClientId --output json |
        ConvertFrom-Json
    Assert-AzSucceeded 'Creating the home-tenant service principal'
}
if ([guid]$servicePrincipal.appId -ne $ClientId) {
    throw "Service principal '$($servicePrincipal.id)' has app ID '$($servicePrincipal.appId)', expected '$ClientId'."
}
if (-not $servicePrincipal.accountEnabled) {
    throw "Service principal '$($servicePrincipal.id)' is disabled."
}

$identityJson = & az identity show `
    --resource-group $ResourceGroup `
    --name $ManagedIdentityName `
    --subscription $SubscriptionId `
    --output json 2>$null
if ($LASTEXITCODE -eq 0) {
    $identity = $identityJson | ConvertFrom-Json
    Write-Host "Reusing managed identity '$ManagedIdentityName'."
}
else {
    Write-Host "Creating managed identity '$ManagedIdentityName'..."
    $identity = & az identity create `
        --resource-group $ResourceGroup `
        --name $ManagedIdentityName `
        --location $webApp.location `
        --subscription $SubscriptionId `
        --tags managedBy=deepbox purpose=easy-auth `
        --output json | ConvertFrom-Json
    Assert-AzSucceeded 'Creating the user-assigned managed identity'
}

Write-Host 'Assigning the managed identity to the web app...'
& az webapp identity assign `
    --resource-group $ResourceGroup `
    --name $WebAppName `
    --subscription $SubscriptionId `
    --identities $identity.id `
    --output none
Assert-AzSucceeded 'Assigning the managed identity to the App Service'

Write-Host "Setting reserved Easy Auth identity pointer '$managedIdentitySettingName'..."
& az webapp config appsettings set `
    --resource-group $ResourceGroup `
    --name $WebAppName `
    --subscription $SubscriptionId `
    --slot-settings "$managedIdentitySettingName=$($identity.clientId)" `
    --output none
Assert-AzSucceeded 'Writing the Easy Auth managed-identity app setting'

$expectedIssuer = "https://login.microsoftonline.com/$TenantId/v2.0"
$expectedAudience = 'api://AzureADTokenExchange'
$existingJson = & az ad app federated-credential list `
    --id $ApplicationObjectId `
    --query "[?name=='$FederatedCredentialName']" `
    --output json
Assert-AzSucceeded 'Reading federated identity credentials'
$existing = @($existingJson | ConvertFrom-Json)

if ($existing.Count -gt 1) {
    throw "More than one federated credential is named '$FederatedCredentialName'."
}
if ($existing.Count -eq 1) {
    $credential = $existing[0]
    $audiences = @($credential.audiences)
    if (
        $credential.issuer -ne $expectedIssuer -or
        $credential.subject -ne $identity.principalId -or
        $audiences.Count -ne 1 -or
        $audiences[0] -ne $expectedAudience
    ) {
        throw "Federated credential '$FederatedCredentialName' exists but does not match the managed identity."
    }
    Write-Host "Reusing federated credential '$FederatedCredentialName'."
}
else {
    Write-Host "Creating federated credential '$FederatedCredentialName'..."
    $ficPath = [System.IO.Path]::GetTempFileName()
    try {
        $fic = @{
            name = $FederatedCredentialName
            issuer = $expectedIssuer
            subject = $identity.principalId
            audiences = @($expectedAudience)
            description = 'Lets App Service Easy Auth authenticate without a client secret.'
        } | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText(
            $ficPath,
            $fic,
            [System.Text.UTF8Encoding]::new($false)
        )
        & az ad app federated-credential create `
            --id $ApplicationObjectId `
            --parameters "@$ficPath" `
            --output none
        Assert-AzSucceeded 'Creating the federated identity credential'
    }
    finally {
        Remove-Item $ficPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host 'Deploying Easy Auth v2 configuration...'
& az deployment group create `
    --resource-group $ResourceGroup `
    --subscription $SubscriptionId `
    --template-file (Join-Path $repoRoot 'infra/microsoft-auth.bicep') `
    --parameters webAppName=$WebAppName tenantId=$TenantId clientId=$ClientId `
    --query properties.provisioningState `
    --output tsv
Assert-AzSucceeded 'Deploying Easy Auth v2'

Write-Host "Microsoft platform authentication is configured for https://$WebAppName.azurewebsites.net."
Write-Host 'DEEPBOX_AUTH_MODE was not changed; use hybrid only when ready for interactive verification.'
