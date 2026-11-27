"""
Appens egna tjänster på servern (appen = app.html, som Tavlan-appen):

  Spotify   Du loggar in en gång i appen; servern sparar inloggningen (DATA_DIR/spotify-app.json)
            och hämtar spellistor, gillade låtar, sök, album, artister och senast spelat åt appen.
            Själva uppspelningen sker på Sonos (hubben spelar spotify:-länkarna med SoCo).
              POST /api/sp/connect  {code, verifier, redirect}   (inloggad)
              POST /api/sp/logout                                   (inloggad)
              POST /api/sp/like     {id, on}                        (inloggad)
              GET  /api/sp/status
              GET  /api/sp/api?path=me/playlists&limit=50 …         (bara läsning, vissa adresser)
  Radio     radio-browser.info:  GET /api/radio?q=…  |  GET /api/radio?top=SE
  Skärmbild bilden av Blaupunkt-skärmen som hubben tar med grim:
              POST /api/screenshot (image/jpeg)  |  GET /api/screenshot  |  GET /api/screenshot?meta=1
  Rum       hubben skickar vad som spelas i rummet appen tittar på:
              POST /api/sonos-rooms {rum: {...}}  |  GET /api/sonos-rooms
  Ringsignal GET /api/chime.wav (timern i appen ringer med den på Sonos)
"""
import base64
import json
import math
import os
import re
import struct
import threading
import time
import urllib.parse
import urllib.request

UA = "Blaupunkt/1.0 (retrospelaren)"
LOCK = threading.Lock()
SHOT = {"jpg": b"", "at": 0.0}
ROOMS = {}
CACHE = {}
_CHIME = None

# läsadresser i Spotifys webb-API som appen får fråga efter
SP_READ = re.compile(r"^(me|me/playlists|me/tracks|me/tracks/contains|me/player/recently-played|me/top/(artists|tracks)|me/albums|"
                     r"search|playlists/[A-Za-z0-9]+(/tracks)?|albums/[A-Za-z0-9]+(/tracks)?|artists/[A-Za-z0-9]+(/top-tracks|/albums)?)$")


class Spotify:
    def __init__(self, data_dir, client_id):
        self.fp = os.path.join(data_dir, "spotify-app.json")
        self.client_id = client_id          # funktion: Client ID från inställningarna (samma som skärmen)
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.fp, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        os.makedirs(os.path.dirname(self.fp), exist_ok=True)
        tmp = self.fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, self.fp)

    def _token_req(self, form):
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urllib.parse.urlencode(form).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8"))

    def connect(self, code, verifier, redirect, client_id=None):
        cid = client_id or self.client_id()
        if not cid:
            raise ValueError("Ingen Spotify Client ID. Lägg in den under Inställningar > Musik.")
        j = self._token_req({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                             "client_id": cid, "code_verifier": verifier})
        d = {"client_id": cid, "refresh": j.get("refresh_token"), "access": j.get("access_token"),
             "exp": time.time() + int(j.get("expires_in") or 3600) - 60, "scope": j.get("scope", "")}
        with self.lock:
            self._save(d)
        try:
            me = self.api("me")
            d["name"] = me.get("display_name") or me.get("id") or ""
            d["product"] = me.get("product") or ""
            with self.lock:
                self._save(d)
        except Exception:
            pass
        CACHE.clear()
        return {"connected": True, "name": d.get("name", "")}

    def logout(self):
        with self.lock:
            try:
                os.remove(self.fp)
            except OSError:
                pass
        CACHE.clear()

    def status(self):
        d = self._load()
        return {"connected": bool(d.get("refresh")), "name": d.get("name", ""), "product": d.get("product", ""),
                "clientId": bool(self.client_id())}

    def _access(self):
        with self.lock:
            d = self._load()
            if not d.get("refresh"):
                raise PermissionError("Inte inloggad på Spotify")
            if d.get("access") and d.get("exp", 0) > time.time():
                return d["access"]
            j = self._token_req({"grant_type": "refresh_token", "refresh_token": d["refresh"], "client_id": d.get("client_id") or self.client_id()})
            d["access"] = j.get("access_token")
            d["exp"] = time.time() + int(j.get("expires_in") or 3600) - 60
            if j.get("refresh_token"):
                d["refresh"] = j["refresh_token"]
            self._save(d)
            return d["access"]

    def api(self, path, query=None, method="GET", body=None):
        url = "https://api.spotify.com/v1/" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self._access(), "Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def read(self, path, query):
        path = path.strip("/")
        if not SP_READ.match(path):
            raise ValueError("Den adressen får appen inte läsa")
        key = path + "?" + urllib.parse.urlencode(sorted(query.items()))
        hit = CACHE.get(key)
        ttl = 10 if path.startswith("me/tracks/contains") or "recently" in path else 60
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        d = self.api(path, query)
        CACHE[key] = (time.time(), d)
        if len(CACHE) > 300:
            for k in sorted(CACHE, key=lambda k: CACHE[k][0])[:100]:
                CACHE.pop(k, None)
        return d

    def like(self, tid, on):
        self.api("me/tracks", {"ids": tid}, method="PUT" if on else "DELETE")
        for k in [k for k in CACHE if k.startswith("me/tracks")]:
            CACHE.pop(k, None)
        return {"ok": True, "liked": bool(on)}


