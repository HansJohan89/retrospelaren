#!/usr/bin/env python3
"""
Retrospelaren - lokal server.

Kör:  python3 server.py          (lyssnar på http://127.0.0.1:8888)
      python3 server.py --port 9000 --host 0.0.0.0

Vägar:
  /                 index.html
  /callback         index.html (Spotify skickar tillbaka hit efter inloggning)
  /api/stream?url=  proxar en radioström med CORS-huvud, så att webbsidan
                    kan analysera ljudet (visualiseringen blir äkta)
  /api/icy?url=     läser låttiteln (ICY StreamTitle) ur en radioström
  /api/radio?...    söker stationer via radio-browser.info
  /api/img?url=     proxar en bild (omslag/stationslogga) med CORS
  /api/cover?term=  omslag, album och år för en radiolåt (iTunes sök-API)
  /api/rss?url=     rubriker, ingress och bild ur ett RSS/Atom-flöde (nyhetssidan)
  /api/ogimage?url= delningsbilden (og:image) för en artikel som saknar bild i flödet
  /api/stops?q=     sök hållplats (ResRobot v2.1, nyckel i RESROBOT_API_KEY eller resrobot.key)
  /api/departures?id=  kommande avgångar från en hållplats (3 h, vid gles trafik upp till 12 h)
  /api/trip?from=&to=  resor mellan två hållplatser: avgång, ankomst, linjer, byten (ResRobot)
  /api/steps        dagens steg från SoumayApp (samma som e-ink-tavlan)
  /api/album?url=   bildadresser ur ett publikt delat Google Foto-album (samma metod som e-ink-tavlan)
  /api/football?src=espn|highlightly&mins=60
                    AIK i Allsvenskan: tabell, föregående och nästa match (ESPN utan nyckel, eller Highlightly)
  /api/state        GET/POST: sparade inställningar, favoriter och senaste station
                    (fil i DATA_DIR, t.ex. en Railway-volym, så de överlever omstarter)
  /api/send?c=      skicka kommando till sidan: open, close, next, prev,
                    playpause, wake, viz:1-5, palette:svartvit|sepia|fosfor|farg
                    (wake = närvarostyrd uppvakning, tänkt för kameratriggern)
                    tradfri-refresh = ignoreras av sidan, pollas istället av
                    monitor.py (se den filen) som en begäran om att hämta en
                    färsk grupp-/scenlista från Trådfri-gatewayen direkt
  /api/sonos-now    GET: vad Sonos (Vardagsrum) spelar just nu, för sidans
                    nu-spelar-panel. POST: monitor.py rapporterar in det
                    (Sonos syns aldrig i Spotifys eget API, se monitor.py)
  /api/tradfri-list GET: lista över Trådfri-grupper/scener, för inställnings-
                    panelens förslag. POST: monitor.py rapporterar in den
                    (gatewayen är bara nåbar lokalt, se monitor.py)
  /deck             deck.html - konfiguratorn och emulatorn för Stream Controllern
  /api/claim?id=&who=  den första sidan som frågar får köra kommandot (Spotify-kommandon ska
                    bara utföras en gång även om flera flikar är öppna)
  /api/deck         GET/POST: Stream Controllerns konfiguration (profiler, sidor,
                    knappar, rattar) med versionsnummer. POST kan också bära
                    "faces": färdigritade knappbilder som den fysiska enheten visar
  /api/deck/faces   GET: knappbilderna (?h=a,b för ett urval)
  /api/deck/live    GET/POST: delat körläge mellan emulatorn och den fysiska enheten
                    (aktuell sida/mapp, lägen, tryckhändelser, hubbens status)

Bara Pythons standardbibliotek. Inget att installera.
"""
import argparse
import json
import re
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Retrospelaren/1.0"
# en del tidningar (DN) svarar bara vanliga webbläsare
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
RADIO_MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
SSL_CTX = ssl.create_default_context()
CMD_LOCK = __import__("threading").Lock()
CMD_COND = __import__("threading").Condition(CMD_LOCK)   # väcker den som väntar på nästa kommando
# Kommandonumren börjar på klockslaget i tiondels sekunder, så att de fortsätter uppåt även efter
# en omstart av servern. Annars väntar öppna sidor och hubben på nummer som redan passerat, och
# missar allt tills de laddas om.
CMD_STATE = {"id": int(time.time() * 10), "list": []}
# Vad Sonos (Vardagsrum) spelar just nu - rapporteras in av monitor.py (som
# styr Sonos lokalt, se den filens kommentarer för varför: Sonos syns aldrig
# i Spotifys eget webb-API), och läses av sidan som ett eget, separat spår
# vid sidan av den vanliga Spotify-statusen.
SONOS_LOCK = __import__("threading").Lock()
SONOS_NOW = {"room": "", "title": "", "artist": "", "album": "", "art": "", "queue": [], "playing": False, "at": 0}
SONOS_MAX_AGE_S = 90  # om inget rapporterats in på så här länge räknas det som stoppat

# Lista över Trådfri-grupper och deras scener (moods) - rapporteras in av
# monitor.py (som har kontakt med gatewayen på det lokala nätverket) så
# inställningspanelen kan visa förslag. Själva valet (vilken grupp/scen som
# ska triggas vid väckning) sparas som vanligt via /api/state.
TRADFRI_LOCK = __import__("threading").Lock()
TRADFRI_LIST = {"groups": [], "at": 0}
OG_CACHE = {}
DEP_CACHE = {}
ALBUM_CACHE = {}
FOOTBALL_CACHE = {}
HOCKEY_CACHE = {"data": None, "at": 0, "fail": 0, "err": ""}
STEPS_CACHE = {"data": None, "at": 0}
# Var inställningarna sparas. På Railway: volymens monteringspunkt (RAILWAY_VOLUME_MOUNT_PATH).
DATA_DIR = (os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(ROOT, "data")).strip()
STATE_FILE = os.path.join(DATA_DIR, "state.json")
STATE_KEYS = ("settings", "favs", "lastStation")
STATE_LOCK = __import__("threading").Lock()
# ---- Stream Controller (deck) ----
# Konfigurationen (deck.json) och de färdigritade knappbilderna (deck-faces.json) sparas
# var för sig i DATA_DIR, skilt från state.json: de är stora och ska inte följa med varje
# gång sidan sparar en vanlig inställning. Körläget (vilken sida/mapp som visas, vilka
# tvålägesknappar som står i läge 2) delas mellan emulatorn i webbläsaren och hubben på
# Pi:n via DECK_LIVE; det sparas i deck-live.json så att det överlever en omstart.
DECK_FILE = os.path.join(DATA_DIR, "deck.json")
DECK_FACES_FILE = os.path.join(DATA_DIR, "deck-faces.json")
DECK_LIVE_FILE = os.path.join(DATA_DIR, "deck-live.json")
DECK_LOCK = __import__("threading").RLock()
DECK_COND = __import__("threading").Condition(DECK_LOCK)
DECK_CACHE = {"loaded": False, "rev": 0, "deck": None, "faces": {}}
DECK_LIVE = {"seq": int(time.time() * 10), "loaded": False, "nav": None, "navRev": 0, "st": {}, "stRev": 0, "vals": {}, "valsRev": 0,
             "ev": [], "evId": int(time.time() * 10), "hub": {"at": 0}}   # evId: se CMD_STATE
DECK_HUB_MAX_AGE_S = 8     # hubben räknas som frånkopplad om den inte hörts av på så här länge
DECK_EV_KEEP = 80
DECK_MAX_BYTES = 16 * 1024 * 1024


def _read_json(fp, default):
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


def _write_json(fp, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, fp)


CLAIMS = {}


def claim_cmd(cmd_id, who):
    """Den första öppna sidan som gör anspråk på ett kommando får köra det. Används för
    kommandon som bara får utföras en gång även om flera flikar har sidan öppen (Spotify)."""
    with CMD_LOCK:
        owner = CLAIMS.setdefault(cmd_id, who)
        if len(CLAIMS) > 300:
            for k in sorted(CLAIMS)[:150]:
                del CLAIMS[k]
        return {"ok": owner == who}


def _deck_load():
    with DECK_LOCK:
        if not DECK_CACHE["loaded"]:
            d = _read_json(DECK_FILE, {})
            DECK_CACHE["rev"] = int(d.get("rev") or 0)
            DECK_CACHE["deck"] = d.get("deck") if isinstance(d.get("deck"), dict) else None
            f = _read_json(DECK_FACES_FILE, {})
            DECK_CACHE["faces"] = f.get("faces") if isinstance(f.get("faces"), dict) else {}
            DECK_CACHE["loaded"] = True
        if not DECK_LIVE["loaded"]:
            l = _read_json(DECK_LIVE_FILE, {})
            DECK_LIVE["nav"] = l.get("nav") if isinstance(l.get("nav"), dict) else None
            DECK_LIVE["st"] = l.get("st") if isinstance(l.get("st"), dict) else {}
            DECK_LIVE["vals"] = l.get("vals") if isinstance(l.get("vals"), dict) else {}
            DECK_LIVE["loaded"] = True


def get_deck():
    _deck_load()
    with DECK_LOCK:
        return {"rev": DECK_CACHE["rev"], "deck": DECK_CACHE["deck"]}


