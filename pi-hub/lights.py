"""
Trådfri-belysningen. Flyttad hit från monitor.py på kameradatorn, där den har gått
mot den riktiga gatewayen: samma namn-till-id-cache på disk, samma direkta PUT mot
gruppens id (ingen listning av gatewayen i vanliga fall) och samma seriella kö som
slår ihop snabba kommandon.

Nytt här, för rattarna och växla-knappar:
  toggle     läser gruppens läge från gatewayen och sätter det motsatta
  dim-rel    ljusare/mörkare i procentenheter, utgår från gruppens verkliga ljusstyrka
  temp-rel   varmare/kallare, utgår från det senast satta värdet
  hue-rel    stegar runt färghjulet (nyans/mättnad, för färglampor)

Kommandoformatet är det gamla "light:"-formatet:
  {"group": "vardagsrum", "action": "on|off|toggle|dim|dim-rel|temp|temp-rel|color|hue-rel|scene", "value": ...}
"""
import colorsys
import json
import os
import queue
import shutil
import threading
import time

try:
    from pytradfri.api.libcoap_api import APIFactory
    from pytradfri.gateway import Gateway
    from pytradfri.command import Command
    from pytradfri.const import (ROOT_GROUPS, ATTR_MOOD, ATTR_DEVICE_STATE, ATTR_LIGHT_DIMMER, ATTR_LIGHT_MIREDS,
                                 ATTR_LIGHT_COLOR_HEX, ATTR_LIGHT_COLOR_HUE, ATTR_LIGHT_COLOR_SATURATION, ATTR_TRANSITION_TIME,
                                 ROOT_DEVICES, ATTR_LIGHT_CONTROL, ATTR_SWITCH_PLUG,
                                 ATTR_LIGHT_COLOR_X, ATTR_LIGHT_COLOR_Y)
    HAVE_TRADFRI = True
except ImportError:
    HAVE_TRADFRI = False

