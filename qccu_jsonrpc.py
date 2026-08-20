#!/usr/bin/env python3
"""JSON-RPC-Auskunft der QCCU — der Weg, auf dem Home Assistant sie sieht.

WOFUER DAS DA IST
-----------------
`aiohomematic` — die Bibliothek hinter Home Assistants „Homematic(IP) Local" —
redet mit einer Zentrale ueber ZWEI Wege gleichzeitig, und beide werden
gebraucht:

  XML-RPC (Port 2010)   die Geraetedaten. Anmeldung mit `init()`, danach ruft
                        die ZENTRALE zurueck, wenn sich ein Wert aendert.
                        Das kann die QCCU laengst — FHEM/HMCCU laeuft darueber.
  JSON-RPC (diese Datei) die Metadaten drumherum: Anmeldung, Seriennummer,
                        Version, welche Schnittstellen es ueberhaupt gibt,
                        Namen der Geraete, Sammelabruf aller Werte.

Ohne den zweiten Weg kommt der erste nicht zustande: `initialize()` holt sich
zuerst die Systemauskunft, und wenn dort die eigene Schnittstelle nicht
auftaucht, wird der Client wieder verworfen („Interface: HmIP-RF is not
available for the backend") — noch bevor ein einziges Geraet geholt wird.

WARUM `HmIP-RF` UND NICHT `CCU-Jack`
------------------------------------
aiohomematic entscheidet allein am NAMEN der Schnittstelle, welchen Weg es
geht: `CCU-Jack` und `CUxD` laufen ohne Rueckkanal, rein ueber JSON-RPC.
Das waere bequemer — aber `CCU-Jack` ist ein fremdes Projekt (mdzio/ccu-jack),
und aiohomematic verlangt ohnehin zusaetzlich eine Schnittstelle aus
{HmIP-RF, BidCos-RF, BidCos-Wired}, sonst startet die Zentrale gar nicht.

Deshalb meldet sich die QCCU unter ihrem eigenen, zutreffenden Namen:
**HmIP-RF**. Der Rueckkanal ist kein Nachteil, sondern der bessere Weg — die
QCCU hat ihn (`init`, `ping`/`PONG`, `event`), und Aenderungen kommen damit
sofort statt im Abrufrhythmus.

WAS SIE NICHT IST
-----------------
Kein Ersatz fuer die XML-RPC-Schnittstelle, sondern ihre Ergaenzung. FHEM/HMCCU
nutzen weiter Port 2010 und `/tclrega.exe`; beides bleibt unangetastet.

WAS HIER NEU ENTSTAND
---------------------
  Interface.isPresent      Steht der Funk?
  Interface.listInterfaces ⚠️ MUSS die eigene Schnittstelle nennen (s.o.)
  Device.listAllDetail     Namen und Kennungen aller Geraete und Kanaele
  ReGa.runScript           die Skript-Auskuenfte. ⚠️ Das SKRIPT gehoert dem
                           Client, nicht uns — wir fuehren es nicht aus,
                           sondern erkennen an seinem Kopf, welche Auskunft
                           es holen will. Denselben Schluss gab es bei HMCCU.

FORMATE, DIE ABWEICHEN
----------------------
`getParamsetDescription` liefert hier eine LISTE von Objekten mit `NAME`-Feld,
nicht — wie ueber XML-RPC — ein Verzeichnis. aiohomematic baut daraus selbst
wieder ein Verzeichnis (`{data["NAME"]: ... for data in json_result}`).
Wer das uebersieht, bekommt eine leere Geraeteliste ohne Fehlermeldung.
Die Geraetebeschreibung wiederum kommt hier kleingeschrieben (`type`,
`address`, `paramsets`) statt in Grossbuchstaben.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote


def _version():
    """Die QCCU-Version, ohne beim Laden von qccu abzuhaengen."""
    try:
        from qccu import VERSION
    except ImportError:
        return "QCCU"
    return VERSION

# Der Name, unter dem sich die QCCU meldet — derselbe, den sie ueber ReGa und
# in der Schnittstellen-Zeile schon fuehrt (`QCCU.interface_name`). Er MUSS zu
# dem passen, was die Gegenstelle in ihrer Einrichtung angehakt hat, sonst
# findet sie ihre Schnittstelle in `listInterfaces` nicht wieder.
INTERFACE_NAME = "HmIP-RF"

# Der Pfad, unter dem eine Zentrale ihre JSON-RPC-Auskunft anbietet.
API_PATH = "/api/homematic.cgi"

# ⚠️ Datentypen werden hier NICHT stillschweigend umgeschrieben. Sie gehoeren
# in der Tabelle richtig (`tables/build_paramsets.py`, `vereinheitliche_typen`);
# eine Ausgabeschicht, die Tabellenfehler repariert, verdeckt sie nur — der
# naechste Tabellenbau bringt sie zurueck, und wer die Tabelle anderswo
# benutzt, faellt herein.
#
# Gemeldet wird der Fall trotzdem, denn von aussen ist er nicht zu sehen:
# `aiohomematic` kennt nur die Namen unten, und ein unbekannter Typ (z.B. das
# `BOOLEAN` aus dem Herstellerarchiv) laesst es beim Anlegen der Datenpunkte
# abbrechen („KeyError: TYPE"), OHNE dass ein Aufruf fehlschlaegt: die
# Geraeteliste kommt an, die Datenpunkte bleiben aus. Ohne diesen Hinweis
# sucht man an der falschen Stelle.
BEKANNTE_TYPEN = frozenset({"ACTION", "BOOL", "DUMMY", "ENUM", "FLOAT",
                            "INTEGER", "STRING", "EMPTY"})
_gemeldete_typen = set()
_gemeldete_luecken = set()

# Was wir koennen — `system.listMethods` gibt das heraus.
#
# ⚠️ Diese Liste ist KEINE Auskunft zur Zierde. Die Gegenstelle haelt ihre
# eigene dagegen und verweigert danach jeden Aufruf, den sie hier nicht
# findet: „method 'CCU.getAuthEnabled' not supported by the backend" — und
# bricht den Verbindungsaufbau ab, bevor ein einziges Geraet geholt wird.
# Wer hier eine Methode weglaesst, die er in Wahrheit beantworten koennte,
# sperrt sich selbst aus.
#
# Deshalb steht hier auch, was wir nur LEER beantworten (Programme,
# Systemvariablen, Raeume). Das ist keine Beschoenigung: die QCCU hat keine
# Programme, und eine leere Liste ist die zutreffende Auskunft darauf.
SUPPORTED_METHODS = (
    # Anmeldung und Selbstauskunft
    "Session.login",
    "Session.renew",
    "Session.logout",
    "CCU.getAuthEnabled",
    "CCU.getHttpsRedirectEnabled",
    "system.listMethods",
    "ReGa.runScript",
    # Geraete und Werte
    "Interface.isPresent",
    "Interface.listInterfaces",
    "Interface.listDevices",
    "Interface.getDeviceDescription",
    "Interface.getParamsetDescription",
    "Interface.getParamset",
    "Interface.getValue",
    "Interface.getMasterValue",
    # Namen: die Gegenstelle vergibt sie beim Anlernen und erwartet, dass die
    # Zentrale sie fuehrt (siehe set_name in qccu.py).
    "Device.setName",
    "Channel.setName",
    "Interface.setValue",
    "Interface.putParamset",
    "Device.listAllDetail",
    # Anlernen — die QCCU kann es, also sagt sie es auch
    "Interface.getInstallMode",
    "Interface.setInstallModeHMIP",
    # Auskuenfte, die bei uns leer ausfallen. Eine leere Liste ist die
    # zutreffende Antwort: die QCCU fuehrt weder Programme noch
    # Systemvariablen, Raeume oder Gewerke.
    "Interface.getSuppressedServiceMessages",
    "Channel.hasProgramIds",
    "Program.getAll",
    "Room.getAll",
    "Subsection.getAll",
    "SysVar.getAll",
)

# ⚠️ BEWUSST NICHT gemeldet — wir koennten sie nur mit einem Fehler
# beantworten, und ein gemeldetes Koennen, das dann scheitert, ist schlechter
# als ein ehrliches Schweigen. aiohomematic schreibt dafuer eine Warnung
# („methods not supported by the backend") und laesst den Aufruf sein:
#
#   Device.setName, Channel.setName          kein Namensregister
#   Program.execute, SysVar.getValueByName   nichts auszufuehren/abzufragen
#   SysVar.create*/set*/deleteSysVarByName   keine Systemvariablen
#   Interface.getLinkInfo/setLinkInfo        Direktverknuepfungen offen
#   Interface.suppressServiceMessages        keine Unterdrueckungsliste
#
# Wird eine davon doch einmal im Aufbau gebraucht, faellt es sofort auf:
# der Aufbau bricht ab, bevor ein Geraet erscheint (so geschehen mit
# CCU.getAuthEnabled).


class Sessions:
    """Anmeldungen. Die QCCU prueft keine Kennwoerter — sie steht im eigenen
    Netz, und die Gegenstelle erwartet trotzdem eine Kennung."""

    def __init__(self, lebensdauer=600.0):
        self._offen = {}
        self._lebensdauer = lebensdauer
        self._lock = threading.Lock()

    def anmelden(self):
        sid = secrets.token_hex(16)
        with self._lock:
            self._offen[sid] = time.time()
            self._aufraeumen()
        return sid

    def erneuern(self, sid):
        with self._lock:
            if sid in self._offen:
                self._offen[sid] = time.time()
                return sid
        # Eine abgelaufene Kennung wird nicht abgewiesen, sondern ersetzt:
        # ein Neustart der QCCU soll die Gegenstelle nicht aussperren.
        return self.anmelden()

    def abmelden(self, sid):
        with self._lock:
            self._offen.pop(sid, None)
        return True

    def _aufraeumen(self):
        grenze = time.time() - self._lebensdauer
        for sid in [s for s, t in self._offen.items() if t < grenze]:
            del self._offen[sid]


KURZ_ID = 4


def _kanal_name(adresse, typ):
    """Der Name, unter dem ein Kanal in der Auskunft steht.

    Aufbau „<Typ> <Kurzkennung>", z.B. `HM-LC-Sw1-Pl-2 2233`. Eine echte
    Zentrale fuehrt hier den vom Benutzer vergebenen Namen; solange keiner
    vergeben ist, ist das die ehrlichste Auskunft — kurz und wiedererkennbar.

    ⚠️ Der Typ, NICHT die Beschriftung. Bei BidCoS sind das zwei Dinge, und
    der rftypes-Langtext taugt nicht als Anzeigename.

    ⚠️ Gekuerzt wird nur die GERAETEadresse. Ein Kanal kommt als
    `112233:1` herein — wer die ganze Zeichenkette kuerzt, macht daraus
    `3:1`. Deshalb erst am Doppelpunkt trennen.

    ⚠️ Vier Hexstellen sind nicht garantiert eindeutig. Stehen zwei Geraete
    desselben Typs mit gleicher Endung im Haus, tragen sie denselben Namen —
    HA vergibt der zweiten Entitaet dann eine Nummer. Der Benutzer kann
    umbenennen; die Adresse bleibt der eindeutige Schluessel.
    """
    basis, trenner, kanal = adresse.partition(":")
    kurz = basis[-KURZ_ID:]
    return f"{typ} {kurz}{trenner}{kanal}"


def _ise_id(text):
    """Die Kennung fuer den POSTEINGANG — als Zeichenkette.

    ⚠️ Zwei Schnittstellen, zwei Typen, und das ist keine Schlamperei der
    Gegenstelle, sondern die Vorlage: `Device.listAllDetail` traegt die
    Kennung als ZAHL (`DeviceDetail.id: int` in aiohomematic), das
    Posteingang-Skript einer Zentrale von eq-3 schreibt sie in
    Anfuehrungszeichen (`Write('{"id":"' # oDev.ID() # '",')`) und die
    Gegenstelle nimmt sie entsprechend als ZEICHENKETTE an
    (`InboxDeviceData.device_id: str`).

    Wir lieferten sie hier als Zahl. Das Panel reicht sie beim Benennen
    unveraendert weiter, und dessen WebSocket-Befehl verlangt `str` —
    Ergebnis war ein blankes „Aktion fehlgeschlagen" im Panel und im
    HA-Protokoll: „expected str for dictionary value @ data['device_id'].
    Got 1185349867 (invalid_format)" (Dirk, 19.08.2026, 22:47).
    """
    return str(_kennung(text))


def _kennung(text):
    """Eine stabile Zahl je Adresse.

    Die Gegenstelle nutzt die Kennungen nur zum Wiedererkennen, nie zum
    Rechnen — sie muessen also lediglich eindeutig und ueber Neustarts hinweg
    gleich sein. Ein Hash der Adresse leistet das ohne Buchfuehrung."""
    h = 0
    for c in text:
        h = (h * 131 + ord(c)) & 0x7FFFFFFF
    return h or 1


class JsonRpc:
    """Die Auskunft selbst — ohne HTTP, damit sie sich pruefen laesst."""

    def __init__(self, qccu, interface=None, rpc_port=2010, hostname=None,
                 bidcos=None, bidcos_port=0):
        self.q = qccu
        # Die QCCU fuehrt ihren Schnittstellennamen schon selbst (ReGa,
        # Schnittstellen-Zeile). Zwei Quellen dafuer waeren eine zu viel.
        self.interface = (interface or getattr(qccu, "interface_name", None)
                          or INTERFACE_NAME)
        self.rpc_port = rpc_port
        # Die zweite Schnittstelle. Ohne sie verhaelt sich alles wie bisher —
        # `bidcos` ist None, und jede Abfrage landet beim HmIP-Bestand.
        self.bidcos = bidcos
        self.bidcos_port = bidcos_port or 0
        self.hostname = hostname or socket.gethostname()
        self.sessions = Sessions()

    # -- die drei neuen Auskuenfte -------------------------------------

    def is_present(self, interface=None):
        """Steht der Funk?

        Auf dem XML-RPC-Weg beantwortet ein Ping-Pong-Spiel diese Frage; das
        gibt es hier nicht (`ping_pong=False` in den Faehigkeiten dieses
        Backends). Uebrig bleibt die schlichte Frage, ob ein Funkpfad da ist.

        ⚠️ Diese Frage wird HAEUFIG gestellt — sie haengt am Verbindungstest,
        und dieser Weg prueft im Takt von Sekunden. Sie muss deshalb billig
        bleiben: `radio_state()` waere hier falsch, es liest die Zaehler AM
        STICK. Gefragt wird stattdessen, was ohne Funkverkehr zu haben ist:
        laeuft die Schleife noch, ist der Anschluss offen, und hat der Stick
        jemals seine Funkadresse genannt — letzteres steht erst, wenn er
        wirklich geantwortet hat.
        """
        if self.bidcos is not None and interface == self.bidcos.interface_name:
            # Die BidCoS-Seite steht, sobald der Funkpfad steht — sie fuehrt
            # keinen eigenen Anschluss.
            return bool(getattr(self.bidcos, "radio", None)) or bool(self.bidcos.devices)
        if interface not in (None, "", self.interface):
            return False
        radio = getattr(self.q, "radio", None)
        if radio is None:
            return False
        if getattr(radio, "_stop", False):
            return False
        ser = getattr(radio, "ser", None)
        if ser is not None and not getattr(ser, "is_open", True):
            return False
        return bool(getattr(radio, "own_addr", None))

    def _ziel(self, p):
        """Welche Schnittstelle ist gemeint? Vorgabe: die HmIP-Seite.

        ⚠️ Die Gegenstelle schickt das Feld `interface` bei jedem
        `Interface.*`-Aufruf mit. Wer es ignoriert, beantwortet BidCoS-Fragen
        aus dem HmIP-Bestand — die Adressen gibt es dort nicht, und der Client
        sieht ein leeres Geraet ohne Fehlermeldung.
        """
        name = (p or {}).get("interface")
        if self.bidcos is not None and name == self.bidcos.interface_name:
            return self.bidcos
        return self.q

    def list_all_detail(self):
        """Namen und Kennungen aller Geraete und Kanaele.

        Auf einer echten Zentrale kommt das aus der ReGa-Datenbank. Die QCCU
        hat keine, also wird die Auskunft aus dem Geraetebestand gebildet."""
        out = []
        with self.q.lock:
            geraete = list(self.q.devices.values())
        for d in geraete:
            kanaele = []
            for idx, _ctype in d.channel_list():
                addr = f"{d.address}:{idx}"
                kanaele.append({
                    "address": addr,
                    "name": self.q.name_of(addr, _kanal_name(addr, d.typname)),
                    "id": _kennung(addr),
                })
            out.append({
                "address": d.address,
                "name": self.q.name_of(d.address,
                                       _kanal_name(d.address, d.typname)),
                "id": _kennung(d.address),
                "interface": self.interface,
                "channels": kanaele,
            })
        # ⚠️ Das Feld `interface` je Geraet ist kein Schmuck: die Gegenstelle
        # setzt daraus die Zugehoerigkeit (`fetch_device_details`). Fehlt es,
        # gilt der Rueckfall der fragenden Schnittstelle — und ein Geraet, das
        # keiner Schnittstelle zugeordnet ist, wird stillschweigend BidCoS.
        if self.bidcos is not None:
            with self.bidcos.lock:
                bg = list(self.bidcos.devices.values())
            for d in bg:
                kanaele = [{"address": f"{d.address}:{idx}",
                            "name": _kanal_name(f"{d.address}:{idx}", d.typname),
                            "id": _kennung(f"{d.address}:{idx}")}
                           for idx, _ in d.channel_list()]
                out.append({
                    "address": d.address,
                    "name": _kanal_name(d.address, d.typname),
                    "id": _kennung(d.address),
                    "interface": self.bidcos.interface_name,
                    "channels": kanaele,
                })
        return out

    def _adresse_zu_kennung(self, ise_id):
        """Zu einer Kennung die Adresse suchen.

        Die Kennungen sind ein Hash der Adresse (siehe _kennung) — es gibt
        also keine Tabelle, die man befragen koennte, wohl aber den kurzen
        Weg: ueber den Bestand laufen und rechnen. Bei ein paar hundert
        Kanaelen ist das billiger als eine zweite Buchfuehrung, die mit dem
        Bestand aus dem Tritt geraten kann.
        """
        try:
            gesucht = int(ise_id)
        except (TypeError, ValueError):
            return None
        with self.q.lock:
            geraete = list(self.q.devices.values())
        for d in geraete:
            if _kennung(d.address) == gesucht:
                return d.address
            for idx, _ctype in d.channel_list():
                addr = f"{d.address}:{idx}"
                if _kennung(addr) == gesucht:
                    return addr
        return None

    def set_name(self, ise_id, name):
        """Device.setName / Channel.setName — den Namen fuehren.

        ⚠️ Frueher wies die QCCU beides ab. Das war folgerichtig, solange sie
        keine Namen fuehrte, hatte aber eine Folge, die man erst am lebenden
        Aufbau sieht: die Integration „Homematic(IP) Local" haelt jedes frisch
        angelernte Geraet zurueck, bis der Anwender ihm einen Namen gibt, und
        schreibt ihn dann hierher. Ohne diese Methode verschwand der Name.
        """
        adresse = self._adresse_zu_kennung(ise_id)
        if not adresse:
            return None, {"name": "InvalidId", "code": -32602,
                          "message": f"Kennung {ise_id} gehoert zu nichts"}
        self.q.set_name(adresse, name)
        gefuehrt = self.q.name_of(adresse)
        if getattr(self.q, "verbose", False):
            print(f"  Name {adresse} = "
                  + (f"„{gefuehrt}\u201c" if gefuehrt else "(zurueckgesetzt)"))
        return True, None

    def run_script(self, script):
        """Die Skript-Auskuenfte.

        ⚠️ Das Skript gehoert der Gegenstelle, nicht uns. Wir fuehren es nicht
        aus — wir erkennen an seinem Kopf, WELCHE Auskunft es holen will, und
        liefern sie. Ein Skript-Ausleger waere dafuer der falsche Aufwand;
        denselben Schluss gab es schon bei HMCCU.

        Jedes Skript traegt seinen Namen in der ersten Zeile (`!# name: …`),
        und die Gegenstelle schickt die Datei ungekuerzt — nur `##param##`
        wird vorher ersetzt. Daran wird erkannt.

        Was wir nicht kennen, wird LEER, aber gueltig beantwortet: eine leere
        Antwort laesst die Gegenstelle weiterlaufen, ein Fehler nicht. Welche
        Form „leer" hat, steht im jeweiligen Skript — Liste oder Verzeichnis,
        das ist nicht beliebig.
        """
        s = script or ""
        m = re.search(r"^!#\s*name:\s*(\S+)", s, re.M)
        name = m.group(1) if m else ""

        if name == "fetch_all_device_data.fn" or "sUse_Interface" in s:
            return self._alle_werte(s)
        if name == "get_backend_info.fn":
            return self._backend_info()
        if name == "get_serial.fn":
            return json.dumps({"serial": self._serial()})
        if name == "get_system_update_info.fn":
            # Wird bei uns gar nicht erst gefragt (siehe _backend_info), aber
            # wenn doch: nichts steht an.
            return json.dumps({"current_firmware": _version(), "available_firmware": "",
                               "update_available": False, "check_script_available": False})
        if name == "get_inbox_devices.fn":
            return self._posteingang()
        if name == "accept_device_in_inbox.fn":
            return self._aufnehmen(s)
        # Diese Skripte schreiben eine Liste — eine leere ist die zutreffende
        # Auskunft: die QCCU fuehrt keine Programme, Systemvariablen und
        # keine Service-/Alarmmeldungen.
        if name in ("get_service_messages.fn", "get_alarm_messages.fn",
                    "get_program_descriptions.fn",
                    "get_system_variable_descriptions.fn"):
            return "[]"
        return ""

    def _posteingang(self):
        """Was im Posteingang steht — zwei Dinge, die dasselbe Ziel haben.

        1. **Geraete, die funken, aber nicht angelernt sind.** Ohne diese
           Auskunft drueckt der Anwender den Anlernknopf und sieht nie, ob
           ueberhaupt etwas ankommt.
        2. **Frisch angelernte Geraete, die die Gegenstelle noch aufnehmen
           muss.** ⚠️ Aus einer Anwendermeldung (19.08.2026): jemand hat
           erfolgreich angelernt und das Geraet danach nicht gefunden. Die
           Integration stellt jedes frische Geraet zurueck, bis es in den
           Reparaturen bestaetigt wurde — bis dahin gibt es dort weder Schalter
           noch Sensoren. Der Posteingang ist die einzige Stelle, an der wir
           ihm das SAGEN koennen, ohne dass er unsere Oberflaeche oeffnet: die
           Integration zeigt ihn selbst an (`sensor.<name>_inbox`).

        Der Name jedes Eintrags traegt die Handlungsanweisung — er ist das
        Einzige, was in der Haussteuerung davon ankommt.
        """
        radio = getattr(self.q, "radio", None)
        liste = []
        # ⚠️ Anlernwuensche kommen hier nur hinein, wenn ein Schluessel
        # hinterlegt ist. Sonst kann die Gegenstelle mit ihrem Knopf
        # „Annehmen" nichts ausrichten — wir brauchen den Aufkleber, den sie
        # nicht hat —, und der Anwender bekommt ein blankes „Aktion
        # fehlgeschlagen" statt einer Erklaerung. Wo nichts anzunehmen ist,
        # steht auch nichts zum Annehmen; die QCCU-Oberflaeche zeigt den Ruf
        # weiterhin unter „Anlernwuensche", dort ist der Aufkleber auch
        # einzutragen.
        hat_schluessel = bool(getattr(radio, "pair_key", None))
        if hat_schluessel and radio is not None and hasattr(radio, "anlernwuensche_liste"):
            try:
                # Die eigene Oberflaeche bekommt mehr Felder als die
                # Haussteuerung; hier geht nur hinaus, was die Gegenstelle
                # liest (`InboxDeviceData` in aiohomematic, fuenf Felder).
                felder = ("id", "address", "name", "type", "interface")
                liste = [{k: e[k] for k in felder if k in e}
                         for e in radio.anlernwuensche_liste()]
                # ⚠️ `id` MUSS eine Zahl sein. Vergibt jemand im Posteingang
                # der Gegenstelle einen Namen, rechnet sie `int(device_id)`
                # (websocket_api.ws_accept_inbox_device → rename_device) — eine
                # Funkadresse wie „1aff5f" wirft dort einen ValueError, den
                # niemand faengt. Kennungen sind bei einer Zentrale von eq-3
                # immer Zahlen; das gilt hier genauso.
                for e in liste:
                    e["id"] = _ise_id(e.get("address", ""))
            except Exception:                        # noqa: BLE001
                liste = []

        # ⚠️ Zwei Faelle, je nachdem ob QCCU zurueckhaelt:
        # (a) Warteraum an (Vorgabe): das Geraet ist der Gegenstelle noch
        #     gar nicht gemeldet — hier steht es, hier wird es aufgenommen.
        # (b) Warteraum aus: es ist gemeldet und wartet auf die Bestaetigung
        #     unter „Reparaturen"; dann ist der Hinweis das Einzige, was hilft.
        wartend = getattr(self.q, "warteraum_liste", None)
        for adresse in (wartend() if wartend else []):
            geraet = self.q.devices.get(adresse)
            typ = geraet.typname if geraet is not None else "Geraet"
            name = self.q.name_of(adresse, typ)
            liste.append({
                "id": _ise_id(adresse),
                "address": adresse,
                "name": (f"{name} — angelernt, hier aufnehmen: Namen "
                         f"eintragen und auf ‚aufnehmen' druecken. Erst dann "
                         f"entstehen Schalter und Sensoren."),
                "type": typ,
                "interface": self.interface,
            })

        offen = getattr(self.q, "frisch_offen", None)
        wartend_menge = set(wartend() if wartend else [])
        for adresse in (offen() if offen else []):
            # ⚠️ Nicht doppelt: ein wartendes Geraet steht schon oben — mit
            # DERSELBEN Kennung. Zwei Zeilen fuer dasselbe Geraet sahen im
            # Panel der Gegenstelle wie zwei Karteileichen aus, und die zweite
            # liess sich nach der Aufnahme der ersten nicht mehr annehmen
            # („Aktion fehlgeschlagen", Dirk 19.08.2026).
            if adresse in wartend_menge:
                continue
            geraet = self.q.devices.get(adresse)
            typ = geraet.typname if geraet is not None else "Geraet"
            name = self.q.name_of(adresse, typ)
            liste.append({
                "id": _ise_id(adresse),
                "address": adresse,
                "name": (f"{name} — angelernt, hier noch nicht aufgenommen: in "
                         f"DIESER Liste auf ‚aufnehmen‘ druecken und dabei benennen "
                         f"(Seitenleiste → HM Device Configuration → Posteingang). "
                         f"Auch moeglich: Einstellungen → System → Reparaturen. "
                         f"Steht dort nichts: Dienst homematicip_local.clear_cache, "
                         f"dann Integration neu laden."),
                "type": typ,
                "interface": self.interface,
            })
        return json.dumps(liste, ensure_ascii=False)

    def _aufnehmen(self, skript):
        """„Geraet aufnehmen" aus dem Posteingang.

        ⚠️ Eine Zentrale von eq-3 kommt hier ohne Zutun an den Schluessel des
        Geraets; wir nicht (nachgewiesen: der Riegel ist der Hauptschluessel im
        Silizium). Ohne hinterlegten Aufkleber ist das Aufnehmen nicht moeglich
        — und das wird gesagt, nicht durch ein stilles `true` verdeckt. Ein
        vorgetaeuschter Erfolg waere die schlechtere Auskunft: der Anwender
        wartete auf ein Geraet, das nie erscheint."""
        m = re.search(r'sDeviceAddress\s*=\s*"([^"]*)"', skript)
        adresse = (m.group(1) if m else "").strip()

        # Steht die Adresse fuer ein Geraet, das wir schon fuehren, ist nichts
        # anzulernen: der Eintrag war nur der Hinweis, es in der Haussteuerung
        # aufzunehmen. Dann gilt er als gelesen.
        if adresse and adresse.upper() in self.q.devices:
            # Ein Geraet, das wir schon fuehren: entweder wartet es noch auf
            # die Freigabe (dann geht sie jetzt hinaus — das ist der
            # `ReadyConfig(true)`-Moment einer Zentrale von eq-3), oder der
            # Eintrag war nur der Hinweis, es in der Haussteuerung
            # aufzunehmen; dann gilt er als gelesen.
            aufnehmen = getattr(self.q, "aufnehmen", None)
            if aufnehmen:
                aufnehmen(adresse)
            self.q.frisch_angelernt.pop(adresse.upper(), None)
            return json.dumps({"success": True, "error": ""})

        radio = getattr(self.q, "radio", None)
        if radio is None:
            return json.dumps({"success": False, "error": "kein Funkpfad"})
        if not getattr(radio, "pair_key", None):
            return json.dumps({
                "success": False,
                "error": ("Kein Geraeteschluessel hinterlegt. Der Aufkleber des "
                          "Geraets (oder sein LocalKey) muss zuerst in der "
                          "QCCU-Oberflaeche eingetragen werden — eine CCU von "
                          "eq-3 holt ihn sich selbst, die QCCU kann das nicht."),
            })
        # Mit Schluessel laeuft das Anlernen ueber das Anlernfenster; das
        # Aufnehmen aus dem Posteingang ist dann nur noch die Aufforderung,
        # es zu oeffnen.
        try:
            self.q.setInstallMode(True, 60, 1, adresse or None)
        except Exception as ex:                      # noqa: BLE001
            return json.dumps({"success": False, "error": str(ex)})
        return json.dumps({"success": True, "error": ""})

    def _backend_info(self):
        """Version, Produkt, Rechnername.

        ⚠️ `product` entscheidet mehr, als es aussieht: die Gegenstelle leitet
        daraus den Zentralentyp ab (`CCU` / `OpenCCU` / sonst `Unknown`), und
        aus DEM wiederum, ob sie Sicherungen und Systemaktualisierungen
        anbietet (`has_backup`, `has_system_update`). Wir sind weder eine CCU
        noch eine OpenCCU — „QCCU" ist die zutreffende Auskunft UND schaltet
        genau die beiden Faehigkeiten ab, die wir nicht haben. Nichts
        vortaeuschen ist hier auch funktional das Richtige.
        """
        return json.dumps({
            "version": _version(),
            "product": "QCCU",
            "hostname": self.hostname,
            "is_ha_app": False,
        })

    def _serial(self):
        """Eine Seriennummer, die ueber Neustarts gleich bleibt.

        Die Gegenstelle nutzt sie, um die Zentrale wiederzuerkennen; sie darf
        sich also nicht bei jedem Start aendern. Die Funkadresse leistet das
        und ist zugleich die ehrlichste Kennung, die wir haben."""
        radio = getattr(self.q, "radio", None)
        addr = (getattr(radio, "own_addr", None) or "").upper()
        return f"QCCU{addr}"[:10] if addr else "QCCU000000"

    def _alle_werte(self, s):
        # Der Interface-Name steht IM Skript: die Gegenstelle ersetzt
        # `##interface##`, bevor sie es schickt. Von dort zu lesen ist
        # verlaesslicher als eine eigene Einstellung — die beiden koennten
        # auseinanderlaufen.
        m = re.search(r'sUse_Interface\s*=\s*"([^"]*)"', s)
        iface = m.group(1) if m and m.group(1) else self.interface

        # ⚠️ Dieses Skript kommt JE SCHNITTSTELLE einmal, und der Name steht
        # darin. Wer ihn liest, aber trotzdem ALLE Geraete zurueckgibt, meldet
        # jeden Wert doppelt — einmal unter jedem Schnittstellennamen —, und
        # die BidCoS-Werte tragen dann das HmIP-Praefix.
        if self.bidcos is not None and iface == self.bidcos.interface_name:
            werte = {}
            with self.bidcos.lock:
                bg = list(self.bidcos.devices.values())
            for d in bg:
                for (ch, param), v in d.values.items():
                    if v is None:
                        continue
                    werte[f"{iface}.{d.address}:{ch}.{param}"] = _wert_aus(v)
            return json.dumps(werte)

        werte = {}
        with self.q.lock:
            geraete = list(self.q.devices.values())
        for d in geraete:
            for (ch, param), v in d.values.items():
                if v is None:
                    continue
                # Genau dieser Aufbau wird beim Nachschlagen erwartet:
                # f"{interface}.{channel_address}.{parameter}"
                werte[f"{iface}.{d.address}:{ch}.{param}"] = _wert_aus(v)
        return json.dumps(werte, ensure_ascii=False)

    # -- die sieben schon vorhandenen, nur uebersetzt -------------------

    def paramset_description(self, address, paramset, ziel=None):
        """⚠️ Drei Abweichungen gegenueber XML-RPC auf einmal.

        1. Es ist eine LISTE, kein Verzeichnis. Der Name steht als `NAME` im
           Eintrag; die Gegenstelle baut daraus wieder ein Verzeichnis.
        2. `ID` ist PFLICHT und wird ungeprueft gelesen — fehlt es, bricht
           die Umwandlung mit einem Schluesselfehler ab. Es traegt denselben
           Namen wie der Parameter.
        3. `VALUE_LIST` ist hier eine ZEICHENKETTE mit Leerzeichen als
           Trenner, keine Liste (`value_list.split(" ")` auf der Gegenseite).
           Eine Liste zu schicken zerlegt sie zeichenweise.

        Ebenfalls ungeprueft gelesen werden `TYPE`, `DEFAULT`, `FLAGS` und
        `OPERATIONS` — sie muessen in jedem Eintrag stehen.
        """
        beschreibung = (ziel if ziel is not None else self.q).getParamsetDescription(address, paramset)
        out = []
        for name, eintrag in beschreibung.items():
            e = dict(eintrag)
            e["NAME"] = name
            e["ID"] = name
            e.setdefault("TYPE", "STRING")
            if e["TYPE"] not in BEKANNTE_TYPEN and e["TYPE"] not in _gemeldete_typen:
                _gemeldete_typen.add(e["TYPE"])
                print(f"  ⚠ Datentyp {e['TYPE']!r} (z.B. {address}.{name}) ist der "
                      f"Gegenstelle unbekannt — sie wird die Datenpunkte dieses "
                      f"Geraets NICHT anlegen. Die Tabelle gehoert neu gebaut "
                      f"(tables/build_paramsets.py).")
            e.setdefault("OPERATIONS", 0)
            e.setdefault("FLAGS", 1)
            # ⚠️ DEFAULT, MIN und MAX werden auf der Gegenseite UNGEPRUEFT
            # gelesen; fehlt eines, bricht sie mit „Unable to create
            # data_point: MIN" ab — und zwar fuer das ganze Geraet. Ein
            # einziger unvollstaendiger Eintrag reicht dafuer.
            #
            # Ergaenzt wird das NICHT hier, sondern im Tabellenbau
            # (`vervollstaendige_grenzen`) — sonst haette die Tabelle einen
            # Mangel, den nur diese eine Ausgabeschicht verdeckt. Gemeldet
            # wird er trotzdem, denn von aussen ist er nicht zu sehen.
            fehlend = [f for f in ("DEFAULT", "MIN", "MAX") if e.get(f) is None]
            if fehlend and name not in _gemeldete_luecken:
                _gemeldete_luecken.add(name)
                print(f"  ⚠ {address}.{name}: {', '.join(fehlend)} fehlt — die "
                      f"Gegenstelle wird die Datenpunkte dieses Geraets NICHT "
                      f"anlegen. Die Tabelle gehoert neu gebaut "
                      f"(tables/build_paramsets.py).")
            if isinstance(e.get("VALUE_LIST"), (list, tuple)):
                e["VALUE_LIST"] = " ".join(str(v) for v in e["VALUE_LIST"])
            out.append(e)
        return out

    def device_descriptions(self, nur_adresse=None, ziel=None):
        """⚠️ Auf diesem Weg heissen die Felder ANDERS als ueber XML-RPC:
        klein und in Binnenversalien (`type`, `address`, `paramsets`,
        `subType`, `rxMode`, …) statt `TYPE`, `ADDRESS`, `PARAMSETS`.

        Pflicht sind nur `type`, `address` und `paramsets`; alles andere
        wird von der Gegenstelle nur uebernommen, wenn es dasteht. Wer die
        XML-RPC-Schreibweise durchreicht, bekommt einen Schluesselfehler
        mitten in der Geraeteliste — und keine Geraete.
        """
        q = ziel if ziel is not None else self.q
        roh = (q.listDevices() if nur_adresse is None
               else [q.getDeviceDescription(nur_adresse)])
        out = []
        for d in roh:
            e = {
                "type": d.get("TYPE", ""),
                "address": d.get("ADDRESS", ""),
                "paramsets": d.get("PARAMSETS", []),
            }
            for json_name, xml_name in (
                ("availableFirmware", "AVAILABLE_FIRMWARE"),
                ("children", "CHILDREN"),
                ("firmware", "FIRMWARE"),
                ("firmwareUpdatable", "UPDATABLE"),
                ("firmwareUpdateState", "FIRMWARE_UPDATE_STATE"),
                ("parent", "PARENT"),
                ("linkSourceRole", "LINK_SOURCE_ROLES"),
                ("linkTargetRole", "LINK_TARGET_ROLES"),
                ("rxMode", "RX_MODE"),
                ("subType", "SUBTYPE"),
                ("updatable", "UPDATABLE"),
            ):
                wert = d.get(xml_name)
                if wert not in (None, "", [], 0, False):
                    e[json_name] = wert
            e["interface"] = self.interface
            out.append(e)
        return out if nur_adresse is None else out[0]

    def list_interfaces(self):
        """⚠️ Der Torwaechter des ganzen Weges.

        Die Gegenstelle gleicht diese Liste mit der Schnittstelle ab, die der
        Anwender bei sich angehakt hat. Steht sie nicht darin, wird der eben
        erst erzeugte Client wieder verworfen — „Interface: HmIP-RF is not
        available for the backend" — und zwar bevor ein einziges Geraet geholt
        wird. Der Name muss also genau der sein, unter dem die QCCU auch sonst
        auftritt, und der Port der, auf dem ihre XML-RPC-Auskunft steht."""
        raus = [{
            "name": self.interface,
            "port": self.rpc_port,
            "info": "QCCU",
        }]
        # ⚠️ Die zweite Schnittstelle MUSS hier stehen, sonst bietet die
        # Gegenstelle sie in ihrer Einrichtung gar nicht erst an — und ein
        # Client, der sie angehakt hat, wird wieder verworfen.
        if self.bidcos is not None and self.bidcos_port:
            raus.append({
                "name": self.bidcos.interface_name,
                "port": self.bidcos_port,
                "info": "QCCU",
            })
        return raus

    # -- Vermittlung ----------------------------------------------------

    def dispatch(self, method, params):
        """Ein Aufruf. Rueckgabe (ergebnis, fehler) — genau einer ist None."""
        p = params or {}

        if method == "Session.login":
            return self.sessions.anmelden(), None
        if method == "Session.renew":
            return self.sessions.erneuern(p.get("_session_id_")), None
        if method == "Session.logout":
            return self.sessions.abmelden(p.get("_session_id_")), None
        if method == "system.listMethods":
            return [{"name": m} for m in SUPPORTED_METHODS], None

        # Der Verbindungsaufbau der Gegenstelle fragt zuerst nach der
        # Zugangsregelung. Die QCCU prueft keine Kennwoerter — beides ist
        # aus, und das muss sie sagen, sonst bricht der Aufbau ab.
        if method == "CCU.getAuthEnabled":
            return False, None
        if method == "CCU.getHttpsRedirectEnabled":
            return False, None

        if method == "Interface.isPresent":
            return self.is_present(p.get("interface")), None
        if method == "Interface.listInterfaces":
            return self.list_interfaces(), None

        # Was es bei uns nicht gibt: leer, aber gueltig. Eine leere Liste ist
        # die zutreffende Auskunft — die QCCU fuehrt weder Programme noch
        # Systemvariablen, Raeume oder Gewerke.
        if method in ("Program.getAll", "Room.getAll", "Subsection.getAll",
                      "SysVar.getAll", "Interface.getSuppressedServiceMessages"):
            return [], None
        if method == "Channel.hasProgramIds":
            return False, None

        # Anlernen — das kann die QCCU wirklich, und aus der Oberflaeche der
        # Gegenstelle heraus ist es der bequemste Weg dorthin.
        if method == "Interface.getInstallMode":
            return self.q.getInstallMode(), None
        if method == "Interface.setInstallModeHMIP":
            an = bool(p.get("on", True))
            radio = getattr(self.q, "radio", None)
            # 🔑 Der Aufruf traegt ein Feld `key` — dafuer vorgesehen, den
            # Schluessel vom Aufkleber mitzugeben (die Oberflaeche einer CCU
            # fuellt es). aiohomematic schickt es HEUTE immer leer
            # (`client/json_rpc.py`, set_install_mode_hmip: key="", keymode=""),
            # weil die Integration keine Eingabe dafuer hat. Das ist aber eine
            # Luecke der Bedienung, nicht des Protokolls: wer das Feld fuellt —
            # ein `rest_command` in Home Assistant, ein Skript, eine spaetere
            # Fassung der Integration —, kann den Aufkleber auf diesem Weg
            # uebergeben und braucht die QCCU-Oberflaeche nicht.
            #
            # Angenommen werden beide Schreibweisen: 26 Zeichen vom Aufkleber
            # oder 32 Hexziffern LocalKey. `keymode` wird nicht ausgewertet —
            # wir kennen nur diesen einen Modus und tun nicht so, als koennten
            # wir andere.
            schluessel = str(p.get("key") or "").strip()
            if an and schluessel and radio is not None:
                roh = schluessel.replace("-", "").replace(" ", "")
                ist_hex = (len(roh) == 32
                           and all(c in "0123456789abcdefABCDEF" for c in roh))
                fehler = radio.start_pairing(
                    None if ist_hex else schluessel,
                    int(p.get("time", 60) or 60),
                    None,
                    roh if ist_hex else None)
                if fehler:
                    return None, {"name": "BadDeviceKey", "code": -32000,
                                  "message": f"Schluessel unbrauchbar: {fehler}"}
                return True, None
            # ⚠️ Ohne hinterlegten Geraeteschluessel kann das Fenster offen
            # sein, so lange es will — angelernt wird nichts. Das als Erfolg
            # zu quittieren waere die schlechteste Auskunft: der Anwender
            # drueckt den Knopf, sieht ein zufriedenes „ok" und wartet dann
            # vergeblich. Deshalb hier ein Fehler MIT Handlungsanweisung.
            if an and radio is not None and not getattr(radio, "pair_key", None):
                return None, {
                    "name": "NoDeviceKey", "code": -32000,
                    "message": ("Anlernen nicht moeglich: kein Geraeteschluessel "
                                "hinterlegt. Entweder den Aufkleber des Geraets "
                                "in der QCCU-Oberflaeche eintragen, oder ihn "
                                "diesem Aufruf im Feld `key` mitgeben "
                                "(26 Zeichen vom Aufkleber oder 32 Hexziffern)."),
                }
            self.q.setInstallMode(an,
                                  int(p.get("time", 60) or 60),
                                  1, p.get("address") or None)
            return True, None

        # Ein einzelner Konfigurationswert. Die QCCU haelt Konfiguration je
        # Kanal beisammen, also wird er dort herausgesucht.
        if method == "Interface.getMasterValue":
            ps = self.q.getParamset(p["address"], "MASTER")
            schluessel = p.get("valueKey") or p.get("name")
            if schluessel not in ps:
                return None, {"name": "Unknown parameter", "code": -32602,
                              "message": str(schluessel)}
            return ps[schluessel], None

        if method in ("Device.setName", "Channel.setName"):
            return self.set_name(p.get("id"), p.get("name"))

        # Aenderungswuensche, die wir nicht fuehren: sauber abweisen statt
        # Erfolg vorzutaeuschen. (Diese Methoden stehen auch nicht in
        # SUPPORTED_METHODS; hier stehen sie nur, damit ein Aufruf trotzdem
        # eine klare Antwort bekommt.)
        if method in ("Program.execute",
                      "SysVar.getValueByName",
                      "Interface.getLinkInfo", "Interface.setLinkInfo",
                      "Interface.suppressServiceMessages"):
            return None, {"name": "NotSupported", "code": -32601,
                          "message": f"{method} wird von der QCCU nicht gefuehrt"}
        if method == "Device.listAllDetail":
            return self.list_all_detail(), None
        if method == "ReGa.runScript":
            return self.run_script(p.get("script", "")), None

        if method == "Interface.listDevices":
            return self.device_descriptions(ziel=self._ziel(p)), None
        if method == "Interface.getDeviceDescription":
            return self.device_descriptions(p["address"], ziel=self._ziel(p)), None
        if method == "Interface.getParamsetDescription":
            return self.paramset_description(p["address"], p.get("paramsetKey")
                                             or p.get("paramsetType") or "VALUES",
                                             ziel=self._ziel(p)), None
        if method == "Interface.getParamset":
            return self._ziel(p).getParamset(p["address"], p.get("paramsetKey")
                                             or p.get("paramsetType") or "VALUES"), None
        if method == "Interface.getValue":
            return self._ziel(p).getValue(p["address"], p["valueKey"]), None
        if method == "Interface.setValue":
            self._ziel(p).setValue(p["address"], p["valueKey"],
                                   _wert_ein(p.get("value"), p.get("type")))
            return "", None
        if method == "Interface.putParamset":
            self._ziel(p).putParamset(p["address"], p.get("paramsetKey")
                                      or p.get("paramsetType") or "VALUES",
                                      p.get("set") or {})
            return "", None

        return None, {"name": "MethodNotFound", "code": -32601, "message": method}


def _wert_ein(wert, json_typ):
    """Ein hereinkommender Schaltwert, zurueck in seinen Datentyp.

    ⚠️ Hier ist der gefaehrlichste Punkt der ganzen Auskunft. Ein Schaltbefehl
    kommt auf diesem Weg als Wert PLUS Typangabe (`bool`, `int`, `double`,
    `string`, `list`) — und der Wert kann als Zeichenkette ankommen. Wer ihn
    ungeprueft weiterreicht, gibt dem Funkpfad die Zeichenkette `"False"`,
    und die ist in Python WAHR: „aus" wuerde einschalten. Der Fehler faellt
    nirgends auf, es gibt keine Fehlermeldung — das Geraet tut nur das
    Gegenteil.

    Deshalb wird nach der mitgelieferten Typangabe zurueckgewandelt und im
    Zweifel der Wert selbst befragt, nicht seine Wahrheit."""
    if isinstance(wert, str):
        w = wert.strip()
    else:
        w = wert
    t = (json_typ or "").lower()

    if t == "bool" or isinstance(w, bool):
        if isinstance(w, bool):
            return w
        return str(w).lower() in ("true", "1", "yes", "on")
    if t == "int":
        try:
            return int(float(w))
        except (TypeError, ValueError):
            return 0
    if t == "double":
        try:
            return float(w)
        except (TypeError, ValueError):
            return 0.0
    if t == "string":
        return "" if w is None else str(w)
    return w



def _wert_aus(v):
    """Was in den Sammelabruf geschrieben wird.

    Der Sammelabruf einer echten Zentrale kodiert Zeichenketten fuer die
    Uebertragung (die Gegenstelle dreht das mit `unquote` zurueck); Zahlen und
    Wahrheitswerte stehen unveraendert."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return quote(str(v))


