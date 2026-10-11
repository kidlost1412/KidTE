# So sánh câu trả lời AI trên bản sao dữ liệu live. Gọi DeepSeek thật (~0,2 USD).
# Dùng: run_context_ab.ps1 [ab|router]   (mặc định ab = gói A; router = gói B, router tắt vs bật)
param([ValidateSet('ab','router')][string]$Mode = 'ab')
$ErrorActionPreference = 'Stop'
# Nguon du lieu that (CHI DOC) = D:\KidTE\DuLieu; chi dung ban sao trong %TEMP%, khong chay tren du lieu that.
$src  = 'D:\KidTE\DuLieu'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$r    = Join-Path $env:TEMP 'kidte_ab'
robocopy $src $r /E /XD video_uploads backups schema_backups snapshots trash .venv logs runtime captcha models reports /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item (Join-Path $src '.env') (Join-Path $r '.env')
try {
    $env:PYTHONIOENCODING = 'utf-8'
    & "$repo\.venv\Scripts\python.exe" "$repo\backend\tools\context_ab.py" --root $r --limit 40 --mode $Mode --out "$repo\ab_report_$Mode"
} finally {
    Remove-Item (Join-Path $r '.env') -ErrorAction SilentlyContinue
}
Write-Host "Xong: $repo\ab_report_$Mode.md"
