param(
    [ValidateSet("CPU", "GPU")]
    [string]$Profile = "CPU",
    [string]$Python,
    [string]$CaptchaPython,
    [ValidatePattern('^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')]
    [string]$ReleaseVersion,
    [ValidatePattern('^[a-z0-9-]*$')]
    [string]$ArtifactLabel = "",
    [switch]$SkipInstaller,
    [switch]$SkipTests,
    [switch]$KeepBuild,
    [switch]$BuildComponents
)
$ErrorActionPreference = "Stop"
$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $PythonCandidates = @(
        (Join-Path $Project ".venv\Scripts\python.exe"),
        (Join-Path $Project "backend\.venv\Scripts\python.exe")
    )
    $Python = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $Python) {
    throw "Python not found. Pass -Python <path> or create .venv / backend\.venv."
}
try { $Python = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path }
catch { throw "Python executable not found: $Python" }
# Build the optional CAPTCHA package in its audited solver environment.
# The CPU core environment intentionally omits
# CAPTCHA build dependencies; do not pip install them into it. Existing builds
# keep working because the sidecar defaults to core Python.
if (-not $CaptchaPython) { $CaptchaPython = $Python }
try { $CaptchaPython = (Resolve-Path -LiteralPath $CaptchaPython -ErrorAction Stop).Path }
catch { throw "CAPTCHA Python executable not found: $CaptchaPython" }
$ProfileSlug = $Profile.ToLowerInvariant()
$ArtifactProfile = if ($ArtifactLabel) { "$ProfileSlug-$ArtifactLabel" } else { $ProfileSlug }
$Dist = Join-Path $Project "dist"
$Build = Join-Path $Project "build"

