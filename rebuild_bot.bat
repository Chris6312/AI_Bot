@echo off
echo.
echo ========================================
echo  Rebuilding Bot with Dependencies
echo ========================================
echo.

REM Stop containers
echo [1/4] Stopping containers...
docker-compose down

REM Rebuild backend with new dependencies
echo.
echo [2/4] Rebuilding backend container...
echo (This will take 1-2 minutes to install pandas, numpy, ta)
docker-compose build backend --no-cache

REM Start containers
echo.
echo [3/4] Starting containers...
docker-compose up -d

REM Wait for containers to be ready
echo.
echo [4/4] Waiting for services to start...
timeout /t 10 /nobreak >nul

REM Verify installation
echo.
echo ========================================
echo  Verifying Installation
echo ========================================
echo.

echo Checking if 'ta' module is installed...
docker-compose exec -T backend python -c "import ta; print('✓ ta version:', ta.__version__)" 2>nul
if errorlevel 1 (
    echo ✗ ta module not found!
    echo.
    echo Try running manually:
    echo   docker-compose exec backend pip install pandas numpy ta
    echo.
) else (
    echo.
    echo Checking if 'pandas' module is installed...
    docker-compose exec -T backend python -c "import pandas; print('✓ pandas version:', pandas.__version__)" 2>nul
    
    echo.
    echo Checking if 'numpy' module is installed...
    docker-compose exec -T backend python -c "import numpy; print('✓ numpy version:', numpy.__version__)" 2>nul
    
    echo.
    echo ========================================
    echo  Build Complete!
    echo ========================================
    echo.
    echo You can now run:
    echo   .\screen_crypto.bat
    echo.
)

pause
