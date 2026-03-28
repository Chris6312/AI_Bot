# AI Bot Stop Script - Gracefully shuts down all services
# Run from project root directory OR scripts directory

$ErrorActionPreference = "Continue"

# Detect project root (go up one level if we're in scripts/)
$ScriptDir = $PSScriptRoot
if ($ScriptDir -like "*\scripts") {
    $ProjectRoot = Split-Path $ScriptDir -Parent
} else {
    $ProjectRoot = $ScriptDir
}

Write-Host "=== AI Bot Shutdown ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot" -ForegroundColor Gray

# 1. Stop FastAPI Backend
Write-Host "`n[1/3] Stopping Backend (uvicorn)..." -ForegroundColor Yellow
$uvicornProcesses = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*uvicorn*main:app*"
}
if ($uvicornProcesses) {
    $uvicornProcesses | ForEach-Object {
        Write-Host "  Stopping PID $($_.Id)..." -ForegroundColor Gray
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "✓ Backend stopped" -ForegroundColor Green
} else {
    Write-Host "  No backend process found" -ForegroundColor Gray
}

# 2. Stop Frontend (npm/node)
Write-Host "`n[2/3] Stopping Frontend (npm dev server)..." -ForegroundColor Yellow
$nodeProcesses = Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*vite*" -or $_.CommandLine -like "*dev*"
}
if ($nodeProcesses) {
    $nodeProcesses | ForEach-Object {
        Write-Host "  Stopping PID $($_.Id)..." -ForegroundColor Gray
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "✓ Frontend stopped" -ForegroundColor Green
} else {
    Write-Host "  No frontend process found" -ForegroundColor Gray
}

# Stop any worker processes
Write-Host "Stopping Workers..." -ForegroundColor Yellow
$workerProcesses = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*worker.py*" -or $_.CommandLine -like "*workers.*"
}
if ($workerProcesses) {
    $workerProcesses | ForEach-Object {
        Write-Host "  Stopping PID $($_.Id)..." -ForegroundColor Gray
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "✓ Workers stopped" -ForegroundColor Green
} else {
    Write-Host "  No worker processes found" -ForegroundColor Gray
}

# 3. Stop Docker Compose
Write-Host "`n[3/3] Stopping Docker services..." -ForegroundColor Yellow
if (Test-Path "$ProjectRoot/docker-compose.yml") {
    Push-Location $ProjectRoot
    docker-compose down
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ Docker services stopped" -ForegroundColor Green
    } else {
        Write-Warning "Docker compose down had issues"
    }
    Pop-Location
} else {
    Write-Host "  docker-compose.yml not found at $ProjectRoot" -ForegroundColor Gray
}

Write-Host "`n=== AI Bot Stopped ===" -ForegroundColor Green
Write-Host "All services have been shut down" -ForegroundColor White
Write-Host "Note: Windows Terminal tabs may still be open - close them manually if needed" -ForegroundColor Yellow
