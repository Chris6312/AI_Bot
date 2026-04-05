# AI Bot Supervisor - Starts local backend/frontend and Docker infra in Windows Terminal tabs
# Run from project root directory OR scripts directory

param(
    [switch]$SkipDocker,
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [switch]$SkipWorkers
)

$ErrorActionPreference = "Stop"

# Detect project root
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ProjectRoot = if ((Split-Path $ScriptDir -Leaf) -ieq "scripts") {
    Split-Path $ScriptDir -Parent
} else {
    $ScriptDir
}

Write-Host "=== AI Bot Supervisor ===" -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot" -ForegroundColor Gray

$PowerShellExe = if (Get-Command "pwsh" -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
$script:WtTabQueue = @()

function Get-DotEnvValue {
    param(
        [AllowEmptyString()]
        [string]$RawValue
    )

    if ($null -eq $RawValue) {
        return ""
    }

    if ($RawValue.Length -eq 0) {
        return ""
    }

    $inSingleQuote = $false
    $inDoubleQuote = $false
    $builder = New-Object System.Text.StringBuilder

    foreach ($char in $RawValue.ToCharArray()) {
        if ($char -eq "'" -and -not $inDoubleQuote) {
            $inSingleQuote = -not $inSingleQuote
            [void]$builder.Append($char)
            continue
        }

        if ($char -eq '"' -and -not $inSingleQuote) {
            $inDoubleQuote = -not $inDoubleQuote
            [void]$builder.Append($char)
            continue
        }

        if ($char -eq '#' -and -not $inSingleQuote -and -not $inDoubleQuote) {
            break
        }

        [void]$builder.Append($char)
    }

    $value = $builder.ToString().Trim()

    if ($value.Length -ge 2) {
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }

    return $value
}

function Import-DotEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvPath,
        [switch]$Quiet
    )

    if (-not (Test-Path $EnvPath)) {
        if (-not $Quiet) {
            Write-Warning "Env file not found: $EnvPath"
        }
        return
    }

    foreach ($rawLine in Get-Content $EnvPath) {
        $line = $rawLine.Trim()

        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }

        if ($line.StartsWith("#")) {
            continue
        }

        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            continue
        }

        $name = $matches[1]
        $rawValue = if ($matches.Count -ge 3) { $matches[2] } else { "" }
        $value = Get-DotEnvValue -RawValue $rawValue

        [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Get-VenvInfo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BackendPath
    )

    $candidates = @(
        (Join-Path $BackendPath ".venv"),
        (Join-Path $BackendPath "venv")
    )

    foreach ($candidate in $candidates) {
        $pythonExe = Join-Path $candidate "Scripts\python.exe"
        if (Test-Path $pythonExe) {
            return [pscustomobject]@{
                Exists    = $true
                Path      = $candidate
                PythonExe = $pythonExe
            }
        }
    }

    $defaultPath = Join-Path $BackendPath ".venv"
    return [pscustomobject]@{
        Exists    = $false
        Path      = $defaultPath
        PythonExe = (Join-Path $defaultPath "Scripts\python.exe")
    }
}

function New-EncodedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScriptText
    )

    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ScriptText))
}

function Add-WtTab {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,
        [Parameter(Mandatory = $true)]
        [string]$ScriptText,
        [string]$WorkingDirectory = $ProjectRoot
    )

    $encoded = New-EncodedCommand -ScriptText $ScriptText
    $tabArgs = "new-tab --title `"$Title`" -d `"$WorkingDirectory`" $PowerShellExe -NoExit -EncodedCommand $encoded"
    $script:WtTabQueue += $tabArgs
}

function Start-WtTabs {
    if ($script:WtTabQueue.Count -eq 0) {
        return
    }

    $wt = Get-Command "wt" -ErrorAction SilentlyContinue
    if (-not $wt) {
        Write-Warning "Windows Terminal (wt.exe) not found in PATH."
        return
    }

    $joinedTabs = $script:WtTabQueue -join " ; "
    Start-Process -FilePath $wt.Source -ArgumentList "-w 0 $joinedTabs" -WorkingDirectory $ProjectRoot | Out-Null
}

