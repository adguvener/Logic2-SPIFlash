from saleae.analyzers import HighLevelAnalyzer, AnalyzerFrame, StringSetting, NumberSetting, ChoicesSetting

import struct

# Value is dummy clocks
CONTINUE_COMMANDS = {
    0x6b: 8,
    0xe7: 2,
    0xeb: 4,
}

DATA_COMMANDS = {
    0x03: "Read",
    0x0b: "Fast Read",
    0x5b: "Read SFDP",
    0x6b: "Quad-Output Fast Read",
    0x9e: "Read JEDEC ID",
    0x9f: "Read JEDEC ID",
    0xe7: "Quad Word Read",
    0xeb: "Quad Read",
    0x02: "Page Program",
    0x32: "Quad Page Program",
    # IS25LP128F Specific Commands
    0x12: "4-byte PAGE PROGRAM",
    0x13: "4-byte READ",
    0x34: "4-byte QUAD INPUT PAGE PROGRAM",
    0x5C: "4-byte BLOCK ERASE 32KB",
    0xD8: "4-byte BLOCK ERASE 64KB",
    0x20: "4-byte SECTOR ERASE 4KB",
    0xE1: "4-byte WRITE DYB REGISTER",
    0xE3: "4-byte PROGRAM PPB"
}

EN4B = 0xB7
EX4B = 0xE9

CONTROL_COMMANDS = {
    0x01: "Write Status Register 1",
    0x06: "Write Enable",
    0x04: "Write Disable",
    0x05: "Read Status Register",
    0x35: "Read Status Register 2",
    0x5A: "Read SFDP Mode",
    0x75: "Program Suspend",
    0xAB: "Release Power-down / Device ID",
    EN4B: "Enable 4 Byte Address",
    EX4B: "Exit 4 Byte Address",
    # IS25LP128F Specific Commands
    0x85: "Read Extended Read Parameters",
    0xC0: "Set Read Parameters",
    0x61: "Read Read Parameters"
}


class FakeFrame:
    def __init__(self, t, time=None):
        self.type = t
        self.start_time = time
        self.end_time = time
        self.data = {}

