param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'

function Find-TeamPython {
    $probes = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        # Enumerate installed interpreters instead of requesting a version:
        # newer Python managers may install a requested runtime on demand.
        try {
            $listed = & $launcher.Source -0p 2>$null
            foreach ($line in $listed) {
                if ($line -match '(?<pythonPath>[A-Za-z]:\\.*\\python.exe)\s*$') {
                    $probes += ,@($Matches.pythonPath)
                }
            }
        } catch { }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike '*\Microsoft\WindowsApps\python.exe') { $probes += ,@($python.Source) }
    foreach ($version in @('313', '314')) {
        $known = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
        if (Test-Path -LiteralPath $known -PathType Leaf) { $probes += ,@($known) }
    }
    foreach ($probe in $probes) {
        try {
            $binary = $probe[0]
            $prefix = @($probe | Select-Object -Skip 1)
            $raw = & $binary @prefix -I -c 'import json,sys;print(json.dumps(dict(path=sys.executable,version=list(sys.version_info[:3]))))' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $result = $raw | ConvertFrom-Json
                if ($result.version[0] -eq 3 -and $result.version[1] -ge 13) { return $result }
            }
        } catch { continue }
    }
    return $null
}

function Start-TeamSetup {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $teamPython = Find-TeamPython
    if ($CheckOnly) {
        [pscustomobject]@{ python = $teamPython; winget = [bool](Get-Command winget.exe -ErrorAction SilentlyContinue) } | ConvertTo-Json -Depth 5
        return
    }
    if (-not $teamPython) {
        Write-Host 'Python 3.13+ was not found. Python can be installed per-user through winget.'
        $choice = Read-Host 'Install Python 3.13 with winget? [y/N]'
        if ($choice -notin @('y', 'Y')) { throw 'Install Python 3.13 from https://www.python.org/downloads/windows/ and rerun install.cmd.' }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'winget is unavailable. Install Python 3.13 from https://www.python.org/downloads/windows/ and rerun.' }
        & $winget.Source install --id Python.Python.3.13 --exact --source winget --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'Python installation failed. No Hermes configuration was changed.' }
        $teamPython = Find-TeamPython
        if (-not $teamPython) { throw 'Python is installed but not detected yet. Open a new terminal and rerun install.cmd.' }
    }
    $venvRoot = Join-Path $repoRoot '.setup-venv'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & $teamPython.path -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the local setup environment.' }
    }
    & $venvPython -I -c 'import sys;assert sys.version_info >= (3,13)'
    if ($LASTEXITCODE -ne 0) { throw 'The existing .setup-venv uses an older Python. Move it aside and rerun install.cmd.' }
    $dependencyReady = $false
    try {
        & $venvPython -I -c "from importlib.metadata import version;assert version('ruamel.yaml')=='0.19.1'" 2>$null
        $dependencyReady = $LASTEXITCODE -eq 0
    } catch { $dependencyReady = $false }
    if (-not $dependencyReady) {
        & $venvPython -m pip install --disable-pip-version-check --only-binary=:all: --require-hashes -r (Join-Path $repoRoot 'requirements-setup.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Setup dependency installation failed. Check network access and rerun; Hermes configuration was not changed.' }
    }
    & $venvPython -X utf8 -B (Join-Path $PSScriptRoot 'windows_setup.py')
    $script:TeamSetupExit = $LASTEXITCODE
}

if ($MyInvocation.InvocationName -ne '.') {
    try { $script:TeamSetupExit = 0; Start-TeamSetup; exit $script:TeamSetupExit }
    catch { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }
}
