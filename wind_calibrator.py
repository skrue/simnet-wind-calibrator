#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Interactive wind-angle calibrator for supported Simrad wind sensors."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
import socket
import sys
import threading
import time
from typing import Optional

import can_protocol as wire

SIMRAD = 1857


def wind_device_label(name: Optional[bytes]) -> str:
    """Friendly description: wind capability comes from PGN 130306, not ISO NAME."""
    if not name:
        return 'Wind sensor (identification pending)'
    desc = extract_name(name)
    if desc['manufacturer'] == SIMRAD:
        return 'Simrad wind sensor'
    return f'Wind sensor (manufacturer code {desc["manufacturer"]})'


def n2k_device_type(desc: dict) -> str:
    """SimNet's published class/function pair, not a model designation."""
    if desc['class'] == 80 and desc['function'] == 190:
        return 'General transducer (SimNet TDU)'
    return f'Class {desc["class"]}, function {desc["function"]}'



def extract_name(name: bytes) -> dict:
    if len(name) != 8:
        raise ValueError('NMEA2000 NAME must have 8 bytes')
    n = int.from_bytes(name, 'little')
    return {'unique': n & 0x1fffff, 'manufacturer': (n >> 21) & 0x7ff,
            'instance': (n >> 32) & 0xff, 'system_instance': (n >> 56) & 0x0f,
            'function': (n >> 40) & 0xff, 'class': (n >> 49) & 0x7f,
            'industry': (n >> 60) & 0x7,
            'arbitrary_address': bool((n >> 63) & 1)}


def clamp_angle(n: int) -> int:
    return max(-180, min(180, n))


def raw_offset(msg: wire.Message) -> Optional[int]:
    if (msg.pgn != wire.P_ECHO or len(msg.payload) < 8 or
            msg.payload[:4] != bytes.fromhex('41 9F 31 C0')):
        return None
    raw = msg.payload[4:6]
    if raw == b'\xff\x7f':  # NMEA unavailable signed16 value
        return None
    return int.from_bytes(raw, 'little', signed=True)


def apparent_wind(msg: wire.Message):
    """Returns (awa_degrees, aws_mps) or None for non-apparent/unavailable."""
    if msg.pgn != wire.P_WIND or len(msg.payload) < 6 or msg.payload[5] & 7 != 2:
        return None
    speed_raw = int.from_bytes(msg.payload[1:3], 'little')
    angle_raw = int.from_bytes(msg.payload[3:5], 'little')
    if angle_raw == 0xffff or speed_raw == 0xffff:
        return None
    return (angle_raw * wire.R2D / 10000, speed_raw / 100)


@dataclass
class Sensor:
    src: int
    name: Optional[bytes] = None
    awa: Optional[float] = None
    aws: Optional[float] = None
    last_wind: float = 0.0
    wind_count: int = 0
    offset: Optional[float] = None
    offset_raw: Optional[int] = None
    offset_at: float = 0.0
    pending_set_raw: Optional[int] = None
    pending_set_since: float = 0.0
    pending_verify_queued: bool = False
    pending_verify_sent_at: float = 0.0
    read_sent: float = 0.0
    read_attempts: int = 0

    @property
    def manufacturer(self):
        return extract_name(self.name)['manufacturer'] if self.name else None

    @property
    def verified_simrad(self):
        return self.manufacturer == SIMRAD and self.wind_count > 0


