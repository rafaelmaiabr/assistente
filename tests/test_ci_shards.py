import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import test_shard  # noqa: E402


class PartitionTests(unittest.TestCase):
    COUNTS = {"a.A": 50, "a.B": 30, "b.C": 30, "b.D": 20, "c.E": 10, "c.F": 5, "d.G": 1}

    def test_every_class_goes_to_exactly_one_slice(self):
        plan = test_shard.partition(self.COUNTS, 3)
        self.assertEqual(set(plan), set(self.COUNTS))
        self.assertTrue(set(plan.values()) <= {1, 2, 3})

    def test_the_plan_is_the_same_every_time_whatever_the_order(self):
        reordered = dict(reversed(list(self.COUNTS.items())))
        self.assertEqual(test_shard.partition(self.COUNTS, 3), test_shard.partition(reordered, 3))

    def test_the_slices_are_balanced_by_number_of_tests(self):
        plan = test_shard.partition(self.COUNTS, 3)
        load = {n: sum(c for k, c in self.COUNTS.items() if plan[k] == n) for n in (1, 2, 3)}
        self.assertLessEqual(max(load.values()) - min(load.values()), 20)  # the biggest class (50) bounds the difference
        self.assertEqual(sum(load.values()), sum(self.COUNTS.values()))

    def test_one_slice_is_the_whole_suite_and_more_slices_than_classes_leaves_some_empty(self):
        self.assertEqual(set(test_shard.partition(self.COUNTS, 1).values()), {1})
        self.assertEqual(len(set(test_shard.partition({"a.A": 3}, 4).values())), 1)


class WholeSuiteTests(unittest.TestCase):
    def test_the_slices_of_the_real_suite_cover_every_test_exactly_once(self):
        tests = test_shard.discover()
        self.assertGreater(len(tests), 500)
        for total in (2, 3, 4):
            plan = test_shard.plan_for(tests, total)
            owners = {}
            for test in tests:
                owners.setdefault(test.id(), set()).add(plan[test_shard.class_key(test)])
            self.assertTrue(all(len(slices) == 1 for slices in owners.values()), total)
            self.assertEqual(len(owners), len(tests))


if __name__ == "__main__":
    unittest.main()