class JsonRpcHandler(BaseHTTPRequestHandler):
    """HTTP davor. Ein Aufruf je Anfrage, wie es die Gegenstelle schickt."""

    server_version = "QCCU"
    api = None          # JsonRpc-Instanz
    verbose = False

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path.split("?")[0] not in (API_PATH, "/"):
            self.send_error(404)
            return

        n = int(self.headers.get("Content-Length", 0))
        roh = self.rfile.read(n) if n else b""
        kennung = None
        try:
            anfrage = json.loads(roh.decode("utf-8", "replace") or "{}")
            kennung = anfrage.get("id")
            method = anfrage.get("method", "")
            params = anfrage.get("params") or {}
            ergebnis, fehler = self.api.dispatch(method, params)
        except KeyError as e:
            ergebnis, fehler = None, {"name": "ParameterMissing", "code": -32602,
                                      "message": str(e)}
        except Exception as e:                       # noqa: BLE001
            ergebnis, fehler = None, {"name": type(e).__name__, "code": -32603,
                                      "message": str(e)}
        else:
            if self.verbose and method:
                self._melden(method, ergebnis, fehler)

        antwort = json.dumps({"version": "1.1", "id": kennung,
                              "result": ergebnis, "error": fehler},
                             ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(antwort)))
        self.end_headers()
        self.wfile.write(antwort)

    # ⚠️ Diese Aufrufe kommen im Sekundentakt, solange eine Haussteuerung
    # angebunden ist: gemessen 1264x Session.login, 1262x Session.renew und
    # 801x Sammelabruf in EINEM Protokoll von 4000 Zeilen — die 59 Zeilen, um
    # die es ging, waren darin nicht mehr zu finden. Wer ein Protokoll zur
    # Fehlersuche schickt, soll darin etwas sehen koennen. Mit `--verbose`
    # steht wieder alles da.
    LEISE = frozenset((
        "Session.login", "Session.renew", "Session.logout",
        "ReGa.runScript", "Program.getAll", "SysVar.getAll",
        "Room.getAll", "Subsection.getAll", "SysVar.getValue",
        "CCU.getAuthEnabled", "CCU.getHttpsRedirectEnabled",
    ))

    laut = False

    def _melden(self, method, ergebnis, fehler):
        # Ein FEHLER wird immer gemeldet, auch bei den leisen Methoden — er
        # ist das Gegenteil von Rauschen.
        if not fehler and method in self.LEISE and not self.laut:
            return
        if fehler:
            print(f"  JSON-RPC: {method} -> {fehler.get('name')}")
        elif isinstance(ergebnis, list):
            print(f"  JSON-RPC: {method} -> {len(ergebnis)} Eintraege")
        elif method == "ReGa.runScript":
            n = ergebnis.count(":") if isinstance(ergebnis, str) else 0
            print(f"  JSON-RPC: Sammelabruf -> {n} Werte")
        else:
            print(f"  JSON-RPC: {method}")


def serve(qccu, bind, port, verbose=False, interface=None, rpc_port=2010,
          hostname=None, laut=False, bidcos=None, bidcos_port=0):
    """Startet die Auskunft in einem eigenen Faden.

    `rpc_port` ist der Port der XML-RPC-Auskunft — er geht in
    `listInterfaces` ein, damit die Gegenstelle die Schnittstelle dort
    wiederfindet, wo sie wirklich horcht."""
    from http.server import HTTPServer

    JsonRpcHandler.api = JsonRpc(qccu, interface=interface, rpc_port=rpc_port,
                                 hostname=hostname, bidcos=bidcos,
                                 bidcos_port=bidcos_port)
    JsonRpcHandler.verbose = verbose
    # `laut` trennt zwei Dinge, die vorher eines waren: DASS gemeldet wird
    # (verbose) und OB auch der Dauerverkehr dazugehoert.
    JsonRpcHandler.laut = laut
    srv = HTTPServer((bind, port), JsonRpcHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
