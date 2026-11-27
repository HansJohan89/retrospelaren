"""
Nest Hub med pekskärm: hubben startar Home Assistants Cast-mottagare (gratis, har touch påslagen
på Nest Hub) och kopplar den till Retrospelaren, som låtsas vara en Home Assistant (hacast.py på
servern). Mottagaren laddar då appen i helskärm (/nestapp): Hem, Musik, Ljus, Scener, Larm och
Knappar, och efter två minuters stillhet klockvyn. Fungerar inte det används DashCast i stället,
som bara visar klockvyn (/nest) utan touch.
Hubben castar om när Nest Hub har gått tillbaka till sin egen vilobild (efter att du pratat med
den, eller efter en omstart).

Rör aldrig Nest Hub när den gör något annat: castar du YouTube eller Spotify dit, eller pratar
med den, får den vara ifred tills den är ledig igen.

hub_config.json (valfritt):  "nest": {"name": "Nest Hub", "on": true, "every_s": 60, "touch": true}
Kommandon:  nest:on   nest:off   nest:now
"""
import threading
import time

DASHCAST = "84912283"
HA_CAST = "A078F6B0"                          # Home Assistant Cast
OURS = {DASHCAST, HA_CAST}
IDLE_APPS = {"E8C28D3C", "", None}            # Backdrop = Nest Hubs egen vilobild


