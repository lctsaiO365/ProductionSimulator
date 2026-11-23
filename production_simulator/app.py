"""
通用生產離散事件模擬器 - Streamlit 地端互動版 (1:1 網頁版風格對齊版)
----------------------------------------------------------------------
特色功能：
1. 佈局與視覺風格 100% 完整復刻網頁版 (index.html) 的左右雙欄卡片佈局與色彩風格。
2. 完整支援 Supabase 雲端資料庫即時同步（event 工時、station_flow 製程 DAG、station_wip 在製品）。
3. 採用 st.pills 膠囊標籤實現水平 WIP 勾選，並支援一鍵套用雲端 WIP。
4. 預設啟用動態當日資料守護模式 (DailyDataGuard)，適應 2~5 筆少資料與 0 筆冷啟動工站。
5. 左右對稱操作區：左側設定資料來源與模型參數，右側設定 WIP 與執行模擬。
6. 蒙地卡羅完工排程儀表板、Plotly 甘特圖、直方圖、稼動率與 ZIP 下載。
7. 現場 QR Code 產生器、計時器、組裝線監控螢幕、生產儀表板四系統快速跳轉聯動。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import urllib.parse
import zipfile

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import streamlit.components.v1 as components

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

DEFAULT_SUPABASE_URL = "https://iomsrjkrvuzrsluirtdj.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_QmLToEOs4wEEPBnASz0dxg_o0hhtgLO"

DEFAULT_FLOW = {
    "stations": {
        "ST01": {"inputs": []},
        "ST02": {"inputs": []},
        "ST03": {"inputs": ["ST01", "ST02"]},
        "ST04": {"inputs": []},
        "ST05": {"inputs": ["ST03", "ST04"]},
    },
    "output_station": "ST05",
}

# ==============================================================================
# 輔助函式：Supabase 與 DAG 解析
# ==============================================================================
def is_test_product_id(pid: str) -> bool:
    return bool(re.match(r"^TEST\d*-", str(pid or "").strip(), re.I))

def get_test_round_from_product_id(pid: str) -> int | None:
    if not pid:
        return None
    m = re.match(r"^TEST(\d*)-", str(pid or "").strip(), re.I)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 1

def build_dag_from_flows(flow_rows: list[dict]) -> dict | None:
    if not flow_rows:
        return None
    all_stations = set()
    for f in flow_rows:
        s = (f.get("station_id") or "").strip()
        n = (f.get("next_station_id") or "").strip()
        if s and not s.startswith("SYSTEM") and not s.endswith("_LIMIT") and not s.endswith("_RAW"):
            all_stations.add(s)
        if n and not n.startswith("SYSTEM") and not n.endswith("_LIMIT") and not n.endswith("_RAW") and n != "END":
            all_stations.add(n)
    if not all_stations:
        return None
    
    stations = {s: {"inputs": []} for s in sorted(list(all_stations))}
    for f in flow_rows:
        s = (f.get("station_id") or "").strip()
        n = (f.get("next_station_id") or "").strip()
        if s and n and n != "END" and n in stations and s in stations:
            if s not in stations[n]["inputs"]:
                stations[n]["inputs"].append(s)
                
    has_outgoing = set()
    for f in flow_rows:
        s = (f.get("station_id") or "").strip()
        n = (f.get("next_station_id") or "").strip()
        if s and n and n != "END":
            has_outgoing.add(s)
            
    candidates = [s for s in stations if s not in has_outgoing]
    output_station = candidates[0] if candidates else sorted(list(stations.keys()))[-1]
    return {"stations": stations, "output_station": output_station}

def fetch_supabase_groups(url: str, key: str) -> list[str]:
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    clean_url = url.rstrip("/").rstrip("/rest/v1")
    groups = set()
    for table in ["event", "station_flow", "station_wip"]:
        try:
            resp = requests.get(f"{clean_url}/rest/v1/{table}?select=group_id", headers=headers, timeout=5)
            if resp.ok:
                for row in resp.json():
                    g = (row.get("group_id") or "").strip()
                    if g and not g.startswith("SYSTEM"):
                        groups.add(g)
        except Exception:
            pass
    for i in range(1, 13):
        groups.add(f"G{i:02d}")
    sorted_groups = sorted(list(groups), key=lambda x: (x == "ALL", len(x), x))
    if "ALL" not in sorted_groups:
        sorted_groups.append("ALL")
    return sorted_groups

def sync_supabase_data(
    url: str,
    key: str,
    group: str,
    filter_type: str,
    sync_events: bool,
    sync_flow: bool,
    sync_wip: bool,
):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    clean_url = url.rstrip("/").rstrip("/rest/v1")
    
    flow_dict = None
    flow_summary = None
    wips = []
    history_df = None
    events_count = 0
    events_avg = 0.0

    # 1. station_flow
    if sync_flow:
        try:
            f_url = f"{clean_url}/rest/v1/station_flow?order=station_id.asc"
            if group != "ALL":
                f_url += f"&group_id=eq.{urllib.parse.quote(group)}"
            r = requests.get(f_url, headers=headers, timeout=6)
            if r.ok:
                flows = r.json()
                if not flows and group.startswith("G") and "-" not in group:
                    r_fb = requests.get(
                        f"{clean_url}/rest/v1/station_flow?group_id=ilike.{urllib.parse.quote(group)}%&order=station_id.asc",
                        headers=headers,
                        timeout=6,
                    )
                    if r_fb.ok and r_fb.json():
                        flows = r_fb.json()
                if flows:
                    dag = build_dag_from_flows(flows)
                    if dag:
                        flow_dict = dag
                        st_keys = sorted(list(dag["stations"].keys()))
                        joined_stations = ", ".join(st_keys)
                        flow_summary = f"{joined_stations} (終點: {dag['output_station']})"
        except Exception:
            pass

    # 2. station_wip
    if sync_wip:
        try:
            w_url = f"{clean_url}/rest/v1/station_wip?select=*"
            if group != "ALL":
                w_url += f"&group_id=eq.{urllib.parse.quote(group)}"
            r = requests.get(w_url, headers=headers, timeout=6)
            if r.ok:
                raw_wips = r.json()
                if not raw_wips and group.startswith("G") and "-" not in group:
                    r_fb = requests.get(
                        f"{clean_url}/rest/v1/station_wip?group_id=ilike.{urllib.parse.quote(group)}%",
                        headers=headers,
                        timeout=6,
                    )
                    if r_fb.ok and r_fb.json():
                        raw_wips = r_fb.json()
                wips = [
                    w for w in raw_wips
                    if (w.get("station_id") or "")
                    and not (w.get("station_id") or "").startswith("SYSTEM")
                    and not (w.get("station_id") or "").endswith("_LIMIT")
                ]
        except Exception:
            pass

    # 3. event
    if sync_events:
        try:
            e_url = f"{clean_url}/rest/v1/event?select=*&order=start_time.asc"
            if group != "ALL":
                e_url += f"&group_id=eq.{urllib.parse.quote(group)}"
            r = requests.get(e_url, headers=headers, timeout=8)
            if not r.ok:
                raise Exception(f"讀取 event 失敗 (HTTP {r.status_code})")
            events = r.json()
            
            if filter_type == "OFFICIAL":
                events = [e for e in events if not is_test_product_id(e.get("product_id"))]
            elif filter_type == "TEST":
                events = [e for e in events if is_test_product_id(e.get("product_id"))]
            elif filter_type == "TEST1":
                events = [e for e in events if get_test_round_from_product_id(e.get("product_id")) == 1]
            elif filter_type == "TEST2":
                events = [e for e in events if get_test_round_from_product_id(e.get("product_id")) == 2]
            elif filter_type == "TEST3":
                events = [e for e in events if get_test_round_from_product_id(e.get("product_id")) == 3]

            valid = []
            for e in events:
                try:
                    dur = float(e.get("duration_sec") or 0)
                    if dur > 0 and e.get("station_id"):
                        valid.append({
                            "round_id": e.get("group_id") or "R01",
                            "station_id": e.get("station_id").strip(),
                            "product": e.get("product_id") or "P001",
                            "duration_sec": dur,
                            "status": e.get("status") or "completed",
                        })
                except (ValueError, TypeError):
                    continue

            events_count = len(valid)
            if valid:
                history_df = pd.DataFrame(valid)
                events_avg = float(history_df["duration_sec"].mean())
                
                # 若 flow_dict 缺少某些站點，自動擴展
                if flow_dict:
                    event_stations = set(history_df["station_id"].unique())
                    for es in event_stations:
                        if es not in flow_dict["stations"]:
                            flow_dict["stations"][es] = {"inputs": []}
            else:
                history_df = pd.DataFrame(columns=["round_id", "station_id", "product", "duration_sec", "status"])
        except Exception as e:
            raise e

    return flow_dict, flow_summary, wips, history_df, events_count, events_avg


# ==============================================================================
# 頁面配置與全局 CSS 樣式 (100% 注入 index.html 設計規範)
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

    /* 主要卡片樣式容器 */
    div[data-testid="stVerticalBlock"] > div:has(div.card-marker) {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 20px 22px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }

    /* 模擬 index.html 的 source-tabs segmented control */
    div[data-testid="stRadio"] > div {
        background: #f1f5f9;
        padding: 4px;
        border-radius: 8px;
        gap: 6px;
        border: 1px solid #e2e8f0;
        display: flex;
        margin-bottom: 14px;
    }
    div[data-testid="stRadio"] label {
        flex: 1;
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 13px !important;
        font-weight: 600 !important;
        cursor: pointer;
        display: flex;
        justify-content: center;
        align-items: center;
        transition: all 0.2s;
        text-align: center;
    }
    div[data-testid="stRadio"] label:has(input:checked) {
        background: #ffffff !important;
        color: #1e40af !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
        font-weight: 700 !important;
    }
    div[data-testid="stRadio"] input {
        display: none;
    }

    /* Supabase 狀態徽章 */
    .status-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 12px;
        font-weight: 600;
    }
    .status-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        display: inline-block;
    }
    .status-online { background: #dcfce7; color: #166534; }
    .status-online .status-dot { background: #16a34a; }
    .status-offline { background: #f1f5f9; color: #475569; }
    .status-offline .status-dot { background: #94a3b8; }
    .status-syncing { background: #e0f2fe; color: #0369a1; }
    .status-syncing .status-dot { background: #0284c7; }
    .status-error { background: #fee2e2; color: #991b1b; }
    .status-error .status-dot { background: #dc2626; }

    /* WIP 即時標籤膠囊 */
    .live-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        background: #ffffff;
        border: 1px solid #cbd5e1;
        padding: 2px 7px;
        border-radius: 6px;
        font-size: 11px;
        font-weight: 500;
        color: #1e3a8a;
        margin: 2px;
    }

    /* 相關聯動系統快速入口 */
    .quick-links {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 12px;
        padding-top: 12px;
        border-top: 1px dashed #cbd5e1;
    }
    .quick-link-btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 5px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        text-decoration: none;
        color: #1e40af;
        background: #ffffff;
        border: 1px solid #bfdbfe;
        transition: all 0.2s;
    }
    .quick-link-btn:hover {
        background: #eff6ff;
        border-color: #3b82f6;
        color: #1d4ed8;
        box-shadow: 0 1px 2px rgba(59,130,246,0.1);
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

    div[data-testid="stMetricValue"] {
        font-size: 26px !important;
        font-weight: 700 !important;
        color: #1e40af !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# 初始化 Session State
# ==============================================================================
if "data_source" not in st.session_state:
    st.session_state.data_source = "☁️ Supabase 雲端資料庫"
if "sb_url" not in st.session_state:
    st.session_state.sb_url = DEFAULT_SUPABASE_URL
if "sb_key" not in st.session_state:
    st.session_state.sb_key = DEFAULT_SUPABASE_KEY
if "sb_group" not in st.session_state:
    st.session_state.sb_group = "G01"
if "sb_filter" not in st.session_state:
    st.session_state.sb_filter = "ALL"
if "group_options" not in st.session_state:
    st.session_state.group_options = [
        "G01", "G01-T1", "G01-T2", "G02", "G02-T1", "G03", "G04", "G05",
        "G06", "G07", "G08", "G09", "G10", "G11", "G12", "G12-T3", "G25",
        "G25-T1", "G25-T2", "G25-T3", "ALL",
    ]
if "last_sync_time" not in st.session_state:
    st.session_state.last_sync_time = None
if "db_status" not in st.session_state:
    st.session_state.db_status = ("offline", "尚未同步")
if "synced_events_count" not in st.session_state:
    st.session_state.synced_events_count = 0
if "synced_events_avg" not in st.session_state:
    st.session_state.synced_events_avg = 0.0
if "synced_flow_summary" not in st.session_state:
    st.session_state.synced_flow_summary = "預設 5 工站 DAG"
if "synced_wips" not in st.session_state:
    st.session_state.synced_wips = []
if "current_history_df" not in st.session_state:
    st.session_state.current_history_df = None
if "current_flow_dict" not in st.session_state:
    st.session_state.current_flow_dict = None
if "auto_sync_interval" not in st.session_state:
    st.session_state.auto_sync_interval = 0
if "auto_wip_triggered" not in st.session_state:
    st.session_state.auto_wip_triggered = False
if "local_sample_type" not in st.session_state:
    st.session_state.local_sample_type = "default"

# 首次加載自動同步 G01 預設雲端資料
if st.session_state.current_history_df is None:
    try:
        f_dict, f_sum, wips, h_df, e_cnt, e_avg = sync_supabase_data(
            st.session_state.sb_url,
            st.session_state.sb_key,
            st.session_state.sb_group,
            st.session_state.sb_filter,
            True, True, True,
        )
        st.session_state.current_flow_dict = f_dict or DEFAULT_FLOW
        st.session_state.synced_flow_summary = f_sum or "預設 5 工站 DAG"
        st.session_state.synced_wips = wips
        st.session_state.current_history_df = h_df
        st.session_state.synced_events_count = e_cnt
        st.session_state.synced_events_avg = e_avg
        st.session_state.last_sync_time = pd.Timestamp.now().strftime("%H:%M:%S")
        st.session_state.db_status = ("online", "連線正常")
    except Exception:
        st.session_state.current_flow_dict = DEFAULT_FLOW
        st.session_state.current_history_df = pd.read_csv(ROOT / "examples" / "history_example.csv")
        st.session_state.db_status = ("error", "連線失敗")

# ==============================================================================
# 頂部標題區塊 (完全對齊網頁版 Header)
# ==============================================================================
st.markdown(
    """
    <div class="custom-header-card">
        <h1>通用生產離散事件模擬器</h1>
        <p>適用於任意 DAG 工站配置。支援當日動態採集日誌、極少筆數 (5 筆/0 筆) 自適應守護與蒙地卡羅完工排程模擬。</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# 守護模式開關 (置於淡藍色醒目橫幅內)
