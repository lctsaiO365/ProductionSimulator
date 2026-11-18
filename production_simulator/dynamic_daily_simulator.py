"""
動態每日資料離散事件模擬器 (Dynamic Daily Production Simulator)
-----------------------------------------------------------------
設計原則：
1. 完全不修改 production_simulator/ 核心程式碼，以外圍適應層 (Adapter) 運作。
2. 解決每日動態資料「某天 30 筆」或「某天僅 5 筆 / 0 筆」的小樣本與冷啟動問題。
3. 支援純當日資料模擬、統計數據增強 (Augmentation) 與 SOP 標稱保底。
4. 預留未來的 SQL 連接介面 (SqlDataProvider)。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

# 直接引用現有核心，不破壞原有結構
from production_simulator.core import (
    validate_flow,
    load_history,
    simulate_many,
    ConfigError,
)

# 預設各工站的 SOP 標準工時 (秒)，當某工站當天 0 筆時作為保底基線
DEFAULT_SOP_TIMES: Dict[str, float] = {
    "ST01": 3.2,
    "ST02": 6.8,
    "ST03": 10.5,
    "ST04": 5.0,
    "ST05": 8.5,
}


class DailyDataGuard:
    """
    動態當日資料守護與增強器：
    - 根據工站當天採集筆數 N，自動切換最佳採樣策略
    - N >= 15: 資料充沛，100% 使用當日真實資料
    - 1 <= N < 15: 資料偏少，保留今日真實均值並依物理變異係數 (CV) 統計增強擴充至安全母體
    - N == 0: 當日無資料，以 SOP 標稱規格與合理波動產生基線，確保模擬不中斷
    """

    def __init__(
        self,
        sop_times: Optional[Dict[str, float]] = None,
        min_safe_samples: int = 15,
        target_augmented_samples: int = 25,
        default_cv: float = 0.12,  # 變異係數 (標準差 / 平均)，製造業機台典型約 10%~15%
        random_seed: int = 42,
    ):
        self.sop_times = sop_times or DEFAULT_SOP_TIMES
        self.min_safe_samples = min_safe_samples
        self.target_augmented_samples = target_augmented_samples
        self.default_cv = default_cv
        self.rng = np.random.default_rng(random_seed)

    def process(
        self, today_df: pd.DataFrame, stations: List[str]
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        清洗並補全當日資料，返回：
        1. 供 core.load_history 使用的完整 DataFrame
        2. 當日各工站數據品質診斷表 (Quality Summary)
        """
        augmented_records = []
        diagnostics = []

        # 篩選有效欄位與有效狀態
        df = today_df.copy()
        if "duration_sec" in df.columns:
            df["duration_sec"] = pd.to_numeric(df["duration_sec"], errors="coerce")
        if "status" in df.columns:
            valid_status = ["complete", "completed", "success", "finished", "done", "ok", "完成"]
            df = df[df["status"].astype(str).str.lower().isin(valid_status)]

        for s in stations:
            station_records = np.array([])
            if "station_id" in df.columns and "duration_sec" in df.columns:
                match = df.loc[(df["station_id"].astype(str) == s) & (df["duration_sec"] > 0), "duration_sec"]
                station_records = match.to_numpy(dtype=float)

            n_raw = len(station_records)

            if n_raw >= self.min_safe_samples:
                # 【狀況 1：充沛】直接全額使用當日數據
                mode = "全當日真實數據 (High)"
                mean_val = float(np.mean(station_records))
                std_val = float(np.std(station_records))
                for val in station_records:
                    augmented_records.append({"station_id": s, "duration_sec": val})

            elif n_raw > 0:
                # 【狀況 2：少量】保留今日真實數據，並依今日均值進行統計增強
                mode = f"統計增強補齊 (Med, 今日{n_raw}筆)"
                mean_val = float(np.mean(station_records))
                # 若已有少數幾筆且有標準差則採用，否則套用製造業經驗 CV
                calc_std = float(np.std(station_records))
                std_val = calc_std if (n_raw >= 3 and calc_std > 0) else (mean_val * self.default_cv)

                # 1. 放入今日真實的 n_raw 筆
                for val in station_records:
                    augmented_records.append({"station_id": s, "duration_sec": val})

                # 2. 統計合成補足至 target_augmented_samples 筆
                needed = max(0, self.target_augmented_samples - n_raw)
                if needed > 0:
                    synth = self.rng.normal(loc=mean_val, scale=std_val, size=needed)
                    # 邊界保護：截斷在合理倍數內 (0.5 ~ 1.8 倍)
                    synth = np.clip(synth, mean_val * 0.5, mean_val * 1.8)
                    for val in synth:
                        augmented_records.append({"station_id": s, "duration_sec": round(float(val), 2)})

            else:
                # 【狀況 3：0 筆】啟用 SOP 標稱保底
                mode = "SOP標稱保底 (Low, 今日0筆)"
                sop_val = self.sop_times.get(s, 10.0)
                mean_val = sop_val
                std_val = sop_val * self.default_cv

                synth = self.rng.normal(loc=mean_val, scale=std_val, size=self.target_augmented_samples)
                synth = np.clip(synth, mean_val * 0.6, mean_val * 1.6)
                for val in synth:
                    augmented_records.append({"station_id": s, "duration_sec": round(float(val), 2)})

            diagnostics.append({
                "工站 (Station)": s,
                "今日採集筆數": n_raw,
                "最終母體筆數": len([r for r in augmented_records if r["station_id"] == s]),
                "處理策略": mode,
                "預估平均工時(秒)": round(mean_val, 2),
                "估計標準差(秒)": round(std_val, 2),
            })

        safe_df = pd.DataFrame(augmented_records)
        diag_df = pd.DataFrame(diagnostics)
        return safe_df, diag_df


class SqlDataProvider:
    """
    未來連接 SQL 資料庫的適應器 (Adapter) 模板。
    當未來部署 SQL 時，只需傳入 DB 連線字串與日期條件即可自動取數。
    """

    def __init__(self, connection_string: Optional[str] = None):
        self.connection_string = connection_string

    def fetch_today_records(self, target_date: Optional[str] = None) -> pd.DataFrame:
        """
        從 SQL 撈取指定日期（或當日）的生產日誌。
        若尚未設定 SQL 連線，則提供提示。
        """
        if not self.connection_string:
            raise NotImplementedError("尚未設定 SQL 連線字串 (connection_string)。")

        # 示範 SQL 語法：
        # SELECT station_id, duration_sec, status, finish_time
        # FROM production_logs
        # WHERE DATE(finish_time) = COALESCE(:target_date, CURDATE())
        #   AND status IN ('completed', 'done', '完成');
        try:
            import sqlalchemy
            engine = sqlalchemy.create_engine(self.connection_string)
            query = """
                SELECT station_id, duration_sec, status, finish_time
                FROM production_logs
                WHERE duration_sec > 0
            """
            return pd.read_sql(query, con=engine)
        except Exception as e:
            raise RuntimeError(f"SQL 連線或查詢失敗: {e}")


def generate_mock_dynamic_data(scenario_type: str = "sparse") -> pd.DataFrame:
    """
    產生測試用的動態當日採集資料：
    - 'sparse' (稀疏/少資料天): 部分工站 0 筆、部分工站 3~5 筆
    - 'plenty' (充足天): 每個工站皆有 25~30 筆
    """
    records = []
    if scenario_type == "sparse":
        # ST01: 5 筆
        for d in [3.1, 3.4, 2.9, 3.2, 3.0]:
            records.append({"station_id": "ST01", "duration_sec": d, "status": "completed"})
        # ST02: 3 筆
        for d in [6.5, 7.2, 6.8]:
            records.append({"station_id": "ST02", "duration_sec": d, "status": "completed"})
        # ST04: 4 筆
        for d in [4.8, 5.1, 4.9, 5.3]:
            records.append({"station_id": "ST04", "duration_sec": d, "status": "completed"})
        # 注意：ST03 (合流瓶頸站)、ST05 (最終輸出站) 在早班採集中為 0 筆！
    else:
        rng = np.random.default_rng(123)
        stations_spec = {"ST01": (3.1, 0.3), "ST02": (6.7, 0.6), "ST03": (10.4, 1.1), "ST04": (4.9, 0.4), "ST05": (8.2, 0.8)}
        for s, (mu, sigma) in stations_spec.items():
            samples = rng.normal(mu, sigma, 28)
            for d in samples:
                records.append({"station_id": s, "duration_sec": round(float(d), 2), "status": "completed"})

    return pd.DataFrame(records)


def run_dynamic_simulation(
    flow_path: str = "examples/flow_example.json",
    scenario_path: str = "examples/scenario_example.json",
    input_data_df: Optional[pd.DataFrame] = None,
    scenario_mode: str = "sparse",
    runs: int = 1000,
    seed: int = 42,
    output_dir: str = "outputs/dynamic_run",
):
    """執行完整動態資料模擬流程"""
    flow_file = Path(flow_path)
    scenario_file = Path(scenario_path)

    if not flow_file.exists() or not scenario_file.exists():
        raise FileNotFoundError("找不到 Flow 或 Scenario 設定檔。")

    flow = json.loads(flow_file.read_text(encoding="utf-8"))
    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))
    graph, output_station = validate_flow(flow)
    station_list = list(graph.keys())

    # 1. 取得當日採集資料 (若未傳入則使用模擬情境資料)
    if input_data_df is None:
        today_raw_df = generate_mock_dynamic_data(scenario_type=scenario_mode)
    else:
        today_raw_df = input_data_df

    print(f"\n========================================================")
    print(f" [動態資料離散模擬] 當日模式: {scenario_mode.upper()}")
    print(f" 原始採集紀錄共 {len(today_raw_df)} 筆")
    print(f"========================================================")

    # 2. 透過 DailyDataGuard 進行清洗、統計增強與 SOP 保底
    guard = DailyDataGuard(min_safe_samples=15, target_augmented_samples=25, random_seed=seed)
    safe_df, quality_df = guard.process(today_raw_df, station_list)

    # 顯示各工站數據品質報告
    print("\n【當日各工站數據品質與處理策略診斷】")
    print(quality_df.to_string(index=False))

    # 3. 呼叫既有的核心程式 (100% 沿用原本的 core.load_history 與 core.simulate_many)
    station_history = load_history(safe_df, station_list)
    trials, stats, example = simulate_many(
        graph=graph,
        output=output_station,
        history=station_history,
        scenario=scenario,
        model="bootstrap",
        runs=runs,
        seed=seed,
    )

    # 4. 輸出結果摘要
    print("\n【模擬預測結果 (Bootstrap 1000次)】")
    print(f" ▶ 平均總完工時間: {stats['mean_sec']:.2f} 秒")
    print(f" ▶ 中位數 (P50)   : {stats['p50_sec']:.2f} 秒")
    print(f" ▶ 90百分位 (P90) : {stats['p90_sec']:.2f} 秒 (保守風險評估)")
    print(f" ▶ 最快完工時間   : {stats['min_sec']:.2f} 秒")
    print(f" ▶ 最慢完工時間   : {stats['max_sec']:.2f} 秒")

    # 5. 存檔
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    quality_df.to_csv(out_path / "data_quality_report.csv", index=False, encoding="utf-8-sig")
    trials.to_csv(out_path / "simulation_trials.csv", index=False)
    example.schedule.to_csv(out_path / "example_schedule.csv", index=False)
    example.station_summary.to_csv(out_path / "station_summary.csv", index=False)
    (out_path / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完整模擬報表已輸出至: {out_path.resolve()}\n")
    return stats, quality_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="動態每日資料離散模擬測試程式")
    parser.add_argument("--mode", choices=["sparse", "plenty"], default="sparse",
                        help="測試情境：sparse (少量/零資料天，如 3~5 筆或 0 筆)；plenty (充足天，25~30 筆)")
    parser.add_argument("--runs", type=int, default=1000, help="模擬次數 (預設 1000)")
    parser.add_argument("--seed", type=int, default=42, help="隨機種子")
    parser.add_argument("--csv", type=str, default="", help="可選：自訂當日 CSV 檔案路徑")
    args = parser.parse_args()

    custom_df = pd.read_csv(args.csv) if args.csv else None
    run_dynamic_simulation(scenario_mode=args.mode, runs=args.runs, seed=args.seed, input_data_df=custom_df)
