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
- **Kanter:** datum och veckonummer till vänster och klocka till höger överst; längs nederkanten vädret till vänster (symbol, temperatur, dagens högsta och lägsta) och stegräknaren till höger, alla i samma stora storlek (Open-Meteo, standard Falun, valfri ort). Storleken ställs med "Datum och klocka %" under Utseende; sidornas innehåll och visualiseringarna flyttar sig så att inget krockar.

### Stegräknare

Samma som e-ink-tavlan: dagens steg hämtas från SoumayApp (`/api/steps`, som telefonen fyller via Health Connect och HC Webhook). De visas till höger längs nederkanten, lika stort som datum och klocka: tassavtryck, en linjal med 10 streck mot dagsmålet (standard 10 000, ett streck per 1 000 steg) och antalet i tusental (1.3 = 1 300). I 60-talsfärg går strecken från rött via orange till grönt. Syns bara när det finns steg för idag. Slås av och på och dagsmålet ställs under S → Kanter.

## När inget spelar

I stället för bara testbilden växlar skärmen mellan sidor (S → När inget spelar):

- **Hemskärm:** flera rutor samtidigt, du väljer vilka: klocka, väder, bussar, nyhet, AIK och foto. Rutnätet anpassar sig efter hur många som är valda; fotot tar två rader. Nyheten och fotot byts medan hemskärmen visas. Väljer du bara hemskärmen stannar den kvar.
- **Väder, bussar och AIK:** överst nuläge (känns som, vind och byar, luftfuktighet, lufttryck, sol upp/ned) med fem dagars prognos bredvid, under det prognos varannan timme. Nederst hållplatsskylten och bredvid den Allsvenskan-tabellen, lika hög som skylten, med AIK i mitten och nästa match ("Nästa:" och de två lagens emblem) med datum och tid. Tabellen kan stängas av. När det är trångt döljs timprognosen och listorna kortas, så att inget hamnar ovanpå något annat.
- **Nyheter:** en artikel i taget med rubrik, ingress och bild, från SVT Nyheter, SVT Dalarna, Ekot eller valfritt RSS-flöde. Saknar flödet bild hämtas artikelns delningsbild. En stapel visar när nästa artikel kommer.
- **Foton:** bilder ur ett eller flera publikt delade Google Foto-album, i helskärm med långsam inzoomning och mjuk övertoning. Visas i bildrörets färgläge eller i originalfärg.
- **Fotboll:** AIK i Allsvenskan: placering, nästa match, de tre senaste resultaten och hela tabellen.
- **Testbild**

Välj vilka sidor som ingår, sekunder per sida, per artikel och per foto.

### Foton från Google Foto

Samma metod som e-ink-tavlan: Googles API för det här är stängt sedan mars 2025, så servern läser bildadresserna ur albumets publika sida. Ingen nyckel behövs.

1. Öppna albumet i Google Foto, tryck Dela och välj **Skapa länk** (inte "Bjud in personer").
2. Klistra in länken under S → När inget spelar, en per rad om du har flera album.

Albumen läses om en gång i timmen, så nya bilder dyker upp av sig själva.

### Fotboll (Highlightly)

Samma källa som e-ink-tavlan: Highlightly, med nyckeln `HIGHLIGHTLY_API_KEY` (på Railway som variabel, lokalt i filen `highlightly.key`). Bara AIK. Gratisnivån ger 100 anrop per dygn och delas med e-ink-tavlan, som använder ungefär 58. Den här servern hämtar därför högst var tredje timme (3 anrop per hämtning, 24 per dygn).

### Bussavgångar (ResRobot)

Avgångarna visas som en hållplatsskylt i Dalatrafiks stil med fyra avgångar: rött namnfält med hållplatsen, vita rader med röda linjer, linjesymbol (grön för lokal- och länstrafik, röd för express), linjenummer, vart bussen går (eller den hållplats du valt att åka till) och avgångstid. Skylten krymps om det behövs så att fyra avgångar alltid får plats, och påverkas av bildrörets spökbild och färgfransar som resten av bilden. Längst ner en smal röd list. Dalatrafiks logotyp (vit häst, `assets/dalatrafik-logo.png`) sitter i namnfältet; en SVG med namnet `dalatrafik-logo.svg` används i stället om den finns. Skylten visas i Dalatrafiks färger, eller i bildrörets färgläge om du stänger av det under S → När inget spelar.

