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
  /api/send?c=      skicka kommando till sidan: open, close, next, prev,
                    playpause, viz:1-5, palette:svartvit|sepia|fosfor|farg

Bara Pythons standardbibliotek. Inget att installera.
"""
import argparse
import json
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
            if path == "/api/send":
                return self.send_cmd(q.get("c", [""])[0])
            if path == "/api/cmd":
                return self.poll_cmd(int(q.get("since", ["0"])[0] or 0))
            if path == "/api/ping":
                return self._json({"ok": True})
            return super().do_GET()
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
        qs = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 1, "country": "SE"})
        try:
            with open_url(f"https://itunes.apple.com/search?{qs}", timeout=8) as r:
                res = json.loads(r.read().decode("utf-8")).get("results") or []
        except Exception as e:
            return self._json({"error": str(e)})
        if not res:
            return self._json({})
        s = res[0]
        art = (s.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        return self._json({
            "cover": art,
            "album": s.get("collectionName") or "",
            "year": (s.get("releaseDate") or "")[:4],
            "artist": s.get("artistName") or "",
            "title": s.get("trackName") or "",
        })

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