function Wait-ForContainerReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ContainerName,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        $status = & docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $ContainerName 2>$null

        if ($LASTEXITCODE -eq 0) {
            $status = ($status | Out-String).Trim()

            if ($status -in @("healthy", "running")) {
                Write-Host "✓ $ContainerName is $status" -ForegroundColor Green
                return
            }
        }

        Start-Sleep -Seconds 2
    }

    throw "Timed out waiting for container '$ContainerName' to become ready."
}

function Stop-DockerBackendContainerIfRunning {
    $status = & docker ps --filter "name=^/trading_bot_backend$" --format "{{.Status}}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        return
    }

    $status = ($status | Out-String).Trim()
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Write-Host "Stopping Docker backend container to avoid port 8000 conflicts..." -ForegroundColor Gray
        & docker stop trading_bot_backend | Out-Null
    }
}

$RootEnvPath = Join-Path $ProjectRoot ".env"
Import-DotEnv -EnvPath $RootEnvPath -Quiet

# 1. Start Docker infra only
if (-not $SkipDocker) {
    Write-Host "`n[1/4] Starting Docker infrastructure..." -ForegroundColor Yellow

    $composePath = Join-Path $ProjectRoot "docker-compose.yml"
    if (Test-Path $composePath) {
        Push-Location $ProjectRoot
        try {
            & docker compose up -d postgres redis
            if ($LASTEXITCODE -ne 0) {
                throw "Docker compose failed to start postgres/redis."
            }

            Stop-DockerBackendContainerIfRunning

            Write-Host "Waiting for PostgreSQL and Redis to become ready..." -ForegroundColor Gray
            Wait-ForContainerReady -ContainerName "trading_bot_postgres" -TimeoutSeconds 120
            Wait-ForContainerReady -ContainerName "trading_bot_redis" -TimeoutSeconds 120

            Write-Host "✓ Docker infrastructure started" -ForegroundColor Green
        }
        finally {
            Pop-Location
        }
    } else {
        Write-Warning "docker-compose.yml not found at $ProjectRoot"
    }
} else {
    Write-Host "`n[1/4] Skipping Docker (--SkipDocker)" -ForegroundColor Gray
}

# 2. Start Backend
if (-not $SkipBackend) {
    Write-Host "`n[2/4] Starting Backend..." -ForegroundColor Yellow

    $backendPath = Join-Path $ProjectRoot "backend"
    if (Test-Path $backendPath) {
        $venvInfo = Get-VenvInfo -BackendPath $backendPath

        if (-not $venvInfo.Exists) {
            Write-Host "Creating backend virtual environment at $($venvInfo.Path)..." -ForegroundColor Gray
            Push-Location $backendPath
            try {
                python -m venv $venvInfo.Path
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed to create backend virtual environment."
                }

                & $venvInfo.PythonExe -m pip install --upgrade pip
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed to upgrade pip."
                }

                if (Test-Path (Join-Path $backendPath "requirements.txt")) {
                    & $venvInfo.PythonExe -m pip install -r requirements.txt
                    if ($LASTEXITCODE -ne 0) {
                        throw "Failed to install backend requirements."
                    }
                }
            }
            finally {
                Pop-Location
            }
        }

        $backendScript = @'
