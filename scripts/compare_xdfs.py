#!/usr/bin/env python3
"""Compare a LabRecorder XDF against a Python EPOCXWIRE reference decode (1× gold).

Typical old-exe workflow:
  1. pixi run python scripts/launch_hid_shim.py session.epocxwire --exe old.exe --speed 1
  2. Record with LabRecorder → lab.xdf
  3. pixi run python scripts/compare_xdfs.py lab.xdf --wire session.epocxwire

You can also pass two XDFs (e.g. LabRecorder vs Python ``--record`` XDF) without a wire file.

Reuses lag-scan / diagnose helpers from compare_py_cpp_replay.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.epocx_wire import (  # noqa: E402
    EEG_STREAM_NAME,
    QUALITY_STREAM_NAME,
)


def _load_compare_helpers():
    path = ROOT / "scripts" / "compare_py_cpp_replay.py"
    spec = importlib.util.spec_from_file_location("compare_py_cpp_replay", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cmp = _load_compare_helpers()
compare_arrays = _cmp.compare_arrays
decode_python_streams = _cmp.decode_python_streams
diagnose_streams = _cmp.diagnose_streams
lag_scan = _cmp.lag_scan


def _load_pyxdf(path: Path) -> Dict[str, np.ndarray]:
    import pyxdf

    streams, _hdr = pyxdf.load_xdf(str(path))
    out: Dict[str, np.ndarray] = {}
    for st in streams:
        name = (st.get("info", {}) or {}).get("name", [None])[0]
        if not name:
            continue
        data = np.asarray(st.get("time_series", []), dtype=np.float64)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        out[str(name)] = data
        print(f"  xdf {path.name}: {name!r} n={data.shape[0]} shape={data.shape}")
    return out


def _pick_stream(
    streams: Dict[str, np.ndarray],
    preferred: Sequence[str],
) -> Tuple[str, np.ndarray]:
    for name in preferred:
        if name in streams and streams[name].size:
            return name, streams[name]
    # Fuzzy: first stream whose name contains preferred token
    for pref in preferred:
        token = pref.lower()
        for name, arr in streams.items():
            if token in name.lower() and arr.size:
                return name, arr
    raise KeyError(f"none of {list(preferred)} found in {list(streams)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Compare LabRecorder XDF vs Python wire decode / another XDF"
    )
    p.add_argument("xdf", type=Path, help="LabRecorder (or other) XDF under test")
    p.add_argument(
        "--wire",
        type=Path,
        default=None,
        help="EPOCXWIRE used to build the Python reference (decrypt/decode at speed 0)",
    )
    p.add_argument(
        "--ref-xdf",
        type=Path,
        default=None,
        help="Alternate reference XDF (instead of --wire Python decode)",
    )
    p.add_argument("--atol", type=float, default=1e-2, help="Absolute µV tolerance")
    p.add_argument("--rtol", type=float, default=0.0, help="Relative tolerance")
    p.add_argument("--no-quality", action="store_true", help="Compare EEG only")
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Always print lag-scan diagnose (also auto on count mismatch)",
    )
    p.add_argument(
        "--max-lag",
        type=int,
        default=128,
        help="Max integer lag for lag-scan (default 128)",
    )
    args = p.parse_args(argv)

    xdf_path = args.xdf.expanduser().resolve()
    if not xdf_path.is_file():
        print(f"XDF not found: {xdf_path}", file=sys.stderr)
        return 2
    if args.wire is None and args.ref_xdf is None:
        print("Provide --wire SESSION.epocxwire and/or --ref-xdf other.xdf", file=sys.stderr)
        return 2

    print(f"Under test: {xdf_path}")
    test_streams = _load_pyxdf(xdf_path)

    if args.wire is not None:
        wire_path = args.wire.expanduser().resolve()
        if not wire_path.is_file():
            print(f"Wire not found: {wire_path}", file=sys.stderr)
            return 2
        print(f"Python reference from wire: {wire_path}")
        ref_streams = decode_python_streams(wire_path)
    else:
        ref_path = args.ref_xdf.expanduser().resolve()  # type: ignore[union-attr]
        if not ref_path.is_file():
            print(f"Ref XDF not found: {ref_path}", file=sys.stderr)
            return 2
        print(f"Reference XDF: {ref_path}")
        ref_streams = _load_pyxdf(ref_path)

    names: List[str] = [EEG_STREAM_NAME]
    if not args.no_quality:
        names.append(QUALITY_STREAM_NAME)

    def _role_prefs(logical: str) -> List[str]:
        if logical == QUALITY_STREAM_NAME:
            return [QUALITY_STREAM_NAME, "eQuality", "quality"]
        return [EEG_STREAM_NAME, "Epoc X", "Epoc", "EEG"]

    paired: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for logical in names:
        try:
            tname, tarr = _pick_stream(test_streams, _role_prefs(logical))
            rname, rarr = _pick_stream(ref_streams, _role_prefs(logical))
        except KeyError as e:
            if logical == QUALITY_STREAM_NAME:
                print(f"  skip quality: {e}")
                continue
            print(f"Missing stream for {logical}: {e}", file=sys.stderr)
            return 3
        paired[logical] = (rarr, tarr)
        print(
            f"  pair {logical}: ref={rname!r} ({rarr.shape[0]}) vs test={tname!r} ({tarr.shape[0]})"
        )

    ok_all = True
    reports = []
    for logical, (ref, test) in paired.items():
        rep = compare_arrays(ref, test, atol=args.atol, rtol=args.rtol, label=logical)
        reports.append(rep)
        status = "OK" if rep["ok"] else "DIFF"
        print(
            f"\n{status} {logical}: n_ref={rep['n_a']} n_test={rep['n_b']} "
            f"compared={rep['n_compared']} max_abs={rep['max_abs']} rmse={rep['rmse']}"
        )
        if rep["first_mismatch"]:
            print(f"  first_mismatch={rep['first_mismatch']}")
        if not rep["ok"]:
            ok_all = False
            rows = lag_scan(
                ref,
                test,
                atol=args.atol,
                rtol=args.rtol,
                max_lag=min(args.max_lag, max(ref.shape[0], test.shape[0]) // 4 or 1),
            )
            if rows:
                best = rows[0]
                print(
                    f"  best lag={best['lag']:+d} n={best['n']} "
                    f"max_abs={best['max_abs']:.6g} frac_close={best['frac_close']:.4f}"
                )

    need_diag = args.diagnose or any(
        (r["n_a"] != r["n_b"]) or (not r["ok"]) for r in reports
    )
    if need_diag:
        # diagnose_streams expects py=ref, cpp=test naming
        py_map = {k: paired[k][0] for k in paired}
        test_map = {k: paired[k][1] for k in paired}
        diagnose_streams(
            py_map,
            test_map,
            list(paired.keys()),
            atol=args.atol,
            rtol=args.rtol,
            effective_speed=1.0,
            span_s=0.0,
            min_duration=0.0,
        )

    print("\nPASS" if ok_all else "\nFAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
