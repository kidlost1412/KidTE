<#
.SYNOPSIS
  Publishes a locally built Kid TE release to GitHub (run by the OWNER; Windows PowerShell 5.1).

.DESCRIPTION
  1. Checks the release folder (files present, SHA256SUMS.txt matches, catalog is signed JSON with a higher sequence).
  2. git push origin main.
  3. Creates the prerelease tag v<Version>-p3 (if missing) and uploads every file in the folder.
     Existing assets with the same size+sha256 are skipped; a DIFFERENT existing asset is an error
     (never overwritten on a version tag).
  4. Replaces kidte-catalog-v2.json on the existing release <CatalogTag> (upload new, delete old, rename).
  5. Re-downloads everything anonymously and verifies sha256.

  Idempotent: safe to re-run after a partial failure. The GitHub token comes from `git credential fill`
  for github.com and is never printed. -WhatIf prints the plan only (no token, no writes).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_release.ps1 -Version 1.14.258 -WhatIf
  powershell -ExecutionPolicy Bypass -File D:\KidTE-NguonMa\packaging\publish_release.ps1 -Version 1.14.258
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$Folder = '',
    [string]$Repo = 'kidlost1412/KidTE',
    [string]$Branch = 'main',
    [string]$Series = 'p3',
    [string]$CatalogTag = 'updates-beta-p3',
    [string]$CatalogName = 'kidte-catalog-v2.json',
    [string]$RepoPath = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $RepoPath) { $RepoPath = Split-Path -Parent $PSScriptRoot }
if (-not $Folder) { $Folder = Join-Path $RepoPath ("PhatHanh\release-" + $Version) }
$Tag = "v$Version-$Series"
$Api = "https://api.github.com/repos/$Repo"
$script:Token = $null

function Say([string]$m) { Write-Host "[publish] $m" }
function Fail([string]$m) { throw "[publish] LOI: $m" }

function Get-Sha256([string]$path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
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
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-publish' }
    if (-not $Anonymous) { $headers.Authorization = "Bearer $(Get-Token)" }
    $args2 = @{ Method = $method; Uri = $url; Headers = $headers }
    if ($null -ne $body) {
        $args2.Body = ($body | ConvertTo-Json -Depth 6)
        $args2.ContentType = 'application/json'
    }
    Invoke-RestMethod @args2
}

function Get-ReleaseByTag([string]$tag) {
    try { return Invoke-Gh 'GET' "$Api/releases/tags/$tag" -Anonymous }
    catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { return $null }
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
    Invoke-RestMethod -Method Post -Uri $url -Headers $headers -InFile $path -ContentType 'application/octet-stream' -TimeoutSec 3600
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
    Say "HEAD $head, truoc origin $ahead commit; source_commit core trong catalog: $sourceCommit"

    # ------------------------------------------------------------------ 2. push main
    if ($PSCmdlet.ShouldProcess("origin/$Branch", "git push ($ahead commit)")) {
        if ([int]$ahead -gt 0) {
            git push origin $Branch
            if ($LASTEXITCODE -ne 0) { Fail 'git push that bai' }
        } else { Say 'main da dong bo voi origin.' }
    }
} finally { Pop-Location }

# ------------------------------------------------------------------ 3. release + assets
$release = Get-ReleaseByTag $Tag
if (-not $release) {
    if ($PSCmdlet.ShouldProcess($Tag, 'tao prerelease')) {
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
    if ($PSCmdlet.ShouldProcess("$Tag/$name", ("tai len {0:N1} MB" -f ($f.Length / 1MB)))) {
        Upload-Asset $release $f.FullName $name | Out-Null
        Say "da tai len $name"
    }
}

# ------------------------------------------------------------------ 4. thay catalog
$feed = Get-ReleaseByTag $CatalogTag
if (-not $feed) { Fail "khong thay release feed $CatalogTag" }
$old = $feed.assets | Where-Object { $_.name -eq $CatalogName } | Select-Object -First 1
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
if ($replace -and $PSCmdlet.ShouldProcess("$CatalogTag/$CatalogName", "thay catalog (upload tam, xoa cu, doi ten)")) {
    $tmpName = "$CatalogName.new"
    $stale = $feed.assets | Where-Object { $_.name -eq $tmpName }
    foreach ($s in $stale) { Invoke-Gh 'DELETE' "$Api/releases/assets/$($s.id)" | Out-Null }
    $uploaded = Upload-Asset $feed $catalogPath $tmpName
    if ($old) { Invoke-Gh 'DELETE' "$Api/releases/assets/$($old.id)" | Out-Null }
    Invoke-Gh 'PATCH' "$Api/releases/assets/$($uploaded.id)" @{ name = $CatalogName } | Out-Null
    Say "da thay $CatalogName tren $CatalogTag"
}

# ------------------------------------------------------------------ 5. kiem tra lai (khong dang nhap)
if (-not $PSCmdlet.ShouldProcess("GitHub", 'tai lai khong dang nhap va doi chieu sha256')) {
    Say 'WhatIf: ket thuc, khong ghi gi.'
    return
}
$work = Join-Path ([IO.Path]::GetTempPath()) ("kidte-verify-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$bad = 0
try {
    $release = Get-ReleaseByTag $Tag
    foreach ($f in $files) {
        $asset = $release.assets | Where-Object { $_.name -eq $f.Name } | Select-Object -First 1
        if (-not $asset) { Say "THIEU tren GitHub: $($f.Name)"; $bad++; continue }
        $dest = Join-Path $work $f.Name
        Download-Anonymous $asset.browser_download_url $dest
        if ((Get-Sha256 $dest) -ne $local[$f.Name]) { Say "SAI sha256: $($f.Name)"; $bad++ } else { Say "OK $($f.Name)" }
        Remove-Item -LiteralPath $dest -Force
    }
    $feed = Get-ReleaseByTag $CatalogTag
    $asset = $feed.assets | Where-Object { $_.name -eq $CatalogName } | Select-Object -First 1
    $dest = Join-Path $work $CatalogName
    Download-Anonymous "https://github.com/$Repo/releases/download/$CatalogTag/$CatalogName" $dest
    if ($asset -and (Get-Sha256 $dest) -eq $local[$CatalogName]) { Say "OK catalog $CatalogTag sequence $newSequence" }
    else { Say "SAI catalog tren $CatalogTag"; $bad++ }
} finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
if ($bad -gt 0) { Fail "$bad muc kiem tra that bai" }
Say "HOAN TAT: $Tag + $CatalogTag sequence $newSequence"
