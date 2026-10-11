# Ghi sức khoẻ KidTE mỗi 5 phút trong phiên live (chỉ đọc, không đổi trạng thái).
param([string]$Out = "D:\KidTE\soak-$(Get-Date -Format yyyyMMdd).csv", [int]$EverySec = 300)
if (-not (Test-Path $Out)) {
    "time,proc,pid,ws_mb,private_mb,cpu_s,handles,threads,tiktok_state,tiktok_age_s,shopee_state,captcha_pending,log_mb" | Set-Content $Out -Encoding utf8
}
while ($true) {
    $now = Get-Date -Format s
    $tk = ''; $tkAge = ''; $sp = ''
    try {
        $p = Invoke-RestMethod "http://127.0.0.1:8793/api/video/position?platform=tiktok" -TimeoutSec 5
        $tk = if ($p.available) { $p.state } else { "unavailable:$($p.reason)" }; $tkAge = $p.age_s
    } catch { $tk = 'backend_down' }
    try {
        $s = Invoke-RestMethod "http://127.0.0.1:8793/api/video/position?platform=shopee" -TimeoutSec 5
        $sp = if ($s.available) { $s.state } else { "unavailable:$($s.reason)" }
    } catch { $sp = 'backend_down' }
    $pending = (Get-ChildItem "D:\KidTE\DuLieu\captcha\screen-recordings" -Filter 'screen-*.mp4' -ErrorAction SilentlyContinue).Count
    $logMb = [math]::Round(((Get-ChildItem D:\KidTE\DuLieu\logs -Recurse -Include *.log -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB, 1)
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'KidTE\\src\\(KidTE.exe|backend\\src\\app_entry.py|plugins\\)|KidTECaptcha|Captcha' }
    foreach ($w in $procs) {
        $gp = Get-Process -Id $w.ProcessId -ErrorAction SilentlyContinue
        if (-not $gp) { continue }
        $name = if ($w.CommandLine -match 'app_entry') { 'backend' } elseif ($w.CommandLine -match 'chay.py') { 'dong-bo-video' } else { $gp.ProcessName }
        "$now,$name,$($gp.Id),$([math]::Round($gp.WorkingSet64/1MB,1)),$([math]::Round($gp.PrivateMemorySize64/1MB,1)),$([math]::Round($gp.TotalProcessorTime.TotalSeconds,1)),$($gp.HandleCount),$($gp.Threads.Count),$tk,$tkAge,$sp,$pending,$logMb" | Add-Content $Out -Encoding utf8
    }
    Start-Sleep -Seconds $EverySec
}