function Get-DotEnvValue {
    param(
        [AllowEmptyString()]
        [string]$RawValue
    )

    if ($null -eq $RawValue) {
        return ""
    }

    if ($RawValue.Length -eq 0) {
        return ""
    }

    $inSingleQuote = $false
    $inDoubleQuote = $false
    $builder = New-Object System.Text.StringBuilder

    foreach ($char in $RawValue.ToCharArray()) {
        if ($char -eq "'" -and -not $inDoubleQuote) {
            $inSingleQuote = -not $inSingleQuote
            [void]$builder.Append($char)
            continue
        }

        if ($char -eq '"' -and -not $inSingleQuote) {
            $inDoubleQuote = -not $inDoubleQuote
            [void]$builder.Append($char)
            continue
        }

        if ($char -eq '#' -and -not $inSingleQuote -and -not $inDoubleQuote) {
            break
        }

        [void]$builder.Append($char)
    }

    $value = $builder.ToString().Trim()

    if ($value.Length -ge 2) {
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }

    return $value
}

function Import-DotEnv {
    param([string]$EnvPath)

    if (-not (Test-Path $EnvPath)) {
        return
    }

    foreach ($rawLine in Get-Content $EnvPath) {
        $line = $rawLine.Trim()

        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) {
            continue
        }

        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            continue
        }

        $name = $matches[1]
        $rawValue = if ($matches.Count -ge 3) { $matches[2] } else { "" }
        $value = Get-DotEnvValue -RawValue $rawValue

        [System.Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

function Set-LocalDevConnectionOverrides {
    $databaseUrl = [System.Environment]::GetEnvironmentVariable('DATABASE_URL', 'Process')
    if (-not [string]::IsNullOrWhiteSpace($databaseUrl)) {
        $updatedDatabaseUrl = $databaseUrl `
            -replace '(@)postgres([:/])', '$1localhost$2' `
            -replace '(://)postgres([:/])', '$1localhost$2'

        if ($updatedDatabaseUrl -ne $databaseUrl) {
            [System.Environment]::SetEnvironmentVariable('DATABASE_URL', $updatedDatabaseUrl, 'Process')
            Write-Host 'Rewrote DATABASE_URL host from postgres to localhost for local backend.' -ForegroundColor Yellow
        }
    }

    $redisUrl = [System.Environment]::GetEnvironmentVariable('REDIS_URL', 'Process')
    if (-not [string]::IsNullOrWhiteSpace($redisUrl)) {
        $updatedRedisUrl = $redisUrl `
            -replace '(@)redis([:/])', '$1localhost$2' `
            -replace '(://)redis([:/])', '$1localhost$2'

        if ($updatedRedisUrl -ne $redisUrl) {
            [System.Environment]::SetEnvironmentVariable('REDIS_URL', $updatedRedisUrl, 'Process')
            Write-Host 'Rewrote REDIS_URL host from redis to localhost for local backend.' -ForegroundColor Yellow
        }
    }

    $postgresHost = [System.Environment]::GetEnvironmentVariable('POSTGRES_HOST', 'Process')
    if ($postgresHost -eq 'postgres') {
        [System.Environment]::SetEnvironmentVariable('POSTGRES_HOST', 'localhost', 'Process')
    }

    $redisHost = [System.Environment]::GetEnvironmentVariable('REDIS_HOST', 'Process')
    if ($redisHost -eq 'redis') {
        [System.Environment]::SetEnvironmentVariable('REDIS_HOST', 'localhost', 'Process')
    }
}

Set-Location "__PROJECT_ROOT__"
Import-DotEnv "__ROOT_ENV__"
Set-LocalDevConnectionOverrides
Set-Location "__BACKEND_PATH__"
Write-Host "AI Bot Backend, local FastAPI with embedded watchlist/exit workers" -ForegroundColor Cyan
& "__PYTHON_EXE__" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
'@

        $backendScript = $backendScript.Replace("__PROJECT_ROOT__", $ProjectRoot)
        $backendScript = $backendScript.Replace("__ROOT_ENV__", $RootEnvPath)
        $backendScript = $backendScript.Replace("__BACKEND_PATH__", $backendPath)
        $backendScript = $backendScript.Replace("__PYTHON_EXE__", $venvInfo.PythonExe)

        Add-WtTab -Title "AI Bot - Backend" -ScriptText $backendScript -WorkingDirectory $backendPath
        Write-Host "✓ Backend queued (http://localhost:8000)" -ForegroundColor Green
    } else {
        Write-Warning "backend/ not found at $backendPath"
    }
} else {
    Write-Host "`n[2/4] Skipping Backend (--SkipBackend)" -ForegroundColor Gray
}

