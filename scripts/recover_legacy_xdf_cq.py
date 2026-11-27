"""CLI utility to inspect or export reconstructed continuous 14-channel CQ from historical `.xdf` files.

Examples:
    uv run python scripts/recover_legacy_xdf_cq.py recording.xdf
    uv run python scripts/recover_legacy_xdf_cq.py recording.xdf -o recovered_equality.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.legacy_xdf_cq import EEG_1020_CHANNELS, recover_legacy_xdf_file


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Recover continuous 14-channel Epoc X eQuality & battery from historical XDF recordings.")
    parser.add_argument("xdf_path", type=Path, help="Input LabRecorder .xdf file")
    parser.add_argument("-o", "--output-csv", type=Path, default=None, help="Optional CSV path to write recovered timestamps + qAF3..qAF4 + battery_percent")
    parser.add_argument("--stream-index", type=int, default=0, help="Which eQuality stream occurrence to export when multiple exist (default: 0)")
    args = parser.parse_args(argv)

    recovered = recover_legacy_xdf_file(args.xdf_path)
    if not recovered:
        print(f"No eQuality streams found in {args.xdf_path}", file=sys.stderr)
        return 1

    for i, item in enumerate(recovered):
        rec = item["recovery"]
        scores = rec.cq_scores
        batt = rec.battery_percent
        valid_batt = batt[np.isfinite(batt)]
        batt_summary = f"{np.min(valid_batt):.0f}%..{np.max(valid_batt):.0f}%" if valid_batt.size > 0 else "n/a"
        print(
            f"[{i}] stream#{item['stream_index']} '{item['stream_name']}' samples={len(scores)} "
            f"legacy_nibble={rec.was_legacy_nibble} battery={batt_summary}"
        )
        means = np.nanmean(scores, axis=0)
        ch_str = " ".join(f"{ch}={means[idx]:.2f}" for idx, ch in enumerate(EEG_1020_CHANNELS))
        print(f"    mean CQ score: {ch_str}")
    ## END for i, item in enumerate(recovered)...

    if args.output_csv is not None:
        sel = recovered[min(max(0, args.stream_index), len(recovered) - 1)]
        rec = sel["recovery"]
        ts = sel["time_stamps"]
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        header = ["timestamp"] + [f"q{ch}" for ch in EEG_1020_CHANNELS] + ["battery_percent", "inferred_counter"]
        with args.output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for row_idx in range(len(ts)):
                batt_val = "" if not np.isfinite(rec.battery_percent[row_idx]) else f"{rec.battery_percent[row_idx]:.0f}"
                writer.writerow(
                    [f"{ts[row_idx]:.9f}"]
                    + [f"{v:.6f}" for v in rec.cq_scores[row_idx]]
                    + [batt_val, int(rec.counters[row_idx])]
                )
            ## END for row_idx in range(len(ts))...

        print(f"Wrote {len(ts)} rows to {args.output_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
