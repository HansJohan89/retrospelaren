"""
Sonos. Flyttad hit från monitor.py: direktanslutning på känd IP-adress (slipper den
sega sökningen på nätet), spellistan via SoCo:s sharelink-tillägg, mjuk infadning vid
väckning och rapporteringen av vad som spelas till skärmen.

Kommandon:
  sonos-vol:{"room","value"}                    fast volym 0-100 (det gamla formatet)
  sonos:{"room","action","value"}               toggle | play | pause | next | prev |
                                                vol-rel | mute | playlist | fav
"""
import base64
import json
import queue
import re
import threading
import time
import urllib.request

try:
    import soco
    from soco.plugins.sharelink import ShareLinkPlugin
    HAVE_SOCO = True
except ImportError:
    soco = None
    HAVE_SOCO = False


class Sonos:
    art_upload = None                       # sätts av hubben: (base64, typ) -> adress på servern

    def __init__(self, cfg, log, on_vals=None):
        self.cfg = cfg or {}
        self.log = log
        self.on_vals = on_vals or (lambda vals: None)
        self.rooms = dict(self.cfg.get("rooms") or {})      # rumsnamn -> IP (tom sträng = sök på nätet)
        self.devices = {}
        self.q = queue.Queue()
        threading.Thread(target=self._worker, name="sonos", daemon=True).start()

    def room_names(self):
        return list(self.rooms.keys())

    def overview(self):
        """För appen: varje högtalares volym, vilka som spelar ihop, och Sonos-favoriterna."""
        out = {"vols": {}, "groups": [], "favs": []}
        if soco is None:
            return out
        for ip in [v for v in self.rooms.values() if v]:
            try:
                zones = [z for z in soco.SoCo(ip).all_zones if z.is_visible]
                for z in zones:
                    try:
                        out["vols"][z.player_name] = int(z.volume)
                    except Exception:
                        pass
                seen = set()
                for z in zones:
                    co = z.group.coordinator
                    if co.uid in seen:
                        continue
                    seen.add(co.uid)
                    try:
                        state = co.get_current_transport_info().get("current_transport_state")
                    except Exception:
                        state = ""
                    out["groups"].append({"co": co.player_name, "members": sorted(m.player_name for m in z.group.members if m.is_visible),
                                          "playing": state == "PLAYING"})
                try:
                    out["favs"] = [f.title for f in zones[0].music_library.get_sonos_favorites()][:60]
                except Exception:
                    pass
                break
            except Exception:
                continue
        return out

    def zone_names(self):
        """Alla Sonos-högtalare i huset, frågat nu (för högtalarvalet i scenerna på webbsidan)."""
        names = self.room_names()
        if soco is not None:
            for ip in [v for v in self.rooms.values() if v]:
                try:
                    found = sorted({z.player_name for z in soco.SoCo(ip).all_zones if z.is_visible})
                    self._zones = found
                    return names + [n for n in found if n.lower() not in {x.lower() for x in names}]
                except Exception:
                    pass
        return names

    def all_room_names(self):
        """Rummen i hub_config.json plus alla andra Sonos-högtalare i huset (för förslagen på
        webbsidan). Frågar en känd högtalare högst var tionde minut och aldrig så att det blockerar."""
        now = time.time()
        if now - getattr(self, "_zones_at", 0) > 600:
            self._zones_at = now

            def look():
                for ip in [v for v in self.rooms.values() if v]:
                    try:
                        self._zones = sorted({z.player_name for z in soco.SoCo(ip).all_zones if z.is_visible})
                        return
                    except Exception:
                        pass
            if soco is not None:
                threading.Thread(target=look, daemon=True).start()
        names = self.room_names()
        return names + [n for n in getattr(self, "_zones", []) if n.lower() not in {x.lower() for x in names}]

    def device(self, room):
        if not HAVE_SOCO:
            self.log("  (varning: soco är inte installerat, Sonos hoppas över)")
            return None
        key = next((r for r in self.rooms if r.lower() == (room or "").strip().lower()), None) or (room or "").strip()
        if not key:
            return None
        dev = self.devices.get(key)
        if dev is not None:
            try:
                dev.player_name
                return dev
            except Exception:
                self.devices.pop(key, None)
        ip = self.rooms.get(key)
        if ip:
            try:
                dev = soco.SoCo(ip)
                if dev.player_name:
                    self.devices[key] = dev
                    return dev
            except Exception as e:
                self.log("  (varning: nådde inte Sonos på %s, söker på nätet i stället: %s)" % (ip, e))
        # ett rum som inte står i hub_config.json: fråga en högtalare vi redan känner om de andra
        for known_ip in [v for v in self.rooms.values() if v]:
            try:
                dev = next((z for z in soco.SoCo(known_ip).all_zones if z.player_name.strip().lower() == key.lower()), None)
                if dev is not None:
                    self.devices[key] = dev
                    return dev
            except Exception:
                pass
        try:
            dev = soco.discovery.by_name(key)
        except Exception as e:
            self.log("  (varning: Sonos-sökningen misslyckades: %s)" % e)
            return None
        if dev is None:
            self.log('  (varning: hittar inget Sonos-rum som heter "%s")' % key)
            return None
        self.devices[key] = dev
        return dev

    # ---- kö: kommandon körs i ordning, snabba volymsteg slås ihop ----
    def submit(self, cmd):
        self.q.put(cmd)

    def _worker(self):
        while True:
            cmd = self.q.get()
            items = [cmd]
            while True:
                try:
                    items.append(self.q.get_nowait())
                except queue.Empty:
                    break
            merged = []
            for c in items:
                prev = merged[-1] if merged else None
                same = prev and prev.get("room") == c.get("room") and prev.get("action") == c.get("action")
                if same and c.get("action") == "vol-rel":
                    prev["value"] = (prev.get("value") or 0) + (c.get("value") or 0)
                elif same and c.get("action") == "vol":
                    prev["value"] = c.get("value")
                else:
                    merged.append(dict(c))
            for c in merged:
                try:
                    self.apply(c)
                except Exception as e:
                    self.log("  (varning: Sonos-kommandot %r misslyckades: %s)" % (c, e))

    def scene(self, m):
        """Musikdelen av en egen scen. m = { do: "play" | "pause", room, src: "resume" | "playlist" | "fav",
        value, vol, fade, with: [rum som ska spela tillsammans] }. Tomma fält faller tillbaka på
        väckningsinställningarna i hub_config.json."""
        do = m.get("do")
        if do not in ("play", "pause", "vol"):
            return
        room = m.get("room") or self.cfg.get("wake_room") or (self.room_names() or [""])[0]
        dev = self.device(room)
        if dev is None:
            return
        try:
            others = [o for o in (self.device(r) for r in m.get("with") or []) if o is not None and o.uid != dev.uid]
            if do == "pause":
                done = set()
                for d in [dev] + others:
                    co = d.group.coordinator
                    if co.uid not in done:
                        done.add(co.uid)
                        co.pause()
                self.log('  -> Sonos: pausar i %s' % ", ".join('"%s"' % d.player_name for d in [dev] + others))
                return
            vol = m.get("vol")
            if do == "vol":
                if vol is not None:
                    for d in [dev] + others:
                        d.volume = max(0, min(100, int(vol)))
                return
            for o in others:
                if o.group.coordinator.uid != dev.group.coordinator.uid:
                    o.join(dev.group.coordinator)
            src = m.get("src") or "playlist"
            value = str(m.get("value") or "") or (self.cfg.get("wake_playlist") or "" if src == "playlist" else "")
            if vol is None and src == "playlist" and not m.get("value"):
                vol = self.cfg.get("wake_volume", 45)
            fade = m.get("fade")
            if fade is None:
                fade = self.cfg.get("wake_fade_s", 10) if (src == "playlist" and not m.get("value")) else 0
            fade = float(fade or 0)
            co = dev.group.coordinator
            if src in ("playlist", "fav") and not value:
                self.log("  (Sonos: scenen har ingen spellista att spela, och ingen väckningsspellista finns i hub_config.json)")
                return
            if vol is not None:
                for d in [dev] + others:
                    d.volume = 0 if fade > 0 else max(0, min(100, int(vol)))
            if src == "playlist" and value:
                self.play_link(co, value)
            elif src == "fav" and value:
                self.play_favorite(co, value)
            else:
                co.play()
            self.log('  -> Sonos: spelar i "%s"%s' % (room, (", fadar in till %d%% på %ds" % (int(vol), fade)) if vol is not None and fade > 0 else ""))
            if vol is not None and fade > 0:
                for d in [dev] + others:
                    threading.Thread(target=self._fade_in, args=(d, int(vol), fade), daemon=True).start()
        except Exception as e:
            self.log("  (varning: scenens musik misslyckades: %s)" % e)

    def _main(self):
        """Huvudrummet: det som andra högtalare grupperas med när inget annat spelar."""
        name = self.cfg.get("main_room") or self.cfg.get("spotify_room") or self.cfg.get("wake_room") or (self.room_names() or [""])[0]
        return self.device(name) if name else None

    @staticmethod
    def _tv(z):
        """Spelar gruppen tv-ljud (HDMI/optisk) eller linjeingång? Då måste den högtalaren leda
        gruppen: går den med i en annan grupp kopplas tv-ljudet bort."""
        try:
            co = z.group.coordinator
            if getattr(co, "is_playing_tv", False) or getattr(co, "is_playing_line_in", False):
                return True
            uri = co.avTransport.GetMediaInfo([("InstanceID", 0)]).get("CurrentURI", "") or ""
            return uri.startswith(("x-sonos-htastream:", "x-rincon-stream:"))
        except Exception:
            return False

    def _playing(self, dev):
        """De högtalare (gruppledare) som spelar just nu: tv-ljud först, sedan den förvalda."""
        playing = []
        for z in dev.all_zones:
            try:
                if not (z.is_coordinator and z.is_visible):
                    continue
                if self._tv(z) or z.get_current_transport_info().get("current_transport_state") == "PLAYING":
                    playing.append(z)
            except Exception:
                pass
        prefer = (self.cfg.get("main_room") or self.cfg.get("spotify_room") or self.cfg.get("wake_room") or "").lower()
        playing.sort(key=lambda z: (not self._tv(z), z.player_name.lower() != prefer))
        return playing

    def group(self, dev, room, action, value):
        """Gruppering: lägga till en högtalare i en grupp, ta bort den, eller alla på en gång."""
        if isinstance(value, list):
            return self.group_many(dev, room, action, value)
        members = [m for m in dev.group.members if m.is_visible]
        if action == "group-toggle":
            action = "ungroup" if len(members) > 1 else "group"
        if action == "group":
            if value:
                other = self.device(str(value))
                if other is None:
                    return
                target = other.group.coordinator
            else:
                # i första hand den högtalare som spelar just nu, annars huvudrummet (Vardagsrum).
                # Huvudrummet gäller även när det spelar tv-ljud över HDMI, oavsett vad det då uppger om sig självt.
                target = next((z for z in self._playing(dev) if z.uid != dev.group.coordinator.uid), None)
                if target is None:
                    main = self._main()
                    target = main.group.coordinator if main is not None and main.group.coordinator.uid != dev.group.coordinator.uid else None
                if target is None:
                    self.log('  (Sonos: ingen annan högtalare spelar just nu, så "%s" har ingen grupp att gå med i)' % room)
                    return
            if target.uid == dev.group.coordinator.uid:
                self.log('  (Sonos: "%s" spelar redan tillsammans med "%s")' % (room, target.player_name))
                return
            if self._tv(dev) and not self._tv(target):
                # dev spelar tv-ljud: den andra går med i dev i stället, så att tv-ljudet ligger kvar
                target.join(dev.group.coordinator)
                self.log('  -> Sonos: "%s" spelar nu tv-ljudet tillsammans med "%s"' % (target.player_name, dev.player_name))
                return
            dev.join(target)
            self.log('  -> Sonos: "%s" spelar nu tillsammans med "%s"' % (dev.player_name, target.player_name))
        elif action == "ungroup":
            if len(members) < 2:
                self.log('  (Sonos: "%s" är inte med i någon grupp)' % room)
                return
            if dev.is_coordinator:
                # gruppledaren (t.ex. den som spelar tv-ljudet) behåller ljudet; de andra lämnar
                for m in members:
                    if m.uid != dev.uid:
                        m.unjoin()
                self.log('  -> Sonos: de andra lämnade gruppen kring "%s"' % dev.player_name)
                return
            dev.unjoin()
            self.log('  -> Sonos: "%s" lämnade gruppen' % dev.player_name)
        elif action == "group-all":
            co, n, missed = dev.group.coordinator, 0, []
            tv = next((z for z in self._playing(dev) if self._tv(z)), None)
            if tv is not None:
                co = tv.group.coordinator       # alla går med i den som spelar tv-ljudet
            for z in dev.all_zones:                     # en i taget, så att en avstängd högtalare inte stoppar resten
                if not z.is_visible or z.uid == co.uid:
                    continue
                try:
                    if z.group.coordinator.uid != co.uid:
                        z.join(co)
                    n += 1
                except Exception:
                    missed.append(z.player_name)
            self.log('  -> Sonos: %d högtalare spelar tillsammans med "%s"%s' % (n, co.player_name, (" (svarade inte: %s)" % ", ".join(missed)) if missed else ""))
        elif action == "ungroup-all":
            keep = dev.group.coordinator
            left = [m for m in members if m.uid != keep.uid]
            for m in left:
                m.unjoin()
            self.log('  -> Sonos: gruppen kring "%s" delades upp (%d högtalare lämnade)' % (keep.player_name, len(left)))

    def group_many(self, dev, room, action, others):
        """Gruppering med ikryssade högtalare: dev och others är de som valts på webbsidan."""
        devs = [dev] + [o for o in (self.device(str(r)) for r in others) if o is not None and o.uid != dev.uid]
        names = ", ".join('"%s"' % d.player_name for d in devs)
        if len(devs) == 1:
            # bara en ikryssad: den går med i (eller lämnar) den högtalare som spelar just nu
            return self.group(dev, room, action, "")
        together = len({d.group.coordinator.uid for d in devs}) == 1
        if action == "group-toggle":
            action = "ungroup" if together else "group"
        if action == "group":
            if together:
                self.log("  (Sonos: %s spelar redan tillsammans)" % names)
                return
            playing = {z.uid for z in self._playing(dev)}
            # den av de valda som spelar tv-ljud leder alltid (annars kopplas tv:n bort), sedan den
            # som spelar något, sedan huvudrummet, annars den först valda
            main = self._main()
            lead = (next((d for d in devs if self._tv(d)), None)
                    or next((d for d in devs if d.group.coordinator.uid in playing), None)
                    or next((d for d in devs if main is not None and d.uid == main.uid), None) or devs[0]).group.coordinator
            for d in devs:
                if d.group.coordinator.uid != lead.uid:
                    try:
                        d.join(lead)
                    except Exception as e:
                        self.log('  (varning: "%s" kunde inte gå med i gruppen: %s)' % (d.player_name, e))
            self.log('  -> Sonos: %s spelar nu tillsammans (leds av "%s")' % (names, lead.player_name))
        elif action == "ungroup":
            # den som leder gruppen spelar vidare; de andra valda lämnar
            n = 0
            for d in devs:
                if not d.is_coordinator:
                    try:
                        d.unjoin()
                        n += 1
                    except Exception as e:
                        self.log('  (varning: "%s" kunde inte lämna gruppen: %s)' % (d.player_name, e))
            self.log("  -> Sonos: %d av %s lämnade gruppen" % (n, names))

    def move_to(self, dev, room):
        """Flyttar det som spelas just nu till högtalaren dev: den tar över uppspelningen, och
        högtalaren som spelade tystnar. Fungerar för allt Sonos spelar, även Spotify."""
        playing = self._playing(dev)
        if any(z.uid == dev.group.coordinator.uid for z in playing):
            self.log('  (Sonos: "%s" spelar redan)' % room)
            return False
        if not playing:
            self.log("  (Sonos: ingen högtalare spelar just nu, så det finns inget att flytta till \"%s\")" % room)
            return False
        src = playing[0]
        if self._tv(src):
            # tv-ljud kan inte flyttas från tv:ns högtalare: dev får spela med i stället
            dev.join(src.group.coordinator)
            self.log('  -> Sonos: tv-ljudet går inte att flytta, "%s" spelar det tillsammans med "%s"' % (dev.player_name, src.player_name))
            return True
        dev.join(src)
        try:
            # lämna över uppspelningen till den nya högtalaren och låt den gamla lämna gruppen
            src.avTransport.DelegateGroupCoordinationTo([("InstanceID", 0), ("NewCoordinator", dev.uid), ("RejoinGroup", False)])
        except Exception as e:
            self.log('  (Sonos: kunde inte lämna över helt, "%s" och "%s" spelar nu tillsammans: %s)' % (src.player_name, room, e))
        self.log('  -> Sonos: flyttade uppspelningen från "%s" till "%s"' % (src.player_name, dev.player_name))
        return True

    def apply(self, cmd):
        room, action, value = cmd.get("room") or "", cmd.get("action"), cmd.get("value")
        dev = self.device(room)
        if dev is None:
            return
        if action == "move":
            self.move_to(dev, room)
            return
        if action in ("group", "ungroup", "group-toggle", "group-all", "ungroup-all"):
            self.group(dev, room, action, value)
            return
        if action == "vol":
            dev.volume = max(0, min(100, int(value)))
        elif action == "vol-rel":
            dev.volume = max(0, min(100, dev.volume + int(value or 0)))
        elif action == "mute":
            dev.mute = not dev.mute
        elif action == "play":
            dev.play()
        elif action == "pause":
            dev.pause()
        elif action == "toggle":
            state = dev.get_current_transport_info().get("current_transport_state")
            dev.pause() if state == "PLAYING" else dev.play()
        elif action == "next":
            dev.next()
        elif action == "prev":
            dev.previous()
        elif action == "seek-rel":
            h, m, sec = (int(x) for x in (dev.get_current_track_info().get("position") or "0:00:00").split(":"))
            pos = max(0, h * 3600 + m * 60 + sec + int(value or 0))
            dev.seek("%d:%02d:%02d" % (pos // 3600, pos % 3600 // 60, pos % 60))
        elif action == "seek":
            pos = max(0, int(float(value or 0)))
            dev.group.coordinator.seek("%d:%02d:%02d" % (pos // 3600, pos % 3600 // 60, pos % 60))
        elif action == "radio":
            # en webbradiostation från appen: {"url", "name"} eller bara adressen
            o = value if isinstance(value, dict) else {"url": str(value or ""), "name": ""}
            url = str(o.get("url") or "")
            if not url:
                return
            uri = re.sub(r"^https?://", "x-rincon-mp3radio://", url)
            dev.group.coordinator.play_uri(uri, title=str(o.get("name") or "Radio"), force_radio=True)
        elif action == "link":
            # spotify:-länk från appen, ev. med vilket spår i listan/albumet som ska börja
            o = value if isinstance(value, dict) else {"link": str(value or "")}
            co = dev.group.coordinator
            co.clear_queue()
            ShareLinkPlugin(co).add_share_link_to_queue(str(o.get("link") or ""))
            co.play_from_queue(max(0, int(o.get("index") or 0)))
        elif action == "shuffle-set":
            co = dev.group.coordinator
            co.shuffle = bool(value)
        elif action == "repeat-set":
            co = dev.group.coordinator
            co.repeat = {"off": False, "all": True, "one": "ONE"}.get(str(value), False)
        elif action == "shuffle":
            dev.shuffle = not dev.shuffle
        elif action == "repeat":
            dev.repeat = {False: True, True: "ONE"}.get(dev.repeat, False)     # av -> listan -> låten -> av
        elif action == "playlist":
            self.play_link(dev, str(value or ""))
        elif action == "fav":
            self.play_favorite(dev, str(value or ""))
        else:
            self.log('  (varning: Sonos, okänd åtgärd "%s")' % action)
            return
        self.log('  -> Sonos: %s%s i "%s"' % (action, "" if value in (None, "") else " " + str(value), room))
        if action in ("vol", "vol-rel"):
            try:
                self.on_vals({"vol:" + room: dev.volume})
            except Exception:
                pass

    def play_link(self, dev, link):
        """Spotify-länk eller spotify:-URI. En vanlig AddURIToQueue ger UPnP-fel 714/800 för
        Spotify-innehåll; sharelink-tillägget lägger in det på rätt sätt."""
        if not link:
            self.log("  (Sonos: ingen spellista angiven)")
            return
        dev.clear_queue()
        ShareLinkPlugin(dev).add_share_link_to_queue(link)
        dev.play_from_queue(0)

    def play_favorite(self, dev, title):
        favs = dev.music_library.get_sonos_favorites()
        fav = next((f for f in favs if (f.title or "").strip().lower() == title.strip().lower()), None)
        if fav is None:
            self.log('  (Sonos: hittar ingen favorit som heter "%s". Finns: %s)' % (title, ", ".join(f.title for f in favs)))
            return
        uri = fav.get_uri()
        meta = getattr(fav, "resource_meta_data", "") or ""
        try:
            dev.play_uri(uri, meta)                     # radiostationer och liknande
        except Exception:
            dev.clear_queue()                           # album/spellistor: via kön
            dev.add_uri_to_queue(uri)
            dev.play_from_queue(0)

    # ---- väckning ----
    def wake(self):
        room = self.cfg.get("wake_room") or (self.room_names() or [""])[0]
        link = self.cfg.get("wake_playlist") or ""
        if not room or not link:
            return
        dev = self.device(room)
        if dev is None:
            return
        target = int(self.cfg.get("wake_volume", 45))
        seconds = float(self.cfg.get("wake_fade_s", 10))
        try:
            dev.volume = 0
            self.play_link(dev, link)
            self.log('  -> Sonos: spelar i "%s", fadar in till %d%% på %ds' % (room, target, seconds))
            threading.Thread(target=self._fade_in, args=(dev, target, seconds), daemon=True).start()
        except Exception as e:
            self.log("  (varning: Sonos-uppspelningen misslyckades: %s)" % e)

    @staticmethod
    def _fade_in(dev, target, seconds):
        steps = 25
        for i in range(1, steps + 1):
            try:
                dev.volume = round(target * i / steps)
            except Exception:
                return
            time.sleep(max(0.05, seconds / steps))

    def pause_room(self, room):
        dev = self.device(room)
        if dev is not None:
            try:
                dev.pause()
            except Exception:
                pass

    # ---- nu spelas, till skärmen ----
    def now_playing(self, room):
        payload = {"room": room, "playing": False, "title": "", "artist": "", "album": "", "art": "", "queue": []}
        dev = self.device(room)
        if dev is None:
            return payload
        info = dev.get_current_track_info()
        playing = dev.get_current_transport_info().get("current_transport_state") == "PLAYING"
        title = (info.get("title") or "").strip()
        if playing and title:
            upcoming = []
            try:
                pos = int(info.get("playlist_position") or 0)
                if pos > 0:
                    upcoming = [{"title": it.title or "", "artist": getattr(it, "creator", "") or ""} for it in dev.get_queue(start=pos, max_items=4)]
            except Exception:
                pass
            payload.update({"playing": True, "title": title, "artist": info.get("artist") or "", "album": info.get("album") or "",
                            "art": self._art_for(info), "queue": upcoming})
        return payload

    def _art_for(self, info):
        """Ett skivomslag som webbsidan kan visa. Sonos ger en adress på det egna nätet (http://
        högtalaren:1400/...), och den kan en sida på internet varken nå eller visa. Därför:
        Spotify-låtar får omslaget från Spotifys öppna tjänst (behöver ingen inloggning); annars
        hämtar hubben bilden från högtalaren och lägger upp den på servern."""
        uri, art = info.get("uri") or "", info.get("album_art") or ""
        key = uri or art
        cache = self.__dict__.setdefault("_art_cache", {})
        if not key:
            return ""
        if key in cache:
            return cache[key]
        url, failed = "", False
        m = re.search(r"spotify(?:%3a|:)track(?:%3a|:)([A-Za-z0-9]{22})", uri, re.I)
        if m:
            try:
                req = urllib.request.Request("https://open.spotify.com/oembed?url=https://open.spotify.com/track/" + m.group(1),
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    url = json.loads(r.read().decode("utf-8")).get("thumbnail_url") or ""
            except Exception as e:
                self.log("  (Sonos: Spotify gav inget skivomslag, tar det från högtalaren: %s)" % e)
        if not url and art.startswith("https://"):
            url = art
        if not url and art.startswith("http://") and self.art_upload:
            try:
                with urllib.request.urlopen(art, timeout=4) as r:
                    data, ctype = r.read(1500000), (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                if data and ctype.startswith("image/"):
                    url = self.art_upload(base64.b64encode(data).decode("ascii"), ctype) or ""
            except Exception as e:
                failed = True
                self.log("  (Sonos: kunde inte hämta skivomslaget från högtalaren: %s)" % e)
        if failed and not url:
            return ""                          # försök igen nästa gång
        if len(cache) > 200:
            cache.clear()
        cache[key] = url
        return url
