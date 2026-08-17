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

### Anlernen

HMCCU hat keinen Anlern-Befehl — an einer echten CCU lernt man an deren
Weboberfläche an, HMCCU erfährt es nur über `newDevices`. Diese Rolle übernimmt
die QCCU-Weboberfläche (`http://<rechner>:8080`), in zwei Schritten:

1. **Aufkleber hinterlegen** — *Gerät anlernen* → Key vom Aufkleber des Geräts
   (26 Zeichen; alternativ die 32 Hexziffern). Eine Zentrale von eQ-3 beschafft
   sich den Geräteschlüssel selbst; QCCU kann das nicht.
2. **Anlernfenster öffnen** und die Anlerntaste am Gerät drücken.

Danach `get ccu update` in FHEM.

Wer lieber skriptet: beides zusammen geht auch als ein Aufruf an die
JSON-Auskunft, mit dem Schlüssel im Feld `key` — siehe
[SCHNITTSTELLEN.md](SCHNITTSTELLEN.md#json-rpc-8082).

### Wenn etwas nicht geht

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
attr qcul hmId <6 Hexziffern>
```

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
