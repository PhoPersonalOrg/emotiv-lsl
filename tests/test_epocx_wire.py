"""Encode → decode roundtrips for epocx_wire helpers (no headset / XDF required)."""
from __future__ import annotations

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
    EVENT_CLOSE,
    EVENT_EMPTY,
    EVENT_ERROR,
    EVENT_OPEN,
    EVENT_REPORT,
    MAGIC_V1,
    MAGIC_V2,
    ReplayFeeder,
    WinUHidFeeder,
    WireEvent,
    WireFile,
    WireHeader,
    WirePacket,
    WireReplaySource,
    WireSessionRecorder,
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
        ## END for a, b in zip(eeg, uv)....

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
        ## END for i in range(1, 10)....

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
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(MAGIC_V2.encode("utf-8")))
            loaded = read_wire_file(path)
            self.assertEqual(loaded.header.serial, self.serial)
            self.assertEqual(loaded.header.format_version, 2)
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
            self.assertTrue(src.eof)


class TestWireV2Session(unittest.TestCase):
    def setUp(self):
        self.serial = DEFAULT_SERIAL
        self.key = aes_key_epocx_mode(serial_bytes_hid(self.serial))
        self.payload = encode_eeg_wire(
            0, [10.0 + i for i in range(14)], self.key, cq=1
        )

    def test_v2_mixed_events_roundtrip(self):
        events = [
            WireEvent(rel_ns=0, kind=EVENT_OPEN),
            WireEvent(rel_ns=100, kind=EVENT_REPORT, payload=self.payload),
            WireEvent(rel_ns=200, kind=EVENT_EMPTY),
            WireEvent(rel_ns=300, kind=EVENT_ERROR, message="boom"),
            WireEvent(rel_ns=400, kind=EVENT_REPORT, payload=self.payload[:20]),
            WireEvent(rel_ns=500, kind=EVENT_CLOSE),
        ]
        wire = WireFile(
            header=WireHeader(
                serial=self.serial,
                serial_source="hid",
                vid=0x1234,
                pid=0xED02,
                manufacturer="Emotiv",
                usage=2,
                host="test-host",
            ),
            events=events,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v2.epocxwire"
            write_wire_file(path, wire)
            loaded = read_wire_file(path)
            self.assertEqual(loaded.header.format_version, 2)
            self.assertEqual(loaded.header.vid, 0x1234)
            self.assertEqual(loaded.header.pid, 0xED02)
            self.assertEqual(loaded.header.manufacturer, "Emotiv")
            self.assertEqual(len(loaded.events), 6)
            self.assertEqual(loaded.events[0].kind, EVENT_OPEN)
            self.assertEqual(loaded.events[1].payload, self.payload)
            self.assertEqual(loaded.events[2].kind, EVENT_EMPTY)
            self.assertEqual(loaded.events[3].message, "boom")
            self.assertEqual(loaded.events[4].payload, self.payload[:20])
            self.assertEqual(loaded.events[5].kind, EVENT_CLOSE)
            # packets property is REPORT-only
            self.assertEqual(len(loaded.packets), 2)

    def test_v1_file_still_loads(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "v1.epocxwire"
            with path.open("wb") as f:
                f.write(f"{MAGIC_V1}\n".encode("utf-8"))
                f.write(f"serial={self.serial}\n".encode("utf-8"))
                f.write(b"serial_source=hid\n")
                f.write(b"adc_div=7.8\n")
                f.write(b"eeg_hz=128\n")
                f.write(b"\n")
                f.write(struct.pack("<QH", 0, len(self.payload)))
                f.write(self.payload)
                f.write(struct.pack("<QH", 1_000_000, len(self.payload)))
                f.write(self.payload)
            loaded = read_wire_file(path)
            self.assertEqual(loaded.header.format_version, 1)
            self.assertEqual(len(loaded.events), 2)
            self.assertEqual(loaded.events[0].kind, EVENT_REPORT)
            self.assertEqual(loaded.packets[0].payload, self.payload)

    def test_empty_replay_not_eof(self):
        events = [
            WireEvent(rel_ns=0, kind=EVENT_OPEN),
            WireEvent(rel_ns=10, kind=EVENT_REPORT, payload=self.payload),
            WireEvent(rel_ns=20, kind=EVENT_EMPTY),
            WireEvent(rel_ns=30, kind=EVENT_REPORT, payload=self.payload),
            WireEvent(rel_ns=40, kind=EVENT_CLOSE),
        ]
        wire = WireFile(
            header=WireHeader(serial=self.serial),
            events=events,
        )
        src = WireReplaySource(wire, speed=0.0)
        self.assertEqual(src.read(32), self.payload)
        self.assertFalse(src.eof)
        gap = src.read(32)
        self.assertEqual(gap, b"")
        self.assertFalse(src.eof)  # EMPTY must not finish replay
        self.assertEqual(src.read(32), self.payload)
        self.assertEqual(src.read(32), b"")
        self.assertTrue(src.eof)

    def test_session_recorder(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rec.epocxwire"
            hdr = WireHeader(
                serial=self.serial,
                vid=0x1234,
                pid=0xED02,
                manufacturer="Emotiv",
            )
            with WireSessionRecorder(path, hdr) as rec:
                rec.record_report(self.payload)
                rec.record_empty()
                rec.record_report(b"\x01\x02")
            loaded = read_wire_file(path)
            kinds = [e.kind for e in loaded.events]
            self.assertEqual(kinds[0], EVENT_OPEN)
            self.assertIn(EVENT_REPORT, kinds)
            self.assertIn(EVENT_EMPTY, kinds)
            self.assertEqual(kinds[-1], EVENT_CLOSE)
            reports = [e for e in loaded.events if e.kind == EVENT_REPORT]
            self.assertEqual(reports[0].payload, self.payload)
            self.assertEqual(reports[1].payload, b"\x01\x02")

    def test_replay_feeder_and_winuhid_stub(self):
        wire = WireFile(
            header=WireHeader(serial=self.serial),
            packets=[WirePacket(rel_ns=0, payload=self.payload)],
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.epocxwire"
            write_wire_file(path, wire)
            feeder = ReplayFeeder.from_path(path, speed=0.0)
            feeder.open()
            self.assertEqual(feeder.read(32), self.payload)
            self.assertEqual(feeder.read(32), b"")
            self.assertTrue(feeder.eof)
            feeder.close()

            stub = WinUHidFeeder(path)
            with self.assertRaises(NotImplementedError):
                stub.open()


if __name__ == "__main__":
    unittest.main(verbosity=2)
