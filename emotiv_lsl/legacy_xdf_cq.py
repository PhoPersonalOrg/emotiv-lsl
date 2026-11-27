"""Retroactive per-electrode Contact Quality (CQ) & Battery recovery for historical EPOC X XDF files.

Historical `emotiv-lsl` recordings prior to the Mode-7 CQ demux fix scraped 4-bit nibbles from
`plain[16..22]` (`AF3 = plain[16] & 0xF`, `F7 = (plain[16] >> 4) & 0xF`, `F3 = plain[17] & 0xF`,
`FC5 = (plain[17] >> 4) & 0xF`, and `T7..AF4` from EEG ADC words `O2/P8/T8`).

Because `plain[16..17]` IS the full little-endian 16-bit `words[7]` (`qAF3 | (qF7 << 4) | (qF3 << 8) | (qFC5 << 12)`),
and EPOC X firmware emits a deterministic 128/256-packet `words[7]` framing signature:
  - `counter % half_rate in 0..13`: 14 per-electrode 16-bit impedance measurements (`F3..F4`)
  - `counter in 16..(half_rate - 1)`: 48 consecutive `0x0000` words
  - `counter in (half_rate + 18)..(eeg_rate - 2)`: 4-packet repeat (`word7[k] == word7[k - 4]`) or zeros
  - `counter == eeg_rate - 1`: 7-bit `battery_percent` (`1..100`)

this module losslessly unpacks `words[7]`, tracks the exact packet counter phase across arbitrary
packet drops via chunked Viterbi decoding, and reconstructs the exact same continuous threshold-aligned
14-channel CQ scores (`AF3..AF4`) and battery percentages emitted by modern `emotiv-lsl`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    CQ_GRADE_THRESHOLDS,
    CQ_SLOT_SCALE_DIVISORS,
    CQ_SLOT_TO_1020_IDX,
    cq_norm_to_score,
)


EEG_1020_CHANNELS: Tuple[str, ...] = (
    "AF3", "F7", "F3", "FC5", "T7", "P7", "O1",
    "O2", "P8", "T8", "FC6", "F4", "F8", "AF4",
)


@dataclass
class LegacyCqRecoveryResult:
    """Reconstructed continuous CQ scores, raw Cortex ratios, battery %, and packet counters."""

    cq_scores: np.ndarray        # (N, 14) float64 continuous threshold-aligned CQ scores in AF3..AF4 order
    cq_norms: np.ndarray         # (N, 14) float64 raw Cortex ratios `(0.25 * word7) / scale[slot]`
    battery_percent: np.ndarray  # (N,) float64 latched battery % (0..100, NaN before first battery packet)
    counters: np.ndarray         # (N,) int32 inferred packet counters `0..(eeg_rate - 1)`
    word7: np.ndarray            # (N,) int32 unpacked 16-bit `words[7]`
    was_legacy_nibble: bool      # True if legacy nibble format was detected and reconstructed


def cq_norms_to_scores_vec(norms: np.ndarray) -> np.ndarray:
    """Vectorized counterpart to `cq_norm_to_score` (`0->0, 0.2->1, 0.7->2, 1.7->3, >1.7->3+(norm-1.7)`)."""
    arr = np.asarray(norms, dtype=np.float64)
    t0, t1, t2 = CQ_GRADE_THRESHOLDS
    out = np.zeros_like(arr, dtype=np.float64)
    m0 = (arr > 0.0) & (arr < t0)
    m1 = (arr >= t0) & (arr < t1)
    m2 = (arr >= t1) & (arr < t2)
    m3 = arr >= t2
    out[m0] = arr[m0] / t0
    out[m1] = 1.0 + (arr[m1] - t0) / (t1 - t0)
    out[m2] = 2.0 + (arr[m2] - t1) / (t2 - t1)
    out[m3] = 3.0 + (arr[m3] - t2)
    return out


def is_legacy_nibble_equality(eq_samples: np.ndarray, *, eeg_samples: Optional[np.ndarray] = None, adc_div: float = ADC_DIV_16BIT) -> bool:
    """Detect whether an `(N, 14)` `Epoc X eQuality` array came from the legacy CyKit nibble scrape."""
    arr = np.asarray(eq_samples, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] < 14:
        return False
    if not np.all(np.isfinite(arr)):
        return False
    rounded = np.rint(arr)
    if np.max(np.abs(arr - rounded)) > 1e-4:
        return False
    if np.min(rounded) < 0 or np.max(rounded) > 15:
        return False

    # If paired EEG is available, legacy qT7/qP7 equal the low byte nibbles of O2 (`eeg[:, 7] * adc_div`).
    if eeg_samples is not None:
        eeg_arr = np.asarray(eeg_samples, dtype=np.float64)
        if eeg_arr.ndim == 2 and eeg_arr.shape[0] == arr.shape[0] and eeg_arr.shape[1] >= 8:
            o2_adc = np.rint(eeg_arr[:, 7] * float(adc_div)).astype(np.int64)
            match_lo = np.mean((o2_adc & 0xF) == rounded[:, 4].astype(np.int64))
            match_hi = np.mean(((o2_adc >> 4) & 0xF) == rounded[:, 5].astype(np.int64))
            if match_lo > 0.90 and match_hi > 0.90:
                return True

    # Without EEG: modern discrete grades only ever take {0, 1, 2, 4}; legacy nibbles populate 5..15
    # and hold qO2 (`col 7`) / qF4 (`col 11`) near the 16-bit DC midpoint MSB nibble (`7..9`).
    has_above_4 = bool(np.any(rounded > 4))
    if not has_above_4:
        return False
    o2_mid = float(np.mean((rounded[:, 7] >= 7) & (rounded[:, 7] <= 9)))
    f4_mid = float(np.mean((rounded[:, 11] >= 7) & (rounded[:, 11] <= 9)))
    return o2_mid > 0.80 and f4_mid > 0.80


def unpack_legacy_word7(eq_samples: np.ndarray) -> np.ndarray:
    """Reconstruct 16-bit `words[7]` from legacy nibble columns `qAF3, qF7, qF3, qFC5`."""
    q = np.rint(np.asarray(eq_samples[:, :4], dtype=np.float64)).astype(np.int32) & 0xF
    return q[:, 0] | (q[:, 1] << 4) | (q[:, 2] << 8) | (q[:, 3] << 12)


def _score_counter_emissions(word7: np.ndarray, period: int) -> np.ndarray:
    """Build `(N, period)` log-likelihood table over `phi = (counter - k) % period`."""
    n = int(len(word7))
    half = period // 2
    w = np.asarray(word7, dtype=np.int32)
    is_zero = (w == 0).astype(np.float32)
    is_batt = ((w >= 1) & (w <= 100)).astype(np.float32)
    is_gt127 = (w > 127).astype(np.float32)
    eq_prev4 = np.zeros(n, dtype=np.float32)
    if n > 4:
        eq_prev4[4:] = (w[4:] == w[:-4]).astype(np.float32)
    # Exclude pure-zero from eq_prev4 bonus so the 48-zero half and 4-repeat half break symmetry cleanly.
    eq_prev4_nz = eq_prev4 * (1.0 - is_zero)

    # Role weights per counter position `c in 0..period-1`:
    w_zero = np.zeros(period, dtype=np.float32)
    w_nz_pen = np.zeros(period, dtype=np.float32)
    w_rep4 = np.zeros(period, dtype=np.float32)
    w_batt = np.zeros(period, dtype=np.float32)
    w_gt127 = np.zeros(period, dtype=np.float32)

    # Counters 16..(half-1): hardware emits 0x0000.
    w_zero[16:half] = 2.0
    w_nz_pen[16:half] = -3.0

    # Counters (half+18)..(period-2): hardware repeats slots 10..13 every 4 packets (or emits 0).
    w_rep4[(half + 18):(period - 1)] = 2.0
    w_zero[(half + 18):(period - 1)] = 0.5

    # Counter (period-1): 7-bit battery percentage (1..100, never > 127).
    w_batt[period - 1] = 14.0
    w_gt127[period - 1] = -12.0

    # Active CQ slots 0..13 and half..(half+13): 16-bit impedance (> 127 when sensor has contact).
    w_gt127[0:14] = 1.0
    w_gt127[half:(half + 14)] = 1.0

    # Evaluate `score_c[k, c]` via outer products `(N, 5) @ (5, period)` -> `(N, period)`,
    # then convert columns from counter `c` to phase `phi = (c - k) % period`.
    feats = np.column_stack([is_zero, 1.0 - is_zero, eq_prev4_nz, is_batt, is_gt127])
    weights = np.vstack([w_zero, w_nz_pen, w_rep4, w_batt, w_gt127])
    score_by_c = feats @ weights  # (N, period)

    k_idx = np.arange(n, dtype=np.int32)[:, None]
    phi_idx = np.arange(period, dtype=np.int32)[None, :]
    c_from_phi = (k_idx + phi_idx) % period
    return score_by_c[k_idx, c_from_phi]


def infer_epocx_counters_from_word7(word7: np.ndarray, *, eeg_rate: int = 128, chunk_size: int = 32, jump_penalty: float = 24.0) -> np.ndarray:
    """Infer packet counter `0..(eeg_rate - 1)` for every row of unpacked `word7` via chunked Viterbi."""
    w = np.asarray(word7, dtype=np.int32)
    n = int(len(w))
    period = 256 if int(eeg_rate) >= 256 else 128
    if n == 0:
        return np.zeros(0, dtype=np.int32)

    phi_scores = _score_counter_emissions(w, period)
    b = max(1, int(chunk_size))
    n_chunks = (n + b - 1) // b
    pad = n_chunks * b - n
    if pad > 0:
        phi_padded = np.vstack([phi_scores, np.zeros((pad, period), dtype=np.float32)])
    else:
        phi_padded = phi_scores
    chunk_emit = phi_padded.reshape(n_chunks, b, period).sum(axis=1)  # (n_chunks, period)

    backptr = np.zeros((n_chunks, period), dtype=np.int16)
    dp = chunk_emit[0].astype(np.float64)
    arange_p = np.arange(period, dtype=np.int16)

    for m in range(1, n_chunks):
        best_prev_phi = int(np.argmax(dp))
        jump_val = float(dp[best_prev_phi]) - float(jump_penalty)
        stay_better = dp >= jump_val
        backptr[m] = np.where(stay_better, arange_p, best_prev_phi)
        dp = np.where(stay_better, dp, jump_val) + chunk_emit[m]
    ## END for m in range(1, n_chunks)...

    chunk_phi = np.zeros(n_chunks, dtype=np.int32)
    chunk_phi[-1] = int(np.argmax(dp))
    for m in range(n_chunks - 1, 0, -1):
        chunk_phi[m - 1] = int(backptr[m, chunk_phi[m]])
    ## END for m in range(n_chunks - 1, 0, -1)...

    sample_phi = np.repeat(chunk_phi, b)[:n]
    # Refine exact jump sample inside any chunk boundary where `chunk_phi` changed.
    for m in range(1, n_chunks):
        p_prev = int(chunk_phi[m - 1])
        p_next = int(chunk_phi[m])
        if p_prev != p_next:
            s0 = max(0, (m - 1) * b)
            s1 = min(n, (m + 1) * b)
            diff = phi_scores[s0:s1, p_prev] - phi_scores[s0:s1, p_next]
            prefix = np.concatenate([[0.0], np.cumsum(diff, dtype=np.float64)])
            split = s0 + int(np.argmax(prefix))
            sample_phi[s0:split] = p_prev
            sample_phi[split:s1] = p_next
    ## END for m in range(1, n_chunks)...

    counters = (np.arange(n, dtype=np.int32) + sample_phi) % period
    return counters


def demux_word7_series(word7: np.ndarray, counters: np.ndarray, *, eeg_rate: int = 128) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Demultiplex `(word7, counters)` into `(cq_scores, cq_norms, battery_percent)`."""
    w = np.asarray(word7, dtype=np.int32) & 0xFFFF
    c = np.asarray(counters, dtype=np.int32)
    n = int(len(w))
    period = 256 if int(eeg_rate) >= 256 else 128
    half_rate = period // 2

    cq_norms = np.zeros((n, 14), dtype=np.float64)
    battery = np.full(n, np.nan, dtype=np.float64)
    state_norm = np.zeros(14, dtype=np.float64)
    cur_batt = np.nan

    slots = c % half_rate
    batt_slot = period - 1
    divisors = CQ_SLOT_SCALE_DIVISORS
    slot_to_1020 = CQ_SLOT_TO_1020_IDX

    for k in range(n):
        wk = int(w[k])
        ck = int(c[k])
        sk = int(slots[k])
        if 0 <= sk < 14:
            state_norm[slot_to_1020[sk]] = (float(wk) * 0.25) / divisors[sk]
        elif ck == batt_slot and 1 <= wk <= 100:
            cur_batt = float(wk)
        cq_norms[k] = state_norm
        battery[k] = cur_batt
    ## END for k in range(n)...

    cq_scores = cq_norms_to_scores_vec(cq_norms)
    return cq_scores, cq_norms, battery


