from typing import Dict, List, Tuple, Optional, Callable, Union, Any
from datetime import datetime, timedelta
# import hid
import logging
import time
from Crypto.Cipher import AES
import numpy as np
import pylsl
from pylsl import StreamInfo, StreamOutlet
from attrs import define, field, Factory
from phopylslhelper.easy_time_sync import EasyTimeSyncParsingMixin, readable_dt_str, from_readable_dt_str


@define(slots=False)
class EmotivBase(EasyTimeSyncParsingMixin):
    READ_SIZE: int = field(default=32)
    serial_number: str = field(default=None)
    device_name: str = field(default='UnknownEmotivHeadset')
    delimiter: str = field(default=',')
    cipher: Any = field(default=None)
    KeyModel: int = field(default = 1)
    
    has_motion_data: bool = field(default=False)
    enable_debug_logging: bool = field(default=False)
    is_reverse_engineer_mode: bool = field(default=False)
    enable_electrode_quality_stream: bool = field(default=False)
    enable_motion_data: bool = field(default=False)
    # Optional WireReplaySource; when set, main_loop skips HID and paces packets.
    replay_source: Any = field(default=None)
    replay_speed: float = field(default=1.0)
    # Optional WireSessionRecorder (or path str/Path); logs every HID read before decode.
    wire_recorder: Any = field(default=None)
    record_path: Any = field(default=None)
    # Called with True/False on connected/disconnected transitions after first EEG success
    on_connection_change: Optional[Callable[[bool], None]] = field(default=None)
    # Milliseconds for timed HID reads; keeps the loop responsive for disconnect checks
    hid_read_timeout_ms: int = field(default=500)
    # Seconds without valid EEG after first connect before reporting disconnected
    disconnect_timeout_sec: float = field(default=3.0)

    # def __attrs_post_init__(self):
    #     self.cipher = Cipher(self.serial_number)
    

    @property
    def eeg_channel_names(self) -> List[str]:
        """The eeg_channel_names property."""
        return ['AF3', 'F7', 'F3', 'FC5', 'T7', 'P7', 'O1', 'O2', 'P8', 'T8', 'FC6', 'F4', 'F8', 'AF4']

    @property
    def eeg_quality_channel_names(self) -> List[str]:
        """The eeg_quality_channel_names property."""
        return [f'q{a_name}' for a_name in self.eeg_channel_names] ## add the 'q' prefix, like ['qAF3', 'qF7', ...]
        

    @classmethod
    def init_with_serial(cls, serial_number: str, cryptokey: Optional[bytearray]=None, **kwargs):
        """ doesn't require `hid` or USB access, makes the object with an explicit key """
        # bytearray(b'6566565666756557')        
        if cryptokey is not None:
            cipher = AES.new(cryptokey, AES.MODE_ECB)
            _obj = cls(cipher=cipher, **kwargs) # , cryptokey=cryptokey
        else:
            _obj = cls(serial_number=serial_number, **kwargs) # , cryptokey=cryptokey
        return _obj
    

    def __attrs_post_init__(self):
        self.init_EasyTimeSyncParsingMixin()
        

    def get_crypto_key(self) -> bytearray:
        raise NotImplementedError('get_crypto_key method must be implemented in subclass')


    def get_lsl_source_id(self) -> str:
        return f"{self.device_name}_{self.KeyModel}_{self.get_crypto_key()}"


    def get_hid_device(self):
        from emotiv_lsl.hid_device import find_emotiv_hid_device

        return find_emotiv_hid_device()


    def on_hid_device_opened(self, hid_device: Any, device: Dict[str, Any]) -> None:
        """Subclass hook invoked immediately after opening a live USB HID handle."""
        pass
    


    def add_lsl_outlet_info_common(self, info: StreamInfo) -> StreamInfo:
        """ adds common LSL metadata
        """
        # Add some metadata
        info.desc().append_child_value("manufacturer", "emotiv_lsl")
        info.desc().append_child_value("version", "0.1.4")
        info.desc().append_child_value("description", "Logged by the open-source tool 'emotiv_lsl' to record raw data from Emotiv headsets.")
        ## add a custom timestamp field to the stream info:
        info = self.EasyTimeSyncParsingMixin_add_lsl_outlet_info(info=info)
        return info
    


    def get_lsl_outlet_eeg_stream_info(self) -> StreamInfo:
        """Create LSL stream for EEG sensor data"""
        info = self.add_lsl_outlet_info_common(info=info)
        return info

    def get_lsl_outlet_motion_stream_info(self) -> StreamInfo:
        """Create LSL stream info for motion sensor data (accelerometer + gyroscope)"""
        info = self.add_lsl_outlet_info_common(info=info)
        return info
    

    def get_lsl_outlet_raw_debugging_stream_info(self) -> StreamInfo:
        """ 
        raw_packet_outlet = None
        if self.is_reverse_engineer_mode:
            raw_packet_outlet = StreamOutlet(self.get_lsl_outlet_raw_debugging_stream_info())
            print(f'Setup raw_packet_outlet (for reverse-engineering)')
            
        """
        packet_size = 32  # bytes
        dtype = 'int8'
        info = StreamInfo('Epoc X DebugRaw', type="Raw", channel_count=packet_size, nominal_srate=0, channel_format=dtype, source_id="debug_raw_001")
        info = self.add_lsl_outlet_info_common(info=info)
        return info
    

    def get_lsl_outlet_electrode_quality_stream_info(self) -> StreamInfo:
        """ Create LSL stream for EEG sensor quality data. Only active if `self.enable_electrode_quality_stream` is True """
        info = self.add_lsl_outlet_info_common(info=info)
        pass
    
        

    def decode_data(self) -> Tuple[List, Optional[List]]:
        raise NotImplementedError(f'Specific hardware class (e.g. Epoc X) must override this to provide a concrete implementation.')
        pass

    def validate_data(self, data) -> bool:
        raise NotImplementedError(f'Specific hardware class (e.g. Epoc X) must override this to provide a concrete implementation.')
        pass


    ## CyKit Conversion/Decoding/Data Packet Parsing Functions
    def convertEPOC_PLUS(self, value_1, value_2):
        # Legacy CyKit string math ≈ little-endian uint16 / 7.8 (Android 16-bit ADC).
        # Epoc X Mode-7 hot path uses explicit LE16 + /7.8 instead; kept for Epoc+.
        edk_value = "%.8f" % (((int(value_1) * .128205128205129) + 4201.02564096001) + ((int(value_2) - 128) * 32.82051289))
        return edk_value

    def extractQualityValues(self, data, return_as_array: bool=True) -> Union[np.ndarray, Dict[str, float], None]:
        """Contact quality from a decrypted package.

        KeyModel 8 (Epoc X Mode-7): per-electrode CQ demux from 16-bit word7 + counter when type==0x10.
        Older models: CyKit nibble scrape of bytes 16–22.
        """
        import struct

        quality_values_dict = {}

        if self.KeyModel == 2 or self.KeyModel == 1:  # Epoc
            quality_values_dict = {'AF3': data[16] & 0xF, 'F7': (data[16] >> 4) & 0xF,
                            'F3': data[17] & 0xF, 'FC5': (data[17] >> 4) & 0xF,
                            'T7': data[18] & 0xF, 'P7': (data[18] >> 4) & 0xF,
                            'O1': data[19] & 0xF, 'O2': (data[19] >> 4) & 0xF,
                            'P8': data[20] & 0xF, 'T8': (data[20] >> 4) & 0xF,
                            'FC6': data[21] & 0xF, 'F4': (data[21] >> 4) & 0xF,
                            'F8': data[22] & 0xF, 'AF4': (data[22] >> 4) & 0xF}
        elif self.KeyModel == 8:  # Epoc X Mode-7 — Cortex per-electrode CQ demux
            if data is None or len(data) < 32:
                return None
            if (data[1] & 0x7F) != 0x10:
                return None
            counter = data[0]
            words = struct.unpack_from("<15H", bytes(data), 2)
            if hasattr(self, "update_cq_from_word7"):
                scores = self.update_cq_from_word7(counter, words[7])
                quality_values_dict = dict(zip(self.eeg_channel_names, scores))
            else:
                cq = float(words[7] & 0x7F)
                quality_values_dict = {name: cq for name in self.eeg_channel_names}
        elif (self.KeyModel == 6) or (self.KeyModel == 5):  # Epoc+
            quality_values_dict = {'AF3': data[16] & 0xF, 'F7': (data[16] >> 4) & 0xF,
                            'F3': data[17] & 0xF, 'FC5': (data[17] >> 4) & 0xF,
                            'T7': data[18] & 0xF, 'P7': (data[18] >> 4) & 0xF,
                            'O1': data[19] & 0xF, 'O2': (data[19] >> 4) & 0xF,
                            'P8': data[20] & 0xF, 'T8': (data[20] >> 4) & 0xF,
                            'FC6': data[21] & 0xF, 'F4': (data[21] >> 4) & 0xF,
                            'F8': data[22] & 0xF, 'AF4': (data[22] >> 4) & 0xF}
        else:
            raise NotImplementedError(self.KeyModel)

        if return_as_array and (quality_values_dict is not None):
            return np.array([quality_values_dict[k] for k in self.eeg_channel_names])
        return quality_values_dict
        


    def _notify_connection_change(self, connected: bool) -> None:
        if self.on_connection_change is None:
            return
        try:
            self.on_connection_change(connected)
        except Exception:
            logging.getLogger(f'emotiv.{self.device_name.replace(" ", "_").lower()}').exception(
                "on_connection_change callback failed"
            )

    def main_loop(self):
        # Outlets are created lazily once live HID / replay metadata is finalized.
        eeg_outlet = None
        motion_outlet = None
        raw_packet_outlet = None
        if self.is_reverse_engineer_mode:
            raw_packet_outlet = StreamOutlet(self.get_lsl_outlet_raw_debugging_stream_info())
            print(f'Setup raw_packet_outlet (for reverse-engineering)')
            
        eeg_quality_outlet = None
        logger = logging.getLogger(f'emotiv.{self.device_name.replace(" ", "_").lower()}')

        hid_device = None
        recorder = self.wire_recorder
        if recorder is None and self.record_path is not None:
            from emotiv_lsl.epocx_wire import WireSessionRecorder, header_from_hid_device

            # Header filled after HID open (or from replay serial).
            recorder = None  # created below once device info is known
            _pending_record_path = self.record_path
        else:
            _pending_record_path = None

        if self.replay_source is not None:
            print(
                f"Replay mode: serial={getattr(self.replay_source.header, 'serial', '?')} "
                f"speed={self.replay_speed}"
            )
            if _pending_record_path is not None:
                from emotiv_lsl.epocx_wire import WireSessionRecorder

                hdr = self.replay_source.header
                recorder = WireSessionRecorder(_pending_record_path, hdr)
                recorder.open()
                print(f"Recording replay→wire to {_pending_record_path}")
        else:
            import hid
            from emotiv_lsl.epocx_wire import WireSessionRecorder, header_from_hid_device
            from emotiv_lsl.hid_device import open_emotiv_hid_device

            ## Get the device info
            device = self.get_hid_device()
            hid_device = open_emotiv_hid_device(device)
            self.on_hid_device_opened(hid_device, device)
            if self.is_reverse_engineer_mode:
                logger.debug(f'hid_device: {hid_device}\n\twith path: {device["path"]}\n')

            if recorder is None and _pending_record_path is not None:
                from emotiv_lsl.epocx_wire import try_get_report_descriptor_hex

                desc_hex = try_get_report_descriptor_hex(hid_device)
                hdr = header_from_hid_device(
                    device,
                    serial_source=getattr(self, "serial_source", "hid") or "hid",
                    adc_div=float(getattr(self, "adc_div", 7.8)),
                    eeg_hz=float(getattr(self, "eeg_rate", 128)),
                    mems_hz=float(getattr(self, "mems_rate", 32)),
                    headset_serial=getattr(self, "headset_serial", None),
                    headset_fw=getattr(self, "headset_fw", None),
                    dongle_fw=getattr(self, "dongle_fw", None),
                    eeg_bits=16 if float(getattr(self, "adc_div", 7.8)) > 4.0 else 14,
                    mems_bits=getattr(self, "mems_bits", None),
                    read_size=self.READ_SIZE,
                    read_timeout_ms=self.hid_read_timeout_ms,
                    report_descriptor=desc_hex,
                )
                if self.serial_number:
                    hdr.serial = str(self.serial_number)
                elif device.get("serial_number"):
                    sn = device["serial_number"]
                    hdr.serial = sn.decode("utf-8", errors="replace") if isinstance(sn, bytes) else str(sn)
                recorder = WireSessionRecorder(_pending_record_path, hdr)
                recorder.open()
                print(f"Recording HID session to {_pending_record_path}")
                if desc_hex:
                    print(f"  report_descriptor: {len(desc_hex)//2} bytes captured")

            elif recorder is not None and getattr(recorder, "_f", None) is None:
                recorder.open()

        if self.has_motion_data and self.enable_motion_data and motion_outlet is None:
            motion_outlet = StreamOutlet(self.get_lsl_outlet_motion_stream_info())
            print(f'Setup motion outlet')

        self.wire_recorder = recorder

        packet_count = 0
        ever_connected = False
        is_connected = False
        last_eeg_time: Optional[float] = None

        def _mark_eeg_received() -> None:
            nonlocal ever_connected, is_connected, last_eeg_time
            last_eeg_time = time.monotonic()
            if not is_connected:
                was_ever = ever_connected
                ever_connected = True
                is_connected = True
                logger.info("Headset EEG connection established" if not was_ever else "Headset EEG connection restored")
                self._notify_connection_change(True)

        def _mark_disconnected(reason: str) -> None:
            nonlocal is_connected
            if ever_connected and is_connected:
                is_connected = False
                logger.warning(f"Headset EEG connection lost ({reason})")
                self._notify_connection_change(False)

        def _check_disconnect_timeout() -> None:
            if (
                ever_connected
                and last_eeg_time is not None
                and (time.monotonic() - last_eeg_time) >= self.disconnect_timeout_sec
            ):
                _mark_disconnected(f"no EEG for {self.disconnect_timeout_sec:.1f}s")

        def _record_raw(data) -> None:
            if recorder is None:
                return
            if not data:
                recorder.record_empty()
            else:
                payload = bytes(data) if not isinstance(data, (bytes, bytearray)) else bytes(data)
                recorder.record_report(payload)

        # hidapi exposes read(max_length, timeout_ms=0); fall back to positional for older builds
        use_timeout_ms_kwarg = True
        try:
            while True:
                try:
                    if self.replay_source is not None:
                        data = self.replay_source.read(self.READ_SIZE)
                        if not data and getattr(self.replay_source, "eof", True):
                            print(f"Replay finished after {packet_count} packet(s)")
                            _mark_disconnected("replay finished")
                            break
                    elif use_timeout_ms_kwarg:
                        data = hid_device.read(self.READ_SIZE, timeout_ms=self.hid_read_timeout_ms)
                    else:
                        data = hid_device.read(self.READ_SIZE, self.hid_read_timeout_ms)
                except TypeError:
                    # Older hid bindings reject timeout_ms=; switch to positional timeout.
                    use_timeout_ms_kwarg = False
                    data = hid_device.read(self.READ_SIZE, self.hid_read_timeout_ms)
                except Exception as e:
                    if recorder is not None:
                        try:
                            recorder.record_error(str(e))
                        except Exception:
                            pass
                    _mark_disconnected(f"HID read error: {e}")
                    raise

                # Capture every read outcome before validate/decode (including empty gaps).
                _record_raw(data)

                if not data:
                    _check_disconnect_timeout()
                    continue

                packet_count += 1
                
                if (self.is_reverse_engineer_mode and (raw_packet_outlet is not None)):
                    ## output the raw data
                    raw_packet_outlet.push_sample(data)

                if self.validate_data(data):
                    if self.enable_debug_logging:
                        logger.debug(f"Packet #{packet_count}: Valid data packet, length={len(data)}")

                    decoded, eeg_quality_data = self.decode_data(data)
                    
                    if (eeg_quality_data is not None) and len(eeg_quality_data) == 14:
                        if self.is_reverse_engineer_mode:
                            logger.debug(f'got eeg quality data: {eeg_quality_data}')
                        if eeg_quality_outlet is None:
                            eeg_quality_outlet = StreamOutlet(self.get_lsl_outlet_electrode_quality_stream_info())
                            logger.debug(f'set up EEG Sensor Quality outlet!')
                        eeg_quality_outlet.push_sample(eeg_quality_data)
                            
                    # else:
                    # decoded = self.decode_data(data)
                        
                    if decoded is not None:
                        # Motion: Android Q0–Q3 + ACC + MAG (10 ch). Legacy CyKit 6-ch gyro removed.
                        if len(decoded) == 10:
                            if self.enable_motion_data:
                                if self.enable_debug_logging:
                                    logger.debug(f"Packet #{packet_count}: Motion data decoded, {len(decoded)} channels")
                                if not self.has_motion_data:
                                    self.has_motion_data = True
                                    logger.debug(f'got first motion data!')

                                if motion_outlet is None:
                                    motion_outlet = StreamOutlet(self.get_lsl_outlet_motion_stream_info())
                                    logger.debug(f'set up motion outlet!')
                                motion_outlet.push_sample(decoded)
                            elif self.enable_debug_logging:
                                logger.debug(
                                    f"Packet #{packet_count}: Motion packet skipped "
                                    f"(enable_motion_data=False)"
                                )

                        elif len(decoded) == 14:  # EEG data has 14 channels
                            if self.enable_debug_logging:
                                logger.debug(f"Packet #{packet_count}: EEG data decoded, {len(decoded)} channels")
                            if eeg_outlet is None:
                                eeg_outlet = StreamOutlet(self.get_lsl_outlet_eeg_stream_info())
                                logger.debug(f'set up EEG outlet!')                                                        
                            eeg_outlet.push_sample(decoded)
                            _mark_eeg_received()
                        else:
                            logger.debug(f"Packet #{packet_count}: Unknown data type with {len(decoded)} channels")
                    elif self.enable_debug_logging:
                        # Common on Epoc X: Mode-7 type 0x20 motion while motion LSL is off,
                        # or other non-EEG types. Not a decrypt failure.
                        logger.debug(
                            f"Packet #{packet_count}: non-EEG HID report skipped "
                            f"(len={len(data)})"
                        )
                else:
                    logger.debug(f"Packet #{packet_count}: Invalid data packet, length={len(data)}")

                _check_disconnect_timeout()
        except KeyboardInterrupt:
            _mark_disconnected("interrupted")
            if recorder is not None:
                print(
                    f"\nStopped recording after {packet_count} packet(s); "
                    f"saved {recorder.path}"
                )
            else:
                print(f"\nStopped after {packet_count} packet(s)")
        except Exception as e:
            if recorder is not None:
                try:
                    recorder.record_error(f"loop error: {e}")
                except Exception:
                    pass
            _mark_disconnected(f"loop error: {e}")
            raise
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass
            if hid_device is not None:
                try:
                    hid_device.close()
                except Exception:
                    pass
