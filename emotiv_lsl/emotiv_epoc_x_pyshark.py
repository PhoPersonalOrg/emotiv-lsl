import logging
import pyshark
from Crypto.Cipher import AES
from pylsl import StreamOutlet

from emotiv_lsl.emotiv_epoc_x import EmotivEpocX


class EmotivEpocXPyShark(EmotivEpocX):

    # def __init__(self) -> None:
    def __attrs_post_init__(self):
        self.delimiter = ','
        self.cipher = AES.new(self.get_crypto_key(), AES.MODE_ECB)        
        # self.capture = pyshark.LiveCapture(interface='XHC20', bpf_filter='len == 72')
        
        self.init_EasyTimeSyncParsingMixin()

        self.capture = pyshark.LiveCapture(interface='\\\\?\\HID#VID_1234&PID_ED02&MI_01#a&30a4df18&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}', bpf_filter='len == 72') #  b'\\\\?\\HID#VID_1234&PID_ED02&MI_01#a&30a4df18&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}'
        print(self.capture)

    def validate_data(self, data) -> bool:
        return len(data) == 64

    def main_loop(self):
        outlet = StreamOutlet(self.get_lsl_outlet_eeg_stream_info())
        for packet in self.capture.sniff_continuously():
            if packet.usb.dst != 'host':
                continue

            data = str(packet.layers[1].get_field('usb.capdata'))
            data = data.replace(':', '')

            if self.validate_data(data):
                data = bytearray.fromhex(data)
                decoded, _cq = self.decode_data(data)
                if decoded is not None and len(decoded) == 14:
                    outlet.push_sample(decoded)
        ## END for packet in self.capture.sniff_continuously()...


