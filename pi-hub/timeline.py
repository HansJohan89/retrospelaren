"""
Tidsstyrning: scener med tidslinje, schemalagda scener och lampregler.

Scen med tidslinje (byggs på /deck under Scener):
  Start  det scenen alltid har gjort (lampor, Trådfri-scener, musik, skärmen), men varje lampa
         kan tonas till sitt läge under en tid och kan börja från ett eget läge ("from"),
         och musikens volym kan tonas under lång tid.
  Under  steg vid bestämda minuter efter start: { at: minuter, lights, music, screen, scene }.
  Slut   efter { after: minuter }: lampor, musik (t.ex. tona ut och pausa), skärm, annan scen.
  Hubben sköter övergångarna själv i små steg (Trådfri klarar själv bara korta övergångar).
  Ändrar någon en lampa eller volymen för hand medan scenen pågår slutar scenen styra just den
  saken (manual: "release"), eller avbryts helt (manual: "stop"), eller bryr sig inte ("keep").
  Pågående scener sparas på disk och fortsätter efter en omstart.

Schema: { scene, when: { kind: "time"|"sunrise"|"sunset", time: "06:45", offset: minuter }, days: [0-6, mån=0] }
Lampregler: värden en lampa får när den tänds inom ett tidsfönster (den tänds aldrig av regeln).
"""
import colorsys
import datetime as dt
from effects import Effects
import json
import math
import os
import threading
import time

TICK = 0.5                 # hur ofta övergångarna räknas fram
LAMP_STEP_MIN = 2.0        # minst så här lång tid mellan två kommandon till samma lampa under en övergång
VOL_STEP_MIN = 1.0
MANUAL_CHECK_S = 3         # hur ofta lamporna i en pågående övergång läses av för att se om någon ändrat dem
CATCH_UP_S = 600           # en schemalagd tid som passerades när hubben var avstängd körs om det var nyss


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _hex_to_hs(hexval):
    h = str(hexval or "").lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
    return hue * 360, sat


def _hs_to_hex(hue, sat):
    r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360, max(0, min(1, sat)), 1)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def mix_lamp(a, b, k):
    """Lampläge mellan a (k=0) och b (k=1). Dimring och vit ton rakt av; färg runt färghjulet
    den kortaste vägen. Byter lampan mellan vitt och färg sker bytet direkt i början."""
    k = max(0.0, min(1.0, k))
    out = {"on": True, "dim": _f(a.get("dim"), 1) + (_f(b.get("dim"), 1) - _f(a.get("dim"), 1)) * k}
    ka, kb = a.get("kind") or "", b.get("kind") or ""
    if kb == "white" and ka == "white":
        out.update(kind="white", temp=_f(a.get("temp"), 35) + (_f(b.get("temp"), 35) - _f(a.get("temp"), 35)) * k)
    elif kb == "color" and ka == "color":
        ha, hb = _hex_to_hs(a.get("color")), _hex_to_hs(b.get("color"))
        if ha and hb:
            d = (hb[0] - ha[0] + 540) % 360 - 180
            out.update(kind="color", color=_hs_to_hex(ha[0] + d * k, ha[1] + (hb[1] - ha[1]) * k))
        else:
            out.update(kind="color", color=b.get("color"))
    elif kb:
        out.update({x: b.get(x) for x in ("kind", "temp", "color") if b.get(x) is not None})
    return out