# 3. Start Frontend
if (-not $SkipFrontend) {
    Write-Host "`n[3/4] Starting Frontend..." -ForegroundColor Yellow

    $frontendPath = if (Test-Path (Join-Path $ProjectRoot "frontend")) {
        Join-Path $ProjectRoot "frontend"
    } elseif (Test-Path (Join-Path $ProjectRoot "ui")) {
        Join-Path $ProjectRoot "ui"
    } else {
        $null
    }

    if ($frontendPath) {
        if (-not (Test-Path (Join-Path $frontendPath "node_modules"))) {
            Write-Host "Running npm install for frontend..." -ForegroundColor Gray
            Push-Location $frontendPath
            try {
                npm install
                if ($LASTEXITCODE -ne 0) {
                    throw "npm install failed."
                }
            }
            finally {
                Pop-Location
            }
        }

        $frontendPrelude = ""
        if (-not $SkipBackend) {
            $frontendPrelude = @'
$apiBaseUrl = [System.Environment]::GetEnvironmentVariable('VITE_API_BASE_URL', 'Process')
if ([string]::IsNullOrWhiteSpace($apiBaseUrl)) {
    $apiBaseUrl = 'http://127.0.0.1:8000'
}

$apiBaseUrl = $apiBaseUrl.TrimEnd('/')
$healthUrl = "$apiBaseUrl/health"
$backendReady = $false

Write-Host "Waiting for backend readiness at $healthUrl ..." -ForegroundColor Gray

for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
        $backendReady = $true
        break
    }
    catch {
        Start-Sleep -Seconds 2
    }
}

if ($backendReady) {
    Write-Host "Backend is responding. Starting frontend..." -ForegroundColor Green
} else {
    Write-Warning "Backend did not become ready before frontend startup. Starting frontend anyway."
}
'@
        }

        $frontendScript = @'
Set-Location "__FRONTEND_PATH__"
Write-Host "AI Bot Frontend" -ForegroundColor Cyan
__BACKEND_WAIT__
npm run dev
'@

        $frontendScript = $frontendScript.Replace("__FRONTEND_PATH__", $frontendPath)
        $frontendScript = $frontendScript.Replace("__BACKEND_WAIT__", $frontendPrelude)

        Add-WtTab -Title "AI Bot - Frontend" -ScriptText $frontendScript -WorkingDirectory $frontendPath
        Write-Host "✓ Frontend queued (http://localhost:5173)" -ForegroundColor Green
    } else {
        Write-Host "No frontend found, skipping" -ForegroundColor Gray
    }
} else {
    Write-Host "`n[3/4] Skipping Frontend (--SkipFrontend)" -ForegroundColor Gray
}

# 4. Workers
Write-Host "`n[4/4] Worker orchestration..." -ForegroundColor Yellow
if ($SkipWorkers) {
    Write-Host "SkipWorkers specified. No separate worker tabs are launched anyway." -ForegroundColor Gray
}
Write-Host "Watchlist monitoring and exit workers are hosted inside backend app lifespan." -ForegroundColor Gray
Write-Host "✓ No standalone worker tabs required" -ForegroundColor Green

# Launch queued tabs
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
Write-Host "  • Docker infra: PostgreSQL + Redis" -ForegroundColor White
Write-Host "  • Backend: local uvicorn app.main:app" -ForegroundColor White
Write-Host "  • Frontend: Vite dev server" -ForegroundColor White
Write-Host "  • Workers: embedded in backend lifespan" -ForegroundColor White
Write-Host "`nUse .\scripts\stop-bot.ps1 to shut things down" -ForegroundColor Yellow