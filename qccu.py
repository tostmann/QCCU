#!/usr/bin/env python3
"""QCCU — die Quiche-Zentrale: gibt sich gegenueber Hausautomationen als solche aus."""
import argparse
import collections
import json
import os
import queue
import re
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
VERSION = "2026.8.25"
PRODUKT = "QCCU"
NAME_UND_FASSUNG = f"{PRODUKT} {VERSION}"


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
        # ALTE TABELLEN (bis 2026.8.12) fuehrten je Kanaltyp EINE Liste ueber
        # alle Fassungen — eine Schaltsteckdose bekam damit 1087
        # Konfigurationsparameter angeboten, darunter Farbverlaeufe. Neue
        # Tabellen fuehren `KANALTYP/vN`. Beide werden gelesen; welche
        # vorliegt, entscheidet der Schluessel.
        self.alt = not any("/v" in k for k in self.paramsets)
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

    def channels_of(self, devtype):
        e = self.catalog.get(str(devtype))
        return e["channels"] if e else {}

    def label_of(self, devtype):
        e = self.catalog.get(str(devtype))
        return e["label"] if e else f"Typ {devtype}"

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

    def chinfo_of(self, devtype, kanal):
        """Fassung und geraeteeigene Parameter eines Kanals."""
        e = self.catalog.get(str(devtype)) or {}
        return (e.get("chinfo") or {}).get(str(kanal)) or {}

    def paramset_of(self, devtype, kanal, pset="VALUES"):
        """Das Paramset eines Kanals bei DIESEM Geraetetyp.

        So setzt es auch eine Zentrale von eQ-3 zusammen (an einer HmIP-PS-2
        ueber alle sieben Kanaele und beide Paramsets geprueft, 18.08.2026):
        die Liste der Kanaltyp-Fassung, dazu was die Geraetebeschreibung
        selbst nennt.
        """
        ctype = self.channels_of(devtype).get(str(kanal))
        if not ctype:
            return {}
        info = self.chinfo_of(devtype, kanal)
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

    def __init__(self, address, devtype, tables, firmware="1.0.0"):
        self.address = address.upper()
        self.devtype = int(devtype)
        self.firmware = firmware
        self.tables = tables
        self.values = {}
        self.master = {}
        self.unreach = None
        self.last_seen = 0.0

    @property
    def label(self):
        return self.tables.label_of(self.devtype)

    @property
    def subtype(self):
        """Kurzform des Typs."""
        base = self.label.split()[0]
        parts = base.split("-")[1:]
        return parts[0] if parts else ""

    def channel_list(self):
        return sorted(((int(k), v) for k, v in
                       self.tables.channels_of(self.devtype).items()))

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
        self.rf = {}
        self.pair_next_addr = None
        self.stick_serial = None
        # Vom Anwender vergebene Namen, je Geraet und je Kanal.
        self.names = {}
        # Frisch angelernte Geraete, die in der Haussteuerung noch bestaetigt
        # werden muessen: Adresse -> Zeitpunkt des Anlernens.
        self.frisch_angelernt = {}
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
        if sauber != vorher:
            self.merke_ereignis("info",
                                f"{key} heisst jetzt „{sauber}“" if sauber
                                else f"{key} traegt wieder seinen Vorgabenamen")
        self.save_store()
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

    def add_device(self, address, devtype, firmware="1.0.0", announce=True):
        d = Device(address, devtype, self.t, firmware)
        with self.lock:
            self.devices[d.address] = d
        if self.verbose:
            print(f"  + {d.address} {d.label} ({len(d.channel_list())} Kanaele)")
        if announce and self.subscribers:
            self._notify_new(d)
        if announce:
            self.save_store()
        return d

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
            print(f"  ! Geraetespeicher nicht lesbar: {ex}")
            return 0
        self.pair_next_addr = data.get("pair_next_addr")
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
                                e.get("firmware", "1.0.0"), announce=False)
                if e.get("rf"):
                    self.rf[addr.upper()] = e["rf"].lower()
                gd = self.devices.get(addr.upper())
                for k, v in (e.get("values") or {}).items():
                    kanal, _, name = k.partition(":")
                    if gd is not None and kanal.isdigit() and name:
                        gd.values[(int(kanal), name)] = v
                if e.get("values_zeit"):
                    self._wert_zeit[addr.upper()] = e["values_zeit"]
                n += 1
            except Exception as ex:
                print(f"  ! Geraet {addr} nicht ladbar: {ex}")
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
        with self.lock:
            data = {"devices": {a: {"devtype": d.devtype,
                                    "firmware": d.firmware,
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
        with self._store_lock:
            try:
                tmp = self.store + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.store)
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
        out = []
        with self.lock:
            for d in self.devices.values():
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
        return self.t.paramset_of(d.devtype, ch, paramset)

    def getParamset(self, address, paramset):
        addr = address.upper()
        base, _, ch = addr.partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d or not ch:
            return {}
        pset = str(paramset).upper()
        src = d.master if pset == "MASTER" else d.values
        gesetzt = {p: v for (c, p), v in src.items() if c == int(ch)}
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

    def getValue(self, address, param):
        base, _, ch = address.upper().partition(":")
        with self.lock:
            d = self.devices.get(base)
        if not d or not ch:
            raise xmlrpc.client.Fault(-2, "Unknown parameter")
        v = d.values.get((int(ch), param))
        if v is None:
            raise xmlrpc.client.Fault(-2, "Unknown parameter")
        return v

    def setValue(self, address, param, value, *rest):
        """Schaltbefehl von aussen: eintragen, senden, melden."""
        base, _, ch = address.upper().partition(":")
        if self.verbose:
            print(f"  setValue {address} {param}={value!r}")
        with self.lock:
            d = self.devices.get(base)
            if d and ch:
                d.values[(int(ch), param)] = value
        if self.on_set:
            try:
                acked = self.on_set(base, int(ch) if ch else 0, param, value)
                if acked is not None:
                    self.note_reachable(base, bool(acked))
            except Exception as ex:
                print(f"  ! Sendepfad scheiterte: {ex}")
        self._notify(address.upper(), param, value)
        return ""

    on_set = None

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
            for p, v in values.items():
                self.setValue(addr, p, v)
            return ""

        if ps.startswith("MASTER") or ps.startswith("LINK"):
            print(f"  ! putParamset {addr} {ps}: kein Schreibweg zum Geraet "
                  f"(Konfiguration wird nicht uebertragen)")
            raise xmlrpc.client.Fault(
                -5, f"Writing paramset {ps} to the device is not implemented")
        raise xmlrpc.client.Fault(-5, f"Unknown paramset {ps}")

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

    def getServiceMessages(self, *rest):
        out = []
        with self.lock:
            for a, d in self.devices.items():
                if d.unreach:
                    out.append([f"{a}:0", "UNREACH", True])
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
        """Der Klient beobachtet einen Wert."""
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
                if b.get("lovf"):
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
    a.add_argument("--rega-port", type=int, default=8181)
    a.add_argument("--json-port", type=int, default=8082,
                   help="JSON-RPC-Auskunft fuer Home Assistant (aiohomematic / "
                        "Homematic(IP) Local). 0 = aus. Ergaenzt die "
                        "XML-RPC-Auskunft, ersetzt sie nicht.")
    a.add_argument("--web-port", type=int, default=8080,
                   help="Weboberflaeche (Anlernen, Geraete, Sendezeit). 0 = aus.")
    a.add_argument("--bind", default="0.0.0.0")
    a.add_argument("--advertise", default=None,
                   help="Adresse, unter der uns die Gegenstelle erreicht "
                        "(geht in die Schnittstellen-Zeile)")
    a.add_argument("--rega-log", default=None,
                   help="jede ReGa-Anfrage vollstaendig in diese Datei schreiben")
    a.add_argument("--device", action="append", default=[], metavar="ADRESSE:TYP[:FUNKADRESSE]",
                   help="Geraet vorab anlegen, z.B. 00AABBCCDDEEFF:490:3a5b01")
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
    lc.rega_log = g.rega_log
    lc.rpc_port = g.rpc_port
    lc.own_host = g.advertise or g.bind if g.bind != "0.0.0.0" else (g.advertise or "127.0.0.1")
    if g.devices:
        lc.set_store(g.devices)
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
        cul = CulDienst(r, version=NAME_UND_FASSUNG, bind=g.bind,
                        port=g.cul_port, verbose=lc.verbose).start()
        r.cul = cul
        lc.cul = cul
        print(f"  CUL-Zugang auf {g.bind}:{g.cul_port}  "
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
            lc.add_device(addr, dt)
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

    rpc = SimpleXMLRPCServer((g.bind, g.rpc_port), requestHandler=RpcHandler,
                             allow_none=True, logRequests=False)
    rpc.register_instance(lc, allow_dotted_names=False)
    rpc.register_introspection_functions()
    rpc.register_multicall_functions()
    threading.Thread(target=rpc.serve_forever, daemon=True).start()
    print(f"  XML-RPC auf {g.bind}:{g.rpc_port}")

    RegaHandler.qccu = lc
    rega = HTTPServer((g.bind, g.rega_port), RegaHandler)
    threading.Thread(target=rega.serve_forever, daemon=True).start()
    print(f"  ReGa    auf {g.bind}:{g.rega_port}")

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
            qccu_jsonrpc.serve(lc, g.bind, g.json_port, verbose=lc.verbose,
                               rpc_port=g.rpc_port, hostname=g.advertise or None,
                               laut=bool(getattr(g, "json_log", False)))
        except OSError as ex:
            print(f"  ! JSON-RPC auf {g.bind}:{g.json_port} nicht moeglich: {ex}")
            print(f"    Home Assistant findet die Zentrale so NICHT. Anderen "
                  f"Port setzen (--json-port) oder abschalten (0).")
            print(f"    Alles Uebrige laeuft weiter.")
        except Exception as ex:                      # noqa: BLE001
            print(f"  ! JSON-RPC nicht gestartet: {ex}")
        else:
            print(f"  JSON-RPC auf {g.bind}:{g.json_port}{qccu_jsonrpc.API_PATH}  "
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
                             "json_port": g.json_port})
        host = g.advertise or ("127.0.0.1" if g.bind == "0.0.0.0" else g.bind)
        print(f"  Weboberflaeche auf http://{host}:{g.web_port}/  "
              f"(hier wird angelernt — HMCCU kann das nicht)")
    # Der Waechter gehoert NICHT zur Oberflaeche: dass der Stick spaeter
    # dazukommt oder neu startet, passiert genauso bei einem Betrieb allein
    # ueber XML-RPC (FHEM/HMCCU). Frueher hing er am Web-Zweig — wer die
    # Oberflaeche abschaltete, hatte auch keine Wiederanbindung.
    threading.Thread(target=stick_waechter, daemon=True).start()
    threading.Thread(target=stille_waechter, daemon=True).start()
    print("  bereit — mit Strg-C beenden")

    def beenden(grund):
        # Der ausstehende Wertestand gehoert noch auf die Platte: gesichert
        # wird gedrosselt, also fehlen sonst die letzten Sekunden — und das
        # ist genau der Stand, den ein Neustart sehen soll.
        try:
            lc._werte_sichern(sofort=True)
        except Exception:                            # noqa: BLE001
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
