# QCCU in Home Assistant

Vom leeren Stick bis zur geschalteten Steckdose. Wer QCCU schon betreibt,
springt zu [Integration einrichten](#3-integration-einrichten). Was QCCU
grundsätzlich ist und kann, steht in der [README](../README.md); FHEM daneben
in [FHEM.md](FHEM.md).

Home Assistant führt die Geräte über die Integration **Homematic(IP) Local**
(HACS, auf `aiohomematic`). QCCU gibt sich ihr gegenüber als Zentrale aus —
ein eigenes Modul braucht es nicht. Geprüft mit Homematic(IP) Local 2.7.0 /
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

**Einstellungen → Geräte & Dienste → Integration hinzufügen → Homematic(IP)
Local.** Dann:

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
→ Key eintragen (26 Zeichen; alternativ die 32 Hexziffern).

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

**Sensoren ohne Einheit, Aufzählungen ohne Auswahl**
Ebenfalls ein alter Tabellenstand, gleiche Abhilfe.

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
