"""Filmtips från Plex: filmer som finns i biblioteket men som inte är sedda.

Plex-servern finns på hemnätet och kan inte nås från internet, så hubben hämtar listan och
omslagen och lägger upp dem på servern, där Blaupunkt-sidan visar dem (sidan "Filmtips").

hub_config.json:
  "plex": { "url": "http://192.168.1.x:32400", "token": "...", "limit": 40 }
Token: logga in i Plex på webben, öppna en film, "Get Info" > "View XML": adressen som öppnas
slutar med X-Plex-Token=... .
"""
import base64
import json
import random
import time
import urllib.parse
import urllib.request


class Plex:
    def __init__(self, cfg, log, upload_img, post_list):
        self.cfg = cfg or {}
        self.log = log
        self.upload_img = upload_img          # (base64, typ) -> adress på servern
        self.post_list = post_list            # (lista) -> None
        self.url = (self.cfg.get("url") or "").rstrip("/")
        self.token = self.cfg.get("token") or ""
        self.enabled = bool(self.url and self.token)
        self.img_cache = {}                   # plex-bild -> adress på servern

    def _get(self, path, params=None, accept="application/json", timeout=15):
        q = dict(params or {}, **{"X-Plex-Token": self.token})
        req = urllib.request.Request(self.url + path + ("&" if "?" in path else "?") + urllib.parse.urlencode(q),
                                     headers={"Accept": accept, "X-Plex-Product": "Blaupunkt", "X-Plex-Client-Identifier": "blaupunkt-hub"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), (r.headers.get("Content-Type") or "").split(";")[0].strip()

    def _json(self, path, params=None):
        return json.loads(self._get(path, params)[0].decode("utf-8")).get("MediaContainer") or {}

    def _poster(self, thumb, w=400, h=600):
        if not thumb:
            return ""
        if thumb in self.img_cache:
            return self.img_cache[thumb]
        try:
            data, ctype = self._get("/photo/:/transcode", {"url": thumb, "width": w, "height": h, "minSize": 1, "upscale": 1}, accept="image/*")
            url = self.upload_img(base64.b64encode(data).decode("ascii"), ctype if ctype.startswith("image/") else "image/jpeg") or ""
        except Exception as e:
            self.log("  (Plex: kunde inte hämta omslag: %s)" % e)
            return ""
        if url:
            self.img_cache[thumb] = url
        return url

    def movies(self):
        """Osedda filmer ur alla filmbibliotek, nyast tillagda först."""
        out = []
        for sec in self._json("/library/sections").get("Directory") or []:
            if sec.get("type") != "movie":
                continue
            mc = self._json("/library/sections/%s/all" % sec.get("key"),
                            {"type": 1, "unwatched": 1, "sort": "addedAt:desc", "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 200})
            for m in mc.get("Metadata") or []:
                if m.get("viewCount") or m.get("viewOffset"):
                    continue                 # sedd eller påbörjad
                out.append(m)
        out.sort(key=lambda m: m.get("addedAt") or 0, reverse=True)
        return out

    def refresh(self):
        if not self.enabled:
            return
        try:
            all_m = self.movies()
        except Exception as e:
            self.log("(varning: Plex svarar inte: %s)" % e)
            return
        limit = int(self.cfg.get("limit", 40))
        # hälften av de senast tillagda, resten slumpade ur hela listan, så att äldre filmer också föreslås
        new = all_m[:limit // 2]
        rest = all_m[limit // 2:]
        pick = new + random.sample(rest, min(len(rest), limit - len(new)))
        items = []
        for m in pick:
            items.append({
                "title": m.get("title") or "", "year": m.get("year"), "summary": m.get("summary") or "",
                "minutes": round((m.get("duration") or 0) / 60000),
                "rating": m.get("audienceRating") or m.get("rating"), "content": m.get("contentRating") or "",
                "genres": [g.get("tag") for g in (m.get("Genre") or [])][:3],
                "director": ", ".join(d.get("tag") for d in (m.get("Director") or [])[:1]),
                "added": m.get("addedAt"), "poster": self._poster(m.get("thumb")),
                "art": self._poster(m.get("art"), 960, 540) if self.cfg.get("backdrops", True) else "",
            })
        self.post_list({"items": items, "total": len(all_m), "at": time.time()})
        self.log("Plex: %d osedda filmer, %d skickade som filmtips." % (len(all_m), len(items)))

    def check_images(self, url_ok):
        """Servern glömmer bilderna när den startas om: töm cachen så att de laddas upp igen."""
        any_url = next(iter(self.img_cache.values()), None)
        if any_url and not url_ok(any_url):
            self.img_cache.clear()
            return False
        return True
