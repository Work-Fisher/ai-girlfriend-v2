from pathlib import Path
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

from ui.affinity import Affinity, DAILY_TURN_CAP


class AffinityTests(unittest.TestCase):
    def test_many_turns_same_day_stop_at_cap_and_report_real_level_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            affinity = Affinity(Path(directory) / "affinity.json")
            state = {}
            for _ in range(62):
                state = affinity.record_turn()

            self.assertEqual(state["turns"], 62)
            self.assertEqual(state["days"], 1)
            self.assertEqual(state["points"], 55.0)
            self.assertEqual(state["earnedToday"], DAILY_TURN_CAP)
            self.assertTrue(state["dailyCapReached"])
            self.assertAlmostEqual(state["progress"], 55 / 60, places=4)
            self.assertEqual(state["nextName"], "熟络")


if __name__ == "__main__":
    unittest.main()
