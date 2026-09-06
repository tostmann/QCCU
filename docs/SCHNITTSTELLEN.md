# Die nachgebildete CCU-Fläche

QCCU bildet die Schnittstellen einer CCU so weit nach, wie die beiden geprüften
Anbindungen sie brauchen — HMCCU (FHEM) und Homematic(IP) Local / aiohomematic
(Home Assistant). Diese Seite listet auf, was genau da ist. Sie ist die
Grundlage für jede weitere Anbindung: wer ein drittes System an QCCU hängen
will, kann hier ablesen, ob es mit dieser Fläche auskommt — bevor er es
ausprobiert.

⚠️ **Geprüft ist nur, was in [FHEM.md](FHEM.md) und
[HOMEASSISTANT.md](HOMEASSISTANT.md) steht.** Für alles andere gilt: nicht
getestet, und QCCU beantwortet Unbekanntes in der Regel **leer statt mit
Fehler** — ein fremder Client kann also „verbunden" sein und trotzdem nichts
sehen, ohne dass irgendwo eine Fehlermeldung steht.

| Port | Dienst | Wer ihn nutzt |
|---|---|---|
| 2010 | XML-RPC, Schnittstelle `HmIP-RF` | HMCCU, aiohomematic |
| 8181 | ReGa-Auskünfte (`/tclrega.exe`) | HMCCU |
| 8082 | JSON-RPC (`/api/homematic.cgi`) | aiohomematic |
| 2000 | CUL-Zugang im culfw-Stil (BidCoS/AskSin) | FHEM `CUL`/`CUL_HM` |
| 8080 | Weboberfläche | Mensch |

Keiner der Ports prüft Zugangsdaten (README, Sicherheit).

---

## XML-RPC (2010)

XML-RPC über HTTP, wie es die Python-Standardbibliothek spricht. **Kein
BIN-RPC.** Es gibt genau eine Schnittstelle, `HmIP-RF`; BidCoS-Geräte
erscheinen hier nicht (sie laufen über den CUL-Zugang).

**Rückruf-Vertrag** — wie an einer CCU:

1. Der Client meldet sich mit `init(url, interface_id)` an. QCCU merkt sich
   die Adresse dauerhaft (`/data/state`) und schickt **von sich aus** den
   Gerätebestand als `newDevices(interface_id, [Beschreibungen])` — bei jeder
   Anmeldung, außer dieselbe Kennung wiederholt sich mit derselben Adresse
   binnen fünf Sekunden.
2. Danach kommen `event(interface_id, adresse, parameter, wert)` bei jeder
   Wertänderung, `newDevices` beim Anlernen, `deleteDevices` beim Löschen.
3. `ping(caller)` wird mit `event(interface_id, "CENTRAL", "PONG", caller)`
   beantwortet.
4. `init(url, "")` meldet ab.

Bleibt ein Rückruf mehrfach unzustellbar (fünfmal), wird der Abonnent
ausgetragen.

**Methoden, die etwas tun:** `init`, `listDevices`, `getDeviceDescription`,
`getParamsetDescription`, `getParamsetId`, `getParamset`, `putParamset`,
`getValue`, `setValue`, `setInstallMode`, `setInstallModeWithWhitelist`,
`getInstallMode`, `deleteDevice`, `deleteDevices`, `getVersion`, `ping`,
`getServiceMessages` (UNREACH je Gerät), `listBidcosInterfaces` (ein Eintrag,
`TYPE` = `QCCU`, mit `DUTY_CYCLE` des Sticks), `clientServerInitialized`,
`system.listMethods`, `system.methodHelp`, `system.methodSignature`,
`system.multicall`.

**`setValue` und `putParamset VALUES` kehren erst mit dem Ausgang am Gerät
zurück** (seit 2026.8.45, wie bei der CCU): angenommen → Wert eingetragen und
als Ereignis gemeldet; vom Gerät abgelehnt → `Fault -1 "Generic error
(RESPONSE_NAK)"`, der alte Wert bleibt; keine Quittung → `Fault -1 "Generic
error (TIMEOUT)"` und `UNREACH`; kein belegter Sendeweg für diesen Parameter
in dieser Form → `Fault -5 "No proven way to set …"`. Nur die Kurzquittung
ohne Auskunft auf Anwendungsebene trägt nichts ein — der nächste Status des
Geräts liefert den Wert. Die Wartezeit beträgt höchstens acht Sekunden.

**Methoden, die leer antworten:** `getLinks`, `getLinkPeers`, `getLinkInfo`,
`addLink`, `removeLink`, `setLinkInfo`, `getSuppressedServiceMessages`,
`reportValueUsage`, `rssiInfo` — Direktverknüpfungen führt QCCU nicht.

