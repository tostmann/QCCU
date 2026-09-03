# QCCU als Home-Assistant-Erweiterung

Wer Home Assistant als Betriebssystem betreibt (HAOS) oder als betreute
Installation, kann keine eigenen Container starten. Für die ist QCCU hier als
Erweiterung eingerichtet.

## Einrichten

1. In Home Assistant: **Einstellungen → Add-ons → Add-on-Store**, oben rechts
   **Repositories**, und `https://github.com/tostmann/QCCU` eintragen.
2. **QCCU** installieren und starten. Es wird nichts gebaut — das fertige
   Abbild kommt von Docker Hub.
3. Die Oberfläche steht danach **im Menü von Home Assistant** (Eintrag QCCU);
   sie läuft darin, ohne eigene Adresse und ohne zweite Anmeldung. Fehlt der
   Eintrag: auf der Add-on-Seite *In Seitenleiste anzeigen* einschalten. Dort
   wird angelernt.

## Einstellungen

| Feld | Bedeutung |
|---|---|
| `serial` | Leer lassen. QCCU sucht seinen Stick am Namen. Nur nötig, wenn mehrere busware-Sticks stecken. |
| `own_addr` | Eigene Funkadresse (6 Hexziffern). Ohne Angabe wird beim ersten Start eine gewürfelt und gemerkt. |
| `advertise` | Die Adresse, unter der die Gegenstelle diesen Rechner erreicht. ⚠️ Ohne sie findet FHEM/HMCCU den XML-RPC-Dienst nicht — der Rückruf ginge ins Leere. |
| `cul_port` | TCP-Zugang im culfw-Stil für BidCoS/AskSin. `0` schaltet ihn ab. |
| `json_port` | JSON-RPC für Home Assistant. `0` schaltet ihn ab. |

## Home Assistant anbinden

Die Erweiterung ist **nicht** die Anbindung selbst — sie ist die Zentrale.
Angebunden wird über die HACS-Integration **Homematic(IP) Local**:

* Host: **`local-qccu`** — unter diesem Namen ist die Erweiterung im Netz von
  Home Assistant erreichbar, eine IP braucht es nicht
* Benutzer/Kennwort: beliebig — QCCU prüft keine Zugangsdaten
* Bei den Schnittstellen **nur `HmIP-RF`** anhaken
* **Eigene Ports setzen anhaken** (Pflicht): **JSON `8082`**, **HmIP-RF `2010`**

⚠️ Ohne den Port-Haken versucht Home Assistant Port 80 — dort antwortet eine
echte CCU, QCCU aber nicht. Die Meldung lautet dann
`ClientConnectorError during Session.login`.

## Gerät anlernen

Zwei Teile:

1. **Schlüssel vom Aufkleber beibringen** — QCCU im Menü öffnen → *Gerät
   anlernen* → Key vom Aufkleber des Geräts. Eine Zentrale von eQ-3 beschafft sich den
   Geräteschlüssel selbst; QCCU kann das nicht. Der Anlernknopf der
   Integration übergibt keinen Schlüssel — wer ohne die Web-UI auskommen
   will, schickt den Anlern-Aufruf selbst, mit dem Key im Feld `key`
   (Vorlage in der Anleitung unten).
2. **Anlernfenster öffnen** — hier oder in Home Assistant über
   `button.<name>_anlernmodus_hmip_rf_aktivieren`. Dann die Anlerntaste am
   Gerät drücken.

Ohne Schlüssel lehnt QCCU das Anlernen ab und nennt den Grund.

## Die Oberfläche im Menü — und der Port daneben

Die Oberfläche läuft über *Ingress*: Home Assistant reicht die Anfragen an die
Erweiterung weiter, prüft dabei die Anmeldung und braucht keinen offenen Port
nach außen — das ist auch von unterwegs erreichbar, wo Port 8080 es nicht ist.
Der Port **bleibt trotzdem veröffentlicht**: für den Zugriff von einem anderen
Rechner, für die Fehlersuche und für den Fall, dass Home Assistant selbst
gerade nicht läuft.

## Wenn ein Port schon belegt ist

Läuft auf demselben Rechner schon eine CCU oder OCCU, belegt sie 8080, 2010,
8181, 2000 und 2001 — die Erweiterung startet dann nicht und meldet
*„Cannot start app … because port 8080 is already in use"*.

Der Weg dahin führt **nicht** über eine Einstellung der Erweiterung, sondern
über **Konfiguration → Netzwerk** in ihrer eigenen Seite: dort steht je Dienst
der Port, unter dem er von außen erreichbar ist. Trage dort einen freien Port
ein (etwa 18080 statt 8080) oder lasse das Feld leer, dann wird der Dienst gar
nicht nach außen gereicht. Innen bleibt alles, wie es ist — die Oberfläche im
Menü läuft über Ingress und funktioniert unabhängig davon weiter.

⚠️ Bis 2026.9.3 gab es dafür eine Einstellung „Ausweichports", die alle Dienste
im Container um 10000 verschob. Sie verschob auch die Oberfläche, während das
Menü fest auf 8080 zeigt — danach war die Oberfläche tot und ließ sich nur
über die Einstellungen zurückholen. Die Einstellung ist deshalb entfallen.

## Warum zwei Ports

Home Assistant redet über **zwei** Wege gleichzeitig mit einer Zentrale:
XML-RPC (2010) für die Gerätedaten und den Rückruf, JSON-RPC (8082) für
Anmeldung, Seriennummer und die Frage, welche Schnittstellen es gibt. Fehlt
der zweite, kommt der erste nicht zustande.

## FHEM läuft parallel weiter

XML-RPC (2010), ReGa (8181) und der CUL-Zugang (2000) bleiben unverändert.
Home Assistant und FHEM/HMCCU können gleichzeitig angemeldet sein; beide
bekommen ihre Gerätemeldungen.

## Der Stick

`uart: true` bindet die seriellen Geräte ein — dort findet QCCU seinen Stick.
`usb: true` kommt hinzu, weil der Stick zum **Einspielen der Firmware** in den
Bootlader geht und sich dort mit einer anderen USB-Kennung meldet; ohne den
rohen USB-Zugang schlüge genau dieser erste Schritt fehl.

Meldet die Oberfläche trotzdem *kein Zugriff auf den USB-Bus*, hat der Rechner
darunter keinen: läuft Home Assistant in einer virtuellen Maschine, muss der
Stick erst dorthin durchgereicht werden.

## Mehr

Der ganze Weg samt Fehlersuche steht in
[docs/HOMEASSISTANT.md](https://github.com/tostmann/QCCU/blob/main/docs/HOMEASSISTANT.md);
was QCCU insgesamt ist und kann, in der
[README](https://github.com/tostmann/QCCU/blob/main/README.md).
