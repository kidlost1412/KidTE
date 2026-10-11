$ErrorActionPreference = 'Stop'
$helperPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'ensure-webview2.ps1'
if (-not (Test-Path -LiteralPath $helperPath)) { throw "Helper not found: $helperPath" }

$script:Mock = $null
. $helperPath -ResultFile 'mock-result-file.txt'

function Get-WebView2RuntimeVersion {
    $script:Mock.DetectCalls++
    return $script:Mock.InitialRuntime
}

function Invoke-WebView2BootstrapperDownload([string]$Url, [string]$Path) {
    $script:Mock.DownloadCalls++
    $script:Mock.DownloadUrl = $Url
    if ($script:Mock.DownloadError) { throw $script:Mock.DownloadError }
}

function Get-WebView2BootstrapperSignature([string]$Path) {
    $script:Mock.SignatureCalls++
    return [pscustomobject]@{
        Status = $script:Mock.SignatureStatus
        SignerCertificate = [pscustomobject]@{ Subject = $script:Mock.SignerSubject }
    }
}

function Start-WebView2Bootstrapper([string]$Path) {
    $script:Mock.LaunchCalls++
    return [int]$script:Mock.BootstrapperExitCode
}

function Wait-WebView2RuntimeVersion {
    $script:Mock.VerifyCalls++
    return $script:Mock.VerifiedRuntime
}

