"""CLI example: python run_cli.py --history ... --flow ... --scenario ..."""
import argparse
import json
from pathlib import Path
import pandas as pd
from production_simulator.core import validate_flow, load_history, simulate_many

parser = argparse.ArgumentParser()
parser.add_argument("--history", default="examples/history_example.csv")
parser.add_argument("--flow", default="examples/flow_example.json")
parser.add_argument("--scenario", default="examples/scenario_example.json")
parser.add_argument("--model", choices=["mean", "bootstrap", "p25", "p75"], default="bootstrap")
parser.add_argument("--runs", type=int, default=1000)
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--output", default="outputs")
args = parser.parse_args()
flow = json.loads(Path(args.flow).read_text(encoding="utf-8"))
scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
graph, output = validate_flow(flow)
history = load_history(pd.read_csv(args.history), list(graph))
trials, stats, example = simulate_many(graph, output, history, scenario, args.model, args.runs, args.seed)
folder = Path(args.output)
folder.mkdir(parents=True, exist_ok=True)
trials.to_csv(folder / "simulation_runs.csv", index=False)
example.schedule.to_csv(folder / "example_schedule.csv", index=False)
example.station_summary.to_csv(folder / "station_summary.csv", index=False)
(folder / "statistics.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(stats, ensure_ascii=False, indent=2))
print(f"已輸出至 {folder.resolve()}")
