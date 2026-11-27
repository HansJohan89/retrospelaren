#!/usr/bin/env python3
"""
Letar upp jalusisensorn (Shelly BLU Door/Window). Lyssnar i 30 sekunder efter
BTHome-sändare och skriver ut adress, namn och avlästa värden. Öppna och stäng
jalusin medan den lyssnar, så ser du vilken som är din: fältet "window" växlar
mellan True (öppen) och False (stängd). Adressen ska in i hub_config.json
under jalusi.mac.

    python3 scan_ble.py            (eller: python3 scan_ble.py 60)
"""
import asyncio
import sys

from bleak import BleakScanner
import bthome


async def main(seconds):
    last = {}

    def handle(device, adv):
        data = (adv.service_data or {}).get(bthome.BTHOME_UUID)
        vals = bthome.parse(data) if data else None
        if vals is None:
            return
        shown = {k: v for k, v in vals.items() if k != "packet_id"}
        if last.get(device.address) == shown:
            return
        last[device.address] = shown
        print("%s  %-14s  signal %s dBm  %s" % (device.address, adv.local_name or device.name or "(inget namn)", adv.rssi, shown))

    scanner = BleakScanner(detection_callback=handle)
    print("Lyssnar i %d sekunder. Öppna och stäng jalusin nu." % seconds)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    if not last:
        print("\nIngen BTHome-sändare hördes. Kontrollera att Bluetooth är på (bluetoothctl show), att sensorn har\n"
              "batteri och sitter inom några meter, och tryck på sensorns knapp en gång så att den sänder.")


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
