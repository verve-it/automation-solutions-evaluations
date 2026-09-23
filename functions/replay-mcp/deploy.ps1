<#
.SYNOPSIS
    Provision the replay MCP server and publish it. PowerShell twin of deploy.sh.

.DESCRIPTION
    Idempotent: re-running redeploys the template and republishes the package.

    The token is read from the environment rather than stored, because the
    cassettes it protects carry ticket and company identifiers. Generate one:

        $bytes = New-Object byte[] 32
        (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
        $env:REPLAY_TOKEN = [BitConverter]::ToString($bytes).Replace('-','').ToLower()

    There is no `openssl` and no `export` on a stock Windows box; that is the
    equivalent, and -NewToken below does it for you.

.EXAMPLE
    .\deploy.ps1 -ResourceGroup Verve-CopilotCapacity -NewToken

.EXAMPLE
    $env:REPLAY_TOKEN = '<existing token>'
    .\deploy.ps1 -ResourceGroup Verve-CopilotCapacity -Location eastus2

    Redeploying: set the SAME token the callers hold (the GitHub environments'
    REPLAY_TOKEN). -NewToken on a redeploy locks every caller out until they
    are updated too.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ResourceGroup,

    # Omitted means: the resource group's own region if it already exists,
    # otherwise eastus2. A group's location says where its metadata lives --
    # the resources inside it may sit anywhere -- so a group in a region Flex
    # Consumption does not serve is not a reason to make a second group.
    [string] $Location,

    # Generate a token, use it, and print it once. A new token on a REDEPLOY
    # locks out every caller holding the old one. The value can be read back
    # from the app's settings (REPLAY_TOKEN) by anyone allowed to list them,
    # not from the deployment's secure parameter.
    [switch] $NewToken,

    # identity is the one to want: no storage key exists anywhere. It needs
    # Microsoft.Authorization/roleAssignments/write at deploy time -- Role
    # Based Access Control Administrator, User Access Administrator or Owner,
    # alongside Contributor, which does not include it.
    # connectionString needs nothing beyond Contributor and puts a storage key
    # in app settings instead.
    [ValidateSet('identity', 'connectionString')]
    [string] $StorageAuth = 'identity',

    # Someone else has already granted the identity its storage role
    # (infra/rbac.bicep). The template then does not declare the assignment,
    # so a deployer without roleAssignments/write can still deploy identity.
    [switch] $SkipRoleAssignment,

    # Deploy without checking the result. Only for when the cassettes are
    # known good and you are iterating on infrastructure.
    [switch] $SkipVerify
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# PowerShell 7.4 turned this on by default, which makes any native command
# that exits non-zero throw. `az group show` on a group that does not exist
# exits 3, and that is information, not a failure. Exit codes are handled
# explicitly by Invoke-Checked below instead.
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($NewToken) {
    $bytes = New-Object byte[] 32
    (New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
    $env:REPLAY_TOKEN = [BitConverter]::ToString($bytes).Replace('-', '').ToLower()
    Write-Host ''
    Write-Host 'REPLAY_TOKEN (save it now; later only from the app settings):' -ForegroundColor Yellow
    Write-Host "  $($env:REPLAY_TOKEN)"
    Write-Host ''
}

if (-not $env:REPLAY_TOKEN) {
    throw @'
REPLAY_TOKEN is not set. Without it the replay server is open to anyone who
finds the URL, and the cassettes carry ticket and company identifiers.

Pass -NewToken to generate one, or set $env:REPLAY_TOKEN yourself.
'@
}

foreach ($tool in 'az', 'python') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found on PATH."
    }
}

function Invoke-Checked {
    param([string] $What, [scriptblock] $Command)
    $output = & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
    return $output
}

$existing = az group show -n $ResourceGroup --query location -o tsv 2>$null
if ($LASTEXITCODE -eq 0 -and $existing) {
    Write-Host "==> resource group $ResourceGroup exists in $existing"
    if (-not $PSBoundParameters.ContainsKey('Location')) { $Location = $existing }
}
else {
    if (-not $Location) { $Location = 'eastus2' }
    Write-Host "==> resource group $ResourceGroup ($Location)"
    Invoke-Checked 'az group create' { az group create -n $ResourceGroup -l $Location -o none }
}