**Gemeldet, aber nicht umgesetzt** (stehen in `system.listMethods`, damit ein
Client sie nicht vermisst, und werden leer beantwortet):
`activateLinkParamset`, `abortDeleteDevice`, `addDevice`, `changeKey`,
`clearConfigCache`, `determineParameter`, `getKeyMismatchDevice`,
`getMetadata`, `installFirmware`, `listReplaceableDevices`, `listTeams`,
`logLevel`, `refreshDeployedDeviceFirmwareList`, `replaceDevice`,
`restoreConfigToDevice`, `searchDevices`, `setBidcosInterface`,
`setInterfaceClock`, `setMetadata`, `setTeam`, `setTempKey`,
`suppressServiceMessages`, `updateFirmware`.

Alles Unbekannte wird ebenfalls leer beantwortet und im Protokoll vermerkt.

**Datentypen** in `getParamsetDescription`: `BOOL`, `INTEGER`, `FLOAT`,
`ENUM`, `STRING`, `ACTION` — die Schreibweise der CCU-Schnittstelle, nicht die
der Herstellerarchive (`BOOLEAN`, `LONG`). Jeder Eintrag trägt `TYPE`,
`OPERATIONS`, `FLAGS`, `DEFAULT`, `MIN`, `MAX`; ENUMs `VALUE_LIST`, Messwerte
`UNIT`, Sonderrollen `CONTROL`. Die Tabellen dahinter entstehen beim ersten
Start aus dem debmatic-Paket (README).

---

## ReGa (8181)

HTTP `POST` (auch `GET`) mit dem Skripttext im Body, auf beliebigem Pfad —
HMCCU nimmt `/tclrega.exe`. Antwort `text/plain`, ISO-8859-1.

⚠️ **QCCU ist kein ReGa-Interpreter.** Es erkennt am Skripttext, welche
Auskunft gemeint ist, und liefert die. Ein Skript, das keinem Muster
entspricht, bekommt eine leere Zeile — keinen Fehler.

| Erkanntes Muster im Skript | Antwort |
|---|---|
| `root.Devices()` und `Interfaces()` | Geräteliste als Zeilen `D;<Schnittstelle>;<Adresse>;<Adresse>;<Typ>;<Kanäle>`, `C;<Kanaladresse>;<Name>;<Richtung>`, `I;HmIP-RF;HmIP-RF;<URL>` |
| `datapoints.Get("<Kennung>")).State(<wert>)` | Wert setzen (auch mehrere je Skript) |
| `datapoints.Get("<Kennung>")).State()` oder `.Value()` | Wert lesen, `<Kanalname>=<Kennung>=<Wert>` je Zeile |
| `sDevList` und `DPs()` | Datenpunktliste eines Geräts |
| `setInstallMode` / `InstallMode` | Anlernfenster öffnen |
| `cat /VERSION` / `GetVersion` | `VERSION=<Fassung>`, `PRODUCT=QCCU`, `PLATFORM=qccu` |
| `groups.gson` / `system.Exec` | leer (keine Gruppen) |
| alles andere | leere Zeile |

Kanalnamen sind `<Adresse>_<Kanal>` (Doppelpunkt durch Unterstrich ersetzt);
eine Namensvergabe gibt es nicht. Systemvariablen, Programme und Räume gibt es
nicht — Skripte dazu werden leer beantwortet.

Jedes unbekannte Skript wird mit `ReGa ?` und seinen ersten Zeichen ins
Protokoll geschrieben — so lässt sich feststellen, was ein Client vermisst.
Belegtes Beispiel: `get ccu deviceinfo` in FHEM schickt ein Skript über
`dom.GetObject(ID_DEVICES).Get(<adresse>)` mit `HssType()`, `ValueType()` und
`Operations()` je Datenpunkt — seit 2026.9.1 beantwortet (eine `D;`-Zeile für
das Gerät, je Datenpunkt eine `C;`-Zeile mit sieben Feldern, wie
`HMCCU_FormatDeviceInfo` sie liest).

---

## JSON-RPC (8082)

`POST /api/homematic.cgi` (auch `/`) mit
`{"version":"1.1","id":…,"method":"…","params":{…}}`; Antwort
`{"version":"1.1","id":…,"result":…,"error":…}`. `Session.login` liefert immer
eine Kennung — Benutzer und Kennwort werden nicht geprüft, die Kennung später
auch nicht.

