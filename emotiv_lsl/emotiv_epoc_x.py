import logging
import math
import struct
from Crypto.Cipher import AES
from typing import Dict, List, Tuple, Optional, Callable, Union, Any
import pylsl
from pylsl import StreamInfo
from attrs import define, field, Factory

from emotiv_lsl.emotiv_base import EmotivBase
from config import MOTION_SRATE, SRATE


logger = logging.getLogger("emotiv_lsl")

# Android aesKeyVersion2_EpocXMode NEON tbl mask (libEmbeddedLibQt VA 0xc413a0).
EPOCX_MODE_PERM = [0, 1, 3, 3, 1, 0, 1, 3, 0, 3, 2, 1, 0, 1, 1, 2]

# Android getRawEegId for HeadsetType 7, mode 7 (skips word 7 / CQ).
EPOCX_RAW_EEG_ID_MODE7 = [2, 3, 0, 1, 4, 5, 6, 8, 9, 10, 13, 14, 11, 12]

# Android convertADCToMicrovolt: 16-bit → ÷7.8, 14-bit → ÷1.95.
ADC_DIV_16BIT = 7.8
ADC_DIV_14BIT = 1.95

EEG_TYPE = 0x10
MOTION_TYPE = 0x20


def aes_key_epocx_mode(s4: bytes) -> bytes:
    """Android Version2 EpocXMode AES-128 key from first four decrypt-serial bytes."""
    if len(s4) < 4:
        raise ValueError(f"EpocXMode needs >= 4 serial bytes, got {len(s4)}")
    return bytes(s4[i] for i in EPOCX_MODE_PERM)


def serial_bytes_hid(serial: str) -> bytes:
    """USB HID / CyKit material: s[0..3] = ASCII of serial[-1], [-2], [-3], [-4]."""
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
    """BLE-style decrypt serial: hex-decoded DIS/name bytes, first four only."""
    if serial_hex is None:
        raise ValueError("BLE hex serial is required")
    cleaned = (
        str(serial_hex)
        .strip()
        .replace(" ", "")
        .replace("-", "")
        .replace(":", "")
    )
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    raw = bytes.fromhex(cleaned)
    if len(raw) < 4:
        raise ValueError(f"BLE hex serial needs >= 4 bytes, got {len(raw)}")
    return raw[:4]


