"""
Ljudet från SVT-klipp (Rapport, Sportnytt, höjdpunkter) spelas av Sonos själv.

Pi:n gör om klippets ljud till en vanlig mp3-ström (ffmpeg, som en webbradio) och ber högtalaren
spela den. Sonos hämtar ljudet med sin egen buffert, så det blir inga avbrott, och volymen rörs
aldrig. Skärmen startar bilden (tyst) i samma ögonblick som Sonos har börjat spela.

Kommandon från sidan:   clip-sonos:start:{"room": "...", "id": "<svtId>", "cid": "<klippets id>"}
                        clip-sonos:stop
Sidan följer läget på /api/clipstate: starting -> playing (eller error).
"""
import http.server
import json
import socket
import subprocess
import threading
import time
import urllib.request

PORT = 8765


class ClipStream:
    def __init__(self, hub, log):
        self.hub, self.sonos, self.log = hub, hub.sonos, log
        self.lock = threading.Lock()
        self.cur = None             # {"cid", "url", "room", "dev", "snap", "procs"}
        self.q = None
        self._server()

    # ---- ljudströmmen ----
    def _server(self):
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.end_headers()

            def do_GET(self):
                cur = me.cur
                if not cur or self.path != "/clip/%s.mp3" % cur["cid"]:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", cur["url"], "-map", "0:a:0", "-vn",
                                      "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                cur["procs"].append(p)
                try:
                    while me.cur is cur:
                        chunk = p.stdout.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    try:
                        p.kill()
                    except Exception:
                        pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            srv = S(("0.0.0.0", PORT), H)
            threading.Thread(target=srv.serve_forever, daemon=True, name="klippström").start()
        except OSError as e:
            self.log("(varning: klippljudet: kan inte öppna port %d: %s)" % (PORT, e))

    def _my_ip(self, towards):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((towards, 1400))
            return s.getsockname()[0]
        finally:
            s.close()

    def _state(self, cid, state, msg=""):
        try:
            self.hub.post_json("/api/clipstate", {"cid": cid, "state": state, "msg": msg, "at": time.time()})
        except Exception:
            pass

    # ---- start och stopp ----
    def start(self, o):
        cid, room, vid = str(o.get("cid") or ""), str(o.get("room") or ""), str(o.get("id") or "")
        self.stop(restore=False)
        dev = self.sonos.device(room)
        if dev is None or not vid:
            self._state(cid, "error", "hittar inte högtalaren")
            return
        try:
            with urllib.request.urlopen("https://api.svt.se/video/" + urllib.request.quote(vid), timeout=10) as r:
                j = json.loads(r.read().decode("utf-8"))
            refs = j.get("videoReferences") or []
            ref = next((x for x in refs if x.get("format") == "hls"), None) or next((x for x in refs if str(x.get("format", "")).startswith("hls")), None)
            if not ref:
                raise RuntimeError("klippet saknar HLS")
        except Exception as e:
            self.log("(varning: klippljudet: SVT: %s)" % e)
            self._state(cid, "error", "SVT svarade inte")
            return
        co = dev.group.coordinator
        snap = None
        try:
            from soco.snapshot import Snapshot
            snap = Snapshot(co)
            snap.snapshot()
        except Exception:
            snap = None
        cur = {"cid": cid, "url": ref["url"], "room": room, "dev": co, "snap": snap, "procs": []}
        self.cur = cur
        try:
            uri = "x-rincon-mp3radio://%s:%d/clip/%s.mp3" % (self._my_ip(co.ip_address), PORT, cid)
            co.play_uri(uri, title=o.get("title") or "Blaupunkt", force_radio=False)
        except Exception as e:
            self.log("(varning: klippljudet: Sonos: %s)" % e)
            self._state(cid, "error", "Sonos svarade inte")
            self.cur = None
            return
        self.log('  -> klippljud: Sonos "%s" spelar klippets ljud' % co.player_name)
        # vänta tills Sonos faktiskt spelar: då startar skärmen bilden
        t0 = time.time()
        while self.cur is cur and time.time() - t0 < 20:
            try:
                if co.get_current_transport_info().get("current_transport_state") == "PLAYING":
                    self._state(cid, "playing")
                    return
            except Exception:
                pass
            time.sleep(0.15)
        if self.cur is cur:
            self._state(cid, "error", "Sonos kom inte igång")

    def stop(self, restore=True, cur=None):
        if cur is None:
            cur, self.cur = self.cur, None
        if not cur:
            return
        for p in cur["procs"]:
            try:
                p.kill()
            except Exception:
                pass
        if not restore:
            return
        try:
            cur["dev"].stop()
        except Exception:
            pass
        snap = cur["snap"]
        if snap is not None:
            try:
                snap._restore_volume = lambda *a, **k: None       # volymen är din: rörs inte
                was = str(getattr(snap, "transport_state", "")).upper()
                snap.restore(fade=False)
                if was != "PLAYING":
                    try:
                        cur["dev"].stop()
                    except Exception:
                        pass
            except Exception as e:
                self.log("(varning: klippljudet: kunde inte återställa Sonos: %s)" % e)
        self.log("  -> klippljud: klart")

    def command(self, rest):
        what, _, arg = rest.partition(":")
        if self.q is None:
            import queue
            self.q = queue.Queue()
            threading.Thread(target=self._worker, daemon=True, name="klippljud").start()
        if what == "stop":
            # släpp klippet direkt (en start som väntar på Sonos avbryts), städa i tur och ordning
            old, self.cur = self.cur, None
            self.q.put(("stopold", old))
            return
        self.q.put((what, arg))

    def _worker(self):
        while True:
            what, arg = self.q.get()
            try:
                if what == "start":
                    self.start(json.loads(arg or "{}"))
                elif what == "stopold" and arg:
                    self.stop(cur=arg)
            except Exception as e:
                self.log("(varning: klippljudet: %s)" % e)
