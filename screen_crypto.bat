@echo off
echo.
echo ========================================
echo  Crypto Momentum Screening
echo ========================================
echo.

REM Make sure Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not running!
    echo Please start Docker Desktop and try again.
    echo.
    pause
    exit /b 1
)

REM Run screening inside Docker container
docker-compose exec -T backend python scripts/screen_crypto.py

if errorlevel 1 (
    echo.
    echo ERROR: Script failed. Check if bot is running:
    echo   docker-compose ps
    echo.
)

echo.
echo ========================================
echo  Screening Complete
echo ========================================
echo.
pause