@define(slots=False)
class EmotivEpocX(EmotivBase):
    READ_SIZE: int = field(default=32)
    device_name: str = field(default="Emotiv Epoc X")
    KeyModel: int = field(default=8)  # Epoc X keymodel 8 (extends CyKit numbering)

    is_reverse_engineer_mode: bool = field(default=False)
    # 'hid' = last-4 ASCII CyKit USB material; 'ble_hex' = first 4 hex-decoded bytes.
    serial_source: str = field(default="hid")
    # Android EEG resolution bits (+0xb4): 0x10 → ÷7.8, else ÷1.95.
    eeg_resolution_bits: int = field(default=0x10)

    def __attrs_post_init__(self):
        if self.cipher is None:
            crypto_key = self.get_crypto_key()
            self.cipher = AES.new(crypto_key, AES.MODE_ECB)
        else:
            print("working cipher was provided on startup!")

        if self.is_reverse_engineer_mode:
            print("starting with reverse engineer mode!")
        self.init_EasyTimeSyncParsingMixin()

    @property
    def adc_div(self) -> float:
        return ADC_DIV_16BIT if self.eeg_resolution_bits == 0x10 else ADC_DIV_14BIT

    def get_hid_device(self):
        import hid

        for device in hid.enumerate():
            if (device.get("manufacturer_string", "") == "Emotiv") and (
                device.get("usage", 0) == 2
                or device.get("usage", 0) == 0
                and device.get("interface_number", 0) == 1
            ):
                return device
        raise Exception("Emotiv Epoc X not found")

    def get_crypto_key(self) -> bytearray:
        """AES-128 key via Android EpocXMode schedule (Version2, fw <= 0x73e).

        V4 / findKey is intentionally unsupported.
        """
        if self.serial_number is None:
            serial = self.get_hid_device()["serial_number"]
            self.serial_number = serial
        else:
            # Allow injecting a finished 16-byte key (tests / offline).
            if isinstance(self.serial_number, (bytes, bytearray)) and len(
                self.serial_number
            ) == 16:
                return bytearray(self.serial_number)
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

    def get_lsl_outlet_motion_stream_info(self) -> StreamInfo:
        """LSL stream for Android makeMotion fields (Q + ACC + MAG; no gyro)."""
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

        info = StreamInfo(
            "Epoc X Motion",
            type="SIGNAL",
            channel_count=n_channels,
            nominal_srate=MOTION_SRATE,
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

        for label in ch_names[4:7]:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "g")
            ch.append_child_value("type", "ACC")
            ch.append_child_value("scaling_factor", "8191")

        for label in ch_names[7:]:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "uT")
            ch.append_child_value("type", "MAG")
            ch.append_child_value("scaling_factor", "6")

        info.desc().append_child_value(
            "motion_layout",
            "Android Epoc X 20B BE int16: Q/ACC/MAG after correctMotionDirection",
        )
        info = self.add_lsl_outlet_info_common(info)
        return info

    def get_lsl_outlet_eeg_stream_info(self) -> StreamInfo:
        ch_names = self.eeg_channel_names
        n_channels = len(ch_names)

        info = StreamInfo(
            "Epoc X",
            type="EEG",
            channel_count=n_channels,
            nominal_srate=SRATE,
            channel_format=pylsl.cf_float32,
            source_id=self.get_lsl_source_id(),
        )
        chns = info.desc().append_child("channels")
        for label in ch_names:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "microvolts")
            ch.append_child_value("type", "EEG")
            # Values are already ADC÷adc_div (Android convertADCToMicrovolt).
            ch.append_child_value("scaling_factor", "1")

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
            "Mode-7 EpocPlus LE16 + getRawEegId remap; word7 is CQ",
        )

        info = self.add_lsl_outlet_info_common(info)
        return info

    def get_lsl_outlet_electrode_quality_stream_info(self) -> StreamInfo:
        """CQ stream. Mode-7 exposes a single contact-quality word broadcast to 14 labels."""
        ch_names = self.eeg_quality_channel_names
        n_channels = len(ch_names)

        info = StreamInfo(
            "Epoc X eQuality",
            type="Raw",
            channel_count=n_channels,
            nominal_srate=SRATE,
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

        cap = info.desc().append_child("cap")
        cap.append_child_value("name", "easycap-M1")
        cap.append_child_value("labelscheme", "10-20")
        info.desc().append_child_value(
            "cq_note",
            "Mode-7: CQ = word7 & 0x7f (broadcast to all quality channels)",
        )

        info = self.add_lsl_outlet_info_common(info)
        return info

    def decrypt_eeg_packet(self, data) -> bytes:
        """XOR 0x55 then AES-128-ECB (Android decryptNewPack default / pre-V4)."""
        xored = bytes(el ^ 0x55 for el in data[:32])
        return self.cipher.decrypt(xored)

    def unpack_mode7_words(self, plain: bytes) -> Tuple[int, int, Tuple[int, ...]]:
        """Parse Mode-7 EpocPlus package: counter, type, 15× LE uint16."""
        if len(plain) < 32:
            raise ValueError(f"Mode-7 package needs 32 bytes, got {len(plain)}")
        counter = plain[0]
        type_raw = plain[1]
        words = struct.unpack_from("<15H", plain, 2)
        return counter, type_raw, words

    def decode_eeg_mode7(
        self, plain: bytes
    ) -> Tuple[Optional[List[float]], Optional[List[float]]]:
        """Mode-7 EEG: LE16 words → getRawEegId remap → ADC scale; CQ from word7."""
        counter, type_raw, words = self.unpack_mode7_words(plain)
        pkg_type = type_raw & 0x7F
        if pkg_type != EEG_TYPE:
            if self.enable_debug_logging:
                logging.getLogger("emotiv.epoc_x").debug(
                    f"Skipping non-EEG Mode-7 type={pkg_type:#x} counter={counter}"
                )
            return None, None

        adc_div = self.adc_div
        packet_data = [float(words[idx]) / adc_div for idx in EPOCX_RAW_EEG_ID_MODE7]

        eeg_quality_data = None
        if self.enable_electrode_quality_stream:
            cq = float(words[7] & 0x7F)
            # Broadcast single Mode-7 CQ word (Android makeCQ gate omitted without counter plumbing).
            eeg_quality_data = [cq] * len(self.eeg_channel_names)

        return packet_data, eeg_quality_data

    def decode_data(self, data) -> Tuple[Optional[List], Optional[List]]:
        """Decode one HID report.

        - 32 B: XOR+AES → Mode-7 EEG (type 0x10). CyKit data[1]==32 motion removed.
        - 20 B: motion pass-through (old fw); Android Q/ACC/MAG layout.
        """
        if self.enable_debug_logging and self.is_reverse_engineer_mode:
            logging.debug(f"decode_data(data: {data})")
            logger.warning(f"{data}")

        n = len(data)
        if n == 20:
            if not self.enable_motion_data:
                return None, None
            motion = self.decode_motion_data(data)
            return motion, None

        if n < 32:
            return None, None

        plain = self.decrypt_eeg_packet(data)
        return self.decode_eeg_mode7(plain)

    def decode_motion_data(self, data) -> Optional[List[float]]:
        """Android convert_pkgs_motion + correctMotionDirection + physical scale.

        Expects a 20-byte motion package (pass-through for fw <= 0x73e).
        Returns [Q0, Q1, Q2, Q3, AccX, AccY, AccZ, MagX, MagY, MagZ].
        """
        if data is None or len(data) < 20:
            return None

        raw = [struct.unpack_from(">h", data, 2 + 2 * i)[0] for i in range(9)]

        # correctMotionDirection (type forced 0x20)
        q1 = float(raw[1])
        q2 = float(-raw[0])
        q3 = float(raw[2])
        accx = float(raw[4])
        accy = float(-raw[3])
        accz = float(raw[5])
        magx = float(-raw[7])
        magy = float(raw[6])
        magz = float(raw[8])

        q1s, q2s, q3s = q1 / 16384.0, q2 / 16384.0, q3 / 16384.0
        n2 = min(q1s * q1s + q2s * q2s + q3s * q3s, 1.0)
        q0 = math.sqrt(1.0 - n2)

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
            return n == self.READ_SIZE
        # 32 = encrypted EEG; 20 = motion pass-through when exposed on HID.
        return n in (20, 32)
