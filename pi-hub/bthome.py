"""
Liten tolk för BTHome v2 (det öppna Bluetooth-format som Shelly BLU-sensorerna ropar
ut sina värden i). Räcker för dörr-/fönstersensorn och drar inte in några stora
beroenden: bara objektens längder behövs för att kunna stega förbi dem vi inte bryr
oss om. Formatet: https://bthome.io/format/

    parse(service_data_bytes) -> {"window": True, "battery": 100, "illuminance": 12.5, ...} eller None
"""
BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"

# objekt-id -> antal databytes (BTHome v2). 0x53 text och 0x54 rådata har en längdbyte först.
_LEN = {0: 1, 1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 2, 7: 2, 8: 2, 9: 1, 10: 3, 11: 3, 12: 2, 13: 2, 14: 2, 15: 1, 16: 1, 17: 1, 18: 2, 19: 2,
        20: 2, 21: 1, 22: 1, 23: 1, 24: 1, 25: 1, 26: 1, 27: 1, 28: 1, 29: 1, 30: 1, 31: 1, 32: 1, 33: 1, 34: 1, 35: 1, 36: 1, 37: 1, 38: 1,
        39: 1, 40: 1, 41: 1, 42: 1, 43: 1, 44: 1, 45: 1, 46: 1, 47: 1, 58: 1, 59: 1, 60: 2, 61: 2, 62: 4, 63: 2, 64: 2, 65: 2, 66: 3, 67: 2,
        68: 2, 69: 2, 70: 1, 71: 2, 72: 2, 73: 2, 74: 2, 75: 3, 76: 4, 77: 4, 78: 4, 79: 4, 80: 4, 81: 2, 82: 2, 85: 4, 86: 2, 87: 1, 88: 1,
        89: 1, 90: 2, 91: 4, 92: 4, 93: 2, 94: 2, 95: 2, 96: 1, 97: 2, 98: 4, 99: 4, 100: 1, 101: 1, 240: 2, 241: 4, 242: 3}
_VARIABLE = (0x53, 0x54)

# de värden vi faktiskt läser ut: id -> (namn, tecken, faktor) ; None-faktor = sant/falskt
_KNOWN = {0x00: ("packet_id", False, 1), 0x01: ("battery", False, 1), 0x05: ("illuminance", False, 0.01),
          0x11: ("opening", False, None), 0x1A: ("door", False, None), 0x2D: ("window", False, None),
          0x3A: ("button", False, 1), 0x3F: ("rotation", True, 0.1), 0x45: ("temperature", True, 0.1)}


def parse(data):
    if not data or len(data) < 2:
        return None
    info = data[0]
    if (info >> 5) & 7 != 2:
        return None                      # inte BTHome v2
    if info & 1:
        return {"encrypted": True}       # krypterad sändning: slå av kryptering i Shelly-appen
    out, i, n = {}, 1, len(data)
    while i < n:
        oid = data[i]
        if oid in _VARIABLE:
            if i + 1 >= n:
                break
            i += 2 + data[i + 1]
            continue
        ln = _LEN.get(oid)
        if ln is None or i + 1 + ln > n:
            break
        raw = data[i + 1:i + 1 + ln]
        if oid in _KNOWN:
            name, signed, factor = _KNOWN[oid]
            val = int.from_bytes(raw, "little", signed=signed)
            out[name] = bool(val) if factor is None else (val if factor == 1 else round(val * factor, 2))
        i += 1 + ln
    return out
