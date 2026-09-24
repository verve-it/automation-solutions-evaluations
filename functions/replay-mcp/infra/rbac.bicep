/*
  rbac.bicep — the one thing Contributor cannot do.

  main.bicep with storageAuth=identity assigns Storage Blob Data Owner to the
  function app's managed identity, which needs
  Microsoft.Authorization/roleAssignments/write on the resource group. That is
  Role Based Access Control Administrator (the least privilege that works),
  User Access Administrator or Owner; Contributor does not include it.

  This file is that role assignment on its own, so someone who has the
  permission can run it without needing to understand or re-run the rest.

  Run it as Owner, or as Role Based Access Control Administrator or User
  Access Administrator together with Contributor: running any deployment
  needs Microsoft.Resources/deployments/write, which those two do not carry.

    az deployment group create -g <rg> -f rbac.bicep \
        -p storageAccountName=<storage> identityName=<name>-replay-id

  Without Contributor, the same assignment as one command (bash, then
  PowerShell):

    az role assignment create --role "Storage Blob Data Owner" --assignee-object-id <identityPrincipalId> --assignee-principal-type ServicePrincipal --scope $(az storage account show -g <rg> -n <storage> --query id -o tsv)

    az role assignment create --role "Storage Blob Data Owner" --assignee-object-id <identityPrincipalId> --assignee-principal-type ServicePrincipal --scope (az storage account show -g <rg> -n <storage> --query id -o tsv)

  (That one gets a random name, so main.bicep declaring its own would fail
  with RoleAssignmentExists: every later deploy needs REPLAY_ASSIGN_ROLE=false,
  or -SkipRoleAssignment in deploy.ps1. The deploy scripts say so if it
  happens.)

  The values: the deploy scripts print storageAccountName, identityName and
  identityPrincipalId from main.bicep's outputs; or

    az storage account list -g <rg> --query "[].name" -o tsv
    az identity list -g <rg> --query "[].{name:name, principalId:principalId}" -o table

  Then the Contributor redeploys on identity without declaring the
  assignment again -- ARM re-puts every resource it declares, and a
  Contributor cannot put a role assignment even when it already exists. The
  storage key disappears from the app's configuration:

    REPLAY_STORAGE_AUTH=identity REPLAY_ASSIGN_ROLE=false ./deploy.sh <rg>
    .\deploy.ps1 -ResourceGroup <rg> -StorageAuth identity -SkipRoleAssignment

  Nothing else about the app changes. The app is not touched here at all, so
  running this while it is serving a replay is safe.

  A role assigned directly to the identity takes up to about 10 minutes to
  take effect (Azure RBAC troubleshooting). The same identity carries the
  host's storage and the deployment package, so during that window the app
  may not start at all; once it is up, verify.py reports replay state as
  `unavailable` with a 403 until the role arrives. The server retries, so
  nothing needs restarting.
*/

@description('The storage account the function app uses. main.bicep outputs it as storageAccountName.')
param storageAccountName string

@description('The function app\'s user-assigned identity. main.bicep names it <name>-replay-id.')
param identityName string

// Storage Blob Data Owner, as in main.bicep: the host's own storage and the
// Flex deployment container use this identity as well as the replay state.
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
