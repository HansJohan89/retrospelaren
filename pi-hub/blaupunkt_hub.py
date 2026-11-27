#!/usr/bin/env python3
"""
BLAUPUNKT-hubben - en tjänst på Raspberry Pi:n i skåpet som sköter allt som bara går
att nå hemifrån:

  * Stream Controllern (knappar och rattar), inställd på webbsidan under /deck
  * Trådfri-belysningen och Sonos (tar över det monitor.py gjorde på kameradatorn)
  * jalusins Bluetooth-sensor: öppnad = väckning, stängd = släck och pausa
  * valfritt: Zigbee2MQTT-enheter via MQTT, HTTP-anrop och namngivna kommandon

Allt ställs in i hub_config.json bredvid den här filen (se hub_config.example.json).

    python3 blaupunkt_hub.py            kör hubben
    python3 blaupunkt_hub.py --check    prova varje del en gång och skriv ut vad som fungerar
    ... --config annan.json             använd en annan inställningsfil än hub_config.json

OBS: när hubben tar hand om kommandokön (handle_queue) ska monitor.py på kameradatorn
vara avstängd, annars utförs varje ljus- och volymkommando två gånger.
"""
import http.client
import json
import os
import queue
import engine
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lights import Lights          # noqa: E402
from sonos import Sonos            # noqa: E402
from plex import Plex              # noqa: E402
from deckdev import DeckRuntime    # noqa: E402
from jalusi import Jalusi          # noqa: E402
from timeline import Timeline      # noqa: E402
from clipaudio import ClipAudio    # noqa: E402
from nestcast import NestCast      # noqa: E402
from appsupport import AppSupport  # noqa: E402
from gamepad import Gamepad        # noqa: E402
from clipstream import ClipStream  # noqa: E402

VERSION = "2.4"
HUB_CMD_PREFIXES = ("screen-shot", "hub-update", "gamepad:", "lightgroups", "watch:", "sonos-focus:", "timer:", "timer-off:", "nest:", "alarm:", "sleep:", "clip-sonos:", "clip-audio:", "scene-stop:", "plug:", "light:", "sonos-vol:", "sonos:", "mqtt:", "http:", "sys:", "deck:", "scene:", "lamps-set:")
_log_lock = threading.Lock()


LOG_FILE = None        # sätts i main(): hubben skriver också sin logg till en fil bredvid sig


REMOTE_LOG = []        # rader som skickas till servern (syns i dess logg, så felsökning går på distans)


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    if len(REMOTE_LOG) < 500:
        REMOTE_LOG.append(line)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        if LOG_FILE:
            try:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2_000_000:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d ") + line + "\n")
            except OSError:
                pass


