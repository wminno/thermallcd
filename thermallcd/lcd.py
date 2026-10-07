"""Minimal Linux driver for the Thermalright Trofeo Vision 9.16" LCD panel.

USB ID 0416:5408 (Winbond "USBDISPLAY"), vendor-specific bulk device:
  - EP 0x09 bulk OUT: commands and JPEG data
  - EP 0x81 bulk IN:  512-byte ACK

Protocol ("LY" wire, read from the vendor DLL USBLCDNEW 2.1.6
ThreadSendDeviceDataLY; see reference/trccref doc/PROTOCOL_USBLCDNEW.md
and thermalright-trcc-linux src/trcc/adapters/device/ly_lcd.py):

  1. HANDSHAKE: write 2048 bytes (cmd 0x02), read 512-byte response
     (resp[0]=3, resp[1]=0xFF).
  2. FRAME: payload (baseline JPEG 1920x462) sliced into 512-byte chunks
     of 16-byte header + 496 data:
       [0]=0x01 [1]=0xFF [2..5]=total size LE32 [6..7]=chunk data len LE16
       [8]=0x01 (LY) [9..10]=chunk count LE16 [11..12]=chunk index LE16
     Chunk count is padded to a multiple of 4 (zero-filled). The buffer is
     written as 4096-byte bulk writes with a final 2048-byte write, then
     ONE 512-byte ACK is read. JPEG must stay under ~450 KB.
"""

import struct
import time

import usb.core
import usb.util

VID = 0x0416
PID = 0x5408
EP_OUT = 0x09
EP_IN = 0x81
INTERFACE = 0
WIDTH = 1920
HEIGHT = 462
HEADER_SIZE = 16
CHUNK_SIZE = 512
CHUNK_DATA_SIZE = CHUNK_SIZE - HEADER_SIZE   # 496
USB_WRITE_SIZE = 4096
LAST_WRITE_SIZE = 2048
ACK_SIZE = 512
MAX_FRAME_BYTES = 450_000

JPEG_QUALITY = 85
JPEG_SUBSAMPLING = "4:2:0"

ACK_TIMEOUT_MS = 1000
WRITE_TIMEOUT_MS = 5000


class TrofeoError(RuntimeError):
    pass


def encode_jpeg(image, quality=JPEG_QUALITY):
    """Encode a PIL image to a protocol-compatible JPEG (baseline, 4:2:0)."""
    import io

    if image.size != (WIDTH, HEIGHT):
        image = image.resize((WIDTH, HEIGHT))
    if image.mode != "RGB":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(
        buf,
        format="JPEG",
        quality=quality,
        subsampling=JPEG_SUBSAMPLING,
        optimize=False,
        dpi=(96, 96),
    )
    return buf.getvalue()


