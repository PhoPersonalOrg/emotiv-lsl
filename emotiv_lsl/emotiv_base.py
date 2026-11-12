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
        # raise NotImplementedError(f'Specific hardware class (e.g. Epoc X) must override this to provide a concrete implementation.')
        import hid
        for device in hid.enumerate():
            if device.get('manufacturer_string', '') == 'Emotiv' and ((device.get('usage', 0) == 2 or device.get('usage', 0) == 0 and device.get('interface_number', 0) == 1)):
                return device
        raise Exception('Emotiv Epoc Base Headset not found')
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
        edk_value = "%.8f" % (((int(value_1) * .128205128205129) + 4201.02564096001) + ((int(value_2) - 128) * 32.82051289))
        return edk_value
    
    # In the EEG class, add a method to extract quality values
    def extractQualityValues(self, data, return_as_array: bool=True) -> Union[np.ndarray, Dict[str, float]]:
        # Quality values are typically in specific bytes of the data packet
        # For EPOC/EPOC+, quality values are often in data[16] and data[17]
        quality_values_dict = {}

        # Different models store quality data differently
        if self.KeyModel == 2 or self.KeyModel == 1:  # Epoc
            # Extract quality values for each channel
            # This is a simplified example - actual implementation depends on the device's data format
            quality_values_dict = {'AF3': data[16] & 0xF, 'F7': (data[16] >> 4) & 0xF, 
                            'F3': data[17] & 0xF, 'FC5': (data[17] >> 4) & 0xF,
                            'T7': data[18] & 0xF, 'P7': (data[18] >> 4) & 0xF,
                            'O1': data[19] & 0xF, 'O2': (data[19] >> 4) & 0xF,
                            'P8': data[20] & 0xF, 'T8': (data[20] >> 4) & 0xF,
                            'FC6': data[21] & 0xF, 'F4': (data[21] >> 4) & 0xF,
                            'F8': data[22] & 0xF, 'AF4': (data[22] >> 4) & 0xF}
        elif (self.KeyModel == 6) or (self.KeyModel == 5) or (self.KeyModel == 8):  # Epoc+ or EpocX
            # Similar extraction for EPOC+
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
            return np.array([quality_values_dict[k] for k in self.eeg_channel_names]) ## ensures consistent channel order
        else:
            # returnt he dict
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
        import hid

        # Create EEG outlet
        eeg_outlet = None 

        # Create motion outlet if the device supports it
        motion_outlet = None
        if self.has_motion_data and self.enable_motion_data:
            motion_outlet = StreamOutlet(self.get_lsl_outlet_motion_stream_info())
            print(f'Setup motion outlet')
            
        # Create motion outlet if the device supports it
        raw_packet_outlet = None
        if self.is_reverse_engineer_mode:
            raw_packet_outlet = StreamOutlet(self.get_lsl_outlet_raw_debugging_stream_info())
            print(f'Setup raw_packet_outlet (for reverse-engineering)')
            
        eeg_quality_outlet = None
        
        ## Get the device info
        device = self.get_hid_device()
        # ctypes pyhidapi: hid.Device(path=...) opens in the constructor.
        # Cython hidapi: hid.device() then open_path(path).
        if hasattr(hid, "Device"):
            hid_device = hid.Device(path=device['path'])
        else:
            hid_device = hid.device()
            hid_device.open_path(device['path'])
        logger = logging.getLogger(f'emotiv.{self.device_name.replace(" ", "_").lower()}')
        
        if self.is_reverse_engineer_mode:
            logger.debug(f'hid_device: {hid_device}\n\twith path: {device["path"]}\n')
        
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

        # hidapi exposes read(max_length, timeout_ms=0); fall back to positional for older builds
        use_timeout_ms_kwarg = True
        try:
            while True:
                try:
                    if use_timeout_ms_kwarg:
                        data = hid_device.read(self.READ_SIZE, timeout_ms=self.hid_read_timeout_ms)
                    else:
                        data = hid_device.read(self.READ_SIZE, self.hid_read_timeout_ms)
                except TypeError:
                    use_timeout_ms_kwarg = False
                    data = hid_device.read(self.READ_SIZE, self.hid_read_timeout_ms)
                except Exception as e:
                    _mark_disconnected(f"HID read error: {e}")
                    raise

                if not data:
                    if (
                        ever_connected
                        and last_eeg_time is not None
                        and (time.monotonic() - last_eeg_time) >= self.disconnect_timeout_sec
                    ):
                        _mark_disconnected(f"no EEG for {self.disconnect_timeout_sec:.1f}s")
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
                        # Check if this is motion data (based on number of channels)
                        if len(decoded) == 6:
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
                                logger.debug(f"Packet #{packet_count}: Motion data decoded but disabled (enable_motion_data=False)")

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
                    else:
                        logger.debug(f"Packet #{packet_count}: self.decode_data(data) failed -- data packet (skipped)")
                else:
                    logger.debug(f"Packet #{packet_count}: Invalid data packet, length={len(data)}")

                if (
                    ever_connected
                    and last_eeg_time is not None
                    and (time.monotonic() - last_eeg_time) >= self.disconnect_timeout_sec
                ):
                    _mark_disconnected(f"no EEG for {self.disconnect_timeout_sec:.1f}s")
        except Exception as e:
            _mark_disconnected(f"loop error: {e}")
            raise
        finally:
            try:
                hid_device.close()
            except Exception:
                pass