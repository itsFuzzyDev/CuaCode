<#
.SYNOPSIS
Run one frontend straight from source, no build step. The Windows half of run.sh.

.EXAMPLE
.\run.ps1                   list frontends

.EXAMPLE
.\run.ps1 deck --resume     run go\frontends\deck, passing the rest through
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Name,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$launchDir = (Get-Location).Path     # where you ran this from, before the cd below
Set-Location -LiteralPath $PSScriptRoot

if (-not $Name) {
    Write-Host "usage: .\run.ps1 <frontend>"
    Write-Host "       .\run.ps1 --usage [--days N] [--json]"
    Write-Host "frontends:"
    Get-ChildItem -Directory go\frontends | ForEach-Object { Write-Host "  $($_.Name)" }
    exit 1
}

# Not a frontend: a question about the app rather than a conversation with it.
# The worker prints what every session has cost and exits, so it never starts a
# session of its own to answer.
if ($Name -in @('--usage', 'usage')) {
    $py = $env:CUACODE_PYTHON
    if (-not $py) {
        $venvPy = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
        $py = if (Test-Path -LiteralPath $venvPy) { $venvPy } else { 'python' }
    }
    & $py (Join-Path $PSScriptRoot 'main.py') --usage @Rest
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath "go\frontends\$Name" -PathType Container)) {
    Write-Host "error: no such frontend: $Name" -ForegroundColor Red
    exit 1
}

# Frontends locate main.py by walking up from cwd; pin it so the worker is always
# this checkout's, whatever directory you launched from.
if (-not $env:CUACODE_WORKER) {
    $env:CUACODE_WORKER = Join-Path $PSScriptRoot 'main.py'
}
# `go run` executes the binary from the module directory, so the frontend's own
# cwd is go\ and the agent's shell would start there on every dev launch. Report
# the directory you were actually standing in instead.
if (-not $env:CUACODE_CWD) {
    $env:CUACODE_CWD = $launchDir
}
# The venv is found by layout at spawn time, but a dev launch is exactly when
# somebody has a half-set-up machine -- say which interpreter, and the worker
# either starts or says why.
$venvPy = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not $env:CUACODE_PYTHON -and (Test-Path -LiteralPath $venvPy)) {
    $env:CUACODE_PYTHON = $venvPy
}

Set-Location -LiteralPath (Join-Path $PSScriptRoot 'go')
& go run "./frontends/$Name" @Rest
exit $LASTEXITCODE
