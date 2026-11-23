"""
Touch på Google Nest Hub, gratis: Home Assistants Cast-mottagare (app-id A078F6B0) har pekskärmen
påslagen på Nest Hub och laddar in egna moduler ("Lovelace-resurser") från den Home Assistant
den kopplar upp sig mot. Här låtsas servern vara en minimal Home Assistant:

  POST /auth/token        ger en åtkomstnyckel (mottagaren byter sin "refresh token" mot den)
  GET  /api/websocket     Home Assistants websocket-API, bara det mottagaren frågar efter
  GET  /nest-card.js      modulen: ett kort som fyller skärmen med appen (/nestapp)

Hubben på Pi:n startar castningen med pychromecasts HomeAssistantController (hub/nestcast.py).
Allt som visas är sådant som redan går att läsa och styra utan inloggning (/api/send med flera);
att ändra konfigurationen (larm, scener) kräver fortfarande inloggning i mobilen.
"""
import base64
import hashlib
import json
import secrets
import struct
import time

HA_VERSION = "2026.11.0"
REFRESH_TOKEN = "blaupunkt-nest"            # inte hemlig: inget här ger mer än det som redan är öppet
TOKENS = {}                                 # åtkomstnyckel -> när den slutar gälla
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def base_url(handler):
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "localhost"
    proto = handler.headers.get("X-Forwarded-Proto") or ("https" if "railway.app" in host else "http")
    return "%s://%s" % (proto.split(",")[0].strip(), host.split(",")[0].strip())


def cors(handler):
    handler.send_header("Access-Control-Allow-Origin", handler.headers.get("Origin") or "*")
    handler.send_header("Access-Control-Allow-Credentials", "true")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def options(handler):
    handler.send_response(204)
    cors(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def token(handler, body):
    """POST /auth/token: grant_type=refresh_token. Svarar som Home Assistant."""
    import re
    import urllib.parse
    text = body.decode("utf-8", "replace")
    if "form-data" in (handler.headers.get("Content-Type") or ""):
        # Home Assistants klient skickar FormData (multipart)
        q = {m.group(1): [m.group(2)] for m in re.finditer(r'name="([^"]+)"\r?\n\r?\n(.*?)\r?\n--', text, re.S)}
    else:
        q = urllib.parse.parse_qs(text)
    if (q.get("grant_type") or [""])[0] == "refresh_token" and (q.get("refresh_token") or [""])[0] == REFRESH_TOKEN:
        acc = secrets.token_urlsafe(32)
        now = time.time()
        for k in [k for k, t in TOKENS.items() if t < now]:
            del TOKENS[k]
        TOKENS[acc] = now + 86400
        out, status = {"access_token": acc, "token_type": "Bearer", "expires_in": 86400}, 200
    else:
        out, status = {"error": "invalid_grant"}, 400
    b = json.dumps(out).encode("utf-8")
    handler.send_response(status)
    cors(handler)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(b)))
    handler.end_headers()
    handler.wfile.write(b)


def card_js(handler):
    base = base_url(handler)
    js = r"""
// Blaupunkt på Nest Hub: ett Lovelace-kort som lägger appen över hela skärmen.
class BlaupunktNest extends HTMLElement {
  setConfig(c){ this._c = c || {}; }
  set hass(h){ if (this._done) return; this._done = true; this.render(); }
  getCardSize(){ return 12; }
  connectedCallback(){ if (!this._done){ this._done = true; this.render(); } }
  render(){
    const f = document.createElement("iframe");
    f.src = "__BASE__/nestapp";
    f.setAttribute("allow", "autoplay");
    f.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;border:0;z-index:2147483647;background:#0e0f11";
    document.body.appendChild(f);
    document.documentElement.style.background = "#0e0f11";
  }
}
if (!customElements.get("blaupunkt-nest")) customElements.define("blaupunkt-nest", BlaupunktNest);
""".replace("__BASE__", base)
    b = js.encode("utf-8")
    handler.send_response(200)
    cors(handler)
    handler.send_header("Content-Type", "application/javascript; charset=utf-8")
    handler.send_header("Content-Length", str(len(b)))
    handler.end_headers()
    handler.wfile.write(b)


# ---------- websocket ----------
def _send(sock, obj):
    data = json.dumps(obj).encode("utf-8")
    n = len(data)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    sock.sendall(head + data)


def _recv_exact(f, n):
    b = b""
    while len(b) < n:
        chunk = f.read(n - len(b))
        if not chunk:
            raise ConnectionError("stängd")
        b += chunk
    return b


