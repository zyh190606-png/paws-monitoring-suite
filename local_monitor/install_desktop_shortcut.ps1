$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$launcher = Join-Path $appDir 'launcher.pyw'
$icon = Join-Path $appDir 'paws_local_monitor.ico'
$conda = 'D:\ProgramData\anaconda3\Scripts\conda.exe'
$pythonExe = (& $conda run --no-capture-output -n paws python -c "import sys; print(sys.executable)").Trim()
$pythonw = $pythonExe -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "找不到 paws 环境的 pythonw.exe：$pythonw" }
if (-not (Test-Path -LiteralPath $launcher)) { throw "找不到桌面启动器：$launcher" }
if (-not (Test-Path -LiteralPath $icon)) {
    & $conda run --no-capture-output -n paws python (Join-Path $appDir 'make_icon.py') | Out-Null
}
if (-not (Test-Path -LiteralPath $icon)) { throw "无法生成 PAWS 图标：$icon" }

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'PAWS本地运行监控.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $appDir
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = '手动选择并只读监控本机正在运行的 PAWS 程序'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output "已创建：$shortcutPath"