class _OpenSession:
    """Håller EN krypterad anslutning till gatewayen öppen (pytradfris aiocoap-variant), så att
    ett kommando tar några hundradelar i stället för att varje gång starta coap-client och
    förhandla fram en ny anslutning. Körs i en egen tråd med egen händelseslinga; anropas som
    en vanlig funktion, precis som den gamla varianten."""

    def __init__(self, host, identity, psk):
        import asyncio
        import DTLSSocket  # noqa: F401  (saknas den finns ingen krypterad transport)
        from pytradfri.api.aiocoap_api import APIFactory as AsyncFactory
        self._asyncio = asyncio
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="tradfri-session", daemon=True).start()
        self.factory = self._run(AsyncFactory.init(host, psk_id=identity, psk=psk), 15)

    def _run(self, coro, timeout):
        return self._asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def __call__(self, command):
        return self._run(self.factory.request(command), 10)

    def close(self):
        try:
            self._run(self.factory.shutdown(), 5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


MIRED_COLD, MIRED_WARM = 250, 454      # samma intervall som pytradfri (RANGE_MIREDS)
STATE_MAX_AGE_S = 3                    # så länge litar vi på gruppens senast lästa läge
REL_ACTIONS = ("dim-rel", "temp-rel", "hue-rel")

# Takt för rattsteg. Kommandon går till varje lampa för sig (riktade, kvitterade anrop), inte till
# gruppen: gruppanrop kvitteras inte och får bara skickas några stycken på ett par sekunder, så
# där tappas både rattsteg och tänd/släck. IKEA-lampor struntar dessutom i nya kommandon medan
# de glider mot ett värde. Därför: samla rattstegen och skicka högst ett kommando per intervall,
# med en glidning som hinner bli klar innan nästa kommer.
# (sekunder mellan kommandon, glidning i tiondels sekunder)
STEP_PACE = {"dim-rel": (0.4, 3), "temp-rel": (0.4, 3), "hue-rel": (0.4, 3)}
HUE_MAX, SAT_MAX = 65535, 65279
WHEEL_WHITE, WHEEL_HUE0 = 105, 210     # färghjulet: så många steg vitt (varmt till kallt), sedan färgerna från blått


def as_int(v, d):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return d


def xy_to_hs(x, y):
    """CIE xy (0-65535) till nyans i grader och mättnad 0-1."""
    x, y = x / 65535, (y or 1) / 65535
    X, Y, Z = x / y, 1.0, (1 - x - y) / y
    rgb = [3.2406 * X - 1.5372 * Y - 0.4986 * Z, -0.9689 * X + 1.8758 * Y + 0.0415 * Z, 0.0557 * X - 0.2040 * Y + 1.0570 * Z]
    rgb = [max(0.0, c) for c in rgb]
    m = max(rgb) or 1
    rgb = [c / m for c in rgb]
    rgb = [12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in rgb]
    h, s, _ = colorsys.rgb_to_hsv(*rgb)
    return h * 360, s


def hs_to_xy(hue, sat):
    """Nyans (grader) och mättnad (0-1) till CIE xy, för färglampor som inte tar nyans direkt."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in colorsys.hsv_to_rgb(hue / 360, sat, 1.0)]
    X = 0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]
    Y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    Z = 0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]
    t = X + Y + Z or 1
    return round(X / t * 65535), round(Y / t * 65535)


class Lights:
    def __init__(self, cfg, log, on_vals=None, ids_file=None):
        self.cfg = cfg or {}
        self.log = log
        self.on_vals = on_vals or (lambda vals: None)
        self.ids_file = ids_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradfri_ids_cache.json")
        self.api = None
        self.gateway = None
        self.session = None                   # öppen anslutning (snabb), om den går att få
        self.session_fails = 0
        self.session_off = (self.cfg.get("transport") or "auto") == "coap-client"
        self.lock = threading.Lock()          # ett anrop i taget mot gatewayen
        self.ids_lock = threading.Lock()
        self.ids = {}
        self.custom = []                      # egna ljusgrupper från appen: [{"name", "lamps": [id, ...]}]
        self.track = {}                       # gruppnyckel -> {"dim", "temp", "hue", "on", "at"}
        self.lamps_tried = False              # id-listan har lästs om en gång för att få med lamporna
        self.own_at = 0.0
        self.busy_until = 0.0                 # lamporna glider fortfarande mot förra värdet till dess
        self.q = queue.Queue()
        self._load_ids()
        threading.Thread(target=self._worker, name="lights", daemon=True).start()

    @property
    def enabled(self):
        return HAVE_TRADFRI and bool(self.cfg.get("host") and self.cfg.get("identity") and self.cfg.get("psk"))

    # ---- id-cachen ----
    def _load_ids(self):
        try:
            with open(self.ids_file, encoding="utf-8") as f:
                self.ids = json.load(f)
        except Exception:
            self.ids = {}

    def _save_ids(self):
        try:
            tmp = self.ids_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.ids, f)
            os.replace(tmp, self.ids_file)
        except Exception as e:
            self.log("  (varning: kunde inte spara Trådfri-id-cachen: %s)" % e)

    def _get_api(self):
        """Ger funktionen som skickar ett kommando till gatewayen. I första hand över en öppen
        anslutning; går den inte att få (paket saknas, gatewayen svarar inte) används det gamla
        sättet med coap-client, som är långsammare men beprövat."""
        if not self.enabled:
            return None
        if self.gateway is None:
            self.gateway = Gateway()
        if not self.session_off and self.session is None:
            try:
                self.session = _OpenSession(self.cfg["host"], self.cfg["identity"], self.cfg["psk"])
                self.log("Trådfri: öppen anslutning till gatewayen (snabba kommandon).")
            except Exception as e:
                self.session_off = True
                self.log("Trådfri: ingen öppen anslutning (%s: %s). Använder coap-client i stället, lite långsammare." % (type(e).__name__, e))
        if self.api is None and shutil.which("coap-client"):
            try:
                self.api = APIFactory(host=self.cfg["host"], psk_id=self.cfg["identity"], psk=self.cfg["psk"]).request
            except Exception:
                self.api = None
        return self._send

    def _send(self, command):
        if self.session is not None:
            try:
                out = self.session(command)
                self.session_fails = 0
                self.session_ok = True
                return out
            except Exception as e:
                self.session_fails += 1
                self.log("Trådfri: den öppna anslutningen svarade inte (%s: %s)" % (type(e).__name__, e or "ingen förklaring"))
                if self.api is not None and not getattr(self, "session_ok", False):
                    # har aldrig fungerat här: vänta inte ut fler försök, använd coap-client direkt
                    self.log("Trådfri: använder coap-client i stället (långsammare, men fungerar).")
                    self.session.close()
                    self.session, self.session_off = None, True
                    return self.api(command)
                if self.api is None and self.session_fails < 3:
                    # ingen coap-client att falla tillbaka på: koppla upp på nytt och försök en gång till
                    try:
                        self.session.close()
                        self.session = _OpenSession(self.cfg["host"], self.cfg["identity"], self.cfg["psk"])
                        out = self.session(command)
                        self.session_fails = 0
                        return out
                    except Exception as e2:
                        self.log("Trådfri: inte heller efter ny uppkoppling (%s: %s)" % (type(e2).__name__, e2 or "ingen förklaring"))
                if self.session_fails >= 3:
                    self.log("Trådfri: den öppna anslutningen har misslyckats tre gånger i rad (%s). Går över till coap-client." % e)
                    self.session.close()
                    self.session, self.session_off = None, True
                if self.api is None:
                    raise
        if self.api is None:
            raise RuntimeError("ingen kontakt med gatewayen (varken öppen anslutning eller coap-client)")
        return self.api(command)

    def refresh(self):
        """Full genomgång av grupper, lampor och scener (trög: flera anrop i rad). Görs vid start,
        någon gång per dygn och när någon trycker Uppdatera på sidan."""
        api = self._get_api()
        if api is None:
            return None
        with self.lock:
            groups = api(api(self.gateway.get_groups()))
            lamps, sockets, inputs, outlets, other = None, set(), {}, {}, {}
            try:
                lamps = {}
                for d in api(api(self.gateway.get_devices())):
                    if d.application_type in (0, 4, 7, 8) or (not d.has_light_control and not d.has_socket_control
                                                               and getattr(d.device_info, "battery_level", None) is not None):
                        # fjärrkontroller, rörelsesensorer och andra batteridrivna givare
                        info = d.device_info
                        inputs[d.id] = {"id": d.id, "name": d.name, "model": getattr(info, "model_number", "") or "",
                                        "battery": getattr(info, "battery_level", None), "reachable": bool(getattr(d, "reachable", True)),
                                        "kind": "motion" if d.application_type == 4 else "remote"}
                        continue
                    if d.has_light_control and d.light_control.lights:
                        l = d.light_control.lights[0]
                        info = d.device_info
                        lamps[d.id] = {"id": d.id, "name": d.name, "model": getattr(info, "model_number", "") or "",
                                       "color": "hs" if l.supports_hsb_xy_color else "xy" if l.supports_xy_color else None,
                                       "temp": bool(l.supports_color_temp), "dim": bool(l.supports_dimmer)}
                    elif d.has_socket_control or d.application_type == 3:
                        sockets.add(d.id)
                        outlets[d.id] = {"id": d.id, "name": d.name, "model": getattr(d.device_info, "model_number", "") or "",
                                         "color": None, "temp": False, "dim": False, "outlet": True}
                    else:
                        other[d.id] = {"id": d.id, "name": d.name, "model": getattr(d.device_info, "model_number", "") or "",
                                       "type": d.application_type}
            except Exception as e:
                lamps = None
                self.log("  (varning: kunde inte läsa lamplistan, styr hela grupper i stället: %s)" % e)
            ids = {}
            for g in groups:
                try:
                    moods = api(api(self.gateway.get_moods(g.id)))
                except Exception:
                    moods = []
                entry = {"id": g.id, "name": g.name,
                         "moods": {m.name.strip().lower(): {"id": m.id, "name": m.name} for m in moods}}
                if lamps is not None:
                    try:
                        members = list(g.member_ids)
                        entry["lights"] = [lamps[m] for m in members if m in lamps]
                        entry["sockets"] = [m for m in members if m in sockets]
                        entry["inputs"] = [inputs[m] for m in members if m in inputs]
                    except Exception:
                        pass
                ids[g.name.strip().lower()] = entry
        with self.ids_lock:
            self.ids = ids
            if lamps is not None:
                self.outlets = outlets
                self.other = list(other.values())
        self._save_ids()
        for e in ids.values():
            if e.get("lights"):
                self.log('  Trådfri "%s": %s' % (e["name"], ", ".join("%s [%s; %s]" % (
                    l["name"], l["model"], "färg" if l["color"] else "vitt spektrum" if l["temp"] else "bara ljusstyrka") for l in e["lights"])))
        return ids

    def group_list(self):
        with self.ids_lock:
            return [{"name": v["name"], "moods": [m["name"] for m in v["moods"].values()],
                     "lamps": [str(l.get("id")) for l in v.get("lights") or [] if isinstance(l, dict)]} for v in self.ids.values()]

    # ---- enskilda lampor (egna scener) ----
    def lamp_list(self):
        """Alla lampor en gång var, med rummet de hör till (den minsta namngivna gruppen de är med i)."""
        with self.ids_lock:
            groups = [e for e in self.ids.values() if e.get("lights")]
            outlets = dict(getattr(self, "outlets", {}) or {})
        groups.sort(key=lambda e: (e["name"].lower().startswith("tradfri group"), len(e["lights"])))
        seen = {}
        for e in groups:
            for l in e["lights"]:
                if l["id"] not in seen:
                    seen[l["id"]] = dict(l, room=e["name"])
        # smarta uttag: i det rum (den grupp) de hör till, annars under "Uttag"
        for o in outlets.values():
            room = next((e["name"] for e in groups if o["id"] in (e.get("sockets") or [])), None)
            if room is None:
                with self.ids_lock:
                    room = next((e["name"] for e in sorted(self.ids.values(), key=lambda e: len(e.get("sockets") or []))
                                 if o["id"] in (e.get("sockets") or []) and not e["name"].lower().startswith("tradfri group")), "Uttag")
            seen[o["id"]] = dict(o, room=room)
        return sorted(seen.values(), key=lambda l: (l["room"].lower(), l["name"].lower()))

    def _lamp_colour_values(self, lamp, s, transition=5):
        """Färg eller vit ton för en lampa i en scen, i det format just den lampan tar."""
        kind = s.get("kind")
        if kind == "white" and (lamp["temp"] or lamp["color"]):
            _, _, values_for = self._white(max(0, min(100, as_int(s.get("temp"), 35))), transition)
            return values_for(lamp)
        if kind == "color" and lamp["color"]:
            hexval = str(s.get("color") or "").lstrip("#")
            try:
                r, g, b = (int(hexval[i:i + 2], 16) / 255 for i in (0, 2, 4))
            except ValueError:
                return None
            h, sat, _ = colorsys.rgb_to_hsv(r, g, b)
            _, _, values_for = self._colour(round(h * 360) % 360, sat, transition)
            return values_for(lamp)
        return None

    def apply_lamps(self, settings, transition=None):
        """En egen scen: varje lampa får sitt eget läge. settings = { lampans id: { on, dim, kind, temp, color } }.
        Först tänds, släcks och dimras alla; därefter sätts färgerna, eftersom en släckt lampa inte
        tar emot färg och en lampa inte byter ljusstyrka och färg i samma kommando."""
        api = self._get_api()
        if api is None or not settings:
            return 0
        lamps = {str(l["id"]): l for l in self.lamp_list()}
        if not lamps:
            try:
                self.refresh()
            except Exception as e:
                self.log("  (varning: kunde inte läsa lamplistan: %s)" % e)
            lamps = {str(l["id"]): l for l in self.lamp_list()}
        first, second, missing = [], [], 0
        for lid, s in settings.items():
            lamp = lamps.get(str(lid))
            if lamp is None or not isinstance(s, dict):
                missing += 1
                continue
            if lamp.get("outlet"):
                first.append((lamp, {ATTR_DEVICE_STATE: 1 if s.get("on", True) else 0}))
                continue
            if not s.get("on", True):
                first.append((lamp, {ATTR_DEVICE_STATE: 0}))
                continue
            v = {ATTR_DEVICE_STATE: 1}
            if s.get("dim") is not None:
                v[ATTR_LIGHT_DIMMER] = round(max(1, min(100, float(s.get("dim") or 100))) / 100 * 254)
                if transition is not None:
                    v[ATTR_TRANSITION_TIME] = max(0, min(600, round(transition * 10)))   # tiondels sekunder
            first.append((lamp, v))
            c = self._lamp_colour_values(lamp, s, 5 if transition is None else max(0, min(600, round(transition * 10))))
            if c:
                second.append((lamp, c))
        ok = 0

        def send(batch):
            nonlocal ok
            self._wait_idle()
            with self.lock:
                for lamp, v in batch:
                    try:
                        api(Command("put", [ROOT_DEVICES, str(lamp["id"])], {ATTR_SWITCH_PLUG if lamp.get("outlet") else ATTR_LIGHT_CONTROL: [dict(v)]}))
                        ok += 1
                    except Exception as e:
                        self.log('  (varning: lampan "%s" svarade inte: %s)' % (lamp["name"], e))
            self._mark_busy(None)
        send(first)
        done = ok
        if second:
            time.sleep(0.25)
            send(second)
        self.track.clear()
        if missing:
            self.log("  (scenen innehåller %d lampor som inte längre finns hos gatewayen)" % missing)
        return done

    def fast_set(self, items, transition=0.3):
        """Snabba ändringar (Skärmfärg): exakt ett kommando per lampa, antingen ljusstyrka eller
        färg, utan väntan mellan. items = { lampans id: {"dim": ...} eller {"kind": ..., ...} }."""
        api = self._get_api()
        if api is None or not items:
            return 0
        lamps = {str(l["id"]): l for l in self.lamp_list()}
        tt = max(0, min(600, round(transition * 10)))
        ok = 0
        if True:
            for lid, s in items.items():
                lamp = lamps.get(str(lid))
                if lamp is None or lamp.get("outlet"):
                    continue
                if s.get("kind"):
                    v = self._lamp_colour_values(lamp, s, tt)
                else:
                    v = {ATTR_LIGHT_DIMMER: round(max(1, min(100, float(s.get("dim") or 1))) / 100 * 254),
                         ATTR_TRANSITION_TIME: tt}
                if not v:
                    continue
                try:
                    with self.lock:          # ett i taget, så att andra kommandon kan komma emellan
                        api(Command("put", [ROOT_DEVICES, str(lamp["id"])], {ATTR_LIGHT_CONTROL: [dict(v)]}))
                    ok += 1
                except Exception as e:
                    self.log('  (varning: lampan "%s" svarade inte: %s)' % (lamp["name"], e))
        self.own_at = time.time()
        return ok

    def read_lamps(self, ids=None):
        """Lampornas läge just nu, i samma form som en scen sparar dem (för "Hämta nuvarande läge").
        ids: bara de här lamporna."""
        api = self._get_api()
        out = {}
        if api is None:
            return out
        want = None if ids is None else {str(i) for i in ids}
        for lamp in self.lamp_list():
            if want is not None and str(lamp["id"]) not in want:
                continue
            try:
                with self.lock:
                    dev = api(self.gateway.get_device(lamp["id"]))
                if lamp.get("outlet"):
                    out[str(lamp["id"])] = {"on": bool(dev.socket_control.raw[0].state)}
                    continue
                raw = dev.light_control.raw[0]
            except Exception:
                continue
            s = {"on": bool(raw.state), "dim": max(1, round((raw.dimmer or 0) / 254 * 100))}
            hue = sat = None
            if lamp["color"] == "hs" and getattr(raw, "color_hue", None) is not None:
                hue, sat = getattr(raw, "color_hue", None) / HUE_MAX * 360, (getattr(raw, "color_saturation", None) or 0) / SAT_MAX
            elif lamp["color"] and getattr(raw, "color_xy_x", None):
                hue, sat = xy_to_hs(getattr(raw, "color_xy_x", None), getattr(raw, "color_xy_y", None))
            if hue is not None and not (sat < 0.78 and (hue < 50 or hue > 345)) and sat > 0.12:
                r, g, b = colorsys.hsv_to_rgb(hue / 360, sat, 1)
                s.update(kind="color", color="#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255)))
            elif lamp["temp"] and getattr(raw, "color_mireds", None):
                s.update(kind="white", temp=max(0, min(100, round((MIRED_WARM - getattr(raw, "color_mireds", None)) / (MIRED_WARM - MIRED_COLD) * 100))))
            elif sat is not None:
                s.update(kind="white", temp=max(0, min(100, round((0.70 - sat) / 0.65 * 100))))
            out[str(lamp["id"])] = s
        return out

    def set_custom(self, groups):
        """Egna ljusgrupper (byggs i appen under Inställningar > Ljusgrupper). De går före
        Trådfris egna grupper; ett namn som inte finns bland dem söks som vanligt hos Trådfri."""
        out = []
        for g in groups or []:
            if isinstance(g, dict) and str(g.get("name") or "").strip():
                out.append({"name": str(g["name"]).strip(), "lamps": [str(x) for x in g.get("lamps") or []]})
        if [(g["name"], g["lamps"]) for g in out] != [(g["name"], g["lamps"]) for g in self.custom]:
            self.custom = out
            self.log("  Ljusgrupper: %s" % (", ".join("%s (%d)" % (g["name"], len(g["lamps"])) for g in out) if out else "Trådfris egna"))

    def _custom_entry(self, key):
        g = next((g for g in self.custom if g["name"].lower() == key), None) \
            or next((g for g in self.custom if key in g["name"].lower()), None)
        if g is None:
            return None
        allx = {str(l["id"]): l for l in self.lamp_list()}
        want = [allx[i] for i in g["lamps"] if i in allx]
        tr = self.ids.get(g["name"].lower()) or {}
        return {"id": tr.get("id"), "name": g["name"], "moods": tr.get("moods") or {}, "custom": True,
                "lights": [l for l in want if not l.get("outlet")], "sockets": [l["id"] for l in want if l.get("outlet")]}

    def _lookup(self, name):
        key = (name or "").strip().lower()
        if not key:
            return None, key
        if self.custom:
            e = self._custom_entry(key)
            if e is not None:
                return e, "egen:" + e["name"].lower()
        with self.ids_lock:
            if key in self.ids:
                return self.ids[key], key
            return next((v for k, v in self.ids.items() if key in k), None), key

    def _entry(self, name):
        entry, key = self._lookup(name)
        if entry is None and self.enabled:
            self.log("  (Trådfri: ingen sparad id-lista för \"%s\", läser om från gatewayen)" % name)
            try:
                self.refresh()
            except Exception as e:
                self.log("  (varning: kunde inte läsa Trådfri-listan: %s)" % e)
            entry, key = self._lookup(name)
        return entry, key

    def _put(self, group_id, data):
        api = self._get_api()
        if api is None:
            return False
        self._wait_idle()
        t0 = time.time()
        with self.lock:
            api(Command("put", [ROOT_GROUPS, str(group_id)], data))
        self.last_ms = int((time.time() - t0) * 1000)
        self._mark_busy(data.get(ATTR_TRANSITION_TIME))
        return True

    def _put_all(self, entry, values, pick=None, values_for=None):
        """Samma värden till var och en av gruppens lampor (riktade anrop). pick väljer vilka lampor,
        values_for kan ge en lampa egna värden. Saknas lamplistan går kommandot till gruppen.
        Ger antalet lampor som tog emot det."""
        api = self._get_api()
        if api is None:
            raise RuntimeError("ingen kontakt med gatewayen")
        if "lights" not in entry:
            self._put(entry["id"], values)
            return -1
        cmds = []
        for l in entry["lights"]:
            if pick is None or pick(l):
                v = values_for(l) if values_for else values
                if v:
                    cmds.append(Command("put", [ROOT_DEVICES, str(l["id"])], {ATTR_LIGHT_CONTROL: [dict(v)]}))
        if ATTR_DEVICE_STATE in values and len(values) == 1:       # rena tänd/släck gäller även uttag
            for sid in entry.get("sockets", []):
                cmds.append(Command("put", [ROOT_DEVICES, str(sid)], {ATTR_SWITCH_PLUG: [dict(values)]}))
        self._wait_idle()
        t0, ok, err = time.time(), 0, None
        with self.lock:
            for c in cmds:
                try:
                    api(c); ok += 1
                except Exception as e:
                    err = e
        self.last_ms = int((time.time() - t0) * 1000)
        self._mark_busy(values.get(ATTR_TRANSITION_TIME))
        if cmds and not ok:
            raise err
        if err is not None:
            self.log("  (varning: %d av %d lampor svarade inte: %s)" % (len(cmds) - ok, len(cmds), err))
        return ok

    def _wait_idle(self):
        left = self.busy_until - time.time()
        if left > 0:
            time.sleep(min(left, 1.0))

    def set_outlet(self, outlet_id, action):
        """Ett smart uttag: on, off eller toggle."""
        api = self._get_api()
        if api is None:
            return
        o = next((l for l in self.lamp_list() if l.get("outlet") and str(l["id"]) == str(outlet_id)), None)
        if o is None:
            self.log('  (varning: hittar inget uttag med nummer "%s")' % outlet_id)
            return
        try:
            with self.lock:
                if action == "toggle":
                    on = not bool(api(self.gateway.get_device(o["id"])).socket_control.raw[0].state)
                else:
                    on = action != "off"
                api(Command("put", [ROOT_DEVICES, str(o["id"])], {ATTR_SWITCH_PLUG: [{ATTR_DEVICE_STATE: 1 if on else 0}]}))
            self.log('  -> Trådfri: uttaget "%s" %s' % (o["name"], "på" if on else "av"))
            self.on_vals({"plug:%s" % o["id"]: on})
        except Exception as e:
            self.log('  (varning: uttaget "%s" svarade inte: %s)' % (o["name"], e))

    def input_list(self):
        """Fjärrkontroller och rörelsesensorer hos gatewayen, med gruppen var och en styr."""
        with self.ids_lock:
            entries = list(self.ids.values())
        out = {}
        for e in entries:
            for i in e.get("inputs") or []:
                o = out.setdefault(i["id"], dict(i, src="tradfri", key="tradfri:%s" % i["id"], groups=[]))
                o["groups"].append(e["name"])
        return sorted(out.values(), key=lambda i: i["name"].lower())

    def group_mood(self, group_name):
        """Namnet på gruppens aktiva Trådfri-scen (gemener), "" om ingen, None om det inte gick att läsa."""
        entry = self._lookup(group_name)[0]
        api = self._get_api()
        if entry is None or api is None or entry.get("id") is None:
            return None
        try:
            with self.lock:
                mid = api(self.gateway.get_group(entry["id"])).mood_id
        except Exception:
            return None
        for name, m in (entry.get("moods") or {}).items():
            if str(m.get("id")) == str(mid):
                return name
        return ""

    def group_any_on(self, group_name):
        """Är någon lampa i gruppen tänd just nu? None om det inte gick att läsa."""
        entry = self._lookup(group_name)[0]
        api = self._get_api()
        if entry is None or api is None or not entry.get("lights"):
            return None
        on, got = False, False
        with self.lock:
            for l in entry["lights"]:
                try:
                    on = on or bool(api(self.gateway.get_device(l["id"])).light_control.raw[0].state)
                    got = True
                except Exception:
                    pass
        return on if got else None

    def _mark_busy(self, transition):
        self.own_at = time.time()             # hubben ändrade själv ljuset nu (se sensorbevakningen)
        # utan angiven tid glider lamporna en halv sekund av sig själva
        self.busy_until = time.time() + (0.5 if transition is None else transition / 10.0) + 0.05

    def _read_group(self, entry, key):
        """Gruppens verkliga läge (på/av, ljusstyrka), med kort cache. Läses från lamporna själva:
        gatewayens uppgift om gruppen följer inte med när en fjärrkontroll eller en annan grupp ändrar."""
        tr = self.track.setdefault(key, {})
        if time.time() - tr.get("at", 0) < STATE_MAX_AGE_S and "dim" in tr and "on" in tr:
            return tr
        api = self._get_api()
        try:
            if entry.get("lights"):
                on, dims = False, []
                with self.lock:
                    for l in entry["lights"]:
                        try:
                            raw = api(self.gateway.get_device(l["id"])).light_control.raw[0]
                        except Exception:
                            continue
                        if raw.state:
                            on = True
                            dims.append(raw.dimmer or 0)
                        elif not on:
                            dims.append(raw.dimmer or 0)
                if not dims:
                    raise RuntimeError("ingen lampa svarade")
                tr["on"], tr["dim"] = on, max(1, round(max(dims) / 254 * 100))
            else:
                if entry.get("id") is None:
                    raise RuntimeError("gruppen har inga lampor")
                with self.lock:
                    g = api(self.gateway.get_group(entry["id"]))
                tr["on"] = bool(g.state)
                tr["dim"] = max(1, round((g.dimmer or 0) / 254 * 100))
            tr["at"] = time.time()
        except Exception as e:
            self.log("  (varning: kunde inte läsa läget för \"%s\": %s)" % (entry["name"], e))
            tr.setdefault("on", False)
            tr.setdefault("dim", 60)
        return tr

    # ---- scener (väckning, jalusi stängt, scen-knappar) ----
    def run_scene(self, group_name, mood_name, turn_on_if_no_mood):
        if not group_name:
            return
        entry, key = self._entry(group_name)
        if entry is None:
            self.log('  (varning: hittar ingen Trådfri-grupp som heter "%s")' % group_name)
            return
        self.own_at = time.time()
        mood_id = None
        if mood_name:
            mood = entry["moods"].get(mood_name.strip().lower())
            if mood is None:
                self.log('  (varning: hittar ingen scen som heter "%s" i "%s")' % (mood_name, group_name))
                if not turn_on_if_no_mood:
                    return
            else:
                mood_id = mood["id"]
        elif not turn_on_if_no_mood:
            return
        data = {ATTR_DEVICE_STATE: 1}
        if mood_id is not None:
            data[ATTR_MOOD] = mood_id
        try:
            if entry.get("custom") and (mood_id is None or entry.get("id") is None):
                self._put_all(entry, {ATTR_DEVICE_STATE: 1})     # egen grupp: tänd dess lampor
            else:
                self._put(entry["id"], data)
            self.track.setdefault(key, {}).update({"on": True, "at": 0})
            self.on_vals({"on:" + group_name.strip(): True})
            self.log('  -> Trådfri: %s i "%s"' % (("scen \"%s\"" % mood_name) if mood_id is not None else "tänder", entry["name"]))
        except Exception as e:
            self.log("  (varning: Trådfri-scenen misslyckades: %s)" % e)

    # ---- light:-kommandon ----
    def submit(self, cmd):
        self.q.put(cmd)

    def _worker(self):
        """Vanliga kommandon utförs i tur och ordning. Rattsteg samlas per grupp och åtgärd och
        skickas i takt (STEP_PACE): det första direkt, resten hopslagna en gång per intervall."""
        pending, next_at = {}, {}
        while True:
            timeout = None
            if pending:
                timeout = max(0.0, min(next_at.get(k, 0) for k in pending) - time.time())
            try:
                cmd = self.q.get(timeout=timeout)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                k = (cmd.get("group"), cmd.get("action"))
                if cmd.get("action") in REL_ACTIONS:
                    try:
                        total = (pending[k].get("value") or 0) + (cmd.get("value") or 0) if k in pending else (cmd.get("value") or 0)
                    except TypeError:
                        total = cmd.get("value") or 0
                    pending[k] = dict(cmd, value=total)
                else:
                    # ett fast värde (tänd, släck, ljusstyrka 40 ...) gör väntande steg av samma slag inaktuella
                    for pk in [pk for pk in pending if pk[0] == k[0] and (k[1] in ("on", "off", "toggle", "scene") or pk[1].startswith(k[1]))]:
                        del pending[pk]
                    self._safe_apply(cmd)
            now = time.time()
            for k in [k for k in pending if now >= next_at.get(k, 0)]:
                c = pending.pop(k)
                next_at[k] = time.time() + float(self.cfg.get("step_interval", STEP_PACE[k[1]][0]))
                if c.get("value"):
                    self._safe_apply(c)

    def _safe_apply(self, cmd):
        try:
            self.apply(cmd)
        except Exception as e:
            self.log("  (varning: ljuskommando misslyckades: %s)" % e)

    @staticmethod
    def _colour(hue, sat, transition):
        """Värden för en färg: nyans/mättnad till lampor som tar det, xy till övriga färglampor."""
        hs = {ATTR_LIGHT_COLOR_HUE: round(hue / 360 * HUE_MAX), ATTR_LIGHT_COLOR_SATURATION: round(sat * SAT_MAX)}
        x, y = hs_to_xy(hue, sat)
        xy = {ATTR_LIGHT_COLOR_X: x, ATTR_LIGHT_COLOR_Y: y}
        if transition is not None:
            hs[ATTR_TRANSITION_TIME] = xy[ATTR_TRANSITION_TIME] = transition
        return hs, (lambda l: l["color"]), (lambda l: hs if l["color"] == "hs" else xy)

    @classmethod
    def _white(cls, pct, transition):
        """Värden för vanligt vitt ljus, 0 = varmast, 100 = kallast. Lampor med vitt spektrum får
        färgtemperaturen; rena färglampor får samma vita ton uttryckt som färg."""
        mired = {ATTR_LIGHT_MIREDS: round(MIRED_WARM - pct / 100 * (MIRED_WARM - MIRED_COLD))}
        if transition is not None:
            mired[ATTR_TRANSITION_TIME] = transition
        _, _, as_colour = cls._colour(29, 0.70 - 0.65 * pct / 100, transition)
        return mired, (lambda l: l["temp"] or l["color"]), (lambda l: mired if l["temp"] else as_colour(l))

    def apply(self, cmd):
        group_name = (cmd.get("group") or "").strip()
        action, value = cmd.get("action"), cmd.get("value")
        if not group_name or not action:
            self.log("  (varning: ljus, ofullständigt kommando %r)" % (cmd,))
            return
        if not self.enabled:
            self.log("  (ljus: Trådfri är inte inställt i hub_config.json, hoppar över %r)" % (cmd,))
            return
        if action == "scene":
            self.run_scene(group_name, str(value or "").strip(), turn_on_if_no_mood=False)
            return
        entry, key = self._entry(group_name)
        if entry is None:
            self.log('  (varning: ljus, hittar ingen Trådfri-grupp som heter "%s")' % group_name)
            return
        if "lights" not in entry and not self.lamps_tried:
            self.lamps_tried = True                    # äldre id-lista utan lamporna: läs om en gång
            try:
                self.refresh()
            except Exception as e:
                self.log("  (varning: kunde inte läsa Trådfri-listan: %s)" % e)
            entry = self._lookup(group_name)[0] or entry
        tr = self.track.setdefault(key, {})
        vals = {}
        name = entry["name"]
        glide = True
        pick = values_for = None


        if action == "on":
            data = {ATTR_DEVICE_STATE: 1}; tr["on"] = True; tr["at"] = 0; vals["on:" + group_name] = True
        elif action == "off":
            data = {ATTR_DEVICE_STATE: 0}; tr["on"] = False; tr["at"] = 0; vals["on:" + group_name] = False
        elif action == "toggle":
            tr["at"] = 0                               # läs alltid färskt: en fjärrkontroll kan ha ändrat läget
            on = not self._read_group(entry, key)["on"]
            data = {ATTR_DEVICE_STATE: 1 if on else 0}; tr["on"] = on; vals["on:" + group_name] = on
        elif action in ("dim", "dim-rel"):
            st = self._read_group(entry, key)
            was_on = st["on"]
            if action == "dim":
                pct = max(1, min(100, as_int(value, 100)))
            else:
                # släckt lampa: börja från noll, så att en vridning uppåt tänder den svagt
                pct = max(1, min(100, (st["dim"] if was_on else 0) + as_int(value, 0)))
            data = {ATTR_LIGHT_DIMMER: round(pct / 100 * 254)}
            if not was_on:
                data[ATTR_DEVICE_STATE] = 1                # ljusstyrka ska alltid tända en släckt lampa
                glide = False                              # ... och lampor tänds inte av ett värde med glidtid
            tr["dim"] = pct; tr["on"] = True; tr["at"] = time.time()
            vals["dim:" + group_name] = pct; vals["on:" + group_name] = True
        elif action in ("temp", "temp-rel"):
            if action == "temp":
                pct = max(0, min(100, as_int(value, 50)))
            else:
                pct = max(0, min(100, tr.get("temp", 35) + as_int(value, 0)))
            data, pick, values_for = self._white(pct, STEP_PACE["temp-rel"][1] if action == "temp-rel" else None)
            tr["temp"] = pct; vals["temp:" + group_name] = pct
        elif action == "color":
            hexval = str(value or "").lstrip("#")
            try:
                r, g, b = (int(hexval[i:i + 2], 16) / 255 for i in (0, 2, 4))
                if len(hexval) != 6:
                    raise ValueError
            except ValueError:
                self.log('  (varning: ljus, ogiltig färgkod "%s")' % value)
                return
            h, sat, _ = colorsys.rgb_to_hsv(r, g, b)
            hue = round(h * 360) % 360
            data, pick, values_for = self._colour(hue, sat, None)
            tr["hue"] = hue; tr["wheel"] = WHEEL_WHITE + (hue - WHEEL_HUE0) % 360; vals["hue:" + group_name] = hue
        elif action == "hue-rel":
            # Färghjulet: ett varv är först vanligt vitt ljus, från varmt till kallt, och sedan alla
            # färger. Det går runt åt båda håll och börjar om vid slutet. (Högst ett kvarts varv per
            # kommando, så att lampan aldrig tar "genvägen" åt fel håll.)
            pos = (tr.get("wheel", WHEEL_WHITE * 0.35) + max(-90, min(90, as_int(value, 0)))) % (WHEEL_WHITE + 360)
            tr["wheel"] = pos
            t = STEP_PACE["hue-rel"][1]
            if pos < WHEEL_WHITE:
                pct = round(pos / WHEEL_WHITE * 100)
                data, pick, values_for = self._white(pct, t)
                tr["temp"] = pct; vals["temp:" + group_name] = pct
                value = "%s (vitt %d %%)" % (value, pct)
            else:
                hue = round(WHEEL_HUE0 + pos - WHEEL_WHITE) % 360
                data, pick, values_for = self._colour(hue, 1.0, t)
                tr["hue"] = hue; vals["hue:" + group_name] = hue
                value = "%s (nyans %d°)" % (value, hue)
        if action in ("color", "hue-rel", "temp", "temp-rel") and "lights" in entry and not any(pick(l) for l in entry["lights"]):
            self.log('  (ljus: ingen lampa i "%s" klarar %s, hoppar över)' % (name, action))
            return
        if action not in ("on", "off", "toggle", "dim", "dim-rel", "temp", "temp-rel", "color", "hue-rel"):
            self.log('  (varning: ljus, okänd åtgärd "%s")' % action)
            return
        # en ratt eller knapp ändrar lamporna: en pågående scen (eller effekt) ska inte ta tillbaka det
        cb = getattr(self, "on_manual", None)
        if cb and entry.get("lights"):
            change = {}
            if action in ("on", "off", "toggle"):
                change["on"] = bool(data.get(ATTR_DEVICE_STATE))
            if "dim:" + group_name in vals:
                change["dim"] = vals["dim:" + group_name]
            if "temp:" + group_name in vals:
                change.update(kind="white", temp=vals["temp:" + group_name])
            elif "hue:" + group_name in vals:
                rr, gg, bb = colorsys.hsv_to_rgb(vals["hue:" + group_name] / 360.0, 1.0 if action == "hue-rel" else 0.9, 1)
                change.update(kind="color", color="#%02x%02x%02x" % (round(rr * 255), round(gg * 255), round(bb * 255)))
            try:
                cb([str(l["id"]) for l in entry["lights"]], change)
            except Exception as e:
                self.log("  (varning: scenen kunde inte släppa lamporna: %s)" % e)
        if action in REL_ACTIONS and glide:
            # rattsteg: låt lampan glida till det nya värdet i stället för att hoppa (tiondels sekunder)
            data.setdefault(ATTR_TRANSITION_TIME, STEP_PACE[action][1])
        try:
            n = self._put_all(entry, data, pick, values_for)
            self.log('  -> Trådfri: %s=%r i "%s"%s (%d ms)' % (action, value, name, (" på %d lampor" % n) if n >= 0 else "",
                                                           getattr(self, "last_ms", 0)))
            if vals:
                self.on_vals(vals)
        except Exception as e:
            self.log('  (varning: Trådfri "%s" i "%s" misslyckades: %s)' % (action, name, e))
