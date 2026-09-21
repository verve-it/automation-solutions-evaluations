/*
  main.bicep — everything the stubbed-tool replay server needs to exist.

  The gate replays an agent against recorded tool responses. Foundry calls the
  replay server, so the server has to be reachable from Azure; that is the one
  part of the gate a checkout cannot provide and this file is the answer to it.

  What it creates
    storage account        deployment package + per-session replay state
    log analytics + app insights
    Flex Consumption plan  (FC1)
    function app           Linux, Python, running server.py as a custom handler
    user-assigned identity so the app reads its own package and state with no
                           connection string anywhere

  Deploy:
    az deployment group create -g <rg> -f main.bicep -p main.bicepparam

  Then publish the code -- this template provisions, it does not deploy the
  package. ../deploy.sh does both in order.
*/

@description('Short name that every resource is derived from. Lower case letters and digits.')
@minLength(3)
@maxLength(14)
param name string

@description('Region. Flex Consumption is not available everywhere; az functionapp list-flexconsumption-locations shows where it is.')
param location string = resourceGroup().location

@description('Python version the custom handler runs under. The handler is stdlib only, so this is about the interpreter in the image.')
@allowed(['3.11', '3.12'])
param pythonVersion string = '3.12'

@description('Flex Consumption will not accept a ceiling below 40. Replay state lives in blob storage precisely so that this does not have to be 1 -- see replay/state_store.py.')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@allowed([2048, 4096])
param instanceMemoryMB int = 2048

@description('Bearer token the replay server requires. Generate one per environment; it is the only thing in front of the cassettes, which carry ticket and company identifiers.')
@secure()
param replayToken string

@description('Cassette id used when a request does not name one in its URL. Blank means the package must contain exactly one cassette.')
param defaultCassette string = ''

@description('What to do when the agent calls something more times than it was recorded. repeat is right for a re-read; diverge is stricter.')
@allowed(['repeat', 'diverge'])
param onExhausted string = 'repeat'

/*
  How the function app authenticates to storage.

  identity          the managed identity, with a role assignment. No key
                    exists to leak. Needs Microsoft.Authorization/
                    roleAssignments/write on the resource group AT DEPLOY
                    TIME -- that is User Access Administrator or Owner, which
                    Contributor does not include.

  connectionString  a storage account key in app settings. Needs nothing
                    beyond Contributor. The key is a real secret sitting in
                    configuration, readable by anyone who can read the app's
                    settings, and it does not rotate on its own.

  Start on connectionString if you cannot assign roles, then have someone who
  can run infra/rbac.bicep and redeploy with storageAuth=identity. Nothing
  else about the app changes.
*/
@allowed(['identity', 'connectionString'])
param storageAuth string = 'identity'

param tags object = {
  workload: 'agent-eval'
  component: 'replay-mcp'
  data: 'recorded-tool-responses'
}

var suffix = uniqueString(resourceGroup().id, name)
var storageName = toLower('st${take(replace(name, '-', ''), 11)}${take(suffix, 8)}')
var functionAppName = '${name}-replay-${take(suffix, 6)}'
var deploymentContainer = 'deploymentpackage'
var stateContainer = 'replay-state'

// Storage Blob Data Owner. Owner rather than Contributor because the app
// creates the state container on first use, and because the Flex deployment
// container is managed by the platform through this same identity.
var blobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

var useIdentity = storageAuth == 'identity'

// Evaluated whichever branch is taken, so it must stay valid in both. listKeys
// succeeds even when allowSharedKeyAccess is false -- the key exists, it just
// will not authenticate -- so this is safe under storageAuth=identity, where
// nothing reads it.
var storageKey = storage.listKeys().keys[0].value
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storageKey};EndpointSuffix=${environment().suffixes.storage}'

// The identity is created either way, and the app carries it either way, so
// that every expression below stays valid whichever branch is taken. ARM
// evaluates both sides of a ternary, so a reference to a resource that only
// sometimes exists is a deployment error rather than a dead branch. Only the
// role assignment is conditional, and nothing reads its output.

// Created before the site so the role assignment does not have to wait for a
// principal that only exists once the site does. That ordering is what breaks
// identity-based deployment storage on a first-time deploy.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-replay-id'
  location: location
  tags: tags
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    // Under storageAuth=identity nothing needs a key, and leaving keys enabled
    // would leave a credential to leak that nothing is using. Under
    // connectionString the key is the only way in, so it has to stay on.
    allowSharedKeyAccess: !useIdentity
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainer
  properties: { publicAccess: 'None' }
}

resource replayStateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: stateContainer
  properties: { publicAccess: 'None' }
}

resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useIdentity) {
  name: guid(storage.id, identity.id, blobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataOwner)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-replay-law'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${name}-replay-ai'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${name}-replay-plan'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: useIdentity ? {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.id
          } : {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: union([
        // Load-bearing and invisible. The mcp-custom-handler configuration
        // profile in host.json is PREVIEW, and without this flag the host
        // does not recognise it: it looks for functions, finds none, never
        // starts the handler, and answers 502 to everything. Nothing in the
        // failure names the flag. Microsoft's own sample carries it in
        // local.settings.json, which is the only place it is written down.
        { name: 'AzureWebJobsFeatureFlags', value: 'EnableMcpCustomHandlerPreview' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'REPLAY_TOKEN', value: replayToken }
        { name: 'REPLAY_CASSETTE', value: defaultCassette }
        { name: 'REPLAY_ON_EXHAUSTED', value: onExhausted }
        { name: 'REPLAY_STATE_CONTAINER', value: stateContainer }
      ], useIdentity ? [
        // Identity-based, so there is no connection string for the host's own
        // storage either.
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'AzureWebJobsStorage__clientId', value: identity.properties.clientId }
        // DefaultAzureCredential inside state_store.py picks the user-assigned
        // identity from this. Without it, it would try the system-assigned one,
        // which this app does not have.
        { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
        { name: 'REPLAY_STATE_ACCOUNT', value: storage.properties.primaryEndpoints.blob }
      ] : [
        // The key is a secret in configuration. It is here because assigning a
        // role needs a permission Contributor does not carry; see storageAuth.
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        { name: 'DEPLOYMENT_STORAGE_CONNECTION_STRING', value: storageConnectionString }
        { name: 'REPLAY_STATE_CONNECTION', value: storageConnectionString }
      ])
    }
  }
  dependsOn: [
    storageRole
    packageContainer
    replayStateContainer
  ]
}

output functionAppName string = site.name
output functionAppHostName string = site.properties.defaultHostName
output mcpUrl string = 'https://${site.properties.defaultHostName}/mcp'
output summaryUrl string = 'https://${site.properties.defaultHostName}/summary'
output storageAccountName string = storage.name
output deploymentContainerName string = deploymentContainer
output stateContainerName string = stateContainer
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output storageAuthMode string = storageAuth
