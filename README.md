# QCCU

**Eine Zentrale für Homematic-IP-Geräte am busware CUL — ohne CCU.**

QCCU bildet die Schnittstellen einer CCU nach: XML-RPC mit Rückruf, die
ReGa-Auskünfte und JSON-RPC. Damit lassen sich Systeme anschließen, die eine
CCU anbinden — mit ihren vorhandenen Modulen, ohne Zusatzsoftware, mehrere
gleichzeitig. Geprüft sind zwei; für alles andere reicht die Fläche genau so
weit, wie [docs/SCHNITTSTELLEN.md](docs/SCHNITTSTELLEN.md) es ausweist.

| System | Weg | Anleitung |
|---|---|---|
| **FHEM** | HMCCU (Homematic IP) und CUL/CUL_HM (BidCoS) | [docs/FHEM.md](docs/FHEM.md) |
| **Home Assistant** | Homematic(IP) Local (aiohomematic) — als Add-on oder Container | [docs/HOMEASSISTANT.md](docs/HOMEASSISTANT.md) |
| weitere CCU-Clients | die nachgebildete Fläche im Einzelnen — ungeprüft | [docs/SCHNITTSTELLEN.md](docs/SCHNITTSTELLEN.md) |

Derselbe Stick bedient nebenher **BidCoS/AskSin** über einen culfw-kompatiblen
TCP-Zugang; ein CUL für beide Welten.

## Was QCCU ist — und was nicht

QCCU ist ein Nachbau der CCU-*Schnittstellen*, kein Nachbau der CCU:

* **Ja:** Homematic-IP-Geräte anlernen, lesen, schalten; Zustandsänderungen
  per Rückruf; die Gerätebeschreibungen einer CCU (Datentypen, Grenzen,
  Wertelisten, Einheiten); die **Uhrzeit**, nach der ein frisch angelerntes
  Gerät fragt; mehrere Clients gleichzeitig; BidCoS über den CUL-Zugang.
* **Nein:** keine CCU-Weboberfläche, keine Programme, Systemvariablen, Räume
  oder Direktverknüpfungen; keine Gerätekonfiguration (MASTER-Parameter) und
  Wochenprofile; keine Firmware-Aktualisierung der Geräte; keine Übernahme
  einer laufenden Zentrale ohne Neu-Anlernen.
* **Der eine harte Unterschied:** eine Zentrale von eQ-3 beschafft sich den
  Schlüssel eines neuen Geräts selbst. QCCU kann das nicht — **der Schlüssel
  vom Aufkleber des Geräts muss ihm beigebracht werden**, in der Weboberfläche
  oder als Feld am Anlern-Aufruf. Alles Weitere kann dann aus FHEM oder Home
  Assistant heraus geschehen.

## Voraussetzungen

- busware CUL V3 (868 MHz). Die Firmware liegt bei und wird aus der
  Weboberfläche eingespielt.
- Docker (amd64 oder arm64) — oder Home Assistant OS mit der Erweiterung
- Netzverbindung beim ersten Start
- FHEM mit HMCCU und/oder Home Assistant mit *Homematic(IP) Local*

## Starten

```sh
docker run -d --name qccu --restart unless-stopped \
    -v /dev:/dev \
    --device-cgroup-rule='c 166:* rmw' --device-cgroup-rule='c 189:* rmw' \
    -v qccu-data:/data \
    -e ADVERTISE=<IP dieses Rechners> \
    -e CUL_PORT=2000 \
    -p 2000:2000 -p 2010:2010 -p 8181:8181 -p 8080:8080 -p 8082:8082 \
    tostmann/qccu
```

- `ADVERTISE`: die IP, unter der FHEM oder Home Assistant diesen Rechner
  erreicht. Sie geht in die Rückruf-Anmeldung ein; ohne sie schaltet alles,
  aber Zustandsänderungen kommen nicht an.
- `-v /dev:/dev` und die beiden cgroup-Regeln: serieller Port (166) und
  USB-Bus zum Flashen (189). Den Stick findet QCCU selbst.
