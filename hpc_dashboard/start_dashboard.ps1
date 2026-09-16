$ErrorActionPreference = 'Stop'
$conda = 'D:\ProgramData\anaconda3\Scripts\conda.exe'
$app = Join-Path $PSScriptRoot 'app.py'
& $conda run --no-capture-output -n paws python $app

