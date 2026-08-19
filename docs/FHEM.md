# QCCU in FHEM

FHEM spricht QCCU mit den Modulen an, die es für eine CCU und für einen CUL
ohnehin hat — ein eigenes QCCU-Modul gibt es nicht und braucht es nicht:

| Geräte | FHEM-Modul | Weg zu QCCU |
|---|---|---|
| Homematic IP (Funk) | **HMCCU** (`HMCCU`, `HMCCUDEV`, `HMCCUCHN`) | ReGa (8181) und XML-RPC (2010), wie an einer CCU |
| BidCoS / AskSin | **CUL** + **CUL_HM** | CUL-Zugang (2000) im culfw-Stil |

Beides läuft gleichzeitig über denselben Stick. Was QCCU grundsätzlich ist und
kann, steht in der [README](../README.md); Home Assistant daneben in
[HOMEASSISTANT.md](HOMEASSISTANT.md).

> ⚠️ **FHEM und QCCU auf demselben Rechner: erst die Firmware einspielen, dann
> FHEM an den USB lassen.** Die mitgelieferte Konfiguration von FHEM enthält
> `define initialUsbCheck notify global:INITIALIZED usb create`. Findet FHEM
> beim Start einen CUL im Bootlader, spielt es ihm **culfw** ein — also genau
> dem Stick, den QCCU dort für q-culfw erwartet. An einem frisch aufgesetzten
> FHEM beobachtet: `CULflash dfu-programmer atmega32u4 erase && … flash
> ./FHEM/firmware/CUL_V3.hex`, abgesetzt ohne Rückfrage; es scheiterte allein
> daran, dass jener Behälter kein `/dev/bus/usb` sah. Wer FHEM nativ oder mit
> `-v /dev:/dev` betreibt, spielt die Firmware also zuerst über QCCU ein und
> löscht danach `initialUsbCheck` — oder gibt FHEM nur `/dev/serial/by-id`
> statt des ganzen `/dev`.

---


## Zwei Fallen beim Anschluss (am Aufbau gemessen, 19.08.2026)

**HMCCU muss dieselbe Adresse tragen wie `ADVERTISE`.** Meldet QCCU sich als
`10.10.11.113` und wird HMCCU mit einer anderen Adresse desselben Rechners
definiert (etwa `172.17.0.1` aus einem Container heraus), scheitert der Start
des RPC-Servers mit `HMCCURPCPROC … HMCCU I/O device not found`: HMCCURPCPROC
sucht sein I/O-Gerät über den Host, den die Zentrale meldet. Mit der
`ADVERTISE`-Adresse läuft er sofort.

**Nach `modify <cul> …` das `rfmode` neu setzen.** Das CUL-Modul setzt seine
Client-Liste beim Neudefinieren auf die SlowRF-Vorgabe zurück — `CUL_HM` fehlt
dann, und **jedes** HomeMatic-Telegramm landet als `Unknown code A… help me!`
im Log, auch die Quittungen. FHEM meldet `MISSING ACK`, obwohl das Gerät
antwortet. Heilung: `attr <cul> rfmode HomeMatic` erneut setzen.

## 1. Homematic IP über HMCCU

```
define ccu HMCCU <rechner>
```

`<rechner>` ist die IP oder der Name des Rechners, auf dem QCCU läuft. Die
Ports 8181 (ReGa) und 2010 (HmIP-RF) sind durch HMCCU festgelegt; QCCU muss
also mit den Vorgaben laufen. HMCCU erfährt die Schnittstellen aus der
ReGa-Auskunft — QCCU nennt dort genau eine, `HmIP-RF`, und nur für die meldet
HMCCU seinen Rückruf an.

**FHEM auf einem anderen Rechner:** HMCCU muss eine Rückruf-Adresse melden, die
QCCU von dort erreicht:

```
attr ccu rpcserveraddr <IP des FHEM-Rechners>
```

Sonst schaltet alles, aber die Readings stehen still — der Rückruf geht ins
Leere. Aus demselben Grund braucht QCCU selbst `ADVERTISE` (README).

**Geräte anlegen:** nach dem Anlernen

```
get ccu ccuConfig
define <name> HMCCUDEV <adresse>
```

`get ccu ccuConfig` liest die Geräte neu ein — es meldet dann etwa
`Devices: 1, Channels: 7`. **Nicht `get ccu update`:** das aktualisiert nur
schon definierte FHEM-Geräte und antwortet auf ein neues Gerät mit
`Found no devices to update`. Die Adresse ist die des Geräts
(`get ccu ccuDevices` zeigt sie); geschaltet wird danach mit `set <name> on`
bzw. `off`.

