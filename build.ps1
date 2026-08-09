<#
.SYNOPSIS
Build every frontend in go\frontends\ into bin\. The Windows half of build.sh.

.EXAMPLE
.\build.ps1                 build all

.EXAMPLE
.\build.ps1 deck            build one

.EXAMPLE
.\build.ps1 -KeepGoing      build all, report failures at the end instead of
                            stopping at the first
#>
[CmdletBinding()]
param(
    [switch]$KeepGoing,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Targets
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    Write-Host "error: go is not installed or not in PATH" -ForegroundColor Red
    Write-Host "       get it from https://go.dev/dl/"
    exit 1
}

New-Item -ItemType Directory -Force -Path bin | Out-Null

if (-not $Targets -or $Targets.Count -eq 0) {
    $Targets = Get-ChildItem -Directory go\frontends | ForEach-Object { $_.Name }
}
if (-not $Targets -or $Targets.Count -eq 0) {
    Write-Host "no frontends in go\frontends\" -ForegroundColor Red
    exit 1
}

$failed = @()
foreach ($name in $Targets) {
    if (-not (Test-Path -LiteralPath "go\frontends\$name" -PathType Container)) {
        Write-Host "error: no such frontend: $name" -ForegroundColor Red
        exit 1
    }
    Push-Location go
    try {
        # Forward slashes on purpose: that is a Go package path, not a file path,
        # and go does not accept backslashes in one.
        & go build -o "../bin/$name.exe" "./frontends/$name"
        $ok = ($LASTEXITCODE -eq 0)
    } finally {
        Pop-Location
    }
    if ($ok) {
        Write-Host "built: bin\$name.exe"
    } elseif ($KeepGoing) {
        $failed += $name
    } else {
        exit 1
    }
}

if ($failed.Count -gt 0) {
    Write-Host "did not build: $($failed -join ' ')" -ForegroundColor Yellow
    exit 1
}
