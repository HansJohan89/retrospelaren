"""
Körmotorn för Stream Controllern - samma regler som i webbsidans deck.html
(<script id="core">, DeckCore). Ingen hårdvara och inget nätverk här, bara
logiken: vilken knapp som ligger var, vad ett tryck gör och vilket kommando
som ska skickas. De två implementationerna jämförs i test/crosscheck.py;
ändrar du något här ska det ändras i deck.html också.

  deck    = { device, profiles:[profile], _faces }
  profile = { id, name, pages:[page], folders:{ fid:{ keys:[6] } } }
  page    = { keys:[6], btns:[3], encs:[3 x {ccw, cw, press}] }
  ctl     = { id, states:[ {a:action, look}, ... ] }
  nav     = { p:profil-id, pg:sidindex, stack:[fid, ...] }
"""
import json
import math

N_KEYS, N_BTNS, N_ENCS = 6, 3, 3
HOLD_MS = 550          # håll in Tillbaka så här länge = hoppa ända upp
ROT_WINDOW_MS = 60     # snabba rattsteg inom fönstret slås ihop till ett kommando

REL_OPS = {"light": ("dim-rel", "temp-rel", "hue-rel"), "sonos": ("vol-rel",), "sp": ("vol-rel", "seek-rel"),
           "deckdev": ("brightness-rel",), "z2m": ("brightness-step",)}


def is_rel(a):
    return bool(a) and a.get("op") in REL_OPS.get(a.get("t"), ())


def _num(v, d):
    """Som Number(v) i JavaScript, med d som reserv för sådant som inte är tal."""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else d
    if v is None:
        return 0
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0
        try:
            f = float(s)
            return f if math.isfinite(f) else d
        except ValueError:
            return d
    return d


