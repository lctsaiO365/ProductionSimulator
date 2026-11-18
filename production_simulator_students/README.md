# 通用生產離散事件模擬器（學生組別共用版）

此套件可在 Windows / macOS / Linux 筆電本機執行，無需伺服器或 API。使用 Python 3.10 以上版本，建議 3.11 或 3.12。

## 安裝與啟動

**Windows**
1. 安裝 Python（https://www.python.org/downloads/）
   ，安裝時勾選「Add Python to PATH」。
2. 解壓縮 ZIP 至可寫入的資料夾。
3. 雙擊 `install_windows.bat` 安裝依賴（首次需要網路）。
4. 雙擊 `start_windows.bat` 開啟網頁介面。一般會在 `http://localhost:8501`。

**macOS / Linux**
1. 確認已安裝 Python 3.10+。
2. 在終端機進入解壓縮資料夾。
3. 執行 `bash install_mac_linux.sh`。
4. 執行 `bash start_mac_linux.sh`。
5. 瀏覽器開啟 `http://localhost:8501`。

也可以手動執行：
```bash
python -m venv .venv
# Windows: .venv\Scripts\python -m pip install -r requirements.txt
# macOS/Linux: .venv/bin/python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## 使用步驟
1. 開啟網頁，預設會載入 `examples/` 的範例資料。
2. 上傳各組的歷史 CSV 和 Flow JSON（也可先用範例測試）。
3. 選擇下一輪目標數量，勾選各站預備完成的產品套組。
4. 選擇 Bootstrap / Mean / P25 / P75，按「執行模擬」。
5. 查看完工時間、排程甘特圖與稼動率，下載結果 ZIP。

## 歷史 CSV
必要欄位：`station_id,duration_sec`。可保留 `round_id,product,start_time,finish_time,status` 等其他欄位。
- `duration_sec`：每件加工持續時間，必須是正數，單位秒。
- `station_id`：必須與 Flow 工站名稱完全一致。
- 若 CSV 包含 `status`，僅使用 complete/completed/success/finished/done/ok/完成 的紀錄；不需要篩選可移除該欄。
- 每站至少需一筆有效加工時間。若僅 8～18 筆，Bootstrap 的尾端風險估計不穩定。
- 此版不使用歷史開始/完成時間來推估額外等待；它們可供後續回測使用。

## Flow JSON
```json
{
  "stations": {
    "A": {"inputs": []},
    "B": {"inputs": []},
    "C": {"inputs": ["A", "B"]}
  },
  "output_station": "C"
}
```
支援任意工站數量、DAG 合流及分流；每條上游邊對每件產品需一件同編號物料。第一版僅指定單一最終輸出站。

## 初始 WIP 語意
- `initial_ready_wip`: 指定工站已經預先準備好的完整物料套組；例如 `{"C":["P001"]}` 表示 C 可立即加工 P001，A/B 不必為 C 再生產 P001。
- `initial_edge_inventory`: 指定已在上游到下游邊上的單件庫存，例如 `{"A->C":["P001"]}`，C 仍須等 B 的 P001。
- **同一產品不可同時出現在同一工站的 Ready WIP 和其入站邊庫存，也不可同時預備於同一條路徑的上下游工站。**
- CLI 情境中 `initial_ready_wip` 也可使用整數（例如 `"C":2`），表示依產品清單取前兩件；網頁介面則可直接選擇產品 ID。
- 預備 WIP 的事前製造時間不包含在模擬起算時間內。比較方案時應額外評估備料成本與時間。

## 模擬假設
- 每站一台設備，同時最多加工一件；依產品 ID 順序排隊。
- 不可中斷、無搬運時間、無停機/換線延遲、原料源頭可隨時開工。
- 完工時產出給所有下游的同編號物料 token；每個下游獨立消耗。此為可分流/複製 token 的抽象製程模型，若實際分流會消耗實體且不能複製，需擴充 BOM/分流規則。
- 加工時間採各站歷史資料的 Mean、Bootstrap（有放回抽樣）、P25 或 P75。
- 使用相同 seed 可重現相同情境的結果。比較不同 WIP 情境時，抽樣消耗順序可能不同，因此不保證逐件共用相同抽樣。
- 模擬次數不等於新增真實觀測資料；少量歷史數據時 P90 只代表模型內不確定性。

## CLI（不開啟網頁）
```bash
python run_cli.py --history examples/history_example.csv --flow examples/flow_example.json --scenario examples/scenario_example.json --model bootstrap --runs 1000 --seed 42
```
輸出到 `outputs/`。

## 測試
```bash
python -m unittest discover -s tests -v
```

## 目前限制與後續擴充
- 尚未提供跨輪回測、停機/換線模型、多人/多機台資源、搬運時間、不同產品 BOM、動態補料策略。
- 同一輪的相關性未建模；歷史輪次偏快/偏慢的影響可在資料足夠後增加 round-aware 抽樣。
- 如果需要支援真正的物料分流（同一件物料只能送往一個下游），需先定義產線分流語意。