if (-not $SkipTests) {
    & $Python (Join-Path $Project "packaging\apply_theme.py") --check
    if ($LASTEXITCODE -ne 0) { throw "Theme assets need generation" }
    & $Python (Join-Path $Project "backend\tools\run_tests.py") --suite all
    if ($LASTEXITCODE -ne 0) { throw "Project tests failed" }
}
$env:KIDTE_BUILD_PROFILE = $ProfileSlug
if ($ReleaseVersion) { $env:KIDTE_RELEASE_VERSION = $ReleaseVersion }
else { Remove-Item Env:KIDTE_RELEASE_VERSION -ErrorAction SilentlyContinue }
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build (Join-Path $PSScriptRoot "KidTE.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
& $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build (Join-Path $PSScriptRoot "Updater.spec")
if ($LASTEXITCODE -ne 0) { throw "Launcher PyInstaller failed" }
Copy-Item -LiteralPath (Join-Path $Dist "KidTELauncher.exe") -Destination (Join-Path $Dist "KidTE\KidTELauncher.exe")
if ($BuildComponents) {
    & $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build (Join-Path $PSScriptRoot "Transcription.spec")
    if ($LASTEXITCODE -ne 0) { throw "Transcription worker PyInstaller failed" }
    & $CaptchaPython -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build (Join-Path $PSScriptRoot "KidTECaptcha.spec")
    if ($LASTEXITCODE -ne 0) { throw "CAPTCHA sidecar PyInstaller failed" }
    $ComponentOutput = Join-Path $Build ("optional-components-" + [guid]::NewGuid().ToString("N"))
    & $Python (Join-Path $PSScriptRoot "modular_components.py") $Dist $ComponentOutput
    if ($LASTEXITCODE -ne 0) { throw "Optional component staging failed" }
    Write-Host "Goi tinh nang de dong goi: $ComponentOutput"
}
# stage_runtime chep plugin/tien ich/ffmpeg va ghi VERSION (= file VERSION + so commit).
& $Python (Join-Path $PSScriptRoot "stage_runtime.py") (Join-Path $Dist "KidTE") --profile $ProfileSlug --minimal
if ($LASTEXITCODE -ne 0) { throw "Runtime validation failed" }
$Version = (Get-Content -LiteralPath (Join-Path $Dist "KidTE\VERSION") -Raw).Trim()
Write-Host "Phien ban: $Version"

# G06: manifest phat hanh (hash exe/file, patch solver, deps, commit solver). Phai chay truoc
# buoc don dist o cuoi; ban sao luu o PhatHanh de verify_release.ps1 -Manifest dung sau.
$ManifestInDist = Join-Path $Dist "KidTE\release-manifest.json"
& $Python (Join-Path $PSScriptRoot "release_manifest.py") generate (Join-Path $Dist "KidTE") --python $Python
if ($LASTEXITCODE -ne 0) { throw "Release manifest generation failed" }
& (Join-Path $PSScriptRoot "verify_release.ps1") -Dist (Join-Path $Dist "KidTE") -Python $Python
if ($LASTEXITCODE -ne 0) { throw "Release manifest verification failed" }
$ReleaseDir = Join-Path $Project "PhatHanh"
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
Copy-Item -LiteralPath $ManifestInDist -Destination (Join-Path $ReleaseDir "KidTE-$Version-$ArtifactProfile.release-manifest.json") -Force

if (-not $SkipInstaller) {
    $Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    $IsccPath = if ($Iscc) { $Iscc.Source } else {
        @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
    }
    if (-not $IsccPath) { throw "Chua cai Inno Setup 6 hoac khong tim thay ISCC.exe." }
    # Nén trong build/: nếu ISCC dừng giữa chừng, bản phát hành cũ vẫn nguyên vẹn.
    $Release = Join-Path $Project "PhatHanh"
    New-Item -ItemType Directory -Path $Release -Force | Out-Null
    $PackageTemp = Join-Path $Build ("setup-output-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $PackageTemp -Force | Out-Null
    & $IsccPath "/DAppVersion=$Version" "/DPackageProfile=$ProfileSlug" "/DArtifactLabel=$ArtifactLabel" "/O$PackageTemp" (Join-Path $PSScriptRoot "installer.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

    $Name = "KidTE-Setup-$Version-$ArtifactProfile.exe"
    $Candidate = Join-Path $PackageTemp $Name
    $Installer = Join-Path $Release $Name
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw "Installer completed but output file is missing."
    }
    $Hash = (Get-FileHash -LiteralPath $Candidate -Algorithm SHA256).Hash
    $HashCandidate = Join-Path $PackageTemp "$Name.sha256.txt"
    [System.IO.File]::WriteAllText($HashCandidate, "$Name  SHA256  $Hash`r`n",
        [System.Text.Encoding]::ASCII)
    if (Test-Path -LiteralPath $Installer -PathType Leaf) {
        [System.IO.File]::Replace($Candidate, $Installer,
            (Join-Path $PackageTemp "previous-installer.exe"))
    } else {
        Move-Item -LiteralPath $Candidate -Destination $Installer
    }
    if (Test-Path -LiteralPath "$Installer.sha256.txt" -PathType Leaf) {
        [System.IO.File]::Replace($HashCandidate, "$Installer.sha256.txt",
            (Join-Path $PackageTemp "previous-installer.sha256.txt"))
    } else {
        Move-Item -LiteralPath $HashCandidate -Destination "$Installer.sha256.txt"
    }
    if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash -ne $Hash) {
        throw "Published installer hash mismatch."
    }
    foreach ($Doc in @("CAI-DAT-WINDOWS.md", "CAI-DAT-TIEN-ICH-CHROME.md")) {
        Copy-Item -LiteralPath (Join-Path $Project "docs\$Doc") -Destination $Release
    }
    $InventoryPath = Join-Path $Release "KidTE-$Version-$ArtifactProfile.inventory.json"
    & $Python (Join-Path $PSScriptRoot "component_inventory.py") (Join-Path $Dist "KidTE") `
        --compressed-artifact $Installer --output $InventoryPath
    if ($LASTEXITCODE -ne 0) { throw "Runtime inventory failed." }
    # Thu muc trung gian cua PyInstaller khong con gia tri sau khi da co bo cai.
    if (-not $KeepBuild) { foreach ($Temp in @($Build, $Dist)) {
        if (Test-Path -LiteralPath $Temp) {
            $Resolved = (Resolve-Path -LiteralPath $Temp).Path
            if ($Resolved -notin @((Join-Path $Project "build"), (Join-Path $Project "dist"))) {
                throw "Unexpected temporary build directory: $Resolved"
            }
            Remove-Item -LiteralPath $Resolved -Recurse -Force
        }
    } }
    Write-Host "Bo cai: $Installer"
    Write-Host "SHA256: $Hash"
}
if ($SkipInstaller) {
    $InventoryPath = Join-Path $ReleaseDir "KidTE-$Version-$ArtifactProfile.inventory.json"
    & $Python (Join-Path $PSScriptRoot "component_inventory.py") (Join-Path $Dist "KidTE") `
        --output $InventoryPath
    if ($LASTEXITCODE -ne 0) { throw "Runtime inventory failed." }
}
