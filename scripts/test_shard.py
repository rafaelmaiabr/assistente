"""Run one slice of the Python test suite, so CI can run the slices in parallel (#153).

    python scripts/test_shard.py --shard 1 --of 3     # run slice 1 of 3 (verbose, like `unittest discover -s tests -v`)
    python scripts/test_shard.py --check --of 3       # prove that the slices together are the whole suite, each test once
    python scripts/test_shard.py --list --of 3        # show how many tests each slice has

The split is by test *class* (a class keeps its fixtures together) and balanced by number of tests; it is deterministic, so every
job computes the same plan. Nothing here changes what a test does.
"""
import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def class_key(test):
    """``module.Class`` of a test (a failed import is its own key, so it still runs and still fails)."""
    return ".".join(test.id().split(".")[:2])


def partition(counts, total):
    """``{key: shard}`` (shards are 1..total). Biggest classes first, each to the lightest shard; ties broken by name."""
    load = {number: 0 for number in range(1, total + 1)}
    plan = {}
    for key, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
        shard = min(load, key=lambda number: (load[number], number))
        plan[key] = shard
        load[shard] += count
    return plan


def discover():
    return list(flatten(unittest.TestLoader().discover(str(ROOT / "tests"))))


def plan_for(tests, total):
    counts = {}
    for test in tests:
        counts[class_key(test)] = counts.get(class_key(test), 0) + 1
    return partition(counts, total)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--of", type=int, required=True, help="how many slices")
    parser.add_argument("--shard", type=int, help="which slice to run (1..of)")
    parser.add_argument("--check", action="store_true", help="verify that the slices cover the whole suite exactly once")
    parser.add_argument("--list", action="store_true", help="print the number of tests of each slice")
    args = parser.parse_args(argv)
    if args.of < 1:
        parser.error("--of must be at least 1")
    tests = discover()
    plan = plan_for(tests, args.of)
    by_shard = {number: [t for t in tests if plan[class_key(t)] == number] for number in range(1, args.of + 1)}
    if args.check or args.list:
        for number, items in by_shard.items():
            print(f"slice {number}/{args.of}: {len(items)} tests, {len({class_key(t) for t in items})} classes")
        ids = [t.id() for items in by_shard.values() for t in items]
        ok = len(ids) == len(tests) and len(set(ids)) == len({t.id() for t in tests}) and set(ids) == {t.id() for t in tests}
        print(f"suite: {len(tests)} tests; slices together: {len(ids)}; every test in exactly one slice: {ok}")
        return 0 if ok or not args.check else 1
    if not args.shard or not 1 <= args.shard <= args.of:
        parser.error("--shard must be between 1 and --of")
    suite = unittest.TestSuite(by_shard[args.shard])
    print(f"slice {args.shard}/{args.of}: {suite.countTestCases()} of {len(tests)} tests", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