# ---------- sol ----------
def sun_times(day, lat, lon):
    """Soluppgång och solnedgång (lokal tid, datetime) för ett datum. None om solen inte går
    upp eller ned den dagen (midnattssol/polarnatt). NOAA:s förenklade formler, ±1 min."""
    n = day.toordinal() - dt.date(2000, 1, 1).toordinal() + 0.0008 + 1
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360
    mr = math.radians(m)
    c = 1.9148 * math.sin(mr) + 0.02 * math.sin(2 * mr) + 0.0003 * math.sin(3 * mr)
    lam = math.radians((m + c + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(mr) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(math.radians(lat)) * math.sin(decl)) / (math.cos(math.radians(lat)) * math.cos(decl))
    if cos_w < -1 or cos_w > 1:
        return None, None
    w = math.degrees(math.acos(cos_w))

    def to_local(jd):
        ts = (jd - 2440587.5) * 86400.0
        return dt.datetime.fromtimestamp(ts)
    return to_local(j_transit - w / 360.0), to_local(j_transit + w / 360.0)


def when_on(day, when, place):
    """Tidpunkten (datetime) en tidsangivelse betyder ett visst datum, eller None."""
    kind = (when or {}).get("kind") or "time"
    off = dt.timedelta(minutes=_f((when or {}).get("offset"), 0))
    if kind in ("sunrise", "sunset"):
        up, down = sun_times(day, _f(place.get("lat"), 60.6), _f(place.get("lon"), 15.6))
        base = up if kind == "sunrise" else down
        return base + off if base else None
    try:
        hh, mm = str((when or {}).get("time") or "00:00").split(":")[:2]
        return dt.datetime.combine(day, dt.time(int(hh) % 24, int(mm) % 60)) + off
    except ValueError:
        return None


def describe_when(when):
    kind = (when or {}).get("kind") or "time"
    off = int(_f((when or {}).get("offset"), 0))
    if kind == "time":
        return str((when or {}).get("time") or "00:00")
    s = "soluppgång" if kind == "sunrise" else "solnedgång"
    return s if not off else "%d min %s %s" % (abs(off), "efter" if off > 0 else "före", s)


# ---------- larm ----------
def alarm_scene(a):
    """Ett larm från appen som scen: musiken tonas upp, ev. skärmen och en scen (t.ex. lampor),
    och stängs av själv efter en stund om man vill."""
    fade = max(0.0, _f(a.get("fade"), 0)) * 60
    room = a.get("room") or ""
    music = {"do": "play", "room": room, "with": list(a.get("with") or []), "src": a.get("src") or "playlist",
             "value": a.get("value") or "", "vol": int(_f(a.get("vol"), 30)), "fade": fade}
    if fade > 0:
        music["volFrom"] = 0
    sc = {"id": "alarm-" + str(a["id"]), "name": a.get("name") or ("Larm " + str(a.get("time") or "")),
          "again": "stop", "alarm": str(a["id"]), "music": music if a.get("music", True) else {"do": ""}}
    if a.get("screen", True):
        sc["screen"] = {"anim": "wake", "pic": "on"}
    if a.get("scene"):
        sc["scene"] = str(a["scene"])
    stop = _f(a.get("stop"), 0)
    if stop > 0:
        sc["end"] = {"use": True, "after": stop, "music": {"do": "fadeout", "fade": 20, "room": room, "with": music["with"]}}
    return sc


def alarm_schedule(a):
    return {"id": "alarm-" + str(a["id"]), "scene": "alarm-" + str(a["id"]), "on": bool(a.get("on", True)),
            "name": a.get("name") or "Larm", "days": a.get("days") if a.get("days") else list(range(7)),
            "when": {"kind": "time", "time": str(a.get("time") or "07:00")}}


# ---------- en pågående scen ----------
class Run:
    def __init__(self, sc, t0):
        self.sc = sc
        self.id = str(sc.get("id"))
        self.name = sc.get("name") or self.id
        self.t0 = t0
        self.stop = threading.Event()
        self.released = set()          # lampor (id) som någon ändrat för hand: scenen rör dem inte mer
        self.music_released = False
        self.end_at = None
        self.snap = None               # hur allt såg ut innan scenen (för "tryck igen = tillbaka")
        self.holding = False           # scenen har gjort sitt och väntar bara på att stängas av
        after = _f((sc.get("end") or {}).get("after"), 0)
        if (sc.get("end") or {}).get("use") and after > 0:
            self.end_at = t0 + after * 60

    def events(self):
        ev = [(0.0, "start", self.sc)]
        for st in self.sc.get("steps") or []:
            if isinstance(st, dict):
                ev.append((max(0.0, _f(st.get("at"), 0)) * 60, "step", st))
        if self.end_at:
            ev.append((self.end_at - self.t0, "end", self.sc.get("end") or {}))
        ev.sort(key=lambda e: e[0])
        return ev


class Timeline:
    def __init__(self, hub, state_file, log):
        self.hub, self.log = hub, log
        self.lights, self.sonos = hub.lights, hub.sonos
        self.effects = Effects(self.lights, log)
        self.state_file = state_file
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.runs = {}                 # scenens id -> Run
        self.lamp_fades = {}           # lampans id -> övergång
        self.vol_fades = {}            # rum -> övergång
        self.sent = {}                 # lampans id -> (tid, läge) som hubben senast satte
        self.fired = {}                # schemats nyckel -> datum då det senast kördes
        self.rule_seen = {}            # lampans id -> på/av senast lampreglerna såg den
        self.rule_hold = set()         # lampor som ändrats för hand: reglerna låter dem vara tills de släcks
        self.rule_win = {}             # regel -> om tidsfönstret var aktivt förra varvet
        self.sleep = None              # insomningstimern
        self.snoozed = {}              # larmets scen-id -> (när det ringer igen, timer)
        self.cfg = {"scenes": [], "schedules": [], "lampRules": [], "place": {}}
        self.cfg_at = 0
        self._load()

    # ---- disk ----
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                d = json.load(f)
            self.fired = d.get("fired") or {}
            self._resume = d.get("runs") or []
        except (OSError, ValueError):
            self._resume = []

    def _save(self):
        with self.lock:
            d = {"fired": self.fired, "runs": [{"id": r.id, "t0": r.t0, "released": sorted(r.released),
                                                "lamps": (r.snap or {}).get("lamps"), "holding": r.holding}
                                               for r in self.runs.values()]}
        try:
            with self.save_lock:
                tmp = self.state_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f)
                os.replace(tmp, self.state_file)
        except OSError as e:
            self.log("(varning: kunde inte spara tidsstyrningens läge: %s)" % e)

    def config(self, fresh=False):
        if fresh or time.time() - self.cfg_at > 20:
            try:
                c = self.hub._scenes() or {}
                cfg = {k: c.get(k) or ([] if k != "place" else {}) for k in ("scenes", "schedules", "lampRules", "place")}
                # larmen (från appen) blir egna scener med egna tider
                try:
                    if getattr(self.hub, "gamepad", None):
                        self.hub.gamepad.set_cfg(c.get("gamepad") or {})      # handkontrollens knappar
                except Exception as e:
                    self.log("(varning: handkontrollen: %s)" % e)
                try:
                    self.hub.lights.set_custom(c.get("lightGroups") or [])      # egna ljusgrupper från appen
                except Exception as e:
                    self.log("(varning: ljusgrupperna: %s)" % e)
                cfg["alarms"] = [a for a in (c.get("alarms") or []) if isinstance(a, dict) and a.get("id")]
                cfg["scenes"] = list(cfg["scenes"]) + [alarm_scene(a) for a in cfg["alarms"]]
                cfg["schedules"] = list(cfg["schedules"]) + [alarm_schedule(a) for a in cfg["alarms"]]
                self.cfg = cfg
                self.cfg_at = time.time()
            except Exception as e:
                self.log("(varning: kunde inte hämta scener och tider: %s)" % e)
        return self.cfg

    def scene(self, sid, fresh=True):
        return next((s for s in self.config(fresh)["scenes"] if str(s.get("id")) == str(sid)), None)

    # ---- starta och avbryta ----
    def start(self, sid, origin="knapp"):
        sc = self.scene(sid)
        if sc is None:
            self.log('(varning: hittar ingen scen med id "%s")' % sid)
            return
        with self.lock:
            old = self.runs.get(str(sid))
        mode = sc.get("again") or "restore"
        if old:
            again = mode
            if again == "restore":
                # andra trycket: scenen stängs av och allt blir som innan den startades
                self.stop_run(sid, restore=True)
                return
            if again == "ignore":
                self.log('  scen "%s" pågår redan, trycket ignoreras' % old.name)
                return
            if again == "stop":
                self.stop_run(sid, run_end=False)
                return
            if again == "end":
                self.stop_run(sid, run_end=True)
                return
            self.stop_run(sid, run_end=False, quiet=True)
        self.log('  -> scen: "%s" (%s)' % (sc.get("name") or sid, origin))
        if sc.get("alarm"):
            self.snoozed.pop(str(sid), None)
        timed = bool(sc.get("steps")) or ((sc.get("end") or {}).get("use") and _f((sc.get("end") or {}).get("after"), 0) > 0) \
            or any(isinstance(v, dict) and (_f(v.get("fade"), 0) > 0 or (v.get("effect") or {}).get("type")) for v in (sc.get("lights") or {}).values()) \
            or _f((sc.get("music") or {}).get("fade"), 0) > 0 or bool(sc.get("alarm"))
        run = Run(sc, time.time())
        if mode == "restore":
            run.snap = self._snapshot(sc)
            if not timed:
                # ingen tidslinje: gör det den ska och står sedan "på" tills knappen trycks igen
                run.holding = True
                run.released |= self._lamps_of(sc)
                with self.lock:
                    self.runs[run.id] = run
                self._apply(run, sc, "start", force=True)
                self._save()
                threading.Thread(target=self.report, daemon=True).start()
                return
        if not timed:
            # vanlig scen utan tidslinje: gör det den ska och är sedan klar
            self._apply(run, sc, "start")
            return
        with self.lock:
            self.runs[run.id] = run
            # en ny scen tar över lampor som en annan pågående scen styr
            mine = self._lamps_of(sc)
            for other in self.runs.values():
                if other is not run:
                    other.released |= mine
        self._save()
        threading.Thread(target=self._run_loop, args=(run, 0.0), daemon=True, name="scen-" + run.id).start()
        threading.Thread(target=self.report, daemon=True).start()

    def stop_run(self, sid, run_end=False, quiet=False, restore=None):
        """restore: None = återställ om scenen sparade hur det var innan (läget "tryck igen = tillbaka")."""
        with self.lock:
            ids = list(self.runs) if sid in ("*", "", None) else [str(sid)]
            runs = [self.runs.pop(i) for i in ids if i in self.runs]
        for run in runs:
            run.stop.set()
            self._drop_fades(run)
            back = (restore if restore is not None else True) and run.snap is not None and not run_end
            if not quiet:
                self.log('  scen "%s" avbryts%s' % (run.name, ", slutet körs" if run_end else ", allt tillbaka som innan" if back else ""))
            if run_end and run.sc.get("end"):
                self._apply(run, run.sc.get("end"), "end")
            if back:
                self._restore(run)
        if runs:
            self._save()
            self.report()

    # ---- hur allt var innan scenen ----
    def _snapshot(self, sc):
        phases = [sc] + list(sc.get("steps") or []) + [sc.get("end") or {}]
        ids = set(self._lamps_of(sc))
        for ph in phases:
            for m in (ph or {}).get("moods") or []:
                try:
                    entry = self.lights._lookup(str(m.get("group") or ""))[0] or {}
                    ids |= {str(l["id"]) for l in entry.get("lights") or []}
                except Exception:
                    pass
        lamps = {}
        if ids:
            try:
                lamps = self.lights.read_lamps(sorted(ids)) or {}
            except Exception as e:
                self.log("(varning: kunde inte läsa lamporna innan scenen: %s)" % e)
        rooms = []
        for ph in phases:
            m = (ph or {}).get("music") or {}
            if m.get("do"):
                rooms += [r for r in self._rooms(m) if r not in rooms]
        speakers, snaps = [], {}
        for r in rooms:
            dev = self.sonos.device(r)
            if dev is None:
                continue
            try:
                co = dev.group.coordinator
                speakers.append({"dev": dev, "co": co, "vol": int(dev.volume)})
                if co.uid not in snaps:
                    from soco.snapshot import Snapshot
                    sn = Snapshot(co)
                    sn.snapshot()
                    snaps[co.uid] = sn
            except Exception as e:
                self.log('(varning: kunde inte spara vad "%s" spelade: %s)' % (r, e))
        return {"lamps": lamps, "sonos": speakers, "snaps": snaps}

    def _restore(self, run):
        snap = run.snap or {}
        lamps = snap.get("lamps") or {}
        if lamps:
            for lid in lamps:
                self.effects.stop_lamp(lid)
            back = {}
            for lid, st in lamps.items():
                if not st.get("on"):
                    back[lid] = {"on": False}
                else:
                    back[lid] = {k: st[k] for k in ("on", "dim", "kind", "color", "temp") if k in st}
            try:
                self.lights.apply_lamps(back, transition=1.0)
                self.log("    lampor tillbaka: %d" % len(back))
            except Exception as e:
                self.log("(varning: kunde inte sätta tillbaka lamporna: %s)" % e)
        speakers, snaps = snap.get("sonos") or [], snap.get("snaps") or {}
        try:
            # högtalarna i samma grupper som innan
            for sp in speakers:
                dev, co = sp["dev"], sp["co"]
                cur = dev.group.coordinator
                if cur.uid != co.uid:
                    if co.uid == dev.uid:
                        dev.unjoin()
                    else:
                        dev.join(co)
            for uid, sn in snaps.items():
                was = str(getattr(sn, "transport_state", "")).upper()
                sn.restore(fade=False)
                if was != "PLAYING":
                    try:
                        sn.device.pause()
                    except Exception:
                        pass
            for sp in speakers:
                sp["dev"].volume = sp["vol"]
            if speakers:
                self.log("    Sonos tillbaka: %s" % ", ".join(sp["dev"].player_name for sp in speakers))
        except Exception as e:
            self.log("(varning: kunde inte sätta tillbaka Sonos: %s)" % e)

    def _drop_fades(self, run):
        self.effects.stop_run(run)
        with self.lock:
            for k in [k for k, f in self.lamp_fades.items() if f["run"] is run]:
                del self.lamp_fades[k]
            for k in [k for k, f in self.vol_fades.items() if f["run"] is run]:
                del self.vol_fades[k]

    def _lamps_of(self, sc):
        ids = set()
        for ph in [sc] + list(sc.get("steps") or []) + [sc.get("end") or {}]:
            for k, v in ((ph or {}).get("lights") or {}).items():
                if isinstance(v, dict) and v.get("use", True):
                    ids.add(str(k))
        return ids

    def try_effect(self, lid, setting, secs=30):
        """"Prova" på en lampa med effekt: effekten körs en halv minut."""
        class _Try:
            sc, id, name, released = {"manual": "keep"}, "prova", "prova", set()
            stop = threading.Event()
        t = _Try()
        lamp = next((l for l in self.lights.lamp_list() if str(l["id"]) == str(lid)), {})
        self.effects.start(t, str(lid), setting, setting.get("effect") or {}, lamp)
        threading.Timer(secs, t.stop.set).start()

    def controlled_lamps(self):
        with self.lock:
            out = set()
            for r in self.runs.values():
                out |= self._lamps_of(r.sc) - r.released
            return out

    # ---- tidslinjen ----
    def _run_loop(self, run, resume_from):
        for at, kind, ph in run.events():
            if at < resume_from:
                # redan passerat (efter en omstart): bara pågående övergångar tas upp igen
                self._apply(run, ph, kind, resume=resume_from - at)
                continue
            wait = run.t0 + at - time.time()
            if wait > 0 and run.stop.wait(wait):
                return
            if run.stop.is_set():
                return
            self._apply(run, ph, kind)
        # slutet är kört (eller scenen har inget slut men alla steg är gjorda)
        if not run.end_at:
            # vänta tills sista övergången är klar innan scenen räknas som slut
            while not run.stop.is_set():
                with self.lock:
                    busy = any(f["run"] is run for f in list(self.lamp_fades.values()) + list(self.vol_fades.values())) \
                        or self.effects.busy(run)
                if not busy:
                    break
                run.stop.wait(2)
        if run.stop.is_set():
            return                      # avbruten: stop_run har redan städat
        if (run.snap is not None or run.sc.get("alarm")) and not run.end_at and not run.stop.is_set():
            # "tryck igen = tillbaka": scenen står kvar tills knappen trycks igen
            run.holding = True
            with self.lock:
                run.released |= self._lamps_of(run.sc)
            self._save()
            self.report()
            if not run.sc.get("alarm"):
                self.log('  scen "%s" har gjort sitt; tryck på knappen igen för att gå tillbaka' % run.name)
            return
        with self.lock:
            if self.runs.get(run.id) is run:
                del self.runs[run.id]
        self._save()
        self.report()
        self.log('  scen "%s" är klar' % run.name)

    def _apply(self, run, ph, kind, resume=None, force=False):
        """Kör en del av scenen. resume = sekunder sedan delen egentligen kördes (efter en
        omstart): då görs inget direkt, bara övergångar som inte hunnit bli klara tas upp igen."""
        if not isinstance(ph, dict):
            return
        if resume is None and kind != "start":
            self.log('  scen "%s": %s' % (run.name, "slutet" if kind == "end" else "steg efter %g min" % _f(ph.get("at"), 0)))
        if resume is None:
            if ph.get("screen"):
                threading.Thread(target=self.hub._scene_screen, args=(ph.get("screen"),), daemon=True).start()
            for m in ph.get("moods") or []:
                if m.get("group"):
                    threading.Thread(target=self.lights.run_scene, args=(str(m.get("group")), str(m.get("mood") or ""), not m.get("mood")), daemon=True).start()
        self._apply_lights(run, ph.get("lights") or {}, resume, force)
        self._apply_music(run, ph.get("music") or {}, resume)
        if resume is None and ph.get("scene") and str(ph.get("scene")) != run.id:
            threading.Thread(target=self.start, args=(ph.get("scene"), "från scenen " + run.name), daemon=True).start()

    def _apply_lights(self, run, lights, resume, force=False):
        lamps = {str(k): v for k, v in lights.items() if isinstance(v, dict) and v.get("use", True) and (force or str(k) not in run.released)}
        if not lamps:
            return
        now, direct, fading = time.time(), {}, {}
        for lid, s in lamps.items():
            # en lampa som får ett nytt läge slutar med sin effekt; har läget en effekt startar den
            self.effects.stop_lamp(lid)
            eff = s.get("effect") or {}
            if eff.get("type") and s.get("on", True) is not False:
                lamp = next((l for l in self.lights.lamp_list() if str(l["id"]) == lid), {})
                self.effects.start(run, lid, s, eff, lamp)
                continue
            fade = max(0.0, _f(s.get("fade"), 0))
            if resume is not None:
                if fade > resume:
                    fading[lid] = (s, fade - resume)
                continue
            if fade > 0:
                fading[lid] = (s, fade)
            else:
                direct[lid] = s
        if direct:
            try:
                n = self.lights.apply_lamps(direct)
                for lid, s in direct.items():
                    self.sent[lid] = (now, dict(s))
                if run.end_at or run.sc.get("steps"):
                    self.log("    lampor: %d satta" % n)
            except Exception as e:
                self.log("(varning: scenens lampor: %s)" % e)
        if not fading:
            return
        # startläget: ett eget "från"-läge, annars hur lampan står nu
        need = [lid for lid, (s, _) in fading.items() if not (isinstance(s.get("from"), dict) and s["from"].get("use") and resume is None)]
        cur = {}
        if need:
            try:
                cur = self.lights.read_lamps(need)
            except Exception:
                cur = {}
        froms = {}
        for lid, (s, dur) in fading.items():
            fr = s.get("from") if isinstance(s.get("from"), dict) and s["from"].get("use") and resume is None else None
            if fr:
                a = dict(fr, on=True)
                froms[lid] = a
            else:
                c = cur.get(lid) or {}
                if not c.get("on"):
                    # släckt lampa tonas upp från nästan mörkt, i scenens färg
                    a = dict(s, on=True, dim=1)
                else:
                    a = dict(c)
            target = dict(s)
            off_at_end = s.get("on") is False
            if off_at_end:
                target = dict(a, dim=1)
            with self.lock:
                self.lamp_fades[lid] = {"run": run, "a": a, "b": target, "t0": now, "dur": dur, "last": 0, "off": off_at_end}
        if froms:
            try:
                self.lights.apply_lamps(froms)
                for lid, a in froms.items():
                    self.sent[lid] = (now, dict(a))
            except Exception as e:
                self.log("(varning: scenens lampor: %s)" % e)
        self.log("    lampor: %d tonas under %s" % (len(fading), ", ".join(sorted({"%g min" % round(d / 60, 1) if d >= 60 else "%d s" % d for _, d in fading.values()}))))

    def _rooms(self, m):
        room = m.get("room") or self.sonos.cfg.get("wake_room") or (self.sonos.room_names() or [""])[0]
        return [r for r in [room] + list(m.get("with") or []) if r]

    def _apply_music(self, run, m, resume):
        do = m.get("do")
        if not do or run.music_released:
            return
        fade = max(0.0, _f(m.get("fade"), 0))
        rooms = self._rooms(m)
        if resume is not None:
            # efter omstart: en volymövergång som inte blivit klar fortsätter från nuvarande volym
            if do in ("play", "vol") and m.get("vol") is not None and fade > resume:
                self._vol_fade(run, rooms, None, _f(m.get("vol")), fade - resume, False)
            elif do == "fadeout" and fade > resume:
                self._vol_fade(run, rooms, None, 0, fade - resume, True)
            return
        if do == "fadeout":
            if fade <= 0:
                threading.Thread(target=self.sonos.scene, args=(dict(m, do="pause"),), daemon=True).start()
            else:
                self.log("    musiken tonas ut under %g s" % fade)
                self._vol_fade(run, rooms, None, 0, fade, True)
            return
        if do == "vol" and fade > 0 and m.get("vol") is not None:
            self._vol_fade(run, rooms, None, _f(m.get("vol")), fade, False)
            return
        if do == "play" and fade > 0:
            vol = m.get("vol")
            if vol is None:
                vol = self.sonos.cfg.get("wake_volume", 45)
            frm = m.get("volFrom")
            start_vol = max(0, min(100, int(_f(frm, 0)))) if frm is not None else 0
            # spelar med startvolymen, sedan tonar hubben upp
            self.sonos.scene(dict(m, vol=start_vol, fade=0))
            self._vol_fade(run, rooms, start_vol, _f(vol), fade, False)
            return
        threading.Thread(target=self.sonos.scene, args=(m,), daemon=True).start()

    def _vol_fade(self, run, rooms, start, target, dur, pause_after):
        now = time.time()
        for r in rooms:
            dev = self.sonos.device(r)
            if dev is None:
                continue
            try:
                a = _f(dev.volume) if start is None else start
            except Exception:
                a = 0
            with self.lock:
                self.vol_fades[r] = {"run": run, "dev": dev, "a": a, "b": target, "t0": now, "dur": dur, "last": 0,
                                     "set": None, "pause": pause_after, "restore": a}

    # ---- övergångarna, i små steg ----
    def fade_loop(self):
        last_check = 0
        while True:
            time.sleep(TICK)
            try:
                self._fade_tick()
                if time.time() - last_check > MANUAL_CHECK_S:
                    last_check = time.time()
                    self._manual_check()
            except Exception as e:
                self.log("(varning: tidsstyrningens övergångar: %s)" % e)
                time.sleep(3)

    def _fade_tick(self):
        now = time.time()
        with self.lock:
            lamps = list(self.lamp_fades.items())
            vols = list(self.vol_fades.items())
        batch, step_for, done = {}, {}, []
        for lid, f in lamps:
            step = max(LAMP_STEP_MIN, min(30.0, f["dur"] / 150.0))
            if now - f["last"] < step and now < f["t0"] + f["dur"]:
                continue
            k = (now + step - f["t0"]) / f["dur"]
            if k >= 1:
                val = dict(f["b"])
                if f["off"]:
                    val = {"on": False}
                done.append(lid)
            else:
                val = mix_lamp(f["a"], f["b"], k)
            f["last"] = now
            batch[lid] = val
            step_for[lid] = 0.4 if k >= 1 and f["off"] else step
        # Har någon släckt eller dimrat en lampa för hand sedan förra steget? Läses av precis före
        # varje steg, annars skulle övergången tända den igen.
        check = [lid for lid in batch if lid in self.sent and now - self.sent[lid][0] > 1.2]
        if check:
            try:
                cur = self.lights.read_lamps(check)
            except Exception:
                cur = {}
            for lid in check:
                c, want, f = cur.get(lid), self.sent[lid][1], dict(lamps).get(lid)
                if not c or not f:
                    continue
                step = max(LAMP_STEP_MIN, min(30.0, f["dur"] / 150.0))
                per_s = abs(_f(f["b"].get("dim"), 0) - _f(f["a"].get("dim"), 0)) / max(1.0, f["dur"])
                if bool(c.get("on")) != bool(want.get("on", True)) or \
                        (c.get("on") and abs(_f(c.get("dim"), 0) - _f(want.get("dim"), 0)) > 8 + per_s * step * 2):
                    self._manual(f["run"], lamp=lid)
                    if lid not in self.lamp_fades or self.lamp_fades[lid]["run"] is not f["run"]:
                        batch.pop(lid, None)
                        step_for.pop(lid, None)
        # en sändning per stegtid (lamporna i samma steg skickas tillsammans)
        for st in sorted(set(step_for.values())):
            part = {lid: v for lid, v in batch.items() if step_for.get(lid) == st}
            if not part:
                continue
            try:
                self.lights.apply_lamps(part, transition=st)
                for lid, v in part.items():
                    self.sent[lid] = (now, dict(v))
            except Exception as e:
                self.log("(varning: övergång: %s)" % e)
        with self.lock:
            for lid in done:
                if lid in self.lamp_fades and self.lamp_fades[lid]["last"] == now:
                    del self.lamp_fades[lid]
        for room, f in vols:
            step = max(VOL_STEP_MIN, min(10.0, f["dur"] / 100.0))
            if now - f["last"] < step and now < f["t0"] + f["dur"]:
                continue
            f["last"] = now
            dev = f["dev"]
            try:
                cur = int(dev.volume)
                if f["set"] is not None and abs(cur - f["set"]) > 3:
                    # någon har ändrat volymen själv: scenen slutar styra musiken
                    self._manual(f["run"], music=True)
                    continue
                k = (now - f["t0"]) / f["dur"]
                v = round(f["a"] + (f["b"] - f["a"]) * min(1.0, k))
                if v != cur:
                    dev.volume = max(0, min(100, v))
                f["set"] = v
                if k >= 1:
                    with self.lock:
                        self.vol_fades.pop(room, None)
                    if f["pause"]:
                        try:
                            dev.group.coordinator.pause()
                            time.sleep(0.5)
                            dev.volume = int(f["restore"])      # nästa gång börjar musiken på vanlig volym
                        except Exception:
                            pass
            except Exception as e:
                self.log("(varning: volymövergång i %s: %s)" % (room, e))
                with self.lock:
                    self.vol_fades.pop(room, None)

    def _manual(self, run, lamp=None, music=False):
        mode = run.sc.get("manual") or "release"
        if mode == "keep":
            return
        if mode == "stop":
            self.log('  scen "%s": ändrades för hand, scenen avbryts' % run.name)
            threading.Thread(target=self.stop_run, args=(run.id, False), daemon=True).start()
            return
        with self.lock:
            if lamp is not None:
                run.released.add(lamp)
                f = self.lamp_fades.get(lamp)
                if f and f["run"] is run:
                    del self.lamp_fades[lamp]
                self.log('  scen "%s": en lampa ändrades för hand, scenen släpper den' % run.name)
            if music:
                run.music_released = True
                for k in [k for k, f in self.vol_fades.items() if f["run"] is run]:
                    del self.vol_fades[k]
                self.log('  scen "%s": volymen ändrades för hand, scenen släpper musiken' % run.name)
        self._save()

    def manual_touch(self, ids, change):
        """En ratt eller knapp ändrade de här lamporna. Pågående scener tar inte tillbaka det:
        - en lampa med effekt behåller effekten, men med den nya ljusstyrkan/färgen som grund
          (åskan blixtrar kring det nya ljuset, Skärmfärg får ett nytt tak); släcks den slutar effekten
        - övriga lampor släpps av scenen (tonar inte vidare, nästa steg rör dem inte)
        Scenens val "Avbryt helt vid ändring för hand" gäller fortfarande. Lampregler håller sig också undan."""
        ids = {str(i) for i in ids}
        with self.lock:
            runs = [r for r in self.runs.values() if (self._lamps_of(r.sc) - r.released) & ids]
        for lid in ids:
            self.rule_hold.add(lid)
        for run in runs:
            if (run.sc.get("manual") or "release") == "stop":
                self._manual(run, lamp=next(iter(ids)))
                continue
            for lid in (self._lamps_of(run.sc) - run.released) & ids:
                if change.get("on") is not False and self.effects.adjust(lid, change):
                    continue
                self.effects.stop_lamp(lid)
                with self.lock:
                    run.released.add(lid)
                    f = self.lamp_fades.get(lid)
                    if f and f["run"] is run:
                        del self.lamp_fades[lid]
            self.log('  scen "%s": lampor ändrade med ratt/knapp, scenen tar inte tillbaka dem' % run.name)
        if runs:
            self._save()

    def _manual_check(self):
        with self.lock:
            watch = {lid: f for lid, f in self.lamp_fades.items()}
        if not watch:
            return
        try:
            cur = self.lights.read_lamps(list(watch))
        except Exception:
            return
        now = time.time()
        for lid, f in watch.items():
            sent = self.sent.get(lid)
            c = cur.get(lid)
            if not sent or not c or now - sent[0] < 1.5:
                continue
            want = sent[1]
            if bool(c.get("on")) != bool(want.get("on", True)):
                self._manual(f["run"], lamp=lid)
                continue
            if now - sent[0] < 4:
                continue
            # dimringen under en övergång: lampan kan ligga mitt i sitt steg, så stor marginal
            step = max(LAMP_STEP_MIN, min(30.0, f["dur"] / 150.0))
            per_s = abs(_f(f["b"].get("dim"), 0) - _f(f["a"].get("dim"), 0)) / max(1.0, f["dur"])
            if abs(_f(c.get("dim"), 0) - _f(want.get("dim"), 0)) > 8 + per_s * step * 2:
                self._manual(f["run"], lamp=lid)

    # ---- efter omstart ----
    def resume(self):
        time.sleep(15)                  # låt gatewayen och Sonos hinna svara
        items, self._resume = self._resume, []
        for it in items:
            sc = self.scene(it.get("id"))
            if sc is None:
                continue
            run = Run(sc, _f(it.get("t0"), time.time()))
            run.released = set(it.get("released") or [])
            if it.get("lamps") is not None:
                run.snap = {"lamps": it.get("lamps"), "sonos": []}
            if it.get("holding"):
                run.holding = True
                with self.lock:
                    self.runs[run.id] = run
                self.log('  scen "%s" är fortfarande på (tryck igen för att gå tillbaka)' % run.name)
                continue
            elapsed = time.time() - run.t0
            last = max([e[0] for e in run.events()] + [0])
            if elapsed > last + 1800:
                continue                # för länge sedan: låt det vara
            self.log('  scen "%s" fortsätter efter omstart (%d min in)' % (run.name, elapsed / 60))
            with self.lock:
                self.runs[run.id] = run
            threading.Thread(target=self._run_loop, args=(run, elapsed), daemon=True, name="scen-" + run.id).start()
        self.report()

    # ---- schema ----
    def schedule_loop(self):
        while True:
            try:
                self._schedule_tick()
            except Exception as e:
                self.log("(varning: schemat: %s)" % e)
            time.sleep(15)

    def _schedule_tick(self):
        cfg = self.config()
        now = dt.datetime.now()
        changed = False
        for sch in cfg["schedules"]:
            if not isinstance(sch, dict) or not sch.get("on", True) or not sch.get("scene"):
                continue
            key = str(sch.get("id"))
            for back in (0, 1):                     # i dag, och i går (för tider strax före midnatt)
                day = now.date() - dt.timedelta(days=back)
                if day.weekday() not in (sch.get("days") or list(range(7))):
                    continue
                t = when_on(day, sch.get("when"), cfg["place"])
                if t is None or t > now or (now - t).total_seconds() > CATCH_UP_S:
                    continue
                stamp = t.strftime("%Y-%m-%d %H:%M")
                if self.fired.get(key) == stamp:
                    continue
                self.fired[key] = stamp
                changed = True
                self.log('Schema: %s (%s)' % (sch.get("name") or describe_when(sch.get("when")), stamp))
                threading.Thread(target=self.start, args=(sch.get("scene"), "schema"), daemon=True).start()
        if changed:
            self._save()

    def upcoming(self, days=7):
        cfg = self.cfg
        now = dt.datetime.now()
        out = []
        for sch in cfg["schedules"]:
            if not isinstance(sch, dict) or not sch.get("on", True):
                continue
            for d in range(days + 1):
                day = now.date() + dt.timedelta(days=d)
                if day.weekday() not in (sch.get("days") or list(range(7))):
                    continue
                t = when_on(day, sch.get("when"), cfg["place"])
                if t and t > now:
                    out.append({"id": sch.get("id"), "scene": sch.get("scene"), "at": t.timestamp()})
                    break
        return out

    # ---- lampregler ----
    def rule_values(self, rule, now=None):
        """Värdena regeln ger just nu, eller None om regeln inte gäller nu."""
        cfg = self.cfg
        now = now or dt.datetime.now()
        for back in (0, 1):
            day = now.date() - dt.timedelta(days=back)
            if day.weekday() not in (rule.get("days") or list(range(7))):
                continue
            a = when_on(day, rule.get("from"), cfg["place"])
            b = when_on(day, rule.get("to"), cfg["place"])
            if a is None or b is None:
                continue
            if b <= a:
                b += dt.timedelta(days=1)           # över midnatt
            if a <= now < b:
                val = dict(rule.get("set") or {}, on=True)
                glide = rule.get("glide")
                if isinstance(glide, dict) and glide.get("use"):
                    k = (now - a).total_seconds() / max(1.0, (b - a).total_seconds())
                    val = mix_lamp(val, dict(glide, on=True), k)
                return val
        return None

    def rules_loop(self):
        while True:
            time.sleep(float(self.hub.cfg.get("rules_poll_s", 1.5)))
            try:
                self._rules_tick()
            except Exception as e:
                self.log("(varning: lampreglerna: %s)" % e)
                time.sleep(10)

    def _rules_tick(self):
        cfg = self.config()
        rules = [r for r in cfg["lampRules"] if isinstance(r, dict) and r.get("on", True) and r.get("lamps")]
        if not rules:
            self.rule_seen.clear()
            return
        busy = self.controlled_lamps()
        now = dt.datetime.now()
        want = {}                                   # lampa -> (regel, värden); första regeln i listan vinner
        for r in rules:
            vals = self.rule_values(r, now)
            rid = str(r.get("id"))
            started = vals is not None and not self.rule_win.get(rid)
            self.rule_win[rid] = vals is not None
            if vals is None:
                continue
            for lid in r.get("lamps"):
                lid = str(lid)
                if lid not in want and lid not in busy:
                    want[lid] = (r, vals, started)
        if not want:
            return
        cur = self.lights.read_lamps(list(want))
        apply_now, fade_in = {}, {}
        for lid, (r, vals, started) in want.items():
            c = cur.get(lid)
            if c is None:
                continue
            on, was = bool(c.get("on")), self.rule_seen.get(lid)
            self.rule_seen[lid] = on
            if not on:
                self.rule_hold.discard(lid)
                continue
            if was is False:
                apply_now[lid] = vals                # tändes nyss: regelns värden direkt
                self.rule_hold.discard(lid)
                continue
            if lid in self.rule_hold:
                continue
            sent = self.sent.get(lid)
            if r.get("manualHold", True) and sent and time.time() - sent[0] > 4 and abs(_f(c.get("dim")) - _f(sent[1].get("dim"))) > 8:
                self.rule_hold.add(lid)              # ändrad för hand: låt den vara tills den släcks
                continue
            if started and r.get("alsoOn"):
                fade_in[lid] = (vals, max(0.0, _f(r.get("alsoOnFade"), 0)) * 60)
            elif (r.get("glide") or {}).get("use") and sent and time.time() - sent[0] > 60:
                apply_now[lid] = vals                # glider sakta mot regelns slutvärden
        if apply_now:
            self.lights.apply_lamps(apply_now, transition=1)
            for lid, v in apply_now.items():
                self.sent[lid] = (time.time(), dict(v))
        for lid, (vals, dur) in fade_in.items():
            if dur <= 0:
                self.lights.apply_lamps({lid: vals}, transition=1)
                self.sent[lid] = (time.time(), dict(vals))
            else:
                c = cur.get(lid) or {}
                with self.lock:
                    self.lamp_fades[lid] = {"run": _RULE_RUN, "a": dict(c), "b": dict(vals), "t0": time.time(), "dur": dur, "last": 0, "off": False}

    # ---- status till deck-redigeraren ----
    # ---- larm: stäng av och snooza ----
    def alarm_cmd(self, rest):
        """alarm:stop:<id>  alarm:snooze:<id>[:<min>]  (id utan "alarm-")"""
        what, _, arg = rest.partition(":")
        aid, _, mins = arg.partition(":")
        sid = "alarm-" + aid if aid else None
        runs = [r for r in list(self.runs.values()) if r.sc.get("alarm") and (sid is None or r.id == sid)]
        for run in runs:
            self.stop_run(run.id, restore=False)
            for room in self._rooms(run.sc.get("music") or {}):
                dev = self.sonos.device(room)
                try:
                    if dev is not None:
                        dev.group.coordinator.pause()
                except Exception:
                    pass
            if what == "snooze":
                a = next((x for x in self.cfg.get("alarms", []) if "alarm-" + str(x.get("id")) == run.id), {})
                m = _f(mins, 0) or _f(a.get("snooze"), 9) or 9
                self.log('  larmet "%s" snoozar %g min' % (run.name, m))
                t = threading.Timer(m * 60, self.start, args=(run.id, "snooze"))
                t.daemon = True
                t.start()
                self.snoozed[run.id] = (time.time() + m * 60, t)
            else:
                self.log('  larmet "%s" stängs av' % run.name)
        if what == "stop":
            # ett stopp tar också bort en väntande snooze
            for k in [k for k in self.snoozed if sid is None or k == sid]:
                self.snoozed.pop(k)[1].cancel()
        self.report()

    # ---- insomningstimer ----
    def sleep_cmd(self, rest):
        """sleep:{"room", "min", "fade", "lights", "screen"}   sleep:off   sleep:add:<min>"""
        if rest == "off":
            if self.sleep:
                self.log("  insomningstimern stängs av")
            self.sleep = None
            self.report()
            return
        if rest.startswith("add:"):
            if self.sleep:
                self.sleep["end"] += _f(rest[4:], 10) * 60
                self.log("  insomningstimern förlängs till %s" % time.strftime("%H:%M", time.localtime(self.sleep["end"])))
            self.report()
            return
        try:
            o = json.loads(rest or "{}")
        except ValueError:
            return
        room = o.get("room") or self.sonos.cfg.get("wake_room") or (self.sonos.room_names() or [""])[0]
        mins = max(1.0, _f(o.get("min"), 30))
        st = {"room": room, "end": time.time() + mins * 60, "fade": max(0.0, min(30.0, _f(o.get("fade"), 5))) * 60,
              "lights": o.get("lights") or "", "screen": bool(o.get("screen")), "id": time.time()}
        self.sleep = st
        self.log('  insomningstimer: %s tystnar %s' % (room, time.strftime("%H:%M", time.localtime(st["end"]))))
        threading.Thread(target=self._sleep_loop, args=(st,), daemon=True, name="insomning").start()
        self.report()

    def _sleep_loop(self, st):
        dev, start_vol, last = None, None, None
        while self.sleep is st:
            left = st["end"] - time.time()
            if left <= 0:
                break
            if left > st["fade"]:
                time.sleep(min(5, left - st["fade"]))
                continue
            # tonas ut mot slutet
            if dev is None:
                dev = self.sonos.device(st["room"])
                try:
                    start_vol = int(dev.volume)
                except Exception:
                    start_vol = None
            if dev is not None and start_vol is not None:
                try:
                    cur = int(dev.volume)
                    if last is not None and cur > last + 3:
                        self.log("  insomningstimern: volymen höjdes för hand, timern stängs av")
                        self.sleep = None
                        self.report()
                        return
                    v = max(0, round(start_vol * left / max(1.0, st["fade"])))
                    if v != cur:
                        dev.volume = v
                    last = v
                except Exception:
                    pass
            time.sleep(2)
        if self.sleep is not st:
            if dev is not None and start_vol is not None:
                try:
                    dev.volume = start_vol           # avbruten under uttoningen: volymen tillbaka
                except Exception:
                    pass
            return
        self.sleep = None
        self.log("  insomningstimern: god natt")
        try:
            dev = dev or self.sonos.device(st["room"])
            if dev is not None:
                dev.group.coordinator.pause()
                if start_vol is not None:
                    time.sleep(1)
                    dev.volume = start_vol           # nästa gång börjar musiken på samma volym som innan
        except Exception as e:
            self.log("(varning: insomningstimern: %s)" % e)
        if st["lights"]:
            for g in str(st["lights"]).split(","):
                if g.strip():
                    self.hub.run_cmd("light:" + json.dumps({"group": g.strip(), "action": "off", "value": None}), "local")
        if st["screen"]:
            self.hub._scene_screen({"power": "close"})
        self.report()

    def report(self):
        with self.lock:
            runs = [{"id": r.id, "name": r.name, "t0": r.t0, "end": r.end_at, "alarm": r.sc.get("alarm") or None, "holding": r.holding,
                     "next": min([r.t0 + e[0] for e in r.events() if r.t0 + e[0] > time.time()] or [0]) or None}
                    for r in self.runs.values()]
        try:
            sl = self.sleep
            self.hub.post_json("/api/tradfri-list", {"runs": runs, "upcoming": self.upcoming(),
                                                     "sleep": {"room": sl["room"], "end": sl["end"], "fade": sl["fade"]} if sl else {},
                                                     "snoozed": {k: v[0] for k, v in self.snoozed.items() if v[0] > time.time()},
                                                     "timers": self.hub.app.report_data() if getattr(self.hub, "app", None) else []})
        except Exception:
            pass

    def report_loop(self):
        while True:
            self.report()
            time.sleep(30)

    def start_loops(self):
        for fn in (self.fade_loop, self.schedule_loop, self.rules_loop, self.report_loop, self.resume):
            threading.Thread(target=fn, name="tid-" + fn.__name__, daemon=True).start()


class _RuleRun:
    """Står i stället för en scen när en lampregel tonar en lampa."""
    sc = {"manual": "keep"}
    id = "lampregel"
    name = "lampregel"
    released = set()


_RULE_RUN = _RuleRun()
