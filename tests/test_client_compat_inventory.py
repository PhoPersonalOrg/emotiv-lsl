"""Layered inventory: known non-sample divergences + soft parity checks.

Hard sample parity lives in ``test_client_compat.py``. This module makes
manufacturer/version/wire-format gaps explicit without failing the sample layer.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.epocx_wire import (  # noqa: E402
    EEG_STREAM_NAME,
    EVENT_REPORT,
    MOTION_STREAM_NAME,
    QUALITY_STREAM_NAME,
    write_wire_file,
)
from emotiv_lsl.flutter_raw_csv import load_session_meta  # noqa: E402
from tests.compat_helpers import (  # noqa: E402
    FLUTTER_BLE_SERIAL,
    FLUTTER_HAS_EQUALITY_LSL,
    FLUTTER_LSL_EEG_NAME,
    FLUTTER_LSL_MOTION_NAME,
    FLUTTER_LSL_MOTION_TYPE,
    FLUTTER_LSL_QUALITY_NAME,
    build_synthetic_flutter_session,
    build_synthetic_wire,
    make_flutter_headset_for_info,
    make_headset_for_info,
    stream_info_snapshot,
    write_v1_for_cpp,
)

# Documented intentional / known divergences (not sample bugs).
PYTHON_MANUFACTURER = "emotiv_lsl"
CPP_MANUFACTURER = "emotiv_lsl_cpp"
PYTHON_APP_VERSION = "0.1.4"
CPP_APP_VERSION = "0.2.0"
PHOPYLSLHELPER_VERSION = "1.0.3"
# Flutter LSL (lib/sinks/lsl_sink.dart) — inventory only.
FLUTTER_MOTION_LSL_TYPE = FLUTTER_LSL_MOTION_TYPE  # vs Python/C++ SIGNAL
FLUTTER_LSL_SOURCE_ID_SCHEME = "ble_remote_id"  # vs Emotiv Epoc X_8_<keyhex>


class TestManufacturerVersionInventory(unittest.TestCase):
    def test_python_manufacturer_and_version(self):
        hs = make_headset_for_info()
        snap = stream_info_snapshot(hs.get_lsl_outlet_eeg_stream_info())
        self.assertEqual(snap["manufacturer"], PYTHON_MANUFACTURER)
        self.assertEqual(snap["version"], PYTHON_APP_VERSION)

    def test_documented_divergence_from_cpp_strings(self):
        """Inventory: Python and C++ advertise different manufacturer/version strings.

        C++ EmotivBase::add_lsl_outlet_info_common uses emotiv_lsl_cpp / 0.2.0.
        This is not a sample-compat failure; track until unified.
        """
        self.assertNotEqual(PYTHON_MANUFACTURER, CPP_MANUFACTURER)
        self.assertNotEqual(PYTHON_APP_VERSION, CPP_APP_VERSION)


class TestMotionDefaultInventory(unittest.TestCase):
    def test_motion_off_by_default_in_info_builder(self):
        """main.py / C++ main both default enable_motion_data=False."""
        hs = make_headset_for_info(enable_motion=False)
        self.assertFalse(hs.enable_motion_data)

    def test_motion_layout_when_enabled(self):
        hs = make_headset_for_info(enable_motion=True)
        snap = stream_info_snapshot(hs.get_lsl_outlet_motion_stream_info())
        self.assertEqual(snap["name"], MOTION_STREAM_NAME)
        self.assertEqual(snap["type"], "SIGNAL")
        self.assertEqual(snap["channel_count"], 10)
        self.assertEqual(
            snap["channel_labels"],
            [
                "Q0",
                "Q1",
                "Q2",
                "Q3",
                "AccX",
                "AccY",
                "AccZ",
                "MagX",
                "MagY",
                "MagZ",
            ],
        )


class TestWireFormatInventory(unittest.TestCase):
    def test_v2_write_v1_export_report_count(self):
        """Python writes EPOCXWIRE/2; C++ reads v1 — REPORT count must survive export."""
        wire = build_synthetic_wire(n_packets=12)
        n_reports = sum(1 for e in wire.events if e.kind == EVENT_REPORT)
        self.assertEqual(n_reports, 12)
        self.assertEqual(wire.header.format_version, 2)

        with tempfile.TemporaryDirectory(prefix="epocx_v1_") as td:
            v2_path = Path(td) / "v2.epocxwire"
            v1_path = Path(td) / "v1.epocxwire"
            write_wire_file(v2_path, wire)
            n_v1 = write_v1_for_cpp(wire, v1_path, rebase_time=True)
        self.assertEqual(n_v1, n_reports)


class TestPhopylslhelperInventory(unittest.TestCase):
    def test_phopylslhelper_keys_present(self):
        """Hard-assert key presence (values are time-dependent)."""
        hs = make_headset_for_info()
        snap = stream_info_snapshot(hs.get_lsl_outlet_eeg_stream_info())
        self.assertEqual(snap["phopylslhelper_version"], PHOPYLSLHELPER_VERSION)
        keys = set(snap["phopylslhelper_keys"])
        self.assertIn("version", keys)
        self.assertIn("stream_start_datetime", keys)
        self.assertIn("stream_start_lsl_local_offset_seconds", keys)


class TestFlutterClientInventory(unittest.TestCase):
    """Third headset client: flutter_application_final_emotiv_logger."""

    def test_eeg_stream_name_matches(self):
        self.assertEqual(FLUTTER_LSL_EEG_NAME, EEG_STREAM_NAME)
        self.assertEqual(FLUTTER_LSL_MOTION_NAME, MOTION_STREAM_NAME)

    def test_equality_lsl_outlet(self):
        """Python publishes per-electrode Mode-7 CQ demux grades {0,1,2,4}; Flutter shares stream name."""
        self.assertTrue(FLUTTER_HAS_EQUALITY_LSL)
        self.assertEqual(FLUTTER_LSL_QUALITY_NAME, QUALITY_STREAM_NAME)
        hs = make_flutter_headset_for_info(enable_quality=True)
        self.assertTrue(hs.enable_electrode_quality_stream)
        snap = stream_info_snapshot(
            hs.get_lsl_outlet_electrode_quality_stream_info()
        )
        self.assertEqual(snap["name"], FLUTTER_LSL_QUALITY_NAME)
        self.assertEqual(snap["type"], "Raw")
        self.assertEqual(snap["channel_count"], 14)

    def test_motion_lsl_type_divergence(self):
        """Flutter LSL motion type is Accelerometer; Python/C++ use SIGNAL."""
        py = make_headset_for_info(enable_motion=True)
        snap = stream_info_snapshot(py.get_lsl_outlet_motion_stream_info())
        self.assertEqual(snap["type"], "SIGNAL")
        self.assertNotEqual(FLUTTER_MOTION_LSL_TYPE, snap["type"])
        self.assertEqual(FLUTTER_MOTION_LSL_TYPE, "Accelerometer")

    def test_source_id_scheme_divergence(self):
        """Flutter live LSL uses BLE remoteId; offline/Python uses Emotiv Epoc X_8_<key>."""
        self.assertEqual(FLUTTER_LSL_SOURCE_ID_SCHEME, "ble_remote_id")
        hs = make_flutter_headset_for_info()
        sid = hs.get_lsl_source_id()
        self.assertTrue(sid.startswith("Emotiv Epoc X_8_"))
        # Document that live Flutter does not use this scheme.
        self.assertNotEqual(FLUTTER_LSL_SOURCE_ID_SCHEME, sid)

    def test_session_serial_source_ble_hex(self):
        with tempfile.TemporaryDirectory(prefix="flutter_inv_") as td:
            session = build_synthetic_flutter_session(Path(td) / "session", n_eeg=1)
            meta = load_session_meta(session)
            self.assertEqual(meta.serial_source, "ble_hex")
            self.assertEqual(meta.bt_key_hex, FLUTTER_BLE_SERIAL)

    def test_motion_on_by_default_in_flutter_offline_path(self):
        """Flutter streams motion live; offline make_headset enables motion."""
        hs = make_flutter_headset_for_info(enable_motion=True)
        self.assertTrue(hs.enable_motion_data)
        py_default = make_headset_for_info(enable_motion=False)
        self.assertFalse(py_default.enable_motion_data)


if __name__ == "__main__":
    unittest.main()
