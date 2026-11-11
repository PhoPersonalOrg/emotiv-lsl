"""Offline assertions for Epoc X Mode-7 key + decode (no headset required).

Run from repo root:
  python -m tests.test_epoc_x_decode
  # or: pixi run python -m tests.test_epoc_x_decode
"""
from __future__ import annotations

import math
import struct
import sys
import unittest
from pathlib import Path

# Allow `python -m tests.test_epoc_x_decode` from project root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Crypto.Cipher import AES

from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    EPOCX_MODE_PERM,
    EPOCX_RAW_EEG_ID_MODE7,
    EmotivEpocX,
    aes_key_epocx_mode,
    serial_bytes_ble_hex,
    serial_bytes_hid,
)


def _build_mode7_plain(
    counter: int,
    type_byte: int,
    words: list[int],
) -> bytes:
    assert len(words) == 15
    return bytes([counter & 0xFF, type_byte & 0xFF]) + struct.pack("<15H", *words)


def _encrypt_wire(plain32: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(plain32)
    return bytes(b ^ 0x55 for b in encrypted)


class TestEpocXModeKey(unittest.TestCase):
    def test_perm_matches_android(self):
        self.assertEqual(
            EPOCX_MODE_PERM,
            [0, 1, 3, 3, 1, 0, 1, 3, 0, 3, 2, 1, 0, 1, 1, 2],
        )

    def test_ble_hex_serial_epocx_example(self):
        # EPOCX (E50202E9) → s = [0xE5, 0x02, 0x02, 0xE9]
        s4 = serial_bytes_ble_hex("E50202E9")
        self.assertEqual(s4, bytes([0xE5, 0x02, 0x02, 0xE9]))
        key = aes_key_epocx_mode(s4)
        # perm [0,1,3,3,1,0,1,3,0,3,2,1,0,1,1,2] over s=[E5,02,02,E9]
        expected = bytes(
            [
                0xE5,
                0x02,
                0xE9,
                0xE9,
                0x02,
                0xE5,
                0x02,
                0xE9,
                0xE5,
                0xE9,
                0x02,
                0x02,
                0xE5,
                0x02,
                0x02,
                0x02,
            ]
        )
        self.assertEqual(key, expected)

    def test_hid_last4_ud2022(self):
        # CyKit USB: last-4 ASCII of UD20221202006756 → '6','5','7','6'
        serial = "UD20221202006756"
        s4 = serial_bytes_hid(serial)
        self.assertEqual(s4, b"6576")
        key = aes_key_epocx_mode(s4)
        # Same schedule as historical get_crypto_key index list.
        sn = [ord(c) for c in serial]
        legacy = bytes(
            [
                sn[-1],
                sn[-2],
                sn[-4],
                sn[-4],
                sn[-2],
                sn[-1],
                sn[-2],
                sn[-4],
                sn[-1],
                sn[-4],
                sn[-3],
                sn[-2],
                sn[-1],
                sn[-2],
                sn[-2],
                sn[-3],
            ]
        )
        self.assertEqual(key, legacy)

    def test_device_get_crypto_key_hid(self):
        serial = "UD20221202006756"
        key = aes_key_epocx_mode(serial_bytes_hid(serial))
        headset = EmotivEpocX.init_with_serial(
            serial_number=serial,
            cryptokey=bytearray(key),
            serial_source="hid",
        )
        # cryptokey path sets cipher; rebuild via helper equality
        self.assertEqual(aes_key_epocx_mode(serial_bytes_hid(serial)), key)

        headset2 = EmotivEpocX(serial_number=serial, serial_source="hid")
        self.assertEqual(bytes(headset2.get_crypto_key()), key)


class TestMode7Decode(unittest.TestCase):
    def setUp(self):
        self.key = aes_key_epocx_mode(serial_bytes_ble_hex("E50202E9"))
        self.headset = EmotivEpocX.init_with_serial(
            serial_number="E50202E9",
            cryptokey=bytearray(self.key),
            serial_source="ble_hex",
            enable_electrode_quality_stream=True,
            enable_motion_data=True,
        )

    def test_plaintext_remap_and_adc(self):
        # Distinct words 0..14 so remap is obvious.
        words = list(range(1000, 1015))
        plain = _build_mode7_plain(counter=3, type_byte=0x10, words=words)
        eeg, cq = self.headset.decode_eeg_mode7(plain)
        self.assertIsNotNone(eeg)
        self.assertEqual(len(eeg), 14)
        expected = [words[i] / ADC_DIV_16BIT for i in EPOCX_RAW_EEG_ID_MODE7]
        for a, b in zip(eeg, expected):
            self.assertAlmostEqual(a, b, places=9)
        # word7 CQ
        self.assertEqual(cq, [float(words[7] & 0x7F)] * 14)

    def test_skips_non_eeg_type(self):
        words = [0] * 15
        plain = _build_mode7_plain(0, 0x20, words)
        eeg, cq = self.headset.decode_eeg_mode7(plain)
        self.assertIsNone(eeg)
        self.assertIsNone(cq)

    def test_encrypted_roundtrip(self):
        words = [100 + i * 17 for i in range(15)]
        words[7] = 0x12AB  # CQ low 7 bits = 0x2B
        plain = _build_mode7_plain(5, 0x10, words)
        wire = _encrypt_wire(plain, self.key)
        eeg, cq = self.headset.decode_data(wire)
        self.assertIsNotNone(eeg)
        expected = [words[i] / ADC_DIV_16BIT for i in EPOCX_RAW_EEG_ID_MODE7]
        for a, b in zip(eeg, expected):
            self.assertAlmostEqual(a, b, places=9)
        self.assertEqual(cq[0], float(0x12AB & 0x7F))

    def test_extract_quality_keymodel8(self):
        words = [0] * 15
        words[7] = 0x0042
        plain = _build_mode7_plain(1, 0x10, words)
        q = self.headset.extractQualityValues(plain, return_as_array=True)
        self.assertEqual(list(q), [0x42] * 14)

    def test_convertEPOC_PLUS_approx_div78(self):
        # CyKit ≈ LE16 / 7.8 within float noise.
        lo, hi = 0x34, 0x12
        le16 = lo | (hi << 8)
        cy = float(self.headset.convertEPOC_PLUS(str(lo), str(hi)))
        self.assertAlmostEqual(cy, le16 / 7.8, places=4)


class TestMotion20(unittest.TestCase):
    def setUp(self):
        key = aes_key_epocx_mode(b"\x00\x00\x00\x00")
        self.headset = EmotivEpocX.init_with_serial(
            serial_number="00000000",
            cryptokey=bytearray(key),
            enable_motion_data=True,
        )

    def test_motion_be_scale_and_axes(self):
        # raw0..raw8 as BE int16; verify correctMotionDirection + divisors.
        raws = [1000, -2000, 3000, 4000, -5000, 6000, 7, -8, 9]
        buf = bytearray(20)
        buf[0] = 1
        buf[1] = 0x20
        for i, v in enumerate(raws):
            struct.pack_into(">h", buf, 2 + 2 * i, v)

        out = self.headset.decode_motion_data(buf)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 10)

        q1 = raws[1] / 16384.0
        q2 = (-raws[0]) / 16384.0
        q3 = raws[2] / 16384.0
        n2 = min(q1 * q1 + q2 * q2 + q3 * q3, 1.0)
        q0 = math.sqrt(1.0 - n2)
        expected = [
            q0,
            q1,
            q2,
            q3,
            raws[4] / 8191.0,
            (-raws[3]) / 8191.0,
            raws[5] / 8191.0,
            (-raws[7]) / 6.0,
            raws[6] / 6.0,
            raws[8] / 6.0,
        ]
        for a, b in zip(out, expected):
            self.assertAlmostEqual(a, b, places=9)

    def test_decode_data_routes_20b_motion(self):
        buf = bytearray(20)
        buf[0] = 2
        for i in range(9):
            struct.pack_into(">h", buf, 2 + 2 * i, i)
        eeg, cq = self.headset.decode_data(buf)
        self.assertIsNotNone(eeg)
        self.assertEqual(len(eeg), 10)
        self.assertIsNone(cq)


class TestNoCyKitGyroHeuristic(unittest.TestCase):
    def test_type_0x20_in_32b_is_not_motion(self):
        key = aes_key_epocx_mode(b"abcd")
        hs = EmotivEpocX.init_with_serial(
            serial_number="abcd",
            cryptokey=bytearray(key),
            enable_motion_data=True,
        )
        plain = _build_mode7_plain(0, 0x20, [0] * 15)
        wire = _encrypt_wire(plain, key)
        decoded, cq = hs.decode_data(wire)
        self.assertIsNone(decoded)
        self.assertIsNone(cq)


if __name__ == "__main__":
    unittest.main(verbosity=2)
