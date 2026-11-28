#!/bin/bash
# Förbereder Pi:n för att skicka ljud till Sonos med AirPlay. Själva AirPlay-utgången skapas av
# hubben bara medan ett klipp visas (hub/clipaudio.py), så här behövs bara verktygen.
set -e
command -v pactl >/dev/null || sudo apt-get install -y pulseaudio-utils
command -v pw-cli >/dev/null || sudo apt-get install -y pipewire-bin
# klippens ljud spelas numera av Sonos som en ljudström som Pi:n gör med ffmpeg
command -v ffmpeg >/dev/null || sudo apt-get install -y ffmpeg
command -v grim >/dev/null || sudo apt-get install -y grim        # skärmbilden i appen
CONF="$HOME/.config/pipewire/pipewire.conf.d/raop-discover.conf"
if [ -f "$CONF" ]; then
  # tidigare version: utgångarna låg uppe hela tiden, och kopplade inte upp igen efter vila
  rm -f "$CONF"
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  systemctl --user restart pipewire pipewire-pulse wireplumber || true
  sleep 3
fi
# Nest Hub som visningsskärm (hub/nestcast.py)
python3 -c "import pychromecast" 2>/dev/null || pip3 install --break-system-packages -q pychromecast
# Handkontrollen (hub/gamepad.py): läsa spelkontroller och para med Bluetooth
python3 -c "import evdev" 2>/dev/null || sudo apt-get install -y python3-evdev
HU=$(systemctl show -p User --value blaupunkt-hub 2>/dev/null); HU=${HU:-$USER}
id -nG "$HU" | grep -qw input || sudo usermod -aG input "$HU"
id -nG "$HU" | grep -qw bluetooth || sudo usermod -aG bluetooth "$HU" 2>/dev/null || true
# Bluetooth för trådlösa spelkontroller (Stadia m.fl.): Bluetooth ska aldrig vara spärrad, och
# BlueZ-inställningarna som rekommenderas när kontrollen paras men knapparna inte går att läsa.
sudo rfkill unblock bluetooth 2>/dev/null || true
BTC=/etc/bluetooth/main.conf
btset(){ # sektion nyckel värde
  if sudo grep -qE "^[[:space:]]*$2[[:space:]]*=" "$BTC"; then sudo sed -i -E "s|^[[:space:]]*$2[[:space:]]*=.*|$2 = $3|" "$BTC"
  else sudo sed -i "/^\[$1\]/a $2 = $3" "$BTC"; fi; }
if [ -f "$BTC" ] && ! grep -q "blaupunkt-spelkontroll" "$BTC"; then
  sudo cp "$BTC" "$BTC.fore-blaupunkt"
  btset General ControllerMode dual
  btset General JustWorksRepairing always
  btset General FastConnectable true
  btset LE MinConnectionInterval 7
  btset LE MaxConnectionInterval 9
  echo "# blaupunkt-spelkontroll: inställningar satta av airplay-setup.sh (original: main.conf.fore-blaupunkt)" | sudo tee -a "$BTC" >/dev/null
  sudo systemctl restart bluetooth
  echo "Bluetooth: inställt för spelkontroller"
fi
echo "Klippljud: klart (ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | cut -d" " -f3))"
