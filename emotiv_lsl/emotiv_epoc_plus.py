import hid
import logging
from Crypto.Cipher import AES
from typing import Dict, List, Tuple, Optional, Callable, Union, Any
from pylsl import StreamInfo
from attrs import define, field, Factory
from emotiv_lsl.emotiv_base import EmotivBase
from config import SRATE


@define(slots=False)
class EmotivEpocPlus(EmotivBase):
    """ 
    from CyKit
    'C:/Users/pho/repos/CyKit/Examples/example_epoc_x_win.py'

    """
    READ_SIZE: int = field(default=32)
    device_name: str = field(default='Emotiv Epoc+')
    KeyModel: int = field(default = 6) # 5 or 6 for Epoc+ according to CyKit

    is_fourteen_bit_mode: bool = field(default=False)


    def __attrs_post_init__(self):
        ## immediately calls the self.get_crypto_key() function to try and set self.cypher
        self.cipher = AES.new(self.get_crypto_key(), AES.MODE_ECB)
        self.init_EasyTimeSyncParsingMixin()


    def get_hid_device(self):
        from emotiv_lsl.hid_device import find_emotiv_hid_device

        return find_emotiv_hid_device()

    def get_crypto_key(self) -> bytearray:
        if self.serial_number is None:
            serial = self.get_hid_device()['serial_number']
            self.serial_number = serial
        elif isinstance(self.serial_number, (bytes, bytearray)) and len(self.serial_number) == 16:
            return bytearray(self.serial_number)
        else:
            serial = str(self.serial_number)

        sn = bytearray()
        for i in range(0, len(serial)):
            sn += bytearray([ord(serial[i])])
        ## END for i in range(0, len(serial))...

        if not self.is_fourteen_bit_mode:
            # EPOC+ 16-bit USB HID key schedule.
            return bytearray([sn[-1], sn[-2], sn[-2], sn[-3], sn[-3], sn[-3], sn[-2], sn[-4], sn[-1], sn[-4], sn[-2], sn[-2], sn[-4], sn[-4], sn[-2], sn[-1]])

        # EPOC+ 14-bit USB HID key schedule.
        return bytearray([sn[-1], 0x00, sn[-2], 21, sn[-3], 0x00, sn[-4], 12, sn[-3], 0x00, sn[-2], 68, sn[-1], 0x00, sn[-2], 88])

    def get_lsl_outlet_eeg_stream_info(self) -> StreamInfo:
        ch_names = ['AF3', 'F7', 'F3', 'FC5', 'T7', 'P7', 'O1', 'O2', 'P8', 'T8', 'FC6', 'F4', 'F8', 'AF4']
        n_channels = len(ch_names)

        info = StreamInfo('Epoc+', 'EEG', n_channels, SRATE, 'float32')
        chns = info.desc().append_child("channels")
        for label in ch_names:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            ch.append_child_value("unit", "microvolts")
            ch.append_child_value("type", "EEG")
            ch.append_child_value("scaling_factor", "1")

        cap = info.desc().append_child("cap")
        cap.append_child_value("name", "easycap-M1")
        cap.append_child_value("labelscheme", "10-20")

        return info

    def decode_data(self, data) -> Tuple[List, Optional[List]]:
        """ 
        From `CyKit/Examples/example_epoc_plus.py`
            join_data = ''.join(map(chr, data[1:]))
            data = self.cipher.decrypt(bytes(join_data,'latin-1')[0:32])
            if str(data[1]) == "32": # No Gyro Data.
                return

        """
        ## Epoc X
        # data = [el ^ 0x55 for el in data]
        # data = self.cipher.decrypt(bytearray(data))
        
        ## Epoc+
        join_data = ''.join(map(chr, data[1:]))
        data = self.cipher.decrypt(bytes(join_data,'latin-1')[0:32])
        if str(data[1]) == "32": # No Gyro Data.
            logging.getLogger('emotiv.epoc_plus').debug(f"Motion/gyro packet detected: data[1]={data[1]}. WARN: NOT YET IMPLEMENTED FOR EPOC+")
            return None, None
        
        ## Check for quality values
        eeg_quality_data = None
        if self.enable_electrode_quality_stream:
            try:
                eeg_quality_data = self.extractQualityValues(data=data, return_as_array=True)

            except Exception as e:
                print(f'getting EEG sensor quality values failed with error: {e}')
                eeg_quality_data = None
                pass
                # raise e
                
        packet_data = ""
        for i in range(2, 16, 2):
            packet_data = packet_data + \
                str(self.convertEPOC_PLUS(
                    str(data[i]), str(data[i+1]))) + self.delimiter

        for i in range(18, len(data), 2):
            packet_data = packet_data + \
                str(self.convertEPOC_PLUS(
                    str(data[i]), str(data[i+1]))) + self.delimiter

        packet_data = packet_data[:-len(self.delimiter)]
        packet_data = packet_data.split(self.delimiter)
        packet_data = [float(i) for i in packet_data]

        # swap positions of AF3 and F3
        packet_data[0], packet_data[2] = packet_data[2], packet_data[0]

        # swap positions of AF4 and F4
        packet_data[13], packet_data[11] = packet_data[11], packet_data[13]

        # swap positions of F7 and FC5
        packet_data[1], packet_data[3] = packet_data[3], packet_data[1]

        # swap positions of FC6 and F8
        packet_data[10], packet_data[12] = packet_data[12], packet_data[10]

        return packet_data, eeg_quality_data


    def convertEPOC_PLUS(self, value_1, value_2):
        edk_value = "%.8f" % (((int(value_1) * .128205128205129) +
                              4201.02564096001) + ((int(value_2) - 128) * 32.82051289))
        return edk_value

    def validate_data(self, data) -> bool:
        return len(data) == 32
