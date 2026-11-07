# Запуск стенда, cv и экрана тренажёра (самостоятельное место).
#
#   .\run.ps1                       камера
#   .\run.ps1 -Source demo          запись
#   .\run.ps1 -Source C:\video.mp4  файл
#   .\run.ps1 -Window               окно OpenCV
#   .\run.ps1 -Record               запись в runs\
#   .\run.ps1 -StandOnly | -CvOnly
#   .\run.ps1 -Check               страница проверки вместо тренажёра
#   .\run.ps1 -Instructor          тренажёр с пультом инструктора (клавиша I)
#   .\run.ps1 -NoBrowser           без окна браузера

param(
    [string]$Source = 'camera',
    [switch]$Window,
    [switch]$NoWindow,   # старый ключ, окно и так выключено
    [switch]$Record,
    [switch]$StandOnly,
    [switch]$CvOnly,
    [switch]$Check,
    [switch]$Instructor,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$base = 'http://127.0.0.1:8080'
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null

function Test-Port {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', 8080); return $true } catch { return $false } finally { $client.Close() }
}

function Start-Logged($title, $workdir, $exe, [string[]]$arguments, $log) {
    # Своё окно с выводом, копия в лог. Команда в base64, чтобы не ломались кавычки.
    $argLine = ($arguments | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
    $command = "`$host.UI.RawUI.WindowTitle = '$title'; " +
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " +
        "Set-Location '$workdir'; " +
        "& '$exe' $argLine 2>&1 | ForEach-Object { `"`$_`" } | Tee-Object -FilePath '$log'"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process powershell -ArgumentList '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded | Out-Null
}

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not $CvOnly) {
    if (Test-Port) {
        Write-Host '[ok] стенд уже запущен' -ForegroundColor Green
    } else {
        $standPy = "$root\stand\.venv\Scripts\python.exe"
        if (-not (Test-Path $standPy)) { Write-Host '[!!] сначала .\install.ps1' -ForegroundColor Red; exit 1 }

        # Общий ключ занятия для браузера и cv.
        $env:KFT_STAND_KEY = 'demo'
        $env:KFT_CONFIG = 'configs/app-ppe-live.yaml'
        # Голос Silero из комплекта.
        $env:TORCH_HOME = "$root\stand\torch"
        Start-Logged 'stand' "$root\stand" $standPy @('run_local.py', '--port', '8080') "$root\logs\stand-$stamp.log"

        Write-Host 'жду стенд...'
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Port)) {
            if ((Get-Date) -gt $deadline) {
                Write-Host '[!!] стенд не поднялся за 90 с, см. logs\stand-*.log' -ForegroundColor Red
                exit 1
            }
            Start-Sleep -Milliseconds 500
        }
        Write-Host '[ok] стенд' -ForegroundColor Green
    }
}

if (-not $StandOnly) {
    $cvPy = "$root\cv\.venv\Scripts\python.exe"
    if (-not (Test-Path "$root\cv\config.laptop.yaml")) { Write-Host '[!!] сначала .\install.ps1' -ForegroundColor Red; exit 1 }

    $src = $Source
    if ($Source -eq 'camera') { $src = '0' }
    elseif ($Source -eq 'demo') { $src = "$root\stand\configs\cv-logs\real-donning.mp4" }
    if ($src -notmatch '^\d+$' -and -not (Test-Path $src)) { Write-Host "[!!] нет файла $src" -ForegroundColor Red; exit 1 }

    $arguments = @('app.py', '--config', 'config.laptop.yaml', '--source', $src, '--preview-port', '8081')
    if (-not $Window) { $arguments += '--no-window' }
    if ($Record) { $arguments += @('--output-dir', "$root\runs\$stamp") }

    # Ускоряет открытие встроенных камер в Windows.
    $env:OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS = '0'
    # Без камеры OpenCV пишет ошибку на каждую попытку поиска.
    $env:OPENCV_LOG_LEVEL = 'FATAL'
    Start-Logged 'cv' "$root\cv" $cvPy $arguments "$root\logs\cv-$stamp.log"
    Write-Host "[ok] cv, источник $src" -ForegroundColor Green
    if ($Record) { Write-Host "     запись: $root\runs\$stamp" }
}

if (-not $CvOnly -and -not $NoBrowser) {
    $browser = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    # У каждого окна свой профиль: отдельный процесс, ключи запуска действуют всегда.
    function Open-App($url, $profileName, [string[]]$extra) {
        if (-not $browser) { Write-Host "[??] нет chrome и edge, открыть вручную: $url" -ForegroundColor Yellow; return }
        Start-Process $browser (@("--app=$url", "--user-data-dir=$env:LOCALAPPDATA\$profileName",
            '--no-first-run', '--no-default-browser-check', '--autoplay-policy=no-user-gesture-required') + $extra)
    }

    if ($Check) { Open-App "$base/check" 'kft-check' @('--start-maximized') }
    elseif ($Instructor) { Open-App "$base/expo/" 'kft-expo' @('--start-maximized') }
    else {
        # Выставка: во весь экран, микрофон разрешён без вопроса. Выйти из полного экрана - F11.
        Open-App "$base/expo/?self=1" 'mentrica-expo' @('--start-fullscreen', '--use-fake-ui-for-media-stream')
        Write-Host '[ok] тренажёр' -ForegroundColor Green
    }
}

Write-Host "тренажёр:  $base/expo/?self=1"
