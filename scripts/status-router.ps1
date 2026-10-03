$RouterRoot = Split-Path $PSScriptRoot -Parent
& "$RouterRoot\.venv\Scripts\python.exe" -m local_ai_router.cli --home $RouterRoot status
