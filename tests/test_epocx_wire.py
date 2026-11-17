"""Encode → decode roundtrips for epocx_wire helpers (no headset / XDF required)."""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    EmotivEpocX,
    aes_key_epocx_mode,
    serial_bytes_hid,
)
from emotiv_lsl.epocx_wire import (
    DEFAULT_SERIAL,
    WireFile,
    WireHeader,
    WirePacket,
    WireReplaySource,
    encode_eeg_wire,
    encode_motion_wire,
    read_wire_file,
    write_wire_file,
)


class TestEncodeDecodeRoundtrip(unittest.TestCase):
    def setUp(self):
        self.serial = DEFAULT_SERIAL
        self.key = aes_key_epocx_mode(serial_bytes_hid(self.serial))
        self.headset = EmotivEpocX.init_with_serial(
            serial_number=self.serial,
            cryptokey=bytearray(self.key),
            serial_source="hid",
            enable_electrode_quality_stream=True,
            enable_motion_data=True,
        )

    def test_eeg_uv_roundtrip(self):
        # Distinct µV so remap is obvious after quantize.
        uv = [100.0 + i * 10.0 for i in range(14)]
        cq = 0x2B
        wire = encode_eeg_wire(3, uv, self.key, cq=cq, adc_div=ADC_DIV_16BIT)
        self.assertEqual(len(wire), 32)
        eeg, quality = self.headset.decode_data(wire)
        self.assertIsNotNone(eeg)
        self.assertEqual(len(eeg), 14)
        for a, b in zip(eeg, uv):
            # Quantize: round(uV * 7.8) / 7.8
            expected = round(b * ADC_DIV_16BIT) / ADC_DIV_16BIT
            self.assertAlmostEqual(a, expected, places=9)
        self.assertEqual(quality[0], float(cq))

    def test_motion_roundtrip(self):
        # Q0 derived; use small quaternion parts so Q0 is real.
        motion = [0.0, 0.1, -0.2, 0.05, 0.5, -0.25, 0.125, 1.0, -2.0, 3.0]
        buf = encode_motion_wire(7, motion)
        self.assertEqual(len(buf), 20)
        out, cq = self.headset.decode_data(buf)
        self.assertIsNone(cq)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 10)
        # Skip Q0 (index 0) — reconstructed from vector part.
        for i in range(1, 10):
            self.assertAlmostEqual(out[i], motion[i], places=2)

    def test_wire_file_io_and_replay_source(self):
        uv = [50.0 + i for i in range(14)]
        payload = encode_eeg_wire(0, uv, self.key, cq=5)
        wire = WireFile(
            header=WireHeader(serial=self.serial, serial_source="hid"),
            packets=[
                WirePacket(rel_ns=0, payload=payload),
                WirePacket(rel_ns=1_000_000, payload=payload),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.epocxwire"
            write_wire_file(path, wire)
            loaded = read_wire_file(path)
            self.assertEqual(loaded.header.serial, self.serial)
            self.assertEqual(len(loaded.packets), 2)
            self.assertEqual(loaded.packets[0].payload, payload)

            src = WireReplaySource(loaded, speed=0.0)
            hs = EmotivEpocX(
                serial_number=self.serial,
                serial_source="hid",
                enable_electrode_quality_stream=True,
                enable_motion_data=False,
                replay_source=src,
                replay_speed=0.0,
            )
            src.apply_serial_to(hs)
            first = src.read(32)
            eeg, _ = hs.decode_data(first)
            self.assertEqual(len(eeg), 14)
            second = src.read(32)
            self.assertEqual(len(second), 32)
            self.assertEqual(src.read(32), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