class CalibratorBackend:
    """All TCP I/O runs in one worker thread; TUI only reads state or queues commands."""

    def __init__(self, host='192.168.4.1', port=20000, source=0x80,
                 log_dir=None, on_change=None, reconnect=True, logging_enabled=True):
        self.host, self.port, self.source = host, port, source
        self.on_change = on_change
        self.reconnect = reconnect
        self.lock = threading.RLock()
        self.devices: dict[int, Sensor] = {}
        self.claims: dict[int, bytes] = {}
        self._source_conflict = False
        self.selected: Optional[int] = None
        self.desired: Optional[int] = None
        self.confirm: Optional[tuple] = None
        self.entry: Optional[str] = None
        self.connected = False
        self.started_at = 0.0
        self.tx_permitted = False
        self.rx_count = 0
        self.tx_count = 0
        self.status = 'Waiting for connection ...'
        self.events = deque(maxlen=7)
        self.stopped = threading.Event()
        self.queue = Queue()
        self.thread = None
        self.logging_enabled = logging_enabled
        filename = 'wind_calibrator_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.log'
        directory = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parent / 'logs'
        self.log_path = directory / filename if logging_enabled else None
        self._log = None
        self._last_refresh = 0.0
        self._last_auto_read_src = None
        self._active_sock = None
        self._buffer = b''
        self._last_broadcast = 0.0

    def notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def log(self, message, status=False):
        line = f'{datetime.now().isoformat(timespec="milliseconds")}  {message}'
        if self._log:
            self._log.write(line + '\n')
            self._log.flush()
        with self.lock:
            self.events.appendleft(message)
            if status:
                self.status = message
        self.notify()

    def start(self):
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name='can-gateway', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.thread = None

    def _run(self):
        with ExitStack() as resources:
            if self.logging_enabled:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    self._log = resources.enter_context(
                        self.log_path.open('a', encoding='utf-8', buffering=1))
                except OSError as exc:
                    self.logging_enabled = False
                    self.log_path = None
                    self.log(f'File logging unavailable: {exc}', status=True)
            while not self.stopped.is_set():
                try:
                    self._connect_and_run()
                except (OSError, ConnectionError) as ex:
                    self.log(f'Connection lost: {ex}', status=True)
                except Exception as ex:
                    self.log(f'Network error: {type(ex).__name__}: {ex}', status=True)
                finally:
                    with self.lock:
                        self.connected = False
                        self.tx_permitted = False
                        self.confirm = None
                        self.entry = None
                        for sensor in self.devices.values():
                            sensor.pending_set_raw = None
                            sensor.pending_verify_queued = False
                            sensor.pending_verify_sent_at = 0.0
                            sensor.read_sent = 0.0
                            sensor.read_attempts = 0
                            sensor.offset = None  # never show previous-session offset as current
                            sensor.offset_raw = None
                        self._last_auto_read_src = None
                    self._drain_queue()  # discard operations after connection failure; never replay a write
                    self.notify()
                if not self.reconnect or self.stopped.is_set():
                    break
                self.stopped.wait(2)
        self._log = None

    def _drain_queue(self):
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                return

    def _connect_and_run(self):
        with socket.create_connection((self.host, self.port), timeout=5.0) as sock:
            sock.settimeout(0.12)
            self._buffer = b''
            self._active_sock = sock
            with self.lock:
                self.devices.clear()
                self.claims.clear()
                self._source_conflict = False
                self.selected = None
                self.desired = None
                self.connected = True
                self.started_at = time.monotonic()
                self.tx_permitted = False
                self.rx_count = 0
                self._last_auto_read_src = None
            self.log(f'Connected to CAN gateway {self.host}:{self.port} · CAN RAW ASCII (bidirectional)', status=True)
            while not self.stopped.is_set():
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    chunk = None
                if chunk == b'':
                    raise ConnectionError('TCP connection closed')
                if chunk:
                    self._handle_chunk(chunk)
                self._tick()
                self._process_commands(sock)
                if time.monotonic() - self._last_refresh > 0.15:
                    self.notify()
                    self._last_refresh = time.monotonic()

    def _handle_chunk(self, chunk):
        self._buffer += chunk
        if len(self._buffer) > 100000:
            self._buffer = self._buffer[-1024:]
            self.log('Incomplete/invalid ASCII data; receive buffer reset', status=True)
        # CAN RAW ASCII TCP servers commonly use CRLF; accept CR or LF alone too.
        self._buffer = self._buffer.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        while b'\n' in self._buffer:
            line, self._buffer = self._buffer.split(b'\n', 1)
            s = line.decode('ascii', 'replace').strip()
            if not s:
                continue
            msg = wire.parse_line(s)
            if msg is None or msg.kind != 'can-raw':
                if self.rx_count == 0 and len(s) >= 8:
                    self.log(f'Unknown input format (expected RAW ASCII): {s[:60]}', status=True)
                continue
            with self.lock:
                self.rx_count += 1
            if msg.src == self.source and ' R ' in (' ' + s + ' '):
                with self.lock:
                    self.tx_permitted = False
                    self._source_conflict = True
                self.log(f'ADDRESS CONFLICT: source 0x{self.source:02X} observed in RX; transmit disabled!', status=True)
            self._handle_message(msg)

    def _handle_message(self, msg):
        now = time.monotonic()
        if msg.pgn == wire.P_CLAIM and len(msg.payload) == 8:
            with self.lock:
                self.claims[msg.src] = msg.payload
                dev = self.devices.get(msg.src)
                if dev is None:
                    return  # Cache claims until an apparent-wind frame identifies the source.
                if dev.name != msg.payload:
                    # CAN source addresses can be reassigned. A different ISO NAME
                    # at the same address is a DIFFERENT device: never carry over
                    # calibration readings or confirmation state from its predecessor.
                    if dev.name is not None:
                        dev.offset = None
                        dev.offset_raw = None
                        dev.offset_at = 0.0
                        dev.pending_set_raw = None
                        dev.pending_verify_queued = False
                        dev.pending_verify_sent_at = 0.0
                        dev.read_attempts = 0
                        dev.read_sent = 0.0
                        dev.wind_count = 0
                        dev.awa = None
                        dev.aws = None
                        dev.last_wind = 0.0
                        if self.selected == msg.src:
                            self.desired = None
                            self.confirm = None
                            self.entry = None
                        self.log(f'IDENTITY CHANGED at 0x{msg.src:02X}: cached calibration discarded', status=True)
                    dev.name = msg.payload
                    desc = extract_name(dev.name)
                    self.log(f'Wind sensor 0x{msg.src:02X}: manufacturer {desc["manufacturer"]}, '
                             f'class {desc["class"]}, function {desc["function"]}')
            return
        wind = apparent_wind(msg)
        if wind is not None:
            with self.lock:
                dev = self.devices.get(msg.src)
                if dev is None:
                    dev = Sensor(msg.src, name=self.claims.get(msg.src))
                    self.devices[msg.src] = dev
                    self.log(f'Wind source 0x{msg.src:02X} discovered (PGN 130306)')
                dev.awa, dev.aws = wind
                dev.last_wind = now
                dev.wind_count += 1
                if self.selected is None:
                    self.selected = msg.src
            return
        if msg.pgn == wire.P_ECHO:
            raw = raw_offset(msg)
            if raw is None:
                return
            with self.lock:
                dev = self.devices.get(msg.src)
                if not dev:
                    return
                if dev.pending_set_raw is not None:
                    if msg.received_at < dev.pending_set_since:
                        self.log('Ignoring reply predating pending SET', status=True)
                        return
                    if not dev.pending_verify_sent_at:
                        # The initial echo is NOT a confirmed calibration. Hold
                        # previous UI value until an independently requested readback.
                        if raw == dev.pending_set_raw and not dev.pending_verify_queued:
                            dev.pending_verify_queued = True
                            self.queue.put(('verify', dev.src, dev.name))
                            self.log(f'SET echo received from 0x{dev.src:02X}; requesting independent readback', status=True)
                        elif raw != dev.pending_set_raw:
                            reported = wire.angle_degrees(raw.to_bytes(2, 'little', signed=True))
                            self.log(f'Offset reply {reported:+.3f}° while SET pending; awaiting matching echo', status=True)
                        return
                    if msg.received_at < dev.pending_verify_sent_at:
                        return
                    # A readback result is received after we transmitted the
                    # verification request. No on-wire correlation ID exists.
                    if raw == dev.pending_set_raw:
                        dev.offset_raw = raw
                        dev.offset = wire.angle_degrees(raw.to_bytes(2, 'little', signed=True))
                        dev.offset_at = now
                        self.log(f'READBACK VERIFIED: 0x{dev.src:02X} offset {dev.offset:+.3f}° (PGN 65480)', status=True)
                        if self.selected == msg.src:
                            self.desired = clamp_angle(round(dev.offset))
                    else:
                        # Actual state is uncertain: do not leave an old value
                        # displayed as known-good or pretend the SET succeeded.
                        dev.offset = None
                        dev.offset_raw = None
                        reported = wire.angle_degrees(raw.to_bytes(2, 'little', signed=True))
                        self.log(f'READBACK MISMATCH: sensor 0x{dev.src:02X} reports {reported:+.3f}°; press R', status=True)
                    dev.pending_set_raw = None
                    dev.pending_verify_queued = False
                    dev.pending_verify_sent_at = 0.0
                else:
                    dev.offset_raw = raw
                    dev.offset = wire.angle_degrees(raw.to_bytes(2, 'little', signed=True))
                    dev.offset_at = now
                    self.log(f'Offset read from 0x{dev.src:02X}: {dev.offset:+.3f}°', status=True)
                    if self.selected == msg.src and self.desired is None:
                        self.desired = clamp_angle(round(dev.offset))
                dev.read_sent = 0
                dev.read_attempts = 0
            return

    def _tick(self):
        now = time.monotonic()
        with self.lock:
            if not self.connected:
                return
            # Passive inspection for 2 seconds, then permit TX unless current source is active.
            if now - self.started_at >= 2.0 and not self.tx_permitted:
                # Sticky conflict flag maintained by set in _handle_chunk.
                if not getattr(self, '_source_conflict', False):
                    self.tx_permitted = True
            selected = self.devices.get(self.selected)
            # One automatically read request, then max two retries on timeout.
            if (selected and selected.verified_simrad and self.tx_permitted and
                    selected.offset is None and selected.pending_set_raw is None):
                if selected.read_attempts == 0:
                    self.queue.put(('read', selected.src, selected.name, True))
                    selected.read_attempts = -1  # reserved, will become 1 once sent
                elif selected.read_attempts in (1, 2) and now-selected.read_sent > 2.5:
                    self.queue.put(('read', selected.src, selected.name, True))
                    selected.read_attempts = -selected.read_attempts  # reservation; converted in _process_commands
                elif selected.read_attempts == 3 and now-selected.read_sent > 3.0:
                    selected.read_attempts = 4
                    self.log('Offset read timed out. Press R to retry.', status=True)
            for dev in self.devices.values():
                if dev.pending_set_raw is not None and now - dev.pending_set_since > 3.0:
                    dev.pending_set_raw = None
                    dev.pending_verify_queued = False
                    dev.pending_verify_sent_at = 0.0
                    dev.offset = None
                    dev.offset_raw = None
                    self.log('Write unverified: timed out waiting for confirmed readback. Press R to re-read.', status=True)

    def _process_commands(self, sock):
        while True:
            try:
                item = self.queue.get_nowait()
            except Empty:
                return
            cmd, src, claimed_name, *args = item
            with self.lock:
                device = self.devices.get(src)
                allowed = (self.tx_permitted and device is not None and device.verified_simrad and
                           device.name == claimed_name and (self.selected == src or cmd == 'verify') and
                           (time.monotonic()-device.last_wind) < 3.0)
            if not allowed:
                self.log(f'Not sent: wind sensor 0x{src:02X} not verified/active or source address blocked', status=True)
                continue
            if cmd == 'verify':
                with self.lock:
                    if not (device.pending_set_raw is not None and device.pending_verify_queued):
                        continue
                msg = wire.Message(wire.P_CMD, self.source, src, 3,
                                   wire.direct_message(0), '', '', time.monotonic())
                self._send(sock, msg)
                with self.lock:
                    device.pending_verify_sent_at = time.monotonic()
                    device.pending_verify_queued = False
                self.log(f'Post-SET verification read sent to 0x{src:02X} (PGN 61184)')
            elif cmd == 'read':
                auto = args[0]
                msg = wire.Message(wire.P_CMD, self.source, src, 3,
                                   wire.direct_message(0), '', '', time.monotonic())
                self._send(sock, msg)
                with self.lock:
                    device.read_attempts = max(1, abs(device.read_attempts)+1) if device.read_sent else 1
                    device.read_sent = time.monotonic()
                self.log(f'Read request sent to 0x{src:02X} (PGN 61184)')
            elif cmd == 'set':
                requested, previous = args
                with self.lock:
                    if (device.offset is None or device.pending_set_raw is not None or
                            abs(device.offset - previous) > 0.15):
                        self.log('Not sent: current offset changed since confirmation', status=True)
                        continue
                payload = wire.direct_message(1, requested)
                expected = int.from_bytes(payload[4:6], 'little', signed=True)
                msg = wire.Message(wire.P_CMD, self.source, src, 3,
                                   payload, '', '', time.monotonic())
                self._send(sock, msg)
                with self.lock:
                    device.pending_set_raw = expected
                    device.pending_set_since = time.monotonic()
                    device.pending_verify_queued = False
                    device.pending_verify_sent_at = 0.0
                self.log(f'SET sent to 0x{src:02X}: {requested:+d}° – awaiting sensor reply', status=True)

    def _send(self, sock, msg):
        for line in wire.format_can_ascii(msg, 0):
            sock.sendall(line)
            self.tx_count += 1

    def select_relative(self, delta):
        with self.lock:
            arr = sorted(self.devices.keys())
            if not arr:
                return
            if self.selected not in arr:
                self.selected = arr[0]
            else:
                self.selected = arr[(arr.index(self.selected)+delta) % len(arr)]
            selected = self.devices[self.selected]
            self.desired = clamp_angle(round(selected.offset)) if selected.offset is not None else None
            self.confirm = None
            self.entry = None
            self.log(f'Wind source 0x{selected.src:02X} selected')
        self.notify()

    def nudge(self, step):
        with self.lock:
            if self.entry is not None:
                return
            dev = self.devices.get(self.selected)
            if not dev or dev.offset is None or not dev.verified_simrad or dev.pending_set_raw is not None:
                return
            current = self.desired if self.desired is not None else round(dev.offset)
            self.desired = clamp_angle(current+step)
            self.confirm = None
        self.notify()

    def request_read(self):
        with self.lock:
            dev = self.devices.get(self.selected)
            if dev and dev.pending_set_raw is not None:
                self.log('Write still awaiting confirmation; wait before requesting a new read', status=True)
                return
            if dev:
                dev.read_attempts = 0
                dev.read_sent = 0
                dev.offset = None
                dev.offset_raw = None
                self.desired = None
                self.entry = None
                dev.read_attempts = -1  # Reserve the request; automatic timer must not duplicate it.
                self.queue.put(('read', dev.src, dev.name, False))
        self.notify()

    def prepare_entry(self):
        with self.lock:
            dev = self.devices.get(self.selected)
            if not dev or dev.offset is None or not dev.verified_simrad:
                self.log('Enter a value only after the offset has been read successfully', status=True)
                return
            self.entry = ''  # Start empty so 'G', '1', '0', Enter directly enters +10°.
            self.confirm = None
        self.notify()

    def input_key(self, key):
        with self.lock:
            if self.entry is None:
                return False
            if key == 'cancel':
                self.entry = None
            elif key == 'backspace':
                self.entry = self.entry[:-1]
            elif key == 'enter':
                try:
                    val = int(self.entry)
                    if self.entry.strip() in ('', '+', '-') or not -180 <= val <= 180:
                        raise ValueError
                except ValueError:
                    self.log('Invalid input: enter an integer from -180 to +180 degrees', status=True)
                    return True
                self.desired = val
                self.entry = None
            elif key in '0123456789+-' and len(self.entry) < 5:
                if key in '+-':
                    self.entry = key + self.entry.lstrip('+-')
                else:
                    self.entry += key
            else:
                return False
        self.notify()
        return True

    def prepare_set(self):
        with self.lock:
            if self.entry is not None:
                return
            dev = self.devices.get(self.selected)
            if not self.connected or not self.tx_permitted or dev is None or not dev.verified_simrad:
                self.log('Unavailable: no verified Simrad wind sensor selected', status=True)
                return
            if dev.offset is None or dev.pending_set_raw is not None or self.desired is None:
                self.log('Unavailable: read the current offset first', status=True)
                return
            if abs(self.desired-dev.offset) < 0.3:
                self.log('Target offset already matches the current offset.', status=True)
                return
            self.confirm = (dev.src, self.desired, dev.offset, dev.name)
        self.notify()

    def confirm_set(self, yes):
        with self.lock:
            v = self.confirm
            self.confirm = None
            if (yes and v is not None and self.selected == v[0] and
                    v[0] in self.devices and self.devices[v[0]].name == v[3]):
                self.queue.put(('set', v[0], v[3], v[1], v[2]))
            elif not yes:
                self.log('Change cancelled; no write command sent.', status=True)
        self.notify()

    def snapshot(self):
        with self.lock:
            devices = [dict(src=d.src, name=d.name, manufacturer=d.manufacturer,
                            awa=d.awa, aws=d.aws, last_wind=d.last_wind,
                            wind_count=d.wind_count, offset=d.offset,
                            pending=d.pending_set_raw is not None,
                            verified=d.verified_simrad) for d in sorted(self.devices.values(), key=lambda x:x.src)]
            return dict(connected=self.connected, tx_permitted=self.tx_permitted,
                        devices=devices, selected=self.selected, desired=self.desired,
                        confirm=self.confirm, entry=self.entry, status=self.status,
                        events=list(self.events), rx_count=self.rx_count,
                        tx_count=self.tx_count, log_path=str(self.log_path) if self.log_path else None,
                        )