def reconstruct_legacy_equality_array(eq_samples: np.ndarray, *, eeg_rate: int = 128, eeg_samples: Optional[np.ndarray] = None, force: bool = False) -> LegacyCqRecoveryResult:
    """Reconstruct continuous per-electrode CQ scores from a legacy nibble-scraped `(N, 14)` array.

    If `eq_samples` is already a modern recording (`was_legacy_nibble=False` and `force=False`),
    returns `cq_scores = eq_samples` unchanged so callers can safely normalize old & new sessions
    through a single function.
    """
    arr = np.asarray(eq_samples, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 14:
        raise ValueError(f"Expected (N, >=14) eQuality array, got shape={arr.shape}")
    arr14 = arr[:, :14]

    is_legacy = True if force else is_legacy_nibble_equality(arr14, eeg_samples=eeg_samples)
    if not is_legacy:
        n = arr14.shape[0]
        return LegacyCqRecoveryResult(
            cq_scores=arr14.copy(),
            cq_norms=np.full_like(arr14, np.nan),
            battery_percent=np.full(n, np.nan, dtype=np.float64),
            counters=np.full(n, -1, dtype=np.int32),
            word7=np.zeros(n, dtype=np.int32),
            was_legacy_nibble=False,
        )

    w7 = unpack_legacy_word7(arr14)
    counters = infer_epocx_counters_from_word7(w7, eeg_rate=eeg_rate)
    cq_scores, cq_norms, battery = demux_word7_series(w7, counters, eeg_rate=eeg_rate)
    return LegacyCqRecoveryResult(
        cq_scores=cq_scores,
        cq_norms=cq_norms,
        battery_percent=battery,
        counters=counters,
        word7=w7,
        was_legacy_nibble=True,
    )


def recover_legacy_xdf_file(xdf_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Load an `.xdf` file and reconstruct every `Epoc X eQuality` stream found."""
    import pyxdf

    streams, _ = pyxdf.load_xdf(str(xdf_path))
    eeg_by_source: Dict[str, np.ndarray] = {}
    first_eeg: Optional[np.ndarray] = None
    for s in streams:
        info = s.get("info") or {}
        name = str((info.get("name") or [""])[0])
        if name == "Epoc X":
            ts = np.asarray(s.get("time_series"))
            if ts.ndim == 2 and ts.shape[1] >= 14:
                sid = str((info.get("source_id") or [""])[0])
                eeg_by_source[sid] = ts
                if first_eeg is None:
                    first_eeg = ts
    ## END for s in streams...

    results: List[Dict[str, Any]] = []
    for idx, s in enumerate(streams):
        info = s.get("info") or {}
        name = str((info.get("name") or [""])[0])
        if "eQuality" not in name:
            continue
        ts = np.asarray(s.get("time_series"))
        if ts.ndim != 2 or ts.shape[1] < 14:
            continue
        sid = str((info.get("source_id") or [""])[0])
        srate_raw = float((info.get("nominal_srate") or [128])[0] or 128)
        eeg_rate = 256 if int(round(srate_raw)) >= 256 else 128
        paired_eeg = eeg_by_source.get(sid, first_eeg)
        if paired_eeg is not None and paired_eeg.shape[0] != ts.shape[0]:
            paired_eeg = None
        rec = reconstruct_legacy_equality_array(ts[:, :14], eeg_rate=eeg_rate, eeg_samples=paired_eeg)
        results.append(
            {
                "stream_index": idx,
                "stream_name": name,
                "source_id": sid,
                "time_stamps": np.asarray(s.get("time_stamps"), dtype=np.float64),
                "recovery": rec,
            }
        )
    ## END for idx, s in enumerate(streams)...

    return results
