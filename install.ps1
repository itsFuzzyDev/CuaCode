<#
.SYNOPSIS
One command from a fresh clone to something you can run: interpreter, venv,
dependencies, binaries. The Windows half of install.sh -- the two stay in step
by hand, so a change here usually belongs there too.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoBuild
#>
[CmdletBinding()]
param(
    [switch]$NoBuild,   # python side only, skip Go
    [switch]$NoVenv     # install deps into the interpreter as found
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Say  ($m) { Write-Host "==> $m" -ForegroundColor White }
function Warn ($m) { Write-Host "warning: $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# --- python -------------------------------------------------------------------
# Version is asked of the interpreter, never read off the name. On Windows there
# is a second reason for that: `python3.exe` on PATH is usually the Microsoft
# Store App Execution Alias -- a zero-byte stub that prints "Python was not
# found" and exits -- and only running it tells you which one you have.
function Test-Python($exe, $preArgs) {
    try {
        $all = @()
        if ($preArgs) { $all += $preArgs }
        $all += @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)')
        & $exe @all 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$python = $null
$pythonArgs = @()
# `py -3` first: the launcher is the one name on Windows that is never the stub.
$candidates = @()
if ($env:CUACODE_PYTHON) { $candidates += ,@($env:CUACODE_PYTHON, @()) }
$candidates += ,@('py',      @('-3'))
$candidates += ,@('python',  @())
$candidates += ,@('python3', @())

foreach ($c in $candidates) {
    $exe = $c[0]; $pre = $c[1]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    if (Test-Python $exe $pre) { $python = $exe; $pythonArgs = $pre; break }
}
if (-not $python) {
    Die "no python >= 3.10 found (tried py -3, python, python3; set CUACODE_PYTHON to override)`n       get it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'"
}
$ver = & $python @pythonArgs -c 'import platform; print(platform.python_version())'
Say "python: $python $($pythonArgs -join ' ') ($ver)"

# --- venv ---------------------------------------------------------------------
$venvPy = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not $NoVenv) {
    if (Test-Path -LiteralPath $venvPy) {
        Say "venv already at .\venv, reusing it"
    } else {
        Say "creating venv at .\venv"
        & $python @pythonArgs -m venv venv
        if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
    }
    if (-not (Test-Path -LiteralPath $venvPy)) { Die "venv created but $venvPy is missing" }
    $python = $venvPy
    $pythonArgs = @()
}

# --- dependencies ---------------------------------------------------------------
# Through `python -m pip`, not the pip script: a copied or moved venv leaves that
# shim pointing at an interpreter that may not be there any more.
Say "installing dependencies"
& $python @pythonArgs -m pip install --upgrade pip *> $null
& $python @pythonArgs -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "pip install failed -- see the output above" }

# The worker imports these at boot, and until it can, a frontend shows nothing at
# all. Better said here than left for the first launch.
Say "checking the worker imports"
& $python @pythonArgs -c 'import ollama, openai, anthropic, mss, PIL, yaml, httpx, win32api'
if ($LASTEXITCODE -ne 0) { Die "dependencies installed but do not import -- delete .\venv and run this again" }

# --- frontends -------------------------------------------------------------------
$built = $false
if (-not $NoBuild) {
    if (Get-Command go -ErrorAction SilentlyContinue) {
        Say "building frontends"
        # Keep going: gio wants a C toolchain, and its absence is no reason to
        # leave somebody with no terminal frontend.
        & (Join-Path $PSScriptRoot 'build.ps1') -KeepGoing
        if ($LASTEXITCODE -ne 0) { Warn "some frontends did not build (see above)" }
        $built = $true
    } else {
        Warn "go not found, skipping the build -- get it from https://go.dev/dl/"
        Warn "you can still run from source once go is installed: .\run.ps1 deck"
    }
}

# --- done -------------------------------------------------------------------------
Write-Host ""
Say "done"
Write-Host "  run a frontend:   .\bin\deck.exe      (or .\run.ps1 deck, straight from source)"
Write-Host "  list frontends:   .\run.ps1"
Write-Host "  api keys/models:  $env:USERPROFILE\.cuacode\config.json, written on first launch"
if (-not $built) { Write-Host "  build later:      .\build.ps1" }
