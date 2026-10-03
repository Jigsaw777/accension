$ErrorActionPreference = 'Stop'
$RouterRoot = Split-Path $PSScriptRoot -Parent
$RouterPython = Join-Path $RouterRoot '.venv\Scripts\python.exe'
try {
    $RouterStatus = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 2
    if ($RouterStatus.service -eq 'local-ai-router') { Write-Output 'Router already running'; exit 0 }
} catch {}
$RouterState = Join-Path $RouterRoot '.router'
New-Item -ItemType Directory -Path $RouterState -Force | Out-Null
Start-Process -FilePath $RouterPython -ArgumentList @('-m','local_ai_router.cli','--home',('"' + $RouterRoot + '"'),'serve') -WorkingDirectory $RouterRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $RouterState 'server.stdout.log') -RedirectStandardError (Join-Path $RouterState 'server.stderr.log') | Out-Null
Write-Output 'Router start requested on 127.0.0.1:8765'