def _recv(f, sock):
    """Ett helt textmeddelande (eller None när anslutningen stängs)."""
    buf = b""
    while True:
        b1, b2 = _recv_exact(f, 2)
        op, fin, masked, n = b1 & 0x0F, b1 & 0x80, b2 & 0x80, b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", _recv_exact(f, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", _recv_exact(f, 8))[0]
        mask = _recv_exact(f, 4) if masked else b"\0\0\0\0"
        payload = bytearray(_recv_exact(f, n))
        for i in range(n):
            payload[i] ^= mask[i % 4]
        if op == 0x8:
            return None
        if op == 0x9:
            sock.sendall(struct.pack("!BB", 0x8A, len(payload)) + bytes(payload))
            continue
        if op in (0x1, 0x0):
            buf += bytes(payload)
            if fin:
                return buf.decode("utf-8", "replace")


def _config(base):
    return {"latitude": 60.6, "longitude": 15.6, "elevation": 0, "unit_system": {"length": "km", "accumulated_precipitation": "mm",
            "mass": "g", "pressure": "Pa", "temperature": "°C", "volume": "L", "wind_speed": "m/s"}, "location_name": "Blaupunkt",
            "time_zone": "Europe/Stockholm", "components": ["lovelace", "frontend", "http", "websocket_api", "auth"],
            "config_dir": "/config", "allowlist_external_dirs": [], "allowlist_external_urls": [], "version": HA_VERSION,
            "config_source": "storage", "recovery_mode": False, "safe_mode": False, "state": "RUNNING", "external_url": base,
            "internal_url": None, "currency": "SEK", "country": "SE", "language": "sv", "radius": 100, "debug": False}


def _answer(t, msg, base):
    """Svaret på ett websocket-kommando: (result, events). Okänt: tomt men lyckat."""
    if t == "get_config":
        return _config(base), []
    if t == "get_states":
        return [], []
    if t == "get_services":
        return {}, []
    if t == "get_panels":
        return {"lovelace": {"component_name": "lovelace", "icon": None, "title": None, "config": {"mode": "storage"},
                             "url_path": "lovelace", "require_admin": False, "config_panel_domain": None}}, []
    if t == "auth/current_user":
        return {"id": "blaupunkt", "name": "Blaupunkt", "is_owner": False, "is_admin": False, "credentials": [], "mfa_modules": []}, []
    if t == "frontend/get_themes":
        return {"themes": {}, "default_theme": "default", "default_dark_theme": None}, []
    if t == "frontend/get_translations":
        return {"resources": {}}, []
    if t in ("frontend/subscribe_user_data", "frontend/subscribe_system_data", "frontend/get_user_data", "frontend/get_system_data"):
        return None, [{"value": None}]
    if t == "lovelace/config":
        return {"title": "Blaupunkt", "views": [{"title": "Blaupunkt", "path": "blaupunkt", "panel": True,
                                                  "cards": [{"type": "custom:blaupunkt-nest"}]}]}, []
    if t == "lovelace/resources":
        return [{"id": "blaupunkt", "type": "module", "url": base + "/nest-card.js?v=%d" % int(time.time() // 3600)}], []
    if t == "lovelace/info":
        return {"mode": "storage", "resource_mode": "storage", "resource_count": 1}, []
    if t == "subscribe_entities":
        return None, [{"a": {}}]
    if t == "config/entity_registry/list_for_display":
        return {"entity_categories": {}, "entities": []}, []
    if t.endswith("/list"):
        return [], []
    if t == "brands/access_token":
        return {"token": "blaupunkt"}, []
    return None, []


def session(handler):
    """GET /api/websocket med Upgrade: websocket."""
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or "websocket" not in (handler.headers.get("Upgrade") or "").lower():
        handler.send_error(400)
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    sock, f = handler.connection, handler.rfile
    sock.settimeout(120)
    base = base_url(handler)
    try:
        _send(sock, {"type": "auth_required", "ha_version": HA_VERSION})
        authed = False
        while True:
            raw = _recv(f, sock)
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                t = str(m.get("type") or "")
                if not authed:
                    if t == "auth" and TOKENS.get(m.get("access_token"), 0) > time.time():
                        authed = True
                        _send(sock, {"type": "auth_ok", "ha_version": HA_VERSION})
                    else:
                        _send(sock, {"type": "auth_invalid", "message": "Invalid access token"})
                        return
                    continue
                mid = m.get("id")
                if t == "ping":
                    _send(sock, {"id": mid, "type": "pong"})
                    continue
                result, events = _answer(t, m, base)
                _send(sock, {"id": mid, "type": "result", "success": True, "result": result})
                for ev in events:
                    _send(sock, {"id": mid, "type": "event", "event": ev})
    except (ConnectionError, OSError, ValueError):
        pass
    handler.close_connection = True
