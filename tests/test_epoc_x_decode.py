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

from emotiv_lsl.emotiv_epoc_plus import EmotivEpocPlus
from emotiv_lsl.emotiv_epoc_x import (
    ADC_DIV_16BIT,
    EPOCX_MODE_PERM,
    EPOCX_RAW_EEG_ID_MODE7,
    EmotivEpocX,
    aes_key_epocx_mode,
    derive_v4_key,
    serial_bytes_ble_hex,
    serial_bytes_hid,
)
from emotiv_lsl.epocx_wire import encrypt_wire_packet
from emotiv_lsl.hid_device import (
    _is_preferred_eeg_interface,
    _is_status_only_interface,
    build_epocx_usb_mode_report,
    is_usb_mode_ack,
    parse_usb_feature_report_00,
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
        ## END for a, b in zip(eeg, expected)...

        # counter=3 -> slot 3 (F7, 10-20 idx 1, div 235.0): (1007 * 0.25) / 235 = 1.071 -> grade 2
        expected_cq = [0.0] * 14
        expected_cq[1] = 2.0
        self.assertEqual(cq, expected_cq)


    def test_cq_demux_sticky_and_battery_hold(self):
        words = [0] * 15
        # Packet 1: counter=2 -> slot 2 (AF3, 10-20 idx 0, div 226.0), word7=1800 -> grade 4
        words[7] = 1800
        _, cq1 = self.headset.decode_eeg_mode7(_build_mode7_plain(2, 0x10, words))
        expected = [0.0] * 14
        expected[0] = 4.0
        self.assertEqual(cq1, expected)

        # Packet 2: counter=0 -> slot 0 (F3, 10-20 idx 2, div 212.0), word7=300 -> grade 1; AF3 holds 4
        words[7] = 300
        _, cq2 = self.headset.decode_eeg_mode7(_build_mode7_plain(0, 0x10, words))
        expected[2] = 1.0
        self.assertEqual(cq2, expected)

        # Packet 3: counter=14 -> slot 14 (outside 0..13), holds previous state
        words[7] = 5000
        _, cq3 = self.headset.decode_eeg_mode7(_build_mode7_plain(14, 0x10, words))
        self.assertEqual(cq3, expected)

        # Packet 4: counter=127 (eeg_rate - 1 battery slot -> slot 63), battery byte does not overwrite CQ
        words[7] = 0x004A
        _, cq4 = self.headset.decode_eeg_mode7(_build_mode7_plain(127, 0x10, words))
        self.assertEqual(cq4, expected)


    def test_skips_non_eeg_type(self):
        words = [0] * 15
        plain = _build_mode7_plain(0, 0x20, words)
        eeg, cq = self.headset.decode_eeg_mode7(plain)
        self.assertIsNone(eeg)
        self.assertIsNone(cq)


    def test_encrypted_roundtrip(self):
        words = [100 + i * 17 for i in range(15)]
        words[7] = 0x12AB  # full 16-bit word7 = 4779 -> grade 4 on counter=5 (slot 5 = P7, 10-20 idx 5)
        plain = _build_mode7_plain(5, 0x10, words)
        wire = _encrypt_wire(plain, self.key)
        eeg, cq = self.headset.decode_data(wire)
        self.assertIsNotNone(eeg)
        expected = [words[i] / ADC_DIV_16BIT for i in EPOCX_RAW_EEG_ID_MODE7]
        for a, b in zip(eeg, expected):
            self.assertAlmostEqual(a, b, places=9)
        ## END for a, b in zip(eeg, expected)...

        expected_cq = [0.0] * 14
        expected_cq[5] = 4.0
        self.assertEqual(cq, expected_cq)


    def test_extract_quality_keymodel8(self):
        words = [0] * 15
        words[7] = 1800  # counter=2 -> slot 2 (AF3, 10-20 idx 0) -> grade 4
        plain = _build_mode7_plain(2, 0x10, words)
        q = self.headset.extractQualityValues(plain, return_as_array=True)
        expected_cq = [0.0] * 14
        expected_cq[0] = 4.0
        self.assertEqual(list(q), expected_cq)
        q_dict = self.headset.extractQualityValues(plain, return_as_array=False)
        self.assertEqual(q_dict["AF3"], 4.0)
        self.assertEqual(q_dict["F7"], 0.0)

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


class TestUsb32ByteMotionDemux(unittest.TestCase):
    def test_type_0x20_in_32b_decodes_usb_motion(self):
        key = aes_key_epocx_mode(b"abcd")
        hs_on = EmotivEpocX.init_with_serial(
            serial_number="abcd",
            cryptokey=bytearray(key),
            enable_motion_data=True,
        )
        hs_off = EmotivEpocX.init_with_serial(
            serial_number="abcd",
            cryptokey=bytearray(key),
            enable_motion_data=False,
        )
        mot20 = bytearray(20)
        mot20[0] = 7
        mot20[1] = 0x20
        struct.pack_into(">9h", mot20, 2, 1000, -2000, 3000, 4000, 8191, 0, 6, -12, 18)
        plain32 = bytes(mot20) + (b"\x00" * 12)
        wire32 = _encrypt_wire(plain32, key)

        decoded, cq = hs_on.decode_data(wire32)
        self.assertIsNotNone(decoded)
        self.assertEqual(len(decoded), 10)
        self.assertIsNone(cq)
        self.assertAlmostEqual(decoded[4], 1.0, places=9)  # AccX = 8191 / 8191

        # 33-byte OS HID buffer with leading Report ID 0x00 must decode identically.
        self.assertTrue(hs_on.validate_data(b"\x00" + wire32))
        decoded33, cq33 = hs_on.decode_data(b"\x00" + wire32)
        self.assertEqual(decoded33, decoded)
        self.assertIsNone(cq33)

        # When motion is disabled, 0x20 reports are skipped cleanly.
        skipped, cq_off = hs_off.decode_data(wire32)
        self.assertIsNone(skipped)
        self.assertIsNone(cq_off)


class TestWinVmRevEngGroundTruthVectors(unittest.TestCase):
    """Exact hardware ground-truth vectors from WinVMRevEng/NOTES/05_CleanRoom_Reimplementation_Spec.md."""

    def test_usb_feature_and_output_report_00_and_hid_filters(self):
        hw_feat = bytes.fromhex("00202006ff0720e50202e92b018000102010")
        cfg = parse_usb_feature_report_00(hw_feat)
        self.assertEqual(cfg.headset_type, 7)
        self.assertEqual(cfg.dongle_fw, 0x06FF)
        self.assertEqual(cfg.headset_fw, 0x0720)
        self.assertEqual(cfg.headset_serial, "E50202E9")
        self.assertEqual((cfg.eeg_rate, cfg.eeg_bits, cfg.mems_rate, cfg.mems_bits), (128, 16, 32, 16))

        out_rep = build_epocx_usb_mode_report(256, 16, 64, 16)
        self.assertEqual(len(out_rep), 33)
        self.assertEqual(out_rep[:6], bytes([0x00, 0x66, 0xBB, 0x20, 0x12, 0xEA]))
        self.assertTrue(is_usb_mode_ack(bytes([0x00, 0x00, 0x88, 0x12]) + bytes(28)))
        self.assertTrue(is_usb_mode_ack(bytes([0x00, 0x00, 0x00, 0x88, 0x12]) + bytes(28)))
        self.assertFalse(is_usb_mode_ack(bytes(32)))

        mi00 = {"vendor_id": 0x1234, "product_id": 0xED02, "usage_page": 0xF0FF, "usage": 0x0010, "interface_number": 0}
        mi01_1234 = {"vendor_id": 0x1234, "product_id": 0xED02, "usage_page": 0xFFFF, "usage": 0x0002, "interface_number": 1}
        mi01_21a1 = {"vendor_id": 0x21A1, "product_id": 0x0001, "usage_page": 0xFFFF, "usage": 0x0002, "interface_number": 1}
        self.assertTrue(_is_status_only_interface(mi00))
        self.assertFalse(_is_preferred_eeg_interface(mi00))
        self.assertTrue(_is_preferred_eeg_interface(mi01_1234))
        self.assertTrue(_is_preferred_eeg_interface(mi01_21a1))


    def test_live_usb_ud2024_eeg_and_motion_vectors(self):
        hs = EmotivEpocX(
            serial_number="UD20240731007676",
            serial_source="hid",
            enable_motion_data=True,
            enable_electrode_quality_stream=True,
        )
        cfg = parse_usb_feature_report_00(bytes.fromhex("00202006ff0720e50202e92b018000102010"))
        hs.apply_usb_feature_config(cfg)
        self.assertEqual(bytes(hs.get_crypto_key()).hex(), "36373737373637373637363736373736")
        self.assertEqual(bytes(hs.get_crypto_key()), b"6777767767676776")
        self.assertFalse(hs.is_v4)

        wire_eeg = bytes.fromhex("1723661a91450ed36b99e0f55a243ba5ec2f84bf5bd3dc3b161ca1395ae3679a")
        eeg, _cq = hs.decode_data(wire_eeg)
        expected_eeg = [
            4210.7692307692305, 4309.2307692307695, 4159.871794871795, 4244.871794871795,
            4251.282051282052, 4326.410256410257, 4331.538461538462, 4504.102564102564,
            4560.0, 4843.717948717949, 4396.538461538462, 5491.794871794872,
            4239.871794871795, 4117.4358974358975,
        ]
        self.assertIsNotNone(eeg)
        for a, b in zip(eeg, expected_eeg):
            self.assertAlmostEqual(a, b, places=12)
        ## END for a, b in zip(eeg, expected_eeg)...

        plain_mot = bytes.fromhex("1a20d2e2e0311887f900e3b0ef5cffffffffffff000000000000000000000000")
        wire_mot = bytes.fromhex("204b16fb12692075bd7911810c68531a8ee9f43612f7d5eb211c86fa823519d6")
        self.assertEqual(hs.decrypt_packet(wire_mot, is_mems=False), plain_mot)
        mot, mot_cq = hs.decode_data(wire_mot)
        self.assertIsNone(mot_cq)
        expected_mot = [
            0.33037257181502816, -0.49700927734375, 0.7049560546875, 0.38323974609375,
            -0.8848736418019778, 0.21877670614088635, -0.5200830179465267,
            1.0 / 6.0, -1.0 / 6.0, -1.0 / 6.0,
        ]
        self.assertIsNotNone(mot)
        for a, b in zip(mot, expected_mot):
            self.assertAlmostEqual(a, b, places=12)
        ## END for a, b in zip(mot, expected_mot)...


    def test_v4_key_equivalence_and_usb_plus_ble_roundtrip(self):
        k_ble5 = derive_v4_key("E50202E92B", 0x0752)
        k_usb4 = derive_v4_key("E50202E9", 0x0752)
        self.assertEqual(len(k_usb4), 32)
        self.assertEqual(k_ble5, k_usb4)
        with self.assertRaises(ValueError):
            derive_v4_key("UD20240731007676", 0x0752)

        feat_v4 = bytearray(32)
        feat_v4[0] = 0x00
        feat_v4[3:5] = bytes.fromhex("06FF")
        feat_v4[5:7] = bytes.fromhex("0752")
        feat_v4[7:11] = bytes.fromhex("E50202E9")
        feat_v4[12] = 0x01
        struct.pack_into("<H", feat_v4, 13, 256)
        feat_v4[15] = 16
        feat_v4[16] = 64
        feat_v4[17] = 16

        class FakeHid:
            def get_feature_report(self, report_id, length):
                return bytes(feat_v4[:length])

        hs = EmotivEpocX(serial_number="UD20240731007676", enable_motion_data=True, enable_electrode_quality_stream=True)
        hs.on_hid_device_opened(FakeHid(), {"serial_number": "UD20240731007676"})
        self.assertTrue(hs.is_v4)
        self.assertEqual(hs.eeg_rate, 256)
        self.assertEqual(hs.mems_rate, 64)
        self.assertEqual(bytes(hs.get_crypto_key()), k_usb4)

        # 32B V4 EEG with marker_hw=1 (0x90 = 0x80 | 0x10) and counter=255 -> battery 87%.
        words = [32768 + i * 100 for i in range(7)] + [87] + [33000 + i * 50 for i in range(7)]
        plain_eeg = bytes([255, 0x90]) + struct.pack("<15H", *words)
        wire_eeg = encrypt_wire_packet(plain_eeg, k_usb4, is_v4=True, is_mems=False)
        eeg, _ = hs.decode_data(wire_eeg)
        self.assertIsNotNone(eeg)
        self.assertEqual(hs.last_marker_hw, 1)
        self.assertEqual(hs.battery_percent, 87)
        self.assertAlmostEqual(eeg[0], words[2] / 7.8, places=9)

        # Hold battery across non-(eeg_rate-1) counter even when word7 == 0.
        plain_eeg0 = bytes([0, 0x10]) + struct.pack("<15H", *([32000] * 7), 1600, *([32000] * 7))
        _, cq0 = hs.decode_data(encrypt_wire_packet(plain_eeg0, k_usb4, is_v4=True, is_mems=False))
        self.assertEqual(hs.battery_percent, 87)
        self.assertEqual(hs.last_marker_hw, 0)
        self.assertEqual(cq0[2], 4.0)  # slot 0 = F3 -> 10-20 idx 2 -> grade 4

        # 32B V4 USB Motion (20B + 12B zero pad) & 20B V4 BLE Motion (16B AES-256 + 4B XOR 0x14).
        plain_mot20 = bytes([42, 0x20]) + struct.pack(">9h", 4096, -4096, 2048, 0, 8191, 0, 120, -60, 300)
        wire_usb_mot32 = encrypt_wire_packet(plain_mot20 + (b"\x00" * 12), k_usb4, is_v4=True, is_mems=False)
        wire_ble_mot20 = encrypt_wire_packet(plain_mot20, k_usb4, is_v4=True, is_mems=True)
        mot_usb, _ = hs.decode_data(wire_usb_mot32)
        mot_ble, _ = hs.decode_data(wire_ble_mot20)
        self.assertEqual(mot_usb, mot_ble)
        self.assertAlmostEqual(mot_usb[4], 1.0, places=9)


    def test_back_headband_identity_and_epoc_plus_keys(self):
        hs_back = EmotivEpocX(serial_number="UD20240731007676", headband_position="back", enable_motion_data=True)
        raws = [1024, -2048, 4096, 8191, -4095, 2048, 12, -18, 24]
        buf = bytes([5, 0x20]) + struct.pack(">9h", *raws)
        out = hs_back.decode_motion_data(buf)
        self.assertAlmostEqual(out[1], raws[0] / 16384.0, places=9)
        self.assertAlmostEqual(out[2], raws[1] / 16384.0, places=9)
        self.assertAlmostEqual(out[3], raws[2] / 16384.0, places=9)
        self.assertAlmostEqual(out[4], raws[3] / 8191.0, places=9)
        self.assertAlmostEqual(out[5], raws[4] / 8191.0, places=9)
        self.assertAlmostEqual(out[7], raws[6] / 6.0, places=9)

        ep_16 = EmotivEpocPlus(serial_number="UD20221202006756", is_fourteen_bit_mode=False)
        ep_14 = EmotivEpocPlus(serial_number="UD20221202006756", is_fourteen_bit_mode=True)
        self.assertEqual(len(ep_16.get_crypto_key()), 16)
        self.assertEqual(len(ep_14.get_crypto_key()), 16)
        self.assertNotEqual(ep_16.get_crypto_key(), ep_14.get_crypto_key())


if __name__ == "__main__":
    unittest.main(verbosity=2)
