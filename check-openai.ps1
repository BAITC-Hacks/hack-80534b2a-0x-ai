$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Сначала выполните .\run.ps1 для установки зависимостей, затем остановите сервер Ctrl+C.'
}
if (-not $env:OPENAI_API_KEY) {
    $apiSecret = Read-Host 'OpenAI API key (ввод скрыт; ключ останется только в окружении этого терминала)' -AsSecureString
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiSecret)
    try {
        $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        $apiSecret.Dispose()
    }
}
& $pythonPath (Join-Path $projectRoot 'scripts\check_openai.py')
if ($LASTEXITCODE -ne 0) {
    throw 'Проверка OpenAI не пройдена. Причины записаны в openai-verification.json; ключ в отчёт не попадает.'
}
Write-Host 'Проверка пройдена. Запускайте .\run.ps1 в этом же терминале, чтобы приложение получило ключ.'
