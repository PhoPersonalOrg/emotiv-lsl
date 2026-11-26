# Emotiv EPOC X — USB HID Conformance Report (`emotiv-lsl`)

| Field | Value |
|---|---|
| **Date** | 2026-11-25 |
| **Target** | `emotiv-lsl` (Python) |
| **Transport under audit** | Modality 1 — 2.4 GHz USB HID receiver (`0x1234:0xED02` / `0x21A1:0x0001`) |
| **Ground truth** | `OfficialEmotivSoftwareReverseEngineer/WinVMRevEng/` (`NOTES/02`, `04`, `05`; `validate_epocx_spec.py` — **7/7 self-tests PASS**) |
| **Audit procedure** | `WinVMRevEng/PROMPTS/2026-11-25_Revise_Implementations_To_Official_Spec_PROMPT.md` §3.1 / §3.3–§3.4 / §4 |
| **Overall USB verdict** | **NON-CONFORMANT** — usable for **V2 firmware EEG only** on a classic `0x1234` dongle; **drops all USB Motion**, **breaks CQ/Battery**, **no Feature/Output Report `0x00`**, **no V4** |

---

## 1. Scope

This report evaluates USB HID interoperability only (enumeration, Feature/Output Report `0x00`, 32-byte input demux, V2/V4 crypto, Mode-7 EEG/CQ/Battery, Motion). BLE GATT (`81072f40`…) is **out of scope** here except where the same codec bugs affect shared decode paths.

Primary files audited:

| File | Role |
|---|---|
| `emotiv_lsl/hid_device.py` | HID discovery / open |
| `emotiv_lsl/emotiv_epoc_x.py` | Key schedule, decrypt, Mode-7 EEG, Motion |
| `emotiv_lsl/emotiv_base.py` | HID read loop (`READ_SIZE=32`) |
| `config.py` | Hardcoded `SRATE=128`, `MOTION_SRATE=16` |

---

## 2. Checklist Summary (USB §3.1 + shared codec §3.3–§3.4)

| # | Spec requirement | Status | Notes |
|---:|---|---|---|
| U1 | VID/PID + `(usage_page & 0xFF00)==0xFF00` + `input_report_length >= 32` (`MI_01`) | **PARTIAL** | `VID==0x1234` + soft `usage==2` / `iface==1`. Missing `0x21A1:0x0001`, no usage-page mask, no report-length gate. |
| U2 | Dual serial: USB descriptor `"UD…"` **and** Feature Report `0x00` `buf[7:11]` headset hex | **FAIL** | Only USB descriptor serial. Never calls `hid_get_feature_report`. |
| U3 | Parse Feature Report `0x00` → `dongle_fw`, `headset_fw`, rates/bits | **FAIL** | Rates/bits hardcoded (`config.SRATE=128`, `eeg_resolution_bits=0x10` default). |
| U4 | Optional Output Report `00 66 BB 20 12 <CFG>` + `0x88 0x12` ACK | **FAIL** | No mode-switch helper/CLI. |
| U5 | Decrypt **all** 32B USB reports (`is_mems=false`), demux `P[1]&0x7F`: `0x10` EEG / `0x20` Motion | **FAIL** | Decrypts 32B then **drops** non-`0x10` (includes encrypted USB Motion). |
| C1 | V2 key: reverse `"UD…"` ASCII → Mode-7 perm → AES-128 + XOR `0x55` | **PASS** (V2 USB) | `serial_bytes_hid` last-4 reverse ≡ first 4 of fully reversed dongle serial for Mode 7. |
| C2 | V4 (`fw>=0x073F`): SHA256 salts + AES-256 + XOR `0x14` | **FAIL** | Explicitly unsupported (`get_crypto_key` docstring). |
| C3 | Mode-7 EEG word map + ÷7.8 / ÷1.95 | **PASS** | Matches `WORD_MAP` / divisors. |
| C4 | Battery gate `counter == eeg_rate-1`; CQ demux by slot | **FAIL** | Broadcasts `words[7]&0x7F` to all 14 CQ channels every packet. |
| C5 | `"top"` / `"back"` headband Motion transform | **PARTIAL** | Hardcoded `"top"` only. |
| C6 | `marker_hw = (P[1]>>7)&1`; counter modulus = `eeg_rate` | **FAIL** | Marker discarded; no counter continuity / rate modulus. |

