# Локальный запуск на этой машине: стенд из проекта, модуль распознавания
# из demo-kit\cv, источник — вебкамера.
#
#     .\.local\start.ps1                      # с вебкамерой
#     .\.local\start.ps1 -Source demo         # с записи вместо камеры
#     .\.local\start.ps1 -Source C:\video.mp4 # с любого видеофайла
#     .\.local\start.ps1 -Window              # ещё и отладочное окно OpenCV
#     .\.local\start.ps1 -StandOnly           # только стенд
#     .\.local\start.ps1 -NoBrowser           # не открывать страницу проверки
#     .\.local\start.ps1 -Expo                # выставочный экран вместо проверки
#     .\.local\start.ps1 -Self                # выставка без инструктора: выбор режима на экране
#
# Каждый процесс открывается в своём окне, вывод дублируется в .local\logs.
# Остановить всё: .\.local\stop.ps1

param(
    [string]$Source = 'camera',
    [switch]$Window,
    [switch]$StandOnly,
    [switch]$NoBrowser,
    [switch]$Expo,
    [switch]$Self
)

$ErrorActionPreference = 'Continue'
$local = $PSScriptRoot
$project = Split-Path $local -Parent
$kitCv = Join-Path (Split-Path $project -Parent) 'demo-kit\cv'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$url = if ($Self) { 'http://127.0.0.1:8080/expo/?self=1' } elseif ($Expo) { 'http://127.0.0.1:8080/expo/' } else { 'http://127.0.0.1:8080/check' }
$profileDir = if ($Self) { "$env:LOCALAPPDATA\mentrica-expo" } elseif ($Expo) { "$env:LOCALAPPDATA\kft-expo" } else { "$env:LOCALAPPDATA\kft-check" }
New-Item -ItemType Directory -Force -Path "$local\logs" | Out-Null

function Test-Port($port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect('127.0.0.1', $port); return $true } catch { return $false } finally { $client.Close() }
}

function Start-Logged($title, $workdir, $exe, [string[]]$arguments, $log) {
    # Отдельное окно с живым выводом, тот же вывод — в журнал. Команда
    # передаётся закодированной: так кавычки и пробелы в путях не ломаются.
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

if (Test-Port 8080) {
    Write-Host '[ OK ] стенд уже запущен' -ForegroundColor Green
} else {
    # Зависимости стенда стоят в общем Python 3.12, отдельного окружения нет.
    $standPy = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
    if (-not $standPy) { Write-Host '[ !! ] Python 3.12 не найден' -ForegroundColor Red; exit 1 }

    # Общий ключ занятия: браузер и модуль распознавания попадают в одно
    # занятие, в каком бы порядке ни запустились.
    $env:KFT_STAND_KEY = 'demo'
    $env:KFT_CONFIG = 'configs/app-ppe-live.yaml'
    Start-Logged 'Stand' $project $standPy @('run_local.py', '--port', '8080') "$local\logs\stand-$stamp.log"

    Write-Host 'Жду стенд...'
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Port 8080)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host '[ !! ] стенд не поднялся за 90 секунд — смотреть его окно и .local\logs\stand-*.log' -ForegroundColor Red
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host '[ OK ] стенд работает' -ForegroundColor Green
}

if ($StandOnly) {
    # только стенд
} elseif (Test-Port 8081) {
    Write-Host '[ OK ] модуль распознавания уже запущен' -ForegroundColor Green
} else {
    $cvPy = "$local\cv-venv\Scripts\python.exe"
    if (-not (Test-Path $cvPy)) { Write-Host '[ !! ] нет окружения .local\cv-venv' -ForegroundColor Red; exit 1 }
    if (-not (Test-Path "$kitCv\app.py")) { Write-Host "[ !! ] нет модуля распознавания в $kitCv" -ForegroundColor Red; exit 1 }

    $src = $Source
    if ($Source -eq 'camera') { $src = '0' }
    elseif ($Source -eq 'demo') { $src = "$project\configs\cv-logs\real-donning.mp4" }
    if ($src -notmatch '^\d+$' -and -not (Test-Path $src)) { Write-Host "[ !! ] нет файла $src" -ForegroundColor Red; exit 1 }

    $arguments = @('app.py', '--config', "$local\cv-config.yaml", '--source', $src, '--preview-port', '8081')
    if (-not $Window) { $arguments += '--no-window' }

    # Лечит медленный старт встроенных камер под Windows, на остальное не влияет.
    $env:OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS = '0'
    # Без камеры OpenCV пишет ошибку на каждую попытку поиска.
    $env:OPENCV_LOG_LEVEL = 'FATAL'
    Start-Logged 'Recognition' $kitCv $cvPy $arguments "$local\logs\cv-$stamp.log"
    Write-Host ("[ OK ] модуль распознавания запущен, источник: {0}" -f $src) -ForegroundColor Green
}

if (-not $NoBrowser) {
    $browser = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($browser) {
        # Свой профиль: отдельное окно приложения, а не вкладка в открытом
        # браузере, и ключи запуска действуют всегда.
        Start-Process $browser @("--app=$url", "--user-data-dir=$profileDir",
            '--no-first-run', '--no-default-browser-check', '--start-maximized',
            '--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream')
    } else {
        Write-Host "нет chrome и edge, открыть вручную: $url"
    }
}

Write-Host ''
Write-Host "Страница показа:        $url"
Write-Host 'Интерфейс инструктора:  http://127.0.0.1:8080/'
Write-Host 'Остановить всё:         .\.local\stop.ps1'
