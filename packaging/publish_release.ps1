<#
.SYNOPSIS
  Publishes a locally built Kid TE release to GitHub (run by the OWNER; Windows PowerShell 5.1).

.DESCRIPTION
  1. Checks the release folder (files present, SHA256SUMS.txt matches, catalog is signed JSON with a higher sequence).
  2. git push origin main.
  3. Creates the prerelease tag v<Version>-<Series> (if missing; default series p4) and uploads every file in the folder.
     Existing assets with the same size+sha256 are skipped; a DIFFERENT existing asset is an error
     (never overwritten on a version tag).
  4. Replaces kidte-catalog-v2.json on the release <CatalogTag> (default updates-beta-<Series>; CREATED as a
     prerelease when it does not exist yet, e.g. the first updates-beta-p4). Upload new, delete old, rename.
  5. Re-downloads everything anonymously (public URLs, not API) and verifies sha256.

  Idempotent: safe to re-run after a partial failure. All api.github.com calls are authenticated (token from
  `git credential fill` for github.com, never printed); only public file downloads are anonymous.
  Run from a worktree with -RepoPath <main checkout> to publish that checkout's HEAD and PhatHanh folder. -WhatIf prints the plan only (no token, no writes).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_release.ps1 -Version 1.15.0 -Series p4 -CatalogTag updates-beta-p4 -WhatIf
  powershell -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_release.ps1 -Version 1.15.0 -Series p4 -CatalogTag updates-beta-p4
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$Folder = '',
    [string]$Repo = 'kidlost1412/KidTE',
    [string]$Branch = 'main',
    [string]$Series = 'p4',
    [string]$CatalogTag = '',
    [string]$CatalogName = 'kidte-catalog-v2.json',
    [string]$RepoPath = '',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $RepoPath) { $RepoPath = Split-Path -Parent $PSScriptRoot }
if (-not $Folder) { $Folder = Join-Path $RepoPath ("PhatHanh\release-" + $Version) }
if (-not $CatalogTag) { $CatalogTag = "updates-beta-$Series" }
if (-not $Python) { $Python = Join-Path $RepoPath '.venv\Scripts\python.exe' }
$Tag = "v$Version-$Series"
$Api = "https://api.github.com/repos/$Repo"
$script:Token = $null
# -WhatIf must not leak into read-only cmdlets (Get-FileHash, downloads): remember it, then reset.
$DryRun = [bool]$WhatIfPreference
$WhatIfPreference = $false

function Say([string]$m) { Write-Host "[publish] $m" }
function Fail([string]$m) { throw "[publish] LOI: $m" }

