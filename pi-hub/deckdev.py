"""
Stream Controllern (SOOMFON Stream Controller SE) - den fysiska enheten.

Konfigurationen görs på webbsidan (/deck). Härifrån hämtas den tillsammans med de
färdigritade knappbilderna, så enheten visar exakt det som syns i emulatorn. Tryck
och vridningar körs genom samma körmotor som emulatorn använder (engine.py), och
aktuell sida/mapp/läge delas med sidan så att emulatorn och enheten följs åt.

Drivrutin: pip-paketet "soomfon" (USB HID, 1500:3001). Knappindex från enheten:
0-5 LCD-knappar, 6-8 små knappar, 9-11 rattarnas tryck; rattar 0-2 med steg -1/+1.
Vilken som är vilken kan läras in från webbsidan (Enhetsinställningar), och sparas
då i deck["device"]["map"].
"""
import base64
import io
import os
import queue
import threading
import time

from engine import Engine, norm_nav, resolve_view, same_nav, BACK_CTL

try:
    from PIL import Image, ImageDraw
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


class DeckRuntime:
    def __init__(self, hub, cfg, log, device_factory=None):
        self.hub = hub
        self.cfg = cfg or {}
        self.log = log
        self.device_factory = device_factory or self._open_soomfon
        self.q = queue.Queue()
        self.deck = None
        self.deck_rev = -1
        self.faces_raw = {}
        self.faces = {}
        self.dev = None
        self.next_try = 0
        self.shown = {}
        self.anim = {}                        # rå knapp -> animation som spelas där just nu
        self.inv = {}
        self.enc_inv = {}
        self.fwd_key = list(range(6))
        self.nav_rev = -1
        self.st_rev = -1
        self.seen_down = {}
        self.swallow = set()
        self.asleep = False
        self.bri = 70
        self.last_input = time.time()
        self.last_keepalive = 0
        self.last_cmd = ""
        self.engine = Engine(lambda: self.deck, self)

    # ---- körmotorns io ----
    def send(self, cmd, a=None):
        self.last_cmd = cmd
        self.log("  tryck/vridning -> %s" % (cmd if len(cmd) < 110 else cmd[:107] + "..."))
        self.hub.run_cmd(cmd, "deck")

    def nav_changed(self, nav):
        self.hub.live_post({"nav": nav}, self._posted)
        self.redraw()

    def st_changed(self, patch):
        self.hub.live_post({"st": patch}, self._posted)
        self.redraw()

    @staticmethod
    def now():
        return time.monotonic() * 1000

    def set_timeout(self, fn, ms):
        t = threading.Timer(ms / 1000.0, lambda: self.q.put(("call", fn)))
        t.daemon = True
        t.start()

    def _posted(self, body, resp):
        if "nav" in body:
            self.nav_rev = max(self.nav_rev, resp.get("navRev", -1))
        if "st" in body:
            self.st_rev = max(self.st_rev, resp.get("stRev", -1))

    @property
    def connected(self):
        return self.dev is not None

    # ---- enheten ----
    def _open_soomfon(self):
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            # Windows: hidapi.dll läggs bredvid den här filen (se "Testa STREAM DECK - installera.bat")
            os.add_dll_directory(os.path.dirname(os.path.abspath(__file__)))
        import soomfon
        rot = self.cfg.get("image_rotation")
        if rot is not None:
            soomfon._device._IMG_ROT = int(rot)      # 0/90/180/270 om bilderna hamnar på sned
        return soomfon.Soomfon()

    def _connect(self):
        try:
            dev = self.device_factory()
        except Exception as e:
            if not getattr(self, "_warned", False):
                self.log("Stream Controllern hittas inte än (%s). Försöker igen var femte sekund." % e)
                self._warned = True
            return
        self._warned = False
        dev.on_key(lambda key, pressed: self.q.put(("key", key, pressed)))
        dev.on_encoder(lambda enc, delta: self.q.put(("enc", enc, delta)))
        dev.start()
        self.dev = dev
        self.shown = {}
        self.asleep = False
        self.last_input = time.time()
        self.connected_at = time.time()
        self.last_keepalive = time.time()
        self.log("Stream Controllern är ansluten.")
        self._apply_brightness()
        self.redraw()
        self.hub.heartbeat_now()

    def _drop(self, why):
        self.log("Tappade kontakten med Stream Controllern efter %d s (%s)." % (time.time() - getattr(self, "connected_at", time.time()), why))
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.next_try = time.time() + 3
        self.hub.heartbeat_now()

    def _dev_call(self, fn):
        if self.dev is None:
            return
        try:
            fn(self.dev)
        except Exception as e:
            self._drop(e)

    def _apply_brightness(self):
        self._dev_call(lambda d: d.set_brightness(0 if self.asleep else int(self.bri)))

    # ---- konfigurationen ----
    def set_deck(self, deck, rev, faces):
        self.q.put(("deck", deck, rev, faces))

    def _set_deck(self, deck, rev, faces):
        first = self.deck is None
        self.deck = deck
        self.deck_rev = rev
        if faces is not None:
            self.faces_raw = faces
            self.faces = {h: v for h, v in self.faces.items() if h in faces}
        device = deck.get("device") or {}
        self._build_map(device.get("map"))
        bri = device.get("brightness", 70)
        if first or bri != getattr(self, "_cfg_bri", None):
            self._cfg_bri = bri
            self.bri = bri
            self._apply_brightness()
        self.engine.nav = norm_nav(deck, self.engine.nav)
        self.shown = {}
        self.redraw()
        self.log("Konfiguration version %s inläst (%d knappbilder)." % (rev, len(self.faces_raw)))

    def _build_map(self, m):
        """deck.device.map = {key:[rå..6], btn:[rå..3], encp:[rå..3], enc:[rå..3], encDir:[±1..3]}"""
        m = m or {}
        inv, enc_inv = {}, {}
        # Utan inlärd ordning: enheten numrerar lilla vänstra ratten som 0 och den stora som 1
        # (sett på Mikaels enhet 2026-11-12), så de två byter plats här. Trycken antas följa samma
        # numrering. "Lär in knapparnas ordning" på sidan gäller alltid före det här.
        defaults = {"key": [0, 1, 2, 3, 4, 5], "btn": [6, 7, 8], "encp": [10, 9, 11]}
        enc_default = [1, 0, 2]
        for kind, dflt in defaults.items():
            arr = m.get(kind) or []
            for i, raw_default in enumerate(dflt):
                raw = arr[i] if i < len(arr) and isinstance(arr[i], int) else raw_default
                inv[raw] = (kind, i)
        encs, dirs = m.get("enc") or [], m.get("encDir") or []
        for i in range(3):
            raw = encs[i] if i < len(encs) and isinstance(encs[i], int) else enc_default[i]
            enc_inv[raw] = (i, -1 if (i < len(dirs) and dirs[i] == -1) else 1)
        self.inv, self.enc_inv = inv, enc_inv
        self.fwd_key = [next((raw for raw, (k, i) in inv.items() if k == "key" and i == n), n) for n in range(6)]

    # ---- knappbilderna ----
    def _face(self, h, title=""):
        if h in self.faces:
            return self.faces[h]
        img = None
        data = self.faces_raw.get(h)

        def decode(url):
            return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
        if isinstance(data, dict) and HAVE_PIL:
            # animerad knapp: en bild per ruta och hur länge var och en visas
            try:
                frames = [decode(u).resize((60, 60), Image.LANCZOS) for u in data.get("frames") or []]
                lo = float(self.cfg.get("anim_min_ms", 80)) / 1000.0     # enheten hinner inte med fortare
                if len(frames) > 1:
                    img = {"frames": frames, "d": [max(lo, float(x) / 1000.0) for x in data.get("d") or []]}
                elif frames:
                    img = frames[0]
            except Exception:
                img = None
        elif data and HAVE_PIL:
            try:
                img = decode(data)
            except Exception:
                img = None
        if img is None and HAVE_PIL:          # ingen färdig bild: enkel reserv med titeln
            img = Image.new("RGB", (120, 120), (30, 32, 38))
            if title:
                ImageDraw.Draw(img).text((8, 50), title[:14], fill=(255, 255, 255))
        self.faces[h] = img
        return img

    def redraw(self):
        if self.dev is None or self.deck is None:
            return
        v = self.engine.view()
        fmap = self.deck.get("_faces") or {}
        for i in range(6):
            ctl = BACK_CTL if i == v["backKey"] else v["keys"][i]
            raw = self.fwd_key[i]
            if not ctl:
                want = "blank"
            elif ctl is BACK_CTL:
                want = fmap.get("__back") or "back?"
            else:
                want = fmap.get("%s:%d" % (ctl["id"], self.engine.state_of(ctl))) or ("?" + ctl["id"])
            if self.shown.get(raw) == want:
                continue
            self.shown[raw] = want
            self.anim.pop(raw, None)
            if want == "blank":
                self._dev_call(lambda d, raw=raw: d.clear_key(raw))
            else:
                title = ""
                if ctl is not BACK_CTL:
                    title = (((ctl["states"][0].get("look") or {}).get("t") or {}).get("text")) or ""
                img = self._face(want, title or ("Tillbaka" if ctl is BACK_CTL else ""))
                if isinstance(img, dict):
                    self.anim[raw] = {"a": img, "k": 0, "next": time.time() + img["d"][0]}
                    img = img["frames"][0]
                if img is not None:
                    self._dev_call(lambda d, raw=raw, img=img: d.set_key_image(raw, img))
            if self.dev is None:
                return

    def _animate(self):
        """Byter bild på de knappar som visar en animation när det är dags."""
        if self.dev is None or self.asleep or not self.anim:
            return
        now = time.time()
        for raw, e in list(self.anim.items()):
            if now < e["next"]:
                continue
            a = e["a"]
            n = len(a["frames"])
            while e["next"] <= now:                       # ligger vi efter: hoppa över rutor, håll takten
                e["k"] = (e["k"] + 1) % n
                e["next"] += a["d"][e["k"]]
            self._dev_call(lambda d, raw=raw, img=a["frames"][e["k"]]: d.set_key_image(raw, img))
            if self.dev is None:
                return

    def _anim_wait(self):
        if self.dev is None or self.asleep or not self.anim:
            return 0.25
        return max(0.005, min(0.25, min(e["next"] for e in self.anim.values()) - time.time()))

    # ---- körläget från servern (emulatorn tryckte, eller en annan flik) ----
    def live_update(self, j):
        self.q.put(("live", j))

    def _live(self, j):
        if self.deck is None:
            return
        changed = False
        if j.get("navRev", -1) > self.nav_rev:
            self.nav_rev = j["navRev"]
            if j.get("nav"):
                nav = norm_nav(self.deck, j["nav"])
                if not same_nav(nav, self.engine.nav):
                    self.engine.nav = nav
                    changed = True
        if j.get("stRev", -1) > self.st_rev:
            self.st_rev = j["stRev"]
            st = dict(j.get("st") or {})
            if st != self.engine.st:
                self.engine.st = st
                changed = True
        if changed:
            self.redraw()

    # ---- deck:-kommandon (knapparnas ljus) ----
    def command(self, c):
        self.q.put(("call", lambda: self._command(c)))

    def _command(self, c):
        action, value = c.get("action"), c.get("value")
        if action == "brightness":
            self.bri = max(0, min(100, int(value if value is not None else 70)))
        elif action == "brightness-rel":
            self.bri = max(0, min(100, int(self.bri + (value or 0))))
        elif action == "sleep":
            self.asleep = True
        elif action == "wake":
            self.asleep = False
        elif action == "sleep-toggle":
            self.asleep = not self.asleep
        else:
            return
        self._apply_brightness()
        self.hub.live_post({"vals": {"deck:bri": self.bri, "deck:sleep": self.asleep}})

    def _wake(self):
        self.asleep = False
        self._apply_brightness()
        self.hub.live_post({"vals": {"deck:sleep": False}})

    # ---- tryck och vridningar från enheten ----
    def _hw_key(self, raw, down):
        kind, i = self.inv.get(raw, (None, None))
        ev = {"src": "hw", "rk": "key", "ri": raw, "d": 1 if down else 0}
        self.last_input = time.time()
        if kind is None:
            self.hub.live_post({"ev": [ev]})
            return
        ev.update({"k": kind, "i": i})
        if self.asleep or raw in self.swallow:
            # första trycket väcker bara: knapparna är svarta, så man ser inte vad man trycker på
            if self.asleep:
                self._wake()
                if down:
                    self.swallow.add(raw)
            if not down:
                self.swallow.discard(raw)
            self.hub.live_post({"ev": [ev]})
            return
        self.last_cmd = ""
        if self.deck is not None:
            if down:
                self.seen_down[raw] = True
                self.engine.input(kind, i, True)
            else:
                if not self.seen_down.pop(raw, False):
                    self.engine.input(kind, i, True)      # enheter som bara rapporterar när knappen släpps
                self.engine.input(kind, i, False)
        if self.last_cmd:
            ev["cmd"] = self.last_cmd
        self.hub.live_post({"ev": [ev]})

    def _hw_enc(self, raw, delta):
        i, sign = self.enc_inv.get(raw, (None, 1))
        self.last_input = time.time()
        ev = {"src": "hw", "rk": "enc", "ri": raw, "d": delta}
        if i is None:
            self.hub.live_post({"ev": [ev]})
            return
        d = (1 if delta > 0 else -1) * sign
        ev.update({"k": "enc", "i": i, "d": d})
        if self.asleep:
            self._wake()
        elif self.deck is not None:
            self.engine.rotate(i, d)
        self.hub.live_post({"ev": [ev]})

    # ---- huvudslingan: allt som rör enheten och körmotorn sker i den här tråden ----
    def run(self):
        while True:
            try:
                item = self.q.get(timeout=self._anim_wait())
            except queue.Empty:
                item = None
            try:
                if item:
                    kind = item[0]
                    if kind == "key":
                        self._hw_key(item[1], item[2])
                    elif kind == "enc":
                        self._hw_enc(item[1], item[2])
                    elif kind == "call":
                        item[1]()
                    elif kind == "live":
                        self._live(item[1])
                    elif kind == "deck":
                        self._set_deck(item[1], item[2], item[3])
                self._animate()
                self._tick()
            except Exception as e:
                self.log("(varning: fel i enhetsslingan: %r)" % e)

    def _tick(self):
        now = time.time()
        if self.dev is None:
            if now >= self.next_try:
                self.next_try = now + 5
                self._connect()
            return
        threads = getattr(self.dev, "_threads", None)
        if threads and not threads[0].is_alive():
            self._drop("läsningen avbröts, är sladden urdragen?")
            return
        sleep_min = ((self.deck or {}).get("device") or {}).get("sleepMin") or 0
        if sleep_min and not self.asleep and now - self.last_input > sleep_min * 60:
            self.asleep = True
            self._apply_brightness()
            self.hub.live_post({"vals": {"deck:sleep": True}})
        # Livstecken: utan det startar enheten om sig själv efter en stund och försvinner från USB
        # i några sekunder (sett på Mikaels enhet 2026-11-12). Samma paket som andra drivrutiner
        # för enhetsfamiljen skickar. Sätt keepalive_s till 0 för att slå av.
        ka = self.cfg.get("keepalive_s", 8)
        if ka and now - self.last_keepalive > ka:
            self.last_keepalive = now
            self._dev_call(lambda d: d._write((b"\x00CRT\x00\x00CONNECT").ljust(1025, b"\x00")))