Avgångarna hämtas från Trafiklabs ResRobot v2.1, samma API och samma nyckel som e-ink-tavlan använder (`RESROBOT_API_KEY`). Nyckeln ligger på servern, aldrig i koden:

- på Railway: variabeln `RESROBOT_API_KEY` på tjänsten `retrospelaren` (samma värde som på SoumayAPP)
- lokalt: en fil `resrobot.key` bredvid `server.py` (den finns i `.gitignore`), eller miljövariabeln `RESROBOT_API_KEY`

Sök sedan hållplats under S → När inget spelar och välj i listan. Välj också gärna **Framme vid**: då visar skylten bara bussar som går direkt från din hållplats till den utan byte (ResRobots resesökning, samma som e-ink-tavlans reseplan), med den hållplatsen på varje rad och avgångstiden till höger. Servern frågar ResRobot högst en gång per minut och hållplats, tre timmar framåt och upp till tolv timmar vid gles trafik, så att det alltid finns flera avgångar att visa.

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

## Styrning utifrån (jalusin, macropad, m.m.)

Servern tar emot enkla kommandon via en delad kö: vem som helst med nätverksåtkomst till
servern kan lägga in ett kommando med `/api/send?c=...`, och den som vill utföra det hämtar
kön med `/api/cmd?since=N` (returnerar `{"last": N, "items": [{"id", "c"}, ...]}`). **Två
olika mottagare pollar samma kö, var och en filtrerar på de kommandon den bryr sig om:**
- **Sidan själv** (webbläsaren, var 300:e ms) - kör musik/skärm-kommandona nedan direkt.
- **monitor.py på kameradatorn** (Windows, har kontakt med Trådfri-gatewayen lokalt) - kör
  `tradfri-refresh` och de nya `light:...`-kommandona (se nästa avsnitt) mot gatewayen.

En macropad behöver alltså bara kunna göra ett enkelt HTTP-anrop per knapptryck - oavsett
om den sitter på Pi:n som USB-tangentbord (ett litet Python-skript med t.ex. `evdev` som
lyssnar på tangenttryckningar och gör anropet) eller något annat. Inget nytt att bygga för
musik/skärm - de kommandona finns redan:

```
curl "http://127.0.0.1:8888/api/send?c=open"       # tänd bild, starta musik
curl "http://127.0.0.1:8888/api/send?c=close"      # pausa, släck bild
curl "http://127.0.0.1:8888/api/send?c=next"       # också: prev, playpause
curl "http://127.0.0.1:8888/api/send?c=playpause"
curl "http://127.0.0.1:8888/api/send?c=viz:3"      # byt kanal/visualisering
curl "http://127.0.0.1:8888/api/send?c=palette:sepia"
```

Reedbrytarens Python-skript på Pi:n behöver alltså bara anropa `open`/`close`.
Kör macropaden mot Railway-adressen i stället för `127.0.0.1` om den inte sitter på
samma dator som servern, t.ex. `https://retrospelaren-production.up.railway.app/api/send?c=open`.

### Belysning (Trådfri) - nytt kommandoformat

Inställningssidan har nu en egen sektion ("Styr belysningen nu") under Vakna-läge/Belysning
som tänder/släcker, dimmar, byter färgton och kör scener direkt - oavsett vad som spelas.
Den skickar kommandon i samma kö, kodade som `light:` följt av en URL-kodad JSON-sträng:

```
light:{"group":"Vardagsrum","action":"on"}
light:{"group":"Vardagsrum","action":"off"}
light:{"group":"Vardagsrum","action":"dim","value":60}      # 1-100 (%)
light:{"group":"Vardagsrum","action":"temp","value":30}     # 0=varmast, 100=kallast
light:{"group":"Vardagsrum","action":"color","value":"ffcc88"}  # hex, endast RGB-lampor
light:{"group":"Vardagsrum","action":"scene","value":"Kväll"}   # samma scennamn som i Trådfri-appen
```