class TrofeoLCD:
    def __init__(self, debug=False):
        self.dev = None
        self.debug = debug
        self.frame_counter = 0

    # -- lifecycle ---------------------------------------------------------

    def open(self):
        if self.dev is not None:
            return
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise TrofeoError("LCD not found (0416:5408). Is the panel plugged in?")
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            if "Active" not in str(e):  # already configured
                raise
        try:
            dev.detach_kernel_driver(INTERFACE)
        except (usb.core.USBError, NotImplementedError):
            pass  # none bound (vendor-specific class) - normal
        usb.util.claim_interface(dev, INTERFACE)
        self.dev = dev

    def close(self):
        dev, self.dev = self.dev, None
        if dev is None:
            return
        for step in (
            lambda: usb.util.release_interface(dev, INTERFACE),
            lambda: usb.util.dispose_resources(dev),
        ):
            try:
                step()
            except usb.core.USBError:
                pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- protocol ----------------------------------------------------------

    def init(self):
        """Send the mandatory handshake; returns info parsed from the reply."""
        self._drain_in()
        packet = bytearray(2048)
        packet[0] = 0x02  # cmd: handshake/query
        packet[1] = 0xFF
        packet[8] = 0x01
        self.dev.write(EP_OUT, bytes(packet), WRITE_TIMEOUT_MS)
        resp = bytes(self.dev.read(EP_IN, ACK_SIZE, ACK_TIMEOUT_MS))
        if resp[0] != 0x03 or resp[1] != 0xFF:
            raise TrofeoError(f"unexpected handshake response: {resp[:16].hex(' ')}")
        if self.debug:
            print(f"handshake response: {resp[:64].hex(' ')}")
        # PM/SUB bytes identify the panel model/variant (vendor semantics)
        raw_pm = resp[20] if len(resp) > 20 else 0
        if raw_pm <= 3:
            raw_pm = 1
        info = {
            "status": resp[8],
            "pm": 64 + raw_pm,
            "sub": (resp[22] if len(resp) > 22 else 0) + 1,
        }
        if self.debug:
            print(f"init ok: {info}")
        return info

    def _drain_in(self, max_reads=8):
        for _ in range(max_reads):
            try:
                self.dev.read(EP_IN, ACK_SIZE, 20)
            except usb.core.USBTimeoutError:
                return

    def _read_ack(self):
        """One 512-byte ACK per frame; a miss is logged, not fatal."""
        try:
            ack = bytes(self.dev.read(EP_IN, ACK_SIZE, ACK_TIMEOUT_MS))
        except usb.core.USBTimeoutError:
            if self.debug:
                print("  ack: timeout")
            return None
        if self.debug and (ack[0] != 0x03 or ack[8] != 0x01):
            print(f"  ack warning: {ack[:16].hex(' ')}")
        return ack

    def _build_chunks(self, payload: bytes) -> bytes:
        """Slice payload into 512-byte chunks; pad count to multiple of 4."""
        total_size = len(payload)
        num_chunks = total_size // CHUNK_DATA_SIZE + 1
        last_data = total_size % CHUNK_DATA_SIZE
        buf = bytearray(num_chunks * CHUNK_SIZE)
        for i in range(num_chunks):
            off = i * CHUNK_SIZE
            data_len = last_data if i == num_chunks - 1 else CHUNK_DATA_SIZE
            buf[off] = 0x01
            buf[off + 1] = 0xFF
            struct.pack_into("<I", buf, off + 2, total_size)
            struct.pack_into("<H", buf, off + 6, data_len)
            buf[off + 8] = 0x01  # LY chunk command
            struct.pack_into("<H", buf, off + 9, num_chunks)
            struct.pack_into("<H", buf, off + 11, i)
            src = i * CHUNK_DATA_SIZE
            buf[off + HEADER_SIZE : off + HEADER_SIZE + data_len] = payload[
                src : src + data_len
            ]
        # pad chunk count to a multiple of 4 with zero chunks
        padded = num_chunks + (-num_chunks % 4)
        buf += bytes(padded * CHUNK_SIZE - len(buf))
        return bytes(buf)

    def send_jpeg(self, jpeg: bytes) -> int:
        """Stream one JPEG frame; returns number of 512-byte chunks sent."""
        if self.dev is None:
            raise TrofeoError("device not open")
        if len(jpeg) >= MAX_FRAME_BYTES:
            raise TrofeoError(f"frame too large: {len(jpeg)} bytes")
        frame = self._build_chunks(jpeg)
        pos, size = 0, len(frame)
        while pos < size:
            remaining = size - pos
            if remaining >= USB_WRITE_SIZE:
                n = USB_WRITE_SIZE
            else:
                n = min(LAST_WRITE_SIZE, remaining)
            self.dev.write(EP_OUT, frame[pos : pos + n], WRITE_TIMEOUT_MS)
            pos += USB_WRITE_SIZE
        self._read_ack()
        self.frame_counter += 1
        return size // CHUNK_SIZE

    def send_image(self, image, quality=JPEG_QUALITY) -> int:
        return self.send_jpeg(encode_jpeg(image, quality))


def find() -> bool:
    """True if the panel is currently plugged in."""
    return usb.core.find(idVendor=VID, idProduct=PID) is not None
