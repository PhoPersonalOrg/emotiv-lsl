"""Flutter logger as third headset client: session → offline → wire → Python/C++.

Hard-fail EEG sample parity. CQ LSL is out of scope (Flutter has no eQuality outlet).
"""
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

from emotiv_lsl.epocx_wire import EEG_STREAM_NAME, read_wire_file  # noqa: E402
from emotiv_lsl.flutter_raw_csv import (  # noqa: E402
    decode_session_folder,
    load_session_meta,
    make_headset,
)
from tests.compat_helpers import (  # noqa: E402
    FLUTTER_BLE_SERIAL,
    SAMPLE_ATOL,
    build_synthetic_flutter_session,
    capture_cpp_lsl,
    compare_sample_arrays,
    compare_sample_arrays_lsl,
    effective_replay_speed,
    expected_synth_eeg_uv,
    find_cpp_exe,
    flutter_session_to_wire,
    make_flutter_headset_for_info,
    python_decode_streams,
    skip_unless_cpp_exe,
    space_reports_if_collapsed,
    write_v1_for_cpp,
)


class TestFlutterOfflineDecode(unittest.TestCase):
    def test_synthetic_session_eeg_matches_mode7(self):
        with tempfile.TemporaryDirectory(prefix="flutter_sess_") as td:
            session = build_synthetic_flutter_session(
                Path(td) / "session", n_eeg=8, n_motion=0
            )
            result = decode_session_folder(session, write_csvs=False)
            self.assertEqual(len(result.eeg), 8)
            self.assertEqual(result.failed, 0)
            for i, sample in enumerate(result.eeg):
                self.assertEqual(len(sample.values), 14)
                for ch in range(14):
                    self.assertAlmostEqual(
                        sample.values[ch],
                        expected_synth_eeg_uv(i, ch),
                        places=9,
                    )
                ## END for ch in range(14)....
            ## END for i, sample in enumerate(result.eeg)....

    def test_wire_export_matches_offline_eeg(self):
        with tempfile.TemporaryDirectory(prefix="flutter_wire_") as td:
            session = build_synthetic_flutter_session(
                Path(td) / "session", n_eeg=8, n_motion=0
            )
            wire_path = Path(td) / "from_flutter.epocxwire"
            result = flutter_session_to_wire(session, wire_path)
            self.assertTrue(wire_path.is_file())

            wire = read_wire_file(wire_path)
            self.assertEqual(wire.header.serial, FLUTTER_BLE_SERIAL)
            self.assertEqual(wire.header.serial_source, "ble_hex")

            py_streams = python_decode_streams(wire_path, quiet=True)
            eeg = py_streams[EEG_STREAM_NAME]
            self.assertEqual(eeg.shape[0], len(result.eeg))
            offline = np.asarray([s.values for s in result.eeg], dtype=np.float64)
            rep = compare_sample_arrays(
                offline, eeg, atol=1e-9, rtol=0.0, label="flutter_offline_vs_wire"
            )
            self.assertTrue(rep["ok"], rep)

    def test_ble_hex_serial_source_not_hid(self):
        with tempfile.TemporaryDirectory(prefix="flutter_meta_") as td:
            session = build_synthetic_flutter_session(Path(td) / "session", n_eeg=1)
            meta = load_session_meta(session)
            self.assertEqual(meta.serial_source, "ble_hex")
            self.assertEqual(meta.bt_key_hex, FLUTTER_BLE_SERIAL)
            hs = make_headset(meta)
            self.assertEqual(hs.serial_source, "ble_hex")
            helper_hs = make_flutter_headset_for_info(serial=FLUTTER_BLE_SERIAL)
            self.assertEqual(bytes(hs.get_crypto_key()), bytes(helper_hs.get_crypto_key()))
            from emotiv_lsl.emotiv_epoc_x import (
                aes_key_epocx_mode,
                serial_bytes_ble_hex,
                serial_bytes_hid,
            )

            ble_key = aes_key_epocx_mode(serial_bytes_ble_hex(FLUTTER_BLE_SERIAL))
            hid_key = aes_key_epocx_mode(serial_bytes_hid(FLUTTER_BLE_SERIAL))
            # ble_hex key material differs from HID last-4 ASCII for the same string.
            self.assertNotEqual(ble_key, hid_key)
            self.assertEqual(bytes(hs.get_crypto_key()), ble_key)
            sid = hs.get_lsl_source_id()
            self.assertTrue(sid.startswith("Emotiv Epoc X_8_"))
            self.assertIn(ble_key.hex(), sid)


@skip_unless_cpp_exe()
class TestFlutterOptionalCppLsl(unittest.TestCase):
    def test_flutter_wire_cpp_eeg_lsl(self):
        with tempfile.TemporaryDirectory(prefix="flutter_cpp_") as td:
            session = build_synthetic_flutter_session(
                Path(td) / "session", n_eeg=16, n_motion=0
            )
            wire_path = Path(td) / "from_flutter.epocxwire"
            flutter_session_to_wire(session, wire_path)

            py_streams = python_decode_streams(wire_path, quiet=True)
            wire = read_wire_file(wire_path)
            cpp_exe = find_cpp_exe()
            assert cpp_exe is not None

            v1_path = Path(td) / "session_v1.epocxwire"
            write_v1_for_cpp(wire, v1_path, rebase_time=True, space_collapsed=True)
            paced = space_reports_if_collapsed(wire)
            speed, _span = effective_replay_speed(paced, speed=0.0, min_duration=6.0)
            child_env = os.environ.copy()
            child_env.pop("SSL_CERT_DIR", None)
            cpp_streams = capture_cpp_lsl(
                cpp_exe=cpp_exe,
                wire_v1=v1_path,
                stream_names=[EEG_STREAM_NAME],
                speed=speed,
                env=child_env,
                kill_stale_windows=False,
                quiet=True,
            )

            rep = compare_sample_arrays_lsl(
                py_streams[EEG_STREAM_NAME],
                cpp_streams[EEG_STREAM_NAME],
                atol=SAMPLE_ATOL,
                rtol=0.0,
                label=EEG_STREAM_NAME,
            )
            self.assertTrue(rep["ok"], rep)


if __name__ == "__main__":
    unittest.main()
