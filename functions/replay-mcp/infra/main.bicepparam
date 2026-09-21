using './main.bicep'

// Short and lower case: the storage account name is derived from it.
param name = 'verveeval'

// Flex Consumption is region-limited. Check before changing:
//   az functionapp list-flexconsumption-locations -o table
param location = 'eastus2'

// Generate one per environment and keep it out of the file:
//   az deployment group create ... -p replayToken=$(openssl rand -hex 32)
// The cassettes carry ticket and company identifiers, so this is the only
// thing standing in front of customer data.
param replayToken = readEnvironmentVariable('REPLAY_TOKEN')

// Leave blank while the package carries a single cassette; name one once it
// carries several and you do not want to put the id in every URL.
param defaultCassette = ''

param onExhausted = 'repeat'
