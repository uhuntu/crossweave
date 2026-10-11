<#
.SYNOPSIS
Installs Crossweave's commands (cw, cb, plus compatibility aliases) as native
.cmd launchers on PATH -- no Git Bash/WSL/Cygwin required.

Run after cloning:
  .\install.ps1

Or as a one-liner, which clones the repo first:
  irm https://raw.githubusercontent.com/uhuntu/crossweave/master/install.ps1 | iex
#>

$ErrorActionPreference = "Stop"
# PS 7.3+ otherwise turns any stderr output from the python/git probes below
# into a terminating error even on exit code 0; harmless to set on older
# versions, where this preference variable doesn't exist yet.
$PSNativeCommandUseErrorActionPreference = $false

$RepoUrl = if ($env:CROSSWEAVE_REPO_URL) { $env:CROSSWEAVE_REPO_URL } else { "https://github.com/uhuntu/crossweave.git" }
$BinDir = if ($env:CROSSWEAVE_BIN_DIR) { $env:CROSSWEAVE_BIN_DIR } elseif ($env:CLISWEAVE_BIN_DIR) { $env:CLISWEAVE_BIN_DIR } elseif ($env:AIMUX_BIN_DIR) { $env:AIMUX_BIN_DIR } else { Join-Path $HOME ".local\bin" }

# When run via `irm | iex` there's no script file on disk, so $PSCommandPath
# is empty -- guard the lookup instead of assuming a local clone, same idea
# as install.sh's BASH_SOURCE[0] check. The fetched clone is re-entered
# through iex as well, NOT as a file: & file.ps1 obeys the machine's
# execution policy, and Restricted -- the factory default on Windows
# client -- forbids every .ps1, which killed this one-liner right after
# the clone on exactly the machines it exists for (seen live 2026-10-11).
# An iex'd string carries no policy; CROSSWEAVE_INSTALL_FROM hands the
# re-entered pass its script directory, since $PSCommandPath is empty
# there too.
#
# Both passes run in the caller's own session when pasted into a live
# PowerShell, so this script may never call `exit`: it would close the
# window that ran it -- the "installer flashed away" report, which was
# the SUCCESS path flashing (seen live 2026-10-11). Failures throw (a
# loud red error; the session survives; -File still exits 1) and the
# re-entry returns. The env var is consumed on entry rather than cleaned
# up on exit, so a throw cannot leave it behind and a second run in the
# same window re-derives it -- otherwise the leftover var would silently
# skip the pull and relink stale code.
if ($PSCommandPath -and (Test-Path (Join-Path (Split-Path $PSCommandPath -Parent) "bin\cw"))) {
    $ScriptDir = Split-Path $PSCommandPath -Parent
} elseif ($env:CROSSWEAVE_INSTALL_FROM -and (Test-Path (Join-Path $env:CROSSWEAVE_INSTALL_FROM "bin\cw"))) {
    $ScriptDir = $env:CROSSWEAVE_INSTALL_FROM
    Remove-Item Env:\CROSSWEAVE_INSTALL_FROM -ErrorAction SilentlyContinue
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "crossweave: git is required to fetch the repo. Install Git for Windows and re-run."
    }
    $RepoDir = if ($env:CROSSWEAVE_REPO_DIR) { $env:CROSSWEAVE_REPO_DIR } elseif ($env:CLISWEAVE_REPO_DIR) { $env:CLISWEAVE_REPO_DIR } elseif ($env:AIMUX_REPO_DIR) { $env:AIMUX_REPO_DIR } else { Join-Path $HOME ".local\share\crossweave" }
    if (Test-Path (Join-Path $RepoDir ".git")) {
        git -C $RepoDir pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            # install.sh aborts here under set -e; silently installing
            # whatever the clone already had is the one outcome worse than
            # stopping, since the whole point of re-running is the update.
            throw "crossweave: could not fast-forward the clone at $RepoDir. Resolve the git error above (or point CROSSWEAVE_REPO_DIR somewhere fresh) and re-run."
        }
    } else {
        git clone --depth 1 $RepoUrl $RepoDir
    }
    $env:CROSSWEAVE_INSTALL_FROM = $RepoDir
    Invoke-Expression (Get-Content -Raw (Join-Path $RepoDir "install.ps1"))
    return
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# Windows ships `python.exe`/`py.exe`, not `python3.exe` -- and the Microsoft
# Store's `python3` PATH alias is a no-op stub that exits without doing
# anything even when a real interpreter is installed under a different name.
# Probe by actually running each candidate rather than trusting PATH presence.
function Get-WorkingPython {
    foreach ($cmd in @("python", "python3")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) {
            & $exe.Source -c "pass" 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return @{ Path = $exe.Source; Args = @() } }
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 -c "pass" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return @{ Path = $py.Source; Args = @("-3") } }
    }
    return $null
}

