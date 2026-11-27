"""
Skärmfärg: tar emot färgerna från datorns skärm (programmet skarmfarg.pyw på datorn) över
hemnätverket, UDP-port 8766.

Datorn skickar ett litet paket: {"r": {"all": [r, g, b, ljus, färgmängd], "left": [...], ...}}
(r, g, b 0-255, ljus 0-1). Hubben svarar "on" när någon lampa följer skärmen just nu, annars
"off"; då skickar datorn bara ett paket varannan sekund så att den nästan inte gör något.
"""
import json
import socket
import threading
import time

PORT = 8766
REGIONS = ("all", "left", "right", "top", "bottom", "center")


class ScreenFeed:
    def __init__(self, log, port=PORT):
        self.log = log
        self.lock = threading.Lock()
        self.latest = {}            # område -> (r, g, b, ljus)
        self.at = 0.0               # när senaste paketet kom
        self.sender = None
        self.wanted = lambda: False  # sätts av Effects: följer någon lampa skärmen?
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", port))
            threading.Thread(target=self._loop, daemon=True, name="skärmfärg").start()
        except OSError as e:
            self.sock = None
            log("(varning: skärmfärg: kan inte lyssna på port %d: %s)" % (port, e))

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                time.sleep(1)
                continue
            try:
                j = json.loads(data.decode("utf-8"))
                r = j.get("r") or {}
                vals = {}
                for k in REGIONS:
                    v = r.get(k)
                    if isinstance(v, list) and len(v) >= 4:
                        vals[k] = (float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]) if len(v) > 4 else 1.0)
                if vals:
                    with self.lock:
                        self.latest, self.at = vals, time.time()
                    if self.sender != addr[0]:
                        self.sender = addr[0]
                        self.log("  -> skärmfärg: tar emot från %s" % addr[0])
            except (ValueError, TypeError, AttributeError):
                pass
            try:
                self.sock.sendto(b"on" if self.wanted() else b"off", addr)
            except OSError:
                pass

    def get(self, region):
        """(r, g, b, ljus) för ett område och hur gammalt värdet är i sekunder."""
        with self.lock:
            v = self.latest.get(region) or self.latest.get("all")
            return v, (time.time() - self.at) if self.at else 1e9