### Anlernen — und wie das Gerät danach nach FHEM kommt

HMCCU hat keinen Anlern-Befehl: an einer echten CCU lernt man an deren
Weboberfläche an, HMCCU erfährt es nur über `newDevices`. Diese Rolle übernimmt
die QCCU-Weboberfläche (`http://<rechner>:8080`). Der ganze Weg, Schritt für
Schritt (so am Aufbau durchlaufen, 20.08.2026):

**1. Aufkleber hinterlegen und anlernen.** *Gerät anlernen* → Key vom Aufkleber
des Geräts (26 Zeichen; Bindestriche und Leerzeichen sind egal, alternativ die
32 Hexziffern), Fenster öffnen, Anlerntaste am Gerät drücken. Eine Zentrale von
eQ-3 beschafft sich den Geräteschlüssel selbst; QCCU kann das nicht.

**2. ⚠️ Das Gerät „melden".** Ein frisch angelerntes Gerät wird der Gegenstelle
**nicht sofort** gemeldet — es wartet, bis es aufgenommen wurde (so hält es
auch eine Zentrale von eQ-3, dort heißt das Merkmal `ReadyConfig`). In der
Geräteliste steht dann **„wartet auf Aufnahme"**, daneben der Knopf
**melden**. Erst dieser Klick schickt `newDevices` an HMCCU.

Home Assistant hat dafür einen eigenen Posteingang; **FHEM hat keinen**, und
darum ist der Knopf hier der Weg. Wer ihn nie drücken will, schaltet in den
Einstellungen **`sofort_melden`** ein (bzw. startet mit `--sofort-melden`) —
dann geht jedes frisch angelernte Gerät sofort hinaus, wie vor 2026.8.29.

**Ohne diesen Schritt sucht man in FHEM vergeblich:** das Gerät ist angelernt,
funkt und ist in der QCCU-Oberfläche schaltbar — die Zentrale hat es der
Gegenstelle nur noch nicht angeboten.

**3. In FHEM einlesen und anlegen.**

```
get ccu ccuConfig                                   # Geräte + Beschreibungen neu lesen
get ccu ccuDevices                                  # zeigt Adresse, Modell, Kanäle
get ccu create <adresse> p=<präfix> forceDev        # FHEM-Gerät daraus bauen
```

`get ccu ccuConfig` meldet danach etwa `Devices: 1, Channels: 7,
Device descriptions: 8, Paramset descriptions: 27`. **Nicht `get ccu update`:**
das aktualisiert nur schon definierte FHEM-Geräte und antwortet auf ein neues
Gerät mit `Found no devices to update`.

`get ccu create` erkennt die Kanäle selbst — bei einer HmIP-PS-2 entsteht
`… HMCCUDEV <adresse> forceDev sd=2.STATE cd=3.STATE`: Kanal 3 schaltet,
Kanal 2 meldet den echten Relaiszustand zurück. Wer lieber von Hand definiert,
nimmt `define <name> HMCCUDEV <adresse>`.

**4. Schalten.** `set <name> on` / `off`. Der Status folgt über den Rückruf;
je nach Aktualisierungstakt von HMCCU steht er ein paar Sekunden später da.