**Methoden** (genau die aus `system.listMethods`; ein Client, der etwas
anderes verlangt, bekommt `MethodNotFound`):

* Anmeldung: `Session.login`, `Session.renew`, `Session.logout`,
  `CCU.getAuthEnabled` (false), `CCU.getHttpsRedirectEnabled` (false),
  `system.listMethods`
* Geräte: `Interface.isPresent`, `Interface.listInterfaces` (ein Eintrag:
  `HmIP-RF` mit dem XML-RPC-Port), `Interface.listDevices`,
  `Interface.getDeviceDescription`, `Interface.getParamsetDescription`,
  `Interface.getParamset`, `Interface.getValue`, `Interface.getMasterValue`,
  `Interface.setValue`, `Interface.putParamset`, `Device.listAllDetail`
* Anlernen: `Interface.getInstallMode`, `Interface.setInstallModeHMIP`.
  Letzteres wertet das Feld **`key`** aus — der Schlüssel vom Aufkleber, als
  26 Zeichen oder 32 Hexziffern; damit lässt sich ein Gerät ohne die
  QCCU-Oberfläche anlernen. `aiohomematic` sendet das Feld heute immer leer;
  ohne Schlüssel — weder im Aufruf noch hinterlegt — **lehnt QCCU ab**
  (`NoDeviceKey`) statt ein Fenster zu öffnen, in dem nichts passieren kann.
  `getInstallMode` meldet die Restzeit unabhängig davon, auf welchem Weg das
  Fenster geöffnet wurde
* Leer, aber gültig: `Interface.getSuppressedServiceMessages`,
  `Channel.hasProgramIds`, `Program.getAll`, `Room.getAll`,
  `Subsection.getAll`, `SysVar.getAll`
* `ReGa.runScript` — siehe unten

Bewusst **nicht** gemeldet und mit `NotSupported` abgewiesen: `Device.setName`,
`Channel.setName`, `Program.execute`, `SysVar.getValueByName`,
`Interface.getLinkInfo`, `Interface.setLinkInfo`,
`Interface.suppressServiceMessages`.

**Skripte über `ReGa.runScript`:** auch hier kein Interpreter. Erkannt wird
der Kopf `!# name: <datei>`, wie aiohomematic seine Skripte kennzeichnet:

| Skript | Antwort |
|---|---|
| `fetch_all_device_data.fn` | alle Werte als `{"<Schnittstelle>.<Kanal>.<Parameter>": wert}` |
| `get_backend_info.fn` | `version`, `product` = `QCCU`, `hostname` |
| `get_serial.fn` | `QCCU` + Funkadresse |
| `get_system_update_info.fn` | nichts steht an |
| `get_inbox_devices.fn` | Posteingang: Geräte, die angelernt werden wollen (in der QCCU-Oberfläche: **Anlernwünsche**), dazu frisch angelernte, die die Gegenstelle noch aufnehmen muss. Er lebt im Arbeitsspeicher — ein Neustart der Zentrale leert ihn; ein Anlernwunsch ist eine Momentaufnahme, keine Bestandsliste. Ein Eintrag verfällt, wenn drei erwartete Rufe ausbleiben (mindestens 5 min, höchstens 1 h); höchstens 20 |
| `accept_device_in_inbox.fn` | öffnet das Anlernfenster; ohne Aufkleber `success: false` mit Grund |
| `get_service_messages.fn`, `get_alarm_messages.fn`, `get_program_descriptions.fn`, `get_system_variable_descriptions.fn` | `[]` |
| alles andere | leere Zeichenkette |

**Formate, die vom XML-RPC-Weg abweichen** (so verlangt es die Gegenstelle):
`getParamsetDescription` ist eine **Liste** von Einträgen mit `NAME` und `ID`,
`VALUE_LIST` darin eine Zeichenkette mit Leerzeichen als Trenner;
Gerätebeschreibungen kommen kleingeschrieben (`type`, `address`, `paramsets`,
`subType`, `rxMode` …); `setValue` nimmt `type` (`bool`, `int`, `double`,
`string`) entgegen und wandelt den Wert danach.

`product` = `QCCU` ist Absicht: aiohomematic leitet daraus ab, dass es weder
Sicherung noch Systemaktualisierung gibt.

---

## CUL-Zugang (2000)

TCP, zeilenorientiert (CR LF), im Stil eines culfw-CUL. Mehrere Klienten
gleichzeitig sind möglich; jeder bekommt die empfangenen BidCoS-Frames als
`A…`-Zeilen.

