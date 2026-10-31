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
  /api/departures?id=  avgångar närmaste två timmarna från en hållplats
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


def resrobot_key():
    """ResRobot-nyckeln (samma som e-ink-tavlan): miljövariabeln RESROBOT_API_KEY, annars filen resrobot.key bredvid servern."""
    k = os.environ.get("RESROBOT_API_KEY", "").strip()
    if not k:
        try:
            with open(os.path.join(ROOT, "resrobot.key"), encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            pass
    return k


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
        self.send_response(200 if path in ("/", "/callback", "/index.html", "/api/ping") else 404)
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
            if path == "/api/send":
                return self.send_cmd(q.get("c", [""])[0])
            if path == "/api/cmd":
                return self.poll_cmd(int(q.get("since", ["0"])[0] or 0))
            if path == "/api/ping":
                return self._json({"ok": True})
            # Inga andra filer serveras: nyckelfiler och källkod ska inte gå att hämta.
            return self._json({"error": "hittades inte"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            # 120 min framåt: ResRobot letar annars bara 60 min, för lite vid gles trafik
            j = self.rr_get("departureBoard", {"id": sid, "duration": 120, "maxJourneys": 20})
        except LookupError as e:
            return self._json({"error": str(e), "nokey": True}, 200)
        except Exception as e:
            return self._json({"error": str(e)}, 502)
        deps = j.get("Departure") or []
        if isinstance(deps, dict):          # ResRobot ger ett objekt i stället för en lista när det bara finns en avgång
            deps = [deps]
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
                data = r.read(4 * 1024 * 1024)
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
