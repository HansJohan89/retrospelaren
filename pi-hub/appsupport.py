"""
Det hubben gör åt appen (app.html):

  Skärmbild   screen-shot          tar en bild av Blaupunkt-skärmen med grim och skickar den till
                                   servern (appen visar den under fliken Skärmen). Högst var 2:a sekund.
  Rummet      sonos-focus:<rum>    appens Musik-flik tittar på det rummet: i en minut skickas
                                   låt, position, längd, volym, shuffle/repeat och gruppen var 1,5 s.
  Timer       timer:{"id","min","name","room"}   timer-off:<id>
                                   ringer med en klocka på Sonos-rummet (det som spelades
                                   sätts igång igen efteråt) och syns i appen och på Nest Hub.
"""
import glob
import json
import os
import subprocess
import threading
import time


class AppSupport:
    def __init__(self, hub, log):
        self.hub, self.sonos, self.log = hub, hub.sonos, log
        self.shot_lock = threading.Lock()
        self.last_shot = 0.0
        self.focus, self.focus_until = None, 0.0
        self.timers = {}                 # id -> {"id", "name", "end", "room", "t": Timer}
        threading.Thread(target=self._room_loop, daemon=True, name="app-rum").start()

    # ---------- skärmbilden ----------
    def _wayland_env(self):
        env = dict(os.environ)
        uid = os.getuid()
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
        if not env.get("WAYLAND_DISPLAY"):
            socks = sorted(p for p in glob.glob(os.path.join(env["XDG_RUNTIME_DIR"], "wayland-*")) if not p.endswith(".lock"))
            if socks:
                env["WAYLAND_DISPLAY"] = os.path.basename(socks[0])
        return env

    def screenshot(self):
        if not self.shot_lock.acquire(blocking=False):
            return
        try:
            if time.time() - self.last_shot < 2:
                return
            self.last_shot = time.time()
            try:
                r = subprocess.run(["grim", "-s", "0.5", "-t", "jpeg", "-q", "72", "-"], capture_output=True, timeout=10, env=self._wayland_env())
            except FileNotFoundError:
                self.log("(varning: skärmbilden: grim saknas, kör uppdatera-pi.bat)")
                self.last_shot = time.time() + 600
                return
            if r.returncode != 0 or not r.stdout:
                self.log("(varning: skärmbilden: %s)" % (r.stderr.decode("utf-8", "replace").strip()[:200] or "grim misslyckades"))
                return
            self.hub._request("POST", "/api/screenshot", r.stdout, timeout=15)
        except Exception as e:
            self.log("(varning: skärmbilden: %s)" % e)
        finally:
            self.shot_lock.release()

    # ---------- rummet appen tittar på ----------
    def set_focus(self, room):
        self.focus, self.focus_until = room, time.time() + 60

    def room_state(self, room):
        out = {"room": room, "state": "", "playing": False, "title": "", "artist": "", "album": "", "art": "",
               "pos": 0, "dur": 0, "vol": None, "tid": "", "mute": False, "shuffle": False, "repeat": "off", "kind": "", "members": [room], "queue": []}
        dev = self.sonos.device(room)
        if dev is None:
            return out
        co = dev.group.coordinator
        info = co.get_current_track_info()
        st = co.get_current_transport_info().get("current_transport_state") or ""
        def secs(t):
            try:
                h, m, s = (int(x) for x in str(t or "0:00:00").split(":"))
                return h * 3600 + m * 60 + s
            except ValueError:
                return 0
        uri = info.get("uri") or ""
        kind = "tv" if "htastream" in uri else "linein" if "x-rincon-stream" in uri else "radio" if ("mp3radio" in uri or "x-sonosapi-stream" in uri or "hls-radio" in uri) \
            else "spotify" if "spotify" in uri else "queue" if uri else ""
        try:
            mode = co.play_mode
        except Exception:
            mode = "NORMAL"
        out.update({"state": st, "playing": st == "PLAYING", "title": (info.get("title") or "").strip(), "artist": info.get("artist") or "",
                    "album": info.get("album") or "", "pos": secs(info.get("position")), "dur": secs(info.get("duration")),
                    "vol": int(dev.volume), "mute": bool(dev.mute), "kind": kind,
                    "shuffle": "SHUFFLE" in mode, "repeat": "one" if mode == "REPEAT_ONE" else "all" if mode in ("REPEAT_ALL", "SHUFFLE") else "off",
                    "members": sorted(m.player_name for m in co.group.members if m.is_visible)})
        m = __import__("re").search(r"spotify(?:%3a|:)track(?:%3a|:)([A-Za-z0-9]{22})", uri, __import__("re").I)
        out["tid"] = m.group(1) if m else ""
        try:
            out["art"] = self.sonos._art_for(info)
        except Exception:
            pass
        try:
            p = int(info.get("playlist_position") or 0)
            if p > 0 and kind in ("spotify", "queue"):
                out["queue"] = [{"title": it.title or "", "artist": getattr(it, "creator", "") or ""} for it in co.get_queue(start=p, max_items=6)]
        except Exception:
            pass
        return out

    def _room_loop(self):
        while True:
            room = self.focus if time.time() < self.focus_until else None
            if room:
                try:
                    self.hub.post_json("/api/sonos-rooms", {room: self.room_state(room)})
                except Exception:
                    pass
                time.sleep(1.5)
            else:
                time.sleep(2)

    # ---------- timer ----------
    def timer_cmd(self, rest, off=False):
        if off:
            t = self.timers.pop(rest, None) if rest != "*" else None
            if rest == "*":
                for k in list(self.timers):
                    self.timers.pop(k)["t"].cancel()
            elif t:
                t["t"].cancel()
            self.log("  -> timer av")
            self.report()
            return
        try:
            o = json.loads(rest or "{}")
        except ValueError:
            return
        tid = str(o.get("id") or int(time.time()))
        mins = max(0.1, float(o.get("min") or 5))
        room = o.get("room") or (self.sonos.room_names() or [""])[0]
        old = self.timers.pop(tid, None)
        if old:
            old["t"].cancel()
        t = threading.Timer(mins * 60, self._ring, args=(tid,))
        t.daemon = True
        self.timers[tid] = {"id": tid, "name": str(o.get("name") or "Timer"), "end": time.time() + mins * 60, "room": room, "t": t, "min": mins}
        t.start()
        self.log('  -> timer "%s": %g min' % (self.timers[tid]["name"], mins))
        self.report()

    def _ring(self, tid):
        t = self.timers.pop(tid, None)
        self.report()
        if not t:
            return
        self.log('  timern "%s" ringer' % t["name"])
        self.hub.run_cmd("osd:" + t["name"] + " – klar", "local")
        self.ring(t["room"])

    def ring(self, room, times=2):
        """Klockan på Sonos; det som spelades sätts igång igen efteråt med samma volym."""
        dev = self.sonos.device(room)
        if dev is None:
            return
        try:
            from soco.snapshot import Snapshot
            co = dev.group.coordinator
            snap = Snapshot(co)
            snap.snapshot()
            vol = int(dev.volume)
            dev.volume = max(vol, 25)
            url = self.hub.server.rstrip("/") + "/api/chime.wav"
            for _ in range(times):
                co.play_uri(url, title="Timer")
                time.sleep(5.2)
            snap.restore(fade=False)
            dev.volume = vol
        except Exception as e:
            self.log("(varning: timern: %s)" % e)

    def report_data(self):
        return [{"id": t["id"], "name": t["name"], "end": t["end"], "min": t["min"]} for t in self.timers.values()]

    def report(self):
        try:
            self.hub.timeline.report()
        except Exception:
            pass
