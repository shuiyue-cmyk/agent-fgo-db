param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Query,
    [int]$Limit = 8
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python "$PSScriptRoot\fgo_kb.py" search $Query --limit $Limit --format markdown