def live_wind_cards(awa: str, aws_mps: str, aws_kn: str) -> list[str]:
    """Two fixed-width highlighted wind-data tiles (plain text; styling in main).

    Each tile occupies 32 terminal columns; the 4-column gap remains unstyled.
    Keep the box dimensions stable so changing 10 Hz readings do not shift UI.
    """
    width = 32
    inner = width - 2

    def tile(label: str, value: str, subtitle: str) -> tuple[str, ...]:
        def padded(text: str, center: bool = False) -> str:
            # Widths of our labels/number strings are one terminal cell each.
            if len(text) > inner - 2:
                text = text[:inner - 2]
            return '│' + (' ' + (text.center(inner - 2) if center else text.ljust(inner - 2)) + ' ') + '│'
        return (
            '┌' + '─' * inner + '┐',
            padded(label),
            padded(value, center=True),
            padded(subtitle, center=True),
            '└' + '─' * inner + '┘',
        )

    left = tile('AWA  ·  APPARENT WIND ANGLE', awa, '')
    right = tile('AWS  ·  APPARENT WIND SPEED', aws_mps, aws_kn)
    return ['  ' + a + '    ' + b for a, b in zip(left, right)]


def render_main(backend: CalibratorBackend):
    s = backend.snapshot()
    selected = next((x for x in s['devices'] if x['src']==s['selected']), None)
    connected = 'CONNECTED' if s['connected'] else 'DISCONNECTED'
    link = 'TX READY' if s['tx_permitted'] else 'TX BLOCKED / WAITING'
    header = f'  SIMNET WIND CALIBRATOR  v2.4.2                   {connected}  |  {link}\n'
    header += f'  CAN gateway: {backend.host}:{backend.port}    Source: 0x{backend.source:02X}    RX: {s["rx_count"]}    TX: {s["tx_count"]}\n'
    header += '  ' + '─'*68 + '\n'
    lines = [header,'  DETECTED WIND SENSORS (↑/↓ to select)']
    if not s['devices']:
        lines += ['    Looking for PGN 130306 (apparent wind) and ISO NAME ...', '']
    # Sliding selection window: the selected sensor must remain on screen,
    # even on unusually large NMEA 2000 networks with >12 wind sources.
    all_devices = s['devices']
    selected_index = next((i for i, d in enumerate(all_devices)
                           if d['src'] == s['selected']), 0)
    first = min(max(0, selected_index - 3), max(0, len(all_devices) - 6))
    visible_devices = all_devices[first:first + 6]
    for d in visible_devices:
        selected_flag = '▶' if d['src']==s['selected'] else ' '
        ident = wind_device_label(d['name'])
        if d['name']:
            ident += f' · ID {extract_name(d["name"])["unique"]}' 
        freshness = 'ACTIVE' if time.monotonic()-d['last_wind'] < 3 else 'STALE'
        lines.append(f'   {selected_flag} 0x{d["src"]:02X}   {ident:<28} {freshness}')
    if len(s['devices']) > len(visible_devices):
        lines.append(f'     ... {len(s["devices"])-len(visible_devices)} more sources; ↑/↓ to select')
    lines += ['  ' + '─'*68, '']
    if not selected:
        lines += ['  No wind sensor selected.', '']
    else:
        info = extract_name(selected['name']) if selected['name'] else None
        lines += [f'  WIND SENSOR  |  {wind_device_label(selected["name"])}']
        if info:
            lines += [f'  CAN address: 0x{selected["src"]:02X}     Device ID: {info["unique"]}     Instance: {info["instance"]}']
        else:
            lines += [f'  CAN address: 0x{selected["src"]:02X}     Device ID: waiting for ISO Address Claim']
        lines.append('  DEVICE DETAILS')
        if info:
            manufacturer = 'Simrad' if info['manufacturer'] == SIMRAD else f'Unknown ({info["manufacturer"]})'
            name = selected['name'].hex(' ').upper()
            lines.extend([f'    Manufacturer: {manufacturer}  |  Code: {info["manufacturer"]}',
                          f'    NMEA 2000 type: {n2k_device_type(info)}  [class {info["class"]} / function {info["function"]}]',
                          f'    System instance: {info["system_instance"]}  |  Industry group: {info["industry"]}',
                          f'    ISO NAME: {name}'])
        else:
            lines.append('    Waiting for ISO Address Claim (PGN 60928)')
        fresh = time.monotonic()-selected['last_wind'] < 3
        awa = f'{selected["awa"]:.2f}°' if fresh and selected['awa'] is not None else '--.--°'
        aws_mps = f'{selected["aws"]:.2f} m/s' if fresh and selected['aws'] is not None else '--.-- m/s'
        aws_kn = f'{selected["aws"]*1.943844:.2f} kn' if fresh and selected['aws'] is not None else '--.-- kn'
        lines += ['']  # visual separation between device identity and live readings
        lines.extend(live_wind_cards(awa, aws_mps, aws_kn))
        lines += ['', '  WIND ANGLE CALIBRATION', '']
        current = f'{selected["offset"]:+8.3f}°' if selected['offset'] is not None else '  ---.---° (not read yet)'
        desired = f'{s["desired"]:+4d}°' if s['desired'] is not None else '  ----'
        lines += [f'  Current offset: {current}',
                  f'  Target offset:  {desired}       [←/→ or −/+ adjusts by 1°]',
                  f'  Allowed range: −180° to +180°', '']
        if s['entry'] is not None:
            lines += [f'  ENTER ANGLE: [{s["entry"]}_]  (Enter: use as target; Esc: cancel)', '']
        if selected['pending']:
            lines += ['  ⏳ Write command sent – waiting for sensor confirmation ...','']
        if s['confirm']:
            _,new,old,_ = s['confirm']
            lines += [f'  ⚠  APPLY OFFSET CHANGE? {old:+.3f}° → {new:+d}°',
                      '     [Y] Yes, write to sensor          [N/Esc] Cancel', '']
    lines += ['  ' + '─'*68,
              f'  STATUS: {s["status"][:110]}',
              '  Recent events:']
    lines.extend('    '+x[:95] for x in s['events'][:3])
    return '\n'.join(lines)


