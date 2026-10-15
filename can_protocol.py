# SPDX-License-Identifier: Apache-2.0
"""Minimal CAN RAW ASCII transport and wind calibration messages."""
from dataclasses import dataclass
from datetime import datetime
import math
import re
import time

P_CLAIM = 60928
P_WIND = 130306
P_CMD = 61184
P_ECHO = 65480
R2D = 180 / math.pi
CAN_PATTERN = re.compile(r"^\d\d:\d\d:\d\d(?:\.\d{1,6})?\s+([RT])\s+([0-9a-fA-F]{8})\s+((?:[0-9a-fA-F]{2}\s*){1,8})$")

@dataclass(frozen=True)
class Message:
    pgn: int
    src: int
    dst: int
    priority: int
    payload: bytes
    kind: str
    raw: str
    received_at: float


def unpack_id(can_id: int):
    src = can_id & 0xff
    ps = (can_id >> 8) & 0xff
    pf = (can_id >> 16) & 0xff
    dp = (can_id >> 24) & 1
    priority = (can_id >> 26) & 7
    pgn = (dp << 16) | (pf << 8) | (ps if pf >= 240 else 0)
    return pgn, src, (255 if pf >= 240 else ps), priority


def pack_id(pgn: int, source: int, destination: int, priority: int):
    pf = (pgn >> 8) & 255
    ps = destination if pf < 240 else pgn & 255
    return (priority << 26) | (((pgn >> 16) & 1) << 24) | (pf << 16) | (ps << 8) | source


def parse_line(line: str, when=None):
    when = time.monotonic() if when is None else when
    line = line.strip()
    match = CAN_PATTERN.fullmatch(line)
    if not match:
        return None
    can_id = int(match.group(2), 16)
    pgn, source, target, priority = unpack_id(can_id)
    return Message(pgn, source, target, priority,
                   bytes.fromhex(match.group(3)), 'can-raw', line, when)


def format_can_ascii(msg: Message, counter=0):
    if len(msg.payload) > 8:
        raise ValueError('Only single-frame CAN messages are supported')
    stamp = datetime.now().strftime('%H:%M:%S.%f')[:12]
    identifier = pack_id(msg.pgn, msg.src, msg.dst, msg.priority)
    line = f'{stamp} T {identifier:08X} ' + ' '.join(f'{b:02X}' for b in msg.payload) + '\r\n'
    return [line.encode('ascii')]


def angle_raw(degrees: float):
    if not -180 <= degrees <= 180:
        raise ValueError('Offset must be between -180 and +180 degrees')
    return round(math.radians(degrees) * 10000).to_bytes(2, 'little', signed=True)


def angle_degrees(raw: bytes):
    return int.from_bytes(raw[:2], 'little', signed=True) * R2D / 10000


def direct_message(op: int, degrees=None):
    if op == 0:
        return bytes.fromhex('41 9F 31 40 FF FF FF FF')
    if op == 1 and degrees is not None:
        return bytes.fromhex('41 9F 31 C0') + angle_raw(degrees) + bytes.fromhex('20 E8')
    raise ValueError('Invalid calibration command')
