#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""PTY smoke test on Unix: render the actual terminal UI against fake CAN gateway."""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from tempfile import TemporaryDirectory

from tests.test_calibrator import FakeGateway

wg=FakeGateway()
logs=TemporaryDirectory()
master,slave=pty.openpty()
fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',42,112,0,0))
env=dict(os.environ,TERM='xterm-256color')
proc=subprocess.Popen([sys.executable,'wind_calibrator.py','--host','127.0.0.1','--port',str(wg.port),'--once','--no-prompt','--log-dir',logs.name],
                      stdin=slave,stdout=slave,stderr=slave,env=env)
os.close(slave)
buf=bytearray()
# Wait until the auto-read has completed; attempting to apply before the
# sensor offset is known makes the smoke test timing-dependent.
start=time.monotonic()
while time.monotonic()-start<10:
    ready,_,_=select.select([master],[],[],.2)
    if ready:
        try: buf+=os.read(master,65536)
        except OSError:break
    if b'Current offset:' in buf and b'+9.001' in buf:
        break
assert b'+9.001' in buf, 'Sensor offset was not read before the UI test'
# Identity diagnostics are visible by default (no I toggle or CAN writes).
details_visible=b'DEVICE DETAILS' in buf and b'ISO NAME:' in buf
# Interactive smoke: +1°, apply, confirm, then restore to +9°.
os.write(master,b'\x1b[C')  # right arrow
time.sleep(.12)
os.write(master,b'a')
time.sleep(.12)
os.write(master,b'y')
start=time.monotonic()
while time.monotonic()-start<4 and abs(wg.offset-10)>0.05:
    time.sleep(.08)
changed=abs(wg.offset-10)<.05
os.write(master,b'\x1b[D')  # left arrow
time.sleep(.12)
os.write(master,b'a')
time.sleep(.12)
os.write(master,b'y')
start=time.monotonic()
while time.monotonic()-start<4 and abs(wg.offset-9)>0.05:
    time.sleep(.08)
restored=abs(wg.offset-9)<.05
os.write(master,b'q')
try: proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill();proc.wait()
ready,_,_=select.select([master],[],[],.5)
if ready:
    try:buf+=os.read(master,65536)
    except OSError:pass
os.close(master)
wg.stop()
logs.cleanup()
print('EXITS',proc.returncode,'BYTES',len(buf),'AWAITED_READ',any(x.pgn==61184 for x in wg.tx),'DETAILS',details_visible,'CHANGED',changed,'RESTORED',restored)
print('BEGIN_SNIPPET',bytes(buf).decode('utf-8','replace')[-1000:])
assert proc.returncode==0
assert details_visible
assert changed and restored
assert any(x.pgn==61184 for x in wg.tx)
assert b'WIND CALIBRATOR' in buf
assert b'APPARENT WIND ANGLE' in buf and b'APPARENT WIND SPEED' in buf
