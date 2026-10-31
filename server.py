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
  /api/football     AIK i Allsvenskan via Highlightly (nyckel i HIGHLIGHTLY_API_KEY)
  /api/send?c=      skicka kommando till sidan: open, close, next, prev,
                    playpause, viz:1-5, palette:svartvit|sepia|fosfor|farg

Bara Pythons standardbibliotek. Inget att installera.
"""
import argparse
import json
import re
import os
import socket
import ssl
import sys
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
OG_CACHE = {}
DEP_CACHE = {}
ALBUM_CACHE = {}
FOOTBALL_CACHE = {"data": None, "at": 0}
STEPS_CACHE = {"data": None, "at": 0}
SOUMAYAPP_URL = os.environ.get("SOUMAYAPP_BASE_URL", "https://soumayapp-production.up.railway.app").rstrip("/")


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
                return self.football()
            if path == "/api/send":
                return self.send_cmd(q.get("c", [""])[0])
            if path == "/api/cmd":
                return self.poll_cmd(int(q.get("since", ["0"])[0] or 0))
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
        # ResRobot ger högst 6 resor per sökning: hämtar en andra omgång från minuten efter den sista avgången
        trips, params = [], {"originId": src, "destId": dst, "numF": 6}
        try:
            for _ in range(2):
                j = self.rr_get("trip", params)
                got = j.get("Trip") or []
                if isinstance(got, dict):    # en enda resa kommer som objekt, inte lista
                    got = [got]
                trips += got
                if len(got) < 6:
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
    def football(self):
        import time
        if FOOTBALL_CACHE["data"] and time.time() - FOOTBALL_CACHE["at"] < 3 * 3600:
            return self._json(FOOTBALL_CACHE["data"])
        key = secret("HIGHLIGHTLY_API_KEY")
        if not key:
            return self._json({"ok": False, "nokey": True, "error": "Ingen HIGHLIGHTLY_API_KEY på servern"})

        def get(path, params):
            qs = urllib.parse.urlencode(params)
            with open_url(f"https://soccer.highlightly.net{path}?{qs}", {"x-rapidapi-key": key}, timeout=12) as r:
                return json.loads(r.read().decode("utf-8"))

        def standings(league):
            return (get("/standings", {"leagueId": league, "season": 2026}).get("groups") or [{}])[0].get("standings", [])

        def matches(team):
            # API:et ger listorna i fallande datumordning per lista; sorterar själva i stället för att lita på ordningen
            allm = get("/matches", {"homeTeamId": team, "limit": 15}).get("data", []) + \
                   get("/matches", {"awayTeamId": team, "limit": 15}).get("data", [])
            up = [m for m in allm if (m.get("state") or {}).get("description") == "Not started"]
            done = [m for m in allm if (m.get("state") or {}).get("description") == "Finished"]
            return (min(up, key=lambda m: m["date"]) if up else None), sorted(done, key=lambda m: m["date"], reverse=True)[:3]

        def summary(team, m):
            if not m:
                return None
            home = m["homeTeam"]["id"] == team
            opp = m["awayTeam"] if home else m["homeTeam"]
            return {"opponent": opp.get("name"), "opponent_logo": opp.get("logo"), "is_home": home,
                    "date": m.get("date"), "score": ((m.get("state") or {}).get("score") or {}).get("current")}

        def team_block(name, league_name, league, team):
            table = sorted(standings(league), key=lambda s: s.get("position", 99))
            nxt, recent = matches(team)
            return {"team_name": name, "team_id": team, "league": league_name,
                    "standing": next((s for s in table if s["team"]["id"] == team), None),
                    "table": [{"position": s.get("position"), "points": s.get("points"),
                               "name": s["team"].get("name"), "id": s["team"].get("id"), "logo": s["team"].get("logo")} for s in table],
                    "next_match": summary(team, nxt), "recent_results": [summary(team, m) for m in recent]}
        try:
            data = {"ok": True, "at": int(time.time() * 1000),
                    "teams": [team_block("AIK", "Allsvenskan", 96947, 321611)]}
        except Exception as e:
            if FOOTBALL_CACHE["data"]:
                return self._json(FOOTBALL_CACHE["data"])
            return self._json({"ok": False, "error": f"Highlightly: {e}"})
        FOOTBALL_CACHE.update(data=data, at=time.time())
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
