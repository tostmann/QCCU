#!/usr/bin/env python3
"""QCCU — die Quiche-Zentrale: gibt sich gegenueber Hausautomationen als solche aus."""
import argparse
import collections
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler

# Fassung im Schema von Home Assistant (Jahr.Monat.Zaehler). ⚠️ Sie ist
# zugleich die Marke des Abbilds auf Docker Hub UND der Wert von `version`
# in addon/config.yaml — der Supervisor zieht `<image>:<version>`. Wer hier
# hochzaehlt, muss beides mitziehen, sonst schlaegt die Installation fehl.
VERSION = "2026.9.1"
PRODUKT = "QCCU"
NAME_UND_FASSUNG = f"{PRODUKT} {VERSION}"

# Hoerertypen — die unteren vier Bit des Betriebsmodus, den ein Geraet in
# seinem Anlernruf ansagt.
#
# ⚠️ Die Aufzaehlung ist LUECKENHAFT: 2, 6, 7, 10 und 13 gibt es nicht.
# Woertlich aus `ListenerMode.java:8-17` des HMIPServer-Jars; ein unbekannter
# Wert wirft dort `InvalidEnumValueException`, und `RouteResponse` faengt das
# ab und faellt still auf PERMANENT_LISTENER zurueck.
#
# Warum das hier steht: der Hoerertyp entscheidet, ob ein Geraet einen Befehl
# sofort hoert oder ihn verschlaeft. Er ist eine dauerhafte Eigenschaft des
# Geraets — die echte Zentrale fuehrt ihn neben SGTIN, Adresse und Typ
# (`HMIPDevice.java:251-254`, `@Property(order=4, name="listenerMode")`).
# QCCU hat ihn bis 2026.8.44 nur als Aufrufparameter durchgereicht und nach
# dem Anlernen weggeworfen.
LM_NAMEN = {
    0:  "permanent",
    1:  "Einfach-Burst",
    3:  "Dreifach-Burst",
    4:  "Ereignis",
    5:  "Ereignis mit Stromsparen",
    8:  "zyklisch",
    9:  "zyklisch + Einfach-Burst",
    11: "zyklisch + Dreifach-Burst",
    12: "permanent am Draht",
    14: "permanent am Backbone",
}
LM_GUELTIG = tuple(sorted(LM_NAMEN))

# Woher der Wert stammt — dieselbe Ehrlichkeit wie bei den Belegstufen der
# Geraetetabellen. „anlernruf" heisst: das Geraet hat ihn selbst angesagt.
LM_QUELLEN = {
    "anlernruf":  "aus dem Anlernruf",
    "mitschnitt": "aus dem Mitschnitt",
    "hand":       "von Hand eingetragen",
}


class Tables:
    """Die Gerätebeschreibungen. Fehlen sie, laeuft QCCU trotzdem an.

    ⚠️ Das ist Absicht und nicht Nachlaessigkeit: die Tabellen entstehen beim
    ersten Start aus einem Paketbezug, und der kann scheitern (kein Netz,
    Sperre, zu kleine MTU auf dem Docker-Netz). Wer dann abbricht, laesst den
    Anwender mit einem Behaelter zurueck, der neu startet und nichts anzeigt —
    er saehe den Grund nur in `docker logs`. Ohne Tabellen kann QCCU keine
    Geraete fuehren, aber die Oberflaeche kann sagen, was fehlt und wie es zu
    beheben ist. Dieselbe Haltung wie beim fehlenden Stick.

    `fehlend` traegt die Namen der Tabellen, die nicht da waren."""

    def __init__(self, base):
        self.base = base
        self.fehlend = []
        self.catalog = self._load(base, "catalog.json")
        self.paramsets = self._load(base, "paramsets.json")
        self.sdt = self._load(base, "sdt_table.json")
        # Bestand der Parameter, die nur in den Geraetebeschreibungen stehen.
        # Fehlt er, ist die Tabelle nach altem Muster gebaut (siehe `alt`).
        self.extra = self._load(base, "extra_params.json", pflicht=False)
        # Link-Rolle je Kanaltyp-Fassung (SENDER/RECEIVER/NONE) — fehlt sie,
        # legt QCCU keine Verknuepfung zur Zentrale an und sagt das.
        self.rollen = self._load(base, "kanalrollen.json", pflicht=False) or {}
        # ALTE TABELLEN (bis 2026.8.12) fuehrten je Kanaltyp EINE Liste ueber
        # alle Fassungen — eine Schaltsteckdose bekam damit 1087
        # Konfigurationsparameter angeboten, darunter Farbverlaeufe. Neue
        # Tabellen fuehren `KANALTYP/vN`. Beide werden gelesen; welche
        # vorliegt, entscheidet der Schluessel.
        self.alt = not any("/v" in k for k in self.paramsets)
        # Fuehrt der Katalog die interne Verdrahtung der Geraete? Bis 2026.8.34
        # tat er das nicht. Der Unterschied ist von aussen nicht zu sehen —
        # „dieser Typ hat keine Links" und „diese Tabelle kennt keine Links"
        # sehen gleich aus —, und der Anlernpfad haengt daran. Deshalb einmal
        # global feststellen statt je Geraet raten.
        self.links_bekannt = any("links" in e for e in self.catalog.values()
                                 if isinstance(e, dict))
        # Fuehrt der Katalog die Firmware-Baender? Bis 2026.8.38 nicht. Ohne
        # sie kann die Fassung eines Geraets nicht ausgewertet werden.
        self.varianten_bekannt = any("varianten" in e
                                     for e in self.catalog.values()
                                     if isinstance(e, dict))
        self._sdt_namen = None

    def _load(self, base, name, pflicht=True):
        p = os.path.join(base, name)
        if not os.path.exists(p):
            if pflicht:
                print(f"  ! Tabelle fehlt: {p}", file=sys.stderr)
                self.fehlend.append(name)
            return {}
        with open(p) as f:
            return json.load(f)

    def zustand(self):
        """Was die Oberflaeche ueber die Tabellen wissen muss."""
        return {
            "ok": not self.fehlend,
            "fehlend": list(self.fehlend),
            "geraetetypen": len(self.catalog),
            "kanaltypen": len(self.paramsets),
            "statusdatentypen": len(self.sdt),
            "veraltet": self.alt,
        }

    @staticmethod
    def fassung_zahl(text):
        """`"2.8.10"` -> 133130, oder None.

        So zaehlt die Zentrale Firmwarefassungen: `major<<16 | minor<<8 |
        patch`. Genau diese Zahlen stehen als `minVersion`/`maxVersion` in
        den Gerätebeschreibungen — 66048 ist 1.2.0, 196608 ist 3.0.0.
        """
        try:
            teile = [int(x) for x in str(text).split(".")]
        except (TypeError, ValueError):
            return None
        if not 1 <= len(teile) <= 3 or any(x < 0 for x in teile):
            return None
        while len(teile) < 3:
            teile.append(0)
        if teile[1] > 255 or teile[2] > 255:
            return None
        return (teile[0] << 16) | (teile[1] << 8) | teile[2]

    def fuer_geraet(self, devtype, fassung=None):
        """Die Beschreibung, die fuer DIESES Geraet gilt.

        Ein Geraetetyp hat oft mehrere Beschreibungen, und welche gilt,
        entscheidet die FASSUNG der Firmware: die Zentrale schlaegt sie ueber
        `DeviceType.findDeviceTypeByVersion` nach, also ueber das
        `minVersion`/`maxVersion`-Band. Beim HmIP-ASIR unterscheiden sich die
        Baender in der internen Verdrahtung — die alte Fassung hat keine, die
        neue den Link 1->2 —, und ein Geraet ohne Verdrahtung meldet sich nach
        dem Anlernen wieder ab.

        ⚠️ Ohne bekannte Fassung bleibt es bei der Vorgabe des Katalogs. Das
        ist Absicht: jedes Geraet, das vor 2026.8.39 angelernt wurde, hat
        keine gespeicherte Firmware, und die Vorgabe ist genau das, womit es
        bisher gelaufen ist. Raten waere hier keine Verbesserung.
        """
        e = self.catalog.get(str(devtype))
        if not e:
            return {}
        roh = self.fassung_zahl(fassung) if fassung is not None else None
        if roh is None:
            return e
        for v in e.get("varianten", ()):
            if not v.get("min", 0) <= roh <= v.get("max", 0xFFFFFFFF):
                continue
            eigen = [k for k in ("channels", "chinfo", "links", "spec")
                     if k in v]
            if not [k for k in eigen if k != "spec"]:
                return e
            zusammen = dict(e)
            for k in eigen:
                zusammen[k] = v[k]
            return zusammen
        return e

    def channels_of(self, devtype, eintrag=None):
        e = eintrag if eintrag is not None else self.catalog.get(str(devtype))
        return e["channels"] if e else {}

    def label_of(self, devtype):
        e = self.catalog.get(str(devtype))
        return e["label"] if e else f"Typ {devtype}"

    def rolle_of(self, devtype, kanal, eintrag=None):
        """Link-Rolle eines Kanals (SENDER, RECEIVER, NONE) — oder None, wenn
        die Tabelle sie nicht fuehrt."""
        if not self.rollen:
            return None
        ctype = self.channels_of(devtype, eintrag).get(str(kanal))
        if not ctype:
            return None
        v = self.chinfo_of(devtype, kanal, eintrag).get("v")
        if v is not None and f"{ctype}/v{v}" in self.rollen:
            return self.rollen[f"{ctype}/v{v}"]
        fassungen = sorted((int(k.split("/v")[1]) for k in self.rollen
                            if k.startswith(ctype + "/v") and k.split("/v")[1].isdigit()),
                           reverse=True)
        if fassungen:
            return self.rollen[f"{ctype}/v{fassungen[0]}"]
        return self.rollen.get(ctype)

    def links_of(self, devtype, eintrag=None):
        """Die interne Verdrahtung eines Geraetetyps: [[Quellkanal, Zielkanal]].

        Ground Truth aus der Geraetebeschreibung des Herstellers
        (`<internalLinks><internalLink sourceIndex= targetIndex=>`), beim
        Tabellenbau uebernommen. 80 der 304 Geraetetypen fuehren solche Links,
        bis zu acht Stueck; wo keine stehen, richtet auch eine Zentrale von
        eQ-3 keine ein.

        ⚠️ Alte Tabellen (bis 2026.8.34) kennen das Feld nicht und liefern eine
        leere Liste. Der Anlernpfad muss das aushalten — er darf daraus NICHT
        schliessen, das Geraet habe keine Verdrahtung.
        """
        e = (eintrag if eintrag is not None
             else self.catalog.get(str(devtype))) or {}
        return [tuple(p) for p in (e.get("links") or ()) if len(p) == 2]

    def params_of(self, channel_type, pset="VALUES", version=None):
        """Parameter eines Kanaltyps — mit Fassung, wenn die Tabelle sie fuehrt.

        Ohne passende Fassung wird die hoechste gefuehrte genommen: besser eine
        vollstaendige benachbarte Liste als gar keine.
        """
        e = self.paramsets.get(channel_type)
        if e is None and not self.alt:
            if version is not None:
                e = self.paramsets.get(f"{channel_type}/v{version}")
            if e is None:
                fassungen = sorted(
                    (int(k.split("/v")[1]) for k in self.paramsets
                     if k.startswith(channel_type + "/v") and k.split("/v")[1].isdigit()),
                    reverse=True)
                if fassungen:
                    e = self.paramsets.get(f"{channel_type}/v{fassungen[0]}")
        return (e or {}).get(pset, {})

    def sdt_name(self, nummer):
        """Name eines Statusdatentyps, wie eq-3 ihn fuehrt (`sdt_table.json`).

        Die Tabelle ist nach Namen sortiert (`{"OPERATING_VOLTAGE": {"type": 3,
        "len": 1}, …}`) — hier wird sie einmal umgedreht. Gebraucht wird das
        fuer Werte, die wir NOCH NICHT deuten: im Protokoll steht dann nicht
        bloss „SDT3", sondern der Name, unter dem der Wert bei eq-3 laeuft.
        So ist beim ersten Geraet, das einen solchen Wert schickt, sofort zu
        sehen, worum es geht — ohne dass wir eine Skalierung erfinden muessen.
        """
        if self._sdt_namen is None:
            self._sdt_namen = {}
            for name, e in (self.sdt or {}).items():
                if isinstance(e, dict) and isinstance(e.get("type"), int):
                    self._sdt_namen.setdefault(e["type"], name)
        return self._sdt_namen.get(nummer)

    def chinfo_of(self, devtype, kanal, eintrag=None):
        """Fassung und geraeteeigene Parameter eines Kanals."""
        e = (eintrag if eintrag is not None
             else self.catalog.get(str(devtype))) or {}
        return (e.get("chinfo") or {}).get(str(kanal)) or {}

    def paramset_of(self, devtype, kanal, pset="VALUES", eintrag=None):
        """Das Paramset eines Kanals bei DIESEM Geraetetyp.

        So setzt es auch eine Zentrale von eQ-3 zusammen (an einer HmIP-PS-2
        ueber alle sieben Kanaele und beide Paramsets geprueft, 18.08.2026):
        die Liste der Kanaltyp-Fassung, dazu was die Geraetebeschreibung
        selbst nennt.
        """
        ctype = self.channels_of(devtype, eintrag).get(str(kanal))
        if not ctype:
            return {}
        info = self.chinfo_of(devtype, kanal, eintrag)
        out = dict(self.params_of(ctype, pset, info.get("v")))
        for schluessel in (info.get("extra") or {}).get(pset, []):
            name = schluessel.split("@")[0]
            if name in out:
                continue
            desc = self.extra.get(schluessel) or self.extra.get(f"{name}@default")
            if desc:
                out[name] = desc
        return out


def channel_direction(ctype):
    """Richtung eines Kanals: 1 = Sender, 2 = Empfaenger, 0 = keines."""
    if ctype.endswith("_RECEIVER"):
        return 2
    if ctype.endswith("_TRANSCEIVER"):
        return 1
    return 0


def channel_paramsets(ctype):
    """Welche Paramsets ein Kanal ankuendigt."""
    ps = ["MASTER", "VALUES"]
    if channel_direction(ctype):
        ps.append("LINK")
    ps.append("SERVICE")
    return ps