- Weboberfläche: `http://<rechner>:8080`
- Die Ports: 2010 XML-RPC, 8181 ReGa, 8082 JSON-RPC (Home Assistant), 2000
  CUL-Zugang (BidCoS), 8080 Weboberfläche. Was welcher Client braucht, steht
  in seiner Anleitung.

**Home Assistant OS:** dort lassen sich keine Container starten — QCCU gibt es
als Erweiterung. Unter **Einstellungen → Add-ons → Add-on-Store → Repositories**
dieses Repository eintragen; Einzelheiten in [addon/README.md](addon/README.md).

Erster Start: der Container lädt das debmatic-Paket (75 MB), liest die
Gerätebeschreibungen aus und verwirft es wieder. Danach liegen die Tabellen in
`/data`, der Schritt entfällt.

**Scheitert dieser Bezug**, startet QCCU trotzdem; die Weboberfläche sagt dann,
was fehlt. Bei jedem Start wird er erneut versucht — ein Neustart des
Containers (oder der Erweiterung) genügt also, sobald das Netz steht. Von Hand
nachholen lässt er sich jederzeit:

```sh
docker run --rm --network host -v qccu-data:/data tostmann/qccu setup
```

Besteht eine Netzverbindung und bricht der Bezug trotzdem mitten im
Herunterladen ab, ist fast immer die **MTU** die Ursache: das Docker-Netz steht
auf 1500, der Anschluss dieses Rechners kann weniger (DSL/PPPoE 1492, VPN- und
Overlay-Netze noch weniger). Der Aufruf oben umgeht das mit `--network host`;
dauerhaft hilft eine passende MTU für das Docker-Netz
(`--opt com.docker.network.driver.mtu=<wert>` beim Anlegen des Netzes oder
`"mtu"` in `/etc/docker/daemon.json`).

