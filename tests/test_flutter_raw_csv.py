"""Offline Flutter raw.csv → decode / .epocxwire.

Run from emotiv-lsl root:
  python -m tests.test_flutter_raw_csv
"""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    EPOCX_RAW_EEG_ID_MODE7,
    aes_key_epocx_mode,
    serial_bytes_ble_hex,
)
from emotiv_lsl.epocx_wire import read_wire_file
from emotiv_lsl.flutter_raw_csv import (
    classify_payload,
    decode_session_folder,
    iter_raw_rows,
    load_session_meta,
    split_packages,
)


def _build_mode7_plain(counter: int, type_byte: int, words: list[int]) -> bytes:
    assert len(words) == 15
    return bytes([counter & 0xFF, type_byte & 0xFF]) + struct.pack("<15H", *words)


def _encrypt_wire(plain32: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_ECB)
    return bytes(b ^ 0x55 for b in cipher.encrypt(plain32))


class TestClassify(unittest.TestCase):
    def test_length_beats_kind_hint(self):
        self.assertEqual(classify_payload(bytes(32), "motion"), "eeg")
        self.assertEqual(classify_payload(bytes(20), "eeg"), "motion")
        self.assertEqual(classify_payload(bytes(64), ""), "eeg")
        self.assertEqual(classify_payload(bytes(40), ""), "motion")
        self.assertEqual(classify_payload(bytes(10), "eeg"), "eeg")

    def test_split_packages(self):
        self.assertEqual(len(split_packages(bytes(64), "eeg")), 2)
        self.assertEqual(len(split_packages(bytes(40), "motion")), 2)
        self.assertEqual(split_packages(bytes(10), "eeg"), [])


class TestFlutterRawRoundtrip(unittest.TestCase):
    def setUp(self):
        self.serial = "E50202E9"
        self.key = aes_key_epocx_mode(serial_bytes_ble_hex(self.serial))
        words = [100 + i * 17 for i in range(15)]
        words[7] = 0x12AB
        plain = _build_mode7_plain(5, 0x10, words)
        self.cipher = _encrypt_wire(plain, self.key)
        self.expected_eeg = [words[i] / ADC_DIV_16BIT for i in EPOCX_RAW_EEG_ID_MODE7]

        self.motion = bytearray(20)
        self.motion[0] = 1
        self.motion[1] = 0x20
        # AccX raw field index 4 → bytes 10-11 BE = 8191 → 1 g
        struct.pack_into(">h", self.motion, 10, 8191)

    def _write_session(self, tmp: Path, *, with_uuid: bool = True) -> Path:
        meta = {
            "deviceName": "EPOCX (E50202E9)",
            "btKeyHex": self.serial,
            "serialSource": "ble_hex",
            "eegResolutionBits": 16,
            "keyFingerprint": "test",
        }
        (tmp / "session.json").write_text(json.dumps(meta), encoding="utf-8")
        eeg_bytes = " ".join(str(b) for b in self.cipher)
        mot_bytes = " ".join(str(b) for b in self.motion)
        uuid = "81072F41-9F3D-11E3-A9DC-0002A5D5C51B"
        if with_uuid:
            lines = [
                "wall_ms,mono_us,kind,char_uuid,bytes",
                f"1000,100,{('eeg')},{uuid},{eeg_bytes}",
                f"2000,200,motion,81072F42-9F3D-11E3-A9DC-0002A5D5C51B,{mot_bytes}",
                # Mis-tagged motion-sized payload should still decode as motion.
                f"3000,300,eeg,,{mot_bytes}",
            ]
        else:
            lines = [
                "wall_ms,mono_us,kind,bytes",
                f"1000,100,eeg,{eeg_bytes}",
                f"2000,200,motion,{mot_bytes}",
            ]
        (tmp / "raw.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return tmp

    def test_decode_session_new_format(self):
        with tempfile.TemporaryDirectory() as td:
            session = self._write_session(Path(td), with_uuid=True)
            wire_path = session / "out.epocxwire"
            result = decode_session_folder(session, write_csvs=True, wire_path=wire_path)
            self.assertEqual(len(result.eeg), 1)
            # two motion packages (one correctly tagged, one mis-tagged)
            self.assertEqual(len(result.motion), 2)
            for a, b in zip(result.eeg[0].values, self.expected_eeg):
                self.assertAlmostEqual(a, b, places=9)
            self.assertAlmostEqual(result.motion[0].values[4], 1.0, places=9)
            self.assertTrue((session / "eeg_offline.csv").is_file())
            self.assertTrue((session / "motion_offline.csv").is_file())
            wire = read_wire_file(wire_path)
            self.assertEqual(wire.header.serial, self.serial)
            self.assertEqual(wire.header.serial_source, "ble_hex")
            self.assertEqual(len(wire.packets), 3)

    def test_legacy_header_without_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            session = self._write_session(Path(td), with_uuid=False)
            rows = list(iter_raw_rows(session / "raw.csv"))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].char_uuid, "")
            meta = load_session_meta(session)
            self.assertEqual(meta.bt_key_hex, self.serial)
            result = decode_session_folder(session, write_csvs=False)
            self.assertEqual(len(result.eeg), 1)
            self.assertEqual(len(result.motion), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
