from io import StringIO
import importlib.util
import unittest

HAS_RESEARCH = all(importlib.util.find_spec(name) is not None
                   for name in ("numpy", "scipy", "statsmodels"))
if HAS_RESEARCH:
    import numpy as np
    from scripts.rp06_calibrate import fit, parse_rows, split_sessions
    from scripts.rp06_d1 import one_sample


@unittest.skipUnless(HAS_RESEARCH, "requires the pinned research extra")
class ResearchAnalysisTests(unittest.TestCase):
    def test_rank_deficiency_rejected_before_choice_fit(self):
        x = np.array([[1., 0., 0.], [1., 1., 1.], [1., 0., 0.], [1., 1., 1.]])
        with self.assertRaisesRegex(ValueError, "not identifiable"):
            fit(x, np.array([1, 1, 0, 0]))

    def test_lagged_rows_and_disjoint_sessions(self):
        row = StringIO("round,node_id,decision,offered_make_coop,offered_make_defect,visibility\n"
                       "1,1_p0,1,3,1,0\n2,1_p0,0,0,4,0\n3,1_p0,NA,1,1,0\n4,1_p0,1,1,1,0\n")
        observations, groups, counts = parse_rows(row)
        self.assertEqual(observations, [(1, 0, 1., 1., .5)])
        self.assertEqual(groups, {1: "0"})
        self.assertEqual(counts["excluded"], {"round_1_no_previous": 1, "missing_current_choice": 1,
                                             "missing_previous_choice": 1})
        sessions = {n: str(n % 2) for n in range(1, 9)}
        split = split_sessions(sessions, {"split_namespace": "frozen", "split_groups_per_visibility": [2, 1, 1]})
        assigned = [session for subset in split.values() for session in subset]
        self.assertCountEqual(assigned, sessions)
        self.assertEqual(set(map(len, split.values())), {2, 4})

    def test_paired_root_t_test_checks_sign_and_degenerate_distribution(self):
        positive = one_sample([.25, .5, .25, .5, .75, .5], alternative="greater")
        self.assertLess(positive["p_value"], .01)
        self.assertGreater(positive["ci95"][0], 0)
        negative = one_sample([-.25, -.5, -.25, -.5, -.75, -.5], alternative="greater")
        self.assertGreater(negative["p_value"], .99)
        self.assertIsNone(one_sample([0, 0])["p_value"])