Wer lieber skriptet: Aufkleber und Anlernfenster gehen auch als ein Aufruf an
die JSON-Auskunft, mit dem Schlüssel im Feld `key` — siehe
[SCHNITTSTELLEN.md](SCHNITTSTELLEN.md#json-rpc-8082).

### Wenn etwas nicht geht

**`Device deleted in CCU`, und nichts schaltet mehr** — das Gerät wurde in
QCCU gelöscht und neu angelernt. HMCCU merkt sich den Verlust am `HMCCUDEV`
und nimmt das Gerät auch dann nicht wieder an, wenn es längst wieder da ist.
`get ccu ccuConfig` liest die Geräte neu ein; danach schaltet dasselbe
`HMCCUDEV` wieder. (Ein Firmware-Einspielen allein löst das **nicht** aus —
angelernte Geräte überstehen es, Adresse und Schlüssel bleiben.)

**Readings stehen still, Schalten geht aber** — der Rückruf kommt nicht an:
`rpcserveraddr` (oben) und `ADVERTISE` prüfen.

**Nach einer Neuinstallation von QCCU (leeres `/data`) kommt nichts mehr an**
— die Rückrufliste von QCCU ist leer, HMCCU merkt das erst nach zehn Minuten.
`set ccu rpcserver off`, dann `set ccu rpcserver on` meldet es sofort neu an.

**`RPCState` bleibt nicht auf `running`** — QCCU erreichbar? Ports 8181 und
2010 offen? Läuft QCCU auf anderen Ports (`RPC_PORT`/`REGA_PORT`), findet
HMCCU sie nicht.

**`get ccu deviceinfo <adresse>` meldet „Execution of CCU script or command
failed"** — erwartet: dieses Kommando schickt ein ReGa-Skript, das QCCU nicht
kennt (`dom.GetObject(ID_DEVICES).Get(…)`), und Unbekanntes wird leer
beantwortet. Der Betrieb hängt nicht daran: Geräte holen, anlegen, schalten
und die Readings laufen. QCCU schreibt jedes unbekannte Skript mit `ReGa ?`
ins Protokoll — wer eines vermisst, findet es dort.

---

## 2. BidCoS / AskSin über CUL_HM

QCCU bietet den Stick zusätzlich als CUL an — über TCP, weil den seriellen
Port nur ein Prozess halten darf. Eingeschaltet wird der Zugang mit
`CUL_PORT=2000` (README, Einstellungen); dann in FHEM:

```
define qcul CUL <rechner>:2000 1234
attr qcul rfmode HomeMatic
attr qcul hmId <6 Hexziffern>
```

`rfmode HomeMatic` ist **nicht** optional: ohne ihn führt FHEM den Zugang im
SlowRF-Modus, und `CUL_HM` steht gar nicht erst in seiner Client-Liste — es
ließe sich also kein einziges BidCoS-Gerät anlegen. Die Umschaltung schickt
Registerbefehle (`X21`, `Ar`), die QCCU verwirft und im Protokoll vermerkt
(`CUL-Zugang: 'Ar' nicht weitergereicht`); das ist richtig so und stört den
Homematic-IP-Betrieb nicht — der Stick steht ohnehin auf dieser Frequenz.

`hmId` ist die Zentralen-Adresse von CUL_HM und Sache des FHEM-Betreibers —
ohne sie hört CUL_HM nur mit und sendet nie (das Gerät blinkt dann weiter).

Angelernt wird wie an jedem CUL: `set qcul hmPairForSec 180`, dann die
Anlerntaste am Gerät. Das Anlernen macht FHEM allein (Pairing-Request,
Konfiguration, Peering); QCCU reicht die Frames nur durch.

**Was der Zugang kann und was nicht:** durchgereicht wird `As<hex>` (senden);
beantwortet werden `V`, `T01` und `?`. Alles andere wird verworfen — auch
Registerbefehle (`W0F`, `W10`, `W11`), die die Frequenz verstellen und den
Homematic-IP-Betrieb beenden würden. Den Empfang schaltet QCCU selbst; ein
`Ar`/`Ax` vom Klienten wird nicht weitergereicht.

**Ein Gerät auf diesen CUL umhängen** (etwa von einem alten IO): `attr <gerät>
IODev qcul` greift **nicht**, solange das alte IO noch definiert ist — CUL_HM
behält das laufende IO und gibt stumm dessen Namen zurück. Erst `delete <altes
IO>` macht den Weg frei, danach greift `attr … IODev qcul`.

**Empfangspegel:** ab Firmware **2.0.50** trägt die `A`-Zeile den Rohwert aus
dem Empfängerbaustein, wie FHEM ihn erwartet. Ältere Fassungen hängten die
bereits umgerechnete dBm-Zahl an, die FHEM ein zweites Mal umrechnete —
gemessen am selben Gerät: `-94 dBm` statt `-38 dBm`, also rund 50 dB zu
pessimistisch. Wer BidCoS über den CUL-Zugang betreibt, sollte die Firmware
einspielen (Oberfläche → *Firmware*).

**Wenn etwas nicht geht:** bleibt der CUL nach einem Neustart des Containers
auf `disconnected` — `set qcul reopen`. Beide Funkfamilien teilen sich das
1-%-Sendezeitkonto des Sticks; FHEM weiß nichts davon und sähe bei
erschöpftem Konto nur ein stummes Gerät.

---

## 3. Grenzen

* HMCCU sieht keine BidCoS-Geräte — die laufen ausschließlich über CUL_HM.
* Gerätekonfiguration (MASTER-Parameter), Wochenprofile, Direktverknüpfungen
  und Programme führt QCCU nicht; entsprechende Aufrufe werden leer
  beantwortet.
* Andere culfw-Funkarten (FS20, IT, EM …) bietet der Stick unter QCCU nicht.