Das Paket stammt aus dem Repository von [debmatic](https://github.com/alexreinert/debmatic) und enthält die HomeMatic-Software von eQ-3. Für deren Nutzung gelten die HomeMatic Software Lizenzbedingungen (HMSL) von eQ-3: [LicenseDE.txt](https://github.com/eq-3/occu/blob/master/LicenseDE.txt). QCCU liest die Gerätebeschreibungen aus, indem es die mitgelieferten Klassen aufruft; das Abbild selbst enthält nichts davon.

## Stick-Firmware

Der CUL wird im Bootlader ausgeliefert und meldet sich beim ersten Start nicht als serielles Gerät. Weboberfläche → Stick-Firmware → Einspielen. Danach spricht der Stick nur noch BidCoS und Homematic IP; die übrigen culfw-Funkarten (FS20, IT, EM …) entfallen. Zurück geht es, indem culfw wieder eingespielt wird.

**Erscheint dort kein Stick**, ist keiner im Bootlader. Entweder einen fabrikneuen CUL einstecken — der meldet sich von selbst — oder bei einem bereits benutzten die **BL-Taste auf der Rückseite** gedrückt halten, während er eingesteckt wird. Das gilt auch, wenn der Bootlader nach einem abgebrochenen Versuch stumm bleibt.

### Welchen Stick QCCU benutzt

Gesucht wird allein nach dem eigenen Namen (`busware.de_q-culfw`). Ein CUL mit anderer Firmware wird **nicht** angefasst — er kann an demselben Rechner für FS20 oder anderes in Betrieb sein. Um einen solchen CUL zu übernehmen: abziehen, die BL-Taste auf der Rückseite gedrückt halten, wieder einstecken; er meldet sich dann im Bootlader, und die Oberfläche bietet das Einspielen an. Dieser Handgriff ist die Zustimmung — anders greift QCCU keinen fremden Stick.

Beim ersten Betrieb merkt sich QCCU die Seriennummer des Sticks (`stick_serial` in `/data/state/qccu_devices.json`). Danach kommt nur noch dieser eine in Frage; weitere q-culfw-Sticks am selben Rechner bleiben unberührt. Nach einem Stick-Tausch den Eintrag löschen oder `SERIAL` setzen.

Angelernte Geräte überstehen ein Einspielen: Netzwerkschlüssel und Sendezähler liegen im EEPROM des Sticks und bleiben erhalten (ab Firmware 2.0.11 — ältere Fassungen holten den Schlüssel beim Start nicht zurück). Nachgeprüft: nach `erase` + `flash` meldet der Stick weiterhin seinen Schlüssel, seinen Zählerstand und seine Kennung.

**Firmware 2.0.29 (mitgeliefert seit 2026.8.17) unbedingt einspielen** — die Oberfläche bietet es an, sobald der Stick älter ist. Zwei Dinge sind darin anders, beide gemessen (Luftmitschnitte gegen eine eq-3-Zentrale, 18.08.2026):

* **Frequenz.** Mit den Registern der eq-3-Zentrale senden und empfangen die CUL-Sticks rund 27 kHz tiefer als sie und alle eq-3-Geräte — außerhalb dessen, was der Empfängerbaustein ausregelt. Ein Nachbargerät kam vorher nur bei jedem vierten Versuch durch, jetzt bei jedem. Die Firmware gleicht das aus (`FSCTRL0`).
* **Quittung.** Die Zentrale von eq-3 bestätigt jeden Frame sofort mit einer kurzen 6-Byte-Quittung; ohne sie wiederholt ein Gerät jede Sendung dreimal und sucht dann einen Router. Der Stick sendet sie jetzt genauso. Sichtbar: ein Schaltbefehl ist eine Sendung, kein Dreierpack, und die Steckdose antwortet in ~100 ms.

Wird ein Gerät nach einem Werksreset neu angelernt, ersetzt die neue Funkadresse die alte — Befehle gehen nicht mehr an die tote Adresse.

**Startet der Stick neu, bindet QCCU von selbst wieder an (2026.8.17).** Werksreset, Firmware einspielen, ein Wackler am USB — der Stick ist Sekunden später wieder da, oft unter einer anderen Anschlussnummer. Bisher lief QCCU danach blind weiter: die Oberfläche meldete „Funk läuft", während nichts mehr ankam, und half nur ein Neustart. Jetzt wird der Verlust bemerkt, der Stick über seine gemerkte Seriennummer wiedergefunden und neu eingerichtet; im Protokoll steht, was passiert ist. Das gilt auch im Betrieb ganz ohne Oberfläche (FHEM/HMCCU über XML-RPC) — dort gab es die Suche vorher überhaupt nicht.

**Die zuletzt gemeldeten Werte überstehen einen Neustart (2026.8.17).** Bisher stand nach jedem Neustart überall nichts, bis sich jedes Gerät von selbst das nächste Mal meldete — bei Geräten, die das nur alle paar Stunden tun, ist das der Unterschied zwischen einer Anzeige und einem leeren Feld. Die Werte sind dabei der letzte bekannte Stand, nicht die Wahrheit: die steht im Gerät, und von sich aus fragt QCCU weiterhin nichts ab.

**Die Integration kommt über HACS** — Homematic(IP) Local von SukramJ. ⚠️ QCCU ist dort **keine unterstützte Zentrale** (unterstützt sind CCU2/3, OpenCCU, debmatic, Homegear): unter QCCU liegt weder ReGaHss noch HMIPServer, sondern eine eigene Nachbildung. Probleme mit QCCU deshalb bitte **hier** melden, nicht in den Repositories der Integration. Eine Zeitlang lieferte die Erweiterung die Integration mit; das ist seit 2026.8.22 auf Bitte ihres Autors wieder draußen.

**Nachsehen statt annehmen (2026.8.23).** Neben jedem Gerät steht ein Knopf **prüfen**: QCCU schickt ihm die Uhrzeit und wartet auf die Quittung — eine Sendung, keine Zustandsänderung, Antwort in einer Sekunde. Und wenn ein Gerät länger als eine halbe Stunde nichts gesagt hat, sieht die Zentrale von selbst nach (sparsam: höchstens eines alle fünf Minuten, der Stillste zuerst). Vorher fiel Stille erst auf, wenn jemand vergeblich schaltete — ein Gerät, das niemand schaltet, galt beliebig lange als in Ordnung.

**Die Oberfläche sagt jetzt, was los ist (2026.8.19).** Die Geräteliste führt den vergebenen Namen, wann das Gerät zuletzt gehört wurde und mit welchem Pegel; wer gerade angelernt werden will, steht unter **Anlernwünsche** (mit der Angabe, wann er zuletzt gerufen hat — wer verstummt, verschwindet dort von selbst wieder); und unter **Zuletzt geschehen** stehen die Vorgänge, die man sonst im Protokoll suchen musste — angelernt, entfernt (mit der Auskunft, ob das Gerät den Funk-Ausschluss quittiert hat), Stick verloren und wiedergefunden; diese Liste lässt sich mit einem Knopf leeren. Und wenn ein Gerät mit dem Netzschlüssel der Anlage funkt, ohne in der Geräteliste zu stehen, sagt die Oberfläche das ebenfalls — solche Geräte sind **nicht im Werkszustand** und lassen sich deshalb auch nicht anlernen; ein Werksreset am Gerät hilft. Fehlermeldungen des Dienstes erscheinen außerdem in der Seite, statt zu verschwinden.

**Gerät entfernen:** in der QCCU-Oberfläche, nicht in Home Assistant. Dort gelöscht verschwindet es nur aus Home Assistant und ist beim nächsten Neuladen der Integration wieder da — die Integration räumt beim Löschen nur bei sich auf und sagt der Zentrale nichts. Über QCCU gelöscht bekommt das Gerät dagegen einen Funk-Ausschluss (es lässt sich danach ohne Werksreset neu anlernen), und Home Assistant räumt von selbst auf.

**Namen (2026.8.18).** Vergibt man in Home Assistant beim Anlernen einen Namen, führt QCCU ihn jetzt selbst: er steht in der Geräteauskunft und in der ReGa-Liste (also auch für FHEM/HMCCU), übersteht Neustarts und geht mit dem Gerät, wenn es gelöscht wird. Vorher wies QCCU `Device.setName` und `Channel.setName` ab und der eingegebene Name verschwand.

**Empfangspegel.** Zu jedem empfangenen Frame meldet QCCU den Pegel als `RSSI_DEVICE` an Kanal 0 — das ist die Messung des eigenen Empfängers: wie stark die Zentrale das Gerät hört. In Home Assistant liegt der Sensor unter „Diagnose" und ist dort von der Integration abgeschaltet; einmal aktivieren, dann läuft er. Den umgekehrten Weg (was das Gerät von der Zentrale hört) meldet ein HmIP-Gerät nicht, deshalb bleibt `RSSI_PEER` leer.

**Stick gewechselt oder zurückgesetzt?** Steckt ein Stick ohne Netzwerkschlüssel, während noch Geräte eingetragen sind, erzeugt QCCU **bewusst keinen** — ein neuer Schlüssel würde alle angelernten Geräte lautlos aussperren. Die Oberfläche nennt dann beide Wege: den bisherigen Stick zurückstecken (dann ist alles wie vorher), oder in der Warnkarte **„Geräte verwerfen und neu beginnen"** drücken. Danach hat der Stick einen frischen Schlüssel; die Geräte brauchen je einen Werksreset und werden neu angelernt.

**Gerätetabellen (2026.8.13).** Die Parameterlisten werden jetzt je Gerätetyp und Kanalfassung zusammengesetzt, so wie es eine Zentrale von eQ-3 tut (nachgemessen an einer HmIP-PS-2, alle Kanäle deckungsgleich). Vorher bekam eine Schaltsteckdose 1087 Konfigurationsparameter angeboten — die Vereinigung aller Fassungen, samt Farbverläufen —, was Home Assistant zu Recht als „Inkonsistenz bei Geräte-Paramsets" meldete. Beim ersten Start der neuen Fassung legt QCCU die Tabellen von selbst neu an (einmal Netz für den Paketbezug nötig).

### Stick auf Werk zurücksetzen

Weitergeben, verkaufen, ein Funknetz endgültig auflösen: der Stick löscht auf
Verlangen alles, was er selbst abgelegt hat. Über den seriellen Zugang, in
zwei Schritten — erst fragen, dann löschen:

```
mV              →  Pm marke=15100514 — loeschen mit mV15100514
mV15100514      →  löscht und startet neu
```

Die Marke stammt aus der Seriennummer des Bausteins und ist bei jedem Stick
eine andere; ein Reihenlauf über mehrere Sticks kann also nichts löschen, ohne
jeden einzeln zu fragen. Weg sind danach Kennung, Aufkleber- und
Netzwerkschlüssel, Funkadresse und beide Sendezähler — **alle angelernten
Geräte müssen neu angelernt werden**. Die bei der Fertigung vergebene
Geräteadresse am Ende des EEPROMs bleibt unangetastet; sie ließe sich nicht
wiederherstellen. Genau deshalb macht das die Firmware und nicht der
Bootlader: ein Löschen von dort träfe sie mit.

Ein fabrikneuer Stick hat noch keinen Netzwerkschlüssel; QCCU legt beim ersten Start einen an. Fehlt er, obwohl bereits Geräte eingetragen sind, wird **kein** neuer erzeugt — das würde alle aussperren, und zwar lautlos. QCCU meldet den Zustand und lässt die Entscheidung beim Betreiber.

**Einen fertigen Schlüssel von außen einzutippen gibt es nicht** — weder hier
noch in der Stick-Firmware. Ein Stick, der jeden mitgebrachten Schlüssel
annimmt, wäre ein Werkzeug zum Mitlesen fremder Anlagen. Der Schlüssel kommt
auf genau zwei Wegen herein: die Zentrale erzeugt ihn selbst, oder sie bekommt
ihn beim Anlernen zugeteilt — gegen SGTIN und Aufkleberschlüssel verpackt,
über Funk. So wird auch eine **laufende Anlage übernommen**: der Stick lernt
sich als Gerät an ihrer bisherigen Zentrale an, deren Schlüssel wandert dabei
mit.

## Gerät anlernen

Zwei Teile, und der erste lässt sich nicht umgehen:

1. **Schlüssel vom Aufkleber beibringen** — Weboberfläche → *Gerät anlernen*, Key vom Aufkleber des Geräts (26 Zeichen; alternativ die 32 Hexziffern). Oder als Feld `key` am Anlern-Aufruf über JSON-RPC (`Interface.setInstallModeHMIP`), siehe [docs/SCHNITTSTELLEN.md](docs/SCHNITTSTELLEN.md#json-rpc-8082) — dann entfällt die Weboberfläche. Eine Zentrale von eQ-3 beschafft sich den Geräteschlüssel selbst, QCCU kann das nicht.
2. **Anlernfenster öffnen** — in der Weboberfläche oder aus Home Assistant heraus (Anlernknopf der Integration; HMCCU hat keinen Anlern-Befehl). Dann die Anlerntaste am Gerät drücken.

Ohne hinterlegten Aufkleber lehnt QCCU das Anlernen ab und nennt den Grund. Die Funkadresse vergibt die Zentrale. Geräte, die angelernt werden wollen und noch unbekannt sind, erscheinen unter **Anlernwünsche** (in Home Assistant im Posteingang der Integration, `sensor.<name>_inbox`). Der Eintrag verfällt, sobald das Gerät aufhört zu rufen.

BidCoS-Geräte lernt FHEM über `CUL_HM` an wie an jedem CUL — siehe [docs/FHEM.md](docs/FHEM.md).

## Gerät löschen

Beim Löschen schließt QCCU das Gerät **über Funk aus** — dieselben drei
Schritte, die eine CCU führt (Antrag, Bereitmeldung, Abschluss). Das Gerät
verwirft daraufhin Adresse und Netzwerkschlüssel und meldet sich von selbst
wieder als anlernbereit: ein Werksreset am Gerät ist nicht nötig, ein
Tastendruck auch nicht. Ist das Gerät gerade stromlos, geht der Ausschluss
trotzdem zu Ende — dann erfährt es nichts davon und braucht doch den
Werksreset.

## Einstellungen

| Umgebung | Vorgabe | Bedeutung |
|---|---|---|
| `ADVERTISE` | – | IP dieses Rechners aus Sicht der Clients. Ohne sie gehen Rückrufe ins Leere. |
| `CUL_PORT` | `0` | TCP-Zugang für BidCoS/AskSin, mit `2000` einschalten. `0` = aus. |
| `SERIAL` | – | Pfad des Sticks. Nur bei mehreren Sticks nötig, sonst wird gesucht. |
| `OWN_ADDR` | – | Funkadresse der Zentrale (6 Hex). Ohne Angabe wird beim ersten Start eine gewürfelt und in `/data` gemerkt. Nach dem Anlernen nicht mehr ändern – angelernte Geräte kennen nur die alte. |
| `JSON_PORT` | `8082` | JSON-RPC für Home Assistant. `0` = aus; dann findet die Integration die Zentrale nicht. Ist der Port belegt, läuft alles Übrige trotzdem. |
| `RPC_PORT` / `REGA_PORT` / `WEB_PORT` | `2010` / `8181` / `8080` | Ports. Für HMCCU müssen 2010 und 8181 bleiben. |

Der CUL-Zugang bedient `As<hex>` (senden), `V`, `T01` und `?`. Alles andere wird verworfen, auch Registerbefehle (`W0F`, `W10`, `W11`), die die Frequenz verstellen würden. Beide Funkfamilien teilen sich das 1-%-Sendezeitkonto des Sticks.

Keiner der Ports hat eine Anmeldung — siehe [Sicherheit](#sicherheit).

## Sichern und Ersatz

```sh
docker run --rm -v qccu-data:/data -v "$PWD":/sicherung alpine \
    tar czf /sicherung/qccu-sicherung.tgz -C /data state
```

`/data/state` enthält die angelernten Geräte, die Rückrufe und die Zählerstände. Die Gerätetabellen daneben werden bei Bedarf neu angelegt.

Der Netzwerkschlüssel liegt nur im Stick, verschlüsselt mit dessen Hauptschlüssel. Er ist weder in der Sicherung noch in QCCU. Ein defekter Stick bedeutet: alle Geräte neu anlernen. Ein Ersatz-Stick übernimmt Geräteliste und Zähler aus `/data`; QCCU hebt seinen Sendezähler beim Start auf einen sicheren Wert an.

## Sicherheit

QCCU prüft **keine** Zugangsdaten. Wer die Ports erreicht, kann Geräte lesen
und schalten — das gilt für XML-RPC (2010), ReGa (8181), JSON-RPC (8082),
den CUL-Zugang (2000) und die Weboberfläche (8080) gleichermaßen. Der Dienst
gehört ins eigene Netz und nicht ins offene Internet.

## Aufbau

| | |
|---|---|
| `qccu.py` | XML-RPC, ReGa, Gerätespeicher |
| `qccu_jsonrpc.py` | JSON-RPC-Auskunft für Home Assistant |
| `qccu_radio.py` | Funkanbindung an den Stick |
| `qccu_web.py` | Weboberfläche |
| `qccu_firmware.py` | Zustand der Stick-Firmware, Einspielen |
| `qccu_cul.py` | CUL-Zugang über TCP |
| `tables/` | Werkzeuge, die die Gerätetabellen erzeugen |
| `firmware/` | Stick-Firmware (`.hex`) und Fremdbestandteil-Vermerke |
| `docker/` | Abbild, Einstiegspunkt, Bau – siehe [docker/README.md](docker/README.md) |
| `addon/` | Home-Assistant-Erweiterung – siehe [addon/README.md](addon/README.md) |
| `docs/` | Anleitungen je System und die nachgebildete Fläche |

---

© 2026 Dirk Tostmann. Lizenz: **Apache-2.0** — siehe [LICENSE](LICENSE).

Das gilt für QCCU selbst. Zwei Dinge daneben haben eigene Bedingungen: die
Stick-Firmware unter [firmware/](firmware/) — PolyForm Perimeter 1.0.1, siehe
[firmware/LICENSE](firmware/LICENSE) und die Fremdbestandteil-Vermerke dort —
und die HomeMatic-Software von eQ-3, die beim ersten Start geladen und
ausgelesen wird — für sie gelten die HMSL, siehe [Starten](#starten).

Genannte Marken sind Eigentum ihrer jeweiligen Inhaber. Dieses Projekt steht in keiner Verbindung zu deren Inhabern und wird von ihnen weder unterstützt noch geprüft.
