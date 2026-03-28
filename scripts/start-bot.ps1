# AI Bot Supervisor - Starts all services in Windows Terminal tabs
# Run from project root directory OR scripts directory

param(
    [switch]$SkipDocker,
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [switch]$SkipWorkers
)

$ErrorActionPreference = "Stop"

# Detect project root (go up one level if we're in scripts/)
$ScriptDir = $PSScriptRoot
if ($ScriptDir -like "*\scripts") {
    $ProjectRoot = Split-Path $ScriptDir -Parent
} else {
    $ProjectRoot = $ScriptDir
}

Write-Host "=== AI Bot Supervisor ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot" -ForegroundColor Gray

$PowerShellExe = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }

# Tab queue for Windows Terminal
$script:WtTabQueue = @()

function Add-WtTab {
    param(
        [string]$Title,
        [string]$Command,
        [string]$WorkingDirectory = $ProjectRoot
    )
    
    $escapedCommand = $Command.Replace('"', '\"').Replace("'", "''")
    $tabArgs = "new-tab --title `"$Title`" -d `"$WorkingDirectory`" $PowerShellExe -NoExit -Command `"$escapedCommand`""
    $script:WtTabQueue += $tabArgs
}

function Start-WtTabs {
    if ($script:WtTabQueue.Count -eq 0) {
        return
    }
    
    $wt = Get-Command 'wt' -ErrorAction SilentlyContinue
    if (-not $wt) {
        Write-Warning "Windows Terminal (wt.exe) not found in PATH"
        return
    }
    
    $joinedTabs = $script:WtTabQueue -join ' ; '
    Start-Process -FilePath $wt.Source -ArgumentList "-w 0 $joinedTabs" -WorkingDirectory $ProjectRoot | Out-Null
}

# 1. Start Docker Compose (PostgreSQL, Redis, etc.)
if (-not $SkipDocker) {
    Write-Host "`n[1/4] Starting Docker services..." -ForegroundColor Yellow
    
    if (Test-Path "$ProjectRoot/docker-compose.yml") {
        Push-Location $ProjectRoot
        docker-compose up -d
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Docker compose failed to start"
        }
        Write-Host "✓ Docker services started" -ForegroundColor Green
        Pop-Location
        
        # Wait for database to be ready
        Write-Host "Waiting for PostgreSQL to be ready..." -ForegroundColor Gray
        Start-Sleep -Seconds 5
    } else {
        Write-Warning "docker-compose.yml not found at $ProjectRoot"
    }
} else {
    Write-Host "`n[1/4] Skipping Docker (--SkipDocker)" -ForegroundColor Gray
}

# 2. Start Backend in new Windows Terminal tab
if (-not $SkipBackend) {
    Write-Host "`n[2/4] Starting Backend..." -ForegroundColor Yellow
    
    if (Test-Path "$ProjectRoot/backend") {
        # Check if virtual environment exists
        if (-not (Test-Path "$ProjectRoot/backend/venv")) {
            Write-Host "Creating Python virtual environment..." -ForegroundColor Gray
            Push-Location "$ProjectRoot/backend"
            python -m venv venv
            Pop-Location
        }
        
        # Check if requirements are installed
        $requirementsPath = "$ProjectRoot/backend/requirements.txt"
        if (Test-Path $requirementsPath) {
            Write-Host "Installing Python dependencies..." -ForegroundColor Gray
            Push-Location "$ProjectRoot/backend"
            & "venv/Scripts/python.exe" -m pip install --upgrade pip --quiet
            & "venv/Scripts/python.exe" -m pip install -r requirements.txt --quiet
            Pop-Location
        }
        
        # Queue backend tab
        $backendCommand = @"
Set-Location `"$ProjectRoot\backend`"
.\venv\Scripts\Activate.ps1
Write-Host 'AI Bot Backend - Tradier and Kraken' -ForegroundColor Cyan
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
"@
        Add-WtTab -Title "AI Bot - Backend" -Command $backendCommand -WorkingDirectory "$ProjectRoot\backend"
        Write-Host "✓ Backend queued (http://localhost:8000)" -ForegroundColor Green
    } else {
        Write-Warning "backend/ not found at $ProjectRoot/backend"
    }
} else {
    Write-Host "`n[2/4] Skipping Backend (--SkipBackend)" -ForegroundColor Gray
}