def _face_ok(v):
    """En knappbild är en data-URL, eller för animerade knappar {"frames": [data-URL ...], "d": [ms ...]}."""
    def img(x):
        return isinstance(x, str) and x.startswith("data:image/") and len(x) < 400000
    if isinstance(v, dict):
        fr, d = v.get("frames"), v.get("d")
        return (isinstance(fr, list) and isinstance(d, list) and 0 < len(fr) <= 120 and len(d) == len(fr)
                and all(img(x) for x in fr) and all(isinstance(x, (int, float)) and 0 < x < 60000 for x in d))
    return img(v)


def save_deck(data):
    """Sparar hela konfigurationen. "faces" (valfritt) ersätter knappbilderna - de är
    nycklade på innehållets kontrollsumma, så samma bild lagras bara en gång."""
    deck = data.get("deck")
    if not isinstance(deck, dict):
        return {"ok": False, "error": "deck saknas"}
    _deck_load()
    with DECK_LOCK:
        DECK_CACHE["rev"] += 1
        DECK_CACHE["deck"] = deck
        DECK_LIVE["seq"] += 1
        DECK_COND.notify_all()
        try:
            _write_json(DECK_FILE, {"rev": DECK_CACHE["rev"], "deck": deck})
            faces = data.get("faces")
            if isinstance(faces, dict):
                faces = {str(k): v for k, v in faces.items() if _face_ok(v)}
                DECK_CACHE["faces"] = faces
                _write_json(DECK_FACES_FILE, {"rev": DECK_CACHE["rev"], "faces": faces})
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "rev": DECK_CACHE["rev"]}


# Skivomslag som hubben hämtat från en Sonos-högtalare (som bara går att nå hemma) och lagt upp
# här, så att sidan kan visa dem. Bara i minnet, de senaste 30.
SONOS_ART = {}
PLEX_LIST = {}                    # filmtips från Plex, skickade av hubben (bara i minnet)

# ---- Plex direkt från servern ----
# Med fjärråtkomst påslagen i Plex når servern filmerna själv. Token läggs som miljövariabeln
# PLEX_TOKEN på Railway (aldrig i sidans inställningar, som alla med adressen kan läsa).
# PLEX_URL kan anges om adressen ska vara fast; annars frågas plex.tv efter serverns adress.
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "").strip()
PLEX_STATE = {"base": os.environ.get("PLEX_URL", "").strip().rstrip("/"), "base_at": 0, "list": None, "list_at": 0, "err": ""}
PLEX_LOCK = __import__("threading").Lock()
PLEX_HDRS = {"Accept": "application/json", "X-Plex-Product": "Blaupunkt", "X-Plex-Client-Identifier": "blaupunkt-retrospelaren"}


def _plex_open(url, timeout=12, accept=None):
    sep = "&" if "?" in url else "?"
    h = dict(PLEX_HDRS)
    if accept:
        h["Accept"] = accept
    req = urllib.request.Request(url + sep + "X-Plex-Token=" + urllib.parse.quote(PLEX_TOKEN), headers=h)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def plex_base():
    """Adressen till Plex-servern, utifrån sett. Hämtas från plex.tv och provas; sparas en timme."""
    if os.environ.get("PLEX_URL"):
        return PLEX_STATE["base"]
    if PLEX_STATE["base"] and time.time() - PLEX_STATE["base_at"] < 3600:
        return PLEX_STATE["base"]
    with _plex_open("https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1") as r:
        res = json.loads(r.read().decode("utf-8"))
    conns = []
    for dev in res:
        if "server" not in (dev.get("provides") or ""):
            continue
        for c in dev.get("connections") or []:
            # utifrån: direkta adresser först, Plex egen relä sist
            conns.append((1 if c.get("relay") else 0, 1 if c.get("local") else 0, c.get("uri")))
    for _, local, uri in sorted(conns):
        if local or not uri:
            continue
        try:
            with _plex_open(uri.rstrip("/") + "/identity", timeout=5):
                PLEX_STATE.update(base=uri.rstrip("/"), base_at=time.time())
                return PLEX_STATE["base"]
        except Exception:
            continue
    raise RuntimeError("hittar ingen Plex-server som går att nå utifrån (är fjärråtkomst påslagen?)")


def plex_list():
    """Alla osedda filmer (högst 300), senast tillagda först. Sparas en halvtimme."""
    import random
    with PLEX_LOCK:
        if PLEX_STATE["list"] is not None and time.time() - PLEX_STATE["list_at"] < 1800:
            return PLEX_STATE["list"]
        base = plex_base()
        movies = []
        with _plex_open(base + "/library/sections") as r:
            secs = (json.loads(r.read().decode("utf-8")).get("MediaContainer") or {}).get("Directory") or []
        for sec in secs:
            if sec.get("type") != "movie":
                continue
            with _plex_open(base + "/library/sections/%s/all?type=1&unwatched=1&sort=addedAt:desc&X-Plex-Container-Start=0&X-Plex-Container-Size=300" % sec.get("key"), timeout=20) as r:
                for m in (json.loads(r.read().decode("utf-8")).get("MediaContainer") or {}).get("Metadata") or []:
                    if not m.get("viewCount") and not m.get("viewOffset"):
                        movies.append(m)
        movies.sort(key=lambda m: m.get("addedAt") or 0, reverse=True)
        # Hela listan skickas (nyast först); sidan själv håller reda på vad som redan visats.
        pick = movies[:300]
        for m in pick:
            v = _imdb_of(m)
            if v is None:
                v = PLEX_IMDB.get(str(m.get("ratingKey") or ""))
            m["_imdb"] = round(v, 1) if v is not None else None
        img = lambda p, w, h: ("/api/plex-img?w=%d&h=%d&p=%s" % (w, h, urllib.parse.quote(p, safe=""))) if p else ""
        items = [{"title": m.get("title") or "", "year": m.get("year"), "summary": m.get("summary") or "",
                  "minutes": round((m.get("duration") or 0) / 60000), "rating": m.get("audienceRating") or m.get("rating"),
                  "imdb": m.get("_imdb"), "key": str(m.get("ratingKey") or ""),
                  "genres": [g.get("tag") for g in (m.get("Genre") or [])][:3],
                  "director": ", ".join(d.get("tag") for d in (m.get("Director") or [])[:1]),
                  "added": m.get("addedAt"), "poster": img(m.get("thumb"), 400, 600), "art": img(m.get("art"), 960, 540)} for m in pick]
        PLEX_STATE.update(list={"items": items, "total": len(movies), "at": time.time(), "src": "server"}, list_at=time.time(), err="")
        # IMDb-betyg som inte fanns i listan hämtas film för film i bakgrunden och fylls i efterhand
        missing = [(m, it) for m, it in zip(pick, items) if it["imdb"] is None and str(m.get("ratingKey") or "") not in PLEX_IMDB]
        if missing:
            def bg():
                _imdb_fill(base, [m for m, _ in missing])
                for m, it in missing:
                    it["imdb"] = m.get("_imdb")
            __import__("threading").Thread(target=bg, daemon=True).start()
        return PLEX_STATE["list"]
PLEX_IMDB = {}     # ratingKey -> IMDb-betyg (eller None), sparas så varje film bara frågas en gång


def _imdb_of(m):
    """IMDb-betyget ur Plex-metadata: Rating-listan, eller audienceRating/rating om bilden säger imdb."""
    for r in m.get("Rating") or []:
        if str(r.get("image", "")).startswith("imdb://") and r.get("value") is not None:
            return float(r["value"])
    for v, im in (("audienceRating", "audienceRatingImage"), ("rating", "ratingImage")):
        if m.get(v) is not None and str(m.get(im, "")).startswith("imdb://"):
            return float(m[v])
    return None


def _imdb_fill(base, movies):
    from concurrent.futures import ThreadPoolExecutor

    def one(m):
        k = str(m.get("ratingKey") or "")
        v = _imdb_of(m)
        if v is None and k:
            if k in PLEX_IMDB:
                v = PLEX_IMDB[k]
            else:
                try:
                    with _plex_open(base + "/library/metadata/" + k, timeout=10) as r:
                        md = ((json.loads(r.read().decode("utf-8")).get("MediaContainer") or {}).get("Metadata") or [{}])[0]
                    v = _imdb_of(md)
                    PLEX_IMDB[k] = v
                except Exception:
                    v = None
        m["_imdb"] = round(v, 1) if v is not None else None

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, movies))


SONOS_ART_LOCK = __import__("threading").Lock()


def set_sonos_art(data):
    import base64
    import hashlib
    try:
        raw = base64.b64decode(str(data.get("data") or ""), validate=True)
    except Exception:
        return {"error": "ogiltig bild"}
    ctype = str(data.get("type") or "image/jpeg")
    if not raw or len(raw) > 1500000 or ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return {"error": "ogiltig bild"}
    v = hashlib.sha1(raw).hexdigest()[:16]
    with SONOS_ART_LOCK:
        SONOS_ART[v] = (ctype, raw)
        while len(SONOS_ART) > 200:
            SONOS_ART.pop(next(iter(SONOS_ART)))
    return {"url": "/api/sonos-art?v=" + v}


