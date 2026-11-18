import unittest
import numpy as np
from production_simulator.core import ConfigError, simulate_once, validate_flow

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.graph, self.output = validate_flow({"stations": {"A": [], "B": [], "C": ["A", "B"]}, "output_station": "C"})
        self.history = {"A": np.array([3.]), "B": np.array([7.]), "C": np.array([10.])}

    def test_no_wip(self):
        r = simulate_once(self.graph, self.output, self.history, {"product_ids": ["P1", "P2", "P3"]}, model="mean")
        self.assertAlmostEqual(r.makespan, 37.)

    def test_ready_kit(self):
        r = simulate_once(self.graph, self.output, self.history, {"product_ids": ["P1", "P2", "P3"], "initial_ready_wip": {"C": ["P1"]}}, model="mean")
        self.assertAlmostEqual(r.makespan, 30.)
        self.assertFalse(((r.schedule.station_id == "A") & (r.schedule.product == "P1")).any())

    def test_partial_edge_inventory(self):
        r = simulate_once(self.graph, self.output, self.history, {"product_ids": ["P1"], "initial_edge_inventory": {"A->C": ["P1"]}}, model="mean")
        self.assertAlmostEqual(r.makespan, 17.)
        self.assertFalse((r.schedule.station_id == "A").any())

    def test_double_count_rejected(self):
        with self.assertRaises(ConfigError):
            simulate_once(self.graph, self.output, self.history, {"product_ids": ["P1"], "initial_ready_wip": {"C": ["P1"]}, "initial_edge_inventory": {"A->C": ["P1"]}})

    def test_cycle_rejected(self):
        with self.assertRaises(ConfigError):
            validate_flow({"stations": {"A": ["B"], "B": ["A"]}, "output_station": "A"})

if __name__ == "__main__":
    unittest.main()
