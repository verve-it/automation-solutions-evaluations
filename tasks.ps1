<#
.SYNOPSIS
    The Makefile, for PowerShell. Same targets, same commands.

.DESCRIPTION
    There is no `make` on a stock Windows box, and every message in this repo
    that said "run make X" was telling half its users to run something they do
    not have. This is the other half.

    Targets match the Makefile one for one. If you change one, change both --
    or better, notice that `tasks.ps1 <target>` and `make <target>` are meant
    to be the same sentence.

.EXAMPLE
    .\tasks.ps1                       # list the targets
    .\tasks.ps1 test
    .\tasks.ps1 cassettes
    .\tasks.ps1 replay-verify -Url https://<app>.azurewebsites.net
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Target = 'help',

    # replay: which cassette to serve.
    [string] $Cassette,

    # replay-deploy: the resource group, and optionally the region.
    [string] $ResourceGroup,
    [string] $Location = 'eastus2',

    # replay-verify: the deployed server.
    [string] $Url
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here
try {

$PY = 'python'
$FULL_TRIAGE = 'traces/2026-09-03-full-triage.json'
$OPS_WORST = 'traces/2026-09-15-ops-worst-case.json'
$FT_BASELINE = 'baselines/full-triage-2026-09-18.json'
$OW_BASELINE = 'baselines/ops-worst-case-2026-09-18.json'

function Run {
    param([string[]] $Arguments, [switch] $AllowFailure)
    Write-Host "  $PY $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $PY @Arguments
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "$PY $($Arguments -join ' ') failed (exit $LASTEXITCODE)"
    }
}

switch ($Target) {
    'help' {
        Write-Host 'Targets (same as the Makefile):'
        Write-Host '  test                unit tests + frozen-set replay (no Azure, no network)'
        Write-Host '  evals               score the known-good set against its baseline'
        Write-Host '  evals-ops           score the known-bad set against its baseline'
        Write-Host '  baselines           re-freeze both baselines from the committed traces'
        Write-Host '  cassettes           build replay cassettes from the committed traces'
        Write-Host '  replay              serve a cassette as an MCP toolbox  -Cassette <path>'
        Write-Host '  replay-package      assemble the Azure Function deployment package'
        Write-Host '  replay-deploy       provision and publish  -ResourceGroup <rg> [-Location]'
        Write-Host '  replay-verify       replay every cassette against the hosted server  -Url <url>'
        Write-Host '  foundry-dataset     build the Foundry evaluation dataset'
        Write-Host '  foundry-register    print the evaluator payloads without calling Foundry'
        Write-Host '  foundry             convert to the Foundry judged-evaluator schema'
        Write-Host '  manifest-skeleton   skeleton manifest from the traces (no schemas)'
        Write-Host '  clean               remove generated output'
    }

    'test' { Run @('-m', 'pytest', 'tests/', '-q') }

    'evals' {
        Run @('trace_to_eval.py', $FULL_TRIAGE, '-o', 'out', '--tool-defs',
              'tool_manifests/', '--skill-registry', 'skills')
        Run @('run_evals.py', 'out/eval_runs.jsonl', '--expected', 'expected.json',
              '--baseline', $FT_BASELINE, '--json', 'artifacts/full-triage.json')
    }

    'evals-ops' {
        Run @('trace_to_eval.py', $OPS_WORST, '-o', 'out-ops', '--tool-defs',
              'tool_manifests/')
        Run @('run_evals.py', 'out-ops/eval_runs.jsonl', '--expected', 'expected.json',
              '--baseline', $OW_BASELINE, '--json', 'artifacts/ops-worst-case.json')
    }

    'baselines' {
        # Must use the same --tool-defs as evals/evals-ops, or every run reports
        # evaluator_ready as a fix and valid_tool_args as newly scored.
        Run @('trace_to_eval.py', $FULL_TRIAGE, '-o', 'out', '--tool-defs',
              'tool_manifests/', '--skill-registry', 'skills')
        Run -AllowFailure @('run_evals.py', 'out/eval_runs.jsonl', '--expected',
                            'expected.json', '--json', $FT_BASELINE)
        Run @('trace_to_eval.py', $OPS_WORST, '-o', 'out-ops', '--tool-defs',
              'tool_manifests/')
        Run -AllowFailure @('run_evals.py', 'out-ops/eval_runs.jsonl', '--expected',
                            'expected.json', '--json', $OW_BASELINE)
    }

    'cassettes' {
        Run @('replay/make_cassette.py', $FULL_TRIAGE, '-o', 'cassettes')
        Run @('replay/make_cassette.py', $OPS_WORST, '-o', 'cassettes')
    }

    'replay' {
        if (-not $Cassette) {
            throw 'usage: .\tasks.ps1 replay -Cassette cassettes/<file>.json'
        }
        Run @('replay/replay_server.py', $Cassette, '--tool-defs', 'tool_manifests/',
              '--journal', 'artifacts/replay-journal.json')
    }

    'replay-package' { Run @('functions/replay-mcp/build.py') }

    'replay-deploy' {
        # REPLAY_TOKEN is deliberately not defaulted: the cassettes carry ticket
        # and company identifiers and it is the only thing in front of them.
        if (-not $ResourceGroup) {
            throw 'usage: .\tasks.ps1 replay-deploy -ResourceGroup <rg> [-Location <region>]'
        }
        & (Join-Path $here 'functions/replay-mcp/deploy.ps1') `
            -ResourceGroup $ResourceGroup -Location $Location
    }

    'replay-verify' {
        # Deploying it is not the same as it being right.
        if (-not $Url) {
            throw 'usage: .\tasks.ps1 replay-verify -Url https://<app>.azurewebsites.net'
        }
        Run @('functions/replay-mcp/verify.py', $Url)
    }

    'foundry-dataset' {
        Run @('foundry/to_foundry_dataset.py', $FULL_TRIAGE, '--expected',
              'expected.json', '--tool-defs', 'tool_manifests/', '-o',
              'artifacts/foundry-dataset.jsonl')
    }

    'foundry-register' { Run @('foundry/register_evaluators.py', '--dry-run') }

    'foundry' {
        Run @('foundry/submit_to_foundry.py', 'out/eval_runs.jsonl', '--dry-run',
              '--sample', '0')
    }

    'manifest-skeleton' {
        # Writes to artifacts/, NOT tool_manifests/ -- the real manifest already
        # lives there and the converter loads every file in the directory.
        Run @('tools/extract_tool_manifest.py', '--from-trace', $FULL_TRIAGE,
              '--toolbox', 'ConnectwiseMCP', '--version', '5', '-o',
              'artifacts/connectwisemcp-skeleton.json')
    }

    'clean' {
        foreach ($path in 'out', 'out-ops', 'artifacts', 'skills', 'cassettes',
                 '.pytest_cache') {
            if (Test-Path $path) { Remove-Item -Recurse -Force $path }
        }
        Get-ChildItem -Recurse -Directory -Filter __pycache__ |
            Remove-Item -Recurse -Force
    }

    default {
        throw "unknown target '$Target'. Run .\tasks.ps1 for the list."
    }
}

}
finally { Pop-Location }
