# QCCU in Home Assistant

Vom leeren Stick bis zur geschalteten Steckdose. Wer QCCU schon betreibt,
springt zu [Integration einrichten](#3-integration-einrichten). Was QCCU
grundsätzlich ist und kann, steht in der [README](../README.md); FHEM daneben
in [FHEM.md](FHEM.md).

Home Assistant führt die Geräte über die Integration **Homematic(IP) Local**
(von SukramJ, auf `aiohomematic`, über HACS). QCCU gibt sich ihr gegenüber als
Zentrale aus — ein eigenes Modul braucht es nicht. ⚠️ QCCU ist dort allerdings
**keine unterstützte Zentrale**; Fehlermeldungen gehören ins QCCU-Repository,
siehe [Abschnitt 3](#3-integration-einrichten). Geprüft mit Homematic(IP) Local 2.7.0 /
aiohomematic 2026.5.0 unter Home Assistant 2026.3.

---

## 1. QCCU aufsetzen

Zwei Wege, je nachdem wie Home Assistant läuft.

### Home Assistant als Betriebssystem (HAOS) oder betreut

Dort lassen sich keine eigenen Container starten — dafür gibt es die
Erweiterung: **Einstellungen → Add-ons → Add-on-Store**, oben rechts
**Repositories**, `https://github.com/tostmann/QCCU` eintragen, dann **QCCU**
installieren und starten. Es wird nichts gebaut; das fertige Abbild kommt von
Docker Hub. Einzelheiten in [addon/README.md](../addon/README.md).

### Home Assistant im Container, oder QCCU auf einem anderen Rechner

```sh
docker run -d --name qccu --restart unless-stopped \
    -v /dev:/dev \
    --device-cgroup-rule='c 166:* rmw' --device-cgroup-rule='c 189:* rmw' \
    -v qccu-data:/data \
    -e ADVERTISE=<IP dieses Rechners> \
    -p 2000:2000 -p 2010:2010 -p 8181:8181 -p 8080:8080 -p 8082:8082 \
    tostmann/qccu
```

`ADVERTISE` ist die Adresse, unter der Home Assistant diesen Rechner erreicht.
Sie geht in die Rückruf-Anmeldung ein; ohne sie kommen Zustandsänderungen
nicht an. Alle übrigen Einstellungen (BidCoS-Zugang, eigene Ports, Übernahme
einer Anlage) stehen in der [README](../README.md#einstellungen).

📌 **Hierauf wird im Folgenden nicht mehr eingegangen:** Die Anleitung
beschreibt ab hier den Weg über die Erweiterung. Wer QCCU als Container
betreibt, öffnet die QCCU-Oberfläche statt aus dem Menü von Home Assistant
unter `http://<rechner>:8080` — sonst ist alles gleich.

Der erste Start lädt das debmatic-Paket (75 MB), liest die
Gerätebeschreibungen aus und verwirft es wieder. Das dauert einige Minuten und
passiert genau einmal.

---

## 2. Stick-Firmware einspielen

**QCCU steht im Menü von Home Assistant** — die Oberfläche läuft darin, ohne
eigene Adresse und ohne zweite Anmeldung. Fehlt der Eintrag in der
Seitenleiste: Einstellungen → Add-ons → QCCU → *In Seitenleiste anzeigen*
einschalten; von der Add-on-Seite aus geht es auch mit **Öffnen**.

Dort führt *Stick-Firmware* durch das Einspielen. Ein fabrikneuer Stick meldet
sich im Bootlader und ist seriell noch gar nicht zu sehen — QCCU startet
trotzdem, damit die Oberfläche erreichbar ist.

---

## 3. Integration einrichten

**Die Integration kommt über HACS.** In Home Assistant: HACS öffnen →
Integrationen → **Homematic(IP) Local** suchen und installieren, danach Home
Assistant neu starten. (HACS selbst wird einmalig über seine eigene Anleitung
eingerichtet.)

⚠️ **QCCU ist keine von der Integration unterstützte Zentrale.** Unterstützt
sind dort CCU2/3, OpenCCU, debmatic und Homegear. Der Autor der Integration hat
uns ausdrücklich gebeten, das deutlich zu sagen — und das ist auch fair: unter
QCCU liegt weder ReGaHss noch HMIPServer, sondern eine eigene Nachbildung.
**Probleme mit QCCU gehören deshalb hierher** (Issues im QCCU-Repository),
nicht in die Repositories von `homematicip_local` oder `aiohomematic`; Meldungen
zu QCCU-Setups werden dort ohne inhaltliche Bewertung geschlossen. Wer prüfen
will, ob ein Fehler an QCCU liegt, hängt die Integration testweise an eine
echte CCU oder an OpenCCU.

(Eine Zeitlang hat die Erweiterung die Integration mitgeliefert. Das ist seit
2026.8.22 wieder draußen — auf Bitte des Autors, und mit guten Gründen: zwei
Quellen für dasselbe Verzeichnis führen zu Zuständen, die niemand
nachvollziehen kann.)

**QCCU sagt es auch dort, wo du suchst.** Ein frisch angelerntes Gerät steht
so lange im **Posteingang** der Integration (`sensor.<name>_inbox`), bis du es
in den Reparaturen aufgenommen hast — mit genau diesem Satz als Name. Wer also
angelernt hat und das Gerät nicht findet, sieht dort, was noch fehlt, ohne die
QCCU-Oberfläche zu öffnen. Der Eintrag verschwindet von selbst, sobald die
Bestätigung erfolgt ist.

Danach: **Einstellungen → Geräte & Dienste → Integration hinzufügen →
Homematic(IP) Local.** Dann:

| Feld | Wert |
|---|---|
| Instanzname | frei wählbar |
| Host | **`local-qccu`** — so heißt die Erweiterung im Netz von Home Assistant, eine IP braucht es nicht. (Container-Betrieb: IP oder Name des Rechners.) |
| Benutzer / Kennwort | beliebig, aber nicht leer |
| Schnittstellen | **nur `HmIP-RF`** anhaken |
| Eigene Ports setzen | **anhaken — das ist Pflicht, nicht Kür** |
| JSON-Port | **8082** |
| HmIP-RF-Port | **2010** |

⚠️ **Ohne den Haken bei „Eigene Ports setzen" schlägt es fehl**, und zwar mit
einer Meldung, die nicht nach der Ursache klingt:
`RPC error on http://<host>/api/homematic.cgi: ClientConnectorError during
Session.login`. Die Adresse darin hat keinen Port — dann versucht Home
Assistant **Port 80**, weil eine Zentrale von eQ-3 dort antwortet. QCCU nicht:
seine JSON-Auskunft liegt auf 8082.

Dieselben Angaben stehen in der Weboberfläche unter *„Home Assistant
anbinden"* — dort mit den tatsächlich laufenden Ports, falls jemand sie
geändert hat.

**Warum zwei Ports:** Home Assistant redet über zwei Wege gleichzeitig.
XML-RPC (2010) trägt die Gerätedaten und den Rückruf, JSON-RPC (8082) die
Anmeldung, Seriennummer, Version und die Frage, welche Schnittstellen es
überhaupt gibt. Fehlt der zweite, kommt der erste nicht zustande.

QCCU prüft **keine** Zugangsdaten — die Felder müssen nur ausgefüllt sein.

---

## 4. Was danach erscheint

Für die Zentrale:

* `binary_sensor.<name>_connectivity_hmip_rf` — steht die Verbindung
* `button.<name>_anlernmodus_hmip_rf_aktivieren` — Anlernfenster öffnen
* `sensor.<name>_anlernmodus_hmip_rf_dauer` — Restzeit des Fensters
* `sensor.<name>_posteingang` — Zahl der Geräte, die angelernt werden wollen;
  die Einträge selbst stehen im Attribut `devices`
* `button.<name>_backup_erstellen` — legt die Integration bei jeder Zentrale
  an. QCCU hat keine Sicherungsfunktion; ein Druck endet mit der Meldung
  „Failed to create and download CCU backup". Gesichert wird `/data/state`,
  siehe [README](../README.md#sichern-und-ersatz).

Dazu Sammelwerte der Integration (Verbindungslatenz, Systemzustand, Service-
und Alarmmeldungen); die letzten beiden bleiben bei QCCU auf 0.

Für jedes angelernte Gerät ein Eintrag unter **Geräte** mit seinen Kanälen.
Bei einer Schaltsteckdose etwa ein `switch` für den Schaltkanal und ein
`binary_sensor` für die Relais-Rückmeldung.

Viele Datenpunkte legt die Integration bewusst **deaktiviert** an
(Wartungswerte, Fehlerzähler). Sie stehen im Geräte-Dialog unter „+N Entitäten
nicht angezeigt" und lassen sich einzeln einschalten. Dort landen auch die
Werte, die ein Gerät im Wartungskanal von sich aus mitschickt und die QCCU
deutet: **`ACTUAL_TEMPERATURE`** — bei einer Schaltsteckdose die
Eigenerwärmung der Elektronik, nicht die Raumtemperatur — und
**`ERROR_CODE`**.

---

## 5. Gerät anlernen

Der Ablauf hat **zwei Teile**, und der erste lässt sich nicht umgehen.

### a) Den Schlüssel vom Aufkleber hinterlegen

Eine Zentrale von eQ-3 beschafft sich den Geräteschlüssel selbst; QCCU kann
das nicht und muss ihn vom Aufkleber bekommen — einmal je Gerät. Zwei Wege:

**In der QCCU-Oberfläche** (im Menü von Home Assistant) → **Gerät anlernen**
→ Key eintragen (26 Zeichen; alternativ die 32 Hexziffern). Bindestriche und
Leerzeichen sind egal — abtippen wie aufgedruckt genügt. Die Zeichen **D, I, O
und V** kommen auf dem Aufkleber nicht vor; was danach aussieht, ist 0, 1, 0
bzw. U.

**Oder am Anlern-Aufruf selbst.** Der JSON-RPC-Aufruf, mit dem eine Zentrale
das Anlernfenster öffnet, hat dafür ein Feld `key` — QCCU wertet es aus. Die
Integration füllt es heute nicht (`aiohomematic` sendet es immer leer), aber
Home Assistant kann den Aufruf selbst schicken, mit einem `rest_command` in
der `configuration.yaml`:

```yaml
rest_command:
  qccu_anlernen:
    url: "http://homeassistant.local:8082/api/homematic.cgi"
    method: POST
    content_type: "application/json"
    payload: >-
      {"version":"1.1","id":1,"method":"Interface.setInstallModeHMIP",
       "params":{"interface":"HmIP-RF","on":true,"time":120,
                 "key":"{{ key }}"}}
```

Als Adresse dieselbe wie bei der Integration (Schritt 3). Aufgerufen wird er
mit dem Aufkleber als Feld — `key: 3YP6E-WCPU3-…`; damit lässt sich ein Gerät
vollständig aus Home Assistant heraus anlernen.

Ohne Schlüssel — weder hinterlegt noch am Aufruf — lehnt QCCU das Anlernen ab
und sagt auch, warum.

### b) Anlernfenster öffnen — von wo aus man mag

Entweder gleich in der QCCU-Oberfläche, oder in Home Assistant über
`button.<name>_anlernmodus_hmip_rf_aktivieren` (der Knopf braucht einen
hinterlegten Schlüssel; wer den `rest_command` oben benutzt, öffnet damit
das Fenster ohnehin schon). Dann die Anlerntaste am Gerät drücken.

Meldet sich ein Gerät, das QCCU nicht kennt, erscheint es im **Posteingang**
(`sensor.<name>_posteingang`, Einträge im Attribut `devices`) — mit
Gerätetyp und dem Hinweis, was noch fehlt. Der Posteingang zeigt nur Geräte,
die angelernt werden *wollen*, nicht jeden Funkverkehr in Reichweite; ein
Neustart der Zentrale leert ihn, und Einträge verfallen nach einer Stunde.

### c) Das Gerät in Home Assistant bestätigen

⚠️ Ein Gerät, das **nach** dem Einrichten der Integration angelernt wird,
legt Home Assistant nicht von selbst an. Die Integration stellt es zurück und
meldet es unter **Einstellungen → System → Reparaturen** („Neues Gerät …");
dort einen Namen vergeben und bestätigen — erst dann entstehen Schalter,
Sensoren und die Gerätekarte. Solange nichts erscheint, ist das der Grund,
kein Funkproblem. Wer alle zurückgestellten Geräte auf einmal übernehmen
will, ruft den Dienst **`homematicip_local.confirm_all_delayed_devices`**
auf. (Geräte, die beim Einrichten der Integration schon angelernt waren,
erscheinen sofort.)

**Warum die Integration das tut:** Sie will den Namen einmal vergeben lassen,
*bevor* die Entitäten entstehen — die Docstring ihres Reparatur-Flusses sagt
es wörtlich: „Fix flow for delayed devices: allows naming the device before
adding it". Der Grund liegt auf der Hand: die Entitäts-IDs werden beim Anlegen
aus dem Namen gebildet, und ohne diesen Schritt trägt jede von ihnen für immer
die Seriennummer. Abschalten lässt sich die Verzögerung nicht; im Code der
Integration steht sie fest verdrahtet (`delay_new_device_creation=True`).

Der vergebene Name geht dabei **in die Zentrale zurück** (`Device.setName`
bzw. `Channel.setName`). QCCU führt ihn seit **2026.8.18**: er steht danach in
`Device.listAllDetail` und in der ReGa-Auskunft, überlebt Neustarts und
verschwindet mit dem Gerät, wenn es gelöscht wird. Ältere Fassungen wiesen
beide Methoden ab — der eingegebene Name ging dann verloren, und im Protokoll
stand `CHECK_SUPPORTED_METHODS: … Channel.setName, Device.setName`.

Wer stattdessen den Dienst `confirm_all_delayed_devices` benutzt, übernimmt
alle zurückgestellten Geräte **ohne** Namen — sie behalten „Typ + Adresse".
Nachträglich umbenennen geht in Home Assistant weiterhin, ändert aber nur die
dortige Anzeige; in QCCU landet der Name nur über den Reparatur-Dialog.

### d) Warum der Knopf „Anlernmodus aktivieren" in Home Assistant absagt

Die Integration legt zu jeder Zentrale einen Knopf **„Anlernmodus aktivieren"**
an. Bei QCCU antwortet er mit einer Absage:

> Anlernen nicht möglich: kein Geräteschlüssel hinterlegt. Entweder den
> Aufkleber des Geräts in der QCCU-Oberfläche eintragen, oder ihn diesem Aufruf
> im Feld `key` mitgeben (26 Zeichen vom Aufkleber oder 32 Hexziffern).

Das ist kein Fehler, sondern die einzige ehrliche Antwort. Ein HmIP-Gerät lernt
sich **nur mit seinem Geräteschlüssel** an — der steckt im Aufkleber (die
26 Zeichen unter dem QR-Code). Ohne ihn kann das Anlernfenster offen stehen,
solange es will: es kommt nichts zustande. Ein zufriedenes „ok" zurückzugeben
wäre die schlechtere Auskunft — der Anwender drückt den Knopf und wartet dann
vergeblich.

**Warum die Integration den Schlüssel nicht mitschickt:** Am Protokoll liegt es
nicht. Der Aufruf `Interface.setInstallModeHMIP` sieht die Felder `key` und
`keymode` ausdrücklich vor — die Oberfläche einer CCU füllt sie, wenn man dort
den Aufkleber einträgt. aiohomematic schickt sie **immer leer**
(`aiohomematic/client/json_rpc.py`, `set_install_mode_hmip`: `KEY: ""`,
`KEYMODE: ""`), weil die Integration keine Eingabe dafür hat: der Knopf ist ein
Knopf, kein Formular. Bei einer echten CCU tippt man den Aufkleber in deren
eigene Oberfläche — die Integration ist der Client einer Zentrale, nicht das
Anlernwerkzeug. Eine ausdrückliche Begründung der Autoren dazu ist uns nicht
bekannt; belegbar ist nur das Verhalten im Code.

**Der normale Weg** ist deshalb die QCCU-Oberfläche: Aufkleber eintragen,
Fenster öffnen, Taste am Gerät drücken. Wer den Knopf in Home Assistant
benutzen will, hinterlegt den Schlüssel vorher dort — oder schickt den Aufruf
mit gefülltem `key` selbst, wie in Schritt a) gezeigt. Beides ist nachgeprüft:
mit Schlüssel öffnet das Fenster und meldet „Quelle: Aufkleber".

### e) Ein neu angelerntes Gerät erscheint nicht in Home Assistant

Steht das Gerät in QCCU, kommt in Home Assistant aber weder eine Entität noch
eine Reparatur, dann hat die Integration die Meldung verworfen, **weil sie die
Adresse in ihrem Zwischenspeicher schon kennt**. Das passiert typischerweise,
wenn dasselbe Gerät vorher schon einmal angelernt und wieder entfernt wurde.
QCCU meldet in diesem Fall korrekt (im Protokoll steht
`newDevices -> <Schnittstelle>: <Adresse>`), es passiert nur nichts damit.

Abhilfe: Dienst **`homematicip_local.clear_cache`** aufrufen und die Integration
neu laden. Danach erscheint das Gerät als Reparatur und lässt sich mit Namen
bestätigen. (Am Aufbau nachgestellt; ob das Leeren allein genügt oder das
Neuladen den Ausschlag gibt, haben wir nicht getrennt gemessen.)

### f) Ein Gerät wieder entfernen — in QCCU, nicht in Home Assistant

⚠️ **Das Gerät in Home Assistant zu löschen entfernt es nicht.** Die
Integration räumt dabei nur bei sich auf: `delete_device` in aiohomematic
löscht Geräteregister und Zwischenspeicher und ruft die Zentrale überhaupt
nicht. Nachgeprüft am Aufbau — nach dem Löschen in Home Assistant führte QCCU
das Gerät unverändert weiter, es ging kein einziges Funktelegramm hinaus, und
beim nächsten Neuladen der Integration war es sofort wieder da.

**Richtig ist der Weg über die QCCU-Oberfläche** (Gerät → entfernen). Dann
passiert dreierlei: das Gerät bekommt einen Funk-Ausschluss und weiß damit
selbst, dass es entlassen ist (es lässt sich danach ohne Werksreset wieder
anlernen — im Protokoll steht `Ausschluss <Adresse>: bestaetigt`, wenn es
quittiert hat); QCCU meldet die Entfernung allen Gegenstellen; und in Home
Assistant verschwinden Gerät und Entitäten daraufhin von selbst.

Steht in Home Assistant gerade eine Reparatur „Neues Gerät …" offen, während
in QCCU gelöscht wird, bleibt der Eintrag stehen. Er ist harmlos: bestätigt man
ihn, entsteht kein Gerät — die Zentrale kennt die Adresse ja nicht mehr —, und
die Meldung verschwindet.

---

## 6. Wenn etwas nicht geht

**`ClientConnectorError during Session.login`**
Der Port fehlt. Siehe [Schritt 3](#3-integration-einrichten) — „Eigene Ports
setzen" anhaken und JSON auf 8082 stellen. Gegenprobe aus Home Assistant
heraus: `local-qccu:8082` muss antworten, `local-qccu` allein nicht.

**„Interface: HmIP-RF is not available for the backend"**
Der JSON-Port stimmt nicht oder ist abgeschaltet. QCCU nennt seine
Schnittstelle über JSON-RPC; kommt die Auskunft nicht an, verwirft Home
Assistant den Zugang, noch bevor ein Gerät geholt wird. Prüfen:
`curl -X POST http://<rechner>:8082/api/homematic.cgi -d '{"version":"1.1","id":1,"method":"Interface.listInterfaces","params":{}}'`
— die Antwort muss `HmIP-RF` und den XML-RPC-Port nennen.

**Das Gerät erscheint, aber ohne Entitäten**
Fast immer ein Tabellenstand von vor 2026.8.1. Home Assistant liest
Datentyp und Grenzen jedes Parameters ungeprüft; fehlt bei einem einzigen
etwas, verwirft es das ganze Gerät. Abhilfe: QCCU aktualisieren, Tabellen neu
anlegen (`docker run --rm -v qccu-data:/data tostmann/qccu setup`) und in Home
Assistant den Dienst **`homematicip_local.clear_cache`** aufrufen — die
Integration hält die Parameterbeschreibungen sonst dauerhaft fest.

**Kein Empfangspegel, keine Temperatur, kein Sendezeitkonto zu sehen**
Diese Werte legt die Integration als **Diagnose-Entitäten an und schaltet sie
von sich aus ab** (`disabled_by: integration`) — das ist ihre Voreinstellung,
unabhängig von QCCU. Sie stehen in der Geräteansicht unter „Diagnose"
(ausgeblendete Entitäten einblenden) und lassen sich dort einzeln aktivieren:
`…_rssi_device`, `…_duty_cycle`, `…_temperature`, die Zeitprofil-Schalter.
QCCU liefert den Pegel seit 2026.8.16 zu jedem empfangenen Frame — auch aus
Quittungen und Nachbarschaftsmeldungen, damit er nicht einschläft, wenn das
Gerät gerade nichts zu sagen hat.

⚠️ **`RSSI_PEER` bleibt leer.** Das wäre der Pegel, mit dem das *Gerät* die
Zentrale hört; er steht in keinem Frame, den es sendet. Eine Zahl dort wäre
erfunden.

**„Der Stick hat keinen Netzwerkschlüssel"**
Fast immer nach einem Stick-Wechsel oder einem Werksreset des Sticks. Sind
noch Geräte eingetragen, erzeugt QCCU absichtlich keinen neuen — er würde sie
alle aussperren. Zwei Wege, beide in der Warnkarte: den bisherigen Stick
zurückstecken, oder dort **„Geräte verwerfen und neu beginnen"** drücken (die
Geräte brauchen danach je einen Werksreset). Ohne eingetragene Geräte genügt
ein Neustart, dann entsteht der Schlüssel von selbst.

**Sensoren ohne Einheit, Aufzählungen ohne Auswahl**
Ebenfalls ein alter Tabellenstand, gleiche Abhilfe.

**„Inkonsistenz bei Geräte-Paramsets erkannt … Parameter im Schema, die nicht
im tatsächlichen MASTER-Paramset vorhanden sind"**
Ein Tabellenstand bis 2026.8.12: dort wurden alle Fassungen eines Kanaltyps
zu einer Liste verschmolzen — eine Schaltsteckdose bekam 1087
Konfigurationsparameter angeboten, darunter Farbverläufe, und zu keinem einen
Wert. Seit 2026.8.13 liefert QCCU je Kanal genau das Paramset, das auch eine
Zentrale von eQ-3 liefert (an einer HmIP-PS-2 über alle Kanäle nachgemessen:
345 beschrieben, 345 Werte). Die Tabellen werden beim ersten Start der neuen
Fassung von selbst neu angelegt (dafür braucht der Rechner einmal Netz zum
Paketbezug); danach in Home Assistant den Dienst
**`homematicip_local.clear_cache`** aufrufen und die Integration neu laden.

**Der Anlernknopf meldet einen Fehler**
Dann ist kein Schlüssel hinterlegt — siehe [Schritt 5a](#a-den-schlüssel-vom-aufkleber-hinterlegen).
Die Meldung nennt es ausdrücklich; ein stilles „erledigt" wäre die schlechtere
Auskunft, weil man sonst auf ein Gerät wartet, das nie erscheint.

**Nach einem Neustart von QCCU bleibt „Connectivity" aus**
Die Integration merkt den Wiederanlauf nicht von selbst — sie hält den
Verbindungsverlust fest, auch wenn die Zentrale längst wieder antwortet
(Schalten funktioniert dann trotzdem, der Sensor steht nur falsch). Abhilfe:
**Einstellungen → Geräte & Dienste → Homematic(IP) Local → ⋮ → Neu laden.**
Danach meldet sich die Integration neu an, bekommt den Gerätebestand und der
Sensor steht wieder auf *verbunden*. (Am Aufbau gemessen; in FHEM entspricht
dem `set ccu rpcserver off/on`.)

**Zustände ändern sich nicht, Schalten geht aber**
Der Rückruf kommt nicht an. `ADVERTISE` muss die Adresse tragen, unter der
Home Assistant den QCCU-Rechner erreicht.

**Die Oberfläche meldet „Gerätebeschreibungen fehlen"**
Der Paketbezug beim ersten Start ist gescheitert. QCCU läuft, führt aber keine
Geräte — Home Assistant sähe eine Zentrale ohne Inhalt. Bei jedem Start wird
der Bezug erneut versucht: **die Erweiterung neu starten**, sobald das Netz
steht. Wer QCCU als Container betreibt, kann ihn auch von Hand nachholen
(`docker run --rm --network host -v qccu-data:/data tostmann/qccu setup`);
zur häufigsten Ursache dort (MTU) siehe [README](../README.md#starten).

**Im Protokoll: „JSON-RPC … nicht moeglich: Address already in use"**
Der Port ist belegt. QCCU läuft trotzdem weiter — XML-RPC, ReGa und der
CUL-Zugang sind davon unberührt —, aber Home Assistant findet die Zentrale
nicht. Anderen Port setzen (`JSON_PORT`) und in der Integration nachziehen.

---

## 7. Was Home Assistant über QCCU nicht kann

* **BidCoS/AskSin-Geräte.** Sie laufen über den CUL-Zugang (Port 2000) und
  damit über FHEM; in Home Assistant erscheinen sie nicht.
* **Gerätekonfiguration (MASTER-Parameter) und Wochenprofile.**
* **Firmware-Aktualisierung der Geräte.**
* **Sicherungen und Systemaktualisierung der Zentrale.** QCCU meldet sich
  als etwas anderes als eine CCU; die Integration bietet daraufhin keine
  Systemaktualisierung an. Den Knopf „Backup erstellen" legt sie trotzdem an
  (siehe oben) — er meldet einen Fehler.
* **Programme, Systemvariablen, Räume, Gewerke, Direktverknüpfungen** — die
  Auskünfte dazu sind leer.

FHEM und Home Assistant dürfen gleichzeitig angeschlossen sein; beide bekommen
ihre Gerätemeldungen — siehe [FHEM.md](FHEM.md). Was QCCU nach außen genau
anbietet, steht in [SCHNITTSTELLEN.md](SCHNITTSTELLEN.md).
