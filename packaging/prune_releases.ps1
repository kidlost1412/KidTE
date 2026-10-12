<#
.SYNOPSIS
  Deletes OLD GitHub releases (and their tags) of kidlost1412/KidTE that no catalog needs
  (Windows PowerShell 5.1). Dry run by default.

.DESCRIPTION
  1. Downloads the signed catalogs anonymously (updates-beta-p3 required; stable-p3 and the p4 feeds optional, 404 ignored).
  2. Lists all releases anonymously (GET API, no token).
  3. Calls release_prune_plan.py (python -I) to decide KEEP / DELETE per release:
       KEEP    tag referenced by a catalog component url
       KEEP    channel feed tag (updates-*)
       KEEP    release holding the newest installer (Setup exe)
       KEEP    newest -KeepCore core versions (v<X.Y.Z>-p3 / -p4)
       DELETE  everything else
  4. Prints the KEEP/DELETE table. Without -Apply nothing is changed.
  5. With -Apply: asks for typed confirmation "XOA" (skipped with -Force), then for each DELETE row
     deletes the release and then the tag ref. The GitHub token comes from `git credential fill`
     for github.com (same helper as publish_release.ps1) and is never printed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File D:\KidTE-wt\gh-prune\packaging\prune_releases.ps1
  powershell -ExecutionPolicy Bypass -File D:\KidTE-wt\gh-prune\packaging\prune_releases.ps1 -KeepCore 3 -Apply
#>
[CmdletBinding()]
param(
    [int]$KeepCore = 2,
    [switch]$Apply,
    [switch]$Force,
    [string]$Repo = 'kidlost1412/KidTE',
    [string]$CatalogName = 'kidte-catalog-v2.json',
    [string[]]$CatalogTags = @('updates-beta-p3', 'updates-stable-p3', 'updates-beta-p4', 'updates-stable-p4'),
    [string]$Python = 'D:\KidTE-NguonMa\.venv\Scripts\python.exe',
    [string]$RepoPath = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $RepoPath) { $RepoPath = Split-Path -Parent $PSScriptRoot }
$PlanScript = Join-Path $PSScriptRoot 'release_prune_plan.py'
$Api = "https://api.github.com/repos/$Repo"
$script:Token = $null

function Say([string]$m) { Write-Host "[prune] $m" }
function Fail([string]$m) { throw "[prune] LOI: $m" }

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
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'kidte-prune' }
    if (-not $Anonymous) { $headers.Authorization = "Bearer $(Get-Token)" }
    $args2 = @{ Method = $method; Uri = $url; Headers = $headers }
    if ($null -ne $body) {
        $args2.Body = ($body | ConvertTo-Json -Depth 6)
        $args2.ContentType = 'application/json'
    }
    Invoke-RestMethod @args2
}

function Download-Anonymous([string]$url, [string]$dest) {
    # Plain GET, no Authorization header.
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -Headers @{ 'User-Agent' = 'kidte-prune' }
}

