param([switch]$PullModels)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path 'config.json')) {
    Copy-Item 'config.example.json' 'config.json'
    Write-Host 'Created config.json. Set your IMAP host, username and timezone, then run this installer again.'
    exit 0
}
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $Installed = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    if (Test-Path $Installed) { & $Installed -m venv .venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
    else { throw 'Install Python 3.11 or newer for Windows, then run this script again.' }
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $Python -m pip install -c requirements-lock.txt -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $Python -m ou_email_agent doctor
if ($LASTEXITCODE -ne 0) { throw 'Connection checks failed.' }
if ($PullModels) {
    & $Python -m ou_email_agent pull-models
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed.' }
}
Write-Host 'Next: open setup.cmd to enter your mailbox password privately.'