function Get-Sha256([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

# True = really do it. In dry run prints the plan line and returns false.
function Confirm-Action([string]$target, [string]$action) {
    if ($DryRun) { Write-Host "What if: $action -> $target"; return $false }
    return $true
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

# Moi loi goi api.github.com / uploads.github.com deu dung token (ho muc 5000/gio, khong phai 60/gio theo IP).
# Chi cac lan TAI tep qua browser_download_url (Download-Anonymous) la an danh.
function Get-HttpStatus($err) {
    if ($err.Exception.Response) { try { return [int]$err.Exception.Response.StatusCode } catch { } }
    return 0
}

function Get-RateLimitMessage($err) {
    $status = Get-HttpStatus $err
    $remaining = $null; $reset = $null
    try {
        $h = $err.Exception.Response.Headers
        $remaining = $h['X-RateLimit-Remaining']
        $reset = $h['X-RateLimit-Reset']
        if (-not $reset -and $h['Retry-After']) { $reset = [string]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + [int]$h['Retry-After']) }
    } catch { }
    $isLimit = ($status -eq 429) -or ($status -eq 403 -and ($remaining -eq '0' -or "$($err.Exception.Message) $($err.ErrorDetails.Message)" -match '(?i)rate limit'))
    if (-not $isLimit) { return $null }
    $when = 'chua ro gio mo lai (thu lai sau ~1 gio)'
    if ($reset -and "$reset" -match '^\d+$') {
        $local = [DateTimeOffset]::FromUnixTimeSeconds([long]"$reset").ToLocalTime()
        $when = 'mo lai luc ' + $local.ToString('HH:mm:ss dd/MM/yyyy')
    }
    return "GitHub bao het luot goi API (HTTP $status). $when. Chay lai dung lenh nay la an toan: script bo qua phan da lam xong."
}

function Invoke-Gh([string]$method, [string]$url, $body = $null) {
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-publish'; Authorization = "Bearer $(Get-Token)" }
    $args2 = @{ Method = $method; Uri = $url; Headers = $headers }
    if ($null -ne $body) {
        $args2.Body = ($body | ConvertTo-Json -Depth 6)
        $args2.ContentType = 'application/json'
    }
    try { Invoke-RestMethod @args2 }
    catch {
        $msg = Get-RateLimitMessage $_
        if ($msg) { Fail $msg }
        throw
    }
}

function Get-ReleaseByTag([string]$tag) {
    try { return Invoke-Gh 'GET' "$Api/releases/tags/$tag" }
    catch {
        if ((Get-HttpStatus $_) -eq 404) { return $null }
        throw
    }
}

function Download-Anonymous([string]$url, [string]$dest) {
    # Plain GET, no Authorization header; GitHub redirects to its CDN.
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -Headers @{ 'User-Agent' = 'kidte-publish' }
}

function Upload-Asset($release, [string]$path, [string]$name) {
    $url = "https://uploads.github.com/repos/$Repo/releases/$($release.id)/assets?name=$([uri]::EscapeDataString($name))"
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-publish'; Authorization = "Bearer $(Get-Token)" }
    try { Invoke-RestMethod -Method Post -Uri $url -Headers $headers -InFile $path -ContentType 'application/octet-stream' -TimeoutSec 3600 }
    catch {
        $msg = Get-RateLimitMessage $_
        if ($msg) { Fail $msg }
        throw
    }
}

# ------------------------------------------------------------------ 1. kiem tra cuc bo
if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { Fail "khong thay thu muc $Folder" }
$files = Get-ChildItem -LiteralPath $Folder -File
if (-not $files) { Fail 'thu muc phat hanh rong' }
foreach ($f in $files) {
    if ($f.Name -match '(?i)\.env$|\.sqlite3?$|\.dpapi$|\.log$|\.jsonl$|\.pem$|\.key$') { Fail "tap tin khong duoc phep: $($f.Name)" }
}
$sumsFile = Join-Path $Folder 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $sumsFile)) { Fail 'thieu SHA256SUMS.txt' }
$expected = @{}
foreach ($line in Get-Content -LiteralPath $sumsFile) {
    if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$') { $expected[$Matches[2].Trim()] = $Matches[1].ToLowerInvariant() }
}
$local = @{}
foreach ($f in $files) {
    $local[$f.Name] = Get-Sha256 $f.FullName
    if ($f.Name -ne 'SHA256SUMS.txt' -and $expected.ContainsKey($f.Name) -and $expected[$f.Name] -ne $local[$f.Name]) {
        Fail "SHA256SUMS.txt khong khop: $($f.Name)"
    }
}
$catalogPath = Join-Path $Folder $CatalogName
if (-not (Test-Path -LiteralPath $catalogPath)) { Fail "thieu $CatalogName" }
$newCatalog = Get-Content -LiteralPath $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
$newSequence = [int]$newCatalog.signed.sequence
Say "Phien ban $Version, tag $Tag, catalog sequence $newSequence, $($files.Count) tap tin"