class Device:
    """Ein angelerntes Geraet, wie es die Gegenstelle sieht."""

    def __init__(self, address, devtype, tables, firmware="1.0.0",
                 fassung=None, opmode=None, opmode_quelle=None):
        self.address = address.upper()
        self.devtype = int(devtype)
        self.firmware = firmware
        self.tables = tables
        # ⚠️ `fassung` ist die im Anlernruf ANGESAGTE Firmware — und nur die
        # taugt zur Bandwahl. `firmware` traegt die Vorgabe „1.0.0", wenn
        # nichts bekannt ist; daraus ein Band zu waehlen hiesse, einen
        # Platzhalter fuer eine Messung zu halten. Ein Geraet, das vor
        # 2026.8.39 angelernt wurde, hat keine — es bleibt bei der Vorgabe
        # des Katalogs, also bei dem, womit es bisher gelaufen ist.
        self.fassung = fassung
        # ⚠️ Das GANZE Byte, nicht nur der Hoerertyp: es traegt daneben
        # `acessController` (0x80), `router` (0x40) und `portableDevice`
        # (0x20) — `RouteResponse.setOperationMode()` zerlegt genau so.
        # QCCU meldet das Byte beim Anlernen unveraendert weiter
        # (`_anlernen_rest`); wer nur die unteren vier Bit sichert, kann diese
        # Meldung nach einem Neustart nicht mehr wiederholen.
        # `None` heisst ausdruecklich „unbekannt" und bedeutet ueberall:
        # behandle das Geraet wie bisher.
        self.opmode = opmode
        self.opmode_quelle = opmode_quelle
        try:
            self.eintrag = tables.fuer_geraet(self.devtype, fassung)
        except Exception:                                # noqa: BLE001
            self.eintrag = None
        self.values = {}
        self.master = {}
        self.unreach = None
        self.last_seen = 0.0

    @property
    def label(self):
        return self.tables.label_of(self.devtype)

    @property
    def hoerer(self):
        """Der Hoerertyp (untere vier Bit) — oder None, wenn unbekannt."""
        return None if self.opmode is None else self.opmode & 0x0F

    @property
    def ist_router(self):
        """Meldet das Geraet sich als Router? None, wenn unbekannt."""
        return None if self.opmode is None else bool(self.opmode & 0x40)

    @property
    def hoerer_text(self):
        """Der Hoerertyp im Klartext, mit Herkunft — fuer die Oberflaeche.

        ⚠️ Steht hier und nicht in `qccu_web`: das Web-Modul importiert
        `qccu` nicht (es kennt nur `qccu_firmware`), und `qccu.py` laeuft als
        Skript — ein Import daraus faende ein zweites Modul mit eigenem
        Zustand vor. Die Oberflaeche liest die Eigenschaft am Geraet.
        """
        h = self.hoerer
        if h is None:
            return None
        name = LM_NAMEN.get(h, "unbekannter Hoerertyp")
        quelle = LM_QUELLEN.get(self.opmode_quelle or "")
        return f"{h} — {name}" + (f" ({quelle})" if quelle else "")

    @property
    def typname(self):
        """Der Typ, wie er im Namen erscheint.

        Bei HmIP ist die Beschriftung zugleich der Typ (`HmIP-PS-2 9YM`) —
        der Zusatz gehoert dazu und wird NICHT abgeschnitten. Bei BidCoS sind
        es zwei verschiedene Dinge, deshalb gibt es die Eigenschaft ueberhaupt.
        """
        return self.label

    @property
    def subtype(self):
        """Kurzform des Typs."""
        base = self.label.split()[0]
        parts = base.split("-")[1:]
        return parts[0] if parts else ""

    def channel_list(self):
        return sorted(((int(k), v) for k, v in
                       self.tables.channels_of(self.devtype,
                                               self.eintrag).items()))

    def paramset(self, kanal, pset="VALUES"):
        """Das Paramset eines Kanals — mit der Beschreibung DIESES Geraets."""
        return self.tables.paramset_of(self.devtype, kanal, pset, self.eintrag)

    def links(self):
        """Die interne Verdrahtung — mit der Beschreibung DIESES Geraets."""
        return self.tables.links_of(self.devtype, self.eintrag)

    def descriptions(self):
        """Geraet und Kanaele, wie `listDevices` sie erwartet."""
        chans = self.channel_list()
        out = [{
            "ADDRESS": self.address,
            "TYPE": self.label,
            "SUBTYPE": self.subtype,
            "FIRMWARE": self.firmware,
            "AVAILABLE_FIRMWARE": "0.0.0",
            "UPDATABLE": False,
            "FIRMWARE_UPDATE_STATE": "UP_TO_DATE",
            "VERSION": 1,
            "FLAGS": 1,
            "PARAMSETS": ["MASTER", "SERVICE"],
            "INTERFACE": "",
            "PARENT": "",
            "PARENT_TYPE": "",
            "INDEX": 0,
            "AES_ACTIVE": 1,
            "CHILDREN": [f"{self.address}:{i}" for i, _ in chans],
            "RF_ADDRESS": int(self.address[-6:], 16) if len(self.address) >= 6 else 0,
            "LINK_SOURCE_ROLES": "",
            "LINK_TARGET_ROLES": "",
            "DIRECTION": 0,
            "GROUP": "",
            "TEAM": "",
            "TEAM_TAG": "",
            "TEAM_CHANNELS": [],
            "ROAMING": 0,
            "RX_MODE": 0,
        }]
        for idx, ctype in chans:
            out.append({
                "ADDRESS": f"{self.address}:{idx}",
                "TYPE": ctype,
                "SUBTYPE": "",
                "PARENT": self.address,
                "PARENT_TYPE": self.label,
                "INDEX": idx,
                "VERSION": 1,
                "FLAGS": 1,
                "PARAMSETS": channel_paramsets(ctype),
                "AES_ACTIVE": 1,
                "DIRECTION": channel_direction(ctype),
                "CHILDREN": [],
                "RF_ADDRESS": 0,
                "FIRMWARE": "",
                "AVAILABLE_FIRMWARE": "",
                "UPDATABLE": False,
                "FIRMWARE_UPDATE_STATE": "",
                "LINK_SOURCE_ROLES": "",
                "LINK_TARGET_ROLES": "",
                "GROUP": "",
                "TEAM": "",
                "TEAM_TAG": "",
                "TEAM_CHANNELS": [],
                "INTERFACE": "",
                "ROAMING": 0,
                "RX_MODE": 0,
            })
        return out


class RpcHandler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/", "/RPC2")

    def log_message(self, fmt, *args):
        pass


NOTIFY_TIMEOUT = 5.0


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def rpc_proxy(url, timeout=NOTIFY_TIMEOUT):
    """Ausgehende Verbindung mit Zeitgrenze."""
    return xmlrpc.client.ServerProxy(url, allow_none=True,
                                     transport=_TimeoutTransport(timeout))


