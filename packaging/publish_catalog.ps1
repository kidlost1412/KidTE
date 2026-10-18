<#
.SYNOPSIS
  Publishes ONLY a renewed catalog (kidte-catalog-v2.json) to the feed release (run by the OWNER; Windows PowerShell 5.1).

.DESCRIPTION
  Use after: update_release.py renew-catalog -> update_release.py sign (see docs\GIA-HAN-CATALOG.md).
  1. Downloads the currently published catalog anonymously.
  2. update_release.py verify-renewed: signature (pinned TRUSTED_KEYS), verify_catalog(minimum_sequence = old+1),
     sequence = old+1, components IDENTICAL to the published ones, later expiry.
  3. update_release.py check-publish (empty release dir): every component URL must already exist on GitHub (anonymous HEAD).
  4. Replaces ONLY the asset kidte-catalog-v2.json on the feed tag (upload new, delete old, rename).
     No version tag is created, no git push, no other asset touched.
  5. Re-downloads anonymously and compares sha256.

  Steps 1-3 are read-only and also run with -WhatIf. -WhatIf stops before any write (and never reads the git token).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_catalog.ps1 -Folder D:\KidTE-NguonMa\PhatHanh\renew-catalog -WhatIf
  powershell -NoProfile -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_catalog.ps1 -Folder D:\KidTE-NguonMa\PhatHanh\renew-catalog
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [string]$Repo = 'kidlost1412/KidTE',
    [string]$CatalogTag = 'updates-beta-p4',
    [string]$CatalogName = 'kidte-catalog-v2.json',
    [string]$Channel = 'beta',
    [string]$RepoPath = '',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $RepoPath) { $RepoPath = Split-Path -Parent $PSScriptRoot }
if (-not $Python) { $Python = Join-Path $RepoPath '.venv\Scripts\python.exe' }
$Api = "https://api.github.com/repos/$Repo"
$script:Token = $null
$DryRun = [bool]$WhatIfPreference
$WhatIfPreference = $false

function Say([string]$m) { Write-Host "[catalog] $m" }
function Fail([string]$m) { throw "[catalog] LOI: $m" }

