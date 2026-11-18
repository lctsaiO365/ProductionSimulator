"""Streamlit UI. Start: streamlit run app.py"""
import io
import json
from pathlib import Path
import zipfile
import pandas as pd
import plotly.express as px
import streamlit as st
from production_simulator.core import (
    ConfigError, load_history, prepare_scenario, product_ids_for_scenario,
    simulate_many, validate_flow
)
try:
    from dynamic_daily_simulator import DailyDataGuard
except ImportError:
    DailyDataGuard = None

ROOT = Path(__file__).parent
st.set_page_config(page_title="通用生產 DES 模擬器", layout="wide")
st.title("通用生產離散事件模擬器")
st.caption("適用於不同組別、任意 DAG 工站配置。以歷史加工時間建立模型，計算 Flow、物料配對與初始 WIP 的影響。")
st.info("範例資料可直接執行。上傳 CSV 與 Flow JSON 後，程式會自動識別工站。所有時間以秒計。")

left, right = st.columns(2)
with left:
    history_upload = st.file_uploader("歷史生產 CSV", type="csv")
with right:
    flow_upload = st.file_uploader("Flow JSON", type="json")
guard_mode = False
if DailyDataGuard is not None:
    guard_mode = st.toggle(
        "🛡️ 啟用動態當日資料守護模式（自動處理 5 筆少樣本與 0 筆工站）",
        value=False,
        help="當上傳的當日資料筆數極少或部分工站尚無資料時，自動啟用統計增強與 SOP 保底，避免模擬中斷。"
    )

try:
    history = pd.read_csv(history_upload if history_upload is not None else ROOT / "examples" / "history_example.csv")
    flow = json.load(flow_upload) if flow_upload is not None else json.loads((ROOT / "examples" / "flow_example.json").read_text(encoding="utf-8"))
    graph, output = validate_flow(flow)

    if guard_mode and DailyDataGuard is not None:
        guard = DailyDataGuard(min_safe_samples=15)
        safe_history_df, quality_df = guard.process(history, list(graph))
        station_history = load_history(safe_history_df, list(graph))
        st.info("💡 已啟用動態資料守護模式：系統已自動處理少樣本與冷啟動工站。")
        with st.expander("📊 點擊查看當日各工站數據品質與處理策略診斷", expanded=True):
            st.dataframe(quality_df, use_container_width=True, hide_index=True)
    else:
        station_history = load_history(history, list(graph))
except (ConfigError, ValueError, KeyError, json.JSONDecodeError) as exc:
    st.error(f"資料讀取失敗：{exc}。提示：若這是當日少筆數資料，可開啟上方「啟用動態當日資料守護模式」以自動補齊。")
    st.stop()

st.subheader("1. 歷史資料摘要")
summary = pd.DataFrame([{
    "工站": s, "筆數": len(v), "平均秒數": round(float(v.mean()), 2),
    "最短秒數": round(float(v.min()), 2), "最長秒數": round(float(v.max()), 2)
} for s, v in station_history.items()])
st.dataframe(summary, use_container_width=True, hide_index=True)
st.caption(f"Flow：{len(graph)} 個工站；最終輸出站：{output}。歷史記錄中非完成狀態、無效或非正數加工時間會被排除。")

st.subheader("2. 下一輪情境")
qty = st.number_input("目標產品數量", min_value=1, max_value=1000, value=6, step=1)
products = [f"P{i:03d}" for i in range(1, qty + 1)]
st.caption("產品依序編號 P001、P002…；相同編號的上游物料才能配對。")
ready = {}
with st.expander("初始 Ready WIP（已備妥完整物料套組）", expanded=True):
    st.caption("勾選哪些產品在該工站已經備妥所有上游物料；這些產品的上游製作會被略過，除非其他下游仍有需求。")
    for s, inputs in graph.items():
        if inputs:
            selected = st.multiselect(f"{s}（需要 {', '.join(inputs)}）", options=products, key=f"ready_{s}")
            ready[s] = selected
with st.expander("進階：初始邊庫存（可選）"):
    st.caption('使用格式 {"ST01->ST03": ["P001"]}。同一產品不可同時設定為該站 Ready WIP。')
    edge_text = st.text_area("initial_edge_inventory JSON", value="{}", height=95)

c1, c2, c3 = st.columns(3)
with c1:
    model = st.selectbox("加工時間模型", ["bootstrap", "mean", "p25", "p75"], index=0)
with c2:
    runs = st.number_input("模擬次數", min_value=1, max_value=10000, value=1000, step=100)
with c3:
    seed = st.number_input("隨機種子", min_value=0, max_value=2147483647, value=42)

st.caption("Mean / P25 / P75 為固定時間基準；Bootstrap 為歷史值重抽樣。歷史資料少時，P90 僅是模型內的風險指標。")
if st.button("執行模擬", type="primary", use_container_width=True):
    try:
        edges = json.loads(edge_text)
        scenario = {"product_ids": products, "initial_ready_wip": ready, "initial_edge_inventory": edges}
        prepare_scenario(graph, output, scenario)
        trials, stats, example = simulate_many(graph, output, station_history, scenario, model=model, runs=int(runs), seed=int(seed))
    except (ConfigError, ValueError, TypeError, json.JSONDecodeError) as exc:
        st.error(f"模擬設定有誤：{exc}")
        st.stop()
    st.subheader("3. 模擬結果")
    a, b, c = st.columns(3)
    a.metric("平均總完工時間", f"{stats['mean_sec']:.2f} 秒")
    b.metric("P50 完工時間", f"{stats['p50_sec']:.2f} 秒")
    c.metric("P90 完工時間", f"{stats['p90_sec']:.2f} 秒")
    st.caption("下方甘特圖是第 1 次模擬的排程示例，並非平均排程。")
    gantt = example.schedule.copy()
    # Plotly timeline expects datetime values; instead use a horizontal bar chart
    # with a base offset to display numeric elapsed seconds correctly.
    fig = px.bar(gantt, x="duration_sec", y="station_id", color="product", orientation="h",
                 base="start_sec", hover_data=["start_sec", "finish_sec"], title="單次模擬排程（秒）")
    fig.update_layout(barmode="overlay", xaxis_title="模擬秒數", yaxis_title="工站")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(example.schedule, use_container_width=True, hide_index=True)
    st.subheader("各站稼動率（單次模擬）")
    st.dataframe(example.station_summary, use_container_width=True, hide_index=True)
    if model == "bootstrap":
        st.plotly_chart(px.histogram(trials, x="makespan_sec", nbins=30, title="總完工時間分布"), use_container_width=True)
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("simulation_runs.csv", trials.to_csv(index=False))
            z.writestr("example_schedule.csv", example.schedule.to_csv(index=False))
            z.writestr("station_summary.csv", example.station_summary.to_csv(index=False))
            z.writestr("scenario.json", json.dumps(scenario, ensure_ascii=False, indent=2))
            z.writestr("statistics.json", json.dumps(stats, ensure_ascii=False, indent=2))
        st.download_button("下載模擬結果 ZIP", buffer.getvalue(), "simulation_results.zip", mime="application/zip")

st.divider()
st.caption("假設：每站一台設備、不可中斷、無搬運時間、無設備故障、每個上游來源各需一件相同產品物料。預備 WIP 的事前製造時間不計入模擬時鐘。")