# ------------------------------------------------------------------ 0. thu muc tam
if (-not (Test-Path -LiteralPath $PlanScript)) { Fail "khong thay $PlanScript" }
if (-not (Test-Path -LiteralPath $Python)) { Fail "khong thay python: $Python" }
$work = Join-Path ([IO.Path]::GetTempPath()) ("kidte-prune-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null

try {
    # ------------------------------------------------------------------ 1. tai catalog (cong khai)
    $catalogPaths = @()
    foreach ($tag in $CatalogTags) {
        $dest = Join-Path $work ("$tag-" + $CatalogName)
        $url = "https://github.com/$Repo/releases/download/$tag/$CatalogName"
        try {
            Download-Anonymous $url $dest
            $catalogPaths += $dest
            Say "tai catalog $tag"
        } catch {
            if ($tag -eq $CatalogTags[0]) { Fail "khong tai duoc catalog bat buoc $tag ($($_.Exception.Message))" }
            Say "bo qua $tag (khong co catalog)"
        }
    }

    # ------------------------------------------------------------------ 2. liet ke release (cong khai, phan trang)
    $all = @()
    $page = 1
    while ($true) {
        $batch = Invoke-Gh 'GET' "$Api/releases?per_page=100&page=$page" -Anonymous
        if (-not $batch -or @($batch).Count -eq 0) { break }
        $all += @($batch)
        if (@($batch).Count -lt 100) { break }
        $page++
    }
    Say "$($all.Count) release tren GitHub"
    $relPath = Join-Path $work 'releases.json'
    $relJson = ConvertTo-Json -InputObject $all -Depth 10
    [IO.File]::WriteAllText($relPath, $relJson, (New-Object Text.UTF8Encoding($false)))

    # ------------------------------------------------------------------ 3. quyet dinh KEEP/DELETE (python)
    $planPath = Join-Path $work 'plan.json'
    $pyArgs = @('-I', $PlanScript, '--releases', $relPath)
    foreach ($c in $catalogPaths) { $pyArgs += @('--catalog', $c) }
    $pyArgs += @('--keep-core', [string]$KeepCore, '--plan-out', $planPath)
    & $Python @pyArgs
    if ($LASTEXITCODE -ne 0) { Fail 'release_prune_plan.py that bai' }
    # PS 5.1 quirk: ConvertFrom-Json can hand back the whole array wrapped as ONE element; unwrap it.
    $plan = @(ConvertFrom-Json -InputObject (Get-Content -LiteralPath $planPath -Raw -Encoding UTF8))
    if ($plan.Count -eq 1 -and $plan[0] -is [array]) { $plan = @($plan[0]) }

    # ------------------------------------------------------------------ 4. an toan
    $deletes = @($plan | Where-Object { $_.decision -eq 'DELETE' })
    $keeps = @($plan | Where-Object { $_.decision -eq 'KEEP' })
    if ($keeps.Count -eq 0) { Fail 'khong co release nao duoc KEEP - dung lai de tranh xoa nham' }
    foreach ($d in $deletes) {
        if (-not $d.tag -or $d.tag -like 'updates-*') { Fail "tu choi xoa tag '$($d.tag)'" }
        if ($d.reason -like 'referenced*' -or $d.reason -like 'newest*') { Fail "tu choi xoa $($d.tag): $($d.reason)" }
    }
    if ($deletes.Count -eq 0) { Say 'Khong co release cu nao can xoa.'; return }

    # ------------------------------------------------------------------ 5. xac nhan va xoa
    if (-not $Apply) {
        Say "DRY RUN: se xoa $($deletes.Count) release (chay lai voi -Apply de xoa that)."
        return
    }
    if (-not $Force) {
        Say "Se XOA VINH VIEN $($deletes.Count) release + tag: $((@($deletes | ForEach-Object { $_.tag })) -join ', ')"
        $typed = Read-Host 'Go chinh xac XOA de xac nhan (bat ky chu khac = huy)'
        if ($typed -cne 'XOA') { Say 'Da huy, khong xoa gi.'; return }
    }
    $failed = 0
    foreach ($d in $deletes) {
        try {
            Invoke-Gh 'DELETE' "$Api/releases/$($d.id)" | Out-Null
            Say "da xoa release $($d.tag) (id $($d.id))"
        } catch {
            Say "LOI xoa release $($d.tag): $($_.Exception.Message)"; $failed++; continue
        }
        try {
            Invoke-Gh 'DELETE' "$Api/git/refs/tags/$($d.tag)" | Out-Null
            Say "da xoa tag $($d.tag)"
        } catch {
            $code = $null
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            if ($code -eq 404) { Say "tag $($d.tag) da khong con" } else { Say "LOI xoa tag $($d.tag): $($_.Exception.Message)"; $failed++ }
        }
    }
    if ($failed -gt 0) { Fail "$failed muc that bai" }
    Say "HOAN TAT: da xoa $($deletes.Count) release cu."
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
