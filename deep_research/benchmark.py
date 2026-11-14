"""Fixed, reproducible CPU workload for grouped market-event aggregation."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
from pathlib import Path
from time import perf_counter


SCENARIO = "行情事件分组聚合加速"
PARAMETERS = {"seed": 42, "rows": 160_000, "groups": 512, "stride": 1_000_000,
              "repetitions": 5, "memory_limit_bytes": 32 * 1024 * 1024}
CANDIDATES = {
    "direct": "直接按键分桶。输出数组随最大键增长。",
    "compressed": "先压缩键，再按连续编号分桶。",
    "sorted": "对键排序，再分段求和。",
    "baseline": "使用 pandas.groupby 基准。",
}


def run_benchmark(candidate: str, checkpoint=lambda: None) -> dict:
    import numpy as np
    import pandas as pd

    if candidate not in CANDIDATES:
        raise ValueError("Unknown benchmark candidate")
    p = PARAMETERS
    rng = np.random.default_rng(p["seed"])
    keys = rng.integers(p["groups"], size=p["rows"], dtype=np.int64)
    values = rng.integers(1, 100_000, size=p["rows"], dtype=np.int64)
    # Total volume is below 2**53, so weighted bincount sums exactly here.
    results = []
    for name, stride in (("密集键", 1), ("稀疏键", p["stride"])):
        checkpoint()
        symbols = keys * stride
        frame = pd.DataFrame({"symbol_id": symbols, "volume": values})

        def baseline():
            grouped = frame.groupby("symbol_id", sort=True)["volume"].agg(["sum", "size"])
            return np.column_stack((grouped.index.to_numpy(), grouped.to_numpy()))

        def evaluate():
            if candidate == "baseline":
                return baseline()
            if candidate == "direct":
                counts = np.bincount(symbols)
                totals = np.bincount(symbols, weights=values)
                observed = np.flatnonzero(counts)
                return np.column_stack((observed, totals[observed].astype(np.int64), counts[observed]))
            if candidate == "compressed":
                inverse, observed = pd.factorize(symbols, sort=True)
                counts = np.bincount(inverse)
                totals = np.bincount(inverse, weights=values).astype(np.int64)
                return np.column_stack((observed, totals, counts))
            order = np.argsort(symbols, kind="stable")
            ordered = symbols[order]
            starts = np.r_[0, np.flatnonzero(ordered[1:] != ordered[:-1]) + 1]
            return np.column_stack((ordered[starts], np.add.reduceat(values[order], starts),
                                    np.diff(np.r_[starts, len(ordered)])))

        row = {"dataset": name, "max_key": int(symbols.max())}
        predicted = (row["max_key"] + 1) * 16
        if candidate == "direct" and predicted > p["memory_limit_bytes"]:
            row.update(passed=False, reason=f"仅计数和求和数组预计需要 {predicted:,} 字节，超过预设上限。",
                       estimated_bytes=predicted)
            results.append(row)
            continue
        expected = baseline()
        actual = evaluate()
        if not np.array_equal(expected, actual):
            row.update(passed=False, reason="与基准逐组对拍不一致。")
            results.append(row)
            continue
        timings = {"baseline": [], "candidate": []}
        for repeat in range(p["repetitions"]):
            checkpoint()
            pair = [("baseline", baseline), ("candidate", evaluate)]
            for label, function in pair[::1 if repeat % 2 == 0 else -1]:
                start = perf_counter()
                function()
                timings[label].append((perf_counter() - start) * 1000)
        b, c = (statistics.median(timings[name]) for name in ("baseline", "candidate"))
        row.update(passed=True, baseline_ms=round(b, 3), candidate_ms=round(c, 3),
                   speedup=round(b / c, 3), timings_ms=timings)
        results.append(row)
    return {"candidate": candidate, "datasets": results, "parameters": p.copy(),
            "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12],
            "environment": {"Python": platform.python_version(), "NumPy": np.__version__,
                            "pandas": pd.__version__, "platform": platform.platform()},
            "reproduce": f"python -m deep_research.benchmark {candidate}"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=SCENARIO)
    parser.add_argument("candidate", choices=CANDIDATES)
    print(json.dumps(run_benchmark(parser.parse_args().candidate), ensure_ascii=False, indent=2))
