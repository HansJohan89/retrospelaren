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
RADIO_MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
SSL_CTX = ssl.create_default_context()
CMD_LOCK = __import__("threading").Lock()
CMD_STATE = {"id": 0, "list": []}
# Vad Sonos (Vardagsrum) spelar just nu - rapporteras in av monitor.py (som
# styr Sonos lokalt, se den filens kommentarer för varför: Sonos syns aldrig
# i Spotifys eget webb-API), och läses av sidan som ett eget, separat spår
# vid sidan av den vanliga Spotify-statusen.
SONOS_LOCK = __import__("threading").Lock()
SONOS_NOW = {"room": "", "title": "", "artist": "", "album": "", "art": "", "next": "", "playing": False, "at": 0}
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
STEPS_CACHE = {"data": None, "at": 0}
# Var inställningarna sparas. På Railway: volymens monteringspunkt (RAILWAY_VOLUME_MOUNT_PATH).
DATA_DIR = (os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(ROOT, "data")).strip()
STATE_FILE = os.path.join(DATA_DIR, "state.json")
STATE_KEYS = ("settings", "favs", "lastStation")
STATE_LOCK = __import__("threading").Lock()
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
                merged = dict(d.get("settings") or {})
                for sk, sv in patch[k].items():
                    if sv is None:
                        merged.pop(sk, None)
                    else:
                        merged[sk] = sv
                d["settings"] = merged
            else:
                d[k] = patch[k]
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True}


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
        for k in ("room", "title", "artist", "album", "art", "next", "playing"):
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
    return {"team_name": "AIK", "team_id": team, "league": "Allsvenskan",
            "standing": next((s for s in table if s["team"]["id"] == team), None),
            "table": [{"position": s.get("position"), "points": s.get("points"), "name": s["team"].get("name"),
                       "id": s["team"].get("id"), "logo": s["team"].get("logo"), "g": tot(s, "games"), "w": tot(s, "wins"),
                       "d": tot(s, "draws"), "l": tot(s, "loses"), "gf": tot(s, "scoredGoals"), "ga": tot(s, "receivedGoals")}
                      for s in table],
            "next_match": summary(min(up, key=lambda m: m["date"]) if up else None),
            "recent_results": [summary(m) for m in done]}


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
        ok = path in ("/", "/callback", "/index.html", "/api/ping") or (
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
                return self.rss(q.get("url", [""])[0])
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
            if path == "/api/send":
                return self.send_cmd(q.get("c", [""])[0])
            if path == "/api/cmd":
                return self.poll_cmd(int(q.get("since", ["0"])[0] or 0))
            if path == "/api/state":
                return self._json(load_state())
            if path == "/api/sonos-now":
                return self._json(get_sonos_now())
            if path == "/api/tradfri-list":
                return self._json(get_tradfri_list())
            if path == "/api/ping":
                return self._json({"ok": True})
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
        if path not in ("/api/state", "/api/sonos-now", "/api/tradfri-list"):
            return self._json({"error": "hittades inte"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 512 * 1024:
                return self._json({"error": "fel storlek"}, 400)
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("inte ett objekt")
        except Exception as e:
            return self._json({"error": "ogiltig data: %s" % e}, 400)
        if path == "/api/sonos-now":
            return self._json(set_sonos_now(data))
        if path == "/api/tradfri-list":
            return self._json(set_tradfri_list(data))
        return self._json(save_state(data))

    def asset(self, name):
        """Bilder i mappen assets (t.ex. Dalatrafiks logotyp). Bara enkla filnamn och bildformat."""
        import re
        types = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
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

    def rss(self, url):
        """Rubriker, ingress, länk och bild ur ett RSS- eller Atom-flöde, som JSON."""
        import re
        import html
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        try:
            with open_url(url, timeout=10) as r:
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
            out.append({"title": title, "desc": desc, "date": iso, "link": link(el), "img": image(el)})
            if len(out) >= 20:
                break
        return self._json(out)

    def ogimage(self, url):
        """Artikelns delningsbild (og:image), för flöden som saknar bild."""
        import re
        import html
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "ogiltig url"}, 400)
        if url in OG_CACHE:
            return self._json({"img": OG_CACHE[url]})
        img = ""
        try:
            with open_url(url, timeout=8) as r:
                page = r.read(600 * 1024).decode("utf-8", "ignore")
            for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
                m = re.search(pat, page, re.I)
                if m:
                    img = html.unescape(m.group(1))
                    break
        except Exception as e:
            return self._json({"error": str(e), "img": ""})
        if len(OG_CACHE) > 500:
            OG_CACHE.clear()
        OG_CACHE[url] = img
        return self._json({"img": img})

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

    # --- styrning utifrån (t.ex. jalusins reedbrytare på Pi:n) ---
    # curl "http://127.0.0.1:8888/api/send?c=open"   (open, close, next, prev, playpause, viz:3)
    def send_cmd(self, c):
        c = c.strip()
        if not c:
            return self._json({"error": "ange c="}, 400)
        with CMD_LOCK:
            CMD_STATE["id"] += 1
            CMD_STATE["list"].append({"id": CMD_STATE["id"], "c": c})
            CMD_STATE["list"] = CMD_STATE["list"][-20:]
            cid = CMD_STATE["id"]
        return self._json({"ok": True, "id": cid})

    def poll_cmd(self, since):
        with CMD_LOCK:
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
        self.send_response(200)
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
