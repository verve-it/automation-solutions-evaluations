/*
  rbac.bicep — the one thing Contributor cannot do.

  main.bicep with storageAuth=identity assigns Storage Blob Data Owner to the
  function app's managed identity, which needs
  Microsoft.Authorization/roleAssignments/write on the resource group. That is
  User Access Administrator or Owner; Contributor does not include it.

  This file is that role assignment on its own, so someone who has the
  permission can run it without needing to understand or re-run the rest.

  Run it (User Access Administrator or Owner on the resource group):

    az deployment group create -g <rg> -f rbac.bicep \
        -p storageAccountName=<storage> identityName=<name>-replay-id

  Both values are printed by main.bicep as storageAccountName and the identity
  it creates. Then redeploy the app with storageAuth=identity and the storage
  key disappears from its configuration:

    REPLAY_STORAGE_AUTH=identity ./deploy.sh <rg>

  Nothing else about the app changes. The app is not touched here at all, so
  running this while it is serving a replay is safe.
*/

@description('The storage account the function app uses. main.bicep outputs it as storageAccountName.')
param storageAccountName string

@description('The function app\'s user-assigned identity. main.bicep names it <name>-replay-id.')
param identityName string

// Storage Blob Data Owner. Owner rather than Contributor because the app
// creates its state container on first use, and because the Flex deployment
// container is managed by the platform through this same identity.
var blobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

// The same name main.bicep computes, so running both is idempotent rather
// than a duplicate assignment.
resource storageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, identity.id, blobDataOwner)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataOwner)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output roleAssignmentId string = storageRole.id
output principalId string = identity.properties.principalId
