"""
Ljuseffekter på lampor i en scen: åska, brasa, levande ljus, andning, regnbåge, fest.

En effekt körs så länge scenen pågår (eller tills ett senare steg sätter lampan till något annat,
eller någon släcker lampan). Trådfri-gatewayen klarar några kommandon i sekunden per lampa, så
åskan är blixtar på avstånd: mjuka uppljusningar i lampans egen färg, inte stroboskop.

Lampans läge i scenen är grunden: färgen och ljusstyrkan du valt (t.ex. mörkblått för åska).
"""
import colorsys
import random
import threading
import time

NAMES = {"storm": "Åska", "fire": "Brasa", "candle": "Levande ljus", "breathe": "Andas", "rainbow": "Regnbåge", "party": "Fest", "screen": "Skärmfärg"}
SPEED = {"calm": 0.6, "normal": 1.0, "wild": 1.7}


def _hex(h, s=1.0):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, max(0, min(1, s)), 1)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _mix(a, b, t):
    """Blanda två färger (#rrggbb), t = 0 ger a, 1 ger b."""
    try:
        x = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        y = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return a
    return "#%02x%02x%02x" % tuple(round(x[i] + (y[i] - x[i]) * t) for i in range(3))


class Effects:
    def __init__(self, lights, log):
        self.lights, self.log = lights, log
        self.lock = threading.Lock()
        self.running = {}                    # lampa -> {"stop": Event, "run": scenkörning}
        self.screen = {}                     # lampor som följer datorns skärm -> inställningar
        self.screen_thread = None
        try:
            from screenfeed import ScreenFeed
            self.feed = ScreenFeed(log)
            self.feed.wanted = lambda: bool(self.screen)
        except Exception as e:
            self.feed = None
            log("(varning: skärmfärg: %s)" % e)

    def stop_lamp(self, lid):
        with self.lock:
            e = self.running.pop(str(lid), None)
        if e:
            e["stop"].set()

    def stop_run(self, run):
        with self.lock:
            ids = [k for k, e in self.running.items() if e["run"] is run]
        for k in ids:
            self.stop_lamp(k)

    def busy(self, run):
        with self.lock:
            return any(e["run"] is run for e in self.running.values())

    def start(self, run, lid, base, eff, lamp):
        """lamp: lampans egenskaper (color/temp) så att effekten passar lampan."""
        self.stop_lamp(lid)
        kind = (eff or {}).get("type")
        if kind not in NAMES:
            return
        stop = threading.Event()
        with self.lock:
            b = dict(base)
            self.running[str(lid)] = {"stop": stop, "run": run, "base": b}
        fn = getattr(self, "_" + kind)
        sp = SPEED.get((eff or {}).get("speed") or "normal", 1.0)
        lamp = dict(lamp or {}, id=str(lid), region=(eff or {}).get("region") or "all",
                    csat=float((eff or {}).get("sat", 70)) / 100, cbri=float((eff or {}).get("bri", 70)) / 100)
        threading.Thread(target=self._wrap, args=(fn, str(lid), b, sp, lamp, stop, run),
                         daemon=True, name="effekt-%s-%s" % (kind, lid)).start()

    def _wrap(self, fn, lid, base, sp, lamp, stop, run):
        self.log("    effekt %s på lampa %s" % (fn.__name__[1:], lid))
        last_check = time.time()

        def put(val, tr):
            if stop.is_set() or run.stop.is_set():
                raise StopIteration
            nonlocal last_check
            # någon har släckt lampan: effekten slutar
            if time.time() - last_check > 4:
                last_check = time.time()
                try:
                    if not (self.lights.read_lamps([lid]).get(lid) or {}).get("on", True):
                        raise StopIteration
                except StopIteration:
                    raise
                except Exception:
                    pass
            self.lights.apply_lamps({lid: dict(val, on=True)}, transition=tr)

        def wait(s):
            if stop.wait(s) or run.stop.is_set():
                raise StopIteration
        try:
            fn(put, wait, base, sp, lamp)
        except StopIteration:
            pass
        except Exception as e:
            self.log("(varning: lampeffekt: %s)" % e)
        finally:
            with self.lock:
                if self.running.get(lid, {}).get("stop") is stop:
                    self.running.pop(lid, None)

    # ---- effekterna ----
    @staticmethod
    def _colour(lamp):
        return bool(lamp.get("color"))

    def _storm(self, put, wait, base, sp, lamp):
        """Blixtar på avstånd: lampan behåller sin färg och lyser bara upp en aning, drar mot en
        kallare ton och klingar av mjukt. Ibland ett litet efterblink, ibland en dov blixt."""
        colour = self._colour(lamp)
        if not base.get("kind"):
            base.update(dict(kind="color", color="#1b2fd6") if colour else dict(kind="white", temp=100))
        base.setdefault("dim", 25)
        d0 = float(base["dim"])
        put(base, 1.5)
        wait(2)
        while True:
            wait(random.uniform(8, 30) / sp)
            d0 = float(base.get("dim") or 25)                            # ratten kan ha ändrat grunden
            strength = random.choice([0.35, 0.5, 0.6, 0.8, 1.0])          # hur nära blixten är
            peak = min(100, d0 + (14 + 30 * strength) * random.uniform(0.8, 1.2))
            # kallare ton: färgen dras en bit mot blåvitt, vit lampa mot kallt ljus
            if colour and base.get("kind") == "color":
                tint = dict(kind="color", color=_mix(base.get("color", "#1b2fd6"), "#dbe4ff", 0.18 + 0.22 * strength))
            elif base.get("kind") == "white":
                tint = dict(kind="white", temp=min(100, float(base.get("temp", 60)) + 25 * strength))
            else:
                tint = None
            rise = random.uniform(0.2, 0.5)
            put({"dim": peak}, rise)                                     # bara ljusstyrkan: ett kommando
            if tint:
                put(tint, rise)
            wait(rise + random.uniform(0.1, 0.4))
            # efterblink: blixten flackar till igen innan den släpper
            for _ in range(random.choice([0, 0, 1, 1, 2])):
                put({"dim": max(d0, peak * random.uniform(0.6, 0.8))}, 0.2)
                wait(random.uniform(0.25, 0.5))
                put({"dim": peak * random.uniform(0.85, 1.0)}, 0.15)
                wait(random.uniform(0.2, 0.45))
            fade = random.uniform(1.8, 4.5)
            put({"dim": d0}, fade)                                       # klingar av
            if tint:
                put({k: base[k] for k in ("kind", "color", "temp") if k in base}, fade)
            wait(fade)

    def _fire(self, put, wait, base, sp, lamp):
        while True:
            top = max(3, base.get("dim", 60))
            step = random.uniform(0.35, 1.0) / sp
            d = random.uniform(top * 0.45, top)
            if self._colour(lamp):
                put({"dim": d, "kind": "color", "color": _hex(random.uniform(14, 34), random.uniform(0.85, 1))}, step)
            else:
                put({"dim": d, "kind": "white", "temp": random.uniform(0, 8)}, step)
            wait(step)

    def _candle(self, put, wait, base, sp, lamp):
        while True:
            top = max(3, base.get("dim", 45))
            step = random.uniform(0.8, 2.2) / sp
            d = random.uniform(top * 0.75, top)
            if random.random() < 0.08:
                d = top * 0.55                     # ett litet fladder
                step = 0.3
            if self._colour(lamp):
                put({"dim": d, "kind": "color", "color": _hex(random.uniform(28, 36), 0.8)}, step)
            else:
                put({"dim": d, "kind": "white", "temp": 0}, step)
            wait(step)

    def _breathe(self, put, wait, base, sp, lamp):
        half = 4.0 / sp
        while True:
            hi = max(3, base.get("dim", 70))
            lo = max(1, hi * 0.15)
            put(dict(base, dim=lo), half)
            wait(half)
            put(dict(base, dim=hi), half)
            wait(half)

    def _rainbow(self, put, wait, base, sp, lamp):
        if not self._colour(lamp):
            return self._breathe(put, wait, base, sp, lamp)
        h, step = random.uniform(0, 360), 2.5 / sp
        while True:
            h += 360 * step / (60 / sp)
            put({"dim": base.get("dim", 70), "kind": "color", "color": _hex(h)}, step)
            wait(step)

    def _party(self, put, wait, base, sp, lamp):
        while True:
            step = random.uniform(0.6, 1.4) / sp
            if self._colour(lamp):
                put({"dim": random.uniform(50, 100), "kind": "color", "color": _hex(random.choice(range(0, 360, 30)))}, 0.2)
            else:
                put({"dim": random.uniform(20, 100), "kind": "white", "temp": random.uniform(0, 100)}, 0.2)
            wait(step)

    def adjust(self, lid, change):
        """Ratten ändrade en lampa som kör en effekt: effekten fortsätter med det nya som grund.
        Svarar False om lampan inte har någon effekt igång."""
        lid = str(lid)
        with self.lock:
            r = self.running.get(lid)
        if not r or not change:
            return False
        base = r.get("base")
        if base is None:
            return False
        for k in ("dim", "kind", "color", "temp"):
            if k in change:
                base[k] = change[k]
        st = self.screen.get(lid)
        if st is not None:
            st["base"] = base                  # Skärmfärg: ny högsta ljusstyrka
        return True

    # ---- skärmfärg: lamporna följer datorns skärm ----
    # Alla lampor som följer skärmen uppdateras tillsammans drygt tre gånger i sekunden. Trådfri tar
    # inte ljusstyrka och färg i samma kommando, så varje lampa får ett kommando per varv: det som
    # ändrats mest (färgen eller ljuset); det andra skickas varvet efter.
    TICK = 0.3
    RATE = 4.0               # kommandon i sekunden till gatewayen, för alla lampor tillsammans

    def _screen(self, put, wait, base, sp, lamp):
        if self.feed is None or self.feed.sock is None:
            self.log("(varning: skärmfärg: hubben kan inte ta emot färger från datorn)")
            return
        lid = lamp["id"]
        with self.lock:
            self.screen[lid] = {"region": lamp.get("region", "all"), "base": base, "lamp": lamp, "sp": sp,
                                "cur": None, "sent": None}
            if self.screen_thread is None or not self.screen_thread.is_alive():
                self.screen_thread = threading.Thread(target=self._screen_loop, daemon=True, name="skärmfärg-lampor")
                self.screen_thread.start()
        try:
            while True:
                wait(1)
        finally:
            with self.lock:
                self.screen.pop(lid, None)

    @staticmethod
    def _target(rgbl, base, lamp):
        """Skärmens färg -> lampans läge. Ljusstyrkan i scenen är taket. Färgen förstärks som på en
        Ambilight-TV: skärmens färger är nästan alltid bleka, lampan ska visa vilken färg det är."""
        r, g, b, lum = rgbl[:4]
        amount = rgbl[4] if len(rgbl) > 4 else 1.0
        c = max(0.0, min(1.0, lamp.get("csat", 0.7)))       # Färgstyrka
        k = max(0.0, min(1.0, lamp.get("cbri", 0.7)))       # Ljuskänslighet
        top = float(base.get("dim") or 100)
        # ljuset: hög känslighet = mörka scener blir riktigt mörka och ljusa går upp till taket
        # Ögat uppfattar ljus ungefär logaritmiskt och lampans ljusstyrka är linjär: en bild som är
        # hälften så ljus ska ge en lampa på ungefär en fjärdedel, annars känns allt lika ljust.
        # Hög känslighet = brantare kurva, lägre golv och full styrka redan vid en ganska ljus bild.
        floor = 0.5 * (1 - k) ** 3              # 70 och uppåt: en svart bild ger runt 1 %
        white = 0.6 - 0.25 * k
        rel = min(1.0, lum / white) ** (1.0 + 1.5 * k)
        dim = max(1.0, min(top, top * (floor + (1 - floor) * rel)))
        h, sat, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if c <= 0.0 or (sat < 0.04 and amount < 0.03):
            sat = 0.0
        else:
            g2 = 0.5 + 2.5 * c
            sat = min(1.0, sat * g2 + 0.3 * c) * min(1.0, 0.35 + amount * g2)
        out = {"dim": dim, "hs": (h * 360, sat)}
        if lamp.get("color"):
            if sat == 0.0:
                out["set"] = {"kind": "white", "temp": 30}      # grå/vit bild: varmvitt
            else:
                rr, gg, bb = colorsys.hsv_to_rgb(h, sat, 1)
                out["set"] = {"kind": "color", "color": "#%02x%02x%02x" % (round(rr * 255), round(gg * 255), round(bb * 255))}
        elif lamp.get("temp"):
            warm = (r - b) / 255.0
            out["set"] = {"kind": "white", "temp": max(0, min(100, 50 - warm * 80))}
        return out

    @staticmethod
    def _hs_dist(a, b):
        if a is None or b is None:
            return 999
        dh = abs(a[0] - b[0]) % 360
        dh = min(dh, 360 - dh) * min(a[1], b[1])          # nyansen spelar mindre roll ju blekare
        return dh + abs(a[1] - b[1]) * 100

    def _screen_loop(self):
        last_check, tokens = time.time(), 0.0
        while True:
            t0 = time.time()
            with self.lock:
                lamps = dict(self.screen)
            if not lamps:
                return
            send, idle, cand = {}, {}, []
            for lid, st in lamps.items():
                v, age = self.feed.get(st["region"])
                if v is None or age > 10:
                    if st["sent"] and st["sent"].get("idle"):
                        continue                               # står redan i scenens läge
                    st["cur"] = None
                    st["sent"] = {"dim": float(st["base"].get("dim") or 60), "rgb": None, "idle": True}
                    idle[lid] = dict({k: st["base"][k] for k in ("kind", "color", "temp") if k in st["base"]},
                                     dim=st["sent"]["dim"], on=True)
                    continue
                if age > 3:
                    continue                                   # datorn hörs inte just nu: stå kvar
                else:
                    want = self._target(v, st["base"], st["lamp"])
                # utjämning: Lugn följer långsamt, Intensiv nästan direkt. Nyansen går den kortaste
                # vägen runt färghjulet (rött -> blått via lila, inte via grått).
                a = {0.6: 0.4, 1.0: 0.75, 1.7: 1.0}.get(st["sp"], 0.75)
                cur = st["cur"]
                if cur and cur.get("hs"):
                    want["dim"] = cur["dim"] + (want["dim"] - cur["dim"]) * min(1.0, a + 0.2)   # ljuset lite snabbare än färgen
                    (h0, s0), (h1, s1) = cur["hs"], want["hs"]
                    dh = ((h1 - h0 + 180) % 360) - 180
                    if s1 == 0.0 or s0 == 0.0:
                        hh = h1 if s0 == 0.0 else h0
                    else:
                        hh = (h0 + dh * a) % 360
                    ss = s0 + (s1 - s0) * a
                    want["hs"] = (hh, ss)
                    if st["lamp"].get("color"):
                        if ss > 0.05:
                            rr, gg, bb = colorsys.hsv_to_rgb(hh / 360.0, ss, 1)
                            want["set"] = {"kind": "color", "color": "#%02x%02x%02x" % (round(rr * 255), round(gg * 255), round(bb * 255))}
                        else:
                            want["set"] = {"kind": "white", "temp": 30}
                st["cur"] = want
                prev = st["sent"]
                if prev and prev.get("idle"):
                    prev = None
                dim = round(want["dim"], 1)
                dim_err = 99 if prev is None else abs(prev["dim"] - dim) / max(3.0, prev["dim"] * 0.12)
                col_err = 0 if not want.get("set") else 99 if (prev is None or prev.get("kind") != want["set"].get("kind")) \
                    else self._hs_dist(prev.get("hs"), want["hs"]) / 12.0
                dim_moved, col_moved = dim_err >= 0.5, col_err >= 0.35
                # ett kommando per lampa och varv: det som ligger mest efter. Ljuset räknas relativt
                # (2 -> 4 % är lika stort som 40 -> 80 %), så mörka scener följs också.
                st["dbg"] = (v[3], dim)
                if col_moved and (not dim_moved or col_err > dim_err * (1.5 if st.get("last") == "col" else 0.7)):
                    cand.append((min(col_err, 50), lid, dict(want["set"]), "col",
                                 {"dim": prev["dim"] if prev else -99, "hs": want["hs"], "kind": want["set"].get("kind")}))
                elif dim_moved:
                    cand.append((min(dim_err, 50), lid, {"dim": dim}, "dim",
                                 {"dim": dim, "hs": (prev or {}).get("hs"), "kind": (prev or {}).get("kind")}))
            # Gatewayen får inte översvämmas: högst RATE kommandon i sekunden sammanlagt, de lampor
            # som ligger mest efter först (resten tar igen nästa varv). Och när något annat styr
            # lamporna (en ratt, en knapp, en scen) pausar Skärmfärg en stund så att det går fram direkt.
            rate = float((getattr(self.lights, "cfg", None) or {}).get("screen_rate", self.RATE))
            tokens = min(rate * self.TICK * 2, tokens + rate * self.TICK)
            others = getattr(self.lights, "busy_until", 0) > time.time() - 1.5
            if others:
                cand = []
            cand.sort(key=lambda c: -c[0])
            for err, lid, cmd, kind, sent in cand:
                if tokens < 1:
                    break
                tokens -= 1
                send[lid] = cmd
                lamps[lid]["last"] = kind
                lamps[lid]["sent"] = sent
            if idle:
                try:
                    self.lights.apply_lamps(idle, transition=1.5)       # tillbaka till scenens läge
                except Exception as e:
                    self.log("(varning: skärmfärg: %s)" % e)
            if send:
                try:
                    self.lights.fast_set(send, transition=self.TICK)
                except Exception as e:
                    self.log("(varning: skärmfärg: %s)" % e)
            if time.time() - getattr(self, "_dbg_at", 0) > 15:
                self._dbg_at = time.time()
                d = ", ".join("%s: bildens ljus %.2f -> %d %%" % (l, st["dbg"][0], st["dbg"][1]) for l, st in lamps.items() if st.get("dbg"))
                if d:
                    self.log("  skärmfärg: " + d)
            # någon har släckt en lampa: den slutar följa skärmen
            if time.time() - last_check > 8:
                last_check = time.time()
                try:
                    now = self.lights.read_lamps(list(lamps))
                    for lid in lamps:
                        if not (now.get(lid) or {}).get("on", True):
                            self.stop_lamp(lid)
                except Exception:
                    pass
            time.sleep(max(0.05, self.TICK - (time.time() - t0)))