class NestCast:
    def __init__(self, hub, log):
        self.hub, self.log = hub, log
        self.cfg = dict((hub.cfg or {}).get("nest") or {})
        self.on = self.cfg.get("on", True)
        self.casts, self.browser, self.found_at = [], None, 0
        self.kick = threading.Event()
        self.touch_fails = 0
        self.busy = False                     # en castning pågår: fler tryck väntar inte i kö
        self.ha = {}                          # en Home Assistant-styrning per Nest Hub (återanvänds)
        self.last_try = 0.0
        try:
            import pychromecast  # noqa: F401
        except ImportError:
            log("(Nest Hub: pychromecast saknas, kör uppdatera-pi.bat)")
            return
        threading.Thread(target=self._loop, daemon=True, name="nest").start()

    def url(self):
        return self.hub.server.rstrip("/") + "/nest"

    def command(self, rest):
        if rest == "off":
            self.on = False
            self.log("  -> Nest Hub: castas inte längre")
            for c in self.casts:
                try:
                    if c.status and c.status.app_id in OURS:
                        c.quit_app()
                except Exception:
                    pass
        else:
            if self.busy:
                self.log("  Nest Hub: castar redan, vänta några sekunder")
                return
            self.on = True
            self.touch_fails = 0                 # försök med touch igen
            # "Visa på Nest Hub nu" (now) och "Slå på" (on): casta direkt, även om den gör något annat
            self.force = rest in ("now", "on")
            self.kick.set()

    def _find(self):
        import pychromecast
        name = self.cfg.get("name")
        try:
            if name:
                casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[name], timeout=10)
            else:
                casts, browser = pychromecast.get_chromecasts(timeout=10)
                # bara skärmar: Nest Hub (och Nest Hub Max), inte högtalare och tv-stickor
                every = casts
                casts = [c for c in casts if "hub" in str(getattr(c, "model_name", "") or c.cast_info.model_name).lower()]
                if not casts and every:
                    self.log("  Nest Hub: Chromecast-enheter på nätet: %s" % ", ".join('"%s" (%s)' % (c.cast_info.friendly_name, c.cast_info.model_name) for c in every))
        except Exception as e:
            self.log("(varning: Nest Hub: kan inte söka efter Chromecast: %s)" % e)
            return []
        # Sökningen (zeroconf) måste få fortsätta: det är den som tar reda på Nest Hubs adress.
        # Stoppades den direkt blev adressen "unknown" och anslutningen kom aldrig igång.
        old, self.browser = self.browser, browser
        if old is not None and old is not browser:
            try:
                old.stop_discovery()
            except Exception:
                pass
        ok = []
        for c in casts:
            try:
                c.wait(timeout=20)
                ok.append(c)
                self.log('  Nest Hub: ansluten till "%s" (%s)' % (c.cast_info.friendly_name, c.cast_info.host))
            except Exception as e:
                self.log('(varning: Nest Hub: "%s" svarar inte: %s)' % (c.cast_info.friendly_name, e))
        return ok

    def _cast(self, c):
        if self.cfg.get("touch", True):
            try:
                self._cast_touch(c)
                self.touch_fails = 0
                return
            except Exception as e:
                self.touch_fails += 1
                self.log("(varning: Nest Hub: appen startade inte (försök %d): %s; försöker igen om en stund)" % (self.touch_fails, e))
                return
        from pychromecast.controllers.dashcast import DashCastController
        d = DashCastController()
        c.register_handler(d)
        d.load_url(self.url(), force=True, reload_seconds=0)
        self.log('  -> Nest Hub "%s": visar %s' % (c.cast_info.friendly_name, self.url()))

    def _beat(self):
        """När appen på Nest Hub senast hörde av sig till servern (0 om aldrig)."""
        try:
            return float(self.hub.get_json("/api/nestbeat", timeout=6).get("at") or 0)
        except Exception:
            return -1.0

    def _cast_touch(self, c):
        """Startar Home Assistant-mottagaren och ber den visa appen. Lyckat = appen hör av sig
        till servern inom 35 s (Nest Hub skickar inte alltid något svar själv)."""
        from pychromecast.controllers.homeassistant import HomeAssistantController
        key = str(c.uuid)
        hc = self.ha.get(key)
        # Alltid en ny start: efter att servern startats om (ny version) har mottagaren tappat sin
        # anslutning men tror ändå att den är uppkopplad, och visar bara "Connected".
        if hc is not None:
            try:
                st = c.status
                if st is not None and getattr(st, "app_id", None) == HA_CAST:
                    c.quit_app()
                    time.sleep(3)
            except Exception:
                pass
            try:
                c.unregister_handler(hc)
            except Exception:
                pass
            self.ha.pop(key, None)
            hc = None
        if hc is None:
            hc = HomeAssistantController(hass_url=self.hub.server.rstrip("/"), hass_uuid="blaupunkt", client_id=None,
                                         refresh_token="blaupunkt-nest", unregister=lambda: None)
            c.register_handler(hc)
            self.ha[key] = hc
        t0 = time.time()
        self.last_try = t0
        hc.show_lovelace_view("blaupunkt", None)
        while time.time() - t0 < 35:
            time.sleep(1.5)
            b = self._beat()
            if b < 0:
                break                          # servern svarar inte: kan inte avgöra
            if b >= t0 - 1:
                self.log('  -> Nest Hub "%s": appen visas (%d s)' % (c.cast_info.friendly_name, time.time() - t0))
                return
            if time.time() - t0 > 18 and not getattr(self, "_resent", False):
                self._resent = True            # fastnat på "Connected": be om vyn en gång till
                try:
                    hc.show_lovelace_view("blaupunkt", None)
                except Exception:
                    pass
        self._resent = False
        st = c.status
        if b < 0 and st is not None and getattr(st, "app_id", None) == HA_CAST:
            return
        raise RuntimeError("appen syntes inte på Nest Hub inom 35 s")

    def _loop(self):
        time.sleep(20)                         # låt hubben komma igång först
        while True:
            try:
                if self.on:
                    if not self.casts or time.time() - self.found_at > 3600:
                        self.casts, self.found_at = self._find(), time.time()
                        if not self.casts:
                            self.log("(Nest Hub: hittar ingen Nest Hub på nätet, försöker igen om en stund)")
                    for c in self.casts:
                        try:
                            st = c.status
                            app = getattr(st, "app_id", None) if st else None
                            name = getattr(st, "display_name", None) if st else None
                            if app == HA_CAST and not getattr(self, "force", False):
                                # Mottagaren visas men appen har inte hört av sig på länge (fastnat på
                                # "Connected" efter en omstart av servern t.ex.): be om appen igen
                                b = self._beat()
                                if b >= 0 and time.time() - b > 75 and time.time() - self.last_try > 90:
                                    self.log('  Nest Hub: mottagaren visas men inte appen, ber om den igen')
                                    self.busy = True
                                    try:
                                        self._cast(c)
                                    finally:
                                        self.busy = False
                                continue
                            if app == DASHCAST and self.cfg.get("touch", True) and time.time() - self.last_try > 90:
                                self.force = True          # visningsläget från förr: byt till appen med touch
                            if app in OURS and not getattr(self, "force", False):
                                continue
                            idle = app in IDLE_APPS or getattr(c, "is_idle", False) or str(name or "").lower() in ("backdrop", "ambient", "")
                            if (app, name) != getattr(self, "_last_seen", None):
                                self._last_seen = (app, name)
                                self.log('  Nest Hub "%s" visar nu: %s (%s)%s' % (c.cast_info.friendly_name, name or "-", app or "-", "" if idle else ", väntar tills den är ledig"))
                            if idle or getattr(self, "force", False):
                                self.force = False
                                self.busy = True
                                try:
                                    self._cast(c)
                                finally:
                                    self.busy = False
                        except Exception as e:
                            self.log("(varning: Nest Hub: %s)" % e)
                            self.found_at = 0          # leta upp den igen nästa varv
            except Exception as e:
                self.log("(varning: Nest Hub: %s)" % e)
            self.kick.wait(float(self.cfg.get("every_s", 60)))
            self.kick.clear()
