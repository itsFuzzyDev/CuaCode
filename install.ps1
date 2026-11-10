# One command from a fresh clone to something you can run on Windows:
# interpreter, venv, dependencies, binaries, and a `cuacode` on PATH.
#
#   .\install.ps1                     everything
#   .\install.ps1 --no-build          python side only
#   .\install.ps1 --no-venv           install deps into the interpreter as found
#   .\install.ps1 -Help               this text
#
# Run it through the policy, not the other way round:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$MinPy = '3.10'          # `X | None` annotations are evaluated at def time
$Venv = '.venv'
$build = $true
$buildFailed = $false
$useVenv = $true

function Say([string]$Message) { Write-Host "==> $Message" }
function Warn([string]$Message) { Write-Host "warning: $Message" -ForegroundColor Yellow }
function Die([string]$Message) {
    [Console]::Error.WriteLine("error: $Message")
    exit 1
}

function Show-Help {
    # The header block above, so there is one place to edit.
    (Get-Content -LiteralPath $PSCommandPath | Select-Object -First 10) |
        ForEach-Object { $_ -replace '^# ?', '' }
}

foreach ($arg in $args) {
    switch ($arg) {
        '--no-build' { $build = $false }
        '--no-venv' { $useVenv = $false }
        '-h' { Show-Help; exit 0 }
        '--help' { Show-Help; exit 0 }
        '-Help' { Show-Help; exit 0 }
        '/?' { Show-Help; exit 0 }
        default {
            [Console]::Error.WriteLine("error: unknown option: $arg")
            exit 1
        }
    }
}

# --- python ------------------------------------------------------------------
# Version checked by asking the interpreter, not by parsing --version: the name
# says nothing about the version behind it, and the WindowsApps `python` is a
# stub that opens the Store on a machine with no Python at all.
function Test-Python([string]$Exe, [string[]]$LauncherArgs) {
    $cmd = Get-Command $Exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $cmd) { return $false }
    & $cmd.Source @LauncherArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>$null |
        Out-Null
    return ($LASTEXITCODE -eq 0)
}

$pyExe = $null
$pyArgs = @()
$candidates = @()
if ($env:CUACODE_PYTHON) { $candidates += , @($env:CUACODE_PYTHON) }
$candidates += , @('py', '-3')
$candidates += , @('python')
$candidates += , @('python3')

foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $rest = @()
    if ($candidate.Count -gt 1) { $rest = @($candidate | Select-Object -Skip 1) }
    if (Test-Python -Exe $exe -LauncherArgs $rest) {
        $pyExe = (Get-Command $exe -CommandType Application | Select-Object -First 1).Source
        $pyArgs = $rest
        break
    }
}

if (-not $pyExe) {
    Die "no python >= $MinPy found (looked for py -3, python, python3; set CUACODE_PYTHON to override)"
}
$pyVersion = (& $pyExe @pyArgs -c 'import platform; print(platform.python_version())').Trim()
Say "python: $pyExe $pyVersion"

# --- venv --------------------------------------------------------------------
if ($useVenv) {
    $venvDir = Join-Path $PSScriptRoot $Venv
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvDir)) {
        Say "creating venv at .\$Venv"
        & $pyExe @pyArgs -m venv $venvDir
        if ($LASTEXITCODE -ne 0) {
            Die "venv creation failed -- install the Python 'venv' component, then run this again"
        }
    }
    elseif (-not (Test-Path -LiteralPath $venvPython)) {
        # Never deleted by this script: it may be a venv somebody is still using,
        # and the interpreter is the only thing that can repair it.
        Die ".\$Venv exists but has no Scripts\python.exe -- remove .\$Venv by hand and run this again"
    }
    else {
        Say "venv already at .\$Venv, reusing it"
    }
    $python = $venvPython
    # A venv's python takes no launcher args: `py -3 -m pip` would pass -3 to pip.
    $pyArgs = @()
}
else {
    $python = $pyExe
}

# --- dependencies ------------------------------------------------------------
# Through `python -m pip` rather than the pip script: the shebang in a copied or
# moved venv points at an interpreter that may not be there any more.
Say "installing dependencies"
& $python @pyArgs -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Warn "could not upgrade pip, carrying on" }
& $python @pyArgs -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die "pip install failed -- see the output above" }

