"""
KISS framing for HF-256 FreeDV transport.
KISS is the interface protocol used by freedvtnc2 on port 8001.

Frame format:
  FEND (0xC0) + DATAFRAME (0x00) + data + FEND (0xC0)

Escape sequences:
  0xC0 in data -> 0xDB 0xDC
  0xDB in data -> 0xDB 0xDD
"""

FEND  = 0xC0
FESC  = 0xDB
TFEND = 0xDC
TFESC = 0xDD

DATA_FRAME = 0x00


def encode(data: bytes) -> bytes:
    """
    Wrap raw data in a KISS data frame.
    Escapes any FEND or FESC bytes in the payload.
    """
    escaped = bytearray()
    for byte in data:
        if byte == FEND:
            escaped.extend([FESC, TFEND])
        elif byte == FESC:
            escaped.extend([FESC, TFESC])
        else:
            escaped.append(byte)

    frame = bytearray()
    frame.append(FEND)
    frame.append(DATA_FRAME)
    frame.extend(escaped)
    frame.append(FEND)
    return bytes(frame)


def decode(data: bytes) -> list:
    """
    Extract complete KISS frames from a raw byte stream.
    Returns a list of decoded payload bytes objects.
    Handles multiple frames and partial frames in one call.
    """
    frames = []
    i = 0
    length = len(data)

    while i < length:
        # Find frame start
        if data[i] != FEND:
            i += 1
            continue

        i += 1  # Skip FEND

        if i >= length:
            break

        # Read frame type byte
        frame_type = data[i]
        i += 1

        # Only process DATA_FRAME (type 0x00)
        # Skip other KISS command frames
        if frame_type != DATA_FRAME:
            # Scan to next FEND
            while i < length and data[i] != FEND:
                i += 1
            continue

        # Read payload until next FEND
        payload = bytearray()
        while i < length:
            byte = data[i]
            i += 1

            if byte == FEND:
                # End of frame
                frames.append(bytes(payload))
                break
            elif byte == FESC:
                # Escape sequence
                if i >= length:
                    break
                next_byte = data[i]
                i += 1
                if next_byte == TFEND:
                    payload.append(FEND)
                elif next_byte == TFESC:
                    payload.append(FESC)
                else:
                    # Invalid escape - skip
                    pass
            else:
                payload.append(byte)

    return frames


class KISSBuffer:
    """
    Stateful KISS frame accumulator for use with streaming TCP reads.
    Feed incoming bytes with feed() and retrieve complete frames with
    get_frames().
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        """Add incoming bytes to the internal buffer."""
        self._buf.extend(data)

    def get_frames(self) -> list:
        """
        Extract all complete frames from the buffer.
        Partial frames remain in the buffer for the next feed() call.
        Returns list of decoded payload bytes objects.
        """
        frames = decode(bytes(self._buf))

        # Find the position after the last complete frame
        # by scanning for FEND boundaries
        last_fend = -1
        i = len(self._buf) - 1
        while i >= 0:
            if self._buf[i] == FEND:
                last_fend = i
                break
            i -= 1

        if last_fend >= 0:
            self._buf = self._buf[last_fend + 1:]
        # If no FEND found, keep entire buffer (partial frame arriving)

        return frames

    def clear(self):
        """Clear the buffer."""
        self._buf.clear()
