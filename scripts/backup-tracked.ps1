# Creates a zip backup of tracked project files into the root backups folder.
# Run from the project root or the scripts folder.

[CmdletBinding()]
param(
    [switch]$IncludeEnv
)

$ErrorActionPreference = 'Stop'

$scriptDir = $PSScriptRoot
$projectRoot = if ($scriptDir -like '*\scripts') { Split-Path $scriptDir -Parent } else { $scriptDir }
$backupRoot = Join-Path $projectRoot 'backups'
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$stagingRoot = Join-Path $env:TEMP "ai-bot-tracked-backup_$timestamp"
$archivePath = Join-Path $backupRoot "tracked_backup_$timestamp.zip"

Write-Host '=== Backup Tracked Files ===' -ForegroundColor Cyan
Write-Host "Project Root: $projectRoot" -ForegroundColor Gray

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git is required for backup-tracked.ps1'
}

Push-Location $projectRoot
try {
    if (-not (Test-Path '.git')) {
        throw 'No .git directory found. backup-tracked.ps1 only backs up tracked files from a git repo.'
    }

    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    if (Test-Path $stagingRoot) {
        Remove-Item -Path $stagingRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

    $trackedFiles = git ls-files
    foreach ($relativePath in $trackedFiles) {
        $sourcePath = Join-Path $projectRoot $relativePath
        if (-not (Test-Path $sourcePath)) {
            continue
        }

        $destinationPath = Join-Path $stagingRoot $relativePath
        $destinationDir = Split-Path $destinationPath -Parent
        if ($destinationDir -and -not (Test-Path $destinationDir)) {
            New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
        }
        Copy-Item -Path $sourcePath -Destination $destinationPath -Force
    }

    if ($IncludeEnv -and (Test-Path (Join-Path $projectRoot '.env'))) {
        Copy-Item -Path (Join-Path $projectRoot '.env') -Destination (Join-Path $stagingRoot '.env') -Force
    }

    if (Test-Path $archivePath) {
        Remove-Item -Path $archivePath -Force
    }
    Compress-Archive -Path (Join-Path $stagingRoot '*') -DestinationPath $archivePath -CompressionLevel Optimal

    Write-Host "Backup created: $archivePath" -ForegroundColor Green
} finally {
    Pop-Location
    if (Test-Path $stagingRoot) {
        Remove-Item -Path $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