function New-MockState {
    return @{
        InitialRuntime = $null
        VerifiedRuntime = '120.0.2210.91'
        SignatureStatus = [System.Management.Automation.SignatureStatus]::Valid
        SignerSubject = 'CN=Microsoft Corporation, O=Microsoft Corporation, C=US'
        BootstrapperExitCode = 0
        DownloadError = $null
        DetectCalls = 0
        DownloadCalls = 0
        SignatureCalls = 0
        LaunchCalls = 0
        VerifyCalls = 0
        DownloadUrl = ''
    }
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Invoke-MockedCase([string]$Name, [hashtable]$Overrides) {
    $script:Mock = New-MockState
    foreach ($key in $Overrides.Keys) { $script:Mock[$key] = $Overrides[$key] }
    $result = Invoke-WebView2RuntimeEnsure
    return [pscustomobject]@{ Name = $Name; Result = $result; Mock = $script:Mock }
}

$cases = @()

$case = Invoke-MockedCase 'existing runtime skips download' @{ InitialRuntime = '120.0.2210.91' }
Assert-True ($case.Result.ExitCode -eq 0 -and $case.Result.ResultLine -eq 'OK|already-installed|120.0.2210.91') 'Existing runtime should pass.'
Assert-True ($case.Mock.DownloadCalls -eq 0 -and $case.Mock.SignatureCalls -eq 0 -and $case.Mock.LaunchCalls -eq 0) 'Existing runtime must skip network, signature, and launch.'
$cases += $case.Name

$case = Invoke-MockedCase 'invalid signature blocks launch' @{ SignatureStatus = [System.Management.Automation.SignatureStatus]::NotSigned }
Assert-True ($case.Result.ExitCode -ne 0 -and $case.Result.ResultLine -like 'FAIL|signature|*Authenticode status NotSigned*') 'Invalid signature should fail at signature stage.'
Assert-True ($case.Mock.LaunchCalls -eq 0) 'Invalid signature must prevent launch.'
$cases += $case.Name

$case = Invoke-MockedCase 'wrong publisher blocks launch' @{ SignerSubject = 'CN=Not Microsoft, O=Example' }
Assert-True ($case.Result.ExitCode -ne 0 -and $case.Result.ResultLine -like 'FAIL|signature|*signer is not Microsoft Corporation*') 'Wrong signer should fail at signature stage.'
Assert-True ($case.Mock.LaunchCalls -eq 0) 'Wrong signer must prevent launch.'
$cases += $case.Name

$case = Invoke-MockedCase 'download failure reports download stage' @{ DownloadError = 'simulated network failure' }
Assert-True ($case.Result.ExitCode -ne 0 -and $case.Result.ResultLine -like 'FAIL|download|*simulated network failure*') 'Download failure should report download stage.'
Assert-True ($case.Mock.SignatureCalls -eq 0 -and $case.Mock.LaunchCalls -eq 0) 'Failed download must prevent signature and launch.'
$cases += $case.Name

$case = Invoke-MockedCase 'successful process without runtime fails verification' @{ VerifiedRuntime = $null }
Assert-True ($case.Result.ExitCode -ne 0 -and $case.Result.ResultLine -like 'FAIL|verification|*no Runtime pv version*') 'Missing post-install Runtime should fail verification.'
Assert-True ($case.Mock.LaunchCalls -eq 1 -and $case.Mock.VerifyCalls -eq 1) 'Verification follows a launched bootstrapper.'
$cases += $case.Name

$case = Invoke-MockedCase 'runtime after install passes' @{}
Assert-True ($case.Result.ExitCode -eq 0 -and $case.Result.ResultLine -eq 'OK|runtime-present|120.0.2210.91') 'Runtime registered after install should pass.'
$cases += $case.Name

$case = Invoke-MockedCase 'runtime after nonzero bootstrapper exit passes and logs code' @{ BootstrapperExitCode = 23 }
Assert-True ($case.Result.ExitCode -eq 0 -and $case.Result.ResultLine -eq 'OK|runtime-present|120.0.2210.91|bootstrapper-exit-code=23') 'Registered Runtime is authoritative even with nonzero bootstrapper exit, which must be logged.'
$cases += $case.Name

# Exercise the actual on-disk ASCII protocol consumed by Inno Setup. Mock-only
# checks above stop at ResultLine and would miss delimiters changed while writing.
$probeResultPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    'KidTE-WebView2-result-test-' + [Guid]::NewGuid().ToString('N') + '.txt'
)
try {
    $ResultFile = $probeResultPath
    $serializedCases = @(
        [pscustomobject]@{ Name = 'existing-runtime protocol'; Input = 'OK|already-installed|155.0.4283.45'; Expected = 'OK|already-installed|155.0.4283.45' },
        [pscustomobject]@{ Name = 'installed-runtime protocol with bootstrapper exit'; Input = 'OK|runtime-present|155.0.4283.45|bootstrapper-exit-code=23'; Expected = 'OK|runtime-present|155.0.4283.45|bootstrapper-exit-code=23' },
        [pscustomobject]@{ Name = 'failure protocol delimiters'; Input = 'FAIL|signature|publisher check failed'; Expected = 'FAIL|signature|publisher check failed' },
        [pscustomobject]@{ Name = 'non-ASCII and controls sanitized'; Input = ('FAIL|download|bad' + [char]1 + 'caf' + [char]233); Expected = 'FAIL|download|bad caf ' }
    )
    foreach ($serializedCase in $serializedCases) {
        Set-ProbeResult $serializedCase.Input
        $written = [System.IO.File]::ReadAllText($probeResultPath, [System.Text.Encoding]::ASCII)
        Assert-True ($written -ceq $serializedCase.Expected) ($serializedCase.Name + ' should match the on-disk protocol.')
        $bytes = [System.IO.File]::ReadAllBytes($probeResultPath)
        foreach ($byte in $bytes) {
            Assert-True ($byte -ge 0x20 -and $byte -le 0x7E) ($serializedCase.Name + ' must contain printable ASCII only.')
        }
    }
    $cases += 'serialized probe result preserves protocol delimiters and sanitizes text'
}
finally {
    if (Test-Path -LiteralPath $probeResultPath) {
        Remove-Item -LiteralPath $probeResultPath -Force
    }
}

Write-Output ('PASS: ' + $cases.Count + ' mocked WebView2 prerequisite cases')
foreach ($name in $cases) { Write-Output ('  - ' + $name) }