def page_version():
    """Ändras när index.html ändras (ny version utlagd). Öppna sidor laddar då om sig själva."""
    import hashlib
    try:
        st = os.stat(os.path.join(ROOT, "index.html"))
        if PAGE_VER.get("key") != (st.st_mtime, st.st_size):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                PAGE_VER.update(key=(st.st_mtime, st.st_size), v=hashlib.sha1(f.read()).hexdigest()[:12])
    except OSError:
        pass
    return PAGE_VER.get("v", "")


PAGE_VER = {}


def get_scenes():
    """Egna scener och sensorernas atgarder ur deck-konfigurationen. Hamtas av hubben nar en
    scen ska koras, sa att den alltid kor den senast sparade versionen."""
    _deck_load()
    with DECK_LOCK:
        deck = DECK_CACHE["deck"] if isinstance(DECK_CACHE.get("deck"), dict) else {}
        return {"rev": DECK_CACHE["rev"], "scenes": deck.get("scenes") if isinstance(deck.get("scenes"), list) else [],
                "sensors": deck.get("sensors") if isinstance(deck.get("sensors"), dict) else {},
                "inputs": deck.get("inputs") if isinstance(deck.get("inputs"), dict) else {},
                "schedules": deck.get("schedules") if isinstance(deck.get("schedules"), list) else [],
                "lampRules": deck.get("lampRules") if isinstance(deck.get("lampRules"), list) else [],
                "place": _place()}


def _place():
    """Var huset ligger (för soluppgång och solnedgång): samma plats som vädret på sidan."""
    try:
        e = (load_state().get("settings") or {}).get("edge") or {}
        return {"lat": float(e.get("lat", 60.6065)), "lon": float(e.get("lon", 15.6355)), "name": e.get("place") or "Falun"}
    except Exception:
        return {"lat": 60.6065, "lon": 15.6355, "name": "Falun"}


def get_deck_faces(want):
    _deck_load()
    with DECK_LOCK:
        faces = DECK_CACHE["faces"]
        if want:
            faces = {h: faces[h] for h in want if h in faces}
        return {"rev": DECK_CACHE["rev"], "faces": faces}


def _deck_hub_public():
    hub = DECK_LIVE["hub"]
    age = time.time() - (hub.get("at") or 0)
    online = age < DECK_HUB_MAX_AGE_S
    return {"online": online, "device": bool(online and hub.get("device")), "age": round(age, 1) if hub.get("at") else None,
            "sys": hub.get("sys") or [], "rooms": hub.get("rooms") or [], "ver": hub.get("ver") or "",
            "name": hub.get("name") or ""}


def get_deck_live(since_ev, seq=-1, wait=0.0):
    """seq + wait: håll kvar anropet tills något har ändrats sedan löpnumret seq."""
    _deck_load()
    with DECK_LOCK:
        L = DECK_LIVE
        if wait > 0 and seq >= 0 and L["seq"] <= seq:
            DECK_COND.wait(timeout=min(wait, 25.0))
        return {"seq": L["seq"], "deckRev": DECK_CACHE["rev"], "nav": L["nav"], "navRev": L["navRev"], "st": L["st"], "stRev": L["stRev"],
                "vals": L["vals"], "valsRev": L["valsRev"], "ev": [e for e in L["ev"] if e["id"] > since_ev],
                "evLast": L["evId"], "hub": _deck_hub_public()}


def patch_deck_live(data):
    """Slår ihop en ändring av körläget. nav ersätts helt (den som trycker senast vinner),
    st och vals slås ihop nyckel för nyckel (None tar bort), ev läggs till i händelseloggen
    och hub är hubbens livstecken."""
    _deck_load()
    with DECK_LOCK:
        L = DECK_LIVE
        persist = False
        if isinstance(data.get("nav"), dict):
            L["nav"] = data["nav"]; L["navRev"] += 1; persist = True
        if isinstance(data.get("st"), dict):
            for k, v in data["st"].items():
                if v is None:
                    L["st"].pop(str(k), None)
                else:
                    L["st"][str(k)] = v
            L["stRev"] += 1; persist = True
        if data.get("stReset"):
            L["st"] = {}; L["stRev"] += 1; persist = True
        if isinstance(data.get("vals"), dict):
            for k, v in data["vals"].items():
                if v is None:
                    L["vals"].pop(str(k), None)
                else:
                    L["vals"][str(k)] = v
            L["valsRev"] += 1; persist = True
        if isinstance(data.get("ev"), list):
            for e in data["ev"][:40]:
                if isinstance(e, dict):
                    L["evId"] += 1
                    e = dict(e); e["id"] = L["evId"]; e["t"] = round(time.time(), 2)
                    L["ev"].append(e)
            L["ev"] = L["ev"][-DECK_EV_KEEP:]
        if isinstance(data.get("hub"), dict):
            hub = dict(data["hub"]); hub["at"] = time.time()
            L["hub"] = hub
        if any(k in data for k in ("nav", "st", "stReset", "vals", "ev")):
            L["seq"] += 1
            DECK_COND.notify_all()
        if persist:
            try:
                _write_json(DECK_LIVE_FILE, {"nav": L["nav"], "st": L["st"], "vals": L["vals"]})
            except OSError:
                pass
        return {"ok": True, "navRev": L["navRev"], "stRev": L["stRev"], "valsRev": L["valsRev"], "evLast": L["evId"],
                "deckRev": DECK_CACHE["rev"]}


SOUMAYAPP_URL = os.environ.get("SOUMAYAPP_BASE_URL", "https://soumayapp-production.up.railway.app").rstrip("/")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(patch):
    """Slår ihop de kända nycklarna i patch med det sparade och skriver filen atomärt.
    "settings" slås ihop FÄLT FÖR FÄLT (inte en full ersättning) - annars kan en
    klient med en äldre/ofullständig kopia av S (t.ex. en flik som inte laddats
    om sedan en ny inställning lades till) radera fält den inte känner till bara
    genom att spara något helt orelaterat. Hände med Trådfri-valen 2026-11-01:
    en annan flik sparade sin äldre S utan de fälten och suddade ut dem."""
    with STATE_LOCK:
        d = load_state()
        for k in STATE_KEYS:
            if k not in patch:
                continue
            if patch[k] is None:
                d.pop(k, None)
            elif k == "settings" and isinstance(patch[k], dict):
                before = json.dumps(d.get("settings") or {}, sort_keys=True)
                merged = dict(d.get("settings") or {})
                for sk, sv in patch[k].items():
                    if sv is None:
                        merged.pop(sk, None)
                    else:
                        merged[sk] = sv
                d["settings"] = merged
                if json.dumps(merged, sort_keys=True) != before:
                    d["settingsRev"] = int(d.get("settingsRev") or 0) + 1
            else:
                d[k] = patch[k]
        # Ändringar i inställningarna skickas som en djup patch: bara de fält som ändrats,
        # ner på lägsta nivå. Två skärmar som ändrar olika saker samtidigt skriver då inte
        # över varandra (det gjorde de när hela objekt skickades).
        sp = patch.get("settingsPatch")
        if isinstance(sp, dict) and sp:
            cur = d.get("settings") or {}
            before = json.dumps(cur, sort_keys=True)
            merged = _deep_merge(cur, sp)
            d["settings"] = merged
            if json.dumps(merged, sort_keys=True) != before:
                d["settingsRev"] = int(d.get("settingsRev") or 0) + 1
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "settings": d.get("settings") or {}, "settingsRev": int(d.get("settingsRev") or 0)}


def _deep_merge(base, patch):
    """patch: {"$del": true} tar bort fältet, objekt slås ihop rekursivt, allt annat ersätter."""
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if isinstance(v, dict) and v.get("$del") is True:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_sonos_now():
    """Hamtas av sidan. 'playing' tvingas till False om inget rapporterats
    in nyligen (t.ex. om monitor.py inte kor just nu), sa en gammal latinfo
    inte ligger kvar och visas som om den fortfarande spelade."""
    with SONOS_LOCK:
        d = dict(SONOS_NOW)
    if time.time() - d.get("at", 0) > SONOS_MAX_AGE_S:
        d["playing"] = False
    return d


def set_sonos_now(patch):
    """Anropas av monitor.py (lokalt pa anvandarens natverk, inget skarmen
    sjalv kan na) nar Sonos-uppspelningen i Vardagsrum andras."""
    with SONOS_LOCK:
        for k in ("room", "title", "artist", "album", "art", "queue", "playing"):
            if k in patch:
                SONOS_NOW[k] = patch[k]
        SONOS_NOW["at"] = time.time()
        d = dict(SONOS_NOW)
    return {"ok": True, "now": d}


def get_tradfri_list():
    """Hamtas av sidans installningspanel for att fylla i forslag pa
    Tradfri-grupper/scener (moods) - sjalva valet sparas som vanligt i
    S/settings via /api/state, bara forslagslistan kommer harifran."""
    with TRADFRI_LOCK:
        return dict(TRADFRI_LIST)