class SPIFlash(HighLevelAnalyzer):
    address_bytes = NumberSetting(min_value=1, max_value=4)
    min_address = NumberSetting(min_value=0)
    max_address = NumberSetting(min_value=0)
    decode_level = ChoicesSetting(choices=('Everything', 'Only Data', 'Only Errors', 'Only Control'))

    result_types = {
        'error': {
            'format': 'Error!'
        },
        'control_command': {
            'format': '{{data.command}}'
        },
        'data_command': {
            'format': '{{data.command}} 0x{{data.address}} - 0x{{data.address_end}} ({{data.num_bytes}} data bytes)'
        }
    }

    def __init__(self):
        self._start_time = None
        self._address_bytes = 3
        self._address_format = "{:0" + str(2 * int(self._address_bytes)) + "x}"
        self._min_address = int(self.min_address)
        self._max_address = None
        if self.max_address:
            self._max_address = int(self.max_address)

        self._miso_data = None
        self._mosi_data = None
        self._empty_result_count = 0

        self._last_cs = 1
        self._last_time = None
        self._transaction = 0
        self._clock_count = 0
        self._mosi_out = 0
        self._miso_in = 0
        self._quad_data = 0
        self._quad_start = None
        self._continuous = False
        self._dummy = 0

        self._fastest_cs = 2000000

    def decode(self, frame: AnalyzerFrame):
        frames = []
        if frame.type == "data":
            data = frame.data["data"]
            cs = data >> 15
            if self._last_time:
                diff = frame.start_time - self._last_time
            else:
                diff = self._fastest_cs
            diff = float(diff * 1_000_000_000)

            self._fastest_cs = min(diff * 4, self._fastest_cs)
            if diff > self._fastest_cs and cs == 0:
                if self._transaction > 0:
                    frames.append(FakeFrame("disable", self._last_time))

                frames.append(FakeFrame("enable", frame.start_time))

                self._transaction += 1
                self._clock_count = 0
                if not self._continuous:
                    self._command = 0
                    self._quad_start = None
                    self._dummy = 0

                    self._mosi_out = 0
                    self._miso_in = 0
                else:
                    self._clock_count = 8
                    f = FakeFrame("result")
                    f.data["mosi"] = [self._command]
                    f.data["miso"] = [0]
                    frames.append(f)

            self._last_time = frame.start_time

            if cs == 1:
                return None

            if self._quad_start is None or self._clock_count < self._quad_start:
                self._mosi_out = self._mosi_out << 1 | (data & 0x1)
                self._miso_in = self._miso_in << 1 | ((data >> 1) & 0x1)
                if self._clock_count % 8 == 7:
                    if self._clock_count == 7:
                        self._command = self._mosi_out
                        if self._command in CONTINUE_COMMANDS:
                            self._quad_start = 8
                            self._dummy = CONTINUE_COMMANDS[self._command]

                    f = FakeFrame("result")
                    f.data["mosi"] = [self._mosi_out]
                    f.data["miso"] = [self._miso_in]
                    frames.append(f)
                    self._mosi_out = 0
                    self._miso_in = 0
            else:
                self._quad_data = (self._quad_data << 4 | data & 0xf)
                if self._clock_count % 2 == 1:
                    f = FakeFrame("result")
                    if not 15 < self._clock_count <= 15 + self._dummy:
                        f.data["mosi"] = [self._quad_data]
                        f.data["miso"] = [0]
                    else:
                        f.data["mosi"] = [0]
                        f.data["miso"] = [self._quad_data]
                    frames.append(f)
                    if self._command in CONTINUE_COMMANDS and self._clock_count == 15:
                        self._continuous = (self._quad_data & 0xf0) == 0xa0
                    self._quad_data = 0

            self._clock_count += 1
        else:
            print("non data!")
            frames = [frame]

        output = None
        for fake_frame in frames:
            if fake_frame.type == "enable":
                self._start_time = fake_frame.start_time
                self._miso_data = bytearray()
                self._mosi_data = bytearray()
            elif fake_frame.type == "result":
                if self._miso_data is None or self._mosi_data is None:
                    if self._empty_result_count == 0:
                        print(fake_frame)
                    self._empty_result_count += 1
                    continue
                self._miso_data.extend(fake_frame.data["miso"])
                self._mosi_data.extend(fake_frame.data["mosi"])
            elif fake_frame.type == "disable":
                if not self._miso_data or not self._mosi_data:
                    continue
                command = self._mosi_data[0]
                frame_type = None
                frame_data = {"command": command}
                if command in DATA_COMMANDS:
                    if len(self._mosi_data) < 1 + int(self._address_bytes):
                        frame_type = "error"
                    else:
                        frame_type = "data_command"
                        frame_data["command"] = DATA_COMMANDS[command]
                        frame_address = 0
                        for i in range(int(self._address_bytes)):
                            frame_address <<= 8
                            frame_address += self._mosi_data[1 + i]
                        if self.min_address > 0 and frame_address < self._min_address:
                            frame_type = None
                        elif self.max_address and frame_address > self.max_address:
                            frame_type = None
                        else:
                            frame_data["address"] = self._address_format.format(frame_address)
                            non_data_bytes = 2
                            if frame_data["command"] == DATA_COMMANDS[0x0b]:
                                non_data_bytes += 1
                            if frame_data["command"] == DATA_COMMANDS[0xeb]:
                                non_data_bytes += 2
                            num_data_bytes = len(self._mosi_data) - int(self.address_bytes) - non_data_bytes
                            frame_data["num_bytes"] = num_data_bytes
                            frame_data["address_end"] = self._address_format.format(frame_address + num_data_bytes)
                else:
                    if command in CONTROL_COMMANDS:
                        frame_data["command"] = CONTROL_COMMANDS[command]
                    else:
                        frame_data["command"] = ''.join(['0x', hex(command).upper()[2:]])
                    if command == EN4B:
                        self._address_bytes = 4
                        self._address_format = "{:0" + str(2 * int(self._address_bytes)) + "x}"
                    elif command == EX4B:
                        self._address_bytes = 3
                        self._address_format = "{:0" + str(2 * int(self._address_bytes)) + "x}"
                    frame_type = "control_command"
                our_frame = None
                if frame_type:
                    our_frame = AnalyzerFrame(frame_type,
                                              self._start_time,
                                              fake_frame.end_time,
                                              frame_data)
                self._miso_data = None
                self._mosi_data = None
                if self.decode_level == 'Only Data' and frame_type == "control_command":
                    continue
                if self.decode_level == 'Only Errors' and frame_type != "error":
                    continue
                if self.decode_level == "Only Control" and frame_type != "control_command":
                    continue
                output = our_frame
        return output
