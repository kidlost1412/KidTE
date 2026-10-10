param(
    [Parameter(Mandatory = $true)][string]$Dist,
    [string]$Manifest,
    [string]$Python
)
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $Here "..\backend\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
}
$ArgsList = @((Join-Path $Here "release_manifest.py"), "verify", $Dist)
if ($Manifest) { $ArgsList += @("--manifest", $Manifest) }
& $Python @ArgsList
if ($LASTEXITCODE -ne 0) { Write-Error "Release verification FAILED"; exit 1 }
# TLS fallback bundle (net.py): without it a fresh PC can report "no network".
if (-not (Test-Path -LiteralPath (Join-Path $Dist "_internal\certifi\cacert.pem"))) {
    Write-Error "Release verification FAILED: _internal\certifi\cacert.pem is missing"; exit 1
}
Write-Host "Release verification passed"
exit 0