En macropad kan alltså skicka exakt samma sak - t.ex.:
```
curl "http://127.0.0.1:8888/api/send?c=$(python3 -c "import json,urllib.parse;print('light:'+urllib.parse.quote(json.dumps({'group':'Vardagsrum','action':'scene','value':'Kväll'})))")"
```

**Implementerat i `monitor.py` på kameradatorn** (inte i det här repot - det körs lokalt,
med kontakt med gatewayen). Pollar `/api/cmd` separat (precis som den redan gör för
`tradfri-refresh`, var 1:a sekund - sänkt från 4s för snabb respons på macropadens reglage),
känner igen kommandon som börjar med `light:`, parsar ut JSON-delen och slår upp gruppen
**på namn** (samma namn som redan rapporteras i `/api/tradfri-list`) via pytradfri direkt
mot gruppens ID (ingen ny listning av gatewayen i vanliga fall), och utför:
- `on`/`off` - sätt gruppens state (`ATTR_DEVICE_STATE`)
- `dim` - `ATTR_LIGHT_DIMMER`, skala 1-100 → pytradfri 1-254
- `temp` - `ATTR_LIGHT_MIREDS`, skala 0 (varmast) till 100 (kallast) → mired 454-250
- `color` - `ATTR_LIGHT_COLOR_HEX`; lampor som inte stödjer RGB ignorerar det tyst
- `scene` - samma mood/scen-logik som redan finns för väckning, men direkt istället för
  vid ett bevakningstillfälle

Alla `light:`-kommandon körs i ORDNING av en egen bakgrundstråd (`_light_cmd_worker`) via
en kö, istället för en egen tråd per kommando - annars finns ingen garanti att t.ex. två
snabba drag på ett reglage faktiskt appliceras i rätt ordning. Hinner flera kommandon samlas
på kön innan tråden hunnit ta det föregående (ett snabbt reglage-drag), slås de ihop per
grupp+action till bara det SENASTE värdet - de äldre mellanlägena är redan inaktuella.

Macropadens körvy (se nästa avsnitt) har förutom knapparna tre reglage: volym (lokal,
ingen nätverksfördröjning), ljusstyrka och färgtemp. De två sista styr **det rum som
senast valdes med en belysningsknapp i rutnätet** - trycker du på en belysningsknapp
"pekar" reglagen om mot den gruppen tills du trycker på en annan.

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


## Sparade inställningar (volym)

Inställningar, favoritstationer och senaste station sparas både i webbläsaren och på servern
(`/api/state`, filen `state.json`). På Railway ligger filen på en volym monterad på `/data`
(servern läser `RAILWAY_VOLUME_MOUNT_PATH`, eller `DATA_DIR` om den är satt), så allt finns kvar
efter omstarter och nya versioner, och är detsamma på alla enheter som öppnar sidan.
Lokalt hamnar filen i mappen `data/`. Spotify-inloggningen sparas bara i webbläsaren.

Vädersidan visar sex dagar framåt. Med fotboll påslagen står timprognosen (fyra tider) ovanför
hållplatsskylten och tabellen till höger med föregående och nästa match. Text och emblem i
skylten och tabellen följer spaltens bredd, så att inget kapas eller krockar i helskärm eller 4:3.

## Fotbollskälla

Tabell och matcher för AIK hämtas som standard från ESPN:s öppna JSON-tjänst
(site.api.espn.com, liga `swe.1`, AIK = lag 994). Den kräver ingen nyckel och har ingen dagskvot.
Under inställningarna kan källan bytas till Highlightly (kräver HIGHLIGHTLY_API_KEY, 100 anrop/dygn
delade med e-ink-tavlan) och hämtningsintervallet ställas in (standard 60 minuter). Under en AIK-match
hämtar ESPN var 5:e minut och vädersidan visar ställningen. Senaste hämtningen sparas på volymen.
