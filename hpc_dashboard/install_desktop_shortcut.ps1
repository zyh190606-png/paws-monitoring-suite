$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$launcher = Join-Path $appDir 'launcher.pyw'
$icon = Join-Path $appDir 'paws_dashboard.ico'
$conda = 'D:\ProgramData\anaconda3\Scripts\conda.exe'
$pythonExe = (& $conda run --no-capture-output -n paws python -c "import sys; print(sys.executable)").Trim()
$pythonw = $pythonExe -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "找不到 paws 环境的 pythonw.exe：$pythonw" }
if (-not (Test-Path -LiteralPath $icon)) { throw "找不到图标文件：$icon" }
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'PAWS HPC监控面板.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $appDir
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = '启动PAWS HPC本地监控面板并打开默认浏览器'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output "已创建：$shortcutPath"