Push-Location $RepoPath
try {
    $branchNow = (git rev-parse --abbrev-ref HEAD).Trim()
    if ($branchNow -ne $Branch) { Fail "dang o nhanh '$branchNow', can '$Branch'" }
    $head = (git rev-parse HEAD).Trim()
    $ahead = (git rev-list --count "origin/$Branch..HEAD").Trim()
    $dirty = git status --porcelain --untracked-files=no
    if ($dirty) { Fail 'cay git con thay doi chua commit' }
    $sourceCommit = $newCatalog.signed.components | Where-Object { $_.id -eq 'core' } | ForEach-Object { $_.source_commit }
    if (-not $sourceCommit) { $sourceCommit = $newCatalog.signed.components | Where-Object { $_.id -eq 'app' } | ForEach-Object { $_.source_commit } }
    Say "HEAD $head, truoc origin $ahead commit; source_commit core trong catalog: $sourceCommit"

    # A4/A5: kiem chu ky catalog bang TRUSTED_KEYS, URL tung thanh phan (tep trong thu muc
    # hoac asset da co tren GitHub, HEAD an danh) va source_commit == HEAD. Chi doc, chay ca khi -WhatIf.
    if (-not (Test-Path -LiteralPath $Python)) { Fail "khong thay Python: $Python" }
    $checkScript = Join-Path $RepoPath 'packaging\update_release.py'
    $checkOut = & $Python -I $checkScript check-publish $catalogPath --release-dir $Folder --head $head --tag $Tag --repository $Repo
    $checkCode = $LASTEXITCODE
    foreach ($line in $checkOut) { Say $line }
    if ($checkCode -ne 0) { Fail 'catalog khong dat kiem tra truoc phat hanh (xem LOI o tren); KHONG thay catalog' }

    # ------------------------------------------------------------------ 2. push main
    if (Confirm-Action "origin/$Branch" "git push ($ahead commit)") {
        if ([int]$ahead -gt 0) {
            git push origin $Branch
            if ($LASTEXITCODE -ne 0) { Fail 'git push that bai' }
        } else { Say 'main da dong bo voi origin.' }
    }
} finally { Pop-Location }

# ------------------------------------------------------------------ 3. release + assets
$release = Get-ReleaseByTag $Tag
if (-not $release) {
    if (Confirm-Action $Tag 'tao prerelease') {
        $release = Invoke-Gh 'POST' "$Api/releases" @{
            tag_name = $Tag; target_commitish = $head; name = "Kid TE $Version ($Series)"
            prerelease = $true; draft = $false
            body = "Kid TE $Version ($Series). Prerelease; cap nhat qua kenh Thu nghiem."
        }
        Say "Da tao release $Tag"
    }
} else { Say "Release $Tag da co ($($release.assets.Count) asset)" }

foreach ($f in ($files | Sort-Object Length)) {
    $name = $f.Name
    $existing = $null
    if ($release) { $existing = $release.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1 }
    if ($existing) {
        $same = ($existing.size -eq $f.Length)
        if ($same -and $existing.digest) { $same = ($existing.digest -eq "sha256:$($local[$name])") }
        if ($same) { Say "bo qua $name (da co, khop)"; continue }
        Fail "asset $name da co tren $Tag nhung KHAC ban cuc bo; khong ghi de asset cua tag phien ban"
    }
    if (Confirm-Action "$Tag/$name" ("tai len {0:N1} MB" -f ($f.Length / 1MB))) {
        Upload-Asset $release $f.FullName $name | Out-Null
        Say "da tai len $name"
    }
}