def set_tradfri_list(patch):
    """Anropas av monitor.py (som har kontakt med Tradfri-gatewayen lokalt,
    den ar precis som Sonos helt onaba for sidan/Railway) med den senaste
    listan av grupper och deras scener."""
    with TRADFRI_LOCK:
        if "groups" in patch and isinstance(patch["groups"], list):
            TRADFRI_LIST["groups"] = patch["groups"]
        # alla enskilda lampor (for egna scener under /deck) och deras lage just nu
        if isinstance(patch.get("lamps"), list) and len(patch["lamps"]) <= 300:
            TRADFRI_LIST["lamps"] = [l for l in patch["lamps"] if isinstance(l, dict)]
        if isinstance(patch.get("other"), list) and len(patch["other"]) <= 200:
            TRADFRI_LIST["other"] = [i for i in patch["other"] if isinstance(i, dict)]
        if isinstance(patch.get("inputs"), list) and len(patch["inputs"]) <= 200:
            TRADFRI_LIST["inputs"] = [i for i in patch["inputs"] if isinstance(i, dict)]
        if isinstance(patch.get("speakers"), list) and len(patch["speakers"]) <= 100:
            TRADFRI_LIST["speakers"] = [str(x)[:80] for x in patch["speakers"]]
        # pågående scener och kommande tider, från hubben (visas i deck-redigeraren)
        if isinstance(patch.get("runs"), list) and len(patch["runs"]) <= 100:
            TRADFRI_LIST["runs"] = [r for r in patch["runs"] if isinstance(r, dict)]
            TRADFRI_LIST["runsAt"] = time.time()
        if isinstance(patch.get("upcoming"), list) and len(patch["upcoming"]) <= 200:
            TRADFRI_LIST["upcoming"] = [r for r in patch["upcoming"] if isinstance(r, dict)]
        if isinstance(patch.get("states"), dict) and len(patch["states"]) <= 300:
            TRADFRI_LIST["states"] = {str(k): v for k, v in patch["states"].items() if isinstance(v, dict)}
            TRADFRI_LIST["statesAt"] = time.time()
        TRADFRI_LIST["at"] = time.time()
        d = dict(TRADFRI_LIST)
    return {"ok": True, "now": d}