# Ask Azure rather than hardcoding a list that goes stale. Flex Consumption is
# region-limited, and learning that from a template failure three resources in
# is worse than learning it now.
Write-Host "==> checking Flex Consumption is available in $Location"
$raw = Invoke-Checked 'az functionapp list-flexconsumption-locations' {
    az functionapp list-flexconsumption-locations --query "[].name" -o tsv
}
$supported = @($raw) -split "`n" |
    ForEach-Object { ($_ -replace '\s', '').ToLower() } |
    Where-Object { $_ } | Sort-Object -Unique
if ($supported -notcontains ($Location -replace '\s', '').ToLower()) {
    # Written out rather than thrown as one string: PowerShell renders an
    # exception message on a single line, and a region list is unreadable
    # that way.
    Write-Host ''
    Write-Host "Flex Consumption is not available in $Location." -ForegroundColor Red
    Write-Host ''
    Write-Host 'The resource group can stay where it is -- pass a supported region'
    Write-Host 'and the resources go there instead:'
    Write-Host ''
    Write-Host "    .\deploy.ps1 -ResourceGroup $ResourceGroup -Location <region>"
    Write-Host ''
    Write-Host 'Available:'
    $supported | ForEach-Object { Write-Host "  $_" }
    Write-Host ''
    throw "Flex Consumption is not available in $Location."
}
Write-Host '    ok'

