"""Hard-fail EEG/CQ compatibility: offline decode gold + optional C++ LSL compare."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SRATE  # noqa: E402
from emotiv_lsl.emotiv_epoc_x import ADC_DIV_16BIT  # noqa: E402
from emotiv_lsl.epocx_wire import (  # noqa: E402
    EEG_STREAM_NAME,
    EVENT_REPORT,
    QUALITY_STREAM_NAME,
    read_wire_file,
    write_wire_file,
)
from tests.compat_helpers import (  # noqa: E402
    FIXTURE_AF3_UV,
    FIXTURE_SERIAL,
    MODE7_TWO_PACKET,
    SAMPLE_ATOL,
    build_synthetic_wire,
    capture_cpp_lsl,
    compare_sample_arrays_lsl,
    effective_replay_speed,
    expected_synth_cq_matrix,
    find_cpp_exe,
    make_headset_for_info,
    python_decode_streams,
    skip_unless_cpp_exe,
    space_reports_if_collapsed,
    stream_info_snapshot,
    write_v1_for_cpp,
)

EXPECTED_EEG_LABELS = [
    "AF3",
    "F7",
    "F3",
    "FC5",
    "T7",
    "P7",
    "O1",
    "O2",
    "P8",
    "T8",
    "FC6",
    "F4",
    "F8",
    "AF4",
]


class TestOfflineDecodeGold(unittest.TestCase):
    def test_mode7_two_packet_fixture_exists(self):
        self.assertTrue(
            MODE7_TWO_PACKET.is_file(),
            f"missing gold fixture: {MODE7_TWO_PACKET}",
        )

    def test_mode7_two_packet_matches_cpp_gold(self):
        """Lock Python decode to the same AF3 µV as C++ test_wire_fixture_replay + Cortex CQ demux."""
        wire = read_wire_file(MODE7_TWO_PACKET)
        self.assertEqual(wire.header.serial, FIXTURE_SERIAL)
        self.assertEqual(wire.header.format_version, 1)
        self.assertEqual(len(wire.packets), 2)

        streams = python_decode_streams(MODE7_TWO_PACKET, quiet=True)
        eeg = streams[EEG_STREAM_NAME]
        quality = streams[QUALITY_STREAM_NAME]
        self.assertEqual(eeg.shape, (2, 14))
        self.assertEqual(quality.shape, (2, 14))
        self.assertAlmostEqual(float(eeg[0, 0]), FIXTURE_AF3_UV, places=9)
        # Fixture packets have word7=0x2B (43) on counters 1..2 -> slots 1 (FC5, idx 3) and 2 (AF3, idx 0).
        from emotiv_lsl.emotiv_epoc_x import cq_norm_to_score

        exp_q0 = np.zeros(14, dtype=np.float64)
        exp_q0[3] = cq_norm_to_score((float(0x2B) * 0.25) / 216.0)
        exp_q1 = exp_q0.copy()
        exp_q1[0] = cq_norm_to_score((float(0x2B) * 0.25) / 226.0)
        self.assertTrue(np.allclose(quality[0], exp_q0, atol=1e-6))
        self.assertTrue(np.allclose(quality[1], exp_q1, atol=1e-6))

    def test_synthetic_encode_decode_roundtrip(self):
        wire = build_synthetic_wire(n_packets=16)
        with tempfile.TemporaryDirectory(prefix="epocx_synth_") as td:
            path = Path(td) / "mode7_synth.epocxwire"
            write_wire_file(path, wire)
            streams = python_decode_streams(path, quiet=True)

        eeg = streams[EEG_STREAM_NAME]
        quality = streams[QUALITY_STREAM_NAME]
        expected_cq = expected_synth_cq_matrix(n_packets=16)
        self.assertEqual(eeg.shape, (16, 14))
        self.assertEqual(quality.shape, (16, 14))
        for i in range(16):
            for ch in range(14):
                expected = round((100.0 + i * 1.0 + ch * 0.5) * ADC_DIV_16BIT) / ADC_DIV_16BIT
                self.assertAlmostEqual(float(eeg[i, ch]), expected, places=9)
            ## END for ch in range(14)....

            self.assertTrue(np.allclose(quality[i], expected_cq[i], atol=1e-5))
        ## END for i in range(16)....

        # Verify continuous non-categorical per-electrode scores across all 4 regimes and sticky hold on counters 14..16.
        last_row = quality[-1]
        self.assertGreater(len(np.unique(np.round(last_row, 4))), 8)
        self.assertTrue(np.any((last_row > 0.0) & (last_row < 1.0)))
        self.assertTrue(np.any((last_row >= 1.0) & (last_row < 2.0)))
        self.assertTrue(np.any((last_row >= 2.0) & (last_row < 3.0)))
        self.assertTrue(np.any(last_row >= 3.0))
        self.assertTrue(np.allclose(quality[13], quality[12], atol=1e-6))
        self.assertTrue(np.allclose(quality[15], quality[12], atol=1e-6))


class TestPythonStreamInfoSchema(unittest.TestCase):
    def test_eeg_and_quality_stream_info(self):
        hs = make_headset_for_info(enable_motion=False, enable_quality=True)
        eeg = stream_info_snapshot(hs.get_lsl_outlet_eeg_stream_info())
        cq = stream_info_snapshot(hs.get_lsl_outlet_electrode_quality_stream_info())

        self.assertEqual(eeg["name"], EEG_STREAM_NAME)
        self.assertEqual(eeg["type"], "EEG")
        self.assertEqual(eeg["channel_count"], 14)
        self.assertEqual(eeg["nominal_srate"], float(SRATE))
        self.assertEqual(eeg["channel_labels"], EXPECTED_EEG_LABELS)

        self.assertEqual(cq["name"], QUALITY_STREAM_NAME)
        self.assertEqual(cq["type"], "Raw")
        self.assertEqual(cq["channel_count"], 14)
        self.assertEqual(cq["nominal_srate"], float(SRATE))
        self.assertEqual(cq["channel_labels"], [f"q{n}" for n in EXPECTED_EEG_LABELS])

        # Shared source_id across outlets (Python / EmotivEpocX C++ parity).
        self.assertEqual(eeg["source_id"], cq["source_id"])
        self.assertTrue(eeg["source_id"].startswith("Emotiv Epoc X_8_"))


@skip_unless_cpp_exe()
class TestOptionalCppLslCompare(unittest.TestCase):
    def _compare_wire(self, wire_path: Path) -> None:
        from emotiv_lsl.epocx_wire import EEG_STREAM_NAME

        cpp_exe = find_cpp_exe()
        assert cpp_exe is not None
        wire = read_wire_file(wire_path)
        # C++ emotiv-lsl-cpp still emits legacy word7 & 0x7f broadcast CQ; compare EEG only here.
        stream_names = [EEG_STREAM_NAME]
        py_streams = python_decode_streams(wire_path, quiet=True)

        with tempfile.TemporaryDirectory(prefix="epocx_cmp_") as td:
            v1_path = Path(td) / "session_v1.epocxwire"
            # Space collapsed timestamps so C++ does not burst-push into LSL drops.
            n_v1 = write_v1_for_cpp(
                wire, v1_path, rebase_time=True, space_collapsed=True
            )
            self.assertGreater(n_v1, 0)
            paced = space_reports_if_collapsed(wire)
            # Stretch short wires so LSL consumers keep up (still may drop 1–2 samples).
            speed, _span = effective_replay_speed(paced, speed=0.0, min_duration=6.0)
            child_env = os.environ.copy()
            child_env.pop("SSL_CERT_DIR", None)
            cpp_streams = capture_cpp_lsl(
                cpp_exe=cpp_exe,
                wire_v1=v1_path,
                stream_names=stream_names,
                speed=speed,
                env=child_env,
                kill_stale_windows=False,
                quiet=True,
            )

        for name in stream_names:
            rep = compare_sample_arrays_lsl(
                py_streams[name],
                cpp_streams[name],
                atol=SAMPLE_ATOL,
                rtol=0.0,
                label=name,
            )
            self.assertTrue(
                rep["ok"],
                f"{name} mismatch: {rep}",
            )
        ## END for name in stream_names....

    def test_cpp_lsl_mode7_two_packet(self):
        self._compare_wire(MODE7_TWO_PACKET)

    def test_cpp_lsl_synthetic(self):
        wire = build_synthetic_wire(n_packets=16)
        with tempfile.TemporaryDirectory(prefix="epocx_synth_") as td:
            path = Path(td) / "mode7_synth.epocxwire"
            write_wire_file(path, wire)
            self._compare_wire(path)


if __name__ == "__main__":
    unittest.main()