def _get_json(url, headers=None, timeout=12):
    with open_url(url, headers or {}, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def match_window(data):
    """Sant från avspark till 2,5 timmar efter, för nästa AIK-match i cachad data."""
    import time
    from datetime import datetime
    try:
        nm = data["teams"][0].get("next_match") or {}
        t = datetime.fromisoformat(nm["date"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return False
    return t - 300 <= time.time() <= t + 9000


# Vissa klubbar saknar emblem hos Highlightly (en riktig 404 på bildens egen CDN, inte bara
# ett tomt fält i svaret - t.ex. Leksand och Södertälje SK i Hockeyallsvenskan). Wikidata/
# Wikimedia Commons visade sig opålitligt (saknar ofta en riktig logga-egenskap för mindre
# klubbar, och fritextsökning på Commons kan träffa helt fel, overifierad bild). TheSportsDB
# har en gratis, strukturerad lag-databas med riktiga klubbmärken på en stabil CDN - filtrerat
# på sport så fel klubb med samma namn i en annan sport inte väljs av misstag.
# Cachas i minnet per processlivstid - varje klubb slås bara upp en gång.
_LOGO_CACHE = {}


def sportsdb_logo(name, sport_kw):
    if not name:
        return None
    key = f"{sport_kw}:{name}"
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    url = None
    try:
        d = _get_json("https://www.thesportsdb.com/api/v1/json/123/searchteams.php?" + urllib.parse.urlencode({"t": name}))
        for t in d.get("teams") or []:
            if sport_kw.lower() in (t.get("strSport") or "").lower():
                url = t.get("strBadge") or t.get("strLogo") or None
                if url:
                    break
    except Exception:
        url = None
    _LOGO_CACHE[key] = url
    return url


def fill_missing_logos(team, sport_kw):
    """Fyller i saknade klubbemblem (tabellrader, egen standing, nästa motståndare) via sportsdb_logo()."""
    def ensure(obj, name_key, logo_key):
        if obj and not obj.get(logo_key):
            obj[logo_key] = sportsdb_logo(obj.get(name_key), sport_kw)
    for r in team.get("table") or []:
        ensure(r, "name", "logo")
    st = (team.get("standing") or {}).get("team")
    if st:
        ensure(st, "name", "logo")
    nm = team.get("next_match")
    if nm:
        ensure(nm, "opponent", "opponent_logo")
    return team


ESPN_LEAGUE = "swe.1"
ESPN_AIK = "994"


def football_espn():
    """Tabell och AIK:s matcher från ESPN:s öppna JSON (samma struktur som Highlightly-varianten)."""
    base = "https://site.api.espn.com/apis"
    st = _get_json(f"{base}/v2/sports/soccer/{ESPN_LEAGUE}/standings")
    entries = st["children"][0]["standings"]["entries"]
    logo = lambda t: ((t.get("logos") or [{}])[0].get("href") or
                      f"https://a.espncdn.com/i/teamlogos/soccer/500/{t.get('id')}.png")
    table = []
    for e in entries:
        v = {x["name"]: x.get("value") for x in e.get("stats", [])}
        i = lambda k: None if v.get(k) is None else int(v[k])
        t = e["team"]
        table.append({"position": i("rank"), "points": i("points"), "name": t.get("displayName"), "id": t.get("id"),
                      "logo": logo(t), "g": i("gamesPlayed"), "w": i("wins"), "d": i("ties"), "l": i("losses"),
                      "gf": i("pointsFor"), "ga": i("pointsAgainst")})
    table.sort(key=lambda r: r["position"] or 99)

    events = []
    for extra in ("", "?fixture=true"):
        try:
            events += _get_json(f"{base}/site/v2/sports/soccer/{ESPN_LEAGUE}/teams/{ESPN_AIK}/schedule{extra}").get("events", [])
        except Exception:
            if not extra:
                raise
    seen, done, upcoming = set(), [], []
    for ev in events:
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors") or []
        me = next((x for x in cs if str(x.get("team", {}).get("id")) == ESPN_AIK), None)
        opp = next((x for x in cs if x is not me), None)
        if not me or not opp:
            continue
        home = next((x for x in cs if x.get("homeAway") == "home"), cs[0])
        away = next((x for x in cs if x.get("homeAway") == "away"), cs[-1])
        sc = lambda x: (x.get("score") or {}).get("displayValue") if isinstance(x.get("score"), dict) else x.get("score")
        m = {"opponent": opp["team"].get("displayName"), "opponent_logo": logo(opp["team"]),
             "is_home": me is home, "date": ev.get("date")}
        state = ((comp.get("status") or {}).get("type") or {})
        if state.get("completed"):
            m["score"] = f"{sc(home)} - {sc(away)}"
            done.append(m)
        else:
            if state.get("state") == "in":
                m["live"] = f"{sc(home)} - {sc(away)}"
            upcoming.append(m)
    done.sort(key=lambda m: m["date"], reverse=True)
    upcoming.sort(key=lambda m: m["date"])
    me_row = next((r for r in table if r["id"] == ESPN_AIK), None)
    standing = {"position": me_row["position"], "points": me_row["points"],
                "team": {"id": ESPN_AIK, "name": "AIK", "logo": me_row["logo"]}} if me_row else None
    return {"team_name": "AIK", "team_id": ESPN_AIK, "league": "Allsvenskan", "standing": standing, "table": table,
            "next_match": upcoming[0] if upcoming else None, "recent_results": done[:3]}


def football_highlightly(key):
    def get(path, params):
        return _get_json(f"https://soccer.highlightly.net{path}?{urllib.parse.urlencode(params)}", {"x-rapidapi-key": key})
    league, team = 96947, 321611
    table = sorted((get("/standings", {"leagueId": league, "season": 2026}).get("groups") or [{}])[0].get("standings", []),
                   key=lambda s: s.get("position", 99))
    allm = get("/matches", {"homeTeamId": team, "limit": 15}).get("data", []) + \
           get("/matches", {"awayTeamId": team, "limit": 15}).get("data", [])
    up = [m for m in allm if (m.get("state") or {}).get("description") == "Not started"]
    done = sorted([m for m in allm if (m.get("state") or {}).get("description") == "Finished"], key=lambda m: m["date"], reverse=True)[:3]

    def summary(m):
        if not m:
            return None
        home = m["homeTeam"]["id"] == team
        opp = m["awayTeam"] if home else m["homeTeam"]
        return {"opponent": opp.get("name"), "opponent_logo": opp.get("logo"), "is_home": home,
                "date": m.get("date"), "score": ((m.get("state") or {}).get("score") or {}).get("current")}
    tot = lambda s, k: (s.get("total") or {}).get(k)
    result = {"team_name": "AIK", "team_id": team, "league": "Allsvenskan",
              "standing": next((s for s in table if s["team"]["id"] == team), None),
              "table": [{"position": s.get("position"), "points": s.get("points"), "name": s["team"].get("name"),
                         "id": s["team"].get("id"), "logo": s["team"].get("logo"), "g": tot(s, "games"), "w": tot(s, "wins"),
                         "d": tot(s, "draws"), "l": tot(s, "loses"), "gf": tot(s, "scoredGoals"), "ga": tot(s, "receivedGoals")}
                        for s in table],
              "next_match": summary(min(up, key=lambda m: m["date"]) if up else None),
              "recent_results": [summary(m) for m in done]}
    fill_missing_logos(result, "Soccer")
    return result


HOCKEY_LEAGUE = 42483    # Hockeyallsvenskan
HOCKEY_AIK = 440751


def hockey_season():
    """Highlightlys hockey-API vill ha säsongen som startårtalet (26/27 -> 2026)."""
    from datetime import datetime
    now = datetime.now()
    return now.year if now.month >= 7 else now.year - 1


def hockey_highlightly(key):
    """AIK i Hockeyallsvenskan. Samma Highlightly-nyckel som fotbollen, annan domän.
    Standings-fälten skiljer sig från fotbollens: ingen 'points' (IIHF-poäng 3-2-1-0
    räknas ut här ur wins/winsOvertime/losesOvertime) och ingen oavgjord (d=0)."""
    def get(path, params):
        return _get_json(f"https://hockey.highlightly.net{path}?{urllib.parse.urlencode(params)}", {"x-rapidapi-key": key})
    league, team, season = HOCKEY_LEAGUE, HOCKEY_AIK, hockey_season()
    groups = get("/standings", {"leagueId": league, "season": season}).get("groups") or [{}]
    raw = groups[0].get("standings", [])

    def pts(s):
        w, wo, lo = s.get("wins") or 0, s.get("winsOvertime") or 0, s.get("losesOvertime") or 0
        return w * 3 - wo + lo

    table = sorted([{"position": s.get("position"), "points": pts(s), "name": s["team"].get("name"),
                      "id": s["team"].get("id"), "logo": s["team"].get("logo"), "g": s.get("gamesPlayed"),
                      "w": s.get("wins"), "d": 0, "l": s.get("loses"),
                      "gf": s.get("scoredGoals"), "ga": s.get("receivedGoals")} for s in raw],
                   key=lambda r: r["position"] or 99)
    allm = get("/matches", {"leagueId": league, "season": season, "homeTeamId": team, "limit": 10}).get("data", []) + \
           get("/matches", {"leagueId": league, "season": season, "awayTeamId": team, "limit": 10}).get("data", [])
    up = [m for m in allm if (m.get("state") or {}).get("description") == "Not started"]

    def summary(m):
        if not m:
            return None
        home = m["homeTeam"]["id"] == team
        opp = m["awayTeam"] if home else m["homeTeam"]
        return {"opponent": opp.get("name"), "opponent_logo": opp.get("logo"), "is_home": home, "date": m.get("date")}
    # OBS: "standing" ska (som i football_highlightly) ha ett nästlat team-objekt - inte
    # plockas ur den redan utplattade "table" (som saknar "team" helt). Annars försvinner
    # AIK:s eget emblem i "nästa match", eftersom frontend läser standing.team.logo.
    own = next((s for s in raw if s["team"].get("id") == team), None)
    standing = {"position": own.get("position"), "points": pts(own), "team": own.get("team")} if own else None
    result = {"team_name": "AIK", "team_id": team, "league": "Hockeyallsvenskan",
              "standing": standing, "table": table,
              "next_match": summary(min(up, key=lambda m: m["date"]) if up else None),
              "recent_results": []}
    fill_missing_logos(result, "Hockey")
    return result


def secret(name):
    """Hemlig nyckel: miljövariabeln NAME, annars filen name.key bredvid servern (filerna finns i .gitignore)."""
    k = os.environ.get(name, "").strip()
    if not k:
        try:
            with open(os.path.join(ROOT, name.lower().replace("_api_key", "") + ".key"), encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            pass
    return k


def resrobot_key():
    """ResRobot-nyckeln (samma som e-ink-tavlan): RESROBOT_API_KEY eller filen resrobot.key."""
    return secret("RESROBOT_API_KEY")


def shrink_image(data, ctype, maxw):
    """Skalar ner en bild till högst maxw px bred (JPEG, eller PNG om den har genomskinlighet).
    Utan Pillow, eller om något går fel, skickas originalet."""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False) or (im.width <= maxw and len(data) < 400 * 1024):
            return data, ctype
        im.draft("RGB", (maxw, maxw * 2))
        if im.width > maxw:
            im = im.resize((maxw, max(1, round(im.height * maxw / im.width))), Image.LANCZOS)
        out = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P") and ("transparency" in im.info or im.mode != "P"):
            im.save(out, "PNG", optimize=True)
            return out.getvalue(), "image/png"
        im.convert("RGB").save(out, "JPEG", quality=82, optimize=True, progressive=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, ctype


def og_fetch(url):
    """Delningsbild och ingress ur artikelsidans metataggar. Sparas i minnet."""
    import re
    import html
    cached = OG_CACHE.get(url)
    if cached is not None:
        return cached if isinstance(cached, dict) else {"img": cached, "desc": ""}
    img = desc = ""
    try:
        with open_url(url, {"User-Agent": BROWSER_UA}, timeout=7) as r:
            page = r.read(600 * 1024).decode("utf-8", "ignore")
        for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
            m = re.search(pat, page, re.I)
            if m:
                img = html.unescape(m.group(1))
                break
        for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']'):
            m = re.search(pat, page, re.I)
            if m:
                desc = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
                break
    except Exception as e:
        OG_CACHE[url] = {"img": "", "desc": "", "error": str(e)}    # försök inte igen varje gång
        return OG_CACHE[url]
    if len(OG_CACHE) > 3000:
        OG_CACHE.clear()
    OG_CACHE[url] = {"img": img, "desc": desc}
    return OG_CACHE[url]


def open_url(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def read_icy_title(url, timeout=8):
    """Läs första metadatablocket ur en Shoutcast/Icecast-ström."""
    with open_url(url, {"Icy-MetaData": "1"}, timeout=timeout) as r:
        metaint = r.headers.get("icy-metaint")
        info = {
            "name": r.headers.get("icy-name"),
            "genre": r.headers.get("icy-genre"),
            "title": None,
        }
        if not metaint:
            return info
        metaint = int(metaint)
        # Läs förbi ljuddatat fram till metadatat (högst två block).
        for _ in range(2):
            remaining = metaint
            while remaining > 0:
                chunk = r.read(min(remaining, 16384))
                if not chunk:
                    return info
                remaining -= len(chunk)
            length_byte = r.read(1)
            if not length_byte:
                return info
            length = length_byte[0] * 16
            if length == 0:
                continue
            raw = r.read(length).rstrip(b"\0")
            text = None
            for enc in ("utf-8", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            if text and "StreamTitle='" in text:
                start = text.index("StreamTitle='") + len("StreamTitle='")
                end = text.find("';", start)
                title = text[start:end if end != -1 else None].strip()
                info["title"] = title or None
            return info
        return info


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/stream" in (args[0] if args else ""):
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Svaren ska aldrig hämtas ur webbläsarens cache (annars kan gamla sparade inställningar läsas in)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        name = path[len("/assets/"):] if path.startswith("/assets/") else ""
        ok = path in ("/", "/callback", "/index.html", "/deck", "/deck.html", "/api/ping") or (
            name and "/" not in name and not name.startswith(".") and os.path.isfile(os.path.join(ROOT, "assets", name)))
        self.send_response(200 if ok else 404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        try:
            if path in ("/", "/callback", "/index.html"):
                self.path = "/index.html"
                return super().do_GET()
            if path in ("/deck", "/deck.html"):
                self.path = "/deck.html"
                return super().do_GET()
            if path == "/api/deck":
                return self._json(get_deck())
            if path == "/api/scenes":
                return self._json(get_scenes())
            if path == "/api/deck/faces":
                want = [h for h in (q.get("h", [""])[0] or "").split(",") if h]
                return self._json(get_deck_faces(want))
            if path == "/api/deck/live":
                return self._json(get_deck_live(int(q.get("ev", ["0"])[0] or 0), int(q.get("seq", ["-1"])[0] or -1), float(q.get("wait", ["0"])[0] or 0)))
            if path == "/api/stream":
                return self.proxy_stream(q.get("url", [""])[0])
            if path == "/api/icy":
                return self.icy(q.get("url", [""])[0])
            if path == "/api/radio":
                return self.radio(q)
            if path == "/api/img":
                return self.img(q.get("url", [""])[0])
            if path == "/api/cover":
                return self.cover(q.get("term", [""])[0])
            if path == "/api/rss":
                return self.rss(q.get("url", [""])[0], rich=q.get("rich", [""])[0] == "1")
            if path == "/api/ogimage":
                return self.ogimage(q.get("url", [""])[0])
            if path == "/api/stops":
                return self.stops(q.get("q", [""])[0])
            if path == "/api/departures":
                return self.departures(q.get("id", [""])[0])
            if path == "/api/trip":
                return self.trip(q.get("from", [""])[0], q.get("to", [""])[0])
            if path == "/api/steps":
                return self.steps()
            if path == "/api/album":
                return self.album(q.get("url", [""])[0])
            if path == "/api/football":
                return self.football(q)
            if path == "/api/hockey":
                return self.hockey(q)
            if path == "/api/send":
                return self.send_cmd(q.get("c", [""])[0])
            if path == "/api/cmd":
                return self.poll_cmd(int(q.get("since", ["0"])[0] or 0), float(q.get("wait", ["0"])[0] or 0))
            if path == "/api/claim":
                return self._json(claim_cmd(int(q.get("id", ["0"])[0] or 0), q.get("who", [""])[0]))
            if path == "/api/state":
                return self._json(load_state())
            if path == "/api/sonos-now":
                return self._json(get_sonos_now())
            if path == "/api/sonos-art":
                return self._sonos_art(q.get("v", [""])[0])
            if path == "/api/plex":
                if PLEX_TOKEN:
                    try:
                        return self._json(plex_list())
                    except Exception as e:
                        PLEX_STATE["err"] = str(e)
                        if PLEX_STATE["list"]:
                            return self._json(PLEX_STATE["list"])
                        return self._json({"items": [], "error": "Plex: %s" % e})
                return self._json(PLEX_LIST or {"items": [], "error": "Plex är inte inställt (PLEX_TOKEN saknas på servern)."})
            if path == "/api/plex-img":
                return self._plex_img(q)
            if path == "/api/tradfri-list":
                return self._json(get_tradfri_list())
            if path == "/api/ping":
                return self._json({"ok": True})
            if path == "/api/version":
                return self._json({"v": page_version()})
            if path == "/api/logo":
                f = next((n for n in ("dalatrafik-logo.svg", "dalatrafik-logo.png") if os.path.isfile(os.path.join(ROOT, "assets", n))), None)
                return self._json({"url": "/assets/" + f if f else ""})
            if path.startswith("/assets/"):
                return self.asset(path[len("/assets/"):])
            # Inga andra filer serveras: nyckelfiler och källkod ska inte gå att hämta.
            return self._json({"error": "hittades inte"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/state", "/api/sonos-now", "/api/tradfri-list", "/api/deck", "/api/deck/live", "/api/sonos-art", "/api/plex"):
            return self._json({"error": "hittades inte"}, 404)
        limit = DECK_MAX_BYTES if path == "/api/deck" else 2500 * 1024 if path == "/api/sonos-art" else 512 * 1024
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > limit:
                return self._json({"error": "fel storlek"}, 400)
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("inte ett objekt")
        except Exception as e:
            return self._json({"error": "ogiltig data: %s" % e}, 400)
        if path == "/api/sonos-now":
            return self._json(set_sonos_now(data))
        if path == "/api/sonos-art":
            return self._json(set_sonos_art(data))
        if path == "/api/plex":
            if isinstance(data.get("items"), list) and len(data["items"]) <= 200:
                PLEX_LIST.clear(); PLEX_LIST.update({"items": data["items"], "total": data.get("total"), "at": time.time()})
            return self._json({"ok": True})
        if path == "/api/tradfri-list":
            return self._json(set_tradfri_list(data))
        if path == "/api/deck":
            return self._json(save_deck(data))
        if path == "/api/deck/live":
            return self._json(patch_deck_live(data))
        return self._json(save_state(data))

    def _plex_img(self, q):
        """Omslag från Plex, hämtade av servern (så att token aldrig hamnar i webbläsaren)."""
        p = q.get("p", [""])[0]
        if not PLEX_TOKEN or not p.startswith("/library/"):
            return self._json({"error": "hittades inte"}, 404)
        try:
            w = max(50, min(1920, int(q.get("w", ["400"])[0]))); h = max(50, min(1920, int(q.get("h", ["600"])[0])))
            url = plex_base() + "/photo/:/transcode?" + urllib.parse.urlencode({"url": p, "width": w, "height": h, "minSize": 1, "upscale": 1})
            with _plex_open(url, timeout=15, accept="image/*") as r:
                data, ctype = r.read(3000000), (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        self.send_response(200)
        self.send_header("Content-Type", ctype if ctype.startswith("image/") else "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def _sonos_art(self, v):
        with SONOS_ART_LOCK:
            item = SONOS_ART.get(v)
        if not item:
            return self._json({"error": "hittades inte"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", item[0])
        self.send_header("Content-Length", str(len(item[1])))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(item[1])

    def asset(self, name):
        """Bilder i mappen assets (t.ex. Dalatrafiks logotyp). Bara enkla filnamn och bildformat."""
        import re
        types = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
                 ".html": "text/html; charset=utf-8"}       # html: bara testsidor som ligger i förrådet
        ext = os.path.splitext(name)[1].lower()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or ext not in types:
            return self._json({"error": "hittades inte"}, 404)
        fp = os.path.join(ROOT, "assets", name)
        if not os.path.isfile(fp):
            return self._json({"error": "hittades inte"}, 404)
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", types[ext])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- radioström med CORS ---
    def proxy_stream(self, url):
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        try:
            upstream = open_url(url, timeout=12)
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        ctype = (upstream.headers.get("Content-Type") or "audio/mpeg").lower()
        if "mpegurl" in ctype or "x-scpls" in ctype or url.lower().endswith((".m3u", ".pls", ".m3u8")):
            # Spellistor: plocka första ljudlänken och följ den.
            text = upstream.read(65536).decode("utf-8", "ignore")
            upstream.close()
            for line in text.splitlines():
                line = line.strip()
                if line.lower().startswith("file") and "=" in line:
                    line = line.split("=", 1)[1].strip()
                if line.startswith(("http://", "https://")) and not line.endswith(".m3u8"):
                    return self.proxy_stream(line)
            return self._json({"error": "spellistan saknar ljudlänk (HLS stöds inte)"}, 415)
        if not (ctype.startswith(("audio/", "application/ogg", "application/octet-stream", "video/mp2t"))):
            upstream.close()
            return self._json({"error": f"inte en ljudström ({ctype})"}, 415)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            upstream.close()
            self.close_connection = True

    def icy(self, url):
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        try:
            return self._json(read_icy_title(url))
        except Exception as e:
            return self._json({"error": str(e), "title": None}, 200)

    def radio(self, q):
        params = {
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": q.get("limit", ["40"])[0],
        }
        for key in ("tag", "name", "country", "countrycode", "tagList"):
            if key in q and q[key][0]:
                params[key] = q[key][0]
        qs = urllib.parse.urlencode(params)
        last = None
        for base in RADIO_MIRRORS:
            try:
                with open_url(f"{base}/json/stations/search?{qs}", timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                out = []
                seen = set()
                for s in data:
                    u = s.get("url_resolved") or s.get("url")
                    if not u or u in seen or u.endswith(".m3u8") or s.get("hls") == 1:
                        continue
                    seen.add(u)
                    out.append({
                        "id": s.get("stationuuid"),
                        "name": (s.get("name") or "").strip(),
                        "url": u,
                        "favicon": s.get("favicon") or "",
                        "tags": s.get("tags") or "",
                        "country": s.get("countrycode") or "",
                        "codec": s.get("codec") or "",
                        "bitrate": s.get("bitrate") or 0,
                    })
                return self._json(out)
            except Exception as e:
                last = e
        return self._json({"error": f"radio-browser svarar inte: {last}"}, 502)

    def cover(self, term):
        """Omslag, album och år för en radiolåt via iTunes sök-API."""
        term = term.strip()
        if not term:
            return self._json({})
        qs = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 5, "country": "SE"})
        try:
            with open_url(f"https://itunes.apple.com/search?{qs}", timeout=8) as r:
                res = json.loads(r.read().decode("utf-8")).get("results") or []
        except Exception as e:
            return self._json({"error": str(e)})
        import re
        STOP = {"the", "and", "och", "feat", "ft", "with", "med", "de", "la", "le"}
        words = lambda x: [w for w in re.split(r"[^a-z0-9åäöéü]+", re.sub(r"\(.*?\)|\[.*?\]", " ", (x or "").lower())) if len(w) > 1 and w not in STOP]
        tw = set(words(term))
        def hit(x):
            w = words(x)
            return bool(w) and sum(1 for v in w if v in tw) / len(w) >= 0.6
        s = next((r for r in res if hit(r.get("artistName")) and hit(r.get("trackName"))), None)
        if not s:
            return self._json({})
        art = (s.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        return self._json({
            "cover": art,
            "album": s.get("collectionName") or "",
            "year": (s.get("releaseDate") or "")[:4],
            "artist": s.get("artistName") or "",
            "title": s.get("trackName") or "",
        })

    def rss(self, url, rich=False):
        """Rubriker, ingress, länk och bild ur ett RSS- eller Atom-flöde, som JSON."""
        import re
        import html
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        try:
            with open_url(url, {"User-Agent": BROWSER_UA, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"}, timeout=10) as r:
                raw = r.read(3 * 1024 * 1024)
            root = ET.fromstring(raw)
        except Exception as e:
            return self._json({"error": str(e)}, 502)

        def local(tag):
            return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

        def child(el, *names):
            for c in el:
                if local(c.tag) in names and (c.text or "").strip():
                    return c.text.strip()
            return ""

        def clean(s):
            s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
            return re.sub(r"\s+", " ", s).strip()

        def image(el):
            for c in el.iter():
                n, a = local(c.tag), c.attrib
                if n == "enclosure" and a.get("url") and a.get("type", "image").startswith("image"):
                    return a["url"]
                if n in ("content", "thumbnail") and a.get("url") and (a.get("medium") == "image" or a.get("type", "image").startswith("image")):
                    return a["url"]
            for c in el.iter():
                m = re.search(r'<img[^>]+src="([^"]+)"', c.text or "")
                if m:
                    return html.unescape(m.group(1))
            return ""

        def link(el):
            for c in el:
                if local(c.tag) == "link":
                    if (c.text or "").strip():
                        return c.text.strip()
                    if c.attrib.get("href") and c.attrib.get("rel", "alternate") == "alternate":
                        return c.attrib["href"]
            return ""

        out = []
        for el in root.iter():
            if local(el.tag) not in ("item", "entry"):
                continue
            title = clean(child(el, "title"))
            if not title:
                continue
            date = child(el, "pubDate", "published", "updated", "date")
            iso = ""
            if date:
                try:
                    iso = parsedate_to_datetime(date).isoformat()
                except Exception:
                    iso = date
            desc = clean(child(el, "description", "summary", "encoded", "content"))
            if len(desc) > 600:
                desc = desc[:597].rsplit(" ", 1)[0] + "..."
            lk = link(el)
            # Bings nyhetssök länkar via sig själv; plocka fram artikelns riktiga adress
            if "bing.com/news/apiclick" in lk or ("bing.com" in lk and "url=" in lk):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(lk).query)
                lk = (q.get("url") or [lk])[0]
            out.append({"title": title, "desc": desc, "date": iso, "link": lk, "img": image(el)})
            if len(out) >= 50:
                break
        if rich:
            # Fyll i bild och längre ingress från artikelsidan där flödet saknar det
            from concurrent.futures import ThreadPoolExecutor
            need = [a for a in out if a["link"] and (not a["img"] or len(a["desc"]) < 140)][:40]
            with ThreadPoolExecutor(8) as ex:
                for a, og in zip(need, ex.map(lambda a: og_fetch(a["link"]), need)):
                    if not a["img"] and og.get("img"):
                        a["img"] = og["img"]
                    if len(og.get("desc") or "") > len(a["desc"]):
                        a["desc"] = og["desc"][:600]
        return self._json(out)

    def ogimage(self, url):
        """Artikelns delningsbild (og:image) och ingress (og:description), för flöden som saknar det."""
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        return self._json(og_fetch(url))

    # --- kollektivtrafik via Trafiklab ResRobot v2.1, samma API och nyckel som e-ink-tavlan (fetchers/bus.py) ---
    def rr_get(self, endpoint, params):
        key = resrobot_key()
        if not key:
            raise LookupError("Ingen RESROBOT_API_KEY på servern")
        qs = urllib.parse.urlencode({**params, "format": "json", "accessId": key})
        with open_url(f"https://api.resrobot.se/v2.1/{endpoint}?{qs}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def stops(self, q):
        q = q.strip()
        if not q:
            return self._json([])
        try:
            j = self.rr_get("location.name", {"input": q})
        except LookupError as e:
            return self._json({"error": str(e), "nokey": True}, 200)
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        out = [{"id": s["StopLocation"].get("extId"), "name": s["StopLocation"].get("name")}
               for s in j.get("stopLocationOrCoordLocation") or [] if "StopLocation" in s]
        return self._json(out[:15])

    def departures(self, sid):
        import time
        sid = "".join(ch for ch in sid if ch.isalnum())
        if not sid:
            return self._json({"error": "ange id"}, 400)
        hit = DEP_CACHE.get(sid)
        if hit and time.time() - hit[0] < 60:
            return self._json(hit[1])
        try:
            # Minst 8 avgångar om det går: först 3 timmar framåt, vid gles trafik (t.ex. en avgång i timmen) upp till 12 timmar
            for minutes in (180, 720):
                j = self.rr_get("departureBoard", {"id": sid, "duration": minutes, "maxJourneys": 40})
                deps = j.get("Departure") or []
                if isinstance(deps, dict):  # ResRobot ger ett objekt i stället för en lista när det bara finns en avgång
                    deps = [deps]
                if len(deps) >= 8:
                    break
        except LookupError as e:
            return self._json({"error": str(e), "nokey": True}, 200)
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        out = []
        for d in deps:
            prod = d.get("ProductAtStop") or d.get("Product") or {}
            if isinstance(prod, list):
                prod = prod[0] if prod else {}
            sched = f"{d.get('date', '')}T{d.get('time', '')}"
            rt = f"{d.get('rtDate') or d.get('date', '')}T{d['rtTime']}" if d.get("rtTime") else None
            out.append({"line": prod.get("displayNumber") or prod.get("line") or prod.get("num") or "",
                        "dest": re.sub(r"\s*\(.*?\)\s*$", "", d.get("direction") or ""),
                        "mode": prod.get("catOutL") or "", "sched": sched, "rt": rt, "live": bool(rt),
                        "canceled": bool(d.get("cancelled")), "plat": d.get("rtTrack") or d.get("track") or ""})
        out.sort(key=lambda x: x["rt"] or x["sched"])
        DEP_CACHE[sid] = (time.time(), out)
        return self._json(out)

    def trip(self, src, dst):
        """Resor från en hållplats till en annan (ResRobot /trip, som e-ink-tavlans fetch_trip): avgång, ankomst, linjer och byten."""
        import time
        src = "".join(ch for ch in src if ch.isalnum()); dst = "".join(ch for ch in dst if ch.isalnum())
        if not src or not dst:
            return self._json({"error": "ange from och to"}, 400)
        key = src + ">" + dst
        hit = DEP_CACHE.get(key)
        if hit and time.time() - hit[0] < 60:
            return self._json(hit[1])
        # Bara resor med en buss direkt från starthållplatsen hela vägen (ingen promenad till annan hållplats, inget byte),
        # så att skylten visar de linjer som faktiskt går från hållplatsen.
        def legs_of(t):
            legs = (t.get("LegList") or {}).get("Leg") or []
            return [legs] if isinstance(legs, dict) else legs

        def direct(t):
            legs = legs_of(t)
            jny = [l for l in legs if l.get("type") == "JNY"]
            if len(jny) != 1 or not legs or legs[0].get("type") != "JNY":
                return False
            ext = str((jny[0].get("Origin") or {}).get("extId") or "")
            return not ext or ext == src

        # ResRobot ger högst 6 resor per sökning: hämtar nya omgångar från minuten efter den sista avgången
        trips, params = [], {"originId": src, "destId": dst, "numF": 6}
        try:
            for _ in range(4):
                j = self.rr_get("trip", params)
                got = j.get("Trip") or []
                if isinstance(got, dict):    # en enda resa kommer som objekt, inte lista
                    got = [got]
                trips += [t for t in got if direct(t)]
                if len(got) < 6 or len(trips) >= 8:
                    break
                last = None
                for t in got:
                    legs = (t.get("LegList") or {}).get("Leg") or []
                    legs = [legs] if isinstance(legs, dict) else legs
                    o = next((l.get("Origin") for l in legs if l.get("type") == "JNY"), None)
                    if o and o.get("date") and o.get("time"):
                        last = max(last or "", f"{o['date']}T{o['time']}")
                if not last:
                    break
                import datetime as _dt
                nxt = _dt.datetime.fromisoformat(last) + _dt.timedelta(minutes=1)
                params = {**params, "date": nxt.strftime("%Y-%m-%d"), "time": nxt.strftime("%H:%M")}
        except LookupError as e:
            return self._json({"error": str(e), "nokey": True}, 200)
        except Exception as e:
            if not trips:
                return self._json({"error": str(e)}, 502)
        out, seen = [], set()
        for t in trips:
            legs = (t.get("LegList") or {}).get("Leg") or []
            if isinstance(legs, dict):
                legs = [legs]
            jny = [l for l in legs if l.get("type") == "JNY"]
            if not jny:
                continue
            def line(l):
                p = l.get("Product") or {}
                if isinstance(p, list):
                    p = p[0] if p else {}
                return p.get("displayNumber") or p.get("num") or p.get("line") or ""
            o, d = jny[0]["Origin"], jny[-1]["Destination"]
            rt_dep = f"{o.get('rtDate') or o.get('date')}T{o['rtTime']}" if o.get("rtTime") else None
            rt_arr = f"{d.get('rtDate') or d.get('date')}T{d['rtTime']}" if d.get("rtTime") else None
            sched = f"{o.get('date')}T{o.get('time')}"
            lines = "/".join(dict.fromkeys(line(l) for l in jny))
            if (sched, lines) in seen:
                continue
            seen.add((sched, lines))
            p0 = jny[0].get("Product") or {}
            p0 = (p0[0] if p0 else {}) if isinstance(p0, list) else p0
            out.append({"line": lines, "mode": p0.get("catOutL") or "", "sched": sched, "rt": rt_dep,
                        "arr": rt_arr or f"{d.get('date')}T{d.get('time')}", "changes": len(jny) - 1,
                        "canceled": any(l.get("cancelled") for l in jny), "live": bool(rt_dep)})
        out.sort(key=lambda x: x["rt"] or x["sched"])
        DEP_CACHE[key] = (time.time(), out)
        return self._json(out)

    # --- Steg: samma källa som e-ink-tavlan (fetchers/steps.py): SoumayApps /api/steps, dagens summa ---
    def steps(self):
        import time
        if STEPS_CACHE["data"] and time.time() - STEPS_CACHE["at"] < 60:
            return self._json(STEPS_CACHE["data"])
        try:
            with open_url(SOUMAYAPP_URL + "/api/steps", timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return self._json({"ok": False, "error": f"Kunde inte hämta steg: {e}"})
        out = {"ok": True, "date": d.get("date"), "steps": int(d.get("steps") or 0),
               "has_data": bool(d.get("has_data")), "updated": d.get("updated")}
        STEPS_CACHE.update(data=out, at=time.time())
        return self._json(out)

    # --- Google Foto: publikt delade album, samma metod som e-ink-tavlan (fetchers/google_photos.py) ---
    # Inte Googles API (stängt för det här sedan mars 2025) utan bildadresserna som ligger inbäddade i
    # albumets publika sida. Bara /pw/-adresser är albumbilder; profilbilder och Googles standardavatar filtreras bort.
    def album(self, url):
        import re
        import time
        url = url.strip()
        if not re.match(r"^https://(photos\.app\.goo\.gl|photos\.google\.com|goo\.gl)/", url):
            return self._json({"error": "Länken måste vara en delningslänk från Google Foto (photos.app.goo.gl/...)"}, 400)
        hit = ALBUM_CACHE.get(url)
        if hit and time.time() - hit[0] < 3600:
            return self._json({"urls": hit[1]})
        try:
            with open_url(url, {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}, timeout=15) as r:
                page = r.read(8 * 1024 * 1024).decode("utf-8", "ignore")
        except Exception as e:
            if hit:
                return self._json({"urls": hit[1], "error": f"{e} (visar sparad lista)"})
            return self._json({"error": f"Nätverksfel: {e}"}, 502)
        found = re.findall(r"https:\\?/\\?/lh3\.googleusercontent\.com\\?/pw(?:\\?/[A-Za-z0-9_.\-]+)+", page)
        urls = list(dict.fromkeys(u.replace("\\/", "/") for u in found if "AAAAAAAAAAI/AAAAAAAAAAA" not in u))
        if not urls:
            return self._json({"error": "Hittade inga bilder. Är länken skapad med 'Skapa länk' (inte 'Bjud in personer')?"})
        ALBUM_CACHE[url] = (time.time(), urls)
        return self._json({"urls": urls})

    # --- Fotboll: Highlightly, samma lag och ligor som e-ink-tavlan (fetchers/football.py) ---
    # Bara AIK här. Gratisnivån ger 100 anrop/dygn och delas med e-ink-tavlan (~58/dygn). Den här servern
    # hämtar högst var tredje timme (3 anrop per hämtning = 24/dygn), så de båda tillsammans håller sig under taket.
    def football(self, q):
        """
        AIK i Allsvenskan. Källa väljs i inställningarna:
          espn         ESPN:s öppna JSON (ingen nyckel, ingen dagskvot) - standard
          highlightly  Highlightly (nyckel, 100 anrop/dygn delade med e-ink-tavlan, 3 anrop per hämtning)
        Hämtningsintervallet (minuter) styrs också från inställningarna. Under en AIK-match
        (avspark till 2,5 h efter) hämtar ESPN var 5:e minut så att resultat och tabell kommer direkt.
        Cachen sparas på volymen, så omstarter kostar inga extra anrop.
        """
        import time
        src = (q.get("src", ["espn"])[0] or "espn").lower()
        if src not in ("espn", "highlightly"):
            src = "espn"
        try:
            mins = max(5, min(24 * 60, int(q.get("mins", ["60"])[0] or 60)))
        except ValueError:
            mins = 60
        if src == "highlightly":
            mins = max(mins, 60)  # skyddar dagskvoten
        c = FOOTBALL_CACHE.setdefault(src, {"data": None, "at": 0, "fail": 0, "err": ""})
        fp = os.path.join(DATA_DIR, f"football-{src}.json")
        if not c["data"]:
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                c.update(data=d["data"], at=d["at"])
            except (OSError, ValueError, KeyError):
                pass
        ttl = mins * 60
        if src == "espn" and c["data"] and match_window(c["data"]):
            ttl = 5 * 60
        now = time.time()
        if c["data"] and now - c["at"] < ttl:
            return self._json(c["data"])
        if now - c["fail"] < 1800:
            return self._json(c["data"] or {"ok": False, "error": c["err"]})
        try:
            if src == "espn":
                team = football_espn()
            else:
                key = secret("HIGHLIGHTLY_API_KEY")
                if not key:
                    return self._json({"ok": False, "nokey": True, "error": "Ingen HIGHLIGHTLY_API_KEY på servern"})
                team = football_highlightly(key)
            data = {"ok": True, "at": int(now * 1000), "src": src, "teams": [team]}
        except Exception as e:
            name = "ESPN" if src == "espn" else "Highlightly"
            c.update(fail=now, err=f"{name}: {e}")
            return self._json(c["data"] or {"ok": False, "error": c["err"]})
        c.update(data=data, at=now, fail=0)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(fp + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"data": data, "at": now}, f, ensure_ascii=False)
            os.replace(fp + ".tmp", fp)
        except OSError:
            pass
        return self._json(data)

    # --- Hockey: Highlightly, AIK i Hockeyallsvenskan (samma nyckel/kvot som fotbollens Highlightly-källa) ---
    def hockey(self, q):
        import time
        try:
            mins = max(60, min(24 * 60, int(q.get("mins", ["60"])[0] or 60)))
        except ValueError:
            mins = 60
        c = HOCKEY_CACHE
        # v3: bytt loggokälla (Wikidata -> TheSportsDB) - nytt filnamn så gammal cachad data
        # (med saknade loggor för t.ex. Leksand/Södertälje) inte används vidare upp till en timme.
        fp = os.path.join(DATA_DIR, "hockey-highlightly-v3.json")
        if not c["data"]:
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                c.update(data=d["data"], at=d["at"])
            except (OSError, ValueError, KeyError):
                pass
        now = time.time()
        if c["data"] and now - c["at"] < mins * 60:
            return self._json(c["data"])
        if now - c["fail"] < 1800:
            return self._json(c["data"] or {"ok": False, "error": c["err"]})
        try:
            key = secret("HIGHLIGHTLY_API_KEY")
            if not key:
                return self._json({"ok": False, "nokey": True, "error": "Ingen HIGHLIGHTLY_API_KEY på servern"})
            team = hockey_highlightly(key)
            data = {"ok": True, "at": int(now * 1000), "src": "highlightly", "teams": [team]}
        except Exception as e:
            c.update(fail=now, err=f"Highlightly: {e}")
            return self._json(c["data"] or {"ok": False, "error": c["err"]})
        c.update(data=data, at=now, fail=0)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(fp + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"data": data, "at": now}, f, ensure_ascii=False)
            os.replace(fp + ".tmp", fp)
        except OSError:
            pass
        return self._json(data)

    # --- styrning utifrån (t.ex. jalusins reedbrytare på Pi:n) ---
    # curl "http://127.0.0.1:8888/api/send?c=open"   (open, close, next, prev, playpause, viz:3)
    def send_cmd(self, c):
        c = c.strip()
        if not c:
            return self._json({"error": "ange c="}, 400)
        with CMD_LOCK:
            CMD_STATE["id"] += 1
            CMD_STATE["list"].append({"id": CMD_STATE["id"], "c": c})
            CMD_STATE["list"] = CMD_STATE["list"][-60:]
            cid = CMD_STATE["id"]
            CMD_COND.notify_all()
        return self._json({"ok": True, "id": cid})

    def poll_cmd(self, since, wait=0.0):
        """wait > 0: håll kvar anropet tills ett nytt kommando kommer (högst wait sekunder).
        Hubben använder det, så att ett tryck når fram direkt i stället för vid nästa fråga."""
        with CMD_LOCK:
            if wait > 0 and CMD_STATE["id"] <= since:
                CMD_COND.wait(timeout=min(wait, 25.0))
            items = [x for x in CMD_STATE["list"] if x["id"] > since]
            last = CMD_STATE["id"]
        return self._json({"last": last, "items": items})

    def img(self, url):
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        try:
            with open_url(url, timeout=8) as r:
                data = r.read(8 * 1024 * 1024)
                ctype = r.headers.get("Content-Type", "image/jpeg")
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        if not ctype.startswith("image/"):
            return self._json({"error": "inte en bild"}, 415)
        # Artikelbilder kan vara flera tusen pixlar breda. Skärmen i skåpet (1 GB minne) visar dem
        # högst ~900 px breda, så de skalas ner här; annars fylls minnet av avkodade jättebilder.
        data, ctype = shrink_image(data, ctype, 960)
        self.send_response(200)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    ap = argparse.ArgumentParser()
    # På Railway/molnet sätts PORT; då lyssnar vi på alla gränssnitt.
    env_port = os.environ.get("PORT")
    ap.add_argument("--host", default="0.0.0.0" if env_port else "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(env_port or 8888))
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"Retrospelaren: http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/")
    print("Stäng med Ctrl+C.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
