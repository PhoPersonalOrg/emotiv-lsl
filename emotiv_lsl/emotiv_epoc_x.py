import hashlib
import logging
import math
import struct
from Crypto.Cipher import AES
from typing import Dict, List, Tuple, Optional, Callable, Union, Any
import pylsl
from pylsl import StreamInfo
from attrs import define, field, Factory

from emotiv_lsl.emotiv_base import EmotivBase
from emotiv_lsl.hid_device import UsbHeadsetConfig, configure_epocx_usb_mode, read_usb_feature_report_00
from config import MOTION_SRATE, SRATE


logger = logging.getLogger("emotiv_lsl")

# Epoc X Version 2 (fw <= 0x073E) Mode-7 16-byte key permutation.
EPOCX_MODE_PERM = [0, 1, 3, 3, 1, 0, 1, 3, 0, 3, 2, 1, 0, 1, 1, 2]

# Epoc X Mode-7 14-channel EEG word index map (skips word 7 = CQ / Battery).
EPOCX_RAW_EEG_ID_MODE7 = [2, 3, 0, 1, 4, 5, 6, 8, 9, 10, 13, 14, 11, 12]

# Cortex DataOutputContactQuality (slot = counter % (eeg_rate // 2), slots 0..13 in ASIC order).
CQ_SLOT_CHANNEL_ORDER: Tuple[str, ...] = (
    "F3", "FC5", "AF3", "F7", "T7", "P7", "O1",
    "O2", "P8", "T8", "F8", "AF4", "FC6", "F4",
)
CQ_SLOT_SCALE_DIVISORS: Tuple[float, ...] = (
    212.0, 216.0, 226.0, 235.0, 231.0, 239.0, 245.0,
    179.0, 167.0, 162.0, 206.0, 204.0, 194.0, 179.0,
)
CQ_GRADE_THRESHOLDS: Tuple[float, float, float] = (0.2, 0.7, 1.7)
CQ_SLOT_TO_1020_IDX: Tuple[int, ...] = (2, 3, 0, 1, 4, 5, 6, 7, 8, 9, 12, 13, 10, 11)

# ADC LSB -> microvolt divisors: 16-bit -> ÷7.8, 14-bit -> ÷1.95.
ADC_DIV_16BIT = 7.8
ADC_DIV_14BIT = 1.95

EEG_TYPE = 0x10
MOTION_TYPE = 0x20

V2_MAX_FW = 0x073E
V2_XOR_BYTE = 0x55
V4_XOR_BYTE = 0x14
V4_SALT_1 = "Vohcha7e"
V4_SALT_2 = "Ut3phaej"


def cq_norm_to_grade(norm: float) -> int:
    """Discrete Cortex CQ grade {0, 1, 2, 4} from normalized ratio."""
    t0, t1, t2 = CQ_GRADE_THRESHOLDS
    if norm < t0:
        return 0
    if norm < t1:
        return 1
    if norm < t2:
        return 2
    return 4


def cq_norm_to_score(norm: float) -> float:
    """Continuous, threshold-aligned CQ score from calibrated Cortex ratio `norm >= 0`.

    Maps Cortex physical impedance breakpoints `(0.0, 0.2, 0.7, 1.7)` piecewise-linearly onto
    `(0.0, 1.0, 2.0, 3.0)` with unit slope above `1.7` (`3.0 + (norm - 1.7)`), preserving full
    analog impedance resolution while aligning with `pyPhoTimeline` / `PhoPyMNEHelper` cutoffs
    (`<= 0` No Signal, `(0, 1]` Bad, `(1, 2]` Low/Mask, `[2, 3)` Fair, `>= 3` Good).
    """
    val = float(norm)
    if val <= 0.0:
        return 0.0
    t0, t1, t2 = CQ_GRADE_THRESHOLDS
    if val < t0:
        return val / t0
    if val < t1:
        return 1.0 + (val - t0) / (t1 - t0)
    if val < t2:
        return 2.0 + (val - t1) / (t2 - t1)
    return 3.0 + (val - t2)


