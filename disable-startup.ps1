$ErrorActionPreference = 'Stop'
$Link = Join-Path ([Environment]::GetFolderPath('Startup')) 'OU Email Agent.lnk'
if (Test-Path $Link) { Remove-Item -LiteralPath $Link }
Write-Host 'Automatic sign-in startup disabled. A running monitor is not stopped.'