class Hub:
    def __init__(self, cfg, device_factory=None):
        self.cfg = cfg
        self.server = (cfg.get("server") or "").rstrip("/")
        self.commands = dict(cfg.get("commands") or {})
        self.lights = Lights(cfg.get("tradfri"), log, self.report_vals)
        self.sonos = Sonos(cfg.get("sonos"), log, self.report_vals)
        self.sonos.art_upload = lambda b64, ctype: (self.post_json("/api/sonos-art", {"data": b64, "type": ctype}) or {}).get("url")
        self.plex = Plex(cfg.get("plex"), log, self.sonos.art_upload, lambda lst: self.post_json("/api/plex", lst, timeout=20))
        deck_cfg = cfg.get("deck") or {}
        self.deck = DeckRuntime(self, deck_cfg, log, device_factory) if deck_cfg.get("enabled", True) else None
        self.jalusi = Jalusi(cfg.get("jalusi"), log, self.on_jalusi)
        self._tl = threading.local()
        self.out_q = queue.Queue()           # kommandon till sidan (/api/send)
        self.live_lock = threading.Lock()
        self.live_pending = {}
        self.live_cbs = []
        self.live_evt = threading.Event()
        self.refresh_evt = threading.Event()
        self.z2m_devices = {}                # givare parade med Zigbee2MQTT (nyckel "z2m:<namn>")
        # scener med tidslinje, schemalagda scener och lampregler (se timeline.py)
        self.clip_audio = ClipAudio(self.sonos, log)    # (äldre: klippens ljud via AirPlay)
        self.nest = NestCast(self, log)                  # Nest Hub som visningsskärm
        self.app = AppSupport(self, log)                 # appen: skärmbild, rummet, timers
        self.gamepad = Gamepad(self, log)                # handkontrollen (Stadia eller annan spelkontroll)
        self.clip_stream = ClipStream(self, log)        # klippens ljud spelas av Sonos som en ljudström
        self.timeline = Timeline(self, os.path.join(HERE, "timeline_state.json"), log)
        self.lights.on_manual = self.timeline.manual_touch      # ratten vinner över en pågående scen

    # ---- http mot servern ----
    # Varje tråd håller sin egen anslutning öppen. En ny krypterad anslutning per anrop kostar
    # några tiondels sekunder, och det märks direkt när man vrider på en ratt.
    def _request(self, method, path, body=None, timeout=8):
        u = urllib.parse.urlsplit(self.server)
        last = None
        for attempt in (0, 1):
            conn = getattr(self._tl, "conn", None)
            if conn is None:
                cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
                conn = self._tl.conn = cls(u.netloc, timeout=timeout)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body is not None else {})
                r = conn.getresponse()
                data = r.read()
                if r.will_close:
                    conn.close()
                    self._tl.conn = None
                if r.status >= 400:
                    raise urllib.error.HTTPError(self.server + path, r.status, r.reason, r.headers, None)
                return data
            except urllib.error.HTTPError:
                raise
            except Exception as e:          # anslutningen hade hunnit stängas: försök en gång till med en ny
                last = e
                try:
                    conn.close()
                except Exception:
                    pass
                self._tl.conn = None
        raise last

    def get_json(self, path, timeout=8):
        return json.loads(self._request("GET", path, timeout=timeout).decode("utf-8"))

    def _self_update(self):
        """Hämtar hubbens filer från servern (/api/hubfiles) och startar om sig själv om något
        ändrats, så att uppdatera-pi.bat inte behövs för varje ändring. Bara .py-filer och
        airplay-setup.sh, och bara till hubbens egen mapp; hub_config.json rörs aldrig."""
        import hashlib
        import re as _re
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            lst = self.get_json("/api/hubfiles", timeout=15).get("files") or {}
            changed = []
            for name, sha in lst.items():
                if not _re.fullmatch(r"[a-z_]+\.py|airplay-setup\.sh", name):
                    continue
                fp = os.path.join(here, name)
                try:
                    with open(fp, "rb") as f:
                        if hashlib.sha256(f.read()).hexdigest() == sha:
                            continue
                except OSError:
                    pass
                data = self._request("GET", "/api/hubfile?n=" + urllib.parse.quote(name), timeout=20)
                if hashlib.sha256(data).hexdigest() != sha:
                    raise RuntimeError("%s kom fram trasig" % name)
                if name.endswith(".py"):
                    compile(data, name, "exec")          # syntaxfel: uppdatera inte alls
                changed.append((fp, data))
            if not changed:
                log("  Uppdatering: hubben har redan senaste versionen")
                return
            for fp, data in changed:
                tmp = fp + ".ny"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, fp)
            log("  Uppdatering: %d filer nya (%s), startar om hubben" % (len(changed), ", ".join(os.path.basename(f) for f, _ in changed)))
            time.sleep(3)                                 # loggen hinner fram till servern
            os._exit(3)                                   # systemd startar hubben igen
        except Exception as e:
            log("(varning: uppdateringen: %s)" % e)

    def _remote_log_loop(self):
        """Hubbens logg till servern var tredje sekund (de senaste raderna, utan de pratigaste)."""
        while True:
            time.sleep(3)
            if not REMOTE_LOG:
                continue
            lines = [l for l in REMOTE_LOG[:80] if "sonos-focus" not in l]
            del REMOTE_LOG[:80]
            if lines:
                try:
                    self.post_json("/api/hublog", {"src": "hubb", "lines": lines}, timeout=6)
                except Exception:
                    pass

    def post_json(self, path, obj, timeout=8):
        return json.loads(self._request("POST", path, json.dumps(obj).encode("utf-8"), timeout).decode("utf-8"))

    # ---- kommandon ----
    def run_cmd(self, cmd, origin):
        """origin: "deck" (fysiskt tryck), "local" (jalusin) eller "queue" (från sidans kö).
        Det hubben själv kan utföra görs direkt här hemma, utan omväg över nätet. Resten är
        kommandon till skärmen (open, viz:3 ...): de skickas till sidan, utom när de redan
        kom därifrån."""
        if self.cfg.get("forward") and origin != "queue" and not cmd.startswith("deck:"):
            # Provläge på en dator utan kontakt med Trådfri: skicka allt vidare till kön, så
            # utför den hubb som har kontakten (t.ex. den i WSL) kommandot.
            self.out_q.put(cmd)
            return
        try:
            if cmd.startswith("light:"):
                self.lights.submit(json.loads(cmd[6:]))
            elif cmd.startswith("plug:"):
                c = json.loads(cmd[5:])
                threading.Thread(target=self.lights.set_outlet, args=(c.get("id"), c.get("action") or "toggle"), daemon=True).start()
            elif cmd.startswith("sonos-vol:"):
                c = json.loads(cmd[10:])
                # medan ett klipp visas styr volymen Pi:ns ljud i stället för Sonos
                if not self.clip_audio.volume(c.get("room"), value=c.get("value")):
                    self.sonos.submit({"room": c.get("room"), "action": "vol", "value": c.get("value")})
            elif cmd.startswith("sonos:"):
                c = json.loads(cmd[6:])
                if c.get("action") == "vol-rel" and self.clip_audio.volume(c.get("room"), rel=c.get("value") or 0):
                    pass
                elif c.get("action") == "vol" and self.clip_audio.volume(c.get("room"), value=c.get("value")):
                    pass
                else:
                    self.sonos.submit(c)
            elif cmd.startswith("mqtt:"):
                self._mqtt(json.loads(cmd[5:]))
            elif cmd.startswith("http:"):
                threading.Thread(target=self._http, args=(json.loads(cmd[5:]),), daemon=True).start()
            elif cmd.startswith("sys:"):
                self._sys(json.loads(cmd[4:]))
            elif cmd.startswith("deck:"):
                if self.deck:
                    self.deck.command(json.loads(cmd[5:]))
            elif cmd.startswith("clip-sonos:"):
                self.clip_stream.command(cmd[11:])
            elif cmd.startswith("clip-audio:"):
                self.clip_audio.command(cmd[11:])
            elif cmd == "screen-shot":
                threading.Thread(target=self.app.screenshot, daemon=True).start()
            elif cmd.startswith("sonos-focus:"):
                self.app.set_focus(cmd[12:])
            elif cmd.startswith("timer-off:"):
                self.app.timer_cmd(cmd[10:], off=True)
            elif cmd.startswith("timer:"):
                self.app.timer_cmd(cmd[6:])
            elif cmd.startswith("nest:"):
                self.nest.command(cmd[5:])
            elif cmd.startswith("alarm:"):
                threading.Thread(target=self.timeline.alarm_cmd, args=(cmd[6:],), daemon=True).start()
            elif cmd.startswith("sleep:"):
                threading.Thread(target=self.timeline.sleep_cmd, args=(cmd[6:],), daemon=True).start()
            elif cmd.startswith("scene-stop:"):
                # scene-stop:<scenens id eller *>[:end]  avbryter en pågående scen, ev. med dess slut
                sid, _, how = cmd[11:].partition(":")
                threading.Thread(target=self.timeline.stop_run, args=(sid or "*", how == "end"), daemon=True).start()
            elif cmd.startswith("scene:"):
                threading.Thread(target=self.run_scene, args=(cmd[6:],), daemon=True).start()
            elif cmd.startswith("lamps-set:"):
                # prova enstaka lampor direkt från scenbyggaren
                threading.Thread(target=self._lamps_set, args=(json.loads(cmd[10:]),), daemon=True).start()
            elif cmd == "lamps-read":
                threading.Thread(target=self._lamps_read, daemon=True).start()
            elif cmd == "hub-update":
                threading.Thread(target=self._self_update, daemon=True).start()
            elif cmd.startswith("gamepad:"):
                self.gamepad.command(cmd[8:])
            elif cmd == "lightgroups":
                # appen har sparat nya ljusgrupper: läs in dem direkt i stället för om en stund
                threading.Thread(target=self.timeline.config, args=(True,), daemon=True).start()
            elif cmd.startswith("watch:"):
                # Nest Hub: något att titta på (SVT-kanal, film eller serie från Plex). Projektorn
                # kopplas in senare; tills dess noteras valet bara här.
                try:
                    w = json.loads(cmd[6:])
                except ValueError:
                    w = {"title": cmd[6:]}
                log('  Att titta på: %s (%s). Projektorn är inte inkopplad än.' % (w.get("title") or "?", w.get("src") or "?"))
            elif cmd == "tradfri-refresh":
                self.refresh_evt.set()
            elif origin != "queue":
                self.out_q.put(cmd)
        except ValueError:
            log('(varning: ogiltigt kommando "%s")' % cmd)

    def _mqtt(self, c):
        topic, payload = str(c.get("topic") or ""), c.get("payload")
        m = self.cfg.get("mqtt") or {}
        allow = m.get("allow") or ["zigbee2mqtt/"]
        if not any(topic.startswith(p) for p in allow):
            log('(MQTT: ämnet "%s" är inte tillåtet. Tillåtna börjar med: %s)' % (topic, ", ".join(allow)))
            return
        try:
            import paho.mqtt.publish as publish
            publish.single(topic, payload="" if payload is None else str(payload), hostname=m.get("host", "127.0.0.1"), port=int(m.get("port", 1883)))
            log("  -> MQTT: %s %s" % (topic, payload))
        except Exception as e:
            log("(varning: MQTT-meddelandet gick inte iväg: %s)" % e)

    def _http(self, c):
        url = str(c.get("url") or "")
        allow = list(self.cfg.get("http_allow") or [])
        if not any(url.startswith(p) for p in allow):
            log('(HTTP: "%s" finns inte i http_allow i hub_config.json, hoppar över)' % url)
            return
        try:
            body = c.get("body") or ""
            method = (c.get("method") or "GET").upper()
            req = urllib.request.Request(url, data=body.encode("utf-8") if method == "POST" else None, method=method)
            with urllib.request.urlopen(req, timeout=10) as r:
                log("  -> HTTP %s %s: %s" % (method, url, r.status))
        except Exception as e:
            log("(varning: HTTP-anropet misslyckades: %s)" % e)

    def _sys(self, c):
        name = str(c.get("name") or "")
        line = self.commands.get(name)
        if not line:
            log('(kommandot "%s" finns inte under commands i hub_config.json)' % name)
            return
        log('  -> kör "%s"' % name)
        subprocess.Popen(line, shell=True)

    # ---- delat körläge med sidan ----
    def live_post(self, patch, cb=None):
        with self.live_lock:
            p = self.live_pending
            if "nav" in patch:
                p["nav"] = patch["nav"]
            for k in ("st", "vals"):
                if k in patch:
                    p.setdefault(k, {}).update(patch[k])
            if "ev" in patch:
                p.setdefault("ev", []).extend(patch["ev"])
            if cb:
                self.live_cbs.append(cb)
        self.live_evt.set()

    def report_vals(self, vals):
        self.live_post({"vals": vals})

    def heartbeat_now(self):
        self.live_evt.set()

    def _heartbeat(self):
        return {"device": bool(self.deck and self.deck.connected), "sys": sorted(self.commands), "rooms": self.sonos.all_room_names(),
                "ver": VERSION, "name": self.cfg.get("name") or ""}

    def _live_post_loop(self):
        while True:
            self.live_evt.wait(timeout=3)
            self.live_evt.clear()
            with self.live_lock:
                body, cbs = self.live_pending, self.live_cbs
                self.live_pending, self.live_cbs = {}, []
            if self.cfg.get("heartbeat", True):
                body["hub"] = self._heartbeat()
            elif not body:
                continue
            try:
                resp = self.post_json("/api/deck/live", body)
                for cb in cbs:
                    cb(body, resp)
            except Exception as e:
                self._net_warn("kunde inte nå servern: %s" % e)

    def _net_warn(self, msg):
        now = time.time()
        if now - getattr(self, "_warn_at", 0) > 60:
            self._warn_at = now
            log("(varning: %s)" % msg)

    def _live_poll_loop(self):
        ev, seq = -1, -1
        while True:
            try:
                # servern håller kvar anropet tills något ändras, så ett tryck i emulatorn når enheten direkt
                j = self.get_json("/api/deck/live?ev=%d&seq=%d&wait=20" % (max(ev, 0), seq), timeout=35)
                ev = max(ev, j.get("evLast", 0))
                seq = j.get("seq", -1)
                if self.deck:
                    if j.get("deckRev", 0) != self.deck.deck_rev:
                        d = self.get_json("/api/deck")
                        if d.get("deck"):
                            faces = self.get_json("/api/deck/faces", timeout=30).get("faces") or {}
                            self.deck.set_deck(d["deck"], d.get("rev", 0), faces)
                            self.deck.deck_rev = d.get("rev", 0)
                        else:
                            self.deck.deck_rev = j.get("deckRev", 0)
                            log("Ingen konfiguration sparad på sidan än. Öppna %s/deck en gång." % self.server)
                    self.deck.live_update(j)
            except Exception as e:
                self._net_warn("kunde inte läsa körläget: %s" % e)
                time.sleep(2)
            if seq < 0:
                time.sleep(0.3)             # äldre server utan väntan: fråga med jämna mellanrum

    # ---- sidans kommandokö ----
    def _queue_loop(self):
        since = -1
        while True:
            try:
                d = self.get_json("/api/cmd?since=%d%s" % (max(since, 0), "" if since < 0 else "&wait=20"), timeout=35)
                if since < 0:
                    since = d.get("last", 0)          # gamla kommandon från före starten körs inte
                elif d.get("last", since) < since:
                    since = d.get("last", 0)          # servern har startats om med lägre nummer
                else:
                    for it in d.get("items", []):
                        since = max(since, it.get("id", since))
                        c = it.get("c") or ""
                        if c in ("tradfri-refresh", "lamps-read") or c.startswith(HUB_CMD_PREFIXES):
                            self.run_cmd(c, "queue")
                        elif c.startswith("sp:") and self._sp_room():
                            threading.Thread(target=self._sp_fallback, args=(it.get("id"), c), daemon=True).start()
            except Exception as e:
                self._net_warn("kunde inte läsa kommandokön: %s" % e)
                time.sleep(2)
            time.sleep(0.02)

    # ---- egna scener (byggs på webbsidan under /deck) ----
    def _scenes(self):
        return self.get_json("/api/scenes") or {}

    def run_scene(self, scene_id):
        """Kör en egen scen (lampor, Trådfri-scener, musik, skärmen, och tidslinjen om den har en)."""
        self.timeline.start(scene_id)

    def _lamps_read(self):
        try:
            d = {"states": self.lights.read_lamps(), "speakers": self.sonos.zone_names()}
            try:
                d["sonos"] = self.sonos.overview()        # volymer, grupper och favoriter till appen
            except Exception:
                pass
            self.post_json("/api/tradfri-list", d)
        except Exception as e:
            log("(varning: kunde inte läsa lampornas läge: %s)" % e)

    def _scene_screen(self, scr):
        """Blaupunkt-skärmens del av en scen. Äldre scener har bara ett ord (wake/open/close),
        nyare { power, viz, palette, page }."""
        if isinstance(scr, str):
            scr = {"power": scr}
        if not isinstance(scr, dict):
            return
        scr = dict(scr)
        legacy = {"wake": {"anim": "wake", "pic": "on", "music": "play"}, "open": {"anim": "warm", "pic": "on", "music": "play"},
                  "close": {"anim": "off", "pic": "off", "music": "pause"}, "playpause": {"music": "toggle"}}
        for k, v in legacy.get(scr.pop("power", None) or "", {}).items():
            scr.setdefault(k, v)
        cmd = engine.to_cmd(dict(scr, t="screen"))
        if cmd:
            self.run_cmd(cmd, "local")

    def _plex_loop(self):
        """Filmtipsen från Plex: hämtas vid start och sedan var halvtimme (eller när servern
        har startats om och glömt omslagen)."""
        last = 0
        while True:
            try:
                fresh = self.plex.check_images(lambda u: self._url_ok(u))
                if not fresh or time.time() - last > float(self.cfg.get("plex", {}).get("every_min", 30)) * 60:
                    self.plex.refresh()
                    last = time.time()
            except Exception as e:
                log("(varning: Plex: %s)" % e)
            time.sleep(120)

    def _url_ok(self, path):
        try:
            self._request("GET", path, None, 10)
            return True
        except Exception:
            return False

    def _lamps_set(self, settings):
        settings = settings if isinstance(settings, dict) else {}
        # lampor med effekt provas med effekten (en halv minut), övriga sätts direkt
        for lid, s in list(settings.items()):
            if isinstance(s, dict) and (s.get("effect") or {}).get("type") and s.get("on", True) is not False:
                self.timeline.try_effect(lid, s)
                log("  -> Trådfri: provar effekten %s på lampa %s" % (s["effect"]["type"], lid))
                del settings[lid]
            else:
                self.timeline.effects.stop_lamp(lid)
        # ändras en lampa som en scen styr (från appen eller "Prova"): scenen tar inte tillbaka den
        for lid, st in settings.items():
            if isinstance(st, dict):
                try:
                    self.timeline.manual_touch([lid], {k: st[k] for k in ("on", "dim", "kind", "color", "temp") if k in st})
                except Exception:
                    pass
        try:
            n = self.lights.apply_lamps(settings)
            log("  -> Trådfri: %d lampor ändrade" % n)
        except Exception as e:
            log("(varning: kunde inte prova lamporna: %s)" % e)

    # ---- fjärrkontroller och sensorer ----
    def input_list(self):
        return self.lights.input_list() + list(self.z2m_devices.values())

    def fire_input(self, key, event, cfg=None):
        """En fjärrkontroll eller sensor har gjort något: kör det som är inställt för det på webbsidan."""
        try:
            cfg = cfg if cfg is not None else (self._scenes().get("inputs") or {})
            ctl = ((cfg.get(key) or {}).get("events") or {}).get(event)
        except Exception as e:
            log("(varning: kunde inte hämta sensorernas inställningar: %s)" % e)
            return False
        if not ctl:
            return False
        log('  sensor %s: "%s"' % (key, event))
        return self._run_ctl(ctl)

    def _inputs_loop(self):
        """Trådfri-gatewayen berättar inte när någon trycker på en fjärrkontroll eller när en
        rörelsesensor slår till. Det som går att se är följden: att lamporna i gruppen den styr
        tänds eller släcks utan att hubben gjorde det. Det bevakas här, för de givare som har
        något inställt på webbsidan."""
        cfg, cfg_at, last, last_mood = {}, 0, {}, {}
        while True:
            time.sleep(float(self.cfg.get("inputs_poll_s", 1.5)))
            try:
                if time.time() - cfg_at > float(self.cfg.get("inputs_cfg_s", 20)):
                    cfg_at = time.time()
                    cfg = self._scenes().get("inputs") or {}
                watch = {}                                        # gruppnamn -> [givarens nyckel]
                by_key = {i["key"]: i for i in self.lights.input_list()}
                for key, c in cfg.items():
                    ev = (c or {}).get("events") or {}
                    if key in by_key and (ev.get("on") or ev.get("off")):
                        for g in by_key[key]["groups"]:
                            watch.setdefault(g, []).append(key)
                for g in [g for g in last if g not in watch]:
                    del last[g]
                for g, keys in watch.items():
                    on = self.lights.group_any_on(g)
                    if on is None:
                        continue
                    own = time.time() - self.lights.own_at < 6      # hubben ändrade själv ljuset nyss
                    if g in last and on != last[g] and not own:
                        for key in keys:
                            self.fire_input(key, "on" if on else "off", cfg)
                    last[g] = on
                # Trådfri-scener som startas utifrån (t.ex. av en genvägsknapp som i IKEA-appen är
                # kopplad till scenen): gruppens aktiva scen byts utan att hubben gjorde det.
                # Inställda som "mood:<grupp>:<scen>" på webbsidan.
                mood_groups = {}
                for key, c in cfg.items():
                    if key.startswith("mood:") and ((c or {}).get("events") or {}).get("run"):
                        _, g, mname = key.split(":", 2)
                        mood_groups.setdefault(g, set()).add(mname)
                for g in [g for g in last_mood if g not in mood_groups]:
                    del last_mood[g]
                for g, names in mood_groups.items():
                    cur = self.lights.group_mood(g)
                    if cur is None:
                        continue
                    own = time.time() - self.lights.own_at < 6
                    if g in last_mood and cur != last_mood[g] and not own and cur in names:
                        self.fire_input("mood:%s:%s" % (g, cur), "run", cfg)
                    last_mood[g] = cur
            except Exception as e:
                log("(varning: sensorbevakningen: %s)" % e)
                time.sleep(10)

    def _z2m_loop(self):
        """Givare som är parade med Zigbee2MQTT: här kommer varje knapptryck och varje rörelse fram
        som ett MQTT-meddelande, så de kan ställas in knapp för knapp."""
        m = self.cfg.get("mqtt") or {}
        base = (m.get("base") or "zigbee2mqtt").rstrip("/")
        state = {}

        def on_message(client, userdata, msg):
            try:
                topic = msg.topic[len(base) + 1:]
                data = json.loads(msg.payload.decode("utf-8") or "null")
                if topic == "bridge/devices":
                    devs = {}
                    for d in data or []:
                        name = d.get("friendly_name")
                        feats = [f for f in ((d.get("definition") or {}).get("exposes") or [])]
                        act = next((f.get("values") for f in feats if f.get("name") == "action"), None)
                        names = {f.get("name") for f in feats}
                        if d.get("type") == "Coordinator" or not name or not (act or names & {"occupancy", "contact"}):
                            continue
                        events = ["action:" + a for a in act or []]
                        if "occupancy" in names:
                            events += ["motion:on", "motion:off"]
                        if "contact" in names:
                            events += ["contact:open", "contact:close"]
                        devs["z2m:" + name] = {"key": "z2m:" + name, "src": "z2m", "id": name, "name": name, "events": events,
                                               "model": (d.get("definition") or {}).get("model") or "", "kind": "motion" if "occupancy" in names else "remote"}
                    self.z2m_devices = devs
                    self.refresh_evt.set()
                    return
                if "/" in topic or not isinstance(data, dict):
                    return
                key, st = "z2m:" + topic, state.setdefault(topic, {})
                if data.get("action"):
                    threading.Thread(target=self.fire_input, args=(key, "action:" + str(data["action"])), daemon=True).start()
                for field, ev_on, ev_off in (("occupancy", "motion:on", "motion:off"), ("contact", "contact:close", "contact:open")):
                    if field in data and st.get(field) is not None and bool(data[field]) != st[field]:
                        threading.Thread(target=self.fire_input, args=(key, ev_on if data[field] else ev_off), daemon=True).start()
                    if field in data:
                        st[field] = bool(data[field])
                if "battery" in data and key in self.z2m_devices:
                    self.z2m_devices[key]["battery"] = data["battery"]
            except Exception as e:
                log("(varning: MQTT-meddelande: %s)" % e)
        while True:
            try:
                import paho.mqtt.client as mqtt
                try:
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                    client.on_connect = lambda c, u, f, rc, p=None: c.subscribe([(base + "/bridge/devices", 0), (base + "/+", 0)])
                except AttributeError:
                    client = mqtt.Client()
                    client.on_connect = lambda c, u, f, rc: c.subscribe([(base + "/bridge/devices", 0), (base + "/+", 0)])
                client.on_message = on_message
                client.connect(m.get("host", "127.0.0.1"), int(m.get("port", 1883)), 60)
                log("Lyssnar på Zigbee2MQTT (%s)." % base)
                client.loop_forever()
            except Exception as e:
                log("(Zigbee2MQTT svarar inte än: %s. Provar igen om en minut.)" % e)
            time.sleep(60)

    def run_sensor(self, name, slot):
        """Kör det som är inställt för en sensor på webbsidan (t.ex. jalusi, open). Ger False om
        inget är inställt där, så att de gamla inställningarna i hub_config.json används."""
        try:
            ctl = ((self._scenes().get("sensors") or {}).get(name) or {}).get(slot)
        except Exception:
            return False
        return self._run_ctl(ctl)

    def _run_ctl(self, ctl):
        try:
            a = ctl["states"][0]["a"] if ctl else None
        except Exception:
            return False
        if not a:
            return False
        steps = a.get("steps") or [] if a.get("t") == "multi" else [{"a": a, "delay": 0}]
        for st in steps:
            cmd = engine.to_cmd(st.get("a") or {})
            if cmd:
                self.run_cmd(cmd, "local")
            time.sleep(max(0, min(60000, float(st.get("delay") or 0))) / 1000.0)
        return True

    # ---- Spotify-knappar när ingen Blaupunkt-sida är öppen ----
    def _sp_room(self):
        s = self.cfg.get("sonos") or {}
        return s.get("spotify_room") or s.get("wake_room") or next(iter(s.get("rooms") or {}), None)

    def _sp_fallback(self, cmd_id, cmd):
        """Spotify-kommandon utförs i första hand av den öppna Blaupunkt-sidan, som är inloggad på
        Spotify. Tar ingen sida kommandot inom en kort stund finns ingen öppen: då styr hubben
        Sonos-högtalaren direkt i stället, vilket fungerar när Spotify spelar på den."""
        try:
            time.sleep(float(self.cfg.get("spotify_wait", 0.7)))
            if not self.get_json("/api/claim?id=%d&who=hub-%d" % (cmd_id, os.getpid())).get("ok"):
                return                                   # en öppen sida har redan tagit det
            c = json.loads(cmd[3:])
            action, value = c.get("action"), c.get("value")
            rooms = (self.cfg.get("sonos") or {}).get("rooms") or {}
            room = next((r for r in rooms if r.lower() == str(c.get("device") or "").strip().lower()), None) or self._sp_room()
            if action == "playlist" and not re.match(r"(spotify:|https?://)", str(value or "")):
                log('  (Spotify: ingen Blaupunkt-sida är öppen, och Sonos kan bara starta spellistor som anges med länk, inte med namnet "%s")' % value)
            elif action == "device":
                self.sonos.submit({"room": str(value or ""), "action": "move"})
            elif action == "like":
                log('  (Spotify: "gilla" kräver att Blaupunkt-sidan är öppen och inloggad på Spotify)')
            else:
                log("  Spotify: ingen Blaupunkt-sida är öppen, styr Sonos i %s i stället" % room)
                self.sonos.submit({"room": room, "action": action, "value": value})
        except Exception as e:
            log("(varning: Spotify-kommandot kunde inte utföras: %s)" % e)

    @staticmethod
    def _merge_rel(cmds):
        """Slår ihop steg-kommandon som hunnit köa upp sig (ratten vrids fortare än nätet hinner
        med): tre "ljusare +8" i rad blir ett "ljusare +24". Annars släpar lampan efter handen."""
        out = []
        for cmd in cmds:
            try:
                pre, _, js = cmd.partition(":")
                c = json.loads(js) if js.startswith("{") else None
                if c and str(c.get("action", "")).endswith("-rel") and out:
                    ppre, _, pjs = out[-1].partition(":")
                    pc = json.loads(pjs) if pjs.startswith("{") else None
                    same = pc and ppre == pre and all(pc.get(k) == c.get(k) for k in ("action", "group", "room", "device"))
                    if same:
                        pc["value"] = (pc.get("value") or 0) + (c.get("value") or 0)
                        out[-1] = pre + ":" + json.dumps(pc, ensure_ascii=False, separators=(",", ":"))
                        continue
            except (ValueError, TypeError):
                pass
            out.append(cmd)
        return out

    def _out_loop(self):
        while True:
            cmds = [self.out_q.get()]
            while True:
                try:
                    cmds.append(self.out_q.get_nowait())
                except queue.Empty:
                    break
            for cmd in self._merge_rel(cmds):
                try:
                    self._request("GET", "/api/send?c=" + urllib.parse.quote(cmd, safe=""))
                except Exception as e:
                    log('(varning: kunde inte skicka "%s" till sidan: %s)' % (cmd, e))

    def _tradfri_list_loop(self):
        next_at = 0
        while True:
            if self.refresh_evt.wait(timeout=30) or time.time() >= next_at:
                self.refresh_evt.clear()
                try:
                    ids = self.lights.refresh()
                    if ids is not None:
                        self.post_json("/api/tradfri-list", {"groups": self.lights.group_list(), "lamps": self.lights.lamp_list(), "speakers": self.sonos.zone_names(), "inputs": self.input_list(),
                                                                "other": getattr(self.lights, "other", [])})
                        log("Trådfri-listan uppdaterad (%d grupper)." % len(ids))
                except Exception as e:
                    log("(varning: kunde inte uppdatera Trådfri-listan: %s)" % e)
                next_at = time.time() + 6 * 3600

    def _sonos_now_loop(self):
        room = (self.cfg.get("sonos") or {}).get("wake_room") or (self.sonos.room_names() or [""])[0]
        while room:
            try:
                self.post_json("/api/sonos-now", self.sonos.now_playing(room))
            except Exception:
                pass
            time.sleep(1.5)

    # ---- jalusin ----
    def on_jalusi(self, is_open):
        j = self.cfg.get("jalusi") or {}
        self.live_post({"vals": {"jalusi": "open" if is_open else "closed"}})
        if self.run_sensor("jalusi", "open" if is_open else "close"):
            return
        try:
            settings = (self.get_json("/api/state") or {}).get("settings") or {}
        except Exception:
            settings = {}
        group = (settings.get("tradfriGroupName") or "").strip()
        if is_open:
            log("  -> väckning: animation, Sonos och Trådfri")
            self.run_cmd(j.get("open_cmd", "wake"), "local")
            if j.get("open_sonos", True):
                threading.Thread(target=self.sonos.wake, daemon=True).start()
            if j.get("open_scene", True):
                threading.Thread(target=self.lights.run_scene, args=(group, (settings.get("tradfriMoodName") or "").strip(), True), daemon=True).start()
        else:
            if j.get("close_cmd", "close"):
                self.run_cmd(j.get("close_cmd", "close"), "local")
            if j.get("close_pause_sonos", True):
                room = (self.cfg.get("sonos") or {}).get("wake_room") or (self.sonos.room_names() or [""])[0]
                threading.Thread(target=self.sonos.pause_room, args=(room,), daemon=True).start()
            mood = (settings.get("tradfriSleepMoodName") or "").strip()
            if j.get("close_scene", True) and mood:
                threading.Thread(target=self.lights.run_scene, args=(group, mood, False), daemon=True).start()

    # ---- start ----
    def start(self):
        loops = [self._live_post_loop, self._out_loop, self._remote_log_loop]
        if self.deck:
            loops += [self.deck.run, self._live_poll_loop]
        if self.cfg.get("handle_queue", True):
            loops.append(self._queue_loop)
        if self.lights.enabled:
            loops += [self._tradfri_list_loop, self._inputs_loop]
        self.timeline.start_loops()
        if self.plex.enabled:
            loops.append(self._plex_loop)
        if (self.cfg.get("mqtt") or {}).get("listen"):
            loops.append(self._z2m_loop)
        if (self.cfg.get("sonos") or {}).get("report_now", True) and self.sonos.room_names():
            loops.append(self._sonos_now_loop)
        for fn in loops:
            threading.Thread(target=fn, name=fn.__name__, daemon=True).start()
        self.jalusi.start()
        log("Hubben är igång mot %s (version %s)." % (self.server, VERSION))


