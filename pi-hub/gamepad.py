"""
Handkontrollen (Stadia-kontrollen eller en annan spelkontroll) styr systemet.

Kontrollen kopplas till Pi:n med USB-kabel eller med Bluetooth. Över Bluetooth går det bara om
Stadia-kontrollen har bytts till Bluetooth-läge; para den med "Para handkontroll" i
deck-redigeraren (Handkontroll) medan du håller Stadia-knappen och Y inne tills den blinkar orange.

Knapparna ställs in i deck-redigeraren (deck.gamepad): { "room", "group", "map": { knapp: kommando } }.
Knappnamnen är Linux egna (BTN_SOUTH, HAT0X- …), så att alla kontroller fungerar; redigeraren
visar vilken knapp som trycktes senast. Kommandona är hubbens vanliga (scene:<id>, light:…,
open, page:next …) eller de här korta:
  gp:nav-up/-down/-left/-right/-ok/-back (markören i Nest Hub-appen)
  gp:play gp:next gp:prev gp:vol+ gp:vol- gp:screen gp:tv-off gp:light gp:dim+ gp:dim- gp:warmer
  gp:colder gp:nest gp:page gp:viz
Volym och ljusstyrka upprepas så länge knappen hålls inne.

Kommando:  gamepad:pair   letar efter kontrollen med Bluetooth i 25 s och parar den.
"""
import json
import os
import re
import select
import subprocess
import threading
import time

# Standard: Stadia-kontrollens knappar som Linux brukar se dem. Det som skiljer sig hos en
# viss kontroll ställs in i redigeraren.
DEFAULT_MAP = {
    # Nest Hub-appen: styrkorset/vänster spak flyttar markören, A trycker, B går tillbaka.
    # Stadia-kontrollen skickar styrkorset som BTN_TRIGGER_HAPPY1-4 (vänster, höger, upp, ner)
    # över USB; andra kontroller som HAT0X/HAT0Y.
    "BTN_TRIGGER_HAPPY1": "gp:nav-left", "BTN_TRIGGER_HAPPY2": "gp:nav-right",
    "BTN_TRIGGER_HAPPY3": "gp:nav-up", "BTN_TRIGGER_HAPPY4": "gp:nav-down",
    "HAT0Y-": "gp:nav-up", "HAT0Y+": "gp:nav-down", "HAT0X-": "gp:nav-left", "HAT0X+": "gp:nav-right",
    "ABS_Y-": "gp:nav-up", "ABS_Y+": "gp:nav-down", "ABS_X-": "gp:nav-left", "ABS_X+": "gp:nav-right",
    "BTN_SOUTH": "gp:nav-ok",      # A
    "BTN_EAST": "gp:nav-back",     # B
    # direkt, var man än är
    "BTN_WEST": "gp:play",         # X (vänster knapp): spela/paus
    "BTN_NORTH": "gp:light",       # Y (översta knappen): ljusgruppen av/på
    "BTN_TL": "gp:vol-", "BTN_TR": "gp:vol+",                      # L1/R1
    "ABS_BRAKE+": "gp:prev", "ABS_GAS+": "gp:next",              # L2/R2 (Stadia)
    "BTN_TL2": "gp:prev", "BTN_TR2": "gp:next",
    # höger spak upp/ner: ljusare/mörkare (Stadia: ABS_RZ, andra: ABS_RY)
    "ABS_RZ-": "gp:dim+", "ABS_RZ+": "gp:dim-", "ABS_RY-": "gp:dim+", "ABS_RY+": "gp:dim-",
    "BTN_START": "gp:screen",      # Meny: Blaupunkt-skärmen öppen/stängd
    "BTN_SELECT": "gp:tv-off",     # Alternativ: stäng tv/film på skärmen
    "BTN_MODE": "gp:nest",         # Stadia-knappen: appen på Nest Hub
}
MAX_REPEAT = {"gp:vol+": 6, "gp:vol-": 6, "gp:dim+": 8, "gp:dim-": 8}   # högst så många upprepningar per tryck
REPEAT = {"gp:vol+", "gp:vol-", "gp:dim+", "gp:dim-", "gp:warmer", "gp:colder", "gp:nav-up", "gp:nav-down", "gp:nav-left", "gp:nav-right"}


