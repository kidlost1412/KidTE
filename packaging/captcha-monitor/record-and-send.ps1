param([Parameter(Mandatory=$true)][string]$DataDir,
      [Parameter(Mandatory=$true)][string]$Ffmpeg,
      [string]$CaptchaType = 'CAPTCHA')
$ErrorActionPreference = 'Stop'
$mutex = [System.Threading.Mutex]::new($false, 'Local\KidTECaptchaRecorder')
$log = Join-Path $DataDir 'telegram-monitor.log'
function Write-Note([string]$message) {
    Add-Content -LiteralPath $log -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $message)
}
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
try {
    $recordings = Join-Path $DataDir 'screen-recordings'
    [void][IO.Directory]::CreateDirectory($recordings)
    $total = (Get-ChildItem -LiteralPath $recordings -File | Measure-Object -Property Length -Sum).Sum
    if ($total + 256MB -gt 5GB) { Write-Note 'Recording paused: 5 GB quota; solver continues'; exit 0 }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace KidTE {
 public static class CaptureWindow {
  [StructLayout(LayoutKind.Sequential)] public struct Rect {public int Left,Top,Right,Bottom;}
  [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr window, out Rect bounds);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr window);
 }
}
'@
    [void][KidTE.CaptureWindow]::SetThreadDpiAwarenessContext([IntPtr](-4))
    $studio = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -match '^TikTok LIVE Studio$' -and $_.MainWindowHandle -ne 0
    })
    if ($studio.Count -ne 1) { throw 'TikTok Studio window unavailable or ambiguous' }
    $window = $studio[0].MainWindowHandle
    if ([KidTE.CaptureWindow]::IsIconic($window)) { throw 'TikTok Studio window minimized' }
    $bounds = New-Object KidTE.CaptureWindow+Rect
    if (-not [KidTE.CaptureWindow]::GetWindowRect($window, [ref]$bounds)) { throw 'Cannot locate Studio window' }
    Add-Type -AssemblyName System.Windows.Forms
    $desktop = [Windows.Forms.SystemInformation]::VirtualScreen
    $bounds.Left = [Math]::Max($bounds.Left, $desktop.Left)
    $bounds.Top = [Math]::Max($bounds.Top, $desktop.Top)
    $bounds.Right = [Math]::Min($bounds.Right, $desktop.Right)
    $bounds.Bottom = [Math]::Min($bounds.Bottom, $desktop.Bottom)
    $width = $bounds.Right - $bounds.Left
    $height = $bounds.Bottom - $bounds.Top
    $width -= $width % 2; $height -= $height % 2
    if ($width -lt 2 -or $height -lt 2) { throw 'Invalid Studio window bounds' }
    $stem = 'screen-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
    $temporary = Join-Path $recordings ($stem + '.recording.mp4')
    $output = Join-Path $recordings ($stem + '.mp4')
    # Physical virtual-desktop offsets, including negative X/Y. Fixed bitrate
    # bounds a 30-second file below the reserved 256 MB. No primary-screen assumption.
    & $Ffmpeg -hide_banner -loglevel error -f gdigrab -framerate 30 -offset_x $bounds.Left -offset_y $bounds.Top -video_size ($width.ToString() + 'x' + $height) -draw_mouse 1 -i desktop -t 30 -an -c:v libx264 -preset ultrafast -b:v 8M -maxrate 8M -bufsize 16M -pix_fmt yuv420p -movflags +faststart $temporary
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporary)) { throw 'Studio recording failed' }
    Move-Item -LiteralPath $temporary -Destination $output
    Write-Note ('Recorded for durable upload ' + [IO.Path]::GetFileName($output))
    # captcha_upload.py discovers finalized files. HTTP never holds this mutex.
} catch { Write-Note ('Recording failed: ' + $_.Exception.GetType().Name) }
finally { [void]$mutex.ReleaseMutex(); $mutex.Dispose() }
