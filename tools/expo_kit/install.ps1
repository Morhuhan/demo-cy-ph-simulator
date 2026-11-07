# Установка: стенд и cv, каждый в своём .venv.
#
#   .\install.ps1
#   .\install.ps1 -Cuda cu128     другой индекс torch
#   .\install.ps1 -Force          пересоздать окружения

param(
    [string]$Cuda = 'cu126',
    [switch]$Force,
    [switch]$SkipStand,
    [switch]$SkipCv
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$pipLog = "$root\logs\pip-$stamp.log"
Start-Transcript -Path "$root\logs\install-$stamp.log" | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

function Step($text) { Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text, $fix) {
    Write-Host "[!!] $text" -ForegroundColor Red
    if ($fix) { Write-Host "     $fix" -ForegroundColor Yellow }
    Write-Host "     лог: $pipLog"
    Stop-Transcript | Out-Null
    exit 1
}
function Run($exe, [string[]]$arguments, $what) {
    & $exe @arguments 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $pipLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { Fail "$what, код $LASTEXITCODE" "Get-Content $pipLog -Tail 40" }
}
$pip = @('--progress-bar', 'off', '--disable-pip-version-check')

function New-Venv($dir) {
    $venv = Join-Path $dir '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if ($Force -and (Test-Path $venv)) { Remove-Item -Recurse -Force $venv }
    if (-not (Test-Path $venvPython)) {
        & $script:python -m venv $venv 2>&1 | Out-Null
        if (-not (Test-Path $venvPython)) { Fail "не создалось окружение в $dir" }
    }
    Run $venvPython (@('-m', 'pip', 'install', '--upgrade', 'pip') + $pip) 'pip'
    return $venvPython
}

Step 'python'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { Fail 'нет py' 'python.org, 3.12' }
$script:python = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
if (-not $script:python) { Fail 'нет python 3.12' 'python.org, 3.12' }
Write-Host "   $script:python"

if (-not $SkipStand) {
    Step 'стенд'
    $standPy = New-Venv "$root\stand"
    Run $standPy (@('-m', 'pip', 'install', '-e', "$root\stand") + $pip) 'зависимости стенда'
    # Импорт самого стенда ловит и косвенные зависимости.
    Push-Location "$root\stand"
    $importError = & $standPy -c "import run_local" 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 1
    $importCode = $LASTEXITCODE
    Pop-Location
    if ($importCode -ne 0) { Fail "стенд не импортируется: $importError" }
    Write-Host '   ok' -ForegroundColor Green

    Step 'голос, ~250 МБ'
    Run $standPy (@('-m', 'pip', 'install', 'torch', 'torchaudio', '--index-url', 'https://download.pytorch.org/whl/cpu') + $pip) 'torch для голоса'
    Run $standPy (@('-m', 'pip', 'install', 'numpy', 'omegaconf') + $pip) 'numpy, omegaconf'
    # Модель лежит в комплекте, из сети не качается.
    $env:TORCH_HOME = "$root\stand\torch"
    $voice = & $standPy -c "import torch; m,_=torch.hub.load('snakers4/silero-models','silero_tts',language='ru',speaker='v4_ru',trust_repo=True,verbose=False); print(len(m.apply_tts(text='Проверка голоса.',speaker='eugene',sample_rate=24000)))" 2>&1 | Select-Object -Last 1
    Remove-Item Env:TORCH_HOME
    if ("$voice" -match '^\d+$') { Write-Host '   ok' -ForegroundColor Green }
    else { Write-Host "[??] голос не работает: $voice" -ForegroundColor Yellow }

    Step 'распознавание речи'
    $envFile = "$root\stand\.env"
    if ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*HF_TOKEN\s*=\s*\S' -Quiet)) {
        Push-Location "$root\stand"
        $env:KFT_CONFIG = 'configs/app-ppe-live.yaml'
        & $standPy -m tools.stt_check
        $sttCode = $LASTEXITCODE
        Remove-Item Env:KFT_CONFIG
        Pop-Location
        if ($sttCode -ne 0) { Write-Host '[??] HF не отвечает, голос будет кнопками' -ForegroundColor Yellow }
    } else {
        Write-Host '[??] нет HF_TOKEN, голос будет кнопками' -ForegroundColor Yellow
    }
}

if (-not $SkipCv) {
    Step 'cv'
    $cvPy = New-Venv "$root\cv"

    $hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue) -or (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")
    if ($Cuda -ne 'cpu' -and -not $hasNvidia) {
        Write-Host '   нет nvidia, ставлю cpu' -ForegroundColor Yellow
        $Cuda = 'cpu'
    }

    # torch и torchvision с одного индекса, иначе с PyPI приедет cpu-сборка.
    Step "torch ($Cuda), ~3 ГБ"
    Run $cvPy (@('-m', 'pip', 'install', 'torch', 'torchvision', '--index-url', "https://download.pytorch.org/whl/$Cuda") + $pip) 'torch'

    Step 'зависимости cv'
    Run $cvPy (@('-m', 'pip', 'install', '-r', "$root\cv\requirements.txt") + $pip) 'зависимости cv'

    Step 'gpu'
    $torchInfo = & $cvPy -c "import torch; print(torch.__version__, torch.version.cuda or 'cpu', torch.cuda.is_available())" 2>$null
    Write-Host "   $torchInfo"
    $cudaOk = [bool]($torchInfo -match 'True$')
    if ($Cuda -ne 'cpu' -and -not $cudaOk) {
        Write-Host '[??] torch не видит gpu, будет cpu' -ForegroundColor Yellow
        Write-Host '     можно попробовать: .\install.ps1 -Cuda cu128 -Force' -ForegroundColor Yellow
    }

    Step 'конфиг'
    $device = 'cpu'
    if ($cudaOk) { $device = '0' }
    & $cvPy "$root\tools\make_config.py" $device
    if ($LASTEXITCODE -ne 0) { Fail 'не собрался cv\config.laptop.yaml' }

    Step 'модель'
    & $cvPy "$root\tools\bench.py"
    if ($LASTEXITCODE -ne 0) { Fail 'модель не загрузилась' }
}

Write-Host ''
Write-Host 'готово, дальше .\camtest.ps1 и .\run.ps1' -ForegroundColor Green
Stop-Transcript | Out-Null