def prompt_gateway_settings(host: str, port: int, prompt_fn=None,
                            logging_enabled: bool = True) -> tuple[str, int, bool]:
    """Ask once for endpoint before entering the full-screen UI.

    prompt_toolkit's ``default`` parameter inserts an *editable* value into the
    input buffer. Pressing Enter accepts it; Ctrl-C / Ctrl-D cancel startup.
    Passing a prompt function makes this interaction straightforward to test.
    """
    if prompt_fn is None:
        from prompt_toolkit.shortcuts import prompt as prompt_fn

    print('\nCAN gateway connection')
    print('Edit the prefilled values, or press Enter to accept each one.')
    while True:
        selected_host = prompt_fn('Gateway IP / hostname: ', default=host).strip()
        if selected_host and not any(c.isspace() for c in selected_host):
            host = selected_host
            break
        print('Please enter a valid IP address or hostname (no spaces).')

    while True:
        selected_port = prompt_fn('Gateway TCP port: ', default=str(port)).strip()
        try:
            candidate = int(selected_port, 10)
        except ValueError:
            candidate = 0
        if 1 <= candidate <= 65535:
            port = candidate
            break
        print('Please enter a TCP port between 1 and 65535.')

    while True:
        choice = prompt_fn('Enable logging? [Y/n]: ',
                           default='Y' if logging_enabled else 'N').strip().lower()
        if choice in ('', 'y', 'yes'):
            logging_enabled = True
            break
        if choice in ('n', 'no'):
            logging_enabled = False
            break
        print('Please answer Y or N.')

    return host, port, logging_enabled


