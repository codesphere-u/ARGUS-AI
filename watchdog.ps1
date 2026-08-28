# RIA Forex Bot — process + heartbeat watchdog (Windows)
#
# Schedule in Task Scheduler:
#   Trigger: every 5 minutes
#   Action:  powershell.exe -ExecutionPolicy Bypass -File "C:\Users\darka\Desktop\ria forex bot\watchdog.ps1"
#
# Alerts via Telegram using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env

$ErrorActionPreference = "SilentlyContinue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$LogDir = Join-Path $Root "data\logs"
$BotLog = Join-Path $LogDir "bot.log"
$WatchLog = Join-Path $LogDir "watchdog.log"
$PidFile = Join-Path $LogDir "bot.pid"
$EnvFile = Join-Path $Root ".env"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Watch([string]$msg) {
    $line = "{0:u} | {1}" -f (Get-Date).ToUniversalTime(), $msg
    Add-Content -Path $WatchLog -Value $line
}

function Get-DotEnv([string]$key) {
    if (-not (Test-Path $EnvFile)) { return $null }
    $m = Select-String -Path $EnvFile -Pattern ("^\s*{0}\s*=\s*(.*)$" -f [regex]::Escape($key)) | Select-Object -First 1
    if (-not $m) { return $null }
    $val = $m.Matches[0].Groups[1].Value.Trim()
    if ($val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length - 2) }
    return $val
}

function Send-Telegram([string]$text) {
    $token = Get-DotEnv "TELEGRAM_BOT_TOKEN"
    $chat = Get-DotEnv "TELEGRAM_CHAT_ID"
    if (-not $token -or -not $chat) {
        Write-Watch "Telegram not configured — skip alert: $text"
        return
    }
    $firstChat = ($chat -split ",")[0].Trim()
    $uri = "https://api.telegram.org/bot$token/sendMessage"
    try {
        Invoke-RestMethod -Method Post -Uri $uri -Body @{
            chat_id = $firstChat
            text = $text
            disable_web_page_preview = "true"
        } | Out-Null
    } catch {
        Write-Watch "Telegram alert failed: $_"
    }
}

function Get-BotProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object {
            $_.CommandLine -and
            ($_.CommandLine -match 'main_bot\.py') -and
            ($_.CommandLine -match '\brun\b')
        }
}

function Start-Bot {
    Write-Watch "Starting python main_bot.py run"
    $out = Join-Path $LogDir "bot_stdout.log"
    $err = Join-Path $LogDir "bot_stderr.log"
    $p = Start-Process -FilePath "python" `
        -ArgumentList "main_bot.py", "run" `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -WindowStyle Hidden `
        -PassThru
    if ($p) {
        Set-Content -Path $PidFile -Value $p.Id
        Write-Watch "Started pid=$($p.Id)"
        Send-Telegram "Watchdog restarted RIA bot (pid=$($p.Id)). Check MT5 AutoTrading."
    } else {
        Write-Watch "Failed to start bot process"
        Send-Telegram "Watchdog FAILED to restart RIA bot — check the machine."
    }
}

function Test-HeartbeatFresh {
    # Heartbeat is every ~300s; allow 12 minutes of silence before declaring a hang
    $maxAgeSec = 720
    if (-not (Test-Path $BotLog)) { return $true }  # no log yet — not a hang signal
    $info = Get-Item $BotLog
    $age = (Get-Date) - $info.LastWriteTime
    if ($age.TotalSeconds -gt $maxAgeSec) {
        Write-Watch ("bot.log stale ({0:n0}s) — possible hang" -f $age.TotalSeconds)
        return $false
    }
    # Prefer an explicit Heartbeat line in the last ~20 KB
    try {
        $tail = Get-Content $BotLog -Tail 80 -ErrorAction SilentlyContinue
        $hit = $tail | Where-Object { $_ -match 'Heartbeat\s*\|' } | Select-Object -Last 1
        if (-not $hit) { return $true }
    } catch {
        return $true
    }
    return $true
}

$procs = @(Get-BotProcesses)
if ($procs.Count -eq 0) {
    Write-Watch "No main_bot.py run process found"
    Start-Bot
    exit 0
}

if ($procs.Count -gt 1) {
    Write-Watch ("Multiple bot processes ({0}) — leaving alone (poll lock should refuse extras)" -f $procs.Count)
}

if (-not (Test-HeartbeatFresh)) {
    $oldest = $procs | Sort-Object CreationDate | Select-Object -First 1
    Write-Watch "Heartbeat stale with process alive (pid=$($oldest.ProcessId)) — alerting"
    Send-Telegram ("Watchdog: RIA bot process alive (pid={0}) but log heartbeat is STALE. Check MT5 / hang." -f $oldest.ProcessId)
}

Write-Watch ("OK — {0} bot process(es) running" -f $procs.Count)
exit 0
