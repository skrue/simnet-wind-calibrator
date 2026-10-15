# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests with a local fake CAN RAW ASCII gateway; no CAN hardware required."""
import socket
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

import can_protocol as wire
import wind_calibrator as app


def test_name(identity: int, manufacturer: int = 1857) -> bytes:
    """Synthetic NMEA 2000 NAME for offline test fixtures."""
    value = identity | (manufacturer << 21) | (130 << 40) | (25 << 49) | (4 << 60)
    return value.to_bytes(8, 'little')


def wait_for(check, duration=8):
    end=time.monotonic()+duration
    while time.monotonic()<end:
        v=check()
        if v:
            return v
        time.sleep(.04)
    raise AssertionError('Timed out waiting for simulator condition')


class FakeGateway:
    def __init__(self, manufacturer=1857, simulate_no_echo=False, collision=False):
        self.manufacturer=manufacturer
        self.no_echo=simulate_no_echo
        self.collision=collision
        self.offset=9.0
        self.tx=[]
        self.listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen(1)
        self.port=self.listener.getsockname()[1]
        self.stopped=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def tx_frame(self, sock, pgn, source, data, priority=3):
        msg=wire.Message(pgn,source,255,priority,data,'','',0)
        sock.sendall(wire.format_can_ascii(msg,0)[0].replace(b' T ',b' R '))

    def run(self):
        try:
            conn,_=self.listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(.04)
            buffer=b''
            last_wind=0
            claimed=False
            try:
                while not self.stopped.is_set():
                    now=time.monotonic()
                    if now-last_wind >=.09:
                        angle=int(round((327.9 + self.offset - 9.0)/wire.R2D*10000))
                        payload=bytes([0, 0, 0])+angle.to_bytes(2,'little')+bytes([0xFA,0xFF,0xFF])
                        self.tx_frame(conn,wire.P_WIND,3,payload,2)
                        last_wind=now
                        if not claimed:
                            name=test_name(4444,self.manufacturer)
                            # Correct Simrad sensor has class=80 function=190; manufacturer matters for validation.
                            v=int.from_bytes(name,'little')
                            v &= ~(((1<<8)-1)<<40 | ((1<<7)-1)<<49)
                            v |= (190<<40) | (80<<49)
                            self.tx_frame(conn,wire.P_CLAIM,3,v.to_bytes(8,'little'),6)
                            if self.collision:
                                self.tx_frame(conn,wire.P_CLAIM,0x80,test_name(1),6)
                            claimed=True
                    try:
                        new=conn.recv(4096)
                    except socket.timeout:
                        new=None
                    if new == b'':
                        break
                    if new:
                        buffer += new
                        while b'\n' in buffer:
                            line, buffer=buffer.split(b'\n',1)
                            msg=wire.parse_line(line.decode('ascii').strip())
                            if not msg:
                                continue
                            self.tx.append(msg)
                            if msg.pgn==wire.P_CMD and msg.dst==3:
                                if msg.payload[3]==0xC0:
                                    self.offset=wire.angle_degrees(msg.payload[4:6])
                                if not self.no_echo:
                                    reply=bytes.fromhex('41 9F 31 C0')+wire.angle_raw(self.offset)+b'\xff\xff'
                                    self.tx_frame(conn,wire.P_ECHO,3,reply,3)
            except (OSError, BrokenPipeError):
                pass

    def stop(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=1)


