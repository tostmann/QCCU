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
  Wertelisten, Einheiten); mehrere Clients gleichzeitig; BidCoS über den
  CUL-Zugang.
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

Angelernte Geräte überstehen ein Einspielen: Netzwerkschlüssel und Sendezähler liegen im EEPROM des Sticks und bleiben erhalten (ab Firmware 2.0.11 — ältere Fassungen holten den Schlüssel beim Start nicht zurück).

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

Ohne hinterlegten Aufkleber lehnt QCCU das Anlernen ab und nennt den Grund. Die Funkadresse vergibt die Zentrale. Geräte, die angelernt werden wollen und noch unbekannt sind, erscheinen im Posteingang (Weboberfläche und Home Assistant).

BidCoS-Geräte lernt FHEM über `CUL_HM` an wie an jedem CUL — siehe [docs/FHEM.md](docs/FHEM.md).

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
Stick-Firmware unter [firmware/](firmware/) (siehe die Vermerke dort) und die
HomeMatic-Software von eQ-3, die beim ersten Start geladen und ausgelesen wird
— für sie gelten die HMSL, siehe [Starten](#starten).

Genannte Marken sind Eigentum ihrer jeweiligen Inhaber. Dieses Projekt steht in keiner Verbindung zu deren Inhabern und wird von ihnen weder unterstützt noch geprüft.