# ==============================================================================
guard_col_sw, guard_col_txt = st.columns([1, 14])
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

    # 資料來源切換頁籤：雲端 Supabase / 地端檔案與範例
    source_choice = st.radio(
        "資料來源模式",
        options=["☁️ Supabase 雲端資料庫", "📁 本地檔案 / 內建範例"],
        index=0 if st.session_state.data_source == "☁️ Supabase 雲端資料庫" else 1,
        horizontal=True,
        label_visibility="collapsed",
        key="source_mode_radio",
    )
    st.session_state.data_source = source_choice

    # ---------------- 區塊 A：Supabase 雲端同步面板 ----------------
    if source_choice == "☁️ Supabase 雲端資料庫":
        status_type, status_text = st.session_state.db_status
        last_sync = st.session_state.last_sync_time or "未連線"

        st.markdown(
            f"""
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <span class="status-badge status-{status_type}">
                    <span class="status-dot"></span> {status_text}
                </span>
                <span style="font-size: 11px; color: #64748b;">最後同步：{last_sync}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        c_grp, c_flt = st.columns(2)
        with c_grp:
            st.markdown("<label style='font-size: 13px; font-weight: 500;'>選擇組別 (Group)：</label>", unsafe_allow_html=True)
            c_gsel, c_gbtn = st.columns([3, 1])
            with c_gsel:
                cur_grp_idx = 0
                if st.session_state.sb_group in st.session_state.group_options:
                    cur_grp_idx = st.session_state.group_options.index(st.session_state.sb_group)
                sel_group = st.selectbox(
                    "組別",
                    options=st.session_state.group_options,
                    index=cur_grp_idx,
                    label_visibility="collapsed",
                    key="sb_group_select",
                )
                if sel_group != st.session_state.sb_group:
                    st.session_state.sb_group = sel_group
                    st.rerun()

            with c_gbtn:
                if st.button("🔍 偵測", help="從雲端自動偵測所有現存組別清單", use_container_width=True):
                    with st.spinner("偵測中..."):
                        discovered = fetch_supabase_groups(st.session_state.sb_url, st.session_state.sb_key)
                        st.session_state.group_options = discovered
                        st.toast(f"已成功獲取 {len(discovered)} 個雲端組別！", icon="✅")
                        st.rerun()

        with c_flt:
            st.markdown("<label style='font-size: 13px; font-weight: 500;'>數據過濾：</label>", unsafe_allow_html=True)
            filter_map = {
                "ALL": "全部工單數據",
                "OFFICIAL": "僅正式工單 (排除 TEST-)",
                "TEST": "僅測試工單 (TEST-)",
                "TEST1": "僅第 1 輪測試 (TEST1-)",
                "TEST2": "僅第 2 輪測試 (TEST2-)",
                "TEST3": "僅第 3 輪測試 (TEST3-)",
            }
            filter_keys = list(filter_map.keys())
            cur_flt_idx = filter_keys.index(st.session_state.sb_filter) if st.session_state.sb_filter in filter_keys else 0
            sel_filter = st.selectbox(
                "過濾",
                options=filter_keys,
                format_func=lambda k: filter_map[k],
                index=cur_flt_idx,
                label_visibility="collapsed",
                key="sb_filter_select",
            )
            if sel_filter != st.session_state.sb_filter:
                st.session_state.sb_filter = sel_filter
                st.rerun()

        # 同步內容核取方塊
        c_cb1, c_cb2, c_cb3 = st.columns(3)
        with c_cb1:
            sync_ev = st.checkbox("工時數據 (event)", value=True, key="cb_sync_ev")
        with c_cb2:
            sync_fl = st.checkbox("製程網路 (flow)", value=True, key="cb_sync_fl")
        with c_cb3:
            sync_wp = st.checkbox("在製品庫存 (wip)", value=True, key="cb_sync_wp")

        # 立即同步按鈕與自動同步
        c_sbtn, c_auto = st.columns([2, 1.2])
        with c_sbtn:
            sync_clicked = st.button("🔄 立即從 Supabase 同步數據", type="primary", use_container_width=True)
        with c_auto:
            auto_opts = {0: "自動同步: 關閉", 5000: "每 5 秒自動同步", 10000: "每 10 秒自動同步", 30000: "每 30 秒自動同步"}
            sel_auto = st.selectbox(
                "自動同步",
                options=list(auto_opts.keys()),
                format_func=lambda x: auto_opts[x],
                index=list(auto_opts.keys()).index(st.session_state.auto_sync_interval),
                label_visibility="collapsed",
            )
            st.session_state.auto_sync_interval = sel_auto

        if sync_clicked:
            with st.spinner("正在同步 Supabase 最新數據..."):
                try:
                    f_dict, f_sum, wips, h_df, e_cnt, e_avg = sync_supabase_data(
                        st.session_state.sb_url,
                        st.session_state.sb_key,
                        st.session_state.sb_group,
                        st.session_state.sb_filter,
                        sync_ev, sync_fl, sync_wp,
                    )
                    if f_dict:
                        st.session_state.current_flow_dict = f_dict
                        st.session_state.synced_flow_summary = f_sum
                    if sync_wp:
                        st.session_state.synced_wips = wips
                    if sync_ev and h_df is not None:
                        st.session_state.current_history_df = h_df
                        st.session_state.synced_events_count = e_cnt
                        st.session_state.synced_events_avg = e_avg
                    st.session_state.last_sync_time = pd.Timestamp.now().strftime("%H:%M:%S")
                    st.session_state.db_status = ("online", "連線正常")
                    st.toast(f"已成功同步【{st.session_state.sb_group}】數據 ({e_cnt} 筆工時)！", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.session_state.db_status = ("error", "同步異常")
                    st.error(f"❌ Supabase 同步失敗：{exc}")

        # 雲端同步資訊摘要小卡
        wip_chips_html = "".join([
            f'<span class="live-chip" title="待料: {w.get("input_wip", 0)}, 出庫: {w.get("output_wip", 0)}"><strong>{w.get("station_id")}</strong>: 待料{w.get("input_wip", 0)}/出庫{w.get("output_wip", 0)}</span>'
            for w in st.session_state.synced_wips
        ]) or '<span style="color:#94a3b8;">無在製品</span>'

        st.markdown(
            f"""
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; font-size: 12px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                    <span style="color: #64748b;">已同步工單筆數：</span>
                    <strong>{st.session_state.synced_events_count} 筆 (平均 {st.session_state.synced_events_avg:.2f}s)</strong>
                </div>
                <div style="display: flex; justify-content: space-between; margin-bottom: 4px;">
                    <span style="color: #64748b;">解析工站架構：</span>
                    <strong>{st.session_state.synced_flow_summary}</strong>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <span style="color: #64748b;">即時雲端 WIP：</span>
                    <span style="text-align: right; font-weight: 500; color: #1e40af;">{wip_chips_html}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 快捷操作：依據 WIP 自動帶入 & 匯出 CSV
        c_wbtn, c_dbtn = st.columns(2)
        with c_wbtn:
            if st.button("⚡ 依雲端 WIP 自動帶入 Ready WIP", use_container_width=True):
                st.session_state.auto_wip_triggered = True
                st.rerun()

        with c_dbtn:
            if st.session_state.current_history_df is not None and len(st.session_state.current_history_df) > 0:
                csv_bytes = st.session_state.current_history_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "📥 匯出當前數據為 CSV",
                    data=csv_bytes,
                    file_name=f"production_events_{st.session_state.sb_group}_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

        # 相關系統聯動快速跳轉入口 (自動攜帶組別參數)
        grp_param = urllib.parse.quote(st.session_state.sb_group)
        qr_url = f"https://lctsaio365.github.io/ie_chair_test/qrcode.html?group={grp_param}"
        timer_url = f"https://lctsaio365.github.io/ie_chair_test/timer.html?group={grp_param}&station=ST01"
        screen_url = f"https://lctsaio365.github.io/ie_chair_test/%E5%B7%A5%E6%A6%82%E8%9E%A2%E5%B9%95.html?group={grp_param}"
        dash_url = "https://lctsaio365.github.io/ie_chair_test/dashboard.html"

        st.markdown(
            f"""
            <div class="quick-links">
                <span style="font-size: 12px; font-weight: 600; color: #475569; width: 100%; margin-bottom: 4px;">
                    🔗 相關聯動系統快速入口 (自動攜帶組別參數)：
                </span>
                <a href="{qr_url}" target="_blank" class="quick-link-btn">🖨️ 工站 QR Code 產生器 ↗</a>
                <a href="{timer_url}" target="_blank" class="quick-link-btn">⏱️ 工站同步計時器 ↗</a>
                <a href="{screen_url}" target="_blank" class="quick-link-btn">🖥️ 組裝線即時監控中心 ↗</a>
                <a href="{dash_url}" target="_blank" class="quick-link-btn">📊 生產儀表板 ↗</a>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 設定 active 資料
        history_df = st.session_state.current_history_df
        flow_dict = st.session_state.current_flow_dict or DEFAULT_FLOW

    # ---------------- 區塊 B：本地檔案上傳 / 內建示範 ----------------
    else:
        csv_upload = st.file_uploader("生產加工時間 CSV：", type=["csv"], key="csv_up")
        c_samp1, c_samp2 = st.columns(2)
        with c_samp1:
            if st.button("示範：標準雙輪真實日誌", use_container_width=True):
                st.session_state.local_sample_type = "default"
                st.rerun()
        with c_samp2:
            if st.button("示範：極端5筆日誌(測守護)", use_container_width=True):
                st.session_state.local_sample_type = "sparse"
                st.rerun()

        flow_upload = st.file_uploader("製程網路 Flow JSON：", type=["json"], key="flow_up")

        if csv_upload is not None:
            history_df = pd.read_csv(csv_upload)
            st.success(f"✅ 已成功載入檔案：{csv_upload.name}")
        elif st.session_state.local_sample_type == "sparse":
            history_df = pd.DataFrame([
                {"station_id": "ST01", "duration_sec": 3.1, "status": "completed"},
                {"station_id": "ST01", "duration_sec": 3.4, "status": "completed"},
                {"station_id": "ST02", "duration_sec": 6.5, "status": "completed"},
                {"station_id": "ST02", "duration_sec": 7.2, "status": "completed"},
                {"station_id": "ST04", "duration_sec": 4.9, "status": "completed"},
            ])
            st.info("✅ 已載入「極端當日範例」(僅5筆，缺ST03/ST05)")
        else:
            history_df = pd.read_csv(ROOT / "examples" / "history_example.csv")
            st.info("✅ 已載入「標準雙輪真實日誌」(20筆完整工站)")

        if flow_upload is not None:
            flow_dict = json.load(flow_upload)
            st.success(f"✅ 已成功載入製程：{flow_upload.name}")
        else:
            flow_dict = json.loads((ROOT / "examples" / "flow_example.json").read_text(encoding="utf-8"))

    st.markdown("---")

    # 共用設定：取樣模型、模擬次數與隨機種子
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

    # 若觸發了「依雲端 WIP 自動帶入」，計算預設勾選
    auto_wip_defaults = {}
    if st.session_state.auto_wip_triggered:
        for w in st.session_state.synced_wips:
            st_id = (w.get("station_id") or "").strip()
            cnt = min(int(target_qty), max(w.get("input_wip", 0), w.get("output_wip", 0)))
            if cnt > 0 and st_id in upstream_stations:
                auto_wip_defaults[st_id] = [f"P{i:03d}" for i in range(1, cnt + 1)]
        st.session_state.auto_wip_triggered = False
        st.toast(f"已依據雲端 WIP 預先勾選各站半成品！", icon="⚡")

    if upstream_stations:
        for s, inputs in upstream_stations.items():
            inputs_str = ", ".join(inputs)
            if s in auto_wip_defaults:
                default_pills = auto_wip_defaults[s]
            elif s == "ST03" and "P003" in products and "P004" in products:
                default_pills = ["P003", "P004"]
            elif s == "ST05" and "P001" in products and "P002" in products:
                default_pills = ["P001", "P002"]
            else:
                default_pills = []

            selected_items = st.pills(
                f"工站 {s}（需要 {inputs_str}）",
                options=products,
                default=default_pills,
                selection_mode="multi",
                key=f"pills_{s}",
            )
            ready_wip[s] = selected_items or []
    else:
        st.info("目前製程無上游合流依賴工站，無需配置 Ready WIP。")

    with st.expander("▶ 進階：初始邊庫存與 JSON (選填)", expanded=False):
        edge_text = st.text_area("initial_edge_inventory JSON", value="{}", height=65)

    # 執行模擬按鈕
    run_simulation_clicked = st.button("🚀 執行離散事件模擬 (Run Simulation)", type="primary", use_container_width=True)

# ==============================================================================
# 當日各工站數據品質與處理策略診斷表 (放置於雙欄下方)
# ==============================================================================
if quality_df is not None:
    st.markdown(
        """
        <div style="background: #ffffff; padding: 18px 22px; border-radius: 10px; border: 1px solid #e2e8f0; margin-top: 16px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
            <h3 style="font-size: 16px; font-weight: 600; color: #0f172a; margin: 0 0 10px 0;">📊 當日各工站數據品質與處理策略診斷</h3>
            <p style="font-size: 12px; color: #64748b; margin: 0 0 10px 0;">三層自適應機制：N ≥ 15 (全當日真實數據 High) | 1 ≤ N < 15 (統計增強補齊 Med) | N = 0 (SOP 標稱保底 Low)</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.dataframe(quality_df, use_container_width=True, hide_index=True)

# ==============================================================================
# 模擬執行運算與結果呈現
# ==============================================================================
if run_simulation_clicked or (st.session_state.data_source == "☁️ Supabase 雲端資料庫" and st.session_state.synced_events_count > 0):
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

        # 單次示範排程甘特圖 (1:1 復刻 index.html 繪製邏輯與配色)
        st.markdown("#### 📅 單次示範排程甘特圖 (Gantt Chart)")
        gantt_colors = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899", "#06b6d4", "#64748b"]
        schedule_df = example_result.schedule.copy()

        fig_gantt = go.Figure()
        seen_products = set()
        for _, row in schedule_df.iterrows():
            p_code = row["product"]
            p_num = int(p_code[1:]) if p_code[1:].isdigit() else 0
            color = gantt_colors[p_num % len(gantt_colors)]
            show_leg = p_code not in seen_products
            seen_products.add(p_code)

            fig_gantt.add_trace(
                go.Bar(
                    name=p_code,
                    orientation="h",
                    y=[row["station_id"]],
                    x=[row["duration_sec"]],
                    base=[row["start_sec"]],
                    text=f"{p_code} ({row['duration_sec']:.1f}s)",
                    textposition="inside",
                    marker=dict(color=color),
                    hoverinfo="text",
                    hovertext=f"工站: {row['station_id']}<br>產品: {p_code}<br>開工: {row['start_sec']:.2f}s<br>完工: {row['finish_sec']:.2f}s<br>耗時: {row['duration_sec']:.2f}s",
                    showlegend=show_leg,
                )
            )

        fig_gantt.update_layout(
            barmode="stack",
            xaxis_title="模擬時間軸 (秒)",
            yaxis_title="工站代號",
            yaxis=dict(autorange="reversed"),
            height=380,
            margin=dict(l=60, r=30, t=30, b=40),
            plot_bgcolor="#ffffff",
            paper_bgcolor="#ffffff",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
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

# ==============================================================================
# 自動輪詢重新載入機制
# ==============================================================================
if st.session_state.auto_sync_interval > 0:
    components.html(
        f"""
        <script>
        setTimeout(function() {{
            window.parent.location.reload();
        }}, {st.session_state.auto_sync_interval});
        </script>
        """,
        height=0,
    )

st.markdown("---")
st.caption(
    "📌 理論假設：每工站配置一台設備、加工不可中斷、無搬運時間、無設備故障、每個上游來源各需一件相同產品物料。預備 WIP 的事前製造時間不計入模擬時鐘。"
)