class Tests(unittest.TestCase):
    def test_byte_decoders_and_bounds(self):
        m=wire.Message(wire.P_WIND,3,255,2,bytes.fromhex('00 00 00 25 DF FA FF FF'),'can-raw','',0)
        awa,aws=app.apparent_wind(m)
        self.assertAlmostEqual(awa,327.30,places=1)
        self.assertEqual(aws,0.0)
        self.assertIsNone(app.apparent_wind(wire.Message(wire.P_WIND,3,255,2,b'\x00\x00\x00\x00\x00\x00\xff\xff','can-raw','',0)))
        self.assertEqual(app.clamp_angle(181),180)
        self.assertEqual(app.clamp_angle(-181),-180)
        for n in (-180,-179,-1,0,1,179,180):
            self.assertAlmostEqual(wire.angle_degrees(wire.angle_raw(n)),n,delta=0.004)
        self.assertEqual(app.extract_name(test_name(12345, 1857))['manufacturer'],1857)
        self.assertEqual(app.raw_offset(wire.Message(wire.P_ECHO,3,255,3,bytes.fromhex('41 9F 31 C0 23 06 FF FF'),'','',0)),0x623)

    def test_name_decoding_and_friendly_device_summary(self):
        # Synthetic test NAME; never publish a real device's identifier.
        v = int.from_bytes(test_name(123456, 1857), 'little')
        v &= ~(((1 << 8) - 1) << 40 | ((1 << 7) - 1) << 49)
        v |= 190 << 40 | 80 << 49
        name = v.to_bytes(8, 'little')
        info = app.extract_name(name)
        self.assertEqual(info['unique'], 123456)
        self.assertEqual(info['manufacturer'], 1857)
        self.assertEqual(info['function'], 190)
        self.assertEqual(info['class'], 80)
        self.assertEqual(info['instance'], (v >> 32) & 0xff)
        self.assertEqual(info['system_instance'], (v >> 56) & 0xf)
        self.assertEqual(app.n2k_device_type(info), 'General transducer (SimNet TDU)')
        b = app.CalibratorBackend(reconnect=False)
        b.devices[3] = app.Sensor(src=3, name=name, awa=327.9, aws=3.1,
                                   last_wind=time.monotonic(), wind_count=1, offset=9.0)
        b.selected = 3
        screen = app.render_main(b)
        self.assertIn('Simrad wind sensor', screen)
        self.assertIn('Device ID: 123456', screen)
        self.assertIn('Instance:', screen)
        self.assertIn('DEVICE DETAILS', screen)
        self.assertIn('General transducer (SimNet TDU)', screen)
        self.assertIn('ISO NAME: ' + name.hex(' ').upper(), screen)
        self.assertNotIn('(I to hide)', screen)

    def test_ui_english_copy(self):
        b=app.CalibratorBackend(reconnect=False)
        screen=app.render_main(b)
        self.assertIn('DETECTED WIND SENSORS',screen)
        self.assertIn('DISCONNECTED',screen)
        self.assertIn('CAN gateway:', screen)
        self.assertNotIn('WGX-1:',screen)
        self.assertIn('Waiting for connection',screen)
        self.assertNotIn('WINDGEBER',screen)
        self.assertNotIn('VERBUNDEN',screen)

    def test_two_live_wind_cards_and_calibration_spacing(self):
        b=app.CalibratorBackend(reconnect=False)
        from wind_calibrator import Sensor
        name=test_name(4444, 1857)
        b.devices[3]=Sensor(src=3, name=name, awa=320.0, aws=5.5, last_wind=time.monotonic(), wind_count=1, offset=9.0)
        b.selected=3
        t=app.render_main(b)
        self.assertIn('AWA',t)
        self.assertIn('AWS',t)
        self.assertIn('m/s',t)
        # Two adjacent, stable-width tiles with a four-column gutter.
        self.assertIn('┌' + '─'*30 + '┐    ┌' + '─'*30 + '┐', t)
        self.assertRegex(t, r'AWA  ·  APPARENT WIND ANGLE[^\n]*AWS  ·  APPARENT WIND SPEED')
        self.assertIn('320.00°', t)
        self.assertNotIn('degrees', t.lower())
        self.assertIn('DEVICE DETAILS', t)
        self.assertIn('ISO NAME:', t)
        self.assertIn('5.50 m/s', t)
        self.assertIn('10.69 kn', t)
        self.assertRegex(t, r'└─{30}┘    └─{30}┘\n\n  WIND ANGLE CALIBRATION')
        self.assertNotIn('WGX-1:', t)
        for line in t.splitlines():
            if line.startswith(('  ┌', '  │', '  └')):
                self.assertEqual(len(line), 70)  # two 32-column tiles, 4 gap, 2 indent
        # Placeholder values fit inside both tiles without shifting the grid.
        b.devices[3].last_wind = time.monotonic() - 4
        stale = app.render_main(b)
        self.assertIn('--.--°', stale)
        self.assertIn('--.-- m/s', stale)
        self.assertIn('--.-- kn', stale)
        for line in stale.splitlines():
            if line.startswith(('  ┌', '  │', '  └')):
                self.assertEqual(len(line), 70)

    def test_gateway_startup_prefilled_defaults(self):
        calls = []
        def fake_prompt(message, *, default):
            calls.append((message, default))
            return default
        result = app.prompt_gateway_settings('192.168.4.1', 20000, fake_prompt)
        self.assertEqual(result, ('192.168.4.1', 20000, True))
        self.assertEqual(calls, [('Gateway IP / hostname: ', '192.168.4.1'),
                                 ('Gateway TCP port: ', '20000'),
                                 ('Enable logging? [Y/n]: ', 'Y')])

    def test_gateway_startup_edit_and_revalidate(self):
        answers = iter(['bad host', '10.0.0.12', 'bad', '65536', '30001', 'maybe', 'n'])
        defaults = []
        def fake_prompt(message, *, default):
            defaults.append(default)
            return next(answers)
        result = app.prompt_gateway_settings('192.168.4.1', 20000, fake_prompt)
        self.assertEqual(result, ('10.0.0.12', 30001, False))
        self.assertEqual(defaults, ['192.168.4.1', '192.168.4.1',
                                    '20000', '20000', '20000', 'Y', 'Y'])

    def test_startup_logging_flag_defaults_to_no(self):
        calls = []
        def fake_prompt(message, *, default):
            calls.append((message, default))
            return default
        actual = app.prompt_gateway_settings('127.0.0.1', 20000, fake_prompt,
                                             logging_enabled=False)
        self.assertEqual(actual, ('127.0.0.1', 20000, False))
        self.assertEqual(calls[-1], ('Enable logging? [Y/n]: ', 'N'))

    def test_logging_disabled_creates_no_directory_and_keeps_events(self):
        wg = FakeGateway()
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'never-created'
            backend = app.CalibratorBackend('127.0.0.1', wg.port,
                                            log_dir=dest, reconnect=False,
                                            logging_enabled=False)
            self.assertIsNone(backend.log_path)
            backend.start()
            try:
                wait_for(lambda: any(x['offset'] is not None
                                     for x in backend.snapshot()['devices']), 7)
                self.assertFalse(dest.exists())
                self.assertIsNone(backend.snapshot()['log_path'])
                self.assertTrue(backend.snapshot()['events'])
                self.assertTrue(backend.snapshot()['status'])
            finally:
                backend.stop()
                wg.stop()
            self.assertFalse(dest.exists())

    def test_logging_enabled_uses_requested_directory(self):
        wg = FakeGateway()
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'logs'
            backend = app.CalibratorBackend('127.0.0.1', wg.port,
                                            log_dir=dest, reconnect=False,
                                            logging_enabled=True)
            backend.start()
            try:
                wait_for(lambda: bool(backend.snapshot()['devices']), 5)
                self.assertEqual(backend.log_path.parent, dest)
                self.assertTrue(backend.log_path.is_file())
                self.assertIn('Connected to CAN gateway',
                              backend.log_path.read_text(encoding='utf-8'))
            finally:
                backend.stop()
                wg.stop()

    def test_default_log_directory_is_within_application(self):
        backend = app.CalibratorBackend(reconnect=False)
        self.assertEqual(backend.log_path.parent,
                         Path(app.__file__).resolve().parent / 'logs')
        disabled = app.CalibratorBackend(reconnect=False, logging_enabled=False)
        self.assertIsNone(disabled.log_path)

    def test_live_read_set_and_no_claims(self):
        wg=FakeGateway()
        with TemporaryDirectory() as directory:
            b=app.CalibratorBackend('127.0.0.1',wg.port,0x80,directory,reconnect=False)
            b.start()
            try:
                sensor=wait_for(lambda: next((d for d in b.snapshot()['devices'] if d['verified'] and d['offset'] is not None),None),7)
                self.assertEqual(sensor['src'],3)
                self.assertAlmostEqual(sensor['offset'],9.0012,places=2)
                self.assertTrue('AWA' in app.render_main(b))
                b.nudge(1)
                self.assertEqual(b.snapshot()['desired'],10)
                b.prepare_set()
                self.assertIsNotNone(b.snapshot()['confirm'])
                b.confirm_set(False)
                self.assertEqual(sum(x.pgn==wire.P_CMD and x.payload[3]==0xC0 for x in wg.tx),0)
                b.prepare_entry()
                self.assertEqual(b.snapshot()['entry'],'')
                for char in '10':
                    b.input_key(char)
                b.input_key('enter')
                self.assertEqual(b.snapshot()['desired'],10)
                b.prepare_set()
                b.confirm_set(True)
                wait_for(lambda: b.snapshot()['devices'][0]['offset'] is not None and abs(b.snapshot()['devices'][0]['offset']-10)<.01,4)
                wait_for(lambda: abs(wg.offset-10)<.02,4)
                self.assertEqual(sum(x.pgn==wire.P_CLAIM for x in wg.tx),0)
                b.nudge(-1)
                b.prepare_set()
                b.confirm_set(True)
                wait_for(lambda: abs(wg.offset-9)<.02,4)
                self.assertTrue(b.snapshot()['devices'][0]['verified'])
                self.assertTrue(Path(b.log_path).is_file())
            finally:
                b.stop(); wg.stop()

    def test_read_only_unknown_manufacturer(self):
        wg=FakeGateway(manufacturer=2046)
        with TemporaryDirectory() as directory:
            b=app.CalibratorBackend('127.0.0.1',wg.port,0x80,directory,reconnect=False)
            b.start()
            try:
                wait_for(lambda: len(b.snapshot()['devices'])>0,3)
                time.sleep(2.6)
                self.assertFalse(b.snapshot()['devices'][0]['verified'])
                self.assertFalse(any(m.pgn==wire.P_CMD for m in wg.tx))
                b.nudge(1);b.prepare_set()
                self.assertIsNone(b.snapshot()['confirm'])
            finally:
                b.stop();wg.stop()

    def test_collision_blocks_tx(self):
        wg=FakeGateway(collision=True)
        with TemporaryDirectory() as directory:
            b=app.CalibratorBackend('127.0.0.1',wg.port,0x80,directory,reconnect=False)
            b.start()
            try:
                wait_for(lambda: len(b.snapshot()['devices'])>0,3)
                time.sleep(2.8)
                self.assertFalse(b.snapshot()['tx_permitted'])
                self.assertFalse(any(m.pgn==wire.P_CMD for m in wg.tx))
            finally:
                b.stop();wg.stop()


    def test_device_identity_change_invalidates_calibration_and_drops_stale_set(self):
        import io
        now = time.monotonic()
        old_name = test_name(100101, 1857)
        new_name = test_name(100102, 1857)
        backend = app.CalibratorBackend(reconnect=False)
        backend.connected = True
        backend.tx_permitted = True
        backend.selected = 3
        backend.desired = 10
        backend.confirm = (3, 10, 9.0012, old_name)
        backend.devices[3] = app.Sensor(src=3, name=old_name, wind_count=1,
                                        last_wind=now, awa=327.9, aws=1.5,
                                        offset=9.0012, offset_raw=0x623,
                                        pending_set_raw=int.from_bytes(wire.angle_raw(10), 'little', signed=True),
                                        pending_set_since=now)
        backend.queue.put(('set', 3, old_name, 10, 9.0012))
        backend._handle_message(wire.Message(wire.P_CLAIM, 3, 255, 6,
                                             new_name, 'can-raw', '', now + .1))
        sensor = backend.devices[3]
        self.assertEqual(sensor.name, new_name)
        self.assertIsNone(sensor.offset)
        self.assertIsNone(sensor.pending_set_raw)
        self.assertEqual(sensor.wind_count, 0)
        self.assertIsNone(backend.confirm)
        self.assertIsNone(backend.desired)
        class CaptureSocket:
            def __init__(self): self.sent = []
            def sendall(self, data): self.sent.append(data)
        sock = CaptureSocket()
        backend._log = io.StringIO()
        backend._process_commands(sock)
        self.assertEqual(sock.sent, [], 'Old identity must not have a queued write replayed on new device')

    def test_old_reply_does_not_confirm_set_and_independent_readback_is_required(self):
        import io
        now = time.monotonic()
        name = test_name(552233, 1857)
        backend = app.CalibratorBackend(reconnect=False)
        backend.connected = True
        backend.tx_permitted = True
        backend.selected = 3
        backend.desired = 10
        backend._log = io.StringIO()
        raw = int.from_bytes(wire.angle_raw(10), 'little', signed=True)
        backend.devices[3] = app.Sensor(src=3, name=name, wind_count=1,
                                        last_wind=now, offset=9.0012, offset_raw=0x623,
                                        pending_set_raw=raw, pending_set_since=now)
        def echo(val, timestamp):
            return wire.Message(wire.P_ECHO, 3, 255, 3,
                                bytes.fromhex('41 9F 31 C0') + wire.angle_raw(val) + b'\xff\xff',
                                'can-raw', '', timestamp)
        backend._handle_message(echo(10, now - .01))
        self.assertAlmostEqual(backend.devices[3].offset, 9.0012)
        self.assertTrue(backend.queue.empty())
        backend._handle_message(echo(10, time.monotonic()))
        self.assertAlmostEqual(backend.devices[3].offset, 9.0012)
        self.assertTrue(backend.devices[3].pending_verify_queued)
        class CaptureSocket:
            def __init__(self): self.sent = []
            def sendall(self, data): self.sent.append(data)
        sock = CaptureSocket()
        # Simulate coarse monotonic-clock resolution (seen on Windows):
        # sending a verification request can have the same timestamp as SET.
        with patch.object(app.time, 'monotonic', return_value=now):
            backend._process_commands(sock)
        self.assertEqual(len(sock.sent), 1, 'A fresh PGN 61184 READ must follow the echo')
        read = wire.parse_line(sock.sent[0].decode().strip())
        self.assertEqual(read.pgn, wire.P_CMD)
        self.assertEqual(read.payload, wire.direct_message(0))
        sent_at = backend.devices[3].pending_verify_sent_at
        self.assertEqual(sent_at, now)
        backend._handle_message(echo(10, sent_at - 0.001))
        self.assertAlmostEqual(backend.devices[3].offset, 9.0012)
        self.assertIsNotNone(backend.devices[3].pending_set_raw)
        # Synthetic receive times must be on either side of the send marker;
        # never rely on the test machine's real clock advancing between calls.
        backend._handle_message(echo(10, sent_at + 0.001))
        self.assertAlmostEqual(backend.devices[3].offset, 10, places=2)
        self.assertIsNone(backend.devices[3].pending_set_raw)
        self.assertIn('READBACK VERIFIED', backend.status)

    def test_readback_mismatch_does_not_claim_success(self):
        now = time.monotonic()
        b = app.CalibratorBackend(reconnect=False)
        b.selected = 3
        b.devices[3] = app.Sensor(src=3, name=test_name(32,1857),
                                  wind_count=1, last_wind=now, offset=9.0,
                                  pending_set_raw=int.from_bytes(wire.angle_raw(10),'little',signed=True),
                                  pending_set_since=now-.1,
                                  pending_verify_sent_at=now-.05)
        bad_reply = wire.Message(wire.P_ECHO,3,255,3,
                                 bytes.fromhex('41 9F 31 C0')+wire.angle_raw(9)+b'\xff\xff',
                                 'can-raw','',time.monotonic())
        b._handle_message(bad_reply)
        self.assertIsNone(b.devices[3].offset)
        self.assertIsNone(b.devices[3].pending_set_raw)
        self.assertIn('READBACK MISMATCH', b.status)

    def test_sensor_window_follows_selection_beyond_twelve_sources(self):
        b = app.CalibratorBackend(reconnect=False)
        for src in range(1, 20):
            b.devices[src] = app.Sensor(src=src, name=test_name(5000+src,1857),
                                         wind_count=1, last_wind=time.monotonic(), offset=9.0)
        b.selected = 9
        screen = app.render_main(b)
        self.assertIn('▶ 0x09', screen)
        b.selected = 19
        self.assertIn('▶ 0x13', app.render_main(b))


if __name__=='__main__':unittest.main()