**Score (USB-critical items U1–U5 + C1–C6): 2 PASS / 1 PARTIAL / 8 FAIL** (counting U1 and C5 as PARTIAL).

---

## 3. Gap Matrix (file + line)

### Defect 1 — USB 32B Motion drop (**CRITICAL**)

| Spec | After decrypting any 32B HID payload, `pkt_type = P[1]&0x7F`: `0x10` → EEG; `0x20` → Motion `P[0:20]` (12 zero pad). |
|---|---|
| **Code** | `emotiv_epoc_x.py` `decode_eeg_mode7` L276–281 returns `(None,None)` unless `pkg_type == 0x10`; `decode_data` L314–315 never branches `0x20`. Motion only accepted as raw `len==20` (L305–309), which **USB HID never delivers**. |
| **Impact** | All USB Motion (~1/5 of live reports at 128 Hz EEG / 32 Hz MEMS) silently discarded. Live ground-truth USB Motion ciphertext `d33414d7…` never reaches `decode_motion_data`. |

### Defect 2 — BLE 64/84B splitting

| Status | **N/A (USB report)** — no BLE stack in this repo. Shared decode still assumes Motion arrives as bare 20B. |

### Defect 3 — Missing V4 (**CRITICAL** for `fw>=0x073F`)

| Spec | `fw>0x073E` → AES-256-ECB, pre-XOR `0x14`, key `SHA256("Vohcha7e"+S8[:4]+"Ut3phaej"+S8[4:8]+f"{fw:04X}")`. |
|---|---|
| **Code** | `emotiv_epoc_x.py` L102–105: *"V4 / findKey is intentionally unsupported."*; decrypt always XOR `0x55` + AES-128 (`decrypt_eeg_packet` L256–259). |
| **Impact** | Garbage EEG/Motion on modern V4 headsets; V4 USB key must use Feature Report headset serial, not `"UD…"`. |

### Defect 4 — Missing Feature Report `0x00`

| Spec | `HidD_GetFeature` / `hid_get_feature_report` Report ID `0x00` (≥18 B): `headset_fw` BE at `[5:7]`, serial hex `[7:11]`, LE `eeg_rate` at `[13:15]` when `[12]!=0`, etc. |
|---|---|
| **Code** | No `get_feature_report` in streaming path. Serial from enumerate only (`get_crypto_key` L107–109). |
| **Impact** | Cannot discover `headset_fw` (V2 vs V4), paired headset ID for V4, or live sample-rate/bit-depth. |

### Defect 5 — CQ broadcast & Battery corruption (**CRITICAL**)

| Spec | Battery = `words[7]&0x7F` **only** when `counter==eeg_rate-1`. CQ: full 16-bit `words[7]`, `slot=counter%(eeg_rate//2)`, scale/grade per electrode. |
|---|---|
| **Code** | `decode_eeg_mode7` L287–290: `cq = float(words[7]&0x7F)` broadcast to all channels. No battery latch. Stream note at L248–251 documents the wrong behavior. |
| **Impact** | Contact quality unusable; battery never exposed correctly. |

### Defect 6 — Windows HID filter

| Spec | `(VID,PID) ∈ {(0x1234,0xED02),(0x21A1,0x0001)}` and `(usage_page&0xFF00)==0xFF00` and `input_report_length>=32`. Never require `usage_page==0xFF00` strictly (Windows `MI_01` is `0xFFFF`). |
|---|---|
| **Code** | `hid_device.py` L17: `EMOTIV_VENDOR_IDS=(0x1234,)`; L31–38 prefers `usage==2` / `iface==1` without usage-page mask or report length. |
| **Impact** | Misses `0x21A1:0x0001`; may open wrong composite interface if soft fallback hits `MI_00`. Practical OK on many `0x1234` Windows setups where `usage==2` selects `MI_01`. |

