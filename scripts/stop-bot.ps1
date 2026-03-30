# AI Bot Supervisor - Stops local backend/frontend and optional Docker infra
# Run from project root directory OR scripts directory

param(
    [switch]$KeepDocker
)

$ErrorActionPreference = "Stop"

# Detect project root
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ProjectRoot = if ((Split-Path $ScriptDir -Leaf) -ieq "scripts") {
    Split-Path $ScriptDir -Parent
} else {
    $ScriptDir
}

Write-Host "=== AI Bot Shutdown ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot" -ForegroundColor Gray

function Get-CommandLine {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return [string]($proc.CommandLine ?? "")
    }
    catch {
        return ""
    }
}

function Get-ExecutablePath {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    try {
        $proc = Get-Process -Id $ProcessId -ErrorAction Stop
        return [string]($proc.Path ?? "")
    }
    catch {
        return ""
    }
}

function Test-IsProjectProcess {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId,
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [string[]]$Patterns = @()
    )

    $commandLine = Get-CommandLine -ProcessId $ProcessId
    $exePath = Get-ExecutablePath -ProcessId $ProcessId
    $projectRootNormalized = $ProjectRoot.ToLowerInvariant()

    if (-not [string]::IsNullOrWhiteSpace($commandLine)) {
        if ($commandLine.ToLowerInvariant().Contains($projectRootNormalized)) {
            return $true
        }

        foreach ($pattern in $Patterns) {
            if ($commandLine -match $pattern) {
                return $true
            }
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($exePath)) {
        if ($exePath.ToLowerInvariant().StartsWith($projectRootNormalized)) {
            return $true
        }
    }

    return $false
}

function Stop-ListeningProcessOnPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [string[]]$Patterns = @()
    )

    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Host "No listening process found on port $Port for $Label." -ForegroundColor Gray
        return
    }

    $processIds = $connections |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Sort-Object

    foreach ($processId in $processIds) {
        if ($processId -le 0) {
            continue
        }

        $commandLine = Get-CommandLine -ProcessId $processId
        $safeToStop = Test-IsProjectProcess -ProcessId $processId -ProjectRoot $ProjectRoot -Patterns $Patterns

        if (-not $safeToStop) {
            Write-Warning "Skipping PID $processId on port $Port because it does not look like this project's $Label process."
            if (-not [string]::IsNullOrWhiteSpace($commandLine)) {
                Write-Host "Command: $commandLine" -ForegroundColor DarkGray
            }
            continue
        }

        try {
            Write-Host "Stopping $Label on port $Port (PID $processId)..." -ForegroundColor Yellow
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Host "✓ Stopped PID $processId" -ForegroundColor Green
        }
        catch {
            Write-Warning "Failed to stop PID $processId on port $Port. $($_.Exception.Message)"
        }
    }
}

function Stop-DockerContainerIfRunning {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ContainerName
    )

    $status = & docker ps --filter "name=^/$ContainerName$" --format "{{.Status}}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        return
    }

    $status = ($status | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($status)) {
        return
    }

    Write-Host "Stopping Docker container $ContainerName..." -ForegroundColor Yellow
    & docker stop $ContainerName | Out-Null
    Write-Host "✓ Stopped $ContainerName" -ForegroundColor Green
}

function Clear-RuntimeFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    $runtimeDir = Join-Path $ProjectRoot "scripts\.runtime"
    if (-not (Test-Path $runtimeDir)) {
        return
    }

    $patterns = @(
        "*.pid",
        "*.json"
    )

    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $runtimeDir -Filter $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                Remove-Item $_.FullName -Force -ErrorAction Stop
                Write-Host "Removed runtime file: $($_.Name)" -ForegroundColor DarkGray
            }
            catch {
                Write-Warning "Could not remove runtime file $($_.FullName)"
            }
        }
    }
}

# Stop local frontend/backend first
Write-Host "`n[1/3] Stopping local app processes..." -ForegroundColor Yellow

Stop-ListeningProcessOnPort `
    -Port 8000 `
    -Label "backend" `
    -ProjectRoot $ProjectRoot `
    -Patterns @(
        'uvicorn',
        'app\.main:app'
    )

Stop-ListeningProcessOnPort `
    -Port 5173 `
    -Label "frontend" `
    -ProjectRoot $ProjectRoot `
    -Patterns @(
        'vite',
        'npm(.cmd)?\s+run\s+dev'
    )

# Stop Docker pieces
Write-Host "`n[2/3] Stopping Docker services..." -ForegroundColor Yellow

$composePath = Join-Path $ProjectRoot "docker-compose.yml"
if (Test-Path $composePath) {
    Push-Location $ProjectRoot
    try {
        if ($KeepDocker) {
            Write-Host "KeepDocker specified, leaving postgres/redis running." -ForegroundColor Gray
            Stop-DockerContainerIfRunning -ContainerName "trading_bot_backend"
        }
        else {
            Write-Host "Stopping compose services..." -ForegroundColor Gray
            & docker compose down --remove-orphans
            if ($LASTEXITCODE -ne 0) {
                throw "docker compose down failed."
            }
            Write-Host "✓ Docker services stopped" -ForegroundColor Green
        }
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "docker-compose.yml not found, skipping compose shutdown." -ForegroundColor Gray
    if (-not $KeepDocker) {
        Stop-DockerContainerIfRunning -ContainerName "trading_bot_backend"
        Stop-DockerContainerIfRunning -ContainerName "trading_bot_postgres"
        Stop-DockerContainerIfRunning -ContainerName "trading_bot_redis"
    }
}

# Cleanup runtime artifacts
Write-Host "`n[3/3] Cleaning runtime artifacts..." -ForegroundColor Yellow
Clear-RuntimeFiles -ProjectRoot $ProjectRoot

Write-Host "`n=== AI Bot Stopped ===" -ForegroundColor Green
Write-Host "Notes:" -ForegroundColor Yellow
Write-Host "  • This script does NOT change stock/crypto mode" -ForegroundColor White
Write-Host "  • This script does NOT force runtime state to PAPER" -ForegroundColor White
Write-Host "  • Use -KeepDocker if you want PostgreSQL/Redis to stay up" -ForegroundColor White