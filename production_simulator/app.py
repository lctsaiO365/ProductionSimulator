"""
通用生產離散事件模擬器 - Streamlit 地端互動版 (1:1 網頁版風格對齊版)
----------------------------------------------------------------------
特色功能：
1. 佈局與視覺風格 100% 完整復刻網頁版 (index.html) 的左右雙欄卡片佈局與色彩風格。
2. 採用 st.pills 膠囊標籤實現水平 WIP 勾選，與網頁版完全一致。
3. 預設啟用動態當日資料守護模式 (DailyDataGuard)，適應 2~5 筆少資料與 0 筆冷啟動工站。
4. 左右對稱操作區：左側設定資料來源與模型參數，右側設定 WIP 與執行模擬。
5. 蒙地卡羅完工排程儀表板、Plotly 甘特圖、直方圖與 ZIP 下載。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from production_simulator.core import (
    ConfigError,
    load_history,
    prepare_scenario,
    simulate_many,
    validate_flow,
)

try:
    from dynamic_daily_simulator import DailyDataGuard
except ImportError:
    DailyDataGuard = None

ROOT = Path(__file__).parent

# ==============================================================================
# 頁面配置與全局 CSS 樣式 (注入網頁版 index.html 專屬設計)
# ==============================================================================
st.set_page_config(
    page_title="通用生產離散事件模擬器",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    /* 全局背景與卡片風格 */
    .stApp {
        background-color: #f8fafc;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    
    /* 隱藏 Streamlit 原生頂部邊距與選單飾條 */
    header[data-testid="stHeader"] {
        background: transparent;
    }
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        max-width: 1250px;
    }
    
    /* 頂部標題卡片 */
    .custom-header-card {
        background: #ffffff;
        padding: 22px 24px;
        border-radius: 12px;
        border: 1px solid #e2e8f0;
        margin-bottom: 18px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .custom-header-card h1 {
        font-size: 24px;
        font-weight: 700;
        color: #1e40af;
        margin: 0 0 6px 0;
        padding: 0;
    }
    .custom-header-card p {
        color: #64748b;
        font-size: 14px;
        margin: 0;
    }

    /* 守護模式橫幅卡片 */
    .custom-guard-banner {
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        padding: 14px 18px;
        border-radius: 10px;
        margin-bottom: 20px;
        display: flex;
        align-items: center;
        gap: 12px;
    }
    
    /* 主要卡片樣式容器 */
    div[data-testid="stVerticalBlock"] > div:has(div.card-marker) {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 20px 22px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }

    /* 按鈕美化 */
    div.stButton > button[kind="primary"] {
        background-color: #1e40af !important;
        border-color: #1e40af !important;
        color: #ffffff !important;
        font-size: 15px !important;
        font-weight: 600 !important;
        padding: 10px 20px !important;
        border-radius: 8px !important;
        box-shadow: 0 2px 4px rgba(30, 64, 175, 0.2) !important;
        transition: all 0.2s ease !important;
    }
    div.stButton > button[kind="primary"]:hover {
        background-color: #1d4ed8 !important;
        border-color: #1d4ed8 !important;
    }

    /* 指標卡片字體加強 */
    div[data-testid="stMetricValue"] {
        font-size: 26px !important;
        font-weight: 700 !important;
        color: #1e40af !important;
    }

    /* 表格字體微調 */
    .stDataFrame {
        border-radius: 8px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# 頂部標題區塊 (完全對齊網頁版 Header)
# ==============================================================================
st.markdown(
    """
    <div class="custom-header-card">
        <h1>通用生產離散事件模擬器</h1>
        <p>適用於任意 DAG 工站配置，支援當日動態採集日誌、極少筆數 (5 筆 / 0 筆) 自適應守護與蒙地卡羅完工排程模擬。</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# 守護模式開關 (置於淡藍色醒目橫幅內)
# ==============================================================================
guard_col_sw, guard_col_txt = st.columns([1, 12])
with guard_col_sw:
    guard_mode = st.toggle(
        "守護開關",
        value=True,
        label_visibility="collapsed",
        help="當上傳的當日資料筆數極少或部分工站尚無資料時，自動啟用「統計數據增強」與「SOP 標稱工時保底」，避免模擬中斷。",
    )