class Gamepad:
    def __init__(self, hub, log):
        self.hub, self.log = hub, log
        self.cfg = {}
        self.dev_name, self.last, self.last_at = "", "", 0.0
        self.held = {}                 # knapp -> när den trycktes (för upprepning)
        self.screen_open = True
        try:
            import evdev  # noqa: F401
        except ImportError:
            log("(handkontrollen: python3-evdev saknas, kör uppdatera-pi.bat)")
            return
        threading.Thread(target=self._loop, daemon=True, name="handkontroll").start()
        threading.Thread(target=self._repeat_loop, daemon=True, name="handkontroll-upprepa").start()

    # ---- inlärning: knapparna gör ingenting, Nest Hub lär sig vilken knapp som är vilken ----
    def learning(self):
        return time.time() < getattr(self, "learn_until", 0)

    def command(self, rest):
        if rest == "learn:on":
            self.learn_until = time.time() + 300
            self.held.clear()
            self.log("  Handkontroll: inlärning på (knapparna gör inget annat i 5 min)")
        elif rest == "learn:off":
            self.learn_until = 0
            self.log("  Handkontroll: inlärning av")
        elif rest == "pair":
            threading.Thread(target=self.pair, daemon=True).start()
        elif rest == "diag":
            threading.Thread(target=self.diag, daemon=True).start()

    def diag(self):
        """Vad Pi:n ser: inmatningsenheterna och Bluetooth-läget för parade kontroller."""
        try:
            import evdev
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    keys = d.capabilities().get(evdev.ecodes.EV_KEY, [])
                    self.log("  Diagnos: %s \"%s\" (%d knappar, gamepad=%s)" % (path, d.name, len(keys), evdev.ecodes.BTN_SOUTH in keys or evdev.ecodes.BTN_GAMEPAD in keys))
                    d.close()
                except Exception as e:
                    self.log("  Diagnos: %s går inte att öppna: %s" % (path, e))
        except Exception as e:
            self.log("  Diagnos: evdev: %s" % e)
        try:
            out = subprocess.run(["bash", "-c", "ls -l /dev/input/ | tail -n +2 | awk '{print $1, $3, $4, $NF}' | tr '\\n' ';'; echo; id; "
                                  "for m in $(bluetoothctl devices Paired 2>/dev/null | awk '{print $2}'); do bluetoothctl info $m | grep -E 'Name|Connected|Paired|Trusted|UUID: Human' | tr -s ' ' | tr '\\n' ';'; echo; done; "
                                  "lsmod | grep -E '^uhid|^hid' | awk '{print $1}' | tr '\\n' ' '"],
                                 capture_output=True, text=True, timeout=20).stdout
            for ln in out.splitlines():
                if ln.strip():
                    self.log("  Diagnos: " + ln.strip()[:380])
        except Exception as e:
            self.log("  Diagnos: %s" % e)

    # ---- inställningarna (från deck-redigeraren) ----
    def set_cfg(self, cfg):
        self.cfg = cfg if isinstance(cfg, dict) else {}

    def mapping(self):
        if self.cfg.get("learned"):
            # inlärd på Nest Hub: bara det inlärda, plus spakarna (som inte lärs in)
            m = {k: v for k, v in DEFAULT_MAP.items() if k.startswith(("ABS_X", "ABS_Y", "ABS_RZ", "ABS_RY"))}
        else:
            m = dict(DEFAULT_MAP)
        own = self.cfg.get("map")
        if isinstance(own, dict) and own:
            m.update({str(k): str(v or "") for k, v in own.items()})
        return m

    def room(self):
        r = self.cfg.get("room") or ((self.hub.cfg.get("sonos") or {}).get("wake_room"))
        return r or (self.hub.sonos.room_names() or ["Vardagsrum"])[0]

    def group(self):
        g = self.cfg.get("group")
        if g:
            return g
        lg = self.hub.lights
        if lg.custom:
            return lg.custom[0]["name"]
        groups = [x["name"] for x in lg.group_list() if not x["name"].lower().startswith(("tradfri group", "supergroup"))]
        return groups[0] if groups else ""

    # ---- läsa kontrollen ----
    def _find(self):
        import evdev
        out = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
                keys = d.capabilities().get(evdev.ecodes.EV_KEY, [])
                if evdev.ecodes.BTN_SOUTH in keys or evdev.ecodes.BTN_GAMEPAD in keys or "stadia" in d.name.lower():
                    out.append(d)
                else:
                    d.close()
            except Exception:
                pass
        return out

    def _loop(self):
        import evdev
        E = evdev.ecodes
        warned = False
        while True:
            devs = []
            try:
                devs = self._find()
            except Exception as e:
                if not warned:
                    self.log("(varning: handkontrollen: %s)" % e)
                    warned = True
            if not devs:
                self.dev_name = ""
                time.sleep(4)
                continue
            d = devs[0]
            for x in devs[1:]:
                x.close()
            self.dev_name = d.name
            self.log('  Handkontroll ansluten: "%s"' % d.name)
            self._report()
            absinfo = {}
            try:
                for code, info in d.capabilities(absinfo=True).get(E.EV_ABS, []):
                    absinfo[code] = info
            except Exception:
                pass
            pressed = set()
            has_pedals = E.ABS_GAS in absinfo or E.ABS_BRAKE in absinfo
            try:
                while True:
                    r, _, _ = select.select([d.fd], [], [], 5)
                    if not r:
                        continue
                    for ev in d.read():
                        if ev.type == E.EV_KEY and ev.value in (0, 1):
                            name = E.BTN.get(ev.code) if ev.code in E.BTN else E.KEY.get(ev.code, str(ev.code))
                            if isinstance(name, (list, tuple)):
                                # BTN_A heter också BTN_SOUTH osv.: väderstrecken är samma på alla kontroller
                                name = next((n for n in name if n in ("BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST")), name[0])
                            self._edge(str(name), ev.value == 1)
                        elif ev.type == E.EV_ABS and ev.code in absinfo:
                            nm = E.ABS.get(ev.code, str(ev.code))
                            if isinstance(nm, (list, tuple)):
                                nm = nm[0]
                            nm = str(nm).replace("ABS_HAT", "HAT")
                            info = absinfo[ev.code]
                            lo, hi = info.min, info.max
                            # avtryckare: GAS/BRAKE, och Z/RZ bara på kontroller som saknar GAS/BRAKE
                            # (på Stadia-kontrollen är Z/RZ höger spak)
                            trigger = nm in ("ABS_GAS", "ABS_BRAKE") or (nm in ("ABS_Z", "ABS_RZ") and not has_pedals and lo >= 0)
                            if nm.startswith("HAT"):
                                state = {"-": ev.value < 0, "+": ev.value > 0}
                            elif trigger:
                                frac = (ev.value - lo) / float(hi - lo or 1)
                                state = {"+": frac > (0.45 if nm + "+" in pressed else 0.65)}
                            else:
                                # spak: tryck vid 75 % utslag, släpp under 40 % (så att den inte fladdrar)
                                mid, span = (lo + hi) / 2.0, (hi - lo) / 2.0 or 1
                                off = (ev.value - mid) / span
                                state = {"-": off < (-0.4 if nm + "-" in pressed else -0.75), "+": off > (0.4 if nm + "+" in pressed else 0.75)}
                            for sign, on in state.items():
                                key = nm + sign
                                if on and key not in pressed:
                                    pressed.add(key)
                                    self._edge(key, True)
                                elif not on and key in pressed:
                                    pressed.discard(key)
                                    self._edge(key, False)
            except OSError:
                self.log("  Handkontrollen kopplades bort")
            except Exception as e:
                self.log("(varning: handkontrollen: %s)" % e)
            try:
                d.close()
            except Exception:
                pass
            self.dev_name = ""
            self.held.clear()
            self._report()
            time.sleep(2)

    def _edge(self, key, down):
        if not down:
            self.held.pop(key, None)
            return
        self.last, self.last_at = key, time.time()
        self._report()
        cmd = self.mapping().get(key, "")
        # Nest Hub visar varje tryck (och lär in knapparna i inlärningsläget)
        self.hub.run_cmd("padkey:%s|%s" % (key, "" if self.learning() else cmd), "local")
        if self.learning():
            self.log("  Handkontroll (inlärning): %s" % key)
            return
        if not cmd:
            self.log("  Handkontroll: %s (ingen funktion)" % key)
            return
        self.log("  Handkontroll: %s -> %s" % (key, cmd))
        if cmd in REPEAT:
            self.held[key] = time.time()
            self.reps = getattr(self, "reps", {}); self.reps[key] = 0
        self.run(cmd)

    def _repeat_loop(self):
        while True:
            time.sleep(0.22)
            now = time.time()
            m = self.mapping()
            for key, t0 in list(self.held.items()):
                cmd = m.get(key)
                if now - t0 > 0.45 and cmd in REPEAT:
                    n = self.reps.get(key, 0)
                    if n >= MAX_REPEAT.get(cmd, 40):
                        continue
                    self.reps[key] = n + 1
                    self.run(cmd)

    def _report(self):
        try:
            self.hub.post_json("/api/tradfri-list", {"gamepad": {"name": self.dev_name, "last": self.last, "lastAt": self.last_at}}, timeout=5)
        except Exception:
            pass

    # ---- kommandona ----
    def run(self, cmd):
        h, room, grp = self.hub, self.room(), None
        sonos = lambda action, value=None: h.run_cmd("sonos:" + json.dumps({"room": room, "action": action, "value": value}), "local")
        light = lambda action, value=None: h.run_cmd("light:" + json.dumps({"group": grp, "action": action, "value": value}), "local") if grp else self.log("  (handkontrollen: ingen ljusgrupp vald)")
        if cmd.startswith(("gp:light", "gp:dim", "gp:warm", "gp:cold")):
            grp = self.group()
        if cmd.startswith("gp:nav-"):
            h.run_cmd("nav:" + cmd[7:], "local")         # till Nest Hub-appen
        elif cmd == "gp:play":
            sonos("toggle")
        elif cmd == "gp:next":
            sonos("next")
        elif cmd == "gp:prev":
            sonos("prev")
        elif cmd == "gp:vol+":
            sonos("vol-rel", 2)
        elif cmd == "gp:vol-":
            sonos("vol-rel", -2)
        elif cmd == "gp:screen":
            self.screen_open = not self.screen_open
            h.run_cmd("open" if self.screen_open else "close", "local")
        elif cmd == "gp:tv-off":
            h.run_cmd('svtlive:{"id":"stop"}', "local")
        elif cmd == "gp:light":
            light("toggle")
        elif cmd == "gp:dim+":
            light("dim-rel", 8)
        elif cmd == "gp:dim-":
            light("dim-rel", -8)
        elif cmd == "gp:warmer":
            light("temp-rel", -10)
        elif cmd == "gp:colder":
            light("temp-rel", 10)
        elif cmd == "gp:nest":
            h.run_cmd("nest:now", "local")
        elif cmd == "gp:page":
            h.run_cmd("page:next", "local")
        elif cmd == "gp:viz":
            h.run_cmd("viz:next", "local")
        elif cmd:
            h.run_cmd(cmd, "local")

    # ---- para med Bluetooth ----
    def pair(self):
        """Letar efter kontrollen i 60 s med en egen bluetoothctl (interaktivt, så att nya enheter
        syns direkt med namn), parar, litar på och ansluter den. Allt viktigt hamnar i loggen."""
        if getattr(self, "_pairing", False):
            self.log("  Handkontroll: letar redan")
            return
        self._pairing = True
        p = None
        try:
            p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            self.log("(varning: handkontrollen: bluetoothctl saknas)")
            self._pairing = False
            return
        lines = []
        ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")

        def say(cmd):
            try:
                p.stdin.write((cmd + "\n").encode()); p.stdin.flush()
            except Exception:
                pass
        buf = [b""]

        def read_for(sec, until=None):
            # läser bitvis (bluetoothctl skriver sin prompt utan radslut, så readline kan fastna)
            end = time.time() + sec
            fd = p.stdout.fileno()
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.5)
                if not r:
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    return None
                buf[0] += chunk
                *done, buf[0] = re.split(rb"[\r\n]", buf[0])
                for raw in done:
                    ln = ansi.sub("", raw.decode("utf-8", "replace")).strip()
                    if ln:
                        lines.append(ln)
                        if until and re.search(until, ln, re.I):
                            return ln
            return None
        try:
            # Bluetooth kan vara spärrad i systemet (rfkill), då går den inte att slå på
            try:
                r1 = subprocess.run(["rfkill", "list", "bluetooth"], capture_output=True, text=True, timeout=5)
                info = " ".join(r1.stdout.split())[:200]
                if "blocked: yes" in r1.stdout.lower():
                    r2 = subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, text=True, timeout=5)
                    self.log("  Handkontroll: Bluetooth var spärrad (%s); häver spärren: %s" % (info, (r2.stderr or "ok").strip()[:150]))
                    time.sleep(2)
                    subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, text=True, timeout=10)
                else:
                    self.log("  Handkontroll: rfkill: %s" % (info or "ingen Bluetooth-enhet hittad"))
            except FileNotFoundError:
                self.log("  Handkontroll: rfkill finns inte")
            except Exception as e:
                self.log("  Handkontroll: rfkill: %s" % e)
            self.log("  Handkontroll: letar med Bluetooth i 60 s (kontrollen ska pulsera orange)")
            for c in ("power on", "scan on"):
                say(c)
            read_for(3)
            self.log("  Handkontroll: bluetoothctl: " + " | ".join(l for l in lines if not l.startswith("[NEW]"))[-300:])
            mac = name = None
            t0 = time.time()
            while time.time() - t0 < 60 and not mac:
                read_for(1)
                for ln in lines:
                    m = re.search(r"Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5}) (.*)", ln, re.I)
                    if m and re.search(r"stadia|controller|gamepad", m.group(2), re.I):
                        mac, name = m.group(1), m.group(2).strip()
                        break
                if not mac and int(time.time() - t0) % 10 == 0:
                    say("devices")
            if not mac:
                seen = sorted(set(re.findall(r"Device ([0-9A-F:]{17}) ([^\r\n]*)", "\n".join(lines), re.I)))
                self.log("  Handkontroll: hittade ingen kontroll på 60 s. Enheter i närheten: %s" %
                         (", ".join("%s" % n[1][:30] for n in seen[:12]) or "inga alls (är Bluetooth på i Pi:n?)"))
                return
            self.log('  Handkontroll: hittade "%s" (%s), parar' % (name, mac))
            say("scan off")
            say("pair " + mac)
            res = read_for(25, r"Pairing successful|Failed to pair|AlreadyExists|org\.bluez\.Error")
            self.log("  Handkontroll: parning: %s" % (res or "inget svar"))
            say("trust " + mac)
            read_for(3)
            say("connect " + mac)
            res = read_for(15, r"Connection successful|Failed to connect|org\.bluez\.Error")
            self.log("  Handkontroll: anslutning: %s" % (res or "inget svar"))
        except Exception as e:
            self.log("(varning: handkontrollen: parningen: %s)" % e)
        finally:
            try:
                say("quit")
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            self._pairing = False