def _round(x):
    """Math.round: halvor avrundas uppåt (Pythons round avrundar till jämnt)."""
    return int(math.floor(x + 0.5))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _str(v):
    """Som JavaScripts String(v) för enkla värden."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _js(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def to_cmd(a, mult=1):
    """Åtgärd -> kommandosträngen som skickas (till /api/send eller körs direkt av hubben)."""
    if not a:
        return ""
    k = mult or 1
    t = a.get("t")
    if t == "bp":
        return a.get("c") or ""
    if t == "viz":
        return "viz:" + str(a.get("n") or "1")
    if t == "palette":
        return "palette:" + str(a.get("p") or "svartvit")
    if t == "page":
        return "page:" + str(a.get("p") or "next")
    if t == "screen":
        o = {}
        for key in ("anim", "pic", "music", "viz", "palette", "page"):
            v = a.get(key)
            if v is not None and v != "":
                o[key] = _str(v)
        return ("screen:" + _js(o)) if o else ""
    if t == "plug":
        if not a.get("id"):
            return ""
        op = a.get("op") if a.get("op") in ("on", "off", "toggle") else "toggle"
        return "plug:" + _js({"id": str(a.get("id")), "action": op})
    if t == "light":
        op = a.get("op") or "on"
        value = None
        if op == "dim":
            value = _clamp(_round(_num(a.get("value", 100), 100)), 1, 100)
        elif op == "temp":
            value = _clamp(_round(_num(a.get("value", 50), 50)), 0, 100)
        elif op in ("dim-rel", "temp-rel", "hue-rel"):
            value = _round(_num(a.get("value", 0), 0)) * k
        elif op == "color":
            value = str(a.get("value") or "ffcc88").replace("#", "").lower()
        elif op == "scene":
            value = str(a.get("value") or "")
        return "light:" + _js({"group": a.get("group") or "", "action": op, "value": value})
    if t == "scene":
        return ("scene:" + str(a.get("id"))) if a.get("id") else ""
    if t == "scenestop":
        return "scene-stop:" + str(a.get("id") or "*") + (":end" if a.get("end") else "")
    if t == "sonos":
        op = a.get("op") or "toggle"
        room = a.get("room") or ""
        if op == "vol":
            return "sonos-vol:" + _js({"room": room, "value": _clamp(_round(_num(a.get("value", 30), 30)), 0, 100)})
        rooms = a.get("rooms")
        if op in ("group", "group-toggle", "ungroup") and isinstance(rooms, list) and rooms:
            rs = [str(r) for r in rooms]
            return "sonos:" + _js({"room": rs[0], "action": op, "value": rs[1:]})
        value = None
        if op == "vol-rel":
            value = _round(_num(a.get("value", 0), 0)) * k
        elif op in ("playlist", "fav", "group", "group-toggle"):
            value = str(a.get("value") or "")
        return "sonos:" + _js({"room": room, "action": op, "value": value})
    if t == "sp":
        op = a.get("op") or "toggle"
        value = None
        if op == "vol":
            value = _clamp(_round(_num(a.get("value", 30), 30)), 0, 100)
        elif op in ("vol-rel", "seek-rel"):
            value = _round(_num(a.get("value", 0), 0)) * k
        elif op in ("playlist", "device"):
            value = str(a.get("value") or "")
        return "sp:" + _js({"action": op, "value": value, "device": a.get("device") or ""})
    if t == "z2m":
        op = a.get("op") or "toggle"
        if op == "on":
            p = {"state": "ON"}
        elif op == "off":
            p = {"state": "OFF"}
        elif op == "brightness":
            p = {"brightness": _clamp(_round(_num(a.get("value", 50), 50) / 100 * 254), 0, 254)}
        elif op == "brightness-step":
            p = {"brightness_step": _round(_num(a.get("value", 0), 0) / 100 * 254) * k}
        elif op == "color_temp":
            p = {"color_temp": _round(454 - _clamp(_num(a.get("value", 50), 50), 0, 100) / 100 * (454 - 250))}
        else:
            p = {"state": "TOGGLE"}
        return "mqtt:" + _js({"topic": "zigbee2mqtt/" + (a.get("dev") or "") + "/set", "payload": _js(p)})
    if t == "mqtt":
        payload = a.get("payload")
        return "mqtt:" + _js({"topic": a.get("topic") or "", "payload": "" if payload is None else str(payload)})
    if t == "http":
        return "http:" + _js({"url": a.get("url") or "", "method": a.get("method") or "GET", "body": a.get("body") or ""})
    if t == "sys":
        return "sys:" + _js({"name": a.get("name") or ""})
    if t == "deckdev":
        op = a.get("op") or "brightness"
        value = None
        if op == "brightness":
            value = _clamp(_round(_num(a.get("value", 70), 70)), 0, 100)
        elif op == "brightness-rel":
            value = _round(_num(a.get("value", 0), 0)) * k
        return "deck:" + _js({"action": op, "value": value})
    return ""


# ---- vy: vad som ligger på knapparna just nu ----
def profile_of(deck, nav):
    pid = (nav or {}).get("p")
    for p in deck["profiles"]:
        if p["id"] == pid:
            return p
    return deck["profiles"][0]


def ctls_of_page(page):
    out = [c for c in (page.get("btns") or []) if c]
    for e in page.get("encs") or []:
        for s in ("ccw", "cw", "press"):
            if e and e.get(s):
                out.append(e[s])
    return out


def has_back_control(page):
    """Finns Tillbaka på en liten knapp eller ratt behåller mappen alla sex LCD-knappar.
    Annars tar en fast Tillbaka-knapp plats 0 i mappen."""
    return any((s.get("a") or {}).get("t") in ("back", "home") for c in ctls_of_page(page) for s in c["states"])


def _toint(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def resolve_view(deck, nav):
    nav = nav or {}
    prof = profile_of(deck, nav)
    pg = _clamp(_toint(nav.get("pg")), 0, len(prof["pages"]) - 1)
    page = prof["pages"][pg]
    stack = [f for f in (nav.get("stack") or []) if f in prof["folders"]]
    keys, back_key, folder = page["keys"], -1, None
    if stack:
        folder = prof["folders"][stack[-1]]
        keys = folder["keys"]
        if not has_back_control(page):
            back_key = 0
    return {"prof": prof, "pg": pg, "page": page, "stack": stack, "keys": keys, "backKey": back_key, "folder": folder}


def norm_nav(deck, nav):
    v = resolve_view(deck, nav or {})
    return {"p": v["prof"]["id"], "pg": v["pg"], "stack": list(v["stack"])}


def same_nav(a, b):
    return bool(a) and bool(b) and a.get("p") == b.get("p") and a.get("pg") == b.get("pg") and \
        list(a.get("stack") or []) == list(b.get("stack") or [])


BACK_CTL = {"id": "__back", "states": [{"a": {"t": "back"}}]}


class Engine:
    """io: send(cmd, action), nav_changed(nav), st_changed(patch), now() i ms, set_timeout(fn, ms)."""

    def __init__(self, get_deck, io):
        self.get_deck = get_deck
        self.io = io
        self.nav = None
        self.st = {}
        self.down_at = {}
        self.rot = {}

    def view(self):
        d = self.get_deck()
        self.nav = norm_nav(d, self.nav)
        return resolve_view(d, self.nav)

    def ctl_at(self, kind, i):
        v = self.view()
        if kind == "key":
            return BACK_CTL if i == v["backKey"] else v["keys"][i]
        if kind == "btn":
            return v["page"]["btns"][i]
        if kind == "encp":
            e = v["page"]["encs"][i]
            return e.get("press") if e else None
        return None

    def state_of(self, ctl):
        if ctl and len(ctl["states"]) > 1:
            return _toint(self.st.get(ctl["id"])) % len(ctl["states"])
        return 0

    def input(self, kind, i, down):
        ctl = self.ctl_at(kind, i)
        if not ctl:
            return
        key = "%s%d" % (kind, i)
        a = ctl["states"][self.state_of(ctl)].get("a")
        holdable = bool(a) and a.get("t") == "back"
        if down:
            self.down_at[key] = self.io.now()
            if not holdable:
                self.run_ctl(ctl)
        else:
            t0 = self.down_at.pop(key, None)
            if holdable:
                self.exec({"t": "home"} if (t0 is not None and self.io.now() - t0 >= HOLD_MS) else a)

    def run_ctl(self, ctl):
        idx = self.state_of(ctl)
        a = ctl["states"][idx].get("a")
        if len(ctl["states"]) > 1:
            nxt = (idx + 1) % len(ctl["states"])
            self.st[ctl["id"]] = nxt
            self.io.st_changed({ctl["id"]: nxt})
        self.exec(a)

    def rotate(self, i, direction):
        v = self.view()
        enc = v["page"]["encs"][i]
        if not enc:
            return
        ctl = enc.get("ccw") if direction < 0 else enc.get("cw")
        if not ctl:
            return
        a = ctl["states"][self.state_of(ctl)].get("a")
        if not is_rel(a):
            self.run_ctl(ctl)
            return
        slot = self.rot.get(ctl["id"])
        if slot:
            slot["n"] += 1
            return
        self.rot[ctl["id"]] = {"n": 1}

        def flush():
            n = self.rot.pop(ctl["id"])["n"]
            cmd = to_cmd(a, n)
            if cmd:
                self.io.send(cmd, a)
        self.io.set_timeout(flush, ROT_WINDOW_MS)

    def set_nav(self, nav):
        self.nav = norm_nav(self.get_deck(), nav)
        self.io.nav_changed(self.nav)

    def exec(self, a):
        if not a:
            return
        d = self.get_deck()
        v = self.view()
        nav = self.nav
        t = a.get("t")
        if t == "folder":
            if a.get("fid") in v["prof"]["folders"]:
                self.set_nav({"p": nav["p"], "pg": nav["pg"], "stack": nav["stack"] + [a["fid"]]})
            return
        if t == "back":
            if nav["stack"]:
                self.set_nav({"p": nav["p"], "pg": nav["pg"], "stack": nav["stack"][:-1]})
            return
        if t == "home":
            if nav["stack"]:
                self.set_nav({"p": nav["p"], "pg": nav["pg"], "stack": []})
            return
        if t == "pagenav":
            n = len(v["prof"]["pages"])
            pg = nav["pg"]
            if a.get("op") == "next":
                pg = (pg + 1) % n
            elif a.get("op") == "prev":
                pg = (pg - 1 + n) % n
            else:
                pg = _clamp((_toint(a.get("page")) or 1) - 1, 0, n - 1)
            if pg != nav["pg"] or nav["stack"]:
                self.set_nav({"p": nav["p"], "pg": pg, "stack": []})
            return
        if t == "profile":
            profs = d["profiles"]
            idx = next((n for n, p in enumerate(profs) if p["id"] == nav["p"]), -1)
            pid = a.get("pid")
            if pid and pid != "next":
                target = next((p for p in profs if p["id"] == pid), None)
            else:
                target = profs[(idx + 1) % len(profs)]
            if target and target["id"] != nav["p"]:
                self.set_nav({"p": target["id"], "pg": 0, "stack": []})
            return
        if t == "multi":
            self.run_multi(a.get("steps") or [], 0)
            return
        cmd = to_cmd(a)
        if cmd:
            self.io.send(cmd, a)

    def run_multi(self, steps, i):
        if i >= len(steps):
            return
        st = steps[i] or {}
        a = st.get("a")
        if a and a.get("t") != "multi":
            self.exec(a)
        if i + 1 < len(steps):
            self.io.set_timeout(lambda: self.run_multi(steps, i + 1), max(0, _num(st.get("delay", 150), 150)))