with guard_col_txt:
    st.markdown(
        """
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; padding: 10px 16px; border-radius: 8px; margin-top: -6px; margin-bottom: 14px;">
            <strong style="color: #1e3a8a; font-size: 14px;">🛡️ 啟用動態當日資料守護模式（自動處理 5 筆少樣本與 0 筆工站）</strong>
            <p style="font-size: 12px; color: #3b82f6; margin: 2px 0 0 0;">當上傳的當日資料筆數極少或部分工站尚無資料時，自動啟用「統計數據增強」與「SOP 標稱工時保底」，避免模擬中斷。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ==============================================================================
# 核心輸入雙欄佈局 (Left: 1. 資料來源設定 | Right: 2. 下一輪情境與初始 WIP 設定)
# ==============================================================================
col_left, col_right = st.columns(2, gap="medium")

# ------------------------------------------------------------------------------
# 左側卡片：1. 資料來源設定
# ------------------------------------------------------------------------------
with col_left:
    st.markdown("### 📂 1. 資料來源設定")

    csv_upload = st.file_uploader("生產加工時間 CSV：", type=["csv"], key="csv_up")
    flow_upload = st.file_uploader("製程網路 Flow JSON：", type=["json"], key="flow_up")

    # 若未上傳檔案，自動使用內建預設範例 (與網頁版 index.html 行為完全一致)
    if csv_upload is not None:
        history_df = pd.read_csv(csv_upload)
    else:
        history_df = pd.read_csv(ROOT / "examples" / "history_example.csv")

    if flow_upload is not None:
        flow_dict = json.load(flow_upload)
    else:
        flow_dict = json.loads((ROOT / "examples" / "flow_example.json").read_text(encoding="utf-8"))

    st.markdown("---")

    st.markdown("---")

    # 加工時間取樣模型與次數
    sim_model = st.selectbox(
        "加工時間取樣模型 (Model)：",
        options=["bootstrap", "mean", "p25", "p75"],
        format_func=lambda x: {
            "bootstrap": "Bootstrap (有放回隨機抽樣 - 蒙地卡羅)",
            "mean": "Mean (各站平均值基準)",
            "p25": "P25 (第25百分位數 - 較快情境)",
            "p75": "P75 (第75百分位數 - 較慢情境)",
        }[x],
        index=0,
    )

    c_run1, c_run2 = st.columns(2)
    with c_run1:
        sim_runs = st.number_input("模擬次數 (Runs)：", min_value=1, max_value=10000, value=1000, step=100)
    with c_run2:
        sim_seed = st.number_input("隨機種子 (Seed)：", min_value=0, max_value=2147483647, value=42)

# ------------------------------------------------------------------------------
# 驗證 Flow 與動態守護適應
# ------------------------------------------------------------------------------
try:
    graph, output_station = validate_flow(flow_dict)
    all_stations = list(graph.keys())

    quality_df = None
    if guard_mode and DailyDataGuard is not None:
        guard = DailyDataGuard(min_safe_samples=15, target_augmented_samples=25, default_cv=0.12)
        safe_history_df, quality_df = guard.process(history_df, all_stations)
        station_history = load_history(safe_history_df, all_stations)
    else:
        station_history = load_history(history_df, all_stations)

except ConfigError as exc:
    st.error(f"❌ 資料檢核錯誤：{exc}")
    st.stop()
except Exception as exc:
    st.error(f"❌ 資料讀取失敗：{exc}")
    st.stop()

# ------------------------------------------------------------------------------
# 右側卡片：2. 下一輪情境與初始 WIP 設定
# ------------------------------------------------------------------------------
with col_right:
    st.markdown("### 🎯 2. 下一輪情境與初始 WIP 設定")

    target_qty = st.number_input(
        "目標產品數量 (Target Quantity)：",
        min_value=1,
        max_value=1000,
        value=6,
        step=1,
    )
    products = [f"P{i:03d}" for i in range(1, int(target_qty) + 1)]
    st.caption("產品依序編號為 P001、P002...；相同編號的上游物料齊全才能合流配對。")

    st.markdown("**初始 Ready WIP（已備妥完整物料套組）：**")
    st.caption("勾選哪些產品在該工站已預先備妥物料，這些產品的上游製作會被略過，大幅縮短完工時間：")

    # 針對有上游依賴的工站，使用 st.pills 生成橫向標籤按鈕 (與網頁版 index.html 完全一致)
    upstream_stations = {s: inputs for s, inputs in graph.items() if inputs}
    ready_wip: dict[str, list[str]] = {}

    if upstream_stations:
        for s, inputs in upstream_stations.items():
            inputs_str = ", ".join(inputs)
            # 預設範例勾選，供使用者直接對比體驗
            default_pills = []
            if s == "ST03" and "P003" in products and "P004" in products:
                default_pills = ["P003", "P004"]
            elif s == "ST05" and "P001" in products and "P002" in products:
                default_pills = ["P001", "P002"]

            # 使用 Streamlit 現代化 pills 膠囊按鈕
            selected_items = st.pills(
                f"工站 {s}（需要 {inputs_str}）",
                options=products,
                default=default_pills,
                selection_mode="multi",
                key=f"pills_{s}",
            )
            ready_wip[s] = selected_items or []
    else:
        st.info("目前製程無上游合流依賴工站。")

    with st.expander("▶ 進階：初始邊庫存與 JSON (選填)", expanded=False):
        edge_text = st.text_area("initial_edge_inventory JSON", value="{}", height=65)

    # 執行模擬按鈕 (直接放置於右卡片底部，與 index.html 相同位置)
    run_simulation_clicked = st.button("🚀 執行離散事件模擬 (Run Simulation)", type="primary", use_container_width=True)

# ==============================================================================
# 當日各工站數據品質與處理策略診斷表 (放置於雙欄下方)
# ==============================================================================
if quality_df is not None:
    st.markdown(
        """
        <div style="background: #ffffff; padding: 18px 22px; border-radius: 10px; border: 1px solid #e2e8f0; margin-top: 16px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
            <h3 style="font-size: 16px; font-weight: 600; color: #0f172a; margin: 0 0 10px 0;">📊 當日各工站數據品質與處理策略診斷</h3>
            <p style="font-size: 12px; color: #64748b; margin: 0 0 10px 0;">三層自適應機制：N ≥ 15 (綠色高品質全額採用) | 1 ≤ N < 15 (黃色中品質均值增強) | N = 0 (紅色低品質 SOP 標稱保底)</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.dataframe(quality_df, use_container_width=True, hide_index=True)

# ==============================================================================
# 模擬執行運算與結果呈現
# ==============================================================================
if run_simulation_clicked:
    try:
        edge_inv = json.loads(edge_text) if edge_text.strip() else {}
        scenario = {
            "product_ids": products,
            "initial_ready_wip": ready_wip,
            "initial_edge_inventory": edge_inv,
        }
        prepare_scenario(graph, output_station, scenario)

        with st.spinner("正在進行離散事件蒙地卡羅運算..."):
            trials_df, stats_dict, example_result = simulate_many(
                graph=graph,
                output=output_station,
                history=station_history,
                scenario=scenario,
                model=sim_model,
                runs=int(sim_runs),
                seed=int(sim_seed),
            )

        st.markdown(
            f"""
            <div style="margin-top: 24px; margin-bottom: 16px;">
                <h2 style="font-size: 20px; font-weight: 700; color: #1e40af; margin: 0;">📈 模擬結果預測 ({sim_runs} 次蒙地卡羅模擬)</h2>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 4 大統計指標卡片
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("平均總完工時間 (Mean)", f"{stats_dict['mean_sec']:.2f} 秒", help="長期運行的數學期望值")
        m2.metric("中位數完工時間 (P50)", f"{stats_dict['p50_sec']:.2f} 秒", help="典型正常生產情境")
        m3.metric(
            "90百分位完工時間 (P90)",
            f"{stats_dict['p90_sec']:.2f} 秒",
            delta="生管承諾交期",
            delta_color="normal",
            help="★ 生管交期承諾推薦指標（代表 90% 機率可在此時間前完工）",
        )
        min_sec = stats_dict.get("min_sec", float(trials_df["makespan_sec"].min()))
        max_sec = stats_dict.get("max_sec", float(trials_df["makespan_sec"].max()))
        m4.metric("完工時間範圍 [Min ~ Max]", f"{min_sec:.1f} ~ {max_sec:.1f} 秒", help="極端情境範圍")

        # 單次示範排程甘特圖
        st.markdown("#### 📅 單次示範排程甘特圖 (Gantt Chart)")
        gantt_df = example_result.schedule.copy()
        fig_gantt = px.bar(
            gantt_df,
            x="duration_sec",
            y="station_id",
            color="product",
            orientation="h",
            base="start_sec",
            hover_data=["start_sec", "finish_sec", "duration_sec"],
            labels={"station_id": "工站", "duration_sec": "加工秒數", "product": "產品代碼"},
        )
        fig_gantt.update_layout(
            barmode="overlay",
            xaxis_title="模擬時間軸 (秒)",
            yaxis_title="工站代號",
            height=360,
            margin=dict(l=40, r=40, t=30, b=40),
            plot_bgcolor="#ffffff",
            paper_bgcolor="#ffffff",
        )
        st.plotly_chart(fig_gantt, use_container_width=True)

        # 完工時間分佈直方圖
        if sim_model == "bootstrap" and len(trials_df) > 1:
            st.markdown("#### 📊 總完工時間機率分佈 (Makespan Distribution)")
            fig_hist = px.histogram(
                trials_df,
                x="makespan_sec",
                nbins=35,
                labels={"makespan_sec": "總完工時間 (秒)"},
                color_discrete_sequence=["#3b82f6"],
            )
            fig_hist.add_vline(
                x=stats_dict["p50_sec"],
                line_width=2,
                line_dash="dash",
                line_color="#0284c7",
                annotation_text=f"P50: {stats_dict['p50_sec']:.1f}s",
                annotation_position="top left",
            )
            fig_hist.add_vline(
                x=stats_dict["p90_sec"],
                line_width=2,
                line_dash="dash",
                line_color="#d97706",
                annotation_text=f"P90: {stats_dict['p90_sec']:.1f}s",
                annotation_position="top right",
            )
            fig_hist.update_layout(
                xaxis_title="總完工時間 (秒)",
                yaxis_title="模擬次數 (頻率)",
                height=320,
                margin=dict(l=40, r=40, t=30, b=40),
                plot_bgcolor="#ffffff",
                paper_bgcolor="#ffffff",
            )
            st.plotly_chart(fig_hist, use_container_width=True)

        # 各站稼動率與單次排程明細表
        col_res1, col_res2 = st.columns(2)
        with col_res1:
            st.markdown("#### ⚙️ 各工站設備稼動率 (Utilization)")
            st.dataframe(example_result.station_summary, use_container_width=True, hide_index=True)

        with col_res2:
            st.markdown("#### 📋 單次排程工單明細 (前 15 筆)")
            st.dataframe(example_result.schedule.head(15), use_container_width=True, hide_index=True)

        # ZIP 下載按鈕
        with io.BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("simulation_runs.csv", trials_df.to_csv(index=False))
                z.writestr("example_schedule.csv", example_result.schedule.to_csv(index=False))
                z.writestr("station_summary.csv", example_result.station_summary.to_csv(index=False))
                z.writestr("scenario.json", json.dumps(scenario, ensure_ascii=False, indent=2))
                z.writestr("statistics.json", json.dumps(stats_dict, ensure_ascii=False, indent=2))
                if quality_df is not None:
                    z.writestr("data_quality_diagnostics.csv", quality_df.to_csv(index=False))
            zip_bytes = buffer.getvalue()

        st.download_button(
            label="💾 下載完整模擬結果 ZIP (含排程明細與統計)",
            data=zip_bytes,
            file_name="simulation_results.zip",
            mime="application/zip",
            use_container_width=True,
        )

    except (ConfigError, ValueError, TypeError, json.JSONDecodeError) as exc:
        st.error(f"❌ 模擬設定或運算錯誤：{exc}")

st.markdown("---")
st.caption(
    "📌 理論假設：每工站配置一台設備、加工不可中斷、無搬運時間、無設備故障、每個上游來源各需一件相同產品物料。預備 WIP 的事前製造時間不計入模擬時鐘。"
)
