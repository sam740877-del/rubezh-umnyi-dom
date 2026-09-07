# Запуск бота «Учёта СКИ» с рабочего стола.
#
# Ярлык на рабочем столе указывает сюда. Окно не закрывать: пока оно
# открыто — бот принимает команды мастеров из MAX.
#
# Устройство взято у бота «Заявок» (C:\zayavki\tools\start-bot.ps1):
# там уже оплачены четыре урока — BOM, общий доступ к журналу,
# отсутствие 2>&1 и человеческие отказы. Изобретать своё не стал.
#
# Файл сохранён в UTF-8 С BOM и CRLF (правило Р9.10): без BOM Windows
# PowerShell 5.1 читает его как cp1251, и кириллица рассыпается.

$root = "C:\Projects\rubezh-umnyi-dom\ski-uchet"
Set-Location $root
$Host.UI.RawUI.WindowTitle = "Бот «Учёт СКИ»"
$ProgressPreference = "SilentlyContinue"

# Русский текст в консоли: без этого вывод Python в UTF-8 идёт кракозябрами.
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host ""
Write-Host "  Бот «Учёт СКИ»" -ForegroundColor Cyan
Write-Host "  ---------------" -ForegroundColor DarkGray

# 1. Токен. В отличие от «Заявок» он лежит в .env, а не в отдельном
#    файле, и manage.py читать .env не умеет — ждёт переменную
#    окружения. Читаем здесь, чтобы не тянуть зависимость ради трёх строк.
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Host ""
    Write-Host "  НЕТ ФАЙЛА .env С ТОКЕНОМ" -ForegroundColor Red
    Write-Host "  Ожидается: $envFile" -ForegroundColor Yellow
    Write-Host "  Внутри одна строка: MAX_BOT_TOKEN=<токен>" -ForegroundColor Yellow
    Write-Host "  Токен берётся в кабинете MAX Business, раздел «Боты»." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Enter — закрыть"
    return
}

foreach ($line in Get-Content $envFile -Encoding UTF8) {
    $line = $line.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { continue }
    $name = $line.Substring(0, $i).Trim()
    $value = $line.Substring($i + 1).Trim()
    # Значение в кавычках — обычная запись в .env, кавычки не часть токена.
    if ($value.Length -ge 2) {
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    Set-Item -Path "env:$name" -Value $value
}

if (-not $env:MAX_BOT_TOKEN) {
    Write-Host ""
    Write-Host "  В .env НЕТ СТРОКИ MAX_BOT_TOKEN" -ForegroundColor Red
    Write-Host "  Файл есть, но токена в нём не нашлось." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Enter — закрыть"
    return
}

# 2. Сертификаты. У MAX они выданы центром Минцифры, которого обычные
#    наборы не знают: без своего набора связь падает с
#    CERTIFICATE_VERIFY_FAILED, и выглядит это как «бот сломался».
$bundle = Join-Path $root "certs\bundle.pem"
if (-not (Test-Path $bundle)) {
    Write-Host ""
    Write-Host "  НЕТ НАБОРА СЕРТИФИКАТОВ" -ForegroundColor Red
    Write-Host "  Ожидается: $bundle" -ForegroundColor Yellow
    Write-Host "  Соберите: python tools\update_certs.py" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Enter — закрыть"
    return
}

# 3. Связь. Молчаливое падение опроса выглядит как «бот сломался».
Write-Host "  Проверяю доступ к MAX..." -ForegroundColor DarkGray
$reachable = Test-NetConnection platform-api2.max.ru -Port 443 `
    -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $reachable) {
    Write-Host ""
    Write-Host "  НЕТ ДОСТУПА К MAX" -ForegroundColor Red
    Write-Host "  Проверьте интернет и запустите ярлык заново." -ForegroundColor Yellow
    Write-Host "  MAX работает без VPN — если включён, попробуйте выключить." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Enter — закрыть"
    return
}
Write-Host "  Доступ есть." -ForegroundColor Green

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host ""
    Write-Host "  НЕТ ОКРУЖЕНИЯ .venv" -ForegroundColor Red
    Write-Host "  Ожидается: $python" -ForegroundColor Yellow
    Write-Host "  Создать: python -m venv .venv" -ForegroundColor Yellow
    Write-Host "           .venv\Scripts\pip install -r requirements.txt" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Enter — закрыть"
    return
}

# 4. Журнал: разбор вчерашнего сбоя без журнала невозможен.
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("max-bot-{0:yyyy-MM-dd_HH-mm}.log" -f (Get-Date))
Write-Host "  Полный журнал: $logFile" -ForegroundColor DarkGray

# Журнал открывается ОДИН раз и с разрешением другим процессам читать
# и писать. Иначе любой, кто откроет файл — антивирус, копирование,
# человек в блокноте, — отнимает доступ, и окно бота заливается
# красными ошибками при полностью исправном боте (урок «Заявок», 12.08.2026).
$writer = $null
try {
    $stream = New-Object System.IO.FileStream(
        $logFile,
        [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::ReadWrite
    )
    $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($true)))
    $writer.AutoFlush = $true
} catch {
    Write-Host "  Журнал недоступен, пишу только в окно: $($_.Exception.Message)" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "  БОТ ЗАПУЩЕН — окно не закрывать" -ForegroundColor Green
Write-Host "  Остановить: Ctrl+C или закрыть окно" -ForegroundColor DarkGray
Write-Host ""

# Без `2>&1`: PowerShell 5.1 завернул бы каждую строку stderr в
# NativeCommandError и выставил $? в $false при исправном выходе.
& $python -u manage.py bot | ForEach-Object {
    $line = ($_ | Out-String).TrimEnd()
    if ($line -eq "") { return }
    # Запись в журнал не имеет права мешать работе: потеря строки журнала
    # допустима, испорченный показ — нет (то же правило, что у аудита, Р9.3).
    if ($writer) {
        try { $writer.WriteLine($line) } catch { }
    }

    switch -Regex ($line) {
        'Сбой опроса' {
            Write-Host ("  [{0:HH:mm:ss}] {1}" -f (Get-Date), $line) -ForegroundColor DarkYellow
            break
        }
        'Бот запущен'           { Write-Host "  $line" -ForegroundColor Green; break }
        'Бот остановлен'        { Write-Host "  $line" -ForegroundColor Yellow; break }
        'обработано событий'    { Write-Host ("  [{0:HH:mm:ss}] {1}" -f (Get-Date), $line) -ForegroundColor Cyan; break }
        'Traceback|Error|ERROR' { Write-Host "  $line" -ForegroundColor Red; break }
        default                 { Write-Host "  $line" }
    }
}

if ($writer) { $writer.Dispose() }

Write-Host ""
Write-Host "  Бот остановлен. Подробности: $logFile" -ForegroundColor Yellow
Read-Host "Enter — закрыть"
