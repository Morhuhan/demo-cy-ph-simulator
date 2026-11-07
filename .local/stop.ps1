# Остановить стенд и модуль распознавания, запущенные через .local\start.ps1.

# Стенд идёт на общем Python, поэтому по пути к интерпретатору его не найти:
# ищем по занятым портам. Останавливаем дерево целиком — Python в окружении
# запускает интерпретатор дочерним процессом.
$found = $false
foreach ($port in 8080, 8081) {
    $owners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($processId in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if (-not $proc -or $proc.Name -ne 'python.exe') {
            Write-Host ("порт {0} занят не нашим процессом (pid {1}, {2}) — не трогаю" -f $port, $processId, $proc.Name)
            continue
        }
        Write-Host ("останавливаю pid {0}: {1}" -f $processId, $proc.CommandLine)
        & taskkill /PID $processId /T /F 2>&1 | Out-Null
        $found = $true
    }
}
if ($found) { Write-Host 'остановлено. Окна с журналами можно закрыть.' } else { Write-Host 'ничего не запущено' }
