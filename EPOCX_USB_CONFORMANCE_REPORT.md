# Emotiv EPOC X — USB HID Conformance Report (`emotiv-lsl`)

| Field | Value |
|---|---|
| **Date** | 2026-11-27 |
| **Target** | `emotiv-lsl` (Python) |
| **Transport under audit** | Modality 1 — 2.4 GHz USB HID receiver (`0x1234:0xED02` / `0x21A1:0x0001`) |
| **Ground truth** | `OfficialEmotivSoftwareReverseEngineer/WinVMRevEng/` (`NOTES/02`, `04`, `05`; `validate_epocx_spec.py` — **7/7 self-tests PASS**) |
| **Audit procedure** | `WinVMRevEng/PROMPTS/2026-11-25_Revise_Implementations_To_Official_Spec_PROMPT.md` §3.1 / §3.3–§3.4 / §4 |
| **Overall USB verdict** | **CONFORMANT (11 / 11 PASS)** — full 2.4 GHz USB HID Feature/Output Report `0x00`, 32B EEG + Motion demux, V2 + V4 crypto, per-slot CQ, battery latch, `marker_hw`, `"top"`/`"back"` IMU, and ground-truth vector parity |

---

## 1. Scope

This report evaluates 2.4 GHz USB HID interoperability (`hid_device.py`, `emotiv_epoc_x.py`, `emotiv_base.py`, `epocx_wire.py`, `config.py`, `main.py`) plus isolated offline Flutter BLE CSV/replay compatibility (`flutter_raw_csv.py`).

---

## 2. Checklist Summary (USB §3.1 + shared codec §3.3–§3.4)

| # | Spec requirement | Status | Implementation |
|---:|---|---|---|
| U1 | VID/PID `(0x1234,0xED02)` & `(0x21A1,0x0001)` + `(usage_page & 0xFF00)==0xFF00` + `MI_00` rejection | **PASS** | `emotiv_lsl/hid_device.py` (`EMOTIV_RECEIVER_VID_PIDS`, `_is_status_only_interface`, `_is_preferred_eeg_interface`) |
| U2 | Dual serial: USB descriptor `"UD..."` **and** Feature Report `0x00` `buf[7:11]` headset hex | **PASS** | `EmotivEpocX.serial_number` + `EmotivEpocX.headset_serial` via `read_usb_feature_report_00` |
| U3 | Parse Feature Report `0x00` -> `dongle_fw`, `headset_fw`, LE `eeg_rate`, `eeg_bits`, `mems_rate`, `mems_bits` | **PASS** | `parse_usb_feature_report_00` + `EmotivEpocX.apply_usb_feature_config` (`config.MOTION_SRATE=32`) |
| U4 | Optional 33B Output Report `00 66 BB 20 12 <CFG>` + `0x88 0x12` ACK + Feature Report re-read | **PASS** | `build_epocx_usb_mode_report`, `is_usb_mode_ack`, `configure_epocx_usb_mode`, CLI `--eeg-rate`/`--eeg-bits`/`--mems-rate` |
| U5 | Decrypt **all** 32B USB reports (`is_mems=False`), demux `plain[1]&0x7F`: `0x10` EEG / `0x20` Motion `plain[0:20]` | **PASS** | `EmotivEpocX.decode_data` (also strips leading `0x00` Report ID on 33B OS HID reads) |
| C1 | V2 key (`fw<=0x073E`): reverse `"UD..."` ASCII -> Mode-7 perm -> AES-128 + XOR `0x55` | **PASS** | `serial_bytes_hid` + `aes_key_epocx_mode` + `decrypt_packet` |
| C2 | V4 key (`fw>=0x073F`): SHA-256(`"Vohcha7e"+S8[:4]+"Ut3phaej"+S8[4:8]+f"{fw:04X}"`) + AES-256 + XOR `0x14` | **PASS** | `derive_v4_key` + `EmotivEpocX.decrypt_packet` (32B USB + 20B V4 offline BLE tail) |
| C3 | Mode-7 EEG word map `[2,3,0,1,4,5,6,8,9,10,13,14,11,12]` + `÷7.8` / `÷1.95` | **PASS** | `EmotivEpocX.decode_eeg_mode7` |
| C4 | Battery gate `counter == eeg_rate - 1` (`words[7] != 0`); per-slot 16-bit continuous CQ demux (`cq_norm_to_score` + `cq_score_to_word7`) | **PASS** | `EmotivEpocX.decode_eeg_mode7` + `update_cq_from_word7` + `epocx_wire.cq_score_to_word7` + `legacy_xdf_cq.py` |
| C5 | `"top"` (default) and `"back"` (identity) headband Motion transform | **PASS** | `EmotivEpocX.decode_motion_data` + CLI `--headband-position {top,back}` |
| C6 | `marker_hw = (plain[1] >> 7) & 1`; EEG counter wrap `mod eeg_rate`, MEMS wrap `mod 128` | **PASS** | `EmotivEpocX.last_marker_hw`, `eeg_counter_drops`, `mems_counter_drops` |

---

## 3. Automated Ground-Truth Verification

- `python -m tests.test_epoc_x_decode`: verifies live USB Feature Report `0x00`, Output Report `0x00` + `0x88 0x12` ACK, `MI_00` vs `MI_01` HID filtering, live `UD20240731007676` + `EPOCX-E50202E9` 32B EEG & 32B Motion ciphertexts (`<= 1e-12` parity), V4 (`fw=0x0752`) SHA-256 key equivalence & AES-256 round-trips, battery latch, `marker_hw`, `"back"` orientation, and `EmotivEpocPlus` key schedules.
- `python -m tests.test_epocx_wire`: verifies 32B encrypted USB motion wire synthesis, per-slot CQ grade round-trip across all 14 slots, and V4 `.epocxwire` metadata persistence.
- `python -m tests.test_flutter_raw_csv`: verifies isolated offline 84-byte BLE `81072f41` super-frame splitting (`2x32B` EEG + `20B` Motion tail).
- `python OfficialEmotivSoftwareReverseEngineer/WinVMRevEng/validate_epocx_spec.py --self-test`: **7/7 PASS**.