# ------------------------------------------------------------------ 4. thay catalog
$feed = Get-ReleaseByTag $CatalogTag
if (-not $feed) {
    if (Confirm-Action $CatalogTag 'tao release feed (prerelease) vi tag chua ton tai') {
        $feed = Invoke-Gh 'POST' "$Api/releases" @{
            tag_name = $CatalogTag; target_commitish = $head; name = "Kid TE update feed $CatalogTag"
            prerelease = $true; draft = $false
            body = "Update feed ($CatalogTag). Chi chua $CatalogName; app doc catalog nay, khong tai tay."
        }
        Say "Da tao release feed $CatalogTag"
    }
} else { Say "Release feed $CatalogTag da co" }
$old = $null
if ($feed) { $old = $feed.assets | Where-Object { $_.name -eq $CatalogName } | Select-Object -First 1 }
$replace = $true
if ($old) {
    $tmpOld = Join-Path ([IO.Path]::GetTempPath()) ("kidte-old-" + [guid]::NewGuid().ToString('N') + '.json')
    try {
        Download-Anonymous $old.browser_download_url $tmpOld
        $oldHash = Get-Sha256 $tmpOld
        $oldSeq = [int]((Get-Content -LiteralPath $tmpOld -Raw -Encoding UTF8 | ConvertFrom-Json).signed.sequence)
    } finally { Remove-Item -LiteralPath $tmpOld -ErrorAction SilentlyContinue }
    if ($oldHash -eq $local[$CatalogName]) { Say "catalog tren $CatalogTag da la ban moi (sequence $oldSeq)"; $replace = $false }
    elseif ($newSequence -le $oldSeq) { Fail "sequence moi $newSequence khong lon hon catalog dang phat hanh $oldSeq" }
    else { Say "catalog dang phat hanh sequence $oldSeq -> thay bang $newSequence" }
}
if ($replace -and (Confirm-Action "$CatalogTag/$CatalogName" 'thay catalog (upload tam, xoa cu, doi ten)')) {
    $tmpName = "$CatalogName.new"
    $stale = $feed.assets | Where-Object { $_.name -eq $tmpName }
    foreach ($s in $stale) { Invoke-Gh 'DELETE' "$Api/releases/assets/$($s.id)" | Out-Null }
    $uploaded = Upload-Asset $feed $catalogPath $tmpName
    if ($old) { Invoke-Gh 'DELETE' "$Api/releases/assets/$($old.id)" | Out-Null }
    Invoke-Gh 'PATCH' "$Api/releases/assets/$($uploaded.id)" @{ name = $CatalogName } | Out-Null
    Say "da thay $CatalogName tren $CatalogTag"
}

# ------------------------------------------------------------------ 5. kiem tra lai (khong dang nhap)
if (-not (Confirm-Action 'GitHub' 'tai lai khong dang nhap va doi chieu sha256 tung asset + catalog')) {
    Say 'WhatIf: ket thuc, khong ghi gi.'
    return
}
$work = Join-Path ([IO.Path]::GetTempPath()) ("kidte-verify-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$bad = 0
try {
    # Tai thang theo URL cong khai (API release co the chua kip cap nhat ngay sau khi upload).
    foreach ($f in $files) {
        $dest = Join-Path $work $f.Name
        try { Download-Anonymous "https://github.com/$Repo/releases/download/$Tag/$($f.Name)" $dest }
        catch { Say "THIEU tren GitHub: $($f.Name)"; $bad++; continue }
        if ((Get-Sha256 $dest) -ne $local[$f.Name]) { Say "SAI sha256: $($f.Name)"; $bad++ } else { Say "OK $($f.Name)" }
        Remove-Item -LiteralPath $dest -Force
    }
    # CDN cua GitHub co the con phuc vu catalog cu vai giay sau khi thay: thu lai (backoff) toi ~60 giay.
    $dest = Join-Path $work $CatalogName
    $catalogOk = $false
    $waited = 0
    foreach ($delay in @(0, 2, 4, 8, 16, 30)) {
        if ($delay -gt 0) { Say "catalog chua khop, doi $delay giay roi kiem lai..."; Start-Sleep -Seconds $delay; $waited += $delay }
        try { Download-Anonymous ("https://github.com/$Repo/releases/download/$CatalogTag/$CatalogName" + "?cb=" + [guid]::NewGuid().ToString('N')) $dest } catch { continue }
        if ((Test-Path -LiteralPath $dest) -and (Get-Sha256 $dest) -eq $local[$CatalogName]) { $catalogOk = $true; break }
    }
    if ($catalogOk) { Say "OK catalog $CatalogTag sequence $newSequence" }
    else { Say "SAI catalog tren $CatalogTag (sau $waited giay)"; $bad++ }
} finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
if ($bad -gt 0) { Fail "$bad muc kiem tra that bai" }
Say "HOAN TAT: $Tag + $CatalogTag sequence $newSequence"
