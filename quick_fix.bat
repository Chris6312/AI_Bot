@echo off
echo.
echo ========================================
echo  Quick Fix - Installing Dependencies
echo ========================================
echo.

echo Installing pandas, numpy, and ta in running container...
echo.

docker-compose exec backend pip install --no-cache-dir pandas==2.2.3 numpy==2.1.2 ta==0.11.0

if errorlevel 1 (
    echo.
    echo ✗ Installation failed!
    echo.
    echo Try the full rebuild instead:
    echo   .\rebuild_bot.bat
    echo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Verifying Installation
echo ========================================
echo.

docker-compose exec backend python -c "import ta; import pandas; import numpy; print('✓ All modules installed successfully!')"

if errorlevel 1 (
    echo.
    echo ✗ Verification failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Success!
echo ========================================
echo.
echo Dependencies installed. You can now run:
echo   .\screen_crypto.bat
echo.
echo NOTE: This is a temporary fix. Dependencies will be lost
echo if you rebuild the container. For permanent fix, run:
echo   .\rebuild_bot.bat
echo.

pause