def _quiet_reader_errors():
    """Drivrutinens lästråd dör med en lång felutskrift när enheten rycks ur. Hubben märker det
    själv och ansluter igen, så en rad räcker."""
    orig = threading.excepthook

    def hook(args):
        if args.thread is not None and args.thread.name.startswith("soomfon"):
            log("(enhetens lästråd avbröts: %s)" % args.exc_value)
            return
        orig(args)
    threading.excepthook = hook


def load_config():
    name = "hub_config.json"
    if "--config" in sys.argv:
        name = sys.argv[sys.argv.index("--config") + 1]
    path = name if os.path.isabs(name) else os.path.join(HERE, name)
    if not os.path.exists(path):
        sys.exit("Saknar %s. Kopiera hub_config.example.json till hub_config.json och fyll i den." % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("server"):
        sys.exit('"server" saknas i hub_config.json.')
    return cfg


def check(cfg):
    """Provar varje del en gång, så att man ser vad som fungerar innan tjänsten slås på."""
    hub = Hub(cfg)
    ok = lambda b: "OK " if b else "FEL"
    try:
        d = hub.get_json("/api/deck")
        print(ok(True), "Servern svarar. Konfiguration: %s" % ("version %s" % d.get("rev") if d.get("deck") else "ingen sparad än, öppna /deck på sidan"))
    except Exception as e:
        print(ok(False), "Servern svarar inte:", e)
    if hub.lights.enabled:
        try:
            ids = hub.lights.refresh()
            print(ok(bool(ids)), "Trådfri: %d grupper: %s" % (len(ids or {}), ", ".join(v["name"] for v in (ids or {}).values())))
        except Exception as e:
            print(ok(False), "Trådfri:", e)
    else:
        print("-- ", "Trådfri är inte inställt (tradfri i hub_config.json)" + ("" if __import__("lights").HAVE_TRADFRI else ", och pytradfri saknas"))
    for room in hub.sonos.room_names():
        dev = hub.sonos.device(room)
        print(ok(dev is not None), 'Sonos "%s"%s' % (room, ": volym %s" % dev.volume if dev is not None else ""))
    if hub.deck:
        try:
            dev = hub.deck._open_soomfon()
            dev.set_brightness(70)
            for i, rgb in enumerate([(200, 40, 40), (220, 140, 0), (200, 200, 0), (40, 170, 60), (40, 120, 220), (150, 60, 200)]):
                dev.set_key_color(i, *rgb)
            print(ok(True), "Stream Controllern svarar. Knapparna ska nu lysa röd, orange, gul, grön, blå, lila (övre raden först).")
            print("    Tryck och vrid i tio sekunder så skrivs det ut vad enheten rapporterar:")
            dev.on_key(lambda k, p: print("    knapp", k, "ner" if p else "upp"))
            dev.on_encoder(lambda e, dl: print("    ratt", e, "%+d" % dl))
            dev.start()
            time.sleep(10)
            dev.clear_all()
            dev.close()
        except Exception as e:
            print(ok(False), "Stream Controllern:", e)
    print("-- ", "Jalusisensorn: %s" % (hub.jalusi.mac or "inte inställd (kör scan_ble.py för att hitta adressen)"))


def main():
    cfg = load_config()
    if "--check" in sys.argv:
        check(cfg)
        return
    global LOG_FILE
    name = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else "hub_config.json"
    stem = os.path.splitext(os.path.basename(name))[0].replace("hub_config", "").strip(".") or "pi"
    LOG_FILE = os.path.join(HERE, "hub-%s.log" % stem)
    _quiet_reader_errors()
    log("=== start ===")
    Hub(cfg).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