# The worker imports these at boot, and until it can, a frontend shows nothing
# at all. Better to say so here than to leave that for the first launch.
Say "checking the worker imports"
& $python @pyArgs -c 'import openai, anthropic, mss, PIL, yaml, httpx'
if ($LASTEXITCODE -ne 0) {
    Die "dependencies installed but do not import -- try deleting .\$Venv and running this again"
}

# --- frontends ---------------------------------------------------------------
# The [[bin]] targets of the crates under rust\, read from the manifests rather
# than listed here, so adding a frontend stays a single file (see build.sh).
function Get-Frontends {
    # The GPUI app is not on the Windows path: doctor --upgrade builds the three
    # below and WINDOWS.md documents the same three. One token to remove if it ships.
    $skip = @('cuacode-app')

    # rust\Cargo.toml plus one level down, the same two globs build.sh reads. Read
    # rather than listed here, so adding a frontend stays a single file.
    $manifests = @((Join-Path $PSScriptRoot 'rust\Cargo.toml')) +
        @(Get-ChildItem -Path (Join-Path $PSScriptRoot 'rust\*\Cargo.toml') -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })

    $found = @()
    foreach ($manifest in $manifests) {
        $package = $null
        $section = ''
        $bin = $null
        foreach ($line in Get-Content -LiteralPath $manifest) {
            $text = $line.Trim()
            if ($text -match '^\[\[?([^\[\]]+)\]\]?$') {
                # The bin's own name comes before its `[dependencies]`, so one
                # ends where the next section starts.
                if ($bin -and $package -and $skip -notcontains $package -and -not ($found | Where-Object { $_.Bin -eq $bin })) {
                    $found += [pscustomobject]@{ Crate = $package; Bin = $bin }
                }
                $bin = $null
                $section = $Matches[1]
                continue
            }
            if ($text -match '^name\s*=\s*"([^"]+)"') {
                if ($section -eq 'package' -and -not $package) { $package = $Matches[1] }
                elseif ($section -eq 'bin') { $bin = $Matches[1] }
            }
        }
        if ($bin -and $package -and $skip -notcontains $package -and -not ($found | Where-Object { $_.Bin -eq $bin })) {
            $found += [pscustomobject]@{ Crate = $package; Bin = $bin }
        }
    }
    return $found
}

function Get-ZeronPin {
    # rust\zeron.toml, the one place the vendored tree's repo and rev are written
    # down; doctor.rs reads the same file so the two cannot disagree. Absent means
    # an older checkout with no path dependency on zeron.
    $pin = Join-Path $PSScriptRoot 'rust\zeron.toml'
    if (-not (Test-Path -LiteralPath $pin)) { return $null }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $pin) {
        $text = ($line -split '#')[0].Trim()
        if ($text -match '^([A-Za-z_]+)\s*=\s*"?([^" ]+)"?$') { $values[$Matches[1]] = $Matches[2] }
    }
    if (-not $values['repo'] -or -not $values['rev']) { return $null }
    return [pscustomobject]$values
}

function Install-VendoredTrees {
    # rust\zeron is in .gitignore: it is vendored, not tracked, and rust\gui links
    # it by path. Cargo resolves the whole workspace before building anything, so
    # without it every `cargo build -p ...` fails -- including the doctor's own,
    # which is the one command that would have explained why. A fresh clone,
    # therefore, has to fetch it, and that is this function's whole job.
    $pin = Get-ZeronPin
    if (-not $pin) { return }
    if (-not (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) {
        Warn "git not found, so the vendored zeron tree was not fetched (the GUI will not build)"
        return
    }

    $tree = Join-Path $PSScriptRoot 'rust\zeron'
    if (Test-Path -LiteralPath (Join-Path $tree 'crates\ui\Cargo.toml')) {
        $at = (& git -C $tree describe --tags --always 2>$null | Select-Object -First 1)
        if ($at -eq $pin.rev) {
            Say "vendored zeron is $($pin.rev)"
            return
        }
        # Rename rather than `git checkout`: a half-written tree from an
        # interrupted clone is not something to guess at, and nothing's work
        # lives in a vendored tree.
        $stale = Join-Path $PSScriptRoot 'rust\zeron.stale'
        if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Recurse -Force -ErrorAction SilentlyContinue }
        Move-Item -LiteralPath $tree -Destination $stale -Force
        Say "vendored zeron was $at, fetching $($pin.rev)"
    }

    Say "fetching the vendored zeron tree ($($pin.rev)) -- once, it is not in the repository"
    # Shallow: the pin is a tag, the history is never linked against.
    & git -C (Join-Path $PSScriptRoot 'rust') clone --quiet --depth 1 --branch $pin.rev -- $pin.repo zeron
    if ($LASTEXITCODE -ne 0) {
        Warn "could not fetch the vendored zeron tree -- the GUI will not build"
        return
    }
    Say "vendored zeron $($pin.rev)"
}

