// Retrospelaren – molnversion för Railway (Bun). Samma API som server.py.
// index.html ligger inbäddad i kommentaren längst ner i den här filen (bygg.sh lägger dit den).
const SELF = await Bun.file(import.meta.path).text();
const MARK = "INDEX" + "_HTML";
const HTML = SELF.slice(SELF.indexOf("/* " + MARK + "\n") + MARK.length + 4, SELF.lastIndexOf("\n" + MARK + " */"));

const UA = "Retrospelaren/1.0";
const RADIO_MIRRORS = [
  "https://de1.api.radio-browser.info",
  "https://fi1.api.radio-browser.info",
  "https://nl1.api.radio-browser.info",
  "https://at1.api.radio-browser.info",
];
const CORS = { "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store" };
const cmd = { id: 0, list: [] as { id: number; c: string }[] };

const json = (obj: unknown, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" } });
const okUrl = (u: string) => /^https?:\/\//i.test(u);

async function timed(url: string, init: RequestInit = {}, ms = 10000) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: ac.signal, headers: { "User-Agent": UA, ...(init.headers || {}) } });
  } finally { clearTimeout(t); }
}

async function stream(url: string, req: Request, depth = 0): Promise<Response> {
  if (!okUrl(url) || depth > 2) return json({ error: "ogiltig url" }, 400);
  let up: Response;
  try {
    up = await fetch(url, { signal: req.signal, headers: { "User-Agent": UA } });
  } catch (e) { return json({ error: String(e) }, 502); }
  if (!up.ok) return json({ error: `uppströms ${up.status}` }, 502);
  const ctype = (up.headers.get("content-type") || "audio/mpeg").toLowerCase();
  if (ctype.includes("mpegurl") || ctype.includes("x-scpls") || /\.(m3u|pls|m3u8)(\?|$)/i.test(url)) {
    const text = await up.text();
    for (let line of text.split(/\r?\n/)) {
      line = line.trim();
      if (/^file\d*=/i.test(line)) line = line.split("=").slice(1).join("=").trim();
      if (okUrl(line) && !/\.m3u8(\?|$)/i.test(line)) return stream(line, req, depth + 1);
    }
    return json({ error: "spellistan saknar ljudlänk (HLS stöds inte)" }, 415);
  }
  if (!/^(audio\/|application\/ogg|application\/octet-stream|video\/mp2t)/.test(ctype)) {
    try { await up.body?.cancel(); } catch {}
    return json({ error: `inte en ljudström (${ctype})` }, 415);
  }
  return new Response(up.body, { headers: { ...CORS, "Content-Type": ctype } });
}

async function icy(url: string) {
  if (!okUrl(url)) return json({ error: "ogiltig url" }, 400);
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 8000);
  const info: Record<string, string | null> = { name: null, genre: null, title: null };
  try {
    const r = await fetch(url, { signal: ac.signal, headers: { "User-Agent": UA, "Icy-MetaData": "1" } });
    info.name = r.headers.get("icy-name"); info.genre = r.headers.get("icy-genre");
    const metaint = parseInt(r.headers.get("icy-metaint") || "0", 10);
    if (!metaint || !r.body) { ac.abort(); return json(info); }
    const reader = r.body.getReader();
    let buf = new Uint8Array(0);
    const need = async (n: number) => {
      while (buf.length < n) {
        const { value, done } = await reader.read();
        if (done) return false;
        const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
      }
      return true;
    };
    for (let block = 0; block < 2; block++) {
      if (!(await need(metaint + 1))) break;
      const len = buf[metaint] * 16;
      if (len === 0) { buf = buf.slice(metaint + 1); continue; }
      if (!(await need(metaint + 1 + len))) break;
      const raw = buf.slice(metaint + 1, metaint + 1 + len);
      let text: string;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(raw); } catch { text = new TextDecoder("latin1").decode(raw); }
      text = text.replace(/\0+$/, "");
      const m = text.match(/StreamTitle='(.*?)';/s) || text.match(/StreamTitle='(.*)/s);
      if (m) info.title = m[1].trim() || null;
      break;
    }
    try { await reader.cancel(); } catch {}
  } catch (e) {
    info.error = String(e);
  } finally { clearTimeout(t); ac.abort(); }
  return json(info);
}

