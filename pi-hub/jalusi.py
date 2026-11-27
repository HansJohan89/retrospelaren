"""
Jalusins sensor: Shelly BLU Door/Window. Sensorn ropar ut sitt läge i BTHome-format
över Bluetooth; Pi:n lyssnar bara, ingen parning och ingen gateway behövs.

Sensorn skickar normalt fältet "window" (sant = öppet). Vissa versioner använder
"opening" eller "door"; det första av dem som finns i meddelandet används.
"""
import asyncio
import json
import os
import threading
import time

DOOR_KEYS = ("window", "opening", "door")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jalusi_state.json")


class Jalusi:
    def __init__(self, cfg, log, on_change):
        self.cfg = cfg or {}
        self.log = log
        self.on_change = on_change            # on_change(True) = öppnad, False = stängd
        self.mac = (self.cfg.get("mac") or "").strip().upper()
        self.debounce_s = float(self.cfg.get("debounce_s", 3))
        self.state = None
        self.changed_at = 0.0
        self.battery = None

    @property
    def enabled(self):
        return bool(self.mac)

    def start(self):
        if not self.enabled:
            self.log("Jalusisensorn är inte inställd (jalusi.mac i hub_config.json). Hoppar över den.")
            return
        threading.Thread(target=self._thread, name="jalusi", daemon=True).start()

    def _thread(self):
        while True:
            try:
                asyncio.run(self._scan())
            except Exception as e:
                self.log("(varning: Bluetooth-lyssningen avbröts: %s. Försöker igen om 15 s.)" % e)
            time.sleep(15)

    async def _scan(self):
        from bleak import BleakScanner
        import bthome

        def handle(device, adv):
            if device.address.upper() != self.mac:
                return
            data = (adv.service_data or {}).get(bthome.BTHOME_UUID)
            vals = bthome.parse(data) if data else None
            if not vals:
                return
            if vals.get("encrypted"):
                if not getattr(self, "_enc_warned", False):
                    self._enc_warned = True
                    self.log("(varning: sensorn sänder krypterat. Slå av kryptering för den i Shelly-appen.)")
                return
            if "battery" in vals:
                self.battery = vals["battery"]
            for name in DOOR_KEYS:
                if name in vals:
                    self.feed(bool(vals[name]))
                    break

        scanner = BleakScanner(detection_callback=handle)
        await scanner.start()
        self.log("Lyssnar efter jalusisensorn %s." % self.mac)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await scanner.stop()

    def feed(self, is_open):
        """Ett avläst läge. Sensorn sänder bara när något händer, så senast kända läge sparas
        på disk: då räknas även den första sändningen efter en omstart som en ändring om
        jalusin faktiskt har bytt läge."""
        now = time.monotonic()
        if self.state is None:
            self.state = self._load()
            if self.state is None:
                self.state = is_open
                self._save()
                self.log("Jalusin är %s." % ("öppen" if is_open else "stängd"))
                return
        if is_open == self.state or now - self.changed_at < self.debounce_s:
            return
        self.state = is_open
        self.changed_at = now
        self._save()
        self.log("Jalusin %s." % ("öppnades" if is_open else "stängdes"))
        try:
            self.on_change(is_open)
        except Exception as e:
            self.log("(varning: jalusihändelsen misslyckades: %s)" % e)

    def _load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return bool(json.load(f)["open"])
        except Exception:
            return None

    def _save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"open": self.state}, f)
        except Exception:
            pass