def main():
    ap=argparse.ArgumentParser(description='Interactive Simrad Wind Calibrator (CAN RAW ASCII TCP gateway).')
    ap.add_argument('--host', default='192.168.4.1', help='CAN gateway hostname or IP address (default: 192.168.4.1)')
    ap.add_argument('--port', type=int, default=20000, help='TCP port configured for bidirectional CAN RAW ASCII (default: 20000)')
    ap.add_argument('--source', type=lambda s:int(s,0), default=0x80, help='Unused CAN source for commands (default 0x80)')
    logging_options = ap.add_mutually_exclusive_group()
    logging_options.add_argument('--logging', dest='logging_enabled', action='store_true',
                                 help='Enable session logging (default)')
    logging_options.add_argument('--no-logging', dest='logging_enabled', action='store_false',
                                 help='Disable session log files')
    ap.set_defaults(logging_enabled=True)
    ap.add_argument('--log-dir', default=None,
                    help='Directory for session logs (default: logs/ next to the application)')
    ap.add_argument('--once', action='store_true', help='Do not automatically reconnect after loss of connection')
    ap.add_argument('--no-prompt', action='store_true', help='Skip interactive gateway setup (for scripts/automation)')
    args=ap.parse_args()
    if not 0<=args.source<=253 or args.source in (0x03,0x10):
        ap.error('Source must be an unused 0..253 address, not 0x03 or 0x10')
    if not 1 <= args.port <= 65535:
        ap.error('TCP port must be between 1 and 65535')
    if not args.host.strip() or any(c.isspace() for c in args.host):
        ap.error('Gateway host must be a hostname or IP address without spaces')
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
    except ImportError:
        ap.exit(2, 'Missing dependency: python3 -m pip install "prompt_toolkit>=3.0"\n')

    if not args.no_prompt:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            ap.error('Gateway setup requires an interactive terminal; use --no-prompt for unattended runs')
        try:
            args.host, args.port, args.logging_enabled = prompt_gateway_settings(
                args.host, args.port, logging_enabled=args.logging_enabled)
        except (KeyboardInterrupt, EOFError):
            print('\nStartup cancelled.')
            return 0

    backend=CalibratorBackend(args.host,args.port,args.source,args.log_dir,
                              reconnect=not args.once, logging_enabled=args.logging_enabled)
    def formatted():
        output = []
        for line in render_main(backend).splitlines(keepends=True):
            if line.startswith(('  ┌', '  │', '  └')):
                # Style only the two tiles, leaving their separating gap blank.
                # No flashing or color changes on every incoming wind frame.
                tile_style = ('class:live-card-value' if line.startswith('  │') and
                              'AWA  ·' not in line and 'AWS  ·' not in line
                              else 'class:live-card')
                output.extend([('', line[:2]),
                               (tile_style, line[2:34]),
                               ('', line[34:38]),
                               (tile_style, line[38:70]),
                               ('', line[70:])])
                continue
            if line.startswith('  SIMNET WIND CALIBRATOR'):
                st = 'class:header'
            elif line.startswith(('  DETECTED WIND SENSORS', '  WIND SENSOR ', '  DEVICE DETAILS', '  WIND ANGLE CALIBRATION')):
                st = 'class:section'
            elif line.startswith('   ▶'):
                st = 'class:selected'
            elif line.startswith('  Current offset:'):
                st = 'class:value'
            elif line.startswith(('  Target offset:', '  ENTER ANGLE:')):
                st = 'class:desired'
            elif line.startswith('  ⚠') or line.startswith('     [Y]'):
                st = 'class:warning'
            elif line.startswith('  STATUS:'):
                st = 'class:status'
            elif line.startswith('    '):
                st = 'class:muted'
            else:
                st = ''
            output.append((st,line))
        return output
    content=FormattedTextControl(formatted, focusable=False)
    footer=FormattedTextControl(lambda: '  ←/→ ±1°  G Enter value  ↑/↓ Sensor  A Apply  R Read  Q Quit  ', focusable=False)
    root=HSplit([Window(content=content, wrap_lines=False),Window(content=footer,height=1, style='class:footer')])
    bindings=KeyBindings()
    app=None

    @bindings.add('left')
    def _(event):backend.nudge(-1)

    @bindings.add('-')
    def _(event):
        if not backend.input_key('-'):
            backend.nudge(-1)

    @bindings.add('right')
    def _(event):backend.nudge(1)

    @bindings.add('+')
    @bindings.add('=')
    def _(event):
        if not backend.input_key('+'):
            backend.nudge(1)

    @bindings.add('g')
    @bindings.add('G')
    def _(event):backend.prepare_entry()

    @bindings.add('enter')
    def _(event):backend.input_key('enter')

    @bindings.add('backspace')
    def _(event):backend.input_key('backspace')

    for digit in '0123456789':
        def make_digit_handler(d):
            def handler(event):backend.input_key(d)
            return handler
        bindings.add(digit)(make_digit_handler(digit))

    @bindings.add('up')
    def _(event):backend.select_relative(-1)

    @bindings.add('down')
    def _(event):backend.select_relative(1)

    @bindings.add('r')
    @bindings.add('R')
    def _(event):
        if not backend.snapshot()['confirm']:
            backend.request_read()

    @bindings.add('a')
    @bindings.add('A')
    def _(event):backend.prepare_set()

    @bindings.add('y')
    @bindings.add('Y')
    @bindings.add('j')
    @bindings.add('J')
    def _(event):
        if backend.snapshot()['confirm']:
            backend.confirm_set(True)

    @bindings.add('n')
    @bindings.add('N')
    @bindings.add('escape')
    def _(event):
        if backend.snapshot()['entry'] is not None:
            backend.input_key('cancel')
        elif backend.snapshot()['confirm']:
            backend.confirm_set(False)

    @bindings.add('q')
    @bindings.add('Q')
    @bindings.add('c-c')
    def _(event):
        if backend.snapshot()['confirm']:
            backend.confirm_set(False)
        else:
            event.app.exit()

    style=Style.from_dict({
        'footer':'bg:#234156 #ffffff',
        'header':'bold fg:#7dbed9',
        'section':'bold fg:#b0b9c6',
        'selected':'bold fg:#82d4ff',
        'live-card':'bg:#155b43 fg:#ffffff',
        'live-card-value':'bold bg:#155b43 fg:#ffffff',
        'value':'bold fg:#b6e6da',
        'desired':'bold fg:#f0d18d',
        'warning':'bold fg:#f2b47d',
        'status':'fg:#c5d3e0',
        'muted':'fg:#7d8a9a',
    })
    app=Application(layout=Layout(root),key_bindings=bindings,style=style,full_screen=True,mouse_support=False)
    backend.on_change=app.invalidate
    backend.start()
    try:
        app.run()
    finally:
        backend.stop()
    if backend.log_path:
        print('Session ended. Log:', backend.log_path)
    else:
        print('Session ended. File logging disabled.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
