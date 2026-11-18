@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo [ERROR] 找不到虛擬環境，請先執行 install_windows.bat 安裝依賴！
    pause
    exit /b 1
)

echo ========================================================
echo       通用生產離散事件模擬系統 (啟動選單)
echo ========================================================
echo  [1] 啟動 Streamlit 網頁版介面 (預設，按 Enter 直接執行)
echo  [2] 執行動態每日資料模擬 (少資料/極端情境：3~5 筆與 0 筆)
echo  [3] 執行動態每日資料模擬 (充足資料情境：28~30 筆)
echo ========================================================
set /p choice="請選擇執行模式 (1/2/3，預設 1): "

if "%choice%"=="2" (
    echo.
    echo 正在啟動「動態每日資料離散模擬 - 少資料/冷啟動模式」...
    call .venv\Scripts\python.exe dynamic_daily_simulator.py --mode sparse
    echo.
    pause
    exit /b 0
)

if "%choice%"=="3" (
    echo.
    echo 正在啟動「動態每日資料離散模擬 - 充足資料模式」...
    call .venv\Scripts\python.exe dynamic_daily_simulator.py --mode plenty
    echo.
    pause
    exit /b 0
)

echo.
echo 正在啟動 Streamlit 網頁介面 (http://localhost:8501)...
call .venv\Scripts\python.exe -m streamlit run app.py
pause
