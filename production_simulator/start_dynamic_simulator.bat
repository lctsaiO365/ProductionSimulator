@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo [ERROR] 找不到虛擬環境，請先執行 install_windows.bat 安裝依賴！
    pause
    exit /b 1
)

echo ========================================================
echo   執行動態每日資料離散事件模擬 (少資料/極端情境測試)
echo ========================================================
echo.
call .venv\Scripts\python.exe dynamic_daily_simulator.py --mode sparse
echo.
pause