### Defect 7 — BLE Active Scan / MTU

| Status | **N/A (USB report)**. |

### Defect 8 — Hardcoded `"top"` IMU orientation

| Spec | `"top"` (default) axis swap/negate; `"back"` identity. |
|---|---|
| **Code** | `decode_motion_data` L328–337 always applies `"top"` transform; no `headband_position` parameter. |

### Defect 9 — Missing USB mode configuration

| Spec | 33-byte Output Report `00 66 BB 20 12 <CFG>`, poll ACK `pkt[2:4]==0x88 0x12`, re-read Feature Report. |
|---|---|
| **Code** | Absent. Rates fixed in `config.py` (`SRATE=128`, `MOTION_SRATE=16` — MEMS rate **16 Hz is also wrong**; hardware is 0/32/64/128). |

### Defect 10 — Missing trigger bit & counter wrap

| Spec | Expose `marker_hw`; EEG counter modulus = `eeg_rate` (128 or 256). |
|---|---|
| **Code** | `type_raw` read but only `&0x7F` used for type filter; bit 7 discarded. No interpolation / counter continuity. |

---

## 4. Confirmed matches (keep)

These already match Cortex / `validate_epocx_spec.py` for **V2 USB EEG**:

1. **Mode-7 V2 key perm** — `EPOCX_MODE_PERM` (`emotiv_epoc_x.py` L17) + `serial_bytes_hid` → live vector key `36373737373637373637363736373736` for `"UD20240731007676"`.
2. **EEG channel remap** — `EPOCX_RAW_EEG_ID_MODE7` L20 == `[2,3,0,1,4,5,6,8,9,10,13,14,11,12]`.
3. **ADC scaling** — ÷7.8 (16-bit) / ÷1.95 (14-bit).
4. **`"top"` Motion math** — BE int16 unpack, axis remap, Q÷16384, Acc÷8191, Mag÷6 (when 20B plaintext is supplied).

Live USB EEG plaintext vector (`cnt=105`, `AF3=4210.7692307692305 µV`) is reachable **if** ciphertext is fed after correct V2 decrypt — the unit path exists; the streaming demux still drops Motion siblings on the same pipe.

---

## 5. Priority fix order (USB)

1. After AES decrypt of every 32B report: branch `0x10` / `0x20` (Defect 1).
2. Implement Feature Report `0x00` parse + drive `headset_fw` / rates / headset serial (Defect 4).
3. Implement V4 key + decrypt path (Defect 3).
4. Fix CQ demux + battery latch (Defect 5).
5. Output Report mode switch + ACK (Defect 9).
6. HID filter `0x21A1` + usage-page mask (Defect 6).
7. `headband_position`, `marker_hw`, correct `MOTION_SRATE` metadata (Defects 8, 10, config).

---

## 6. Oracle cross-check

```text
python OfficialEmotivSoftwareReverseEngineer/WinVMRevEng/validate_epocx_spec.py --self-test
→ ALL VALIDATIONS PASSED (7/7)
```

No `emotiv-lsl` automated test currently asserts USB Feature `0x00`, USB 32B Motion demux, V4, or CQ/Battery against these vectors (`tests/test_epoc_x_decode.py` covers V2 EEG/key paths only).

---

## 7. Verdict

**`emotiv-lsl` is not USB-spec-conformant for EPOC X.** It is a **partial V2 EEG decoder** that:

- Correctly derives the V2 USB AES-128 key from the dongle `"UD…"` serial and scales Mode-7 EEG channels.
- **Fails** USB Motion demux, Feature/Output Report `0x00`, V4 crypto, CQ/Battery, and sample-rate autodetection.

Treat live USB streaming as **EEG-only / V2-only / CQ-invalid** until the priority fixes above land.