class QCCU:
    def __init__(self, tables, verbose=True):
        self.t = tables
        self.devices = {}
        self.subscribers = {}
        self._notify_fails = {}
        self.verbose = verbose
        self.rega_log = None
        self.store = None
        # Gesetzt, wenn der Speicher beim Start unlesbar war — sperrt save_store.
        self.store_gesperrt = None
        self._sperre_gemeldet = False
        self.rf = {}
        self.pair_next_addr = None
        self.stick_serial = None
        # Was der Anwender in der Oberflaeche umstellen kann und was den
        # Neustart ueberleben muss. ⚠️ Beides wirkt erst beim naechsten
        # Start — Ports werden einmal gebunden.
        self.einstellungen = {}
        # Vom Anwender vergebene Namen, je Geraet und je Kanal.
        self.names = {}
        # Frisch angelernte Geraete, die in der Haussteuerung noch bestaetigt
        # werden muessen: Adresse -> Zeitpunkt des Anlernens.
        self.frisch_angelernt = {}
        # ⚠️ Der Warteraum — das `ReadyConfig` einer Zentrale von eq-3.
        # Ein frisch angelerntes Geraet gehoert zur Anlage, ist aber noch
        # nicht „in Betrieb genommen": es wird den Gegenstellen NICHT
        # gemeldet, sondern steht in deren Posteingang, bis jemand es dort
        # aufnimmt (und dabei benennt). Erst dann geht `newDevices` hinaus.
        # Grund: die Haussteuerung stellt jedes neu gemeldete Geraet ohnehin
        # zurueck und verlangt eine Bestaetigung unter „Reparaturen" — ein
        # zweiter Ort fuer dieselbe Sache. Wer zuerst aufnimmt, sieht die
        # Reparatur gar nicht; sie wird beim Melden automatisch quittiert.
        self.warteraum = set()
        # Systemvariablen. Eine Zentrale von eq-3 fuehrt sie in der ReGa; die
        # QCCU hat keine ReGa, also fuehrt sie sie selbst — Name -> Eintrag.
        # Siehe `sysvar_anlegen` fuer die Form eines Eintrags.
        self.sysvars = {}
        # ⚠️ Kennungen sind ZEICHENKETTEN und beginnen bei 100. Die 40 und die
        # 41 sind bei eq-3 vergeben und stehen in aiohomematics
        # `IGNORE_SYSVARS_BY_ID` — eine Variable mit einer dieser Kennungen
        # wird dort stillschweigend uebergangen.
        self._sysvar_id = 100
        # Aufgenommene Geraete, deren Meldung noch auf ihren Namen wartet.
        # Adresse -> Wecker. Siehe `aufnehmen`.
        self._freigabe = {}
        # Aus: alles wird sofort gemeldet (wie bis 2026.8.28). Fuer
        # Gegenstellen ohne Posteingang — FHEM/HMCCU kennt keinen.
        self.zurueckhalten = True
        # Die letzten Ereignisse, die den Anwender angehen. Nicht das
        # Protokoll — dort steht auch der Verkehr; hier steht, was passiert
        # IST: angelernt, entfernt, Stick verloren, Name vergeben.
        self.ereignisse = collections.deque(maxlen=60)
        # Wann zuletzt ein Wert von welchem Geraet kam — damit sichtbar ist,
        # wie alt der gesicherte Stand ist.
        self._wert_zeit = {}
        self._werte_offen = False
        self._werte_zuletzt = 0.0
        self.own_addr = None
        self.interface_name = "HmIP-RF"
        self.own_host = "127.0.0.1"
        self.rpc_port = 2010
        self.install_until = 0.0
        self.radio = None
        self._budget_at = 0.0
        self.lock = threading.Lock()
        self._store_lock = threading.Lock()
        self._notify_q = queue.Queue()
        self._notify_thread = None
        self._bestand_gemeldet = {}

    # Wie lange ein frisch angelerntes Geraet im Posteingang steht, falls die
    # Bestaetigung ausbleibt. Wer nach einer Stunde nicht bestaetigt hat, will
    # es vermutlich nicht — dann soll der Hinweis auch nicht ewig stehen.
    FRISCH_HALTBARKEIT = 3600.0

    def merke_frisch_angelernt(self, address):
        """Ein Geraet ist angelernt — die Haussteuerung muss es noch aufnehmen.

        ⚠️ Der Grund steht in einer Anwendermeldung (19.08.2026): jemand hat
        erfolgreich angelernt und **das Geraet danach nicht gefunden**. Die
        Integration „Homematic(IP) Local" stellt jedes frisch angelernte Geraet
        absichtlich zurueck, bis es in den Reparaturen bestaetigt wurde — bis
        dahin gibt es in der Haussteuerung weder Schalter noch Sensoren, und
        wer das nicht weiss, sucht den Fehler beim Funk.

        Deshalb erscheint es solange im **Posteingang**, den die Integration
        selbst anzeigt (`sensor.<name>_inbox`) — mit dem Satz, der zu tun ist.
        Der Eintrag verschwindet, sobald die Bestaetigung erfolgt ist; sie ist
        daran zu erkennen, dass die Gegenstelle den Namen zurueckschreibt.
        """
        self.frisch_angelernt[(address or "").upper()] = time.time()

    def frisch_offen(self):
        """Die noch nicht bestaetigten Geraete — juengste zuerst."""
        jetzt = time.time()
        with self.lock:
            offen = [(t, a) for a, t in self.frisch_angelernt.items()
                     if jetzt - t < self.FRISCH_HALTBARKEIT and a in self.devices]
        return [a for _t, a in sorted(offen, reverse=True)]

    def merke_ereignis(self, art, text):
        """Ein Ereignis fuer die Oberflaeche festhalten.

        ⚠️ Das Protokoll des Behaelters taugt dafuer nicht: dort gehen die
        paar wichtigen Zeilen zwischen tausenden Abrufen der Haussteuerung
        unter (gemessen: 4000 Zeilen Protokoll, davon 59 zur Sache). Wer
        wissen will, was mit seiner Anlage geschehen ist, soll es in der
        Oberflaeche sehen und nicht in einer Datei suchen muessen.

        `art` steuert nur die Darstellung: ok · warn · bad · info.
        """
        self.ereignisse.append({"zeit": time.time(), "art": art, "text": text})

    def ereignis_liste(self, anzahl=25):
        """Die juengsten Ereignisse zuerst."""
        return list(self.ereignisse)[-anzahl:][::-1]

    def ereignisse_leeren(self):
        """„Zuletzt geschehen" auf Wunsch leeren (Dirk, 19.08.2026).

        Die Liste ist eine Erinnerungshilfe, kein Protokoll: wer sie gelesen
        und abgearbeitet hat, will mit leerem Blatt weitersehen, ob etwas
        Neues passiert. Das Protokoll der Zentrale bleibt unberuehrt — dort
        steht der Verlauf weiter, nachvollziehbar auch nach dem Leeren.
        """
        with self.lock:
            n = len(self.ereignisse)
            self.ereignisse.clear()
        return n

    # Ein Name darf die ReGa-Auskunft nicht zerlegen: dort trennt das
    # Semikolon die Felder und der Zeilenumbruch die Eintraege.
    NAME_MAX = 80

    @staticmethod
    def _name_saeubern(name):
        sauber = "".join(" " if c in ";\r\n\t" else c for c in (name or "")).strip()
        return sauber[:QCCU.NAME_MAX]

    def set_name(self, address, name):
        """Einen vom Anwender vergebenen Namen fuehren (Geraet oder Kanal).

        ⚠️ Eine Zentrale von eQ-3 fuehrt die Namen in ihrer ReGa-Datenbank,
        und die Haussteuerung verlaesst sich darauf. Die Integration
        „Homematic(IP) Local" haelt ein frisch angelerntes Geraet sogar
        ABSICHTLICH zurueck, bis der Anwender ihm einen Namen gegeben hat
        (Docstring ihres Reparatur-Flusses: „Fix flow for delayed devices:
        allows naming the device before adding it"), und schreibt den Namen
        anschliessend mit Device.setName / Channel.setName in die Zentrale.
        Ohne diese Buchfuehrung ging der eingegebene Name lautlos verloren —
        die Gegenstelle meldete beide Methoden als „not supported by the
        backend".

        Ein leerer Name loescht den Eintrag; danach gilt wieder der Name aus
        Geraetetyp und Adresse.
        """
        key = (address or "").upper()
        if not key:
            return False
        sauber = self._name_saeubern(name)
        with self.lock:
            vorher = self.names.get(key)
            if sauber:
                self.names[key] = sauber
            else:
                self.names.pop(key, None)
        # ⚠️ Schreibt die Gegenstelle einen Namen, hat sie das Geraet aufgenommen
        # — genau das ist der letzte Schritt ihres Reparatur-Dialogs. Damit ist
        # der Hinweis im Posteingang erledigt.
        self.frisch_angelernt.pop(key.split(":")[0], None)
        # Wartet die Meldung dieses Geraets noch auf seinen Namen, ist sie
        # jetzt faellig — die Entitaeten der Gegenstelle entstehen dann gleich
        # mit dem richtigen Namen.
        basis = key.split(":")[0]
        with self.lock:
            faellig = basis in self._freigabe
        if faellig:
            self._freigeben(basis)
        if sauber != vorher:
            self.merke_ereignis("info",
                                f"{key} heisst jetzt „{sauber}“" if sauber
                                else f"{key} traegt wieder seinen Vorgabenamen")
        self.save_store()
        return True

    def set_hoerer(self, address, opmode, quelle="hand"):
        """Den Betriebsmodus eines Geraets nachtragen.

        ⚠️ Der Weg fuer den BESTAND. Der Hoerertyp steht NUR im Anlernruf —
        weder in der Geraetebeschreibung des Herstellers (357 XML-Dateien des
        Jars geprueft: kein `listenerMode`) noch in irgendeiner Meldung des
        laufenden Betriebs. Ein vor 2026.8.45 angelerntes Geraet hat ihn
        darum nicht, und ein Geraet neu anzulernen, nur um ihn zu erfahren,
        ist bei einem Heizungsregler VERBOTEN: der Zentralenwechsel setzt die
        Ventiladaption zurueck, und waehrend der Adaption lehnt das Geraet
        jeden Stellbefehl ab.

        Bleibt: aus dem Rohmitschnitt lesen (`scripts/hoerertyp_aus_mitschnitt.py`)
        oder von Hand eintragen. Beides landet hier.

        `opmode is None` loescht den Eintrag wieder.
        """
        key = (address or "").upper()
        with self.lock:
            d = self.devices.get(key)
        if d is None:
            return False
        if opmode is None:
            neu, neue_quelle = None, None
        else:
            try:
                neu = int(opmode)
            except (TypeError, ValueError):
                return False
            if not 0 <= neu <= 0xFF or (neu & 0x0F) not in LM_GUELTIG:
                return False
            neue_quelle = quelle if quelle in LM_QUELLEN else "hand"
        vorher = d.opmode
        with self.lock:
            d.opmode = neu
            d.opmode_quelle = neue_quelle
        # Ausserhalb des Schlosses — `save_store` nimmt es selbst.
        self.save_store()
        if neu != vorher:
            self.merke_ereignis(
                "info",
                f"{key}: Hoerertyp {d.hoerer_text}" if neu is not None
                else f"{key}: Hoerertyp wieder unbekannt")
        return True

    def name_of(self, address, vorgabe=None):
        """Der gefuehrte Name — oder die Vorgabe, wenn keiner vergeben ist."""
        return self.names.get((address or "").upper()) or vorgabe

    def _namen_entfernen(self, address):
        """Beim Loeschen eines Geraets gehen seine Namen mit."""
        key = (address or "").upper()
        with self.lock:
            for k in [k for k in self.names
                      if k == key or k.startswith(key + ":")]:
                self.names.pop(k, None)

    def add_device(self, address, devtype, firmware="1.0.0", announce=True,
                   neu_angelernt=False, fassung=None, opmode=None,
                   opmode_quelle=None):
        d = Device(address, devtype, self.t, firmware, fassung,
                   opmode, opmode_quelle)
        with self.lock:
            self.devices[d.address] = d
        if self.verbose:
            print(f"  + {d.address} {d.label} ({len(d.channel_list())} Kanaele)")
        if announce and neu_angelernt and self.zurueckhalten:
            # Nicht melden — der Anwender nimmt es im Posteingang auf.
            with self.lock:
                self.warteraum.add(d.address)
            self.merke_ereignis("info", f"{d.label} {d.address} angelernt — "
                                        f"wartet auf Aufnahme in der "
                                        f"Haussteuerung")
        elif announce and self.subscribers:
            self._notify_new(d)
        if announce:
            self.save_store()
        return d

    # ---- Systemvariablen -------------------------------------------------
    #
    # ⚠️ Die Typen sind die von `HubValueType`: LOGIC, ALARM, FLOAT, INTEGER,
    # LIST, STRING. NUMBER gibt es dort auch, wird hier aber NICHT benutzt:
    # aiohomematic entscheidet bei NUMBER ueber `"." in raw_value`, ob es eine
    # Gleit- oder Ganzzahl ist — und diese Zeile steht ausserhalb seines
    # `try`. Eine Zahl statt einer Zeichenkette wirft dort einen TypeError,
    # der den GESAMTEN Abruf abbricht, nicht nur den einen Eintrag. Wer FLOAT
    # und INTEGER direkt nennt, umgeht die Falle.
    SYSVAR_TYPEN = ("LOGIC", "ALARM", "FLOAT", "INTEGER", "LIST", "STRING")

    def sysvar_anlegen(self, name, typ, wert=None, werte=None, mn=None,
                       mx=None, einheit="", beschreibung="", intern=False):
        """Eine Systemvariable anlegen oder eine vorhandene ueberschreiben."""
        name = str(name or "").strip()
        typ = str(typ or "").upper()
        if not name or typ not in self.SYSVAR_TYPEN:
            return None
        with self.lock:
            alt = self.sysvars.get(name)
            if alt:
                kennung = alt["id"]
            else:
                kennung = str(self._sysvar_id)
                self._sysvar_id += 1
            e = {"id": kennung, "typ": typ, "einheit": str(einheit or ""),
                 "beschreibung": str(beschreibung or ""), "intern": bool(intern)}
            if werte:
                e["werte"] = [str(w) for w in werte]
            # ⚠️ Grenzen NUR bei Zahlen. Die Gegenstelle parst `minValue` und
            # `maxValue` mit demselben Datentyp wie den Wert — an einer
            # LOGIC-Variablen mit Grenzen wirft ihr `to_bool` einen TypeError,
            # und dann verschwindet der Eintrag STILL aus der Liste (ihr
            # `except` verwirft ihn), ohne dass die QCCU etwas merkt.
            if typ in ("FLOAT", "INTEGER"):
                if mn is not None:
                    e["min"] = mn
                if mx is not None:
                    e["max"] = mx
            e["wert"] = self._sysvar_pruefen(typ, wert, e)
            self.sysvars[name] = e
        self.save_store()
        return e

    @staticmethod
    def _sysvar_pruefen(typ, wert, eintrag):
        """Einen Wert in die Form bringen, die der Typ verlangt."""
        try:
            if typ in ("LOGIC", "ALARM"):
                if isinstance(wert, str):
                    return wert.strip().lower() in ("1", "true", "yes", "on", "y", "t")
                return bool(wert)
            if typ == "FLOAT":
                return float(wert if wert is not None else 0.0)
            if typ in ("INTEGER", "LIST"):
                return int(float(wert if wert is not None else 0))
            return "" if wert is None else str(wert)
        except (TypeError, ValueError):
            # Ein unbrauchbarer Wert setzt die Variable auf ihren Grundwert,
            # statt einen Eintrag zu hinterlassen, den die Gegenstelle beim
            # Auslesen verwirft.
            return {"LOGIC": False, "ALARM": False, "FLOAT": 0.0,
                    "INTEGER": 0, "LIST": 0}.get(typ, "")

    def sysvar_setzen(self, name, wert):
        """Den Wert einer vorhandenen Systemvariablen aendern."""
        name = str(name or "").strip()
        with self.lock:
            e = self.sysvars.get(name)
            if not e:
                return False
            e["wert"] = self._sysvar_pruefen(e["typ"], wert, e)
        self.save_store()
        return True

    def sysvar_loeschen(self, name):
        """Eine Systemvariable entfernen."""
        with self.lock:
            weg = self.sysvars.pop(str(name or "").strip(), None)
        if weg:
            self.save_store()
        return bool(weg)

    def sysvar_liste(self):
        """Alle Systemvariablen — Name und Eintrag, sortiert."""
        with self.lock:
            return sorted(self.sysvars.items())

    def aufnehmen(self, address):
        """Ein wartendes Geraet freigeben und den Gegenstellen melden.

        Das ist `ReadyConfig(true)` einer Zentrale von eq-3: erst jetzt
        erfaehrt die Haussteuerung von dem Geraet. Rueckgabe: True, wenn
        dadurch etwas geschehen ist.
        """
        key = (address or "").upper().split(":")[0]
        with self.lock:
            d = self.devices.get(key)
            wartete = key in self.warteraum
            self.warteraum.discard(key)
        if d is None:
            return False
        if wartete:
            self.save_store()
            # ⚠️ NICHT sofort melden. Die Gegenstelle nimmt auf und schickt den
            # Namen unmittelbar danach (`accept_device_in_inbox.fn`, dann
            # `Device.setName`). Melden wir dazwischen, legt sie ihre
            # Entitaeten noch mit dem Vorgabenamen an — die Kennungen tragen
            # dann fuer immer die Seriennummer statt „Greta" (19.08.2026 am
            # Aufbau gesehen). Also: auf den Namen warten, hoechstens
            # FREIGABE_FRIST Sekunden. Wer ohne Namen aufnimmt, verliert dabei
            # nichts als diese paar Sekunden.
            with self.lock:
                alt = self._freigabe.pop(key, None)
            if alt is not None:
                alt.cancel()
            wecker = threading.Timer(self.FREIGABE_FRIST, self._freigeben, (key,))
            wecker.daemon = True
            with self.lock:
                self._freigabe[key] = wecker
            wecker.start()
        return wartete

    # Wie lange die Meldung auf den Namen wartet.
    FREIGABE_FRIST = 8.0

    def _freigeben(self, key):
        """Jetzt melden — der Name ist da oder kommt nicht mehr."""
        with self.lock:
            wecker = self._freigabe.pop(key, None)
            d = self.devices.get(key)
        if wecker is not None:
            wecker.cancel()
        if d is None:
            return
        self.merke_ereignis("ok", f"{self.name_of(key, d.label)} aufgenommen "
                                  f"— an die Haussteuerung gemeldet")
        if self.subscribers:
            self._notify_new(d)

    def wartet(self, address):
        """Steht dieses Geraet noch im Warteraum?"""
        with self.lock:
            return (address or "").upper().split(":")[0] in self.warteraum

    def warteraum_liste(self):
        """Die wartenden Geraete, juengste zuerst."""
        with self.lock:
            return [a for a in self.devices if a in self.warteraum]

    def set_store(self, path):
        self.store = path
        self.load_store()

    def load_store(self):
        if not self.store or not os.path.exists(self.store):
            return 0
        try:
            with open(self.store) as f:
                data = json.load(f)
        except Exception as ex:
            # ⚠️ „nicht lesbar" ist NICHT „leer". Bis zum 02.09.2026 lief QCCU
            # hier mit einem leeren Bestand weiter — und schrieb ihn beim
            # naechsten `save_store` ueber die noch vorhandene Datei. Ein
            # halb geschriebenes JSON (Neustart mitten im Speichern) oder eine
            # NUL-praefixierte Datei auf NFS kostete damit ALLE angelernten
            # Geraete, alle Funkbindungen und jeden gemessenen Hoerertyp —
            # endgueltig, waehrend die Geraete weiter funkten. Deshalb: die
            # kaputte Datei beiseitelegen und das Speichern SPERREN, bis ein
            # Mensch entschieden hat. QCCU laeuft weiter (Empfang und
            # Protokoll sind ja nuetzlich), aber es zerstoert nichts mehr.
            print(f"  ! Geraetespeicher nicht lesbar: {ex}")
            self.store_gesperrt = (
                f"Geraetespeicher {self.store} war beim Start nicht lesbar "
                f"({ex}). Es wird NICHTS gespeichert, damit der vorhandene "
                f"Bestand nicht ueberschrieben wird. Die Datei pruefen, "
                f"reparieren oder beiseiteraeumen und QCCU neu starten.")
            print(f"  ! {self.store_gesperrt}")
            try:
                import shutil
                sicherung = self.store + ".unlesbar"
                shutil.copy2(self.store, sicherung)
                print(f"  ! Kopie der unlesbaren Datei: {sicherung}")
            except Exception as ex2:                      # noqa: BLE001
                print(f"  ! Kopie scheiterte: {ex2}")
            return 0
        self.pair_next_addr = data.get("pair_next_addr")
        # ⚠️ VOR dem Laden der Geraete: `add_device` fragt ihn nicht, aber die
        # Menge muss stehen, bevor irgendetwas gemeldet wird.
        self.warteraum = {str(a).upper() for a in (data.get("warteraum") or [])}
        self.stick_serial = data.get("stick_serial")
        self.own_addr = data.get("own_addr")
        for k, v in (data.get("names") or {}).items():
            sauber = self._name_saeubern(v if isinstance(v, str) else "")
            if sauber:
                self.names[str(k).upper()] = sauber
        n = 0
        for addr, e in (data.get("devices") or {}).items():
            try:
                self.add_device(addr, int(e["devtype"]),
                                e.get("firmware", "1.0.0"), announce=False,
                                fassung=e.get("fassung"),
                                opmode=e.get("opmode"),
                                opmode_quelle=e.get("opmode_quelle"))
                if e.get("rf"):
                    self.rf[addr.upper()] = e["rf"].lower()
                gd = self.devices.get(addr.upper())
                for k, v in (e.get("values") or {}).items():
                    kanal, _, name = k.partition(":")
                    if gd is not None and kanal.isdigit() and name:
                        # Ereignisse (ACTION) aus aelteren Staenden nicht
                        # wieder zu Werten machen.
                        if self._ist_ereignis(gd, kanal, name):
                            continue
                        gd.values[(int(kanal), name)] = v
                if e.get("values_zeit"):
                    self._wert_zeit[addr.upper()] = e["values_zeit"]
                n += 1
            except Exception as ex:
                print(f"  ! Geraet {addr} nicht ladbar: {ex}")
        self.einstellungen.update(data.get("einstellungen") or {})
        # ⚠️ EIGENE Schleifennamen. `n` ist weiter oben der Geraetezaehler und
        # `e` der Geraeteeintrag — wer sie hier wiederverwendet, laesst
        # `load_store` den Namen einer Systemvariablen als Geraetezahl
        # zurueckgeben und protokolliert Unsinn.
        for sn, se in (data.get("sysvars") or {}).items():
            if not isinstance(se, dict) or se.get("typ") not in self.SYSVAR_TYPEN:
                continue
            if not str(se.get("id") or "").strip():
                continue
            # ⚠️ Beim Laden in Form bringen. Ein von Hand verbogener Speicher
            # brachte sonst einen Wert mit, an dem `SysVar.getAll` spaeter
            # scheitert — und dann faellt nicht der eine Eintrag aus, sondern
            # die ganze Auskunft.
            se = dict(se)
            se["id"] = str(se["id"])
            se["wert"] = self._sysvar_pruefen(se["typ"], se.get("wert"), se)
            self.sysvars[str(sn)] = se
        self._sysvar_id = max(int(data.get("sysvar_id") or 100),
                              *(int(v["id"]) + 1 for v in self.sysvars.values()
                                if str(v.get("id", "")).isdigit()),
                              100)
        subs = data.get("subscribers") or {}
        if subs:
            self.subscribers.update(subs)
            print(f"  Rueckrufe uebernommen: {', '.join(subs)}")
        if n:
            print(f"  Geraetespeicher: {n} Geraet(e) geladen")
        return n

    def save_store(self):
        """Atomar schreiben."""
        if not self.store:
            return
        # ⚠️ Sperre aus `load_store`: war die Datei beim Start unlesbar, wird
        # NICHT geschrieben. Sonst ueberschriebe ein leerer Bestand einen
        # vorhandenen — siehe die Begruendung dort.
        if getattr(self, "store_gesperrt", None):
            if not getattr(self, "_sperre_gemeldet", False):
                print(f"  ! Speichern gesperrt: {self.store_gesperrt}")
                self._sperre_gemeldet = True
            return
        with self.lock:
            data = {"devices": {a: {"devtype": d.devtype,
                                    "firmware": d.firmware,
                                    **({"fassung": d.fassung}
                                       if d.fassung else {}),
                                    # ⚠️ `is not None`, NICHT `if d.opmode`:
                                    # 0 ist ein gueltiger Wert
                                    # (PERMANENT_LISTENER, und genau den
                                    # meldet die HmIP-BWTH-A) und wuerde
                                    # sonst nie geschrieben.
                                    **({"opmode": d.opmode}
                                       if d.opmode is not None else {}),
                                    **({"opmode_quelle": d.opmode_quelle}
                                       if d.opmode_quelle else {}),
                                    "rf": self.rf.get(a),
                                    # Die zuletzt gemeldeten Werte. Sie sind
                                    # nicht die Wahrheit — die steht im Geraet
                                    # —, aber sie sind der letzte bekannte
                                    # Stand, und ohne sie steht nach einem
                                    # Neustart bis zur naechsten Meldung
                                    # ueberall nichts. Bei Geraeten, die sich
                                    # nur alle paar Stunden ruehren, ist das
                                    # der Unterschied zwischen einer Anzeige
                                    # und einem leeren Feld.
                                    "values": {f"{c}:{p}": v
                                               for (c, p), v in d.values.items()},
                                    "values_zeit": self._wert_zeit.get(a)}
                                for a, d in self.devices.items()}}
        if self.warteraum:
            data["warteraum"] = sorted(self.warteraum)
        if self.names:
            data["names"] = dict(self.names)
        if self.pair_next_addr:
            data["pair_next_addr"] = self.pair_next_addr
        if self.stick_serial:
            data["stick_serial"] = self.stick_serial
        if self.own_addr:
            data["own_addr"] = self.own_addr
        if self.subscribers:
            data["subscribers"] = dict(self.subscribers)
        if self.einstellungen:
            data["einstellungen"] = dict(self.einstellungen)
        # ⚠️ EIGENE Ebene. Eingerueckt unter `einstellungen` wurden die
        # Systemvariablen nur gesichert, wenn zufaellig auch Einstellungen
        # dastanden — im Betrieb durch die `wunsch_*`-Vorgaben verdeckt, im
        # Pruefstand und in jeder eingebetteten Nutzung ein stiller Verlust
        # beim Neustart.
        if self.sysvars:
            data["sysvars"] = {n: dict(e) for n, e in self.sysvars.items()}
            data["sysvar_id"] = self._sysvar_id
        with self._store_lock:
            try:
                tmp = self.store + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.store)
                # Auch das VERZEICHNIS sichern: `os.replace` ist erst mit dem
                # Verzeichniseintrag dauerhaft. Auf einem Netzdateisystem
                # zeigt ein Leser sonst zwischendurch eine halbe Datei (NUL am
                # Anfang) — auf dem Docker-Volume eine Feinheit, aber der
                # Weg ist derselbe, und er kostet nichts.
                try:
                    dfd = os.open(os.path.dirname(os.path.abspath(self.store))
                                  or ".", os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
            except Exception as ex:
                print(f"  ! Geraetespeicher nicht geschrieben: {ex}")

    def note_rf(self, address, rf):
        """Vom Funkpfad: Zentralen-Adresse <-> Funkadresse festhalten."""
        self.rf[address.upper()] = rf.lower()
        self.save_store()

    def set_value_internal(self, address, channel, param, value):
        """Vom Funkempfang aufzurufen: Wert eintragen und melden."""
        key = address.upper()
        with self.lock:
            d = self.devices.get(key)
            if not d:
                return False
            d.values[(int(channel), param)] = value
            self._wert_zeit[key] = int(time.time())
        self.note_reachable(key, True)
        self._notify(f"{key}:{channel}", param, value)
        self._werte_sichern()
        return True

    def event_internal(self, address, channel, param, value=True):
        """Ein Ereignis melden, OHNE es als Wert zu fuehren.

        So macht es die Zentrale mit Tastendruecken: die Legacy-Schicht
        schickt `client.event(kanal, "PRESS_SHORT", TRUE)` und legt nichts im
        Kanalzustand ab (`LegacyBackendNotificationHandler`, jar 04cb5e4e);
        ein spaeteres `getValue` auf den Parameter scheitert dort mit
        „Unknown Parameter value" (`DeviceUtil.getValue`: kein Wert, Parameter
        bekannt). Wer den Wert speicherte, zeigte Klienten einen Taster als
        dauerhaft „pressed" (HMCCU beim Anlegen, 03.09.2026 13:08).
        """
        key = address.upper()
        with self.lock:
            d = self.devices.get(key)
            if not d:
                return False
            self._wert_zeit[key] = int(time.time())
        self.note_reachable(key, True)
        self._notify(f"{key}:{channel}", param, value)
        return True

    # Wie oft die Werte hoechstens auf die Platte gehen. Bei jeder Meldung zu
    # schreiben waere teuer und unnoetig: was zaehlt, ist dass nach einem
    # Neustart ein brauchbarer Stand da ist, nicht der allerletzte.
    WERTE_ABSTAND = 20.0

    def _werte_sichern(self, sofort=False):
        """Die Werte sichern — hoechstens alle paar Sekunden."""
        if not self.store:
            return
        jetzt = time.time()
        self._werte_offen = True
        if not sofort and jetzt - self._werte_zuletzt < self.WERTE_ABSTAND:
            return
        self._werte_zuletzt = jetzt
        self._werte_offen = False
        self.save_store()

    def setInstallMode(self, on, seconds=60, mode=1, address=None):
        with self.lock:
            self.install_until = (time.time() + int(seconds)) if on else 0.0
        if self.verbose:
            print(f"  setInstallMode: {'offen fuer '+str(seconds)+' s' if on else 'zu'}")
        if self.on_install:
            try:
                self.on_install(bool(on), int(seconds))
            except Exception as ex:
                print(f"  ! Anlernpfad scheiterte: {ex}")
        return ""

    def getInstallMode(self):
        """Restzeit des Anlernfensters in Sekunden, 0 = zu.

        ⚠️ Gefragt wird AUCH der Funkpfad, nicht nur der eigene Merker. Das
        Fenster laesst sich naemlich auf zwei Wegen oeffnen: ueber diese
        Schnittstelle (`setInstallMode`) und direkt am Funkpfad (die
        Weboberflaeche und jeder Aufruf, der einen Aufkleber mitbringt, gehen
        dorthin, weil nur dort der Schluessel hinterlegt wird). Wer hier nur
        den eigenen Merker liest, meldet „zu", waehrend das Fenster laeuft —
        in Home Assistant stuende der Restzeit-Sensor dann auf 0, obwohl die
        Anlerntaste gerade gedrueckt werden soll. Genommen wird der groessere
        der beiden Werte; das ist die zutreffende Auskunft."""
        with self.lock:
            rest = self.install_until - time.time()
        rest = int(rest) if rest > 0 else 0
        radio = getattr(self, "radio", None)
        bis = getattr(radio, "pair_until", 0.0) or 0.0
        rest_funk = int(bis - time.time())
        return max(rest, rest_funk if rest_funk > 0 else 0)

    def _notify_new(self, device):
        """Neues Geraet an alle angemeldeten Gegenstellen melden."""
        self._enqueue(("new", device.address, device.descriptions()))

    def deleteDevices(self, address):
        """Geraet entfernen, ueber Funk ausschliessen, Gegenstellen melden."""
        key = address.upper().split(":")[0]
        with self.lock:
            d = self.devices.pop(key, None)
            rf = self.rf.pop(key, None)
        if not d:
            return ""

        # Erst dem Geraet Bescheid sagen, dann die Buecher fuehren: ohne den
        # Ausschluss ueber Funk merkt es nichts vom Rauswurf und liesse sich
        # nur noch mit einem Werksreset von Hand wieder anlernen.
        quittiert = None
        if rf and self.radio is not None:
            try:
                quittiert = self.radio.funk_exclude(rf)
            except Exception as ex:                      # noqa: BLE001
                print(f"  ! Ausschluss von {rf} scheiterte: {ex}")
                quittiert = False

        # ⚠️ Ob das Geraet den Ausschluss quittiert hat, ist die einzige
        # Angabe, aus der der Anwender etwas ableiten kann: ohne Quittung hat
        # es den Rauswurf nicht mitbekommen und braucht vor dem naechsten
        # Anlernen einen Werksreset. Das stand bisher nur im Protokoll.
        name = self.name_of(key, key)
        if quittiert is True:
            self.merke_ereignis("ok", f"{name} entfernt — das Geraet hat den "
                                      f"Funk-Ausschluss quittiert")
        elif quittiert is False:
            self.merke_ereignis("warn", f"{name} entfernt, aber OHNE Quittung "
                                        f"— das Geraet braucht vor dem naechsten "
                                        f"Anlernen einen Werksreset")
        else:
            self.merke_ereignis("info", f"{name} entfernt (kein Funk — das "
                                        f"Geraet weiss nichts davon)")

        self._namen_entfernen(key)
        self.frisch_angelernt.pop(key, None)
        with self.lock:
            self.warteraum.discard(key)
            wecker = self._freigabe.pop(key, None)
        if wecker is not None:
            wecker.cancel()
        self.save_store()
        addrs = [x["ADDRESS"] for x in d.descriptions()]
        self._enqueue(("delete", key, addrs))
        return ""

    on_install = None

    NOTIFY_MAX_FAILS = 5
    BESTAND_RUHE = 5.0   # Sekunden gegen doppelte Anmeldung

    def _notify(self, address, param, value):
        self._enqueue(("event", address, param, value))

    def _enqueue(self, job):
        """Rueckrufe laufen in einem eigenen Faden, nie im Aufrufer."""
        if not self.subscribers:
            return
        if self._notify_thread is None or not self._notify_thread.is_alive():
            self._notify_thread = threading.Thread(target=self._notify_worker,
                                                   name="rueckrufe", daemon=True)
            self._notify_thread.start()
        self._notify_q.put(job)

    def _notify_worker(self):
        while True:
            job = self._notify_q.get()
            try:
                self._deliver(job)
            except Exception as ex:
                print(f"  ! Rueckruf-Faden: {ex}")
            finally:
                self._notify_q.task_done()

    def _deliver(self, job):
        kind = job[0]
        for ident, url in list(self.subscribers.items()):
            try:
                p = rpc_proxy(url)
                if kind == "event":
                    p.event(ident, job[1], job[2], job[3])
                elif kind == "new":
                    p.newDevices(ident, job[2])
                    if self.verbose:
                        print(f"  newDevices -> {ident}: {job[1]}")
                elif kind == "bestand":
                    # Nur an den einen, der sich gerade angemeldet hat.
                    if ident != job[1]:
                        continue
                    p.newDevices(ident, job[2])
                    if self.verbose:
                        print(f"  newDevices (Bestand) -> {ident}: "
                              f"{len(job[2])} Eintraege")
                elif kind == "delete":
                    p.deleteDevices(ident, job[2])
                self._notify_fails.pop(ident, None)
            except Exception as ex:
                n = self._notify_fails.get(ident, 0) + 1
                self._notify_fails[ident] = n
                if self.verbose:
                    print(f"  ! Rueckruf an {url} scheiterte ({n}): {ex}")
                if n >= self.NOTIFY_MAX_FAILS:
                    self.subscribers.pop(ident, None)
                    self._notify_fails.pop(ident, None)
                    self.save_store()
                    print(f"  Rueckruf {ident} nach {n} Fehlschlaegen verworfen")

    def notify_flush(self, timeout=10.0):
        """Warten, bis alle anstehenden Rueckrufe zugestellt sind."""
        ende = time.time() + timeout
        while time.time() < ende:
            if self._notify_q.empty() and self._notify_q.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False

    def init(self, url, interface_id=""):
        """Anmeldung eines Rueckrufs. Leere Kennung = Abmeldung.

        ⚠️ Nach der Anmeldung wird der GERAETEBESTAND von sich aus gemeldet.
        Das ist keine Höflichkeit, sondern der Vertrag: eine Gegenstelle, die
        den Rueckruf-Weg geht, fragt die Geraete NICHT ab — sie meldet sich an
        und wartet darauf, dass die Zentrale `newDevices` schickt. Genau so
        verhaelt sich eine CCU. Wer das weglaesst, hat eine Verbindung, die
        sauber aufgebaut wird und in der nie ein Geraet erscheint (aiohomematic
        blieb daran haengen: `connected`, aber null Geraete).

        HMCCU faellt das nicht auf, weil es seine Geraeteliste ueber ReGa holt
        — deshalb ist die Luecke lange nicht aufgefallen.
        """
        if interface_id:
            voriges = self.subscribers.get(interface_id)
            self.subscribers[interface_id] = url
            self._notify_fails.pop(interface_id, None)
            self.save_store()
            if self.verbose:
                print(f"  init: {interface_id} -> {url}")
            # Gegen Flattern: meldet sich dieselbe Kennung mit derselben
            # Adresse binnen Sekunden erneut, hat die Gegenstelle den Bestand
            # noch. Ein neuer Abonnent, eine geaenderte Adresse oder eine
            # spaetere Anmeldung bekommen ihn immer — wer ihn braucht, darf
            # ihn nicht verpassen, das ist teurer als eine Wiederholung.
            jetzt = time.time()
            zuletzt = self._bestand_gemeldet.get(interface_id, 0.0)
            frisch = (voriges == url) and (jetzt - zuletzt) < self.BESTAND_RUHE
            if not frisch:
                with self.lock:
                    bestand = [beschr for d in self.devices.values()
                               for beschr in d.descriptions()]
                if bestand:
                    self._bestand_gemeldet[interface_id] = jetzt
                    # Ueber die Warteschlange, nie im Aufrufer: die Gegenstelle
                    # wartet noch auf die Antwort auf ihr eigenes `init`.
                    self._enqueue(("bestand", interface_id, bestand))
        else:
            for k, v in list(self.subscribers.items()):
                if v == url:
                    del self.subscribers[k]
                    self._notify_fails.pop(k, None)
            self.save_store()
            if self.verbose:
                print(f"  init: Abmeldung {url}")
        return ""

    def listDevices(self, interface_id=None):
        """Was die Gegenstelle als Bestand sieht.

        ⚠️ Wartende Geraete gehoeren NICHT hinein: sonst holt sich die
        Gegenstelle beim naechsten Anmelden genau das Geraet, das wir ihr
        gerade nicht melden wollten — und der Warteraum waere wirkungslos.
        """
        out = []
        with self.lock:
            for a, d in self.devices.items():
                if a in self.warteraum:
                    continue
                out.extend(d.descriptions())
        if self.verbose:
            print(f"  listDevices -> {len(out)} Eintraege")
        return out

    def getDeviceDescription(self, address):
        addr = address.upper()
        base = addr.split(":")[0]
        with self.lock:
            d = self.devices.get(base)
        if not d:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        for desc in d.descriptions():
            if desc["ADDRESS"] == addr:
                return desc
        raise xmlrpc.client.Fault(-2, "Unknown channel")

    def getParamsetDescription(self, address, paramset):
        addr = address.upper()
        base, _, ch = addr.partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        if not ch:
            return {}
        # `FASSUNG` (die Form eines Parameters, fuer den Sendepfad) ist kein
        # Feld der Schnittstelle — ein Klient bekaeme ein Feld, das keine
        # Zentrale kennt.
        return {p: ({k: v for k, v in e.items() if k != "FASSUNG"}
                    if isinstance(e, dict) else e)
                for p, e in self.t.paramset_of(d.devtype, ch, paramset).items()}

    def getParamset(self, address, paramset):
        addr = address.upper()
        base, _, ch = addr.partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d or not ch:
            return {}
        pset = str(paramset).upper()
        src = d.master if pset == "MASTER" else d.values
        gesetzt = {p: v for (c, p), v in src.items() if c == int(ch)
                   and not (pset != "MASTER" and self._ist_ereignis(d, ch, p))}
        if pset != "MASTER":
            return gesetzt
        # ⚠️ Bei MASTER liefert eine Zentrale von eQ-3 zu JEDEM beschriebenen
        # Parameter einen Wert — an der HmIP-PS-2 gemessen: 345 beschrieben,
        # 345 geliefert, in jedem Kanal deckungsgleich. Der Konfigurations-
        # bestand eines Geraets ist eben immer vollstaendig; was niemand
        # geaendert hat, steht auf seiner Vorgabe.
        #
        # Wir haben diese Werte nicht aus dem Geraet gelesen — sie stammen aus
        # der Beschreibung. Das ist die ehrlichste verfuegbare Auskunft: die
        # Alternative waere eine leere Antwort, und die liest die Gegenstelle
        # als „das Geraet hat die Haelfte seiner Parameter verloren" (Home
        # Assistant meldete genau das am 18.08.2026).
        aus_beschreibung = {
            p: e.get("DEFAULT")
            for p, e in self.t.paramset_of(d.devtype, ch, "MASTER").items()
            if isinstance(e, dict) and e.get("DEFAULT") is not None
        }
        aus_beschreibung.update(gesetzt)
        return aus_beschreibung

    def _ist_ereignis(self, d, ch, param):
        """Ist der Parameter vom Typ ACTION — ein Ereignis, das nie einen
        Wert hat? (Auch dann, wenn ein aelterer Stand einen abgelegt hat.)"""
        try:
            return (self.t.paramset_of(d.devtype, int(ch), "VALUES") or {}
                    ).get(param, {}).get("TYPE") == "ACTION"
        except Exception:                        # noqa: BLE001
            return False

    def getValue(self, address, param):
        """Einen Wert lesen — mit den Fehlern der Zentrale.

        `DeviceUtil.getValue` (jar 04cb5e4e, Legacy-Schicht) unterscheidet:
        Parameter unbekannt → -5 „Unknown Parameter for value key: X";
        Parameter bekannt, aber nie ein Wert → -5 „Unknown Parameter value for
        value key: X" (`NotificationUtil.getUnknownParameterValueRpcRemote-
        Exception`). Letzteres ist der Normalfall fuer Tasten: PRESS_SHORT
        wird nur als Ereignis gemeldet, nie abgelegt (`event_internal`).
        """
        base, _, ch = address.upper().partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        if not ch:
            raise xmlrpc.client.Fault(-5, f"Unknown Parameter for value key: {param}")
        v = d.values.get((int(ch), param))
        if v is not None and self._ist_ereignis(d, ch, param):
            v = None                             # ein Ereignis hat keinen Wert
        if v is None:
            try:
                bekannt = param in self.getParamsetDescription(f"{base}:{ch}", "VALUES")
            except Exception:                    # noqa: BLE001
                bekannt = False
            raise xmlrpc.client.Fault(
                -5, f"Unknown Parameter {'value ' if bekannt else ''}for value key: {param}")
        return v

    # Wie lange `setValue` auf den Ausgang wartet. Drei Anlaeufe mit Weckpaar,
    # Urteil und Antwortfenster brauchen bis zu ~6 s; danach gilt der Befehl
    # als nicht zugestellt. Der XML-RPC-Dienst ist einfaedig — so lange steht
    # er fuer andere Aufrufe. Die Zentrale von eq-3 haelt es genauso: ihr
    # `setValue` kehrt erst mit dem Ausgang der Transaktion zurueck.
    STELL_WARTEN = 8.0

    def setValue(self, address, param, value, *rest):
        """Schaltbefehl von aussen: senden, den Ausgang abwarten, DANN melden.

        ⚠️ Bis zum 02.09.2026 stand der Wert in Home Assistant und FHEM,
        bevor der Rahmen hinaus war — auch wenn das Geraet ihn ablehnte
        (BOOST_MODE am HmIP-BWTH-A, 30.08.: NAK, die Oberflaeche zeigte
        „an") oder gar nicht erreichbar war. Die Zentrale von eq-3 wartet die
        ANSWER ab und meldet eine Ablehnung als XML-RPC-Fehler
        `Generic error (RESPONSE_NAK)` (31.08.2026 an headlessCCU gemessen).
        Hier jetzt genauso, nach dem Ausgang des `Stellauftrag`s:

          angenommen (ANSWER ACK / Huckepack)  -> Wert eintragen und melden
          abgelehnt (NAK)                      -> Fehler, Wert unveraendert
          angekommen, aber keine Auskunft      -> nichts vorwegnehmen; was das
                                                  Geraet meldet, kommt ueber
                                                  seinen STATUS herein
          nicht angekommen / nicht stellbar    -> Fehler; UNREACH kommt aus
                                                  dem Sendepfad
        """
        base, _, ch = address.upper().partition(":")
        kanal = int(ch) if ch else 0
        if self.verbose:
            print(f"  setValue {address} {param}={value!r}")
        if not self.on_set:
            # Ohne Funkpfad gibt es keinen Ausgang abzuwarten.
            self._wert_uebernehmen(base, ch, param, value)
            return ""
        try:
            auftrag = self.on_set(base, kanal, param, value)
        except Exception as ex:
            print(f"  ! Sendepfad scheiterte: {ex}")
            raise xmlrpc.client.Fault(-1, "Generic error (SEND_FAILED)")
        if not hasattr(auftrag, "warten"):
            # Ein Funkpfad, der nur „quittiert ja/nein" zurueckgibt.
            if auftrag is not None:
                self.note_reachable(base, bool(auftrag))
            self._wert_uebernehmen(base, ch, param, value)
            return ""
        self._ausgang_abwarten(auftrag, base, ch, ((param, value),))
        return ""

    def _wert_uebernehmen(self, base, ch, param, value):
        """Einen gestellten Wert eintragen und den Gegenstellen melden."""
        with self.lock:
            d = self.devices.get(base)
            if d and ch:
                d.values[(int(ch), param)] = value
        self._notify(f"{base}:{ch}" if ch else base, param, value)

    def _ausgang_abwarten(self, auftrag, base, ch, satz):
        """Den Ausgang eines Stellauftrags in Wert, Meldung oder Fehler
        uebersetzen — siehe `setValue`."""
        name = self.name_of(base, base)
        namen = "+".join(p for p, _ in satz)
        if not auftrag.warten(self.STELL_WARTEN):
            self.merke_ereignis("warn", f"{name}: {namen} — kein Ausgang nach "
                                        f"{self.STELL_WARTEN:.0f} s")
            raise xmlrpc.client.Fault(-1, "Generic error (TIMEOUT)")
        if auftrag.antwort is True:
            for p, v in satz:
                self._wert_uebernehmen(base, ch, p, v)
            return
        if auftrag.antwort is False:
            # Dieselbe Meldung, die die Zentrale von eq-3 dem Klienten gibt.
            self.merke_ereignis("warn", f"{name}: {namen} vom Geraet abgelehnt "
                                        f"({auftrag.klartext})")
            raise xmlrpc.client.Fault(-1, "Generic error (RESPONSE_NAK)")
        if auftrag.mac:
            # Angekommen, keine Auskunft auf Anwendungsebene: nichts eintragen.
            # Der naechste STATUS des Geraets sagt, was gilt.
            if self.verbose:
                print(f"  {base}:{ch} {namen}: angekommen, {auftrag.klartext} "
                      f"— Wert kommt mit dem Status des Geraets")
            return
        if auftrag.mac is None:
            # Nie hinausgegangen: keine Funkadresse, keine belegte Form, kein
            # belegter Satz. Das ist kein Zeitablauf, sondern eine Absage.
            raise xmlrpc.client.Fault(
                -5, f"No proven way to set {namen} on this device "
                    f"({auftrag.klartext})")
        # Nicht angekommen: die UNREACH-Meldung setzt der Sendepfad; hier nur
        # die Wahrheit an den Aufrufer.
        raise xmlrpc.client.Fault(-1, "Generic error (TIMEOUT)")

    on_set = None
    on_set_many = None
    kann_stellen = None

    def putParamset(self, address, paramset, values, *rest):
        addr = address.upper()
        base, _, ch = addr.partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        if not isinstance(values, dict):
            raise xmlrpc.client.Fault(-5, "Parameter set must be a struct")

        ps = str(paramset).upper()
        if ps == "VALUES":
            if not ch:
                raise xmlrpc.client.Fault(-2, "Unknown channel")
            return self._put_values(addr, base, int(ch), d, values)

        if ps.startswith("MASTER") or ps.startswith("LINK"):
            print(f"  ! putParamset {addr} {ps}: kein Schreibweg zum Geraet "
                  f"(Konfiguration wird nicht uebertragen)")
            raise xmlrpc.client.Fault(
                -5, f"Writing paramset {ps} to the device is not implemented")
        raise xmlrpc.client.Fault(-5, f"Unknown paramset {ps}")

    def _put_values(self, addr, base, ch, d, values):
        """Einen SATZ von Werten schreiben — in EINEM Funkrahmen.

        ⚠️ Nicht Wert fuer Wert durch `setValue`. Beide Integrationen, die
        QCCU bedienen, buendeln hier:

          * `aiohomematic` (Home Assistant) schickt bei EINEM Wert `setValue`,
            bei mehreren `putParamset` (`model/data_point.py`,
            `CallParameterCollector._send_paramset`). Fuer „manuell mit
            Solltemperatur" sind das CONTROL_MODE und SET_POINT_TEMPERATURE.
          * `HMCCU` (FHEM) schreibt GRUNDSAETZLICH ueber `putParamset`
            (`HMCCU_SetMultipleParameters`); `set manu` ist dort
            `V:CONTROL_MODE:1 V:SET_POINT_TEMPERATURE:?temperature`.

        Und die Zentrale baut daraus EINEN Rahmen: Werte mit gleichem
        <Code, Datentyp, Occurrence> werden verodert. Einzeln geschickt sieht
        das Geraet dazwischen einen Zwischenzustand — bei der Betriebsart
        „manuell mit dem alten Sollwert".

        ⚠️ Was keinen belegten Weg zum Geraet hat, wird NICHT stillschweigend
        eingetragen und zurueckgemeldet. Sonst steht der Wert in Home
        Assistant und FHEM, ohne dass ihn je ein Geraet gesehen hat — beim
        Abwesenheitsmodus (SET_POINT_MODE + PARTY_TIME_START/END) waere das
        der ganze Befehl.
        """
        satz = [(str(p), v) for p, v in values.items()]

        unbekannt = [p for p, _ in satz
                     if not isinstance(d.paramset(ch).get(p), dict)]
        if unbekannt:
            raise xmlrpc.client.Fault(
                -5, f"Unknown parameter(s): {', '.join(sorted(unbekannt))}")

        if self.kann_stellen and not self.kann_stellen(base, ch, satz):
            namen = ", ".join(p for p, _ in satz)
            print(f"  ! putParamset {addr}: fuer {namen} gibt es keinen "
                  f"belegten Weg zum Geraet — nichts eingetragen")
            raise xmlrpc.client.Fault(
                -5, f"No proven way to set {namen} on this device")

        if self.on_set_many:
            auftrag = self.on_set_many(base, ch, satz)
            if hasattr(auftrag, "warten"):
                # Wie `setValue`: erst der Ausgang, dann der Wert.
                self._ausgang_abwarten(auftrag, base, str(ch), satz)
                return ""
        else:
            for p, v in satz:
                self.setValue(f"{base}:{ch}", p, v)
            return ""

        with self.lock:
            for p, v in satz:
                d.values[(ch, p)] = v
        for p, v in satz:
            self._notify(f"{base}:{ch}", p, v)
        return ""

    def getParamsetId(self, address, paramset):
        """Kennung eines Paramsets."""
        addr = address.upper()
        base, _, ch = addr.partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        ctype = self.t.channels_of(d.devtype).get(ch, "") if ch else self.t.label_of(d.devtype)
        return f"{ctype}:{str(paramset).upper()}"

    def note_reachable(self, address, reachable):
        key = address.upper().split(":")[0]
        with self.lock:
            d = self.devices.get(key)
            if not d:
                return
            if reachable:
                d.last_seen = time.time()
            changed = d.unreach != (not reachable)
            d.unreach = not reachable
        if changed:
            if self.verbose:
                print(f"  {key}: {'NICHT erreichbar' if not reachable else 'wieder erreichbar'}")
            name = self.name_of(key, key)
            self.merke_ereignis("warn" if not reachable else "ok",
                                f"{name} meldet sich nicht mehr" if not reachable
                                else f"{name} ist wieder da")
            self._notify(f"{key}:0", "UNREACH", not reachable)

    # Was ausser UNREACH als Dienstmeldung gilt: die Wartungswerte des
    # Kanals 0, die ein Eingreifen verlangen. LOW_BAT und SABOTAGE meldet das
    # Geraet selbst (HmIP-SCI, 03.09.2026: SABOTAGE=true beim Anlernen mit
    # offenem Gehaeuse); die Zentrale von eq-3 fuehrt beide ebenfalls als
    # Dienstmeldung. Nur, wenn der Wert WAHR ist.
    DIENSTMELDUNGEN = ("LOW_BAT", "SABOTAGE")

    def getServiceMessages(self, *rest):
        out = []
        with self.lock:
            for a, d in self.devices.items():
                if d.unreach:
                    out.append([f"{a}:0", "UNREACH", True])
                for name in self.DIENSTMELDUNGEN:
                    if d.values.get((0, name)) is True:
                        out.append([f"{a}:0", name, True])
        return out

    def getSuppressedServiceMessages(self, *rest):
        return []

    def getLinks(self, address=None, flags=None):
        return []

    def getLinkPeers(self, address=None, flags=None):
        return []

    def getLinkInfo(self, sender, receiver):
        return ["", ""]

    def addLink(self, sender, receiver, name="", description=""):
        raise xmlrpc.client.Fault(-5, "Direct links are not implemented")

    def removeLink(self, sender, receiver):
        raise xmlrpc.client.Fault(-5, "Direct links are not implemented")

    def setLinkInfo(self, sender, receiver, name="", description=""):
        raise xmlrpc.client.Fault(-5, "Direct links are not implemented")

    def reportValueUsage(self, address, value_id, ref_counter):
        """Der Klient beobachtet einen Wert — dann soll das Geraet ihn auch
        melden.

        So haelt es die Zentrale von eq-3 (`LegacyServiceHandler.
        reportValueUsage` -> `DeviceUtil.createCentralLink`): ein Zaehler
        ueber 0 legt die Verknuepfung des Kanals zur Zentrale an, sofern der
        Kanal eine Link-Rolle hat. Ohne diese Verknuepfung schickt ein
        Sender-Kanal sein Ereignis nicht als STATUS. QCCU legt sie zwar schon
        beim Anlernen an; ein Klient, der hier anklopft, bekommt sie
        trotzdem — auch fuer Geraete, die vor dieser Fassung angelernt wurden.
        """
        try:
            base, _, ch = address.upper().partition(":")
            if ch and int(ref_counter or 0) > 0 and self.radio is not None:
                self.radio.zentralen_verknuepfen(base, [int(ch)])
        except Exception as ex:                          # noqa: BLE001
            print(f"  ! reportValueUsage {address}: {ex}")
        return ""

    def rssiInfo(self):
        """Empfangsguete je Geraet."""
        return {}

    def listBidcosInterfaces(self):
        """Die Funkschnittstellen der Zentrale."""
        addr = "000000"
        duty = 0
        connected = False
        if self.radio is not None:
            addr = (getattr(self.radio, "own_addr", None) or "000000").upper()
            b = self._budget()
            connected = True
            if b and b.get("max"):
                duty = int(round((b["max"] - b["credit"]) * 100.0 / b["max"]))
                duty = max(0, min(100, duty))
                # ⚠️ `lovf` ist ein SEIT DEM START LAUFENDER Zaehler, kein
                # Zustand — der Stick setzt ihn nur bei `mZ` zurueck. Wer
                # daraus `duty = 100` macht, nagelt die Anzeige nach dem
                # ersten erschoepften Vorrat fuer immer auf 100 %, auch wenn
                # das Konto laengst wieder voll ist. Die Haussteuerung liest
                # daraus eine dauerhaft ueberlastete Zentrale.
                # Aufgefallen ist es erst, als der Vorlauf gebucht wurde und
                # LOVF von „nie" zu „gelegentlich" wurde.
                # Der Zaehler bleibt sichtbar — die Oberflaeche zeigt ihn
                # neben dem Vorrat —, aber der Auslastungswert kommt jetzt
                # allein aus dem Vorrat.
                if b["credit"] <= 0:
                    duty = 100
        return [{
            "ADDRESS": addr,
            "DESCRIPTION": f"{self.interface_name} {addr}",
            "CONNECTED": connected,
            "DEFAULT": True,
            "TYPE": "QCCU",
            "FIRMWARE_VERSION": VERSION,
            "DUTY_CYCLE": duty,
        }]

    def _budget(self):
        """Sendezeit-Konto des Sticks, hoechstens alle 5 s frisch geholt."""
        if self.radio is None:
            return None
        now = time.time()
        if now - self._budget_at > 5.0:
            try:
                self.radio.radio_state()
                self._budget_at = now
            except Exception as ex:
                if self.verbose:
                    print(f"  ! Sendezeit nicht lesbar: {ex}")
        return getattr(self.radio, "budget", None)

    def clientServerInitialized(self, interface_id=""):
        return 1 if interface_id in self.subscribers else 0

    def deleteDevice(self, address, flags=0):
        """Loeschen aus Sicht des Klienten."""
        return self.deleteDevices(address)

    def listMethods(self):
        return self._listMethods()

    def getVersion(self):
        return VERSION

    def ping(self, caller=""):
        self._notify("CENTRAL", "PONG", caller)
        return True

    KNOWN_UNIMPLEMENTED = (
        "activateLinkParamset", "abortDeleteDevice", "addDevice", "changeKey",
        "clearConfigCache", "determineParameter", "getKeyMismatchDevice",
        "getMetadata", "installFirmware", "listReplaceableDevices",
        "listTeams", "logLevel", "refreshDeployedDeviceFirmwareList",
        "replaceDevice", "restoreConfigToDevice", "searchDevices",
        "setBidcosInterface", "setInterfaceClock", "setMetadata",
        "setTeam", "setTempKey", "suppressServiceMessages",
        "updateFirmware",
    )

    def _listMethods(self):
        """Unterstuetzte Methoden."""
        return sorted({
            "init", "listDevices", "getDeviceDescription",
            "getParamsetDescription", "getParamset", "putParamset",
            "getParamsetId", "getValue", "setValue",
            "setInstallMode", "setInstallModeWithWhitelist", "getInstallMode",
            "deleteDevice", "deleteDevices", "getVersion", "ping",
            "getLinks", "getLinkPeers", "getLinkInfo", "addLink",
            "removeLink", "setLinkInfo",
            "getServiceMessages", "getSuppressedServiceMessages",
            "reportValueUsage", "rssiInfo", "listBidcosInterfaces",
            "clientServerInitialized",
            "system.listMethods", "system.methodHelp",
            "system.methodSignature", "system.multicall",
        } | set(self.KNOWN_UNIMPLEMENTED))

    def setInstallModeWithWhitelist(self, on, seconds=60, whitelist=None):
        """Wie setInstallMode, mit Geraeteliste."""
        return self.setInstallMode(on, seconds)

    def _dispatch(self, method, params):
        m = None
        if method == "listMethods" or method in self._listMethods():
            m = getattr(self, method, None)
        if callable(m) and not method.startswith("_"):
            return m(*params)
        why = ("nicht umgesetzt" if method in self.KNOWN_UNIMPLEMENTED
               else "unbekannt")
        print(f"  ? {method}({', '.join(repr(p) for p in params)[:100]}) "
              f"— {why}, leer beantwortet")
        return ""


def chan_name(address, lc=None):
    """Der Name, unter dem ein Kanal in der ReGa-Auskunft steht.

    Hat der Anwender einen Namen vergeben (Channel.setName), gilt der — so
    haelt es auch eine Zentrale von eQ-3. Sonst bleibt es beim Aufbau aus der
    Adresse, der wenigstens eindeutig ist."""
    if lc is not None:
        gefuehrt = lc.name_of(address)
        if gefuehrt:
            return gefuehrt
    return address.replace(":", "_")


# ValueType der ReGa-Datenpunkte, wie HMCCU_FormatDeviceInfo sie liest
# (88_HMCCU.pm: 0 n, 2 b, 4 f, 6 a, 8 n, 11 s, 16 i, 20 s, 23 p, 29 e).
# ACTION wird hier wie ein Schaltwert (2) gefuehrt — ein Tastendruck ist ein
# Wahrheitswert ohne Zustand; eine eigene Nummer nennt HMCCU dafuer nicht.
REGA_VALUETYPE = {"BOOL": 2, "ACTION": 2, "FLOAT": 4, "INTEGER": 16,
                  "STRING": 20, "ENUM": 29}
RE_DP_SET = re.compile(r'datapoints\.Get\("([^"]+)"\)\)\.State\(\s*([^)]+?)\s*\)')
RE_DP_GET = re.compile(r'datapoints\.Get\("([^"]+)"\)\)\.(?:State|Value)\(\s*\)')


def rega_value_in(text):
    """Wert aus dem Skripttext."""
    t = text.strip()
    if t in ("true", "false"):
        return t == "true"
    if len(t) >= 2 and t[0] in "'\"" and t[-1] == t[0]:
        return t[1:-1]
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def rega_value_out(value):
    """Wert fuer die Antwort."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def rega_answer(lc, script):
    """Die Antwort auf ein Skript — ohne Skriptdeuter."""
    s = script or ""

    if "root.Devices()" in s and "Interfaces()" in s:
        out = []
        with lc.lock:
            devs = list(lc.devices.values())
        for d in devs:
            chans = d.channel_list()
            for idx, ctype in chans:
                addr = f"{d.address}:{idx}"
                out.append(f"C;{addr};{chan_name(addr, lc)};"
                           f"{channel_direction(ctype)}")
            out.append(f"D;{lc.interface_name};{d.address};"
                       f"{d.address};{lc.name_of(d.address, d.label)};"
                       f"{len(chans)}")
        out.append(f"I;{lc.interface_name};{lc.interface_name};"
                   f"xmlrpc_bin://{lc.own_host}:{lc.rpc_port}")
        return "\n".join(out) + "\n", f"Geraeteliste ({len(devs)})"

    # `get ccu deviceinfo` (HMCCU, Skript "GetDeviceInfo" aus HMCCUConf.pm):
    # je Datenpunkt eine Zeile, die HMCCU_FormatDeviceInfo in sieben Felder
    # zerlegt — C;Kanaladresse;Kanalname;Datenpunktname;ValueType;Wert;Flags.
    # Vorher eine D-Zeile mit Schnittstelle, Adresse, Name und HssType.
    # ⚠️ Erkannt an HssType() UND ValueType(): das Datenpunkt-Skript unten
    # nennt ebenfalls ID_DEVICES und OPERATION_READ.
    if "ID_DEVICES" in s and "HssType()" in s and "ValueType()" in s:
        m = re.search(r'\.Get\("([^"]*)"\)', s)
        gesucht = (m.group(1) if m else "").strip()
        with lc.lock:
            d = lc.devices.get(gesucht.upper())
            kandidaten = list(lc.devices.items())
        if d is None:
            for a, e in kandidaten:
                if gesucht and lc.name_of(a) == gesucht:
                    d = e
                    break
        if d is None:
            return "ERROR: Device not found\n", "Geraeteinfo (unbekannt)"
        out = [f"D;{lc.interface_name};{d.address};"
               f"{lc.name_of(d.address, d.address)};{d.label}"]
        for idx, _ctype in d.channel_list():
            ca = f"{d.address}:{idx}"
            for dp, desc in sorted(d.paramset(idx, "VALUES").items()):
                if not isinstance(desc, dict):
                    continue
                ops = desc.get("OPERATIONS")
                ops = ops if isinstance(ops, int) else 0
                flags = "".join(b for bit, b in ((1, "R"), (2, "W"), (4, "E"))
                                if ops & bit)
                with lc.lock:
                    v = d.values.get((idx, dp))
                out.append(f"C;{ca};{chan_name(ca, lc)};"
                           f"{lc.interface_name}.{ca}.{dp};"
                           f"{REGA_VALUETYPE.get(desc.get('TYPE'), 20)};"
                           f"{'' if v is None else rega_value_out(v)};{flags}")
        return "\n".join(out) + "\n", f"Geraeteinfo {d.address} ({len(out) - 1} Datenpunkte)"
    sets = RE_DP_SET.findall(s)
    if sets:
        done = []
        for dpid, raw in sets:
            parts = dpid.split(".")
            if len(parts) < 3:
                continue
            chnaddr, dpt = parts[1], parts[2]
            lc.setValue(chnaddr, dpt, rega_value_in(raw))
            done.append(f"{chnaddr}.{dpt}={raw}")
        return "\n", "setzen " + " ".join(done)

    gets = RE_DP_GET.findall(s)
    if gets:
        out = []
        for dpid in gets:
            parts = dpid.split(".")
            if len(parts) < 3:
                continue
            chnaddr, dpt = parts[1], parts[2]
            base, _, ch = chnaddr.partition(":")
            with lc.lock:
                d = lc.devices.get(base.upper())
            v = d.values.get((int(ch), dpt)) if (d and ch) else None
            out.append("" if v is None else rega_value_out(v))
        return "".join(out) + "\n", "lesen " + ",".join(gets)

    if "sDevList" in s and "DPs()" in s:
        m = re.search(r'sDevList\s*=\s*"([^"]*)"', s)
        wanted = [x.strip().upper() for x in (m.group(1).split(",") if m else []) if x.strip()]
        out = []
        with lc.lock:
            devs = [d for a, d in lc.devices.items() if not wanted or a in wanted]
            snapshot = [(d.address, dict(d.values)) for d in devs]
        for addr, values in snapshot:
            for (ch, dpt), v in sorted(values.items()):
                ca = f"{addr}:{ch}"
                out.append(f"{chan_name(ca, lc)}={lc.interface_name}.{ca}.{dpt}="
                           f"{rega_value_out(v)}")
        out.append(str(len(out)))
        return "\n".join(out) + "\n", f"Datenpunkte ({len(out) - 1})"

    if "setInstallMode" in s or "InstallMode" in s:
        m = re.search(r"(\d+)", s)
        lc.setInstallMode(True, int(m.group(1)) if m else 60)
        return "\n", "Anlernfenster"

    if "cat /VERSION" in s or "GetVersion" in s:
        return (f"VERSION={VERSION}\nPRODUCT={PRODUKT}\nPLATFORM=qccu\n",
                "Fassung")

    if "groups.gson" in s or "system.Exec" in s:
        return "\n", "Gruppen (keine)"

    return "\n", None


class RegaHandler(BaseHTTPRequestHandler):
    """Beantwortet die Skripte, die die Gegenstelle schickt."""
    server_version = "QCCU"
    qccu = None

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        out = self._answer(body)
        data = out.encode("iso-8859-1", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=ISO-8859-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST

    def _answer(self, script):
        s = script or ""
        lc = self.qccu

        if getattr(lc, "rega_log", None):
            try:
                with open(lc.rega_log, "a", encoding="utf-8") as f:
                    f.write("=" * 60 + "\n")
                    f.write(time.strftime("%H:%M:%S") + "  " + self.path + "\n")
                    f.write(s + "\n")
            except Exception:
                pass
        t0 = time.time()
        out, note = rega_answer(lc, s)
        if note and getattr(lc, "radio", None) is not None:
            try:
                lc.radio._log("##", f"ReGa {note} — beantwortet in "
                                    f"{(time.time() - t0) * 1000:.0f} ms")
            except Exception:
                pass
        if lc.verbose and s.strip():
            if note:
                print(f"  ReGa: {note}")
            else:
                print(f"  ReGa ? {' '.join(s.split())[:140]}")
        return out


PORTFELDER = (("cul_port", "CUL-Zugang"), ("rpc_port", "XML-RPC HmIP-RF"),
              ("bidcos_port", "XML-RPC BidCos-RF"), ("rega_port", "ReGa"),
              ("json_port", "JSON-RPC"), ("web_port", "Weboberflaeche"))
PORT_VERSATZ = 10000


def port_frei(bind, port):
    """Laesst sich dieser Port oeffnen?

    ⚠️ Mit SO_REUSEADDR pruefen — genau so binden die Dienste selbst. Ohne
    das meldet ein Port, auf dem eben noch etwas lief, faelschlich „belegt"
    (TIME_WAIT), und wir wichen ohne Grund aus.
    """
    if not port:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((bind, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ports_waehlen(g):
    """Ports pruefen und noetigenfalls ausweichen. Gibt Meldezeilen zurueck.

    Laeuft auf derselben Maschine schon eine OCCU, sind 2000, 2001, 2010 und
    8181 belegt. Statt beim Binden abzustuerzen, wird um `PORT_VERSATZ`
    verschoben — die Endziffern bleiben dabei erhalten (2010 -> 12010).
    `0` heisst „Dienst aus" und bleibt aus.
    """
    def belegte():
        return [(name, getattr(g, feld))
                for feld, name in PORTFELDER
                if not port_frei(g.bind, getattr(g, feld, 0))]

    def verschieben():
        for feld, _ in PORTFELDER:
            wert = getattr(g, feld, 0)
            if wert:
                setattr(g, feld, wert + PORT_VERSATZ)

    zeilen = []
    if g.alt_ports:
        verschieben()
        zeilen.append(f"  Ausweichports: alle Dienste um {PORT_VERSATZ} verschoben")
    else:
        weg = belegte()
        if weg:
            zeilen.append("  ! Belegt: " + ", ".join(f"{n} {p}" for n, p in weg))
            verschieben()
            g.alt_ports = True
            zeilen.append(f"  Ausweichports: alle Dienste um {PORT_VERSATZ} "
                          f"verschoben (laeuft hier schon eine OCCU?)")
    noch = belegte()
    if noch:
        zeilen.append("  ! Auch belegt: "
                      + ", ".join(f"{n} {p}" for n, p in noch)
                      + " — diese Dienste werden nicht starten.")
    return zeilen


def zeitlage_melden():
    """Die Ortszeit ansagen, mit der QCCU die Geraete versorgt.

    ⚠️ Das ist keine Zierde. Ein frisch angelerntes HmIP-Geraet fragt nach
    einem Zeitgeber und bekommt von QCCU die ORTSZEIT dieses Rechners
    (`Radio.zeit_payload`, so hat es die echte Zentrale im Referenzmitschnitt
    getan). Steht im Behaelter keine Zeitzone, ist das UTC — ein Thermostat
    fuehrt sein Wochenprogramm dann zwei Stunden daneben aus. Am 30.08.2026
    an einem HmIP-BWTH-A gemessen: es bekam 23:45 statt 01:45 und schaltete
    entsprechend um.

    Der Supervisor von Home Assistant setzt `TZ` bei Erweiterungen selbst; wer
    den Behaelter von Hand startet, muss es tun. Darum wird hier gesagt, was
    tatsaechlich gilt — nicht, was gemeint war.
    """
    jetzt = time.localtime()
    versatz = -(time.altzone if jetzt.tm_isdst else time.timezone)
    zone = time.tzname[1 if jetzt.tm_isdst else 0]
    vz = "+" if versatz >= 0 else "-"
    print(f"  Ortszeit {time.strftime('%d.%m.%Y %H:%M:%S', jetzt)} "
          f"({zone}, UTC{vz}{abs(versatz) // 3600:02d}:{abs(versatz) % 3600 // 60:02d})"
          f" — diese Zeit bekommen die Geraete.")
    if not os.environ.get("TZ") and versatz == 0:
        print("  ! Keine Zeitzone gesetzt (TZ), es gilt UTC. Ein Thermostat "
              "fuehrt sein Wochenprogramm dann versetzt aus.")
        print("    Abhilfe beim Behaelter: -e TZ=Europe/Berlin")


def main():
    a = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--tables", default="tables", help="Verzeichnis der Tabellen")
    a.add_argument("--firmware", default=None,
                   help="mitgelieferte Stick-Firmware (.hex) fuer die Oberflaeche")
    a.add_argument("--cul-port", type=int, default=0,
                   help="TCP-Zugang im culfw-Stil fuer BidCoS/AskSin "
                        "(0 = aus). In FHEM: define cul CUL <rechner>:<port> 1234")
    a.add_argument("--rpc-port", type=int, default=2010)
    # ⚠️ Vorgabe 0 = AUS. Die BidCoS-Schnittstelle liest bis auf Weiteres nur
    # mit; wer sie einschaltet, bekommt einen zweiten XML-RPC-Dienst auf 2001,
    # wie ihn eine Zentrale von eQ-3 dort anbietet. Solange im selben Funknetz
    # FHEM/CUL_HM die Zentrale spielt, gehoert sie AUS.
    a.add_argument("--bidcos-port", type=int, default=0, metavar="PORT",
                   help="XML-RPC-Port der Schnittstelle BidCos-RF "
                        "(0 = aus, uebliche Wahl 2001)")
    a.add_argument("--bidcos-state", default=None, metavar="DATEI",
                   help="Zustand der BidCoS-Schnittstelle "
                        "(Vorgabe: neben --state)")
    # ⚠️ Ohne diesen Schalter liest die BidCoS-Schnittstelle nur mit. Mit ihm
    # quittiert sie Rahmen an die eigene Adresse und schliesst Anlernvorgaenge
    # ab — sie greift also in den Funk ein. Das gehoert ausdruecklich gewollt,
    # nicht als Nebenwirkung des Einschaltens.
    a.add_argument("--bidcos-senden", action="store_true",
                   help="die BidCoS-Schnittstelle darf senden (quittieren und "
                        "anlernen). Ohne dies liest sie nur mit.")
    a.add_argument("--bidcos-fremd", default="", metavar="ADRESSEN",
                   help="Adressen fremder Zentralen im selben Funknetz, "
                        "durch Komma getrennt — sie werden als eigene Adresse "
                        "AUSGESCHLOSSEN und ihr Verkehr nicht quittiert")
    a.add_argument("--rega-port", type=int, default=8181)
    a.add_argument("--json-port", type=int, default=8082,
                   help="JSON-RPC-Auskunft fuer Home Assistant (aiohomematic / "
                        "Homematic(IP) Local). 0 = aus. Ergaenzt die "
                        "XML-RPC-Auskunft, ersetzt sie nicht.")
    a.add_argument("--web-port", type=int, default=8080,
                   help="Weboberflaeche (Anlernen, Geraete, Sendezeit). 0 = aus.")
    a.add_argument("--bind", default="0.0.0.0")
    # Ausweichports fuer den Fall, dass auf derselben Maschine schon eine
    # OCCU/RaspberryMatic laeuft: die belegt 2000, 2001, 2010 und 8181, und
    # zwei Dienste auf demselben Port gibt es nicht. Der Schalter verschiebt
    # ALLE eingeschalteten Ports gleichmaessig um 10000 — die Endziffern
    # bleiben dabei erhalten (2010 -> 12010, 8082 -> 18082), die Zuordnung
    # ist also ablesbar. Ausgeschaltete Dienste (0) bleiben aus.
    a.add_argument("--localhost", action="store_true",
                   help="nur auf 127.0.0.1 lauschen — von aussen nicht "
                        "erreichbar")
    a.add_argument("--alt-ports", action="store_true",
                   help="alle Ports um 10000 verschieben (2010 -> 12010), "
                        "wenn auf derselben Maschine schon eine OCCU laeuft")
    a.add_argument("--kennung", default="QCCU",
                   help="Kennung dieser Instanz vor der Funkadresse in der Seriennummer "
                        "(Vorgabe QCCU). Zwei Instanzen mit demselben Stick — etwa ein "
                        "Pruefstand neben der Produktion — brauchen verschiedene Kennungen, "
                        "sonst haelt Home Assistant sie fuer dieselbe Zentrale. Vier Zeichen "
                        "empfohlen: HA behaelt die letzten zehn Zeichen der Seriennummer.")
    a.add_argument("--advertise", default=None,
                   help="Adresse, unter der uns die Gegenstelle erreicht "
                        "(geht in die Schnittstellen-Zeile)")
    a.add_argument("--rega-log", default=None,
                   help="jede ReGa-Anfrage vollstaendig in diese Datei schreiben")
    a.add_argument("--device", action="append", default=[], metavar="ADRESSE:TYP[:FUNKADRESSE]",
                   help="Geraet vorab anlegen, z.B. 00AABBCCDDEEFF:490:3a5b01")
    a.add_argument("--sofort-melden", action="store_true",
                   help="frisch angelernte Geraete SOFORT an die Gegenstellen "
                        "melden, statt sie im Posteingang warten zu lassen. "
                        "Fuer Gegenstellen ohne Posteingang (FHEM/HMCCU) — "
                        "dort erscheint das Geraet sonst erst, wenn es in der "
                        "QCCU-Oberflaeche aufgenommen wurde.")
    a.add_argument("--serial", default=None,
                   help="Stick anbinden, z.B. /dev/serial/by-id/usb-busware.de_q-culfw-if00")
    a.add_argument("--own-addr", default=None,
                   help="eigene Funkadresse (6 Hex). Ohne Angabe wird beim "
                        "ersten Start eine gewuerfelt und gemerkt.")
    a.add_argument("--devices", default="qccu_devices.json",
                   metavar="DATEI",
                   help="Speicher der angelernten Geraete (ueberdauert "
                        "Neustarts). Ohne ihn ist die ReGa-Geraeteliste nach "
                        "jedem Start leer.")
    a.add_argument("--state", default="qccu_state.json",
                   help="Datei fuer die Zaehlerstaende (ueberdauert Neustarts)")
    a.add_argument("--json-log", action="store_true",
                   help="auch den Dauerverkehr der Haussteuerung ins "
                        "Protokoll schreiben (Anmeldungen, Sammelabrufe). "
                        "Ohne das stehen dort nur Vorgaenge, die etwas "
                        "bedeuten — Fehler immer.")
    a.add_argument("--raw-log", default=None, metavar="DATEI",
                   help="MESSBETRIEB: jede Stickzeile roh mit Zeitstempel "
                        "mitschreiben, dazu das Sendeurteil des Sticks und die "
                        "Zaehlwerke vor/nach jedem Befehl. Ohne das laesst sich "
                        "ein Aussetzer nicht einordnen.")
    a.add_argument("--no-icmp-answer", action="store_true",
                   help="den Netz-Haushalt (ICMPv6) nicht "
                        "beantworten. Empfangen und vermerkt wird er trotzdem.")
    a.add_argument("--no-answer", action="store_true",
                   help="die Anwendungs-Antwort (ANSWER) nicht "
                        "senden. Nur zum Messen der Wirkung — im Betrieb "
                        "fordert das Geraet sie an.")
    a.add_argument("--answer-delay", type=float, default=0.075, metavar="SEK",
                   help="Wartezeit vor der Anwendungs-Antwort (Vorgabe 0.075).")
    g = a.parse_args()

    t = Tables(g.tables)
    print(f"  Tabellen: {len(t.catalog)} Geraetetypen, "
          f"{len(t.paramsets)} Kanaltypen, {len(t.sdt)} Statusdatentypen")

    lc = QCCU(t)
    # ⚠️ VOR dem Laden des Speichers setzen: der Warteraum wird von dort
    # uebernommen, und wer sofort melden will, soll auch beim ersten Start
    # nichts festhalten.
    lc.zurueckhalten = not g.sofort_melden
    lc.rega_log = g.rega_log
    lc.rpc_port = g.rpc_port
    if g.devices:
        lc.set_store(g.devices)

    # ⚠️ ERST jetzt: die in der Oberflaeche gesetzten Schalter stehen im
    # Speicher und muessen vor der Portwahl bekannt sein. Ein Schalter auf
    # der Kommandozeile hat Vorrang — wer ihn setzt, meint ihn.
    for feld in ("alt_ports", "localhost"):
        if not getattr(g, feld) and lc.einstellungen.get(f"wunsch_{feld}"):
            setattr(g, feld, True)
    for zeile in ports_waehlen(g):
        print(zeile)
    lc.einstellungen.setdefault("wunsch_alt_ports", bool(g.alt_ports))
    lc.einstellungen.setdefault("wunsch_localhost", bool(g.localhost))

    # Die Dienste, die eine Gegenstelle auf DERSELBEN Maschine anspricht
    # (FHEM, Home Assistant), koennen auf 127.0.0.1 bleiben. Die Oberflaeche
    # NICHT — sonst sperrt man sich mit dem Haekchen selbst aus.
    dienst_bind = "127.0.0.1" if g.localhost else g.bind
    lc.kennung = (g.kennung or "QCCU").strip().upper()
    lc.own_host = (g.advertise or dienst_bind if dienst_bind != "0.0.0.0"
                   else (g.advertise or "127.0.0.1"))
    try:
        from qccu_firmware import stick_suchen, stick_serial
        from qccu_radio import zufaellige_adresse
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from qccu_firmware import stick_suchen, stick_serial
        from qccu_radio import zufaellige_adresse

    def stick_merken(pfad):
        """Die Seriennummer des benutzten Sticks festhalten.

        Ab dann kommt nur noch dieser eine in Frage. Wer den Stick tauscht,
        loescht den Eintrag (oder setzt SERIAL) — das ist eine bewusste
        Handlung, und genau so soll es sein.
        """
        sn = stick_serial(pfad)
        if sn and lc.stick_serial != sn:
            lc.stick_serial = sn
            lc.save_store()
            print(f"  Stick gemerkt: {sn}")

    radio = None
    def bind_radio():
        if not g.serial or not os.path.exists(g.serial):
            gefunden, kandidaten = stick_suchen(lc.stick_serial)
            if not gefunden:
                if lc.stick_serial and kandidaten:
                    print(f"  ! Der gemerkte Stick ({lc.stick_serial}) ist nicht "
                          f"angesteckt; {len(kandidaten)} anderer/andere gefunden.")
                return None
            if g.serial != gefunden:
                print(f"  Stick gefunden: {gefunden}")
            g.serial = gefunden
            lc.serial_path = gefunden
        try:
            from qccu_radio import Radio
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from qccu_radio import Radio
        r = Radio(g.serial, lc, t, state_file=g.state, raw_log=g.raw_log,
                  answer=not g.no_answer,
                  answer_delay=g.answer_delay,
                  icmp_answer=not g.no_icmp_answer)
        r.setup(lc.own_addr)
        for addr, rf in lc.rf.items():
            r.bind(rf, addr)
        if not lc.pair_next_addr:
            lc.pair_next_addr = zufaellige_adresse((lc.own_addr,))[:4] + "01"
        r.pair_next_addr = lc.pair_next_addr
        r.start()
        lc.radio = r
        stick_merken(g.serial)
        return r

    def start_cul(r):
        """CUL-Zugang ueber TCP an das Funkobjekt haengen."""
        try:
            from qccu_cul import CulDienst
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from qccu_cul import CulDienst
        cul = CulDienst(r, version=NAME_UND_FASSUNG, bind=dienst_bind,
                        port=g.cul_port, verbose=lc.verbose).start()
        r.cul = cul
        lc.cul = cul
        print(f"  CUL-Zugang auf {dienst_bind}:{g.cul_port}  "
              f"(FHEM: define cul CUL <rechner>:{g.cul_port} 1234)")
        return cul

    def bind_radio_retry(versuche=30, pause=1.0):
        """Anbinden, bis der Stick nach einem Neustart wieder antwortet."""
        letzter = None
        for _ in range(versuche):
            try:
                r = bind_radio()
                if r:
                    alt_cul = getattr(lc, "cul", None)
                    if alt_cul is not None:
                        r.cul = alt_cul
                        alt_cul.radio_wechsel(r)
                    elif g.cul_port:
                        start_cul(r)
                    return r
            except Exception as ex:
                letzter = ex
            time.sleep(pause)
        if letzter is not None:
            print(f"  ! Funk nicht angebunden: {letzter}")
        return None

    if not g.serial or not os.path.exists(g.serial):
        gefunden, kandidaten = stick_suchen(lc.stick_serial)
        if gefunden:
            if g.serial:
                print(f"  {g.serial} gibt es nicht — stattdessen {gefunden}")
            else:
                print(f"  Stick gefunden: {gefunden}")
            g.serial = gefunden
        elif len(kandidaten) > 1:
            print("  Mehrere Sticks gefunden — bitte einen auswaehlen:")
            for k in kandidaten:
                print(f"    {k}")

    # Eigene Funkadresse: ausdruecklich genannt > gemerkt > gewuerfelt.
    # Anlagen aus Fassungen vor der Vergabe liefen alle mit derselben festen
    # Adresse; sind Geraete eingetragen, aber keine Adresse gemerkt, muss es
    # die gewesen sein — sonst waeren die Geraete taub.
    if g.own_addr:
        lc.own_addr = g.own_addr.lower()
    elif not lc.own_addr:
        lc.own_addr = "8e9e5c" if lc.devices else zufaellige_adresse()
        print(f"  Eigene Funkadresse: {lc.own_addr}"
              + ("" if lc.devices else " (neu gewuerfelt)"))
    if lc.store:
        lc.save_store()

    def stick_waechter(pause=8.0):
        """Sucht den Stick weiter, solange keiner angebunden ist.

        ⚠️ Ohne das bleibt QCCU blind, wenn der Stick NACH dem Start dazukommt
        — und genau so laeuft die Erstinbetriebnahme: die Erweiterung wird
        installiert und startet, der Stick wird eingesteckt oder aus dem
        Bootlader geholt, und erst dann gibt es etwas zu finden. Bis hierher
        half nur ein Neustart; gesucht wurde nur beim Start und nach einem
        Einspielen ueber die Oberflaeche.

        Der Waechter fasst nichts an, solange der Funk steht — er sieht nur
        alle paar Sekunden nach, ob ein Geraet aufgetaucht ist. Gesucht wird
        wie sonst auch: am Namen und, wenn einer gemerkt ist, an der
        Seriennummer.

        ⚠️ „Der Funk steht" heisst NICHT „das Objekt ist da". Verschwindet der
        Stick im Betrieb — abgezogen, Wackler, Hub-Reset —, bleibt das
        Funkpfad-Objekt bestehen und QCCU meldet munter „laeuft", waehrend
        nichts mehr ankommt. Deshalb wird zusaetzlich geprueft, ob sein
        Anschluss ueberhaupt noch existiert; ist er weg, wird der Pfad
        verworfen und von vorn gesucht. (Am Aufbau nachgestellt: Stick von der
        virtuellen Maschine getrennt — der Zustand blieb auf „angebunden".)

        ⚠️ Und umgekehrt: der Anschluss kann DA sein und trotzdem tot. Startet
        der Stick neu — Werksreset, Wachhund, Einspielen, ein kurzer Wackler —,
        dann kommt das Geraet binnen Sekunden zurueck, oft unter einer anderen
        Nummer, waehrend der geoeffnete Deskriptor zu einer Verbindung gehoert,
        die es nicht mehr gibt. Der Pfad existiert also, und die Pruefung oben
        griffe nie. Der Funkpfad meldet sich in diesem Fall selbst ab (`tot`),
        und hier wird er wie ein verschwundener behandelt: loesen, suchen,
        einrichten. Gesucht wird ueber die gemerkte Seriennummer, damit die
        neue Nummer des Anschlusses egal ist.
        """
        while True:
            time.sleep(pause)
            r = getattr(lc, "radio", None)
            if r is not None:
                pfad = getattr(r, "port", None) or getattr(lc, "serial_path", None)
                weg  = bool(pfad) and not os.path.exists(pfad)
                tot  = bool(getattr(r, "tot", False))
                if weg or tot:
                    if weg:
                        print(f"  ! Der Stick ist weg ({pfad}) — Funk wird geloest.")
                        lc.merke_ereignis("bad", "Der Stick ist nicht mehr da "
                                                 "— Funk geloest, es wird gesucht")
                    else:
                        print(f"  ! Der Zugang zum Stick ist tot "
                              f"({getattr(r, 'tot_grund', None)}) — Funk wird geloest.")
                    try:
                        r.stop()
                    except Exception:                # noqa: BLE001
                        pass
                    lc.radio = None
                    try:
                        from qccu_web import WebHandler
                        WebHandler.radio = None
                    except Exception:                # noqa: BLE001
                        pass
                    # Den festen Pfad nur dann aufgeben, wenn wir den Stick an
                    # seiner Seriennummer wiederfinden koennen; sonst bliebe
                    # nach einem Neustart gar nichts, woran zu suchen waere.
                    if lc.stick_serial or weg:
                        g.serial = None
                else:
                    continue
            try:
                neu = bind_radio()
            except Exception as ex:                  # noqa: BLE001
                if lc.verbose:
                    print(f"  ! Stick-Suche: {ex}")
                continue
            if not neu:
                continue
            alt_cul = getattr(lc, "cul", None)
            if alt_cul is not None:
                neu.cul = alt_cul
                alt_cul.radio_wechsel(neu)
            elif g.cul_port:
                start_cul(neu)
            lc.radio = neu
            try:
                from qccu_web import WebHandler
                WebHandler.radio = neu
            except Exception:                        # noqa: BLE001
                pass
            print("  Stick aufgetaucht — Funk angebunden.")
            lc.merke_ereignis("ok", "Stick wieder angebunden")

    def stille_waechter(pause=300.0, stille=1800.0):
        """Nachsehen, wenn ein Geraet lange nichts gesagt hat.

        ⚠️ Ohne das faellt Stille erst auf, wenn jemand vergeblich schaltet:
        `unreach` wird gesetzt, wenn ein Befehl dreimal unquittiert bleibt.
        Ein Geraet, das niemand schaltet, gilt bis dahin als in Ordnung — am
        Aufbau gemessen (19.08.2026): eine PS-2 war zwei Stunden aus dem Netz,
        die Oberflaeche meldete unveraendert „erreichbar".

        Gefragt wird sparsam: nur Geraete, die laenger als `stille` Sekunden
        nichts gesagt haben, und pro Durchgang nur EINES — die Sendezeit ist
        ein knappes Gut, und wer wirklich weg ist, ist es auch in fuenf
        Minuten noch. Jede Frage kostet eine Sendung; die Antwort ist die
        Kurzquittung des Geraets.
        """
        while True:
            time.sleep(pause)
            r = getattr(lc, "radio", None)
            if r is None or not hasattr(r, "erreichbarkeit_pruefen"):
                continue
            # Verstummte Anlernrufe wegraeumen. Das geschieht sonst nur, wenn
            # jemand die Liste liest — eine Anlage, die niemand ansieht, haette
            # den Eintrag ewig stehen. Kostet keine Sendung.
            for name, was in (("fremde_aufraeumen", "Anlernwuensche"),
                              ("verwaiste_aufraeumen", "verwaiste Geraete")):
                aufraeumen = getattr(r, name, None)
                if aufraeumen:
                    try:
                        aufraeumen()
                    except Exception as ex:          # noqa: BLE001
                        if lc.verbose:
                            print(f"  ! {was} aufraeumen: {ex}")
            jetzt = time.time()
            faellig = []
            with lc.lock:
                for addr, d in lc.devices.items():
                    zuletzt = getattr(d, "last_seen", None) or lc._wert_zeit.get(addr)
                    rf = lc.rf.get(addr)
                    if not rf:
                        continue
                    # Wer noch nie etwas gesagt hat, wird ebenso gefragt.
                    alter = (jetzt - zuletzt) if zuletzt else stille + 1
                    if alter > stille:
                        faellig.append((alter, addr, rf))
            if not faellig:
                continue
            faellig.sort(reverse=True)          # der Stillste zuerst
            _alter, addr, rf = faellig[0]
            try:
                r.erreichbarkeit_pruefen(rf)
            except Exception as ex:              # noqa: BLE001
                if lc.verbose:
                    print(f"  ! Erreichbarkeitsprobe {addr}: {ex}")

    lc.rebind_radio = bind_radio_retry
    lc.firmware_hex = g.firmware
    lc.serial_path = g.serial
    if g.serial:
        radio = None if not os.path.exists(g.serial) else True

    # ⚠️ Die Funkadresse aus `--device ADDR:TYP:FUNKADRESSE` wird GEMERKT, nicht
    # sofort gebunden: der Funkpfad steht hier noch gar nicht, er wird erst ein
    # paar Zeilen weiter angebunden. Wer sie hier eintrug, sah sie stillschweigend
    # verfallen — das Geraet stand dann ohne Funkadresse da und liess sich weder
    # schalten noch pruefen.
    vorgemerkte_rf = []
    for spec in g.device:
        parts = spec.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            addr, dt = parts[0], int(parts[1])
            lc.add_device(addr, dt, announce=False)
            if len(parts) >= 3 and parts[2]:
                vorgemerkte_rf.append((parts[2], addr))

    if radio:
        radio = bind_radio()
        if radio:
            for rf, addr in vorgemerkte_rf:
                radio.bind(rf, addr)
            print(f"  Funk angebunden ueber {g.serial}")
            if g.cul_port:
                start_cul(radio)
    elif g.serial:
        print(f"  Kein Stick an {g.serial} — die Oberflaeche fuehrt durch das "
              f"Einspielen der Firmware.")

    rpc = SimpleXMLRPCServer((dienst_bind, g.rpc_port), requestHandler=RpcHandler,
                             allow_none=True, logRequests=False)
    rpc.register_instance(lc, allow_dotted_names=False)
    rpc.register_introspection_functions()
    rpc.register_multicall_functions()
    threading.Thread(target=rpc.serve_forever, daemon=True).start()
    print(f"  XML-RPC auf {dienst_bind}:{g.rpc_port}")

    # --- Schnittstelle BidCos-RF (zweiter Dienst, siehe qccu_bidcos_rpc) ---
    # ⚠️ Sie ist eine ZUGABE und darf die Zentrale nicht mitreissen: faellt
    # sie aus, laufen HmIP, ReGa und der CUL-Zugang weiter. Dieselbe Haltung
    # wie beim JSON-RPC-Dienst.
    bidcos = None
    if g.bidcos_port:
        try:
            try:
                import qccu_bidcos_rpc
                from qccu_bidcos import BidcosTables
            except ImportError:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import qccu_bidcos_rpc
                from qccu_bidcos import BidcosTables
            bt = BidcosTables(g.tables)
            fremd = [x.strip().upper() for x in (g.bidcos_fremd or "").split(",")
                     if x.strip()]
            zustand = g.bidcos_state or (
                os.path.join(os.path.dirname(g.state), "qccu_bidcos.json")
                if g.state else None)
            bidcos = qccu_bidcos_rpc.BidcosRpc(
                bt, radio=radio, state_file=zustand, verbose=lc.verbose,
                version=NAME_UND_FASSUNG, fremde_zentralen=fremd,
                senden_erlaubt=bool(g.bidcos_senden))
            # Anlernvorgaenge beider Familien landen in DERSELBEN Liste
            # „Zuletzt geschehen" — der Anwender soll nicht zwei Stellen
            # ansehen muessen, um zu erfahren, was gerade passiert ist.
            bidcos.ereignis = lc.merke_ereignis
            brpc = SimpleXMLRPCServer((dienst_bind, g.bidcos_port),
                                      requestHandler=RpcHandler,
                                      allow_none=True, logRequests=False)
            brpc.register_instance(bidcos, allow_dotted_names=False)
            brpc.register_introspection_functions()
            brpc.register_multicall_functions()
            threading.Thread(target=brpc.serve_forever, daemon=True).start()
            if radio is not None:
                radio.bidcos = bidcos
            print(f"  XML-RPC auf {dienst_bind}:{g.bidcos_port}  "
                  f"(Schnittstelle BidCos-RF, Adresse "
                  f"{bidcos.zentrale.eigene_id}, {len(bidcos.devices)} Geraete)")
            if bt.fehlend:
                print(f"    ! BidCoS-Tabellen fehlen: {', '.join(bt.fehlend)} "
                      f"— `tables/build_bidcos.py` erzeugt sie.")
            if bidcos.zentrale.senden_erlaubt:
                print(f"    Sendet — quittiert Rahmen an {bidcos.zentrale.eigene_id} "
                      f"und lernt an."
                      + (f" Fremde Zentralen: {', '.join(fremd)}" if fremd else ""))
            else:
                print(f"    Sendet NICHT — die Schnittstelle liest nur mit "
                      f"(--bidcos-senden schaltet es frei).")
        except OSError as ex:
            print(f"  ! BidCos-RF auf {dienst_bind}:{g.bidcos_port} nicht moeglich: {ex}")
            print(f"    Alles Uebrige laeuft weiter.")
        except Exception as ex:                      # noqa: BLE001
            print(f"  ! BidCos-RF nicht gestartet: {ex}")

    RegaHandler.qccu = lc
    rega = HTTPServer((dienst_bind, g.rega_port), RegaHandler)
    threading.Thread(target=rega.serve_forever, daemon=True).start()
    print(f"  ReGa    auf {dienst_bind}:{g.rega_port}")

    if g.json_port:
        # ⚠️ Die Auskunft fuer Home Assistant ist eine ZUGABE. Wenn ihr Port
        # belegt ist, darf das nicht die Zentrale mitreissen: FHEM haengt an
        # XML-RPC, ReGa und dem CUL-Zugang, und die laufen laengst. Ein
        # Anwender, der nach einer Aktualisierung zufaellig etwas anderes auf
        # diesem Port hat, wuerde sonst seine ganze Anlage verlieren — wegen
        # eines Dienstes, den er vielleicht gar nicht benutzt.
        try:
            try:
                import qccu_jsonrpc
            except ImportError:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import qccu_jsonrpc
            qccu_jsonrpc.serve(lc, dienst_bind, g.json_port, verbose=lc.verbose,
                               rpc_port=g.rpc_port, hostname=g.advertise or None,
                               laut=bool(getattr(g, "json_log", False)),
                               bidcos=bidcos, bidcos_port=g.bidcos_port)
        except OSError as ex:
            print(f"  ! JSON-RPC auf {dienst_bind}:{g.json_port} nicht moeglich: {ex}")
            print(f"    Home Assistant findet die Zentrale so NICHT. Anderen "
                  f"Port setzen (--json-port) oder abschalten (0).")
            print(f"    Alles Uebrige laeuft weiter.")
        except Exception as ex:                      # noqa: BLE001
            print(f"  ! JSON-RPC nicht gestartet: {ex}")
        else:
            print(f"  JSON-RPC auf {dienst_bind}:{g.json_port}{qccu_jsonrpc.API_PATH}  "
                  f"(Home Assistant, Schnittstelle {lc.interface_name})")

    if g.web_port:
        try:
            from qccu_web import serve as web_serve
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from qccu_web import serve as web_serve
        web_serve(lc, radio, NAME_UND_FASSUNG, g.bind, g.web_port,
                  anbindung={"host": g.advertise or None,
                             "interface": lc.interface_name,
                             "rpc_port": g.rpc_port,
                             "json_port": g.json_port,
                             "bidcos_port": g.bidcos_port,
                             "rega_port": g.rega_port,
                             "cul_port": g.cul_port,
                             "web_port": g.web_port,
                             # Was LAEUFT — und was gewuenscht ist. Weichen
                             # sie ab, fehlt ein Neustart.
                             "alt_ports": bool(g.alt_ports),
                             "localhost": bool(g.localhost),
                             "wunsch_alt_ports": bool(
                                 lc.einstellungen.get("wunsch_alt_ports",
                                                      g.alt_ports)),
                             "wunsch_localhost": bool(
                                 lc.einstellungen.get("wunsch_localhost",
                                                      g.localhost))},
                  bidcos=bidcos)
        host = g.advertise or ("127.0.0.1" if g.bind == "0.0.0.0" else g.bind)
        print(f"  Weboberflaeche auf http://{host}:{g.web_port}/  "
              f"(hier wird angelernt — HMCCU kann das nicht)")
    # Der Waechter gehoert NICHT zur Oberflaeche: dass der Stick spaeter
    # dazukommt oder neu startet, passiert genauso bei einem Betrieb allein
    # ueber XML-RPC (FHEM/HMCCU). Frueher hing er am Web-Zweig — wer die
    # Oberflaeche abschaltete, hatte auch keine Wiederanbindung.
    threading.Thread(target=stick_waechter, daemon=True).start()
    def fenster_waechter(pause=2.0):
        """Meldet, wenn ein Anlernfenster von selbst ablaeuft.

        ⚠️ Oeffnen und Schliessen von Hand melden die jeweiligen Wege selbst;
        das ABLAUFEN meldete niemand. Gerade das ist aber die Erklaerung fuer
        „ich habe gedrueckt und nichts passierte" — beide Familien deshalb
        gleich behandeln.
        """
        # Je Familie das zuletzt gesehene Ende. Damit lassen sich die beiden
        # Faelle trennen: verschwindet das Fenster VOR seinem Ende, hat es
        # jemand von Hand geschlossen — das meldet der jeweilige Weg bereits.
        # Erst wenn das Ende erreicht war, ist es abgelaufen.
        bis = {"HmIP": 0.0, "BidCoS": 0.0}
        while True:
            time.sleep(pause)
            r = getattr(lc, "radio", None)
            jetzt = time.time()
            enden = {
                "HmIP": float(getattr(r, "pair_until", 0.0) or 0.0),
                "BidCoS": (jetzt + bidcos.zentrale.anlernen_offen()
                           if bidcos and bidcos.zentrale.anlernen_offen() > 0
                           else 0.0),
            }
            for name, ende in enden.items():
                if ende > jetzt:
                    bis[name] = ende
                elif bis[name]:
                    abgelaufen = jetzt >= bis[name] - pause
                    bis[name] = 0.0
                    if abgelaufen:
                        lc.merke_ereignis(
                            "warn", f"{name} Anlernfenster abgelaufen — es "
                                    f"wurde nichts angelernt.")

    def bestand_nachmelden():
        """Uebernommenen Rueckrufen den Bestand von sich aus schicken.

        ⚠️ Die Abonnentenliste ueberlebt den Neustart. Danach HAELT QCCU die
        Gegenstelle fuer angemeldet, die Gegenstelle haelt ihre Verbindung
        fuer bestehend — und niemand schickt ein neues `init`. Was in der
        Zwischenzeit angelernt wurde, erfaehrt sie dann nie. Deshalb hier
        einmal von sich aus melden, ohne auf ein `init` zu warten.

        Das ist kein Ersatz fuer `init`: es geht an genau die Rueckrufe, die
        aus dem Speicher kamen. Meldet sich die Gegenstelle regulaer neu, ist
        der Weg der alte.
        """
        for zentrale, name in ((lc, "HmIP"), (bidcos, "BidCoS")):
            if zentrale is None:
                continue
            try:
                with zentrale.lock:
                    bestand = [b for d in zentrale.devices.values()
                               for b in d.descriptions()]
                    abos = list(zentrale.subscribers)
                if not (bestand and abos):
                    continue
                for ident in abos:
                    zentrale._enqueue(("bestand", ident, bestand))
                print(f"  {name}: Bestand ({len(bestand)}) an uebernommene "
                      f"Rueckrufe nachgemeldet: {', '.join(abos)}")
            except Exception as ex:                      # noqa: BLE001
                print(f"  ! {name}: Bestand nicht nachgemeldet: {ex}")

    bestand_nachmelden()
    threading.Thread(target=stille_waechter, daemon=True).start()
    threading.Thread(target=fenster_waechter, daemon=True).start()
    zeitlage_melden()
    print("  bereit — mit Strg-C beenden")

    def beenden(grund):
        # Der ausstehende Wertestand gehoert noch auf die Platte: gesichert
        # wird gedrosselt, also fehlen sonst die letzten Sekunden — und das
        # ist genau der Stand, den ein Neustart sehen soll.
        try:
            lc._werte_sichern(sofort=True)
        except Exception:                            # noqa: BLE001
            pass
        # Dasselbe fuer die BidCoS-Seite: sie sichert ebenfalls gedrosselt,
        # also fehlen ohne diesen Aufruf die letzten Sekunden.
        if bidcos is not None:
            try:
                bidcos._werte_sichern(sofort=True)
            except Exception:                        # noqa: BLE001
                pass
        print(f"\n  beendet ({grund})")

    # Docker/HA halten den Behaelter mit SIGTERM an, nicht mit Strg-C.
    try:
        import signal

        def _term(_sig, _frm):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
    except Exception:                                # noqa: BLE001
        pass

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        beenden("Abbruch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
