$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости Python.' }

Set-Location $projectRoot
& $venvPython -m uvicorn main:app --host 127.0.0.1 --port 8000
