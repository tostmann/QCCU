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
`192.0.2.10` und wird HMCCU mit einer anderen Adresse desselben Rechners
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

**`get ccu deviceinfo <adresse>`** wird seit 2026.9.1 beantwortet: je Kanal
und Datenpunkt Name, Wertetyp, letzter bekannter Wert und die Rechte R/W/E, so
wie HMCCU es von der CCU erwartet. Ältere Fassungen meldeten dafür „Execution
of CCU script or command failed". Jedes andere unbekannte Skript schreibt QCCU
weiterhin mit `ReGa ?` ins Protokoll — wer eines vermisst, findet es dort.

**`set … ` schlägt mit „Generic error (RESPONSE_NAK)" fehl** — das Gerät hat
den Befehl abgelehnt, und QCCU sagt es, statt den Wert trotzdem einzutragen
(seit 2026.8.45). Typischer Fall: `BOOST_MODE` an einem Heizkörperthermostat,
dessen Ventil noch nicht vermessen ist (`VALVE_STATE` ≠ `ADAPTION_DONE`).
„Generic error (TIMEOUT)" heißt: keine Quittung vom Gerät, es ist als
`UNREACH` gemeldet.

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
`X21` und `Ar`: die Meldeform reicht QCCU an den Stick weiter, den Empfang
führt es ohnehin selbst. Ein `Ax` (Wechsel auf einen anderen rfmode) wird
angenommen, aber nicht ausgeführt — am selben Stick hängt die
Homematic-IP-Seite, die nicht taub werden darf.

`hmId` ist die Zentralen-Adresse von CUL_HM und Sache des FHEM-Betreibers —
ohne sie hört CUL_HM nur mit und sendet nie (das Gerät blinkt dann weiter).

Angelernt wird wie an jedem CUL: `set qcul hmPairForSec 180`, dann die
Anlerntaste am Gerät. Das Anlernen macht FHEM allein (Pairing-Request,
Konfiguration, Peering); QCCU reicht die Frames nur durch.

**Was der Zugang kann und was nicht:** durchgereicht wird `As<hex>` (senden);
beantwortet werden `V`, `?`, `T01`, `T03`, `t`, `X` und `C<hh>` — damit laufen
die `get`-Abfragen des CUL-Moduls (`version`, `cmds`, `fhtbuf`, `uptime`,
`credit10ms`, `ccconf`) durch, statt in FHEMs Drei-Sekunden-Fenster zu
verhungern. Ausgeführt wird weiterhin nichts übriges — insbesondere nicht die
Registerschreibbefehle (`W0F`, `W10`, `W11`), die die Frequenz verstellen und
den Homematic-IP-Betrieb beenden würden, und keine Registerlesung ab 0x30 (dort
liegt der Empfangspuffer). Quittiert wird es aber: unbekannte Zeilen bekommen
`? `, wie sie der Stick selbst gibt. Ohne diese Quittung wirft FHEM bei
`get <cul> raw` die Verbindung weg (`DevIo_Disconnected`), statt bloß in den
Zeitablauf zu laufen. Die letzte verworfene Zeile hält der Zugang fest —
wer wissen will, was FHEM hier schickt, findet sie im Zustand der Oberfläche
unter `cul.letzte_unbekannt`.

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

**Frequenzversatz.** Meldet der Stick eine Sendung als erfolgreich (kein
`Ps ERR`, Sendezeitkonto voll), reagiert das Gerät aber nicht, kann der
Sendekanal zu weit neben dem Gerät liegen. Die Firmware bringt dafür schon
einen Ausgleich mit — die CUL-V3-Exemplare, an denen sie entstanden ist,
liegen mit denselben Frequenzregistern rund 27 kHz unter den eq-3-Geräten,
weshalb FSCTRL0 auf +17 Schritten steht statt auf 0. Liegt der Quarz eines
Sticks anders herum daneben, schiebt dieser Ausgleich ihn um denselben Betrag
in die falsche Richtung, und die Nachführung des Empfängerbausteins fängt bei
dieser Bandbreite nur ±25,4 kHz.

Messen, ohne neu zu flashen: in der Oberfläche *Mitschnitt einschalten*, dann
*Frequenzdiagnose einschalten*. Danach steht im Mitschnitt je empfangenem
Rahmen eine Zeile

    PH fe=-6 first=-6 min=-7 max=-5 n=4 rssi=-71 len=27 raw=<gekuerzt>

`fe` ist der Versatz, der **nach** dem Ausgleich noch bleibt, in Schritten zu
je 1,587 kHz. ⚠️ Der Wert sättigt bei ±16 — am Aufbau durchgestimmt und dort so
gemessen; er passt zum Fangbereich der Nachführung (±25,4 kHz bei 101,6 kHz
Bandbreite, also ±16 Schritte). Steht dort +15 oder -15, ist der wahre Versatz
größer als angezeigt, und es braucht einen zweiten Durchgang. Gemessen wird an
den Rahmen, die hereinkommen; es braucht also ein Gerät, das sendet.

Die Diagnose danach wieder ausschalten: sie schreibt je empfangenem Rahmen
eine zusätzliche Zeile über dieselbe serielle Leitung, über die auch der
Funkverkehr läuft — zum Messen ist das richtig, im Dauerbetrieb ist es
unnötige Last.

Nachstellen: den gemessenen Wert als Einstellung `freq_offset` eintragen (in
Schritten, negativ wie positiv) und die Erweiterung neu starten. ⚠️ Der Wert
wird zum Ausgleich der Firmware **addiert**, nicht an seine Stelle gesetzt —
`fe=-6` heißt also `freq_offset: -6`. Danach noch einmal messen: `fe` sollte
jetzt um 0 herum liegen.

Braucht es einen zweiten Durchgang, wird der neue Messwert zum **bereits
eingetragenen** addiert: stand dort `-16` und die Diagnose zeigt danach noch
`-7`, lautet der neue Eintrag `-23`. `freq_offset` bezieht sich immer auf den
Ausgangswert der Firmware, nicht auf den zuletzt gesetzten — QCCU merkt sich
dafür, was vor dem ersten Eingriff im Register stand.

**Wenn etwas nicht geht:** bleibt der CUL nach einem Neustart des Containers
auf `disconnected` — `set qcul reopen`. Beide Funkfamilien teilen sich das
1-%-Sendezeitkonto des Sticks; `get qcul credit10ms` nennt den Rest (in
Einheiten von 10 ms, wie bei culfw). FHEM *rechnet* damit nicht — weder
`00_CUL.pm` noch `CUL_HM` werten das Reading aus, FHEMs eigene Bremse ist
`XMIT_TIME`/`NR_CMD_LAST_H` —, aber ohne diesen Blick sieht man bei
erschöpftem Konto nur ein stummes Gerät.

---

## 3. Grenzen

* HMCCU sieht keine BidCoS-Geräte — die laufen ausschließlich über CUL_HM.
* Gerätekonfiguration (MASTER-Parameter), Wochenprofile, Direktverknüpfungen
  und Programme führt QCCU nicht; entsprechende Aufrufe werden leer
  beantwortet.
* Andere culfw-Funkarten (FS20, IT, EM …) bietet der Stick unter QCCU nicht.