def cq_score_to_norm(score: float) -> float:
    """Exact inverse of `cq_norm_to_score` (continuous score -> calibrated Cortex ratio `norm >= 0`)."""
    s = float(score)
    if s <= 0.0:
        return 0.0
    t0, t1, t2 = CQ_GRADE_THRESHOLDS
    if s < 1.0:
        return s * t0
    if s < 2.0:
        return t0 + (s - 1.0) * (t1 - t0)
    if s < 3.0:
        return t1 + (s - 2.0) * (t2 - t1)
    return t2 + (s - 3.0)


def aes_key_epocx_mode(s4: bytes) -> bytes:
    """Version 2 (fw <= 0x073E) Mode-7 AES-128 key from first four prepared serial bytes."""
    if len(s4) < 4:
        raise ValueError(f"EpocXMode needs >= 4 serial bytes, got {len(s4)}")
    return bytes(s4[i] for i in EPOCX_MODE_PERM)


def serial_bytes_hid(serial: str) -> bytes:
    """2.4 GHz USB HID V2 key material: reversed last-4 ASCII bytes of `"UD..."` dongle serial."""
    if serial is None or len(serial) < 4:
        raise ValueError("HID serial must be a string with length >= 4")
    return bytes(
        [
            ord(serial[-1]),
            ord(serial[-2]),
            ord(serial[-3]),
            ord(serial[-4]),
        ]
    )


def serial_bytes_ble_hex(serial_hex: str) -> bytes:
    """Offline BLE replay V2 key material: first 4 unreversed binary bytes of hex serial."""
    if serial_hex is None:
        raise ValueError("BLE hex serial is required")
    cleaned = (
        str(serial_hex)
        .strip()
        .replace(" ", "")
        .replace("-", "")
        .replace(":", "")
    )
    if cleaned.upper().startswith("EPOCX-"):
        cleaned = cleaned[6:]
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    raw = bytes.fromhex(cleaned)
    if len(raw) < 4:
        raise ValueError(f"BLE hex serial needs >= 4 bytes, got {len(raw)}")
    return raw[:4]


def derive_v4_key(headset_serial_hex: Union[str, bytes, bytearray], headset_fw: int) -> bytes:
    """Version 4 (fw >= 0x073F) 32-byte AES-256 key from 8-char paired headset hex serial + fw."""
    if isinstance(headset_serial_hex, (bytes, bytearray)):
        raw = bytes(headset_serial_hex)
        s_clean = raw[:4].hex().upper() if len(raw) in (4, 5) else raw.decode("ascii").strip().upper()
    else:
        s_clean = str(headset_serial_hex).strip().upper().replace(" ", "").replace("-", "").replace(":", "")
        if s_clean.startswith("EPOCX"):
            s_clean = s_clean[5:]
        if s_clean.startswith("0X"):
            s_clean = s_clean[2:]
    if s_clean.startswith("UD"):
        raise ValueError(f"V4 key requires paired headset hex serial (Feature Report 0x00 buf[7:11]), got USB dongle serial {s_clean!r}")
    if len(s_clean) < 8 or not all(c in "0123456789ABCDEF" for c in s_clean[:8]):
        raise ValueError(f"V4 key requires >= 8 uppercase hex chars, got {headset_serial_hex!r}")
    s8 = s_clean[:8]
    preimage = f"{V4_SALT_1}{s8[:4]}{V4_SALT_2}{s8[4:8]}{int(headset_fw):04X}".encode("ascii")
    return hashlib.sha256(preimage).digest()