async function radio(q: URLSearchParams) {
  const p = new URLSearchParams({ hidebroken: "true", order: "clickcount", reverse: "true", limit: q.get("limit") || "40" });
  for (const k of ["tag", "name", "country", "countrycode", "tagList"]) if (q.get(k)) p.set(k, q.get(k)!);
  let last = "";
  for (const base of RADIO_MIRRORS) {
    try {
      const r = await timed(`${base}/json/stations/search?${p}`, {}, 8000);
      if (!r.ok) { last = `HTTP ${r.status}`; continue; }
      const data = (await r.json()) as any[];
      const seen = new Set<string>(); const out = [];
      for (const s of data) {
        const u = s.url_resolved || s.url;
        if (!u || seen.has(u) || /\.m3u8(\?|$)/i.test(u) || s.hls === 1) continue;
        seen.add(u);
        out.push({ id: s.stationuuid, name: (s.name || "").trim(), url: u, favicon: s.favicon || "", tags: s.tags || "",
          country: s.countrycode || "", codec: s.codec || "", bitrate: s.bitrate || 0 });
      }
      return json(out);
    } catch (e) { last = String(e); }
  }
  return json({ error: `radio-browser svarar inte: ${last}` }, 502);
}

async function cover(term: string) {
  term = term.trim(); if (!term) return json({});
  const p = new URLSearchParams({ term, entity: "song", limit: "5", country: "SE" });
  try {
    const r = await timed(`https://itunes.apple.com/search?${p}`, {}, 8000);
    const res = ((await r.json()) as any).results || [];
    const STOP = new Set(["the", "and", "och", "feat", "ft", "with", "med", "de", "la", "le"]);
    const words = (x: string) => (x || "").toLowerCase().replace(/\(.*?\)|\[.*?\]/g, " ").split(/[^a-z0-9åäöéü]+/).filter(w => w.length > 1 && !STOP.has(w));
    const tw = new Set(words(term));
    const hit = (x: string) => { const w = words(x); return w.length > 0 && w.filter(v => tw.has(v)).length / w.length >= 0.6; };
    const s = res.find((r: any) => hit(r.artistName) && hit(r.trackName));
    if (!s) return json({});
    return json({ cover: (s.artworkUrl100 || "").replace("100x100bb", "600x600bb"), album: s.collectionName || "",
      year: (s.releaseDate || "").slice(0, 4), artist: s.artistName || "", title: s.trackName || "" });
  } catch (e) { return json({ error: String(e) }); }
}

async function img(url: string) {
  if (!okUrl(url)) return json({ error: "ogiltig url" }, 400);
  try {
    const r = await timed(url, {}, 8000);
    const ctype = r.headers.get("content-type") || "";
    if (!ctype.startsWith("image/")) return json({ error: "inte en bild" }, 415);
    const body = await r.arrayBuffer();
    if (body.byteLength > 4 * 1024 * 1024) return json({ error: "för stor" }, 413);
    return new Response(body, { headers: { ...CORS, "Content-Type": ctype, "Cache-Control": "public, max-age=86400" } });
  } catch (e) { return json({ error: String(e) }, 502); }
}

Bun.serve({
  port: Number(Bun.env.PORT || 8888),
  hostname: "0.0.0.0",
  idleTimeout: 0, // radioströmmar är oändliga
  async fetch(req) {
    const u = new URL(req.url); const q = u.searchParams;
    switch (u.pathname) {
      case "/": case "/index.html": case "/callback":
        return new Response(HTML, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
      case "/api/ping": return json({ ok: true });
      case "/api/stream": return stream(q.get("url") || "", req);
      case "/api/icy": return icy(q.get("url") || "");
      case "/api/radio": return radio(q);
      case "/api/cover": return cover(q.get("term") || "");
      case "/api/img": return img(q.get("url") || "");
      case "/api/send": {
        const c = (q.get("c") || "").trim(); if (!c) return json({ error: "ange c=" }, 400);
        cmd.id++; cmd.list.push({ id: cmd.id, c }); cmd.list = cmd.list.slice(-20);
        return json({ ok: true, id: cmd.id });
      }
      case "/api/cmd": {
        const since = parseInt(q.get("since") || "0", 10) || 0;
        return json({ last: cmd.id, items: cmd.list.filter(x => x.id > since) });
      }
    }
    return new Response("Hittades inte", { status: 404 });
  },
});
console.log("Retrospelaren lyssnar på port", Bun.env.PORT || 8888);
