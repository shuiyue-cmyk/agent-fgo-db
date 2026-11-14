$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python "$PSScriptRoot\fgo_kb.py" sync