| Vom Klienten | Wirkung | FHEM |
|---|---|---|
| `As<hex>` | Frame senden (roh, der Stick prüft nur die Länge) | `CUL_HM` |
| `V` | `V <fassung> q-culfw` — die Fassung des Sticks | `get <cul> version` |
| `?` | `? (? is unknown) Use one of A C T V X t` | `get <cul> cmds` |
| `T01` / `T01<xxxx>` | FHT-Kennung lesen / setzen (nur gemerkt, damit `CUL_DoInit` durchläuft) | Anmeldung |
| `T03` | `00` — diese Zentrale hat keinen FHT-Sendepuffer | `get <cul> fhtbuf` |
| `t` | Laufzeit von QCCU, acht Hexziffern in Ticks von 8 ms | `get <cul> uptime` |
| `X` | Meldeform und Restkonto des Sticks, etwa `21 900` | `get <cul> credit10ms` |
| `X<hh>` | Meldeform setzen (FHEM schickt beim Anmelden `X21`); keine Antwort | Anmeldung |
| `C<hh>` | ein CC1101-Register **bis 0x2E**, in der Schreibweise von culfw: `C0D = 21 / 33` | `get <cul> ccconf` |
| `Ar` | durchgereicht (schaltet den Empfang ein, den QCCU ohnehin führt) | rfmode-Wechsel |
| `Ax` | angenommen, aber **nicht** ausgeführt — abschalten darf den Empfang kein Klient: am selben Stick hängt die HmIP-Seite | rfmode-Wechsel |
| alles andere | verworfen und gezählt, die letzte Zeile steht im Zustand (`letzte_unbekannt`) — darunter die Registerschreibbefehle `W…`, die die Frequenz verstellen würden | — |

Ab Register 0x30 liest der Stick mit gesetztem Burst-Bit, und 0x3F ist der
RX-FIFO: ein `C3F` zöge ein Byte aus einem laufenden Empfang. Darum endet der
Zugang bei 0x2E — dieselbe Grenze, die q-culfw für seinen Schreibbefehl zieht.
FHEM braucht ohnehin nur 0D, 0E, 0F, 10, 1B und 1D.

**Das Format ist nicht Geschmackssache.** FHEM prüft jede Antwort gegen ein
Muster (`00_CUL.pm`, `%gets`) und wartet sonst drei Sekunden vergeblich; eine
Antwort in der falschen Form ist so gut wie keine — und **gar keine** Antwort
ist schlimmer als eine späte: `CUL_Get` ruft dann `DevIo_Disconnected` und meldet
die Verbindung neu an. Deshalb antwortet `X` immer (notfalls nur mit der
Meldeform, die leere Zahl ist zulässig), und `C<hh>` greift auf den zuletzt
gelesenen Registerwert zurück, wenn der Stick gerade nicht antwortet. Zwei Stellen weichen darum
bewusst von dem ab, was der Stick selbst sagt: er antwortet auf `C0D` knapp mit
`C0D=21`, FHEM erwartet aber `^C.* = .*` und liest den **Dezimalwert** aus dem
fünften Feld — QCCU schreibt die Antwort des Sticks entsprechend um. Und `t`
kennt der Stick gar nicht; dort antwortet die Zentrale mit ihrer eigenen
Laufzeit.

Homematic-IP-Frames erscheinen hier nicht — der Stick trennt die Familien
selbst. Beide teilen sich das 1-%-Sendezeitkonto des Sticks.

---

## Was einem weiteren System zu prüfen bleibt

* **XML-RPC, kein BIN-RPC.** Wer nur BIN-RPC spricht (oft die Vorgabe für
  BidCos-RF auf 2001), kommt so nicht an QCCU heran.
* **Nur `HmIP-RF` auf 2010.** Feste Erwartungen an 2001 (BidCos-RF) oder
  2000 (BidCos-Wired) laufen ins Leere; BidCoS gibt es nur über den
  CUL-Zugang.
* **Welche ReGa-Skripte schickt er?** Alles außerhalb der Tabellen oben wird
  leer beantwortet. Ein Client, der seine Geräteliste über ein anderes Skript
  holt, sieht keine Geräte — und keinen Fehler.
* **Rückruf.** Wer den XML-RPC-Weg geht, muss `init` mit erreichbarer Adresse
  senden und den Bestand aus `newDevices` nehmen, statt ihn abzufragen.
* **Zugangsdaten** werden angenommen, aber nicht geprüft.

Wer eine weitere Anbindung ausprobiert: was gefehlt hat — Methode, Skript,
Format — ist als Rückmeldung willkommen; daraus lässt sich die Fläche
gezielt erweitern.
