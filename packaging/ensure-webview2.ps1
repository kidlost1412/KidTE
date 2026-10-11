param(
    [Parameter(Mandatory = $true)]
    [string]$ResultFile
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$bootstrapperUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
$runtimeClientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'

function Set-ProbeResult([string]$line) {
    $safeLine = $line -replace '[^\x20-\x7E]', ' '
    # Keep protocol separators: Inno Setup recognizes the serialized OK| prefix.
    # Exception text is sanitized separately before constructing ResultLine.
    if ($safeLine.Length -gt 500) { $safeLine = $safeLine.Substring(0, 500) }
    [System.IO.File]::WriteAllText($ResultFile, $safeLine, [System.Text.Encoding]::ASCII)
}

function Get-WebView2RuntimeVersion {
    $registryViews = @(
        [Microsoft.Win32.RegistryView]::Registry64,
        [Microsoft.Win32.RegistryView]::Registry32
    )
    $registryHives = @(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryHive]::CurrentUser
    )
    $versions = New-Object 'System.Collections.Generic.List[System.Version]'
    $readErrors = New-Object 'System.Collections.Generic.List[string]'

    foreach ($hive in $registryHives) {
        foreach ($view in $registryViews) {
            $baseKey = $null
            $clientKey = $null
            try {
                $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
                $root = if ($hive -eq [Microsoft.Win32.RegistryHive]::CurrentUser) {
                    'Software\Microsoft\EdgeUpdate\Clients\' + $runtimeClientId
                } else {
                    'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + $runtimeClientId
                }
                $clientKey = $baseKey.OpenSubKey($root, $false)
                if ($null -eq $clientKey) { continue }

                $rawVersion = $clientKey.GetValue('pv', $null)
                if ($rawVersion -is [string] -and $rawVersion.Trim()) {
                    $parsedVersion = $null
                    if ([Version]::TryParse($rawVersion.Trim(), [ref]$parsedVersion) -and
                        $parsedVersion -gt [Version]'0.0.0.0') {
                        $versions.Add($parsedVersion)
                    }
                }
            }
            catch {
                $readErrors.Add($_.Exception.GetType().Name) | Out-Null
            }
            finally {
                if ($clientKey) { $clientKey.Dispose() }
                if ($baseKey) { $baseKey.Dispose() }
            }
        }
    }

    if ($versions.Count -gt 0) {
        return ($versions | Sort-Object -Descending | Select-Object -First 1).ToString()
    }
    if ($readErrors.Count -gt 0) {
        throw ('registry read failed: ' + (($readErrors | Select-Object -Unique) -join ','))
    }
    return $null
}

function Invoke-WebView2BootstrapperDownload([string]$Url, [string]$Path) {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path -TimeoutSec 180
    $fileLength = (Get-Item -LiteralPath $Path).Length
    if ($fileLength -lt 100000 -or $fileLength -gt 20000000) {
        throw 'unexpected bootstrapper size'
    }
}

function Get-WebView2BootstrapperSignature([string]$Path) {
    return Get-AuthenticodeSignature -LiteralPath $Path
}

function Start-WebView2Bootstrapper([string]$Path) {
    # Setup runs with PrivilegesRequired=lowest; Start-Process therefore keeps
    # the current user's token and installs the Runtime per-user when possible.
    $process = Start-Process -FilePath $Path -ArgumentList @('/silent', '/install') `
        -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(600000)) {
        $process.Kill()
        $process.WaitForExit()
        throw 'bootstrapper timed out after 600 seconds'
    }
    return [int]$process.ExitCode
}

function Wait-WebView2RuntimeVersion {
    $verifyDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $version = Get-WebView2RuntimeVersion
        if ($version) { return $version }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $verifyDeadline)
    return $null
}

function Invoke-WebView2RuntimeEnsure {
    $bootstrapperPath = $null
    $stage = 'detection'
    $resultLine = ''
    $exitCode = 1
    $bootstrapperExitCode = $null

    try {
        $runtimeVersion = Get-WebView2RuntimeVersion
        if ($runtimeVersion) {
            $resultLine = 'OK|already-installed|' + $runtimeVersion
            $exitCode = 0
        } else {
            $stage = 'download'
            $bootstrapperPath = Join-Path ([System.IO.Path]::GetTempPath()) (
                'KidTE-WebView2-' + [Guid]::NewGuid().ToString('N') + '.exe'
            )
            Invoke-WebView2BootstrapperDownload $bootstrapperUrl $bootstrapperPath

            $stage = 'signature'
            $signature = Get-WebView2BootstrapperSignature $bootstrapperPath
            if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
                throw ('Authenticode status ' + [string]$signature.Status)
            }
            if ($null -eq $signature.SignerCertificate -or
                $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
                throw 'signer is not Microsoft Corporation'
            }

            $stage = 'launch'
            $bootstrapperExitCode = Start-WebView2Bootstrapper $bootstrapperPath

            $stage = 'verification'
            $runtimeVersion = Wait-WebView2RuntimeVersion
            if (-not $runtimeVersion) {
                if ($null -ne $bootstrapperExitCode -and $bootstrapperExitCode -ne 0) {
                    throw ('bootstrapper exit code ' + $bootstrapperExitCode +
                        ' and no Runtime pv version was registered')
                }
                throw 'no Runtime pv version was registered after bootstrapper exit'
            }

            $resultLine = 'OK|runtime-present|' + $runtimeVersion
            if ($null -ne $bootstrapperExitCode -and $bootstrapperExitCode -ne 0) {
                $resultLine += '|bootstrapper-exit-code=' + $bootstrapperExitCode
            }
            $exitCode = 0
        }
    }
    catch {
        $detail = $_.Exception.Message -replace '[^\x20-\x7E]', ' '
        $detail = $detail.Replace('|', '/')
        if ($detail.Length -gt 350) { $detail = $detail.Substring(0, 350) }
        $resultLine = 'FAIL|' + $stage + '|' + $detail
    }
    finally {
        if ($bootstrapperPath -and (Test-Path -LiteralPath $bootstrapperPath)) {
            Remove-Item -LiteralPath $bootstrapperPath -Force -ErrorAction SilentlyContinue
        }
    }

    return [pscustomobject]@{ ExitCode = $exitCode; ResultLine = $resultLine }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-WebView2RuntimeEnsure
    try {
        Set-ProbeResult $result.ResultLine
    }
    catch {
        exit 2
    }
    exit $result.ExitCode
}
