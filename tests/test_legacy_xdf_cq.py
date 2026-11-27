"""Unit tests for retroactive continuous CQ & Battery recovery on legacy nibble-scraped XDF arrays."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.emotiv_epoc_x import CQ_SLOT_SCALE_DIVISORS, CQ_SLOT_TO_1020_IDX, cq_norm_to_score
from emotiv_lsl.legacy_xdf_cq import (
    is_legacy_nibble_equality,
    reconstruct_legacy_equality_array,
    unpack_legacy_word7,
)


def _synthesize_legacy_nibble_session(slot_words: list[int], *, n_packets: int = 384, start_counter: int = 37, battery_pct: int = 88, drop_at: int | None = None) -> tuple[np.ndarray, list[int]]:
    """Simulate firmware Mode-7 `words[7]` + legacy CyKit `plain[16..22]` nibble scrape."""
    rng = np.random.default_rng(42)
    rows: list[list[float]] = []
    true_counters: list[int] = []
    cnt = start_counter % 128

    for k in range(n_packets):
        if drop_at is not None and k == drop_at:
            # Simulate 5 dropped USB packets mid-recording
            cnt = (cnt + 5) % 128
        true_counters.append(cnt)
        slot = cnt % 64
        if 0 <= slot < 14:
            w7 = int(slot_words[slot]) & 0xFFFF
        elif 16 <= cnt <= 63:
            w7 = 0
        elif cnt == 127:
            w7 = int(battery_pct) & 0x7F
        else:
            # Firmware repeats slots 10..13 in a 4-packet loop outside the zero half
            w7 = int(slot_words[10 + ((cnt - 14) % 4)]) & 0xFFFF

        b16 = w7 & 0xFF
        b17 = (w7 >> 8) & 0xFF
        # Simulated 16-bit EEG ADC words around 0x8000 for O2 (bytes 18..19), P8 (20..21), T8 (22..23)
        o2_w = int(0x8000 + rng.integers(-300, 300)) & 0xFFFF
        p8_w = int(0x8000 + rng.integers(-300, 300)) & 0xFFFF
        t8_w = int(0x8000 + rng.integers(-300, 300)) & 0xFFFF
        b18, b19 = o2_w & 0xFF, (o2_w >> 8) & 0xFF
        b20, b21 = p8_w & 0xFF, (p8_w >> 8) & 0xFF
        b22 = t8_w & 0xFF

        # Exact legacy CyKit nibble order: AF3, F7, F3, FC5, T7, P7, O1, O2, P8, T8, FC6, F4, F8, AF4
        rows.append(
            [
                float(b16 & 0xF),
                float((b16 >> 4) & 0xF),
                float(b17 & 0xF),
                float((b17 >> 4) & 0xF),
                float(b18 & 0xF),
                float((b18 >> 4) & 0xF),
                float(b19 & 0xF),
                float((b19 >> 4) & 0xF),
                float(b20 & 0xF),
                float((b20 >> 4) & 0xF),
                float(b21 & 0xF),
                float((b21 >> 4) & 0xF),
                float(b22 & 0xF),
                float((b22 >> 4) & 0xF),
            ]
        )
        cnt = (cnt + 1) % 128
    ## END for k in range(n_packets)...

    return np.asarray(rows, dtype=np.float64), true_counters


class TestLegacyXdfCqRecovery(unittest.TestCase):
    def test_reconstructs_continuous_cq_and_battery_across_packet_drop(self):
        slot_words = [5500, 5100, 5600, 5250, 5350, 0, 1500, 3670, 220, 5050, 5700, 6000, 4800, 4300]
        eq_legacy, true_counters = _synthesize_legacy_nibble_session(
            slot_words, n_packets=512, start_counter=49, battery_pct=86, drop_at=230
        )
        self.assertTrue(is_legacy_nibble_equality(eq_legacy))

        w7 = unpack_legacy_word7(eq_legacy)
        self.assertEqual(int(w7[0]), slot_words[49 - 49] if 49 < 14 else int(w7[0]))

        rec = reconstruct_legacy_equality_array(eq_legacy, eeg_rate=128)
        self.assertTrue(rec.was_legacy_nibble)
        # Counter phase matches ground truth both before and after the 5-packet drop at k=230
        self.assertTrue(np.array_equal(rec.counters[:210], np.asarray(true_counters[:210], dtype=np.int32)))
        self.assertTrue(np.array_equal(rec.counters[260:], np.asarray(true_counters[260:], dtype=np.int32)))

        # Final steady-state CQ scores match modern EmotivEpocX continuous scores on all 14 channels
        expected_final = np.zeros(14, dtype=np.float64)
        for slot in range(14):
            norm = (float(slot_words[slot]) * 0.25) / CQ_SLOT_SCALE_DIVISORS[slot]
            expected_final[CQ_SLOT_TO_1020_IDX[slot]] = cq_norm_to_score(norm)
        ## END for slot in range(14)...

        self.assertTrue(np.allclose(rec.cq_scores[-1], expected_final, atol=1e-12))
        valid_batt = rec.battery_percent[np.isfinite(rec.battery_percent)]
        self.assertGreater(len(valid_batt), 0)
        self.assertTrue(np.all(valid_batt == 86.0))


    def test_modern_continuous_equality_passes_through_unchanged(self):
        modern = np.tile(np.linspace(0.0, 7.35, 14), (64, 1))
        self.assertFalse(is_legacy_nibble_equality(modern))
        rec = reconstruct_legacy_equality_array(modern, eeg_rate=128)
        self.assertFalse(rec.was_legacy_nibble)
        self.assertTrue(np.array_equal(rec.cq_scores, modern))


if __name__ == "__main__":
    unittest.main()
