# Retrospelaren (BLAUPUNKT)

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

## Bildrör och störningar

Under **S → Bildrör** finns förinställningarna *Ren bild*, *Ny TV 1962*, *Sliten* och *Dålig mottagning*, och sedan reglage för allt:

- **Bild:** ljusstyrka, kontrast, mjukhet, efterglöd (fosfor), välvning, vinjett, glasreflex, linjer
- **Störningar:** flimmer, snö, brumband, skakning, vågiga linjer, bildrullning, tillfälliga störningar (korta skurar av snö, revor, brum eller rullning), spökbild, färgfransar
- Uppvärmning när bilden startar och avstängning till en punkt vid `close`

På Pi:n är *Vågiga linjer*, *Spökbild* och *Färgfransar* de tyngsta effekterna; sänk dem om bilden hackar.

## Utseende, skärm och kanter

- **Utseende:** märkning (standard BLAUPUNKT), rubrik- och brödtypsnitt, textstorlek, versaler, omslag som fyrkant, snurrande vinylskiva eller dolt
- **Skärm:** 4:3, 16:9 eller fyll, samt storlek och förskjutning för att passa bilden bakom masken
- **Kanter:** veckodag och datum, veckonummer, klocka och väder (Open-Meteo, standard Falun, valfri ort)

## När inget spelar

I stället för bara testbilden växlar skärmen mellan sidor (S → När inget spelar):

- **Väder och bussar:** nuläge, känns som, vind och byar, luftfuktighet, lufttryck, sol upp/ned, prognos varannan timme och fem dagar framåt. Bredvid vädret visas nästa timmes avgångar från vald hållplats, med realtid, förseningar och inställda turer.
- **Nyheter:** en artikel i taget med rubrik, ingress och bild, från SVT Nyheter, SVT Dalarna, Ekot eller valfritt RSS-flöde. Saknar flödet bild hämtas artikelns delningsbild. En stapel visar när nästa artikel kommer.
- **Foton:** bilder ur ett eller flera publikt delade Google Foto-album, i helskärm med långsam inzoomning och mjuk övertoning. Visas i bildrörets färgläge eller i originalfärg.
- **Fotboll:** AIK i Allsvenskan och Bayern München i Bundesliga, varannan gång: placering, nästa match, de tre senaste resultaten och hela tabellen.
- **Testbild**

Välj vilka sidor som ingår, sekunder per sida, per artikel och per foto.

### Foton från Google Foto

Samma metod som e-ink-tavlan: Googles API för det här är stängt sedan mars 2025, så servern läser bildadresserna ur albumets publika sida. Ingen nyckel behövs.

1. Öppna albumet i Google Foto, tryck Dela och välj **Skapa länk** (inte "Bjud in personer").
2. Klistra in länken under S → När inget spelar, en per rad om du har flera album.

Albumen läses om en gång i timmen, så nya bilder dyker upp av sig själva.

### Fotboll (Highlightly)

Samma källa som e-ink-tavlan: Highlightly, med nyckeln `HIGHLIGHTLY_API_KEY` (på Railway som variabel, lokalt i filen `highlightly.key`). Gratisnivån ger 100 anrop per dygn och delas med e-ink-tavlan, som använder ungefär 58. Den här servern hämtar därför högst var sjätte timme (24 anrop per dygn).

### Bussavgångar (ResRobot)

Avgångarna hämtas från Trafiklabs ResRobot v2.1, samma API och samma nyckel som e-ink-tavlan använder (`RESROBOT_API_KEY`). Nyckeln ligger på servern, aldrig i koden:

- på Railway: variabeln `RESROBOT_API_KEY` på tjänsten `retrospelaren` (samma värde som på SoumayAPP)
- lokalt: en fil `resrobot.key` bredvid `server.py` (den finns i `.gitignore`), eller miljövariabeln `RESROBOT_API_KEY`

Sök sedan hållplats under S → När inget spelar och välj i listan. Servern frågar ResRobot högst en gång per minut och hållplats.

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

https://retrospelaren-production.up.railway.app

Railway-tjänsten är kopplad till GitHub-repot `HansJohan89/retrospelaren` och bygger om automatiskt vid varje push till `main`. Den kör `server.py` enligt `railway.json`.

För Spotify där: lägg även till `https://retrospelaren-production.up.railway.app/callback` som Redirect URI.

## Filer

- `index.html` – hela spelaren (en fil)
- `server.py` – servern (lokalt och på Railway): statiska filer, radioproxy, låttitlar, omslag, nyhetsflöden, kommandon
- `starta.bat` / `starta.sh` – startar servern och öppnar webbläsaren

Inställningar, favoriter och Spotify-inloggning sparas i webbläsarens lokala lagring.