function Get-Sha256([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Get-Token {
    if ($script:Token) { return $script:Token }
    $request = "protocol=https`nhost=github.com`n`n"
    $out = $request | git credential fill
    if ($LASTEXITCODE -ne 0) { Fail 'git credential fill that bai (chua dang nhap GitHub?)' }
    foreach ($line in $out) { if ($line -like 'password=*') { $script:Token = $line.Substring(9) } }
    if (-not $script:Token) { Fail 'khong lay duoc token tu git credential' }
    return $script:Token
}

function Invoke-Gh([string]$method, [string]$url, $body = $null, [switch]$Anonymous) {
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-publish-catalog' }
    if (-not $Anonymous) { $headers.Authorization = "Bearer $(Get-Token)" }
    $args2 = @{ Method = $method; Uri = $url; Headers = $headers }
    if ($null -ne $body) {
        $args2.Body = ($body | ConvertTo-Json -Depth 6)
        $args2.ContentType = 'application/json'
    }
    Invoke-RestMethod @args2
}

function Download-Anonymous([string]$url, [string]$dest) {
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -Headers @{ 'User-Agent' = 'kidte-publish-catalog' }
}

# ------------------------------------------------------------------ 1. kiem tra cuc bo
$catalogPath = Join-Path $Folder $CatalogName
if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) { Fail "khong thay $catalogPath (da chay renew-catalog + sign chua?)" }
if (-not (Test-Path -LiteralPath $Python)) { Fail "khong thay Python: $Python" }
$script = Join-Path $RepoPath 'packaging\update_release.py'
$newCatalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
$newSequence = [int]$newCatalog.signed.sequence
if ($newCatalog.signed.channel -ne $Channel) { Fail "catalog thuoc kenh '$($newCatalog.signed.channel)', can '$Channel'" }
Say "Catalog moi: sequence $newSequence, het han $($newCatalog.signed.expires_at)"

$work = Join-Path ([IO.Path]::GetTempPath()) ("kidte-catalog-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    # ------------------------------------------------------------------ 2. catalog dang cong bo (an danh)
    $feed = $null
    try { $feed = Invoke-Gh 'GET' "$Api/releases/tags/$CatalogTag" -Anonymous }
    catch { Fail "khong doc duoc release feed $CatalogTag ($($_.Exception.Message)); khong tao tag moi" }
    $old = $feed.assets | Where-Object { $_.name -eq $CatalogName } | Select-Object -First 1
    if (-not $old) { Fail "feed $CatalogTag chua co $CatalogName - day khong phai viec gia han; dung publish_release.ps1" }
    $publishedPath = Join-Path $work 'published.json'
    Download-Anonymous $old.browser_download_url $publishedPath
    $oldHash = Get-Sha256 $publishedPath
    $oldSeq = [int]((Get-Content -LiteralPath $publishedPath -Raw -Encoding UTF8 | ConvertFrom-Json).signed.sequence)
    Say "Catalog dang cong bo tren $CatalogTag : sequence $oldSeq"
    if ($oldHash -eq (Get-Sha256 $catalogPath)) { Say 'Catalog tren GitHub da la ban nay; khong lam gi.'; return }

    # ------------------------------------------------------------------ 3. verify (chi doc; chay ca khi -WhatIf)
    $out = & $Python -I $script verify-renewed $catalogPath --published $publishedPath --channel $Channel
    $code = $LASTEXITCODE
    foreach ($line in $out) { Say $line }
    if ($code -ne 0) { Fail 'catalog gia han khong dat verify-renewed; KHONG thay catalog' }

    $emptyDir = Join-Path $work 'empty'
    New-Item -ItemType Directory -Path $emptyDir | Out-Null
    $head = (git -C $RepoPath rev-parse HEAD).Trim()
    $out = & $Python -I $script check-publish $catalogPath --release-dir $emptyDir --head $head --repository $Repo
    $code = $LASTEXITCODE
    foreach ($line in $out) { Say $line }
    if ($code -ne 0) { Fail 'check-publish that bai (URL thanh phan khong ton tai tren GitHub?); KHONG thay catalog' }

    # ------------------------------------------------------------------ 4. thay chi asset catalog
    if ($DryRun) {
        Write-Host "What if: thay $CatalogTag/$CatalogName (sequence $oldSeq -> $newSequence); khong tao tag, khong push"
        Say 'WhatIf: ket thuc, khong ghi gi.'
        return
    }
    $tmpName = "$CatalogName.new"
    foreach ($s in @($feed.assets | Where-Object { $_.name -eq $tmpName })) { Invoke-Gh 'DELETE' "$Api/releases/assets/$($s.id)" | Out-Null }
    $url = "https://uploads.github.com/repos/$Repo/releases/$($feed.id)/assets?name=$([uri]::EscapeDataString($tmpName))"
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-publish-catalog'; Authorization = "Bearer $(Get-Token)" }
    $uploaded = Invoke-RestMethod -Method Post -Uri $url -Headers $headers -InFile $catalogPath -ContentType 'application/octet-stream' -TimeoutSec 600
    Invoke-Gh 'DELETE' "$Api/releases/assets/$($old.id)" | Out-Null
    Invoke-Gh 'PATCH' "$Api/releases/assets/$($uploaded.id)" @{ name = $CatalogName } | Out-Null
    Say "da thay $CatalogName tren $CatalogTag"

    # ------------------------------------------------------------------ 5. kiem tra lai (khong dang nhap)
    $dest = Join-Path $work 'after.json'
    $expectedHash = Get-Sha256 $catalogPath
    $ok = $false
    $waited = 0
    foreach ($delay in @(0, 2, 4, 8, 16, 30)) {
        if ($delay -gt 0) { Say "catalog chua khop, doi $delay giay roi kiem lai..."; Start-Sleep -Seconds $delay; $waited += $delay }
        try { Download-Anonymous ("https://github.com/$Repo/releases/download/$CatalogTag/$CatalogName" + "?cb=" + [guid]::NewGuid().ToString('N')) $dest } catch { continue }
        if ((Test-Path -LiteralPath $dest) -and (Get-Sha256 $dest) -eq $expectedHash) { $ok = $true; break }
    }
    if (-not $ok) { Fail "catalog tren $CatalogTag khong khop sha256 sau $waited giay" }
    Say "HOAN TAT: $CatalogTag sequence $newSequence, het han $($newCatalog.signed.expires_at)"
} finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