# main.bicepparam reads these. Restored after the deployment so they do not
# linger in the caller's session and steer a later deployment silently.
$saved = @{}
foreach ($var in 'REPLAY_STORAGE_AUTH', 'REPLAY_ASSIGN_ROLE', 'REPLAY_LOCATION') {
    $saved[$var] = [Environment]::GetEnvironmentVariable($var)
}
$env:REPLAY_STORAGE_AUTH = $StorageAuth
$env:REPLAY_ASSIGN_ROLE = if ($SkipRoleAssignment) { 'false' } else { 'true' }
Write-Host "==> template (storage auth: $StorageAuth, assign role: $($env:REPLAY_ASSIGN_ROLE))"
# One parameter source only: the CLI will not take a .bicepparam file and
# inline -p overrides in the same deployment. main.bicepparam reads these.
$env:REPLAY_LOCATION = $Location
$deploymentName = "replay-mcp-$((Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss'))"
try {
    $json = az deployment group create `
        -g $ResourceGroup `
        -n $deploymentName `
        -f (Join-Path $here 'infra/main.bicep') `
        -p (Join-Path $here 'infra/main.bicepparam') `
        --query properties.outputs -o json 2>&1
    $deployExit = $LASTEXITCODE
}
finally {
    foreach ($var in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($var, $saved[$var])
    }
}
if ($deployExit -ne 0) {
    $text = ($json | Out-String)
    Write-Host $text
    # The one failure with a specific answer. Assigning a role needs Role
    # Based Access Control Administrator, User Access Administrator or Owner;
    # Contributor stops exactly here, after the storage account already exists.
    if ($text -match 'RoleAssignmentExists') {
        Write-Host ''
        Write-Host 'The identity already has its role, granted outside this template' -ForegroundColor Yellow
        Write-Host '(e.g. az role assignment create, under another name). Deploy without'
        Write-Host 'declaring it, now and every time after:'
        Write-Host ''
        Write-Host "    .\deploy.ps1 -ResourceGroup $ResourceGroup -Location $Location -SkipRoleAssignment"
        Write-Host ''
    }
    elseif ($text -match 'roleAssignments') {
        Write-Host ''
        Write-Host 'That is the role assignment, and it is the only step Contributor' -ForegroundColor Yellow
        Write-Host 'cannot do. Two ways on:'
        Write-Host ''
        Write-Host '  1. Deploy without it -- a storage key goes into app settings'
        Write-Host '     instead of the identity being granted a role:'
        Write-Host ''
        Write-Host "       .\deploy.ps1 -ResourceGroup $ResourceGroup -Location $Location -StorageAuth connectionString"
        Write-Host ''
        Write-Host '  2. Have someone with Role Based Access Control Administrator,'
        Write-Host '     User Access Administrator or Owner grant it (infra/rbac.bicep'
        Write-Host '     says how), then redeploy without declaring it again:'
        Write-Host ''
        Write-Host "       .\deploy.ps1 -ResourceGroup $ResourceGroup -Location $Location -SkipRoleAssignment"
        Write-Host ''
        Write-Host '     The key disappears from configuration and nothing else changes.'
        Write-Host '     Their values:'
        Write-Host "       az storage account list -g $ResourceGroup --query `"[].name`" -o tsv"
        Write-Host "       az identity list -g $ResourceGroup --query `"[].{name:name, principalId:principalId}`" -o table"
        Write-Host ''
        Write-Host 'Nothing is half-built: the deployment is incremental and re-running'
        Write-Host 'it is safe.'
        Write-Host ''
    }
    throw "az deployment group create failed (exit $deployExit)"
}

$outputs = $json | ConvertFrom-Json
$app = $outputs.functionAppName.value
$hostName = $outputs.functionAppHostName.value

Write-Host '==> package'
Invoke-Checked 'build.py' { python (Join-Path $here 'build.py') }

Write-Host "==> waiting for $app to resolve"
# ARM returns before a new app is consistently readable, and publishing into
# that window fails in a way that reads like the app was never created.
$visible = $false
foreach ($attempt in 1..6) {
    az functionapp show -g $ResourceGroup -n $app -o none 2>$null
    if ($LASTEXITCODE -eq 0) { $visible = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $visible) {
    throw @"
$app is not readable in $ResourceGroup. The template reported success, so
check the subscription the CLI is pointed at: az account show
"@
}

Write-Host "==> publish to $app"
$build = Join-Path $here '.build'
$zip = Join-Path ([System.IO.Path]::GetTempPath()) "replay-mcp-$deploymentName.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
# .NET rather than Compress-Archive: it writes entries relative to the folder
# with forward slashes and no directory records, which is what the remote build
# expects. Compress-Archive on Windows PowerShell does not always.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($build, $zip)

# az, not Core Tools, and on purpose.
#
# `func azure functionapp publish <name>` finds the app by searching the
# subscription and reads only the first page of results. Past ~999 resources it
# reports "Can't find app with name" for an app that plainly exists -- which is
# what happened here. az takes the resource group explicitly and does not
# search.
#
# No --build-remote, because there is nothing to build: requirements.txt is
# empty and the server is stdlib only. A remote build installed packages into
# .python_packages, which a custom handler never looks in -- an install that
# appeared to succeed and left the app without its dependency. Despite the
# command's name this routes to Flex Consumption package deployment, the only
# deployment technology Flex supports.
az functionapp deployment source config-zip `
    -g $ResourceGroup -n $app --src $zip -o none
$published = $LASTEXITCODE -eq 0
Remove-Item $zip -Force
if (-not $published) {
    Write-Host ''
    Write-Host 'The package uploaded; the app did not come up.' -ForegroundColor Yellow
    Write-Host ''
    # Run it rather than print it. A deploy that hands over a command to run
    # is two round trips where one would do, and the log is the only thing
    # that separates a crashed handler from one that never started.
    & python (Join-Path $here 'diagnose.py') -g $ResourceGroup
    throw 'the app is unhealthy after publishing'
}

Write-Host ''
Write-Host '==> deployed' -ForegroundColor Green
Write-Host "  MCP      https://$hostName/mcp/<cassette-id>"
Write-Host "  summary  https://$hostName/summary/<cassette-id>"
Write-Host "  storage  $($outputs.storageAccountName.value) (auth: $($outputs.storageAuthMode.value))"
Write-Host "  identity $($outputs.identityName.value) (principal $($outputs.identityPrincipalId.value))"

if ($SkipVerify) {
    Write-Host ''
    Write-Host 'Skipped verification. Deployed is not the same as right:'
    Write-Host "  python $(Join-Path $here 'verify.py') https://$hostName -g $ResourceGroup"
    return
}

# Deploying it is not the same as it being right, and finding that out in a
# second command is a second round trip. verify.py waits for the app to come
# up, replays every cassette against it, and runs the diagnosis itself if the
# server never answers.
Write-Host ''
Write-Host '==> verifying'
& python (Join-Path $here 'verify.py') "https://$hostName" -g $ResourceGroup
if ($LASTEXITCODE -ne 0) {
    throw 'the deployment is not serving the cassettes correctly (see above)'
}

Write-Host ''
Write-Host 'Run the gate:'
Write-Host '  python replay/run_replay.py `'
Write-Host '      --cassette cassettes/<cassette-id>.json `'
Write-Host "      --server-url https://$hostName/mcp/<cassette-id> ``"
Write-Host '      --token $env:REPLAY_TOKEN'
