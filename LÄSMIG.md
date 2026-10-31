# Retrospelaren

Retro-uppspelning för Blaupunkt-möbeln: 4:3-bild med CRT-känsla, fem visualiseringar i 50/60-talsstil, och information om vad som spelas från webbradio eller Spotify.

## Starta på datorn

Kräver bara Python 3 (inget att installera).

- **Windows:** dubbelklicka `starta.bat`
- **Mac/Linux:** `./starta.sh` (eller `python3 server.py`)

Öppna sedan **http://127.0.0.1:8888** i Chrome. Använd `127.0.0.1`, inte `localhost` – Spotify godkänner bara den adressen.

Klicka på skärmen för att "vrida på", tryck **S** för inställningar och välj en genre under Radio.

## Kanaler

| Tangent | Kanal |
|---|---|
| 1 | Oscilloskop – fosforspår med efterlysning |
| 2 | Magiskt öga – avstämningsöga (EM34-stil), övre fältet vänster kanal, nedre höger |
| 3 | VU-mätare – två analoga mätare med nålballistik |
| 4 | Atomålder – strålkrans och elektronbanor |
| 5 | Ljusorgel – lampstaplar med topphållning |

Testbilden med klocka visas när inget spelar.

Övriga tangenter: **C** nästa kanal, **B** färgläge (svartvit, sepia, grön fosfor, 60-talsfärg), **Mellanslag** spela/paus, **→/N** nästa, **←/P** föregående, **I** dölj info, **F** helskärm, **S** inställningar.

## Radio

Stationer hämtas från radio-browser.info (50-tal, 60-tal, oldies, jazz, swing, rockabilly, doo-wop, svenska, egen sökning). Stjärnan sparar favoriter.

Strömmen går via den lokala servern, så visualiseringen läser det riktiga ljudet. Servern läser också låttiteln ur strömmen (där stationen skickar den) och hämtar omslag, album och år från iTunes sök-API.

HLS-strömmar (.m3u8) stöds inte och filtreras bort.

## Spotify

1. Gå till https://developer.spotify.com/dashboard och skapa en app.
2. Lägg till Redirect URI: `http://127.0.0.1:8888/callback`
3. Välj **Web API** och spara.
4. Kopiera **Client ID** till inställningarna i Retrospelaren och klicka **Logga in med Spotify**.

Spela sedan musik i Spotify på valfri enhet. Titel, artist, album, år, omslag och position i låten visas. Paus/nästa från Retrospelaren kräver Premium (Spotifys utvecklarläge kräver numera också Premium för appens ägare).

**Visualisering med Spotify:** Spotify ger inte ut själva ljudet till webbsidor. Välj därför:
- *Simulerad takt* – rörelse i jämn takt som följer play/paus, eller
- *Ljud in* – visualiseringen lyssnar på mikrofon eller linjeingång. På Pi:n kopplas detta till ALSA loopback så att bilden följer exakt det som spelas.

## Styrning utifrån (jalusin)

Servern tar emot enkla kommandon som sidan hämtar varje sekund:

```
curl "http://127.0.0.1:8888/api/send?c=open"      # tänd bild, starta musik
curl "http://127.0.0.1:8888/api/send?c=close"     # pausa, släck bild
curl "http://127.0.0.1:8888/api/send?c=next"      # också: prev, playpause, viz:3, palette:sepia
```

Reedbrytarens Python-skript på Pi:n behöver alltså bara anropa `open`/`close`.

## På Raspberry Pi (senare)

Kioskläge med autostart utan klick:

```
chromium --kiosk --autoplay-policy=no-user-gesture-required "http://127.0.0.1:8888/?autostart=1"
```

## Testversion i molnet (Railway)

Körs som Railway Function i projektet `retrospelaren`:
https://retrospelaren-production.up.railway.app

För Spotify där: lägg även till `https://retrospelaren-production.up.railway.app/callback` som Redirect URI.

`railway/server.ts` är molnversionen av `server.py` (Bun). `railway/bygg.sh` bygger `railway/function.ts` = servern med `index.html` inbäddad, vilket är filen som ligger på Railway.

## Filer

- `index.html` – hela spelaren (en fil)
- `server.py` – lokal server: statiska filer, radioproxy, låttitlar, omslag, kommandon
- `starta.bat` / `starta.sh` – startar servern och öppnar webbläsaren

Inställningar, favoriter och Spotify-inloggning sparas i webbläsarens lokala lagring.