function Clear-StaleImages {
    # An upgrade replaces a frontend by renaming the running image aside, since
    # Windows will not let cargo write a file it has mapped. Windows also refuses
    # to delete such a copy while its process holds it -- so this is safe to run
    # unconditionally, and anything it cannot remove is still in use.
    $release = Join-Path $PSScriptRoot 'rust\target\release'
    if (-not (Test-Path -LiteralPath $release)) { return }
    Get-ChildItem -LiteralPath $release -Filter '*.exe.old-*' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # .running- is the name an earlier build used for the same copy.
    Get-ChildItem -LiteralPath $release -Filter '*.exe.running-*' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Find-VsDevCmd {
    # vswhere knows about instances a glob would miss; the glob still covers a
    # Build Tools install that never registered one.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $vswhere) {
        $hit = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find '**\Common7\Tools\VsDevCmd.bat' 2>$null |
            Select-Object -First 1
        if ($hit -and (Test-Path -LiteralPath $hit)) { return $hit }
    }
    foreach ($root in @("${env:ProgramFiles(x86)}\Microsoft Visual Studio", "$env:ProgramFiles\Microsoft Visual Studio")) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $hit = Get-ChildItem -Path (Join-Path $root '*\*\Common7\Tools\VsDevCmd.bat') -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Test-FileReplaceable([string]$Path) {
    # A running image is held with a share mode that refuses writes, so asking
    # for write access is the test. It never changes the file: the handle is
    # closed at once, and a file that is already gone is not one anybody holds.
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'Write', 'None')
        $stream.Close()
        return $true
    }
    catch { return $false }
}

function Move-RunningAside([string]$Path) {
    # Windows refuses to open a running image for write (`os error 5`), which is
    # what makes cargo fail when the frontend being rebuilt is the one the user
    # is running install.ps1 from. It does allow the file to be *renamed*: the
    # process keeps running from the moved image, unaffected, and a fresh binary
    # can be written at the old name. doctor.rs does the same thing for
    # `doctor --upgrade`; Clear-StaleImages takes these away again once whatever
    # held them has exited, and never touches one still in use.
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    if (Test-FileReplaceable $Path) { return $null }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
    $aside = "$Path.old-$stamp"
    try {
        Move-Item -LiteralPath $Path -Destination $aside -Force -ErrorAction Stop
        return $aside
    }
    catch { return $null }
}

function Build-Frontends([string]$VsDevCmd) {
    # One -p/--bin per cargo call: several bins in one call is the bug
    # `doctor --upgrade` hit. Keep going, like build.sh --keep-going: a GUI that
    # will not link is not a reason to leave somebody with no terminal.
    #
    # From rust\, because that is where the workspace manifest is: cargo reads
    # the workspace from the directory it stands in, and the repo root has no
    # Cargo.toml at all. One shared target directory either way, so the
    # artifacts still land in rust\target\release.
    $failed = @()
    $rust = Join-Path $PSScriptRoot 'rust'
    foreach ($frontend in Get-Frontends) {
        # Free the image first if this frontend is the one running install.ps1 --
        # cargo cannot write a running exe, and closing the terminal you are
        # installing from is not something to ask of anyone.
        $exe = Join-Path $rust "target\release\$($frontend.Bin).exe"
        $aside = Move-RunningAside $exe
        if ($aside) {
            Say "$($frontend.Bin).exe is running -- moved it to $(Split-Path -Leaf $aside) so the new one can be written"
        }
        $line = 'cd /d "{0}" && call "{1}" -arch=x64 >nul && cargo build --release -p {2} --bin {3}' -f $rust, $VsDevCmd, $frontend.Crate, $frontend.Bin
        # Not piped: a pipe makes PowerShell wait on every handle cargo hands
        # down, and cargo's progress output carries its own carriage returns.
        & cmd.exe /d /c $line
        if ($LASTEXITCODE -eq 0) {
            Say "built: $($frontend.Bin).exe"
        }
        else {
            $failed += $frontend.Bin
            # Windows cannot replace a running executable, so the usual reason a
            # rebuild of a working checkout fails is that the frontend being
            # rebuilt is the one the user is running in. Saying which one beats
            # an access-denied line from cargo.
            if ((Test-Path -LiteralPath $exe) -and -not (Test-FileReplaceable $exe)) {
                Warn "$($frontend.Bin).exe is running -- close it and run this again to update it"
            }
        }
    }
    return $failed
}

