"""
Ljudet från klipp och sändningar på skärmen (highlights, Rapport …) till en Sonos-högtalare.

Pi:n skickar sitt ljud till högtalaren med AirPlay (PipeWires RAOP-modul, se README). När ett
klipp startar flyttas Chromiums ljud dit; när det är slut flyttas det tillbaka till skärmen.
AirPlay tar över högtalaren, så det som spelades innan sparas först (SoCo-snapshot) och sätts
igång igen efteråt, med samma volym.

Kommandon (från sidan): clip-audio:on:<rum>   clip-audio:off
"""
import json
import os
import subprocess
import threading
import time


class ClipAudio:
    def __init__(self, sonos, log):
        self.sonos, self.log = sonos, log
        self.lock = threading.Lock()
        self.active = None          # {"room", "sink", "before", "snap", "dev", "vol", "keep"}
        self.pi_vol = 40
        self.pts = {}               # AirPlay-utgång -> {PipeWire-%: uppmätt Sonos-volym}

    # ---- PipeWire via pactl ----
    def _env(self):
        uid = os.getuid()
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
        env.setdefault("PULSE_SERVER", "unix:/run/user/%d/pulse/native" % uid)
        return env

    def _pactl(self, *args):
        r = subprocess.run(["pactl"] + list(args), capture_output=True, text=True, timeout=8, env=self._env())
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "pactl misslyckades").strip())
        return r.stdout

    def sinks(self):
        try:
            return json.loads(self._pactl("-f", "json", "list", "sinks"))
        except (ValueError, RuntimeError):
            out, cur = [], None
            for line in self._pactl("list", "sinks").splitlines():
                line = line.strip()
                if line.startswith("Sink #"):
                    cur = {"index": int(line[6:]), "name": "", "description": ""}
                    out.append(cur)
                elif cur is not None and line.startswith("Name:"):
                    cur["name"] = line[5:].strip()
                elif cur is not None and line.startswith("Description:"):
                    cur["description"] = line[12:].strip()
            return out

    def find_sink(self, room):
        """AirPlay-utgången för ett Sonos-rum. Namnet innehåller högtalarens id, beskrivningen rummet."""
        key = (room or "").strip().lower()
        uid = ""
        dev = self.sonos.device(room) if room else None
        if dev is not None:
            uid = (getattr(dev, "uid", "") or "").replace("RINCON_", "")[:12].lower()
        for s in self.sinks():
            name, desc = str(s.get("name", "")).lower(), str(s.get("description", "")).lower()
            if not name.startswith("raop"):
                continue
            if (uid and uid in name) or (key and key in desc):
                return s.get("name")
        return None

    def _guess(self, sink, target):
        """Bästa gissning på PipeWire-% för en Sonos-volym, utifrån det som mätts upp förut."""
        known = sorted(self.pts.get(sink, {}).items())
        lower = [p for p in known if p[1] <= target]
        upper = [p for p in known if p[1] >= target]
        if lower and upper:
            (p1, v1), (p2, v2) = lower[-1], upper[0]
            return p1 if v2 == v1 else round(p1 + (target - v1) * (p2 - p1) / (v2 - v1))
        if upper:
            return max(1, upper[0][0] - 10)
        if lower:
            return min(100, lower[-1][0] + 10)
        return 40

    def _move_all(self, sink):
        n = 0
        for line in self._pactl("list", "short", "sink-inputs").splitlines():
            parts = line.split()
            if parts:
                try:
                    self._pactl("move-sink-input", parts[0], sink)
                    n += 1
                except RuntimeError:
                    pass
        return n

    # ---- AirPlay-utgångarna: nya för varje klipp ----
    # PipeWires AirPlay-utgång kopplar inte alltid upp igen efter att ha varit i vila: andra klippet
    # efter en omstart blev tyst. Därför skapas utgångarna bara medan ett klipp visas, av en egen
    # pw-cli-process som avslutas efteråt; nästa klipp får en helt ny anslutning.
    def _start_discover(self):
        self._stop_discover()
        try:
            self.proc = subprocess.Popen(["pw-cli"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=self._env(), text=True)
            lat = int((self.sonos.cfg or {}).get("clip_latency_ms", 2000))
            self.proc.stdin.write("load-module libpipewire-module-raop-discover { raop.latency.ms = %d raop.disable-volume = true "
                                  "stream.rules = [ { matches = [ { raop.ip = \"~.*\" } ] actions = { create-stream = "
                                  "{ raop.latency.ms = %d raop.disable-volume = true } } } ] }\n" % (lat, lat))
            self.proc.stdin.flush()
        except Exception as e:
            self.proc = None
            self.log("(varning: klippljudet: kunde inte starta pw-cli: %s)" % e)

    def _stop_discover(self):
        p, self.proc = getattr(self, "proc", None), None
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ---- på och av ----
    def _on_airplay(self, a):
        """Spelar högtalaren fortfarande Pi:ns AirPlay-ljud? Trycker någon "nästa" eller väljer musik
        på Sonos släpper den AirPlay, och då ska nästa klipp koppla upp ljudet på nytt."""
        dev = a.get("dev")
        if dev is None:
            return True
        try:
            co = dev.group.coordinator
            uri = str(co.get_current_track_info().get("uri") or "").lower()
            st = str(co.get_current_transport_info().get("current_transport_state") or "").upper()
            return ("airplay" in uri or "x-sonos-vli" in uri) and st in ("PLAYING", "TRANSITIONING")
        except Exception:
            return True            # kan inte avgöra: låt det vara

    def on(self, room):
        with self.lock:
            old = None
            if self.active:
                a = self.active
                if a["room"] == room and self._still_playing(a) and self._on_airplay(a):
                    return
                # Ljudet har tappats (Sonos bytte till annat, eller sidan laddades om): koppla upp
                # på nytt, men behåll det som spelades före första klippet så att det kommer tillbaka.
                self.log('  -> klippljud: högtalaren spelar inte längre skärmens ljud, kopplar upp igen')
                old, self.active = a, None
                try:
                    if a.get("before"):
                        self._pactl("set-default-sink", a["before"])
                        self._move_all(a["before"])
                except Exception:
                    pass
                self._stop_discover()
                time.sleep(1.0)
            self._start_discover()
            sink, t0 = None, time.time()
            while time.time() - t0 < 10:          # högtalarna hittas på nätet efter någon sekund
                try:
                    sink = self.find_sink(room)
                except Exception as e:
                    self.log("(varning: klippljudet: kan inte läsa ljudutgångarna: %s)" % e)
                    break
                if sink:
                    break
                time.sleep(0.4)
            if not sink:
                self._stop_discover()
                self.log('(varning: klippljudet: hittar ingen AirPlay-utgång för "%s"; se README om PipeWire och AirPlay)' % room)
                return
            snap, dev = None, self.sonos.device(room)
            keep = None
            if old is not None and old.get("room") == room:
                snap, keep = old.get("snap"), old.get("keep")
            elif dev is not None:
                try:
                    keep = int(dev.volume)          # högtalarens volym innan klippet: den ska stå kvar
                except Exception:
                    keep = None
                try:
                    from soco.snapshot import Snapshot
                    snap = Snapshot(dev.group.coordinator)
                    snap.snapshot()
                except Exception as e:
                    self.log("(varning: klippljudet: kunde inte spara vad Sonos spelade: %s)" % e)
                    snap = None
            try:
                before = self._pactl("get-default-sink").strip()
                try:
                    self._pactl("suspend-sink", sink, "0")     # ny AirPlay-anslutning
                except Exception:
                    pass
                self.pi_vol = int((self.sonos.cfg or {}).get("clip_pi_volume", 40))
                self._pactl("set-sink-volume", sink, "%d%%" % self.pi_vol)
                self._pactl("set-default-sink", sink)
                n = self._move_all(sink)
            except Exception as e:
                self.log("(varning: klippljudet: kunde inte byta ljudutgång: %s)" % e)
                self._stop_discover()
                return
            self.active = {"room": room, "sink": sink, "before": before, "snap": snap, "dev": dev, "at": time.time()}
            # Volymen under klippet: Pi:ns ljud börjar på 40 % (clip_pi_volume) och styrs med
            # volymratten/-knapparna så länge klippet visas (se volume()); efteråt styr de Sonos igen.
            self.active["vol"] = self.pi_vol
            self.active["keep"] = keep
            self.log('  -> klippljud till Sonos "%s" (%s, %d ljudströmmar flyttade)' % (room, sink, n))

    def off(self):
        with self.lock:
            a, self.active = self.active, None
        if not a:
            return
        try:
            if a["before"]:
                self._pactl("set-default-sink", a["before"])
                self._move_all(a["before"])
        except Exception as e:
            self.log("(varning: klippljudet: kunde inte flytta tillbaka ljudet: %s)" % e)
        # stäng AirPlay helt: utgången försvinner och högtalaren släpps
        self._stop_discover()
        snap = a["snap"]
        time.sleep(1.5)                # låt AirPlay släppa högtalaren först
        try:
            if snap is not None and str(getattr(snap, "transport_state", "")).upper() == "PLAYING":
                try:
                    snap.restore(fade=False)       # något spelade: sätt igång det igen, med volymen från innan
                except Exception as e:
                    # "invalid timestamp": positionen gick inte att läsa (t.ex. Spotify); börja om låten
                    if "timestamp" not in str(e):
                        raise
                    snap.track_position = "0:00:00"
                    snap.restore(fade=False)
            elif a.get("keep") is not None and a["dev"] is not None:
                a["dev"].volume = int(a["keep"])   # tillbaka till volymen från innan klippet
        except Exception as e:
            if "701" in str(e):
                pass        # högtalaren kan inte "spela" i sitt läge just nu (t.ex. redan bytt till annat): inget att göra
            else:
                self.log("(varning: klippljudet: kunde inte sätta igång Sonos igen: %s)" % e)
        self.log("  -> klippljud tillbaka till skärmen")

    def volume(self, room, value=None, rel=None):
        """Volymratten och -knapparna medan ett klipp visas: de styr Pi:ns ljud (som AirPlay för
        vidare till högtalaren). Svarar False när inget klipp visas, då går kommandot till Sonos."""
        a = self.active
        if not a:
            return False
        if room and a.get("room") and str(room).strip().lower() != str(a["room"]).strip().lower():
            return False                         # ett annat rum än klippets: till Sonos som vanligt
        if not self._still_playing(a):
            # klippet är inte igång längre (sidan laddades om, "av" kom aldrig fram): släpp det,
            # annars styr ratten Pi:ns ljud i stället för Sonos för all framtid
            self.log("  -> klippljud: inget klipp spelar längre, ratten styr Sonos igen")
            self.command("off")
            self.active = None
            return False
        cur = int(a.get("vol", 40))
        new = cur + int(rel) if rel is not None else int(value if value is not None else cur)
        new = max(0, min(100, new))
        a["vol"] = new
        try:
            self._pactl("set-sink-volume", a["sink"], "%d%%" % new)
        except Exception as e:
            self.log("(varning: klippljudet: volymen: %s)" % e)
        return True

    def _still_playing(self, a):
        if time.time() - a.get("at", time.time()) > 4 * 3600:
            return False
        try:
            idx = None
            for sk in self.sinks():
                if sk.get("name") == a["sink"]:
                    idx = sk.get("index")
            if idx is None:
                return False
            for line in self._pactl("list", "short", "sink-inputs").splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[1] == str(idx):
                    return True
            return False
        except Exception:
            return True        # kan inte avgöra: tro på klippet

    def command(self, rest):
        """Körs i tur och ordning i en egen tråd: ett "av" som sätter igång Sonos igen får aldrig
        hamna efter nästa klipps "på"."""
        what, _, room = rest.partition(":")
        if not hasattr(self, "q"):
            import queue
            self.q = queue.Queue()
            threading.Thread(target=self._worker, daemon=True, name="klippljud").start()
        self.q.put((what, room))

    def _worker(self):
        while True:
            what, room = self.q.get()
            try:
                if what == "on":
                    if self.active and self.active["room"] != room:
                        self.off()
                    self.on(room)
                else:
                    self.off()
            except Exception as e:
                self.log("(varning: klippljudet: %s)" % e)
