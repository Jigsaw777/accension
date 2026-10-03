param([switch]$InstallClients)
$ErrorActionPreference = 'Stop'
$RouterRoot = $PSScriptRoot
if (-not (Test-Path "$RouterRoot\.venv\Scripts\python.exe")) { python -m venv "$RouterRoot\.venv" }
& "$RouterRoot\.venv\Scripts\python.exe" -m pip install -e "$RouterRoot[test,azure]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& "$RouterRoot\.venv\Scripts\python.exe" -m local_ai_router.cli --home $RouterRoot config validate
& "$RouterRoot\.venv\Scripts\python.exe" "$RouterRoot\scripts\install_clients.py"
if ($InstallClients) {
    & "$RouterRoot\.venv\Scripts\python.exe" "$RouterRoot\scripts\install_clients.py" --apply
    if ($LASTEXITCODE -ne 0) { throw 'Client integration failed' }
}
& "$RouterRoot\scripts\start-router.ps1"