# 3. Start Frontend in new Windows Terminal tab
if (-not $SkipFrontend) {
    Write-Host "`n[3/4] Starting Frontend..." -ForegroundColor Yellow
    
    $frontendPath = if (Test-Path "$ProjectRoot/frontend") { 
        "$ProjectRoot/frontend" 
    } elseif (Test-Path "$ProjectRoot/ui") {
        "$ProjectRoot/ui"
    } else {
        $null
    }
    
    if ($frontendPath) {
        # Check if node_modules exists
        if (-not (Test-Path "$frontendPath/node_modules")) {
            Write-Host "Running npm install (first time setup)..." -ForegroundColor Gray
            Push-Location $frontendPath
            npm install
            if ($LASTEXITCODE -ne 0) {
                Write-Error "npm install failed"
            }
            Pop-Location
        }
        
        # Queue frontend tab
        $frontendCommand = @"
Set-Location `"$frontendPath`"
Write-Host 'AI Bot Frontend - Stock and Crypto Dashboard' -ForegroundColor Cyan
npm run dev
"@
        Add-WtTab -Title "AI Bot - Frontend" -Command $frontendCommand -WorkingDirectory $frontendPath
        Write-Host "✓ Frontend queued (http://localhost:5173)" -ForegroundColor Green
    } else {
        Write-Host "No frontend found - skipping" -ForegroundColor Gray
        Write-Host "Extract the frontend/ folder from the delivery ZIP to $ProjectRoot" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[3/4] Skipping Frontend (--SkipFrontend)" -ForegroundColor Gray
}

# 4. Start Workers in new Windows Terminal tab
if (-not $SkipWorkers) {
    Write-Host "`n[4/4] Starting Workers..." -ForegroundColor Yellow
    
    if (Test-Path "$ProjectRoot/backend/workers") {
        # Find all Python files in workers directory
        $workerFiles = Get-ChildItem "$ProjectRoot/backend/workers" -Filter "*.py" -File
        
        if ($workerFiles.Count -gt 0) {
            Write-Host "Found $($workerFiles.Count) worker file(s)" -ForegroundColor Gray
            
            # For each worker file, create a tab
            foreach ($workerFile in $workerFiles) {
                $workerName = $workerFile.BaseName
                $workerCommand = @"
Set-Location `"$ProjectRoot\backend`"
.\venv\Scripts\Activate.ps1
Write-Host 'AI Bot Worker - $workerName' -ForegroundColor Cyan
python -m workers.$workerName
"@
                Add-WtTab -Title "AI Bot - Worker: $workerName" -Command $workerCommand -WorkingDirectory "$ProjectRoot\backend"
                Write-Host "✓ Worker queued: $workerName" -ForegroundColor Green
            }
        } else {
            Write-Host "No worker Python files found in workers/" -ForegroundColor Gray
        }
    } else {
        Write-Host "No workers/ directory found" -ForegroundColor Gray
    }
} else {
    Write-Host "`n[4/4] Skipping Workers (--SkipWorkers)" -ForegroundColor Gray
}

# Launch all queued tabs at once
if ($script:WtTabQueue.Count -gt 0) {
    Write-Host "`nLaunching Windows Terminal tabs..." -ForegroundColor Cyan
    Start-WtTabs
} else {
    Write-Host "`nNo tabs to launch" -ForegroundColor Yellow
}

Write-Host "`n=== AI Bot Started ===" -ForegroundColor Green
Write-Host "Backend:  http://localhost:8000" -ForegroundColor Cyan
Write-Host "Frontend: http://localhost:5173" -ForegroundColor Cyan
Write-Host "Docs:     http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "`nServices:" -ForegroundColor Yellow
Write-Host "  • Stock Trading: Tradier (Live)" -ForegroundColor White
Write-Host "  • Crypto Trading: Kraken (Paper Only)" -ForegroundColor White
Write-Host "  • AI Decisions: Discord Webhook" -ForegroundColor White
Write-Host "`nAll services are running in Windows Terminal tabs" -ForegroundColor Yellow
Write-Host "Use .\scripts\stop-bot.ps1 to gracefully shut down all services" -ForegroundColor Yellow
