import unittest

from civitas.experiments.labor_factorial import summarize_d1


class D1StatisticsTests(unittest.TestCase):
    def test_assigned_and_eligible_denominators_remain_distinct(self):
        rows = []
        for seed, tick, values in ((7, 21, (1, 1, 1, 2, 1)), (19, 19, (0, 0, 0, 0, 0))):
            for arm, choices in zip(("t0r0", "t0r1", "t1r0", "t1r1", "uniform"), values):
                rows.append({"seed": seed, "arm": arm, "status": "completed", "pretreatment_hash": f"h{seed}",
                             "opportunity_hash": f"o{seed}" if tick >= 20 else None,
                             "stopping": {"tick": tick},
                             "cooperation": {"offered": 2 if tick >= 20 else 0, "cooperative_choices": choices}})
        result = summarize_d1(rows, [7, 19], 20)
        self.assertEqual(result["assigned"]["mean"], .25)
        self.assertEqual(result["eligible"]["mean"], .5)
        self.assertEqual(result["eligible"]["roots"], [7])
        self.assertEqual(result["pre_offer_stops"], [19])
        self.assertEqual(result["eligible"]["all_at_risk_bounds"], [.5, .5])

    def test_failed_or_unmatched_arms_remain_in_bounds(self):
        rows = [{"seed": 7, "arm": "t1r1", "status": "failed", "stopping": {"tick": 21}}]
        result = summarize_d1(rows, [7], 20)
        self.assertEqual(result["assigned"]["all_assigned_bounds"], [-2., 2.])
        self.assertIsNone(result["eligible"]["mean"])
        self.assertEqual(result["eligible"]["all_at_risk_bounds"], [-2., 2.])
        self.assertEqual(result["incomplete_roots"], [7])
