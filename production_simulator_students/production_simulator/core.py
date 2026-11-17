"""Generic single-machine-per-station discrete-event manufacturing simulator.

Material tokens are keyed by (upstream, downstream, product). A ready kit at a
station replaces all incoming material requirements for that product only.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import heapq
import math
import re
from typing import Any
import numpy as np
import pandas as pd


class ConfigError(ValueError):
    pass


def validate_flow(flow: dict) -> tuple[dict[str, list[str]], str]:
    stations = flow.get("stations")
    if not isinstance(stations, dict) or not stations:
        raise ConfigError("flow.json 必須包含非空的 stations 物件")
    graph = {}
    for name, spec in stations.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("工站名稱不可為空")
        inputs = spec.get("inputs", []) if isinstance(spec, dict) else spec
        if not isinstance(inputs, list) or any(not isinstance(v, str) for v in inputs):
            raise ConfigError(f"{name}: inputs 必須是工站名稱清單")
        if len(set(inputs)) != len(inputs):
            raise ConfigError(f"{name}: 重複的上游工站")
        graph[name] = inputs
    for station, inputs in graph.items():
        for upstream in inputs:
            if upstream not in graph:
                raise ConfigError(f"{station}: 未定義上游工站 {upstream}")
    visiting, visited = set(), set()
    def visit(s):
        if s in visiting:
            raise ConfigError("Flow 有循環；第一版只支援 DAG")
        if s in visited:
            return
        visiting.add(s)
        for u in graph[s]:
            visit(u)
        visiting.remove(s)
        visited.add(s)
    for s in graph:
        visit(s)
    output = flow.get("output_station")
    if output not in graph:
        raise ConfigError("output_station 必須是 stations 中的一個工站")
    return graph, output


def load_history(df: pd.DataFrame, stations: list[str]) -> dict[str, np.ndarray]:
    required = {"station_id", "duration_sec"}
    if not required.issubset(df.columns):
        raise ConfigError("歷史 CSV 缺少 station_id 或 duration_sec")
    data = df.copy()
    data["duration_sec"] = pd.to_numeric(data["duration_sec"], errors="coerce")
    if "status" in data:
        data = data[data["status"].astype(str).str.lower().isin(["complete", "completed", "success", "finished", "done", "ok", "完成"])]
    data = data[np.isfinite(data["duration_sec"]) & (data["duration_sec"] > 0)]
    result = {}
    for s in stations:
        values = data.loc[data["station_id"].astype(str) == s, "duration_sec"].to_numpy(dtype=float)
        if len(values) == 0:
            raise ConfigError(f"工站 {s} 沒有有效歷史加工時間（需 > 0）")
        result[s] = values
    return result


def product_ids_for_scenario(scenario: dict) -> list[str]:
    products = scenario.get("product_ids")
    if products is None:
        qty = int(scenario.get("target_quantity", 0))
        if not 1 <= qty <= 10000:
            raise ConfigError("target_quantity 必須在 1～10000 之間")
        products = [f"P{i:03d}" for i in range(1, qty + 1)]
    if not isinstance(products, list) or not products or not all(isinstance(p, str) and p for p in products):
        raise ConfigError("product_ids 必須是非空字串清單")
    if len(products) != len(set(products)):
        raise ConfigError("product_ids 不可重複")
    return products


def prepare_scenario(graph: dict, output: str, scenario: dict):
    products = product_ids_for_scenario(scenario)
    product_set = set(products)
    ready_raw = scenario.get("initial_ready_wip", {})
    edge_raw = scenario.get("initial_edge_inventory", {})
    if not isinstance(ready_raw, dict) or not isinstance(edge_raw, dict):
        raise ConfigError("初始 WIP 與邊庫存必須是 JSON 物件")
    ready = {}
    for station, value in ready_raw.items():
        if station not in graph:
            raise ConfigError(f"未知工站: {station}")
        if not graph[station] and value:
            raise ConfigError(f"{station} 是源頭工站，不需要設定 ready WIP")
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0 or value > len(products):
                raise ConfigError(f"{station}: WIP 數量不合法")
            value = products[:value]
        if not isinstance(value, list) or any(v not in product_set for v in value) or len(set(value)) != len(value):
            raise ConfigError(f"{station}: WIP 必須是產品 ID 清單或數量")
        ready[station] = set(value)
    edge = defaultdict(set)
    for key, values in edge_raw.items():
        parts = key.split("->")
        if len(parts) != 2:
            raise ConfigError(f"邊庫存鍵格式必須是 ST01->ST03: {key}")
        u, s = (part.strip() for part in parts)
        if s not in graph or u not in graph[s]:
            raise ConfigError(f"Flow 不存在這條邊: {key}")
        if not isinstance(values, list) or any(v not in product_set for v in values) or len(set(values)) != len(values):
            raise ConfigError(f"{key}: 必須是產品 ID 清單且不可重複")
        edge[(u, s)].update(values)
    # A ready kit is a complete material set. Supplying incoming edge stock for
    # the same station/product would double-count the initial physical material.
    for s, pset in ready.items():
        for u in graph[s]:
            duplicate = pset & edge[(u, s)]
            if duplicate:
                raise ConfigError(f"{s}: ready WIP 與 {u}->{s} 的庫存重複: {sorted(duplicate)}")
    # A product may not be pre-kitted at both an upstream and downstream
    # station in the same ancestry chain; those would be different physical
    # copies of the same product, violating one-product-one-route semantics.
    def ancestors(s):
        out = set()
        for u in graph[s]:
            out.add(u)
            out.update(ancestors(u))
        return out
    for s in graph:
        for u in ancestors(s):
            conflict = ready.get(s, set()) & ready.get(u, set())
            if conflict:
                raise ConfigError(f"產品 {sorted(conflict)} 同時預備於上下游 {u}、{s}，會重複計算")
    return products, ready, edge


def required_tasks(graph, output, products, ready, edge):
    needed = set()
    def require(s, p):
        if (s, p) in needed:
            return
        needed.add((s, p))
        if p in ready.get(s, set()):
            return
        for u in graph[s]:
            if p not in edge[(u, s)]:
                require(u, p)
    for p in products:
        require(output, p)
    return needed


@dataclass
class SimulationResult:
    makespan: float
    schedule: pd.DataFrame
    station_summary: pd.DataFrame


def simulate_once(graph, output, history, scenario, model="bootstrap", seed=42):
    products, ready, initial_edge = prepare_scenario(graph, output, scenario)
    needed = required_tasks(graph, output, products, ready, initial_edge)
    rng = np.random.default_rng(seed)
    inventory = defaultdict(set, {k: set(v) for k, v in initial_edge.items()})
    downstream = defaultdict(list)
    for s, ups in graph.items():
        for u in ups:
            downstream[u].append(s)
    busy = {s: False for s in graph}
    running = set()
    finished = set()
    schedule = []
    events = []
    event_id = 0
    clock = 0.0
    order = {p: i for i, p in enumerate(products)}
    def sample(s):
        values = history[s]
        if model == "mean":
            return float(np.mean(values))
        if model == "bootstrap":
            return float(rng.choice(values))
        if model == "p25":
            return float(np.percentile(values, 25))
        if model == "p75":
            return float(np.percentile(values, 75))
        raise ConfigError(f"未知加工時間模型: {model}")
    def dispatch():
        nonlocal event_id
        # Deterministic dispatch: flow insertion order and product order.
        # Event completion at the same timestamp is processed before dispatch.
        changed = True
        while changed:
            changed = False
            for s in graph:
                if busy[s]:
                    continue
                for p in products:
                    if (s, p) not in needed or (s, p) in finished or (s, p) in running:
                        continue
                    if p not in ready.get(s, set()) and not all(p in inventory[(u, s)] for u in graph[s]):
                        continue
                    if p in ready.get(s, set()):
                        ready[s].remove(p)
                    else:
                        for u in graph[s]:
                            inventory[(u, s)].remove(p)
                    duration = sample(s)
                    busy[s] = True
                    running.add((s, p))
                    event_id += 1
                    heapq.heappush(events, (clock + duration, event_id, s, p, clock, duration))
                    changed = True
                    break
    dispatch()
    while events:
        clock = events[0][0]
        batch = []
        while events and abs(events[0][0] - clock) < 1e-10:
            batch.append(heapq.heappop(events))
        for finish, _, s, p, start, duration in batch:
            busy[s] = False
            running.remove((s, p))
            finished.add((s, p))
            schedule.append({"station_id": s, "product": p, "start_sec": start,
                             "finish_sec": finish, "duration_sec": duration})
            for d in downstream[s]:
                inventory[(s, d)].add(p)
        dispatch()
    missing = [p for p in products if (output, p) not in finished]
    if missing:
        raise ConfigError(f"模擬無法完成，缺少產品 {missing}。請檢查初始 WIP 與 Flow")
    frame = pd.DataFrame(schedule).sort_values(["start_sec", "station_id", "product"]).reset_index(drop=True)
    makespan = float(frame.loc[frame.station_id == output, "finish_sec"].max())
    summary = pd.DataFrame([{
        "station_id": s,
        "jobs": int((frame.station_id == s).sum()),
        "busy_sec": float(frame.loc[frame.station_id == s, "duration_sec"].sum()),
        "utilization": float(frame.loc[frame.station_id == s, "duration_sec"].sum() / makespan) if makespan else 0.0
    } for s in graph])
    return SimulationResult(makespan, frame, summary)


def simulate_many(graph, output, history, scenario, model="bootstrap", runs=1000, seed=42):
    if not 1 <= runs <= 10000:
        raise ConfigError("模擬次數必須在 1～10000 之間")
    master = np.random.default_rng(seed)
    results = []
    example = None
    for i in range(runs):
        run_seed = int(master.integers(0, 2**32 - 1))
        result = simulate_once(graph, output, history, scenario, model=model, seed=run_seed)
        if example is None:
            example = result
        results.append({"run": i + 1, "makespan_sec": result.makespan})
    data = pd.DataFrame(results)
    stats = {
        "mean_sec": float(data.makespan_sec.mean()),
        "p50_sec": float(data.makespan_sec.quantile(.5)),
        "p90_sec": float(data.makespan_sec.quantile(.9)),
        "min_sec": float(data.makespan_sec.min()),
        "max_sec": float(data.makespan_sec.max()),
    }
    return data, stats, example
