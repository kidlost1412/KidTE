param([Parameter(Mandatory=$true)][string]$DataDir,
      [Parameter(Mandatory=$true)][string]$Ffmpeg,
      [Parameter(Mandatory=$true)][int]$SolverPid)
$ErrorActionPreference = 'Stop'
$mutex = [System.Threading.Mutex]::new($false, 'Local\KidTECaptchaMonitor')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
$monitorLog = Join-Path $DataDir 'telegram-monitor.log'
$solverLog = Join-Path $DataDir 'tiktok-captcha-solver-logs\ttls-captcha-solver.log'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$recordings = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
function Write-Note([string]$message) {
    Add-Content -LiteralPath $monitorLog -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $message)
}
function Read-Config {
    $chatFile = Join-Path $DataDir 'telegram.json'
    $tokenFile = Join-Path $DataDir 'telegram-token.dpapi'
    if (-not (Test-Path -LiteralPath $chatFile) -or -not (Test-Path -LiteralPath $tokenFile)) { return $null }
    Add-Type -AssemblyName System.Security
    $bytes = [System.IO.File]::ReadAllBytes($tokenFile)
    $plain = [System.Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    return @{ ChatId = [string]((Get-Content -LiteralPath $chatFile -Raw | ConvertFrom-Json).chat_id);
              Token = [System.Text.Encoding]::UTF8.GetString($plain) }
}
function Send-Alert([string]$message) {
    try {
        $config = Read-Config
        if (-not $config) { return }
        $uri = 'https://api.telegram.org/bot' + $config.Token + '/sendMessage'
        $response = Invoke-RestMethod -Uri $uri -Method Post -Body @{chat_id=$config.ChatId; text=$message} -TimeoutSec 12
        if (-not $response.ok) { throw 'Telegram returned ok=false' }
        Write-Note 'Telegram text delivered'
    } catch {
        $response = $_.Exception.Response
        $reason = if ($response -and $response.StatusCode) {
            'HTTP ' + [int]$response.StatusCode
        } else { $_.Exception.GetType().Name }
        Write-Note ('Telegram text failed: ' + $reason)
    }
}
function Start-Recording([string]$captchaType) {
    $script = Join-Path $scriptDir 'record-and-send.ps1'
    $quote = { param($value) "'" + $value.Replace("'", "''") + "'" }
    $command = '& ' + (& $quote $script) + ' -DataDir ' + (& $quote $DataDir) +
               ' -Ffmpeg ' + (& $quote $Ffmpeg) + ' -CaptchaType ' + (& $quote $captchaType)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $recording = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded
    ) -WorkingDirectory $DataDir -WindowStyle Hidden -PassThru
    $recordings.Add($recording)
    Write-Note ("Recording requested: $captchaType PID=$($recording.Id)")
}
try {
    $offset = if (Test-Path -LiteralPath $solverLog) { (Get-Item -LiteralPath $solverLog).Length } else { 0L }
    $logIdentity = if (Test-Path -LiteralPath $solverLog) { (Get-Item -LiteralPath $solverLog).CreationTimeUtc.Ticks } else { 0L }
    $partialLine = ''
    $decoder = [Text.Encoding]::UTF8.GetDecoder()
    $lastAlert = [datetime]::MinValue
    $lastRecording = [datetime]::MinValue
    $lastType = ''
    Write-Note ('Watching native log from byte ' + $offset)
    while ($true) {
        if (Test-Path -LiteralPath (Join-Path $DataDir 'stop-requested.marker')) { break }
        $solverAlive = [bool](Get-Process -Id $SolverPid -ErrorAction SilentlyContinue)
        try {
            if (Test-Path -LiteralPath $solverLog) {
                $logInfo = Get-Item -LiteralPath $solverLog
                $length = $logInfo.Length
                if ($length -lt $offset -or $logInfo.CreationTimeUtc.Ticks -ne $logIdentity) {
                    $offset = 0L; $partialLine = ''; $decoder.Reset()
                    $logIdentity = $logInfo.CreationTimeUtc.Ticks
                }
                if ($length -gt $offset) {
                    $stream = [IO.File]::Open($solverLog, 'Open', 'Read', 'ReadWrite')
                    try {
                        [void]$stream.Seek($offset, [IO.SeekOrigin]::Begin)
                        $buffer = New-Object byte[] 65536
                        $chars = New-Object char[] 65536
                        $text = [Text.StringBuilder]::new()
                        while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                            $characters = $decoder.GetChars($buffer, 0, $count, $chars, 0, $false)
                            [void]$text.Append($chars, 0, $characters)
                        }
                        $lines = ($partialLine + $text.ToString()) -split "`n"
                        $partialLine = $lines[$lines.Length - 1]
                        if ($partialLine.Length -gt 65536) { $partialLine = ''; Write-Note 'Oversized incomplete log line dropped' }
                        for ($lineIndex = 0; $lineIndex -lt $lines.Length - 1; $lineIndex++) {
                            $rawLine = $lines[$lineIndex]
                            $line = $rawLine.TrimEnd("`r")
                            # Rotation can expose old detections. Only fresh,
                            # timestamped native events can trigger a recording.
                            if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \|') {
                                $eventTime = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
                                $eventAge = ((Get-Date) - $eventTime).TotalSeconds
                                if ($eventAge -gt 30 -or $eventAge -lt -5) { continue }
                            } else { continue }
                            $kind = if ($line -match 'modern puzzle captcha detected') { 'puzzle mới' }
                                    elseif ($line -match 'legacy puzzle captcha detected') { 'puzzle cũ' }
                                    elseif ($line -match 'shapes captcha detected') { 'shapes' }
                                    else { '' }
                            if ($kind) {
                                if (((Get-Date) - $lastRecording).TotalSeconds -ge 30) {
                                    Start-Recording $kind
                                    $lastRecording = Get-Date
                                }
                                if ($kind -ne $lastType -or ((Get-Date) - $lastAlert).TotalSeconds -ge 180) {
                                    Send-Alert ("Kid TE: Phát hiện CAPTCHA $kind lúc $(Get-Date -Format 'HH:mm:ss'). Solver đang xử lý; chưa xác nhận giải thành công.")
                                    $lastAlert = Get-Date
                                    $lastType = $kind
                                }
                            } elseif ($line -match 'CAPTCHA_MANUAL_REQUIRED') {
                                Send-Alert 'CAPTCHA TikTok: đã thử 7 lần chưa qua — vào giải tay'
                            } elseif ($line -match 'solver stopped: SadCaptcha rejected the API key') {
                                Send-Alert 'Kid TE: SadCaptcha từ chối key (HTTP 401). Solver đã dừng; kiểm tra key và số credit còn lại.'
                            }
                            # 'puzzle still visible after drag' chỉ ghi log: mỗi đợt tự thử tới 7 lần,
                            # chỉ báo Telegram một lần khi cần vào giải tay (CAPTCHA_MANUAL_REQUIRED).
                        }
                        $offset = $stream.Position
                    } finally { $stream.Dispose() }
                }
            }
        } catch { Write-Note ('Monitor read failed: ' + $_.Exception.GetType().Name) }
        for ($index = $recordings.Count - 1; $index -ge 0; $index--) {
            if ($recordings[$index].HasExited) {
                $recordings[$index].Dispose()
                $recordings.RemoveAt($index)
            }
        }
        if (-not $solverAlive) { break }
        Start-Sleep -Milliseconds 500
    }
    Write-Note 'Solver exited; monitor stopped'
} finally {
    foreach ($recording in $recordings) {
        if (-not $recording.HasExited) {
            & taskkill.exe /PID $recording.Id /T /F 2>$null | Out-Null
        }
        $recording.Dispose()
    }
    [void]$mutex.ReleaseMutex()
    $mutex.Dispose()
}