# ---------- radio (radio-browser.info) ----------
def radio(q=None, top=None):
    key = "radio:%s:%s" % (q, top)
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    params = {"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": "60"}
    if q:
        params["name"] = q
        params["limit"] = "40"
    else:
        params["countrycode"] = (top or "SE").upper()
    last = None
    for host in ("de1.api.radio-browser.info", "de2.api.radio-browser.info", "fi1.api.radio-browser.info", "all.api.radio-browser.info"):
        try:
            req = urllib.request.Request("https://%s/json/stations/search?%s" % (host, urllib.parse.urlencode(params)), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                rows = json.loads(r.read().decode("utf-8"))
            out = []
            seen = set()
            for s in rows:
                url = s.get("url_resolved") or s.get("url")
                name = (s.get("name") or "").strip()
                if not url or not name or name.lower() in seen:
                    continue
                if str(s.get("codec", "")).upper() not in ("MP3", "AAC", "AAC+", "OGG", ""):
                    continue
                seen.add(name.lower())
                out.append({"name": name, "url": url, "icon": s.get("favicon") or "", "tags": (s.get("tags") or "")[:60],
                            "country": s.get("countrycode") or "", "codec": s.get("codec") or "", "bitrate": s.get("bitrate") or 0})
            CACHE[key] = (time.time(), out)
            return out
        except Exception as e:
            last = e
    raise RuntimeError("radio-browser svarar inte: %s" % last)


# ---------- skärmbilden ----------
def shot_put(data):
    if not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG"):
        raise ValueError("ingen bild")
    with LOCK:
        SHOT["jpg"], SHOT["at"] = data, time.time()


def shot_get():
    with LOCK:
        return SHOT["jpg"], SHOT["at"]


# ---------- rummen ----------
def rooms_put(d):
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict) and len(ROOMS) < 40:
                v["at"] = time.time()
                ROOMS[str(k)] = v


def rooms_get():
    now = time.time()
    return {k: v for k, v in ROOMS.items() if now - v.get("at", 0) < 30}


# ---------- ringsignal ----------
def chime():
    """En mjuk klocka i tre slag (wav), till timern och larmen."""
    global _CHIME
    if _CHIME:
        return _CHIME
    sr, out = 22050, []
    notes = [(0.0, 880.0), (0.45, 1108.7), (0.9, 1318.5), (2.2, 880.0), (2.65, 1108.7), (3.1, 1318.5)]
    n = int(sr * 4.6)
    buf = [0.0] * n
    for t0, f in notes:
        s0 = int(t0 * sr)
        for i in range(int(sr * 1.4)):
            j = s0 + i
            if j >= n:
                break
            t = i / sr
            env = math.exp(-t * 3.2) * min(1.0, t * 200)
            buf[j] += 0.32 * env * (math.sin(2 * math.pi * f * t) + 0.35 * math.sin(2 * math.pi * f * 2.01 * t) + 0.12 * math.sin(2 * math.pi * f * 3.0 * t))
    for v in buf:
        out.append(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 30000)))
    data = b"".join(out)
    head = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", len(data))
    _CHIME = head + data
    return _CHIME


def b64(s):
    return base64.urlsafe_b64encode(s).decode().rstrip("=")
