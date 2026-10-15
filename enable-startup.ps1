$ErrorActionPreference = 'Stop'
$Python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $Python)) { throw 'Install the local Python environment first.' }
$Config = Join-Path $PSScriptRoot 'config.json'
if (-not (Test-Path $Config)) { throw 'Configure and verify the mailbox before enabling startup.' }
$Startup = [Environment]::GetFolderPath('Startup')
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut((Join-Path $Startup 'OU Email Agent.lnk'))
$Shortcut.TargetPath = $Python
$Shortcut.Arguments = '-m ou_email_agent --config "' + $Config + '" monitor'
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = 'Local email sync and configured indexing schedule'
$Shortcut.Save()
Write-Host 'OU Email Agent will start at Windows sign-in. Logs: data\monitor.log'
