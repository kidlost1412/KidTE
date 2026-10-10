param([string]$OutputPath = '')

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'KidTESourceLauncher.cs'
$icon = Join-Path $root 'assets\kid-te.ico'
$target = if ($OutputPath) { [System.IO.Path]::GetFullPath($OutputPath) } else { Join-Path $root 'KidTE.exe' }
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'Windows .NET Framework C# compiler is unavailable.'
}
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ "/out:$target" "/win32icon:$icon" /reference:System.Windows.Forms.dll $source
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw 'KidTE.exe launcher build failed.'
}
Write-Host "Built $target"