$Python = Get-WorkingPython
if (-not $Python) {
    throw "crossweave: no working Python 3 interpreter found (tried python, python3, py -3). Install Python from https://python.org and re-run."
}
$PyArgsStr = ($Python.Args -join " ")

# cw/cb are plain Python scripts with a `#!/usr/bin/env python3`
# shebang, which only Git Bash/WSL/msys know how to run. A .cmd launcher
# runs natively from both cmd.exe and PowerShell (PATHEXT covers .cmd), and
# always invokes the *current* file at $Target -- nothing is copied, so a
# later `git pull` in the clone is picked up automatically, same promise
# install.sh makes with its symlinks.
function Write-Launcher($Name, $Target) {
    $cmdPath = Join-Path $BinDir "$Name.cmd"
    $pyPart = if ($PyArgsStr) { "`"$($Python.Path)`" $PyArgsStr" } else { "`"$($Python.Path)`"" }
    $lines = @(
        "@echo off"
        "$pyPart `"$Target`" %*"
    )
    Set-Content -Path $cmdPath -Value $lines -Encoding oem
    Write-Host "linked $cmdPath -> $Target"
}

$CwPath = Join-Path $BinDir "cw.cmd"
if (Test-Path $CwPath) {
    Write-Warning "$CwPath already exists; leaving it unchanged"
} else {
    Write-Launcher "cw" (Join-Path $ScriptDir "bin\cw")
}
$CbPath = Join-Path $BinDir "cb.cmd"
if (Test-Path $CbPath) {
    Write-Warning "$CbPath already exists; leaving it unchanged"
} else {
    Write-Launcher "cb" (Join-Path $ScriptDir "bin\cb")
}
# crossweave is the self-named command; clisweave is last generation's
# compatibility alias. `ai` is created again on purpose: measured usage on
# the machines this tool is actually for is 98 `ai` invocations against 7
# `cw` ones (PSReadLine history, 2026-10-11) -- two renames never changed
# the hands, and a fresh install without it strands the muscle memory the
# whole compatibility layer exists to serve. aim/aimux are dropped from
# the list: zero uses in the same history, two generations old. ai-sessions
# stays deliberately unlinked -- an active PyPI package of the same name
# owns that word, and `cw sessions` / `cw resume` is the same surface.
Write-Launcher "crossweave" (Join-Path $ScriptDir "bin\cw")
Write-Launcher "clisweave" (Join-Path $ScriptDir "bin\cw")
Write-Launcher "ai" (Join-Path $ScriptDir "bin\cw")

$pathDirs = $env:PATH -split ";"
if ($pathDirs -notcontains $BinDir) {
    Write-Host "Note: $BinDir is not on your PATH. Add it, e.g.:"
    Write-Host "  [Environment]::SetEnvironmentVariable('PATH', `"$BinDir;`$env:PATH`", 'User')"
    Write-Host "  (then restart your terminal)"
}

Write-Host "Done. Try: cw --help"