@define(slots=False)
class EmotivEpocX(EmotivBase):
    READ_SIZE: int = field(default=32)
    device_name: str = field(default="Emotiv Epoc X")
    KeyModel: int = field(default=8)  # Epoc X keymodel 8 (extends CyKit numbering)

    is_reverse_engineer_mode: bool = field(default=False)
    # 'hid' = 2.4 GHz USB HID dongle ("UD..." V2 default); 'ble_hex' = isolated offline BLE replay.
    serial_source: str = field(default="hid")
    # Paired headset metadata discovered from USB Feature Report 0x00 (or wire replay header).
    headset_serial: Optional[str] = field(default=None)
    headset_fw: Optional[int] = field(default=None)
    dongle_fw: Optional[int] = field(default=None)
    # EEG resolution bits: 0x10 (16-bit -> ÷7.8) or 0x0E (14-bit -> ÷1.95).
    eeg_resolution_bits: int = field(default=0x10)
    eeg_rate: int = field(default=SRATE)
    mems_rate: int = field(default=MOTION_SRATE)
    mems_bits: int = field(default=16)
    headband_position: str = field(default="top")
    # Optional USB Output Report 0x00 mode reconfiguration applied on HID open.
    target_eeg_rate: Optional[int] = field(default=None)
    target_eeg_bits: Optional[int] = field(default=None)
    target_mems_rate: Optional[int] = field(default=None)
    target_mems_bits: Optional[int] = field(default=None)
    # In-band state updated during Mode-7 EEG / Motion decoding.
    battery_percent: Optional[int] = field(default=None)
    last_marker_hw: int = field(default=0)
    eeg_counter_drops: int = field(default=0, init=False, repr=False)
    mems_counter_drops: int = field(default=0, init=False, repr=False)
    _last_eeg_counter: Optional[int] = field(default=None, init=False, repr=False)
    _last_mems_counter: Optional[int] = field(default=None, init=False, repr=False)
    _explicit_cipher_provided: bool = field(default=False, init=False, repr=False)
    _cq_scores: List[float] = field(factory=lambda: [0.0] * 14, init=False, repr=False)


    def __attrs_post_init__(self):
        if self.cipher is None:
            crypto_key = self.get_crypto_key()
            self.cipher = AES.new(crypto_key, AES.MODE_ECB)
        else:
            self._explicit_cipher_provided = True
            print("working cipher was provided on startup!")

        if self.is_reverse_engineer_mode:
            print("starting with reverse engineer mode!")
        self.init_EasyTimeSyncParsingMixin()


    @property
    def is_v4(self) -> bool:
        return self.headset_fw is not None and int(self.headset_fw) > V2_MAX_FW


    @property
    def adc_div(self) -> float:
        return ADC_DIV_16BIT if int(self.eeg_resolution_bits) >= 16 else ADC_DIV_14BIT


    def get_hid_device(self):
        from emotiv_lsl.hid_device import find_emotiv_hid_device

        return find_emotiv_hid_device()


    def apply_usb_feature_config(self, cfg: UsbHeadsetConfig) -> None:
        """Apply parsed USB HID Feature Report 0x00 metadata and refresh AES cipher if needed."""
        self.dongle_fw = cfg.dongle_fw
        if cfg.headset_fw == 0:
            return
        self.headset_fw = cfg.headset_fw
        if cfg.headset_serial and cfg.headset_serial != "00000000":
            self.headset_serial = cfg.headset_serial
        if cfg.eeg_rate in (128, 256):
            self.eeg_rate = cfg.eeg_rate
        if cfg.eeg_bits in (14, 16):
            self.eeg_resolution_bits = 0x10 if cfg.eeg_bits == 16 else 0x0E
        if cfg.mems_rate in (0, 32, 64, 128):
            self.mems_rate = cfg.mems_rate
        if cfg.mems_bits in (14, 16):
            self.mems_bits = cfg.mems_bits
        if not self._explicit_cipher_provided:
            self.cipher = AES.new(self.get_crypto_key(), AES.MODE_ECB)


    def on_hid_device_opened(self, hid_device: Any, device: Dict[str, Any]) -> None:
        """Query USB Feature Report 0x00 (and apply optional Output Report 0x00 mode switch) on live HID open."""
        if self.serial_number is None and device.get("serial_number"):
            sn = device["serial_number"]
            self.serial_number = sn.decode("utf-8", errors="replace") if isinstance(sn, bytes) else str(sn)

        cfg = read_usb_feature_report_00(hid_device)
        if any(v is not None for v in (self.target_eeg_rate, self.target_eeg_bits, self.target_mems_rate, self.target_mems_bits)):
            cur_eeg_rate = self.target_eeg_rate if self.target_eeg_rate is not None else (cfg.eeg_rate if cfg and cfg.headset_fw else self.eeg_rate)
            cur_eeg_bits = self.target_eeg_bits if self.target_eeg_bits is not None else (cfg.eeg_bits if cfg and cfg.headset_fw else (16 if self.adc_div == ADC_DIV_16BIT else 14))
            cur_mems_rate = self.target_mems_rate if self.target_mems_rate is not None else (cfg.mems_rate if cfg and cfg.headset_fw else self.mems_rate)
            cur_mems_bits = self.target_mems_bits if self.target_mems_bits is not None else (cfg.mems_bits if cfg and cfg.headset_fw and cfg.mems_bits else self.mems_bits)
            updated = configure_epocx_usb_mode(
                hid_device,
                eeg_rate=cur_eeg_rate,
                eeg_bits=cur_eeg_bits,
                mems_rate=cur_mems_rate,
                mems_bits=cur_mems_bits,
            )
            if updated is not None:
                cfg = updated

        if cfg is not None:
            self.apply_usb_feature_config(cfg)
            if cfg.headset_fw != 0:
                print(
                    f"USB Feature Report 0x00: headset=EPOCX-{self.headset_serial} "
                    f"headset_fw=0x{cfg.headset_fw:04X} dongle_fw=0x{cfg.dongle_fw:04X} "
                    f"eeg={self.eeg_rate}Hz/{16 if self.adc_div == ADC_DIV_16BIT else 14}b "
                    f"mems={self.mems_rate}Hz/{self.mems_bits}b "
                    f"crypto={'V4-AES256' if self.is_v4 else 'V2-AES128'}"
                )


    def get_crypto_key(self) -> bytearray:
        """Derive V2 AES-128 key (fw <= 0x073E) or V4 AES-256 key (fw >= 0x073F)."""
        if isinstance(self.serial_number, (bytes, bytearray)) and len(self.serial_number) in (16, 32):
            return bytearray(self.serial_number)

        if self.is_v4:
            v4_serial = self.headset_serial or self.serial_number
            if v4_serial is None:
                raise ValueError("V4 firmware (fw >= 0x073F) requires headset_serial from USB Feature Report 0x00")
            return bytearray(derive_v4_key(v4_serial, int(self.headset_fw)))

        if self.serial_number is None:
            serial = self.get_hid_device()["serial_number"]
            self.serial_number = serial
        else:
            serial = self.serial_number

        if isinstance(serial, (bytes, bytearray)) and len(serial) >= 4:
            s4 = bytes(serial[:4])
        elif self.serial_source == "ble_hex":
            s4 = serial_bytes_ble_hex(str(serial))
        else:
            s4 = serial_bytes_hid(str(serial))

        return bytearray(aes_key_epocx_mode(s4))


    def get_lsl_source_id(self) -> str:
        source_id: str = self.get_crypto_key().hex()
        return f"{self.device_name}_{self.KeyModel}_{source_id}"


    def _append_usb_device_desc(self, info: StreamInfo) -> StreamInfo:
        """Append USB dongle / paired headset metadata to LSL StreamInfo XML."""
        desc = info.desc()
        if isinstance(self.serial_number, str) and self.serial_number:
            desc.append_child_value("usb_dongle_serial", self.serial_number)
        if self.headset_serial:
            hs_id = self.headset_serial if self.headset_serial.startswith("EPOCX-") else f"EPOCX-{self.headset_serial}"
            desc.append_child_value("headset_serial", hs_id)
        if self.headset_fw is not None:
            desc.append_child_value("headset_fw", f"0x{int(self.headset_fw):04X}")
        if self.dongle_fw is not None:
            desc.append_child_value("dongle_fw", f"0x{int(self.dongle_fw):04X}")
        desc.append_child_value("crypto_version", "V4" if self.is_v4 else "V2")
        return info


    def get_lsl_outlet_motion_stream_info(self) -> StreamInfo:
        """LSL stream for Epoc X 10-channel IMU Motion (Q0..Q3 + AccX..Z + MagX..Z)."""
        ch_names = [
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
        ]
        n_channels = len(ch_names)
        nominal_mems_hz = float(self.mems_rate if self.mems_rate and self.mems_rate > 0 else MOTION_SRATE)

        info = StreamInfo(
            "Epoc X Motion",
            type="SIGNAL",
            channel_count=n_channels,
            nominal_srate=nominal_mems_hz,
            channel_format=pylsl.cf_float32,
            source_id=self.get_lsl_source_id(),
        )
        chns = info.desc().append_child("channels")

        for label in ch_names[:4]:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "normalized")
            ch.append_child_value("type", "QUAT")
            ch.append_child_value("scaling_factor", "16384")
        ## END for label in ch_names[:4]...

        for label in ch_names[4:7]:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "g")
            ch.append_child_value("type", "ACC")
            ch.append_child_value("scaling_factor", "8191")
        ## END for label in ch_names[4:7]...

        for label in ch_names[7:]:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "uT")
            ch.append_child_value("type", "MAG")
            ch.append_child_value("scaling_factor", "6")
        ## END for label in ch_names[7:]...

        info.desc().append_child_value(
            "motion_layout",
            f"Epoc X USB 32B encrypted type 0x20 (20B BE int16 + 12B pad): Q/ACC/MAG ({self.headband_position})",
        )
        info.desc().append_child_value("headband_position", str(self.headband_position))
        info = self._append_usb_device_desc(info)
        info = self.add_lsl_outlet_info_common(info)
        return info


    def get_lsl_outlet_eeg_stream_info(self) -> StreamInfo:
        ch_names = self.eeg_channel_names
        n_channels = len(ch_names)

        info = StreamInfo(
            "Epoc X",
            type="EEG",
            channel_count=n_channels,
            nominal_srate=float(self.eeg_rate),
            channel_format=pylsl.cf_float32,
            source_id=self.get_lsl_source_id(),
        )
        chns = info.desc().append_child("channels")
        for label in ch_names:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "microvolts")
            ch.append_child_value("type", "EEG")
            ch.append_child_value("scaling_factor", "1")
        ## END for label in ch_names...

        cap = info.desc().append_child("cap")
        cap.append_child_value("name", "easycap-M1")
        cap.append_child_value("labelscheme", "10-20")

        info.desc().append_child_value("adc_div", str(self.adc_div))
        info.desc().append_child_value(
            "adc_scale_note",
            "Mode-7 16-bit default: sample_uV = LE16_word / 7.8 "
            "(14-bit uses /1.95). CyKit convertEPOC_PLUS ≈ LE16/7.8.",
        )
        info.desc().append_child_value(
            "eeg_layout",
            "Epoc X USB 32B encrypted Mode-7 (type 0x10): LE16 + word remap; word7 is CQ/Battery",
        )

        info = self._append_usb_device_desc(info)
        info = self.add_lsl_outlet_info_common(info)
        return info


    def get_lsl_outlet_electrode_quality_stream_info(self) -> StreamInfo:
        """Continuous per-electrode Contact Quality (CQ) stream in 10-20 channel order."""
        ch_names = self.eeg_quality_channel_names
        n_channels = len(ch_names)

        info = StreamInfo(
            "Epoc X eQuality",
            type="Raw",
            channel_count=n_channels,
            nominal_srate=float(self.eeg_rate),
            channel_format=pylsl.cf_float32,
            source_id=self.get_lsl_source_id(),
        )
        chns = info.desc().append_child("channels")
        for label in ch_names:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "cq")
            ch.append_child_value("type", "CQ")
            ch.append_child_value("scaling_factor", "1")
        ## END for label in ch_names...

        cap = info.desc().append_child("cap")
        cap.append_child_value("name", "easycap-M1")
        cap.append_child_value("labelscheme", "10-20")
        info.desc().append_child_value(
            "cq_note",
            "Mode-7 continuous per-electrode CQ demux: slot = counter % (eeg_rate // 2), norm = (word7 * 0.25) / scale[slot], continuous score (0->0, 0.2->1, 0.7->2, 1.7->3, >1.7->3+(norm-1.7))",
        )

        info = self._append_usb_device_desc(info)
        info = self.add_lsl_outlet_info_common(info)
        return info


    def update_cq_from_word7(self, counter: int, word7: int) -> List[float]:
        """Demux Mode-7 16-bit word7 by packet counter into sticky 10-20 continuous CQ scores."""
        half_rate = max(1, int(self.eeg_rate) // 2)
        slot = (int(counter) & 0xFF) % half_rate
        if 0 <= slot < 14:
            norm = (float(int(word7) & 0xFFFF) * 0.25) / CQ_SLOT_SCALE_DIVISORS[slot]
            self._cq_scores[CQ_SLOT_TO_1020_IDX[slot]] = cq_norm_to_score(norm)
        return list(self._cq_scores)


    def decrypt_packet(self, data, *, is_mems: bool = False) -> bytes:
        """Decrypt a 32-byte USB/BLE block (`is_mems=False`) or 20-byte offline BLE Motion packet (`is_mems=True`)."""
        raw = bytes(data)
        if not self.is_v4:
            if is_mems:
                return raw
            xored = bytes(el ^ V2_XOR_BYTE for el in raw[:32])
            return self.cipher.decrypt(xored)

        n_aligned = (len(raw) // 16) * 16
        head_xored = bytes(el ^ V4_XOR_BYTE for el in raw[:n_aligned])
        head_plain = self.cipher.decrypt(head_xored) if n_aligned > 0 else b""
        tail_plain = bytes(el ^ V4_XOR_BYTE for el in raw[n_aligned:])
        return head_plain + tail_plain


    def decrypt_eeg_packet(self, data) -> bytes:
        """Decrypt a 32-byte USB HID report (EEG `0x10` or Motion `0x20`)."""
        return self.decrypt_packet(data[:32], is_mems=False)


    def unpack_mode7_words(self, plain: bytes) -> Tuple[int, int, Tuple[int, ...]]:
        """Parse Mode-7 Epoc X package: counter, type_raw, 15x LE uint16."""
        if len(plain) < 32:
            raise ValueError(f"Mode-7 package needs 32 bytes, got {len(plain)}")
        counter = plain[0]
        type_raw = plain[1]
        words = struct.unpack_from("<15H", plain, 2)
        return counter, type_raw, words


    def decode_eeg_mode7(self, plain: bytes) -> Tuple[Optional[List[float]], Optional[List[float]]]:
        """Mode-7 EEG: LE16 words -> channel remap -> ADC scale; battery gate + CQ demux from word7."""
        counter, type_raw, words = self.unpack_mode7_words(plain)
        pkg_type = type_raw & 0x7F
        if pkg_type != EEG_TYPE:
            if self.enable_debug_logging:
                logging.getLogger("emotiv.epoc_x").debug(
                    f"Skipping non-EEG Mode-7 type={pkg_type:#x} counter={counter}"
                )
            return None, None

        self.last_marker_hw = (type_raw >> 7) & 1
        mod = 256 if int(self.eeg_rate) >= 256 else 128
        cnt_mod = int(counter) % mod
        if self._last_eeg_counter is not None:
            delta = (cnt_mod - self._last_eeg_counter) % mod
            if delta > 1:
                self.eeg_counter_drops += delta - 1
        self._last_eeg_counter = cnt_mod

        if int(counter) == int(self.eeg_rate) - 1 and words[7] != 0:
            self.battery_percent = min(100, max(0, int(words[7]) & 0x7F))

        adc_div = self.adc_div
        packet_data = [float(words[idx]) / adc_div for idx in EPOCX_RAW_EEG_ID_MODE7]

        eeg_quality_data = None
        if self.enable_electrode_quality_stream:
            eeg_quality_data = self.update_cq_from_word7(counter, words[7])

        return packet_data, eeg_quality_data


    def decode_data(self, data) -> Tuple[Optional[List], Optional[List]]:
        """Decode one USB HID input report (or isolated offline replay packet).

        - 32 B (or 33 B with leading Report ID 0x00): decrypt 32 B (`is_mems=False`), then branch on `plain[1] & 0x7F`:
            * `0x10` -> Mode-7 EEG + CQ/Battery (`plain[0:32]`)
            * `0x20` -> USB Motion (`plain[0:20]`, ignoring 12 zero-pad bytes `plain[20:32]`)
        - 20 B: isolated offline BLE replay Motion fallback (`decrypt_packet(..., is_mems=True)`).
        """
        if self.enable_debug_logging and self.is_reverse_engineer_mode:
            logging.debug(f"decode_data(data: {data})")
            logger.warning(f"{data}")

        if len(data) == 33 and data[0] == 0:
            data = data[1:33]

        n = len(data)
        if n == 20:
            if not self.enable_motion_data:
                return None, None
            plain20 = self.decrypt_packet(data, is_mems=True)
            return self.decode_motion_data(plain20), None

        if n < 32:
            return None, None

        plain = self.decrypt_packet(data[:32], is_mems=False)
        pkg_type = plain[1] & 0x7F
        if pkg_type == EEG_TYPE:
            return self.decode_eeg_mode7(plain)
        if pkg_type == MOTION_TYPE:
            if not self.enable_motion_data:
                return None, None
            return self.decode_motion_data(plain[:20]), None

        if self.enable_debug_logging:
            logging.getLogger("emotiv.epoc_x").debug(
                f"Skipping non-data USB HID report type={pkg_type:#x} counter={plain[0]}"
            )
        return None, None


    def decode_motion_data(self, data) -> Optional[List[float]]:
        """Decode 20-byte Motion plaintext (`plain[0:20]`) with `"top"` or `"back"` headband orientation.

        Returns `[Q0, Q1, Q2, Q3, AccX, AccY, AccZ, MagX, MagY, MagZ]`.
        """
        if data is None or len(data) < 20:
            return None

        raw_bytes = bytes(data)
        cnt_mems = raw_bytes[0] % 128
        if self._last_mems_counter is not None:
            delta = (cnt_mems - self._last_mems_counter) % 128
            if delta > 1:
                self.mems_counter_drops += delta - 1
        self._last_mems_counter = cnt_mems

        q1_r, q2_r, q3_r, ax_r, ay_r, az_r, mx_r, my_r, mz_r = struct.unpack_from(">9h", raw_bytes, 2)

        if str(self.headband_position).strip().lower() == "back":
            q1, q2, q3 = float(q1_r), float(q2_r), float(q3_r)
            accx, accy, accz = float(ax_r), float(ay_r), float(az_r)
            magx, magy, magz = float(mx_r), float(my_r), float(mz_r)
        else:
            q1, q2, q3 = float(q2_r), float(-q1_r), float(q3_r)
            accx, accy, accz = float(ay_r), float(-ax_r), float(az_r)
            magx, magy, magz = float(-my_r), float(mx_r), float(mz_r)

        q1s, q2s, q3s = q1 / 16384.0, q2 / 16384.0, q3 / 16384.0
        n2 = q1s * q1s + q2s * q2s + q3s * q3s
        q0 = 0.0 if n2 >= 1.0 else math.sqrt(1.0 - n2)

        self.has_motion_data = True
        return [
            q0,
            q1s,
            q2s,
            q3s,
            accx / 8191.0,
            accy / 8191.0,
            accz / 8191.0,
            magx / 6.0,
            magy / 6.0,
            magz / 6.0,
        ]


    def validate_data(self, data) -> bool:
        n = len(data)
        if self.is_reverse_engineer_mode:
            return n == self.READ_SIZE or (n == self.READ_SIZE + 1 and data[0] == 0)
        # 32 (or 33 with leading 0x00 Report ID) = encrypted USB HID EEG/Motion; 20 = offline BLE replay.
        return n == 32 or (n == 33 and data[0] == 0) or n == 20
