using './main.bicep'

// Every value comes from the environment, because the Azure CLI will not take
// a .bicepparam file AND inline -p overrides in the same deployment -- it is
// one parameter source or the other. Environment variables are how a
// .bicepparam file stays parameterised, and they work identically from bash
// and from PowerShell.

// Short and lower case: the storage account name is derived from it.
param name = readEnvironmentVariable('REPLAY_NAME', 'verveeval')

// Flex Consumption is region-limited. Check before changing:
//   az functionapp list-flexconsumption-locations -o table
param location = readEnvironmentVariable('REPLAY_LOCATION', 'eastus2')

// Required, and deliberately without a default. The cassettes carry ticket and
// company identifiers, so this is the only thing standing in front of customer
// data. Generate one per environment; deploy.sh and deploy.ps1 both refuse to
// run without it.
param replayToken = readEnvironmentVariable('REPLAY_TOKEN')

// Leave blank while the package carries a single cassette; name one once it
// carries several and you do not want the id in every URL.
param defaultCassette = readEnvironmentVariable('REPLAY_CASSETTE', '')

param onExhausted = readEnvironmentVariable('REPLAY_ON_EXHAUSTED', 'repeat')

// identity is the one to want: no storage key exists anywhere. It needs
// Microsoft.Authorization/roleAssignments/write at deploy time -- Role Based
// Access Control Administrator, User Access Administrator or Owner, alongside
// Contributor, which does not include it.
//
// connectionString needs nothing beyond Contributor and puts a storage key in
// app settings instead. Start there if you cannot assign roles, have someone
// who can grant the role (infra/rbac.bicep), then redeploy with identity and
// REPLAY_ASSIGN_ROLE=false.
param storageAuth = readEnvironmentVariable('REPLAY_STORAGE_AUTH', 'identity')

// false once the role has been granted by someone else: the deployment then
// does not declare the assignment, so it needs no roleAssignments/write.
param assignRole = bool(readEnvironmentVariable('REPLAY_ASSIGN_ROLE', 'true'))