if ($build) {
    $vsDevCmd = Find-VsDevCmd
    if (-not $vsDevCmd) {
        Warn "Visual Studio Build Tools with the C++ workload not found, skipping the build"
        Warn "get it from https://aka.ms/vs/17/release/vs_BuildTools.exe and run this again"
        $build = $false
    }
    elseif (-not (Get-Command cargo -CommandType Application -ErrorAction SilentlyContinue)) {
        Warn "cargo not found, skipping the build -- get it from https://rustup.rs/"
        $build = $false
    }
    if ($build) {
        # The vendored tree first: cargo cannot even resolve the workspace without
        # it, so this has to happen before any -p build, doctor's own included.
        Install-VendoredTrees
        Clear-StaleImages
        Say "building frontends with $vsDevCmd"
        $failed = @(Build-Frontends -VsDevCmd $vsDevCmd)
        if ($failed.Count -gt 0) {
            # A warning and a non-zero exit, not a warning and success: the
            # python side is installed either way, but a run that built nothing
            # has not done what it was asked to.
            Warn "did not build: $($failed -join ', ') (see above)"
            $buildFailed = $true
        }
    }
}

# --- command -----------------------------------------------------------------
# The checkout's bin\ already holds cuacode.cmd, which finds the tree, the venv
# and the worker by itself; all it needs is to be on PATH. User scope only: the
# machine PATH needs an administrator, and this is one person's checkout.
function Get-UserPath {
    # Raw and unexpanded: writing back an expanded PATH would bake %USERPROFILE%
    # into it, and the next machine change would not reach it.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
    if (-not $key) { return '' }
    try {
        return [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    }
    finally { $key.Close() }
}

function Test-UserPath([string]$Directory) {
    $want = $Directory.TrimEnd('\')
    foreach ($entry in (Get-UserPath).Split(';')) {
        if ($entry.Trim().TrimEnd('\') -ieq $want) { return $true }
    }
    return $false
}

function Add-UserPath([string]$Directory) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    if (-not $key) {
        [Console]::Error.WriteLine('error: could not open HKCU\Environment, so none of this went on PATH')
        exit 1
    }
    try {
        # Keep whatever kind the value already is: turning an ExpandString into a
        # plain string would stop %USERPROFILE% expanding in it.
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        try { $kind = $key.GetValueKind('Path') } catch { }
        $raw = Get-UserPath
        $new = if ($raw.Trim()) { $raw.TrimEnd(';') + ';' + $Directory } else { $Directory }
        $key.SetValue('Path', $new, $kind)
    }
    finally { $key.Close() }

    # Without this, a terminal started from an Explorer that is already running
    # inherits the old PATH and `cuacode` is still not found.
    if (-not ('CuaCode.Environment' -as [type])) {
        Add-Type -Namespace CuaCode -Name Environment -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
'@
    }
    $result = [UIntPtr]::Zero
    [CuaCode.Environment]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 1000, [ref]$result) |
        Out-Null
}

$binDir = Join-Path $PSScriptRoot 'bin'
if (-not (Test-Path -LiteralPath (Join-Path $binDir 'cuacode.cmd'))) {
    Warn "bin\cuacode.cmd is missing, so there is no cuacode command to put on PATH"
}
elseif (Test-UserPath -Directory $binDir) {
    Say "$binDir is already on the user PATH"
}
else {
    Add-UserPath -Directory $binDir
    Say "added $binDir to the user PATH"
    Warn "open a new terminal before using cuacode, so it sees the new PATH"
}

# --- done --------------------------------------------------------------------
Write-Host
Say "done"
Write-Host "  run it:           cuacode          (or .\bin\quik.cmd from this checkout)"
Write-Host "  doctor:           cuacode doctor   (build, checkout, updates, worker)"
Write-Host "  api keys/models:  $env:USERPROFILE\.cuacode\config.json, written on first launch"
if (-not $build) { Write-Host "  build later:      .\install.ps1     (drop --no-build)" }
exit ([int]$buildFailed)
