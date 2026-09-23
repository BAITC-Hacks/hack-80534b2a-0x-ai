param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

$caBundlePath = Join-Path $env:TEMP ('career-quest-ca-' + [Guid]::NewGuid().ToString('N') + '.pem')
try {
    python -c "import ssl; print(''.join(ssl.DER_cert_to_PEM_cert(cert) for cert, encoding, trust in ssl.enum_certificates('ROOT') if encoding == 'x509_asn'))" | Out-File -LiteralPath $caBundlePath -Encoding ascii
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Windows root certificates.' }
    & $venvPython -m pip install --cert $caBundlePath --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Python dependencies.' }
}
finally {
    Remove-Item -LiteralPath $caBundlePath -Force -ErrorAction SilentlyContinue
}

Set-Location $projectRoot
& $venvPython (Join-Path $projectRoot 'scripts\start.py') --port $Port
