#!/usr/bin/env python3
"""Die XML-RPC-Schnittstelle `BidCos-RF` — der zweite Dienst neben `HmIP-RF`.

WOFUER DAS DA IST
-----------------
`qccu.py` fuehrt die HmIP-Geraete und bietet sie auf Port 2010 als
Schnittstelle `HmIP-RF` an. Die klassischen BidCoS-Geraete brauchen daneben
eine EIGENE Schnittstelle — so macht es eine Zentrale von eQ-3 auch, und
`aiohomematic` erwartet es genau so:

  Port 2010  HmIP-RF     die HmIP-Geraete      (qccu.py, unveraendert)
  Port 2001  BidCos-RF   die BidCoS-Geraete    (dieses Modul)

⚠️ **Getrennt, nicht gemeinsam.** Beide Dienste fuehren ihren eigenen
Gerätebestand und ihre eigenen Rueckruf-Abonnenten. Der Grund steht im
Vertrag der Gegenstelle: sie meldet sich je Schnittstelle mit einer eigenen
Kennung an (`<Zentrale>-HmIP-RF` und `<Zentrale>-BidCos-RF`), schickt aber
BEIDE an dieselbe Rueckruf-Adresse. Wer die Abonnenten zusammenwirft, liefert
BidCoS-Ereignisse an den HmIP-Client — und der verwirft sie.

WAS `aiohomematic` HIER WIRKLICH RUFT
-------------------------------------
Aus der Quelle von `aiohomematic` 2026.5.0 gelesen, nicht geraten:

* `system.listMethods` — **bindend**. Was hier fehlt, wird nie gefragt; was
  drinsteht, muss gehen. Die Bibliothek prueft VOR dem Aufruf.
* `getVersion` (nur wenn gemeldet), `init(url, kennung)` / `init(url)`,
  `ping(aufrufer)`, `listDevices`, `getDeviceDescription`,
  `getParamsetDescription`, `getParamset`, `getValue`, `setValue`,
  `putParamset`, `getInstallMode`, `setInstallMode`.
* ⚠️ `setInstallMode` kommt hier ueber **XML-RPC** (bei HmIP ueber JSON-RPC),
  und das dritte Argument ist POSITIONSUEBERLADEN: mit Zielgeraet ist es die
  Geraeteadresse, ohne ist es die Zahl `mode`.
* ⚠️ Die Versionszeichenkette darf **nicht** `Homegear` oder `pydevccu`
  enthalten — sonst waehlt die Bibliothek ein anderes Backend und der Client
  verliert Anlernen, Verknuepfen und Ping-Pong.
* `setValue`/`putParamset` koennen ein VIERTES Argument tragen (`rx_mode`).

WAS ES (NOCH) NICHT TUT
-----------------------
Gesendet wird nur, wenn die Zentrale es erlaubt (`Zentrale.senden_erlaubt`).
In der Vorgabe steht der Riegel zu: der Weg vom Anlernruf zur Antwort ist
noch nicht an Hardware belegt. Alles Uebrige — Bestand, Beschreibungen,
Paramsets, Rueckrufe — arbeitet.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import xmlrpc.client

from qccu_bidcos import BidcosTables, BidcosDevice
from qccu_bidcos_mac import Zentrale, zufaellige_adresse, status_aus, Frame

INTERFACE_NAME = "BidCos-RF"
DEFAULT_PORT = 2001

# Was gemeldet, aber leer beantwortet wird — dieselbe Haltung wie im
# HmIP-Zweig: die Gegenstelle soll die Methode nicht vermissen.
LEER_BEANTWORTET = (
    "getLinks", "getLinkPeers", "getLinkInfo", "addLink", "removeLink",
    "setLinkInfo", "getSuppressedServiceMessages", "reportValueUsage",
    "rssiInfo", "activateLinkParamset", "abortDeleteDevice", "addDevice",
    "changeKey", "clearConfigCache", "determineParameter",
    "getKeyMismatchDevice", "getMetadata", "installFirmware",
    "listReplaceableDevices", "listTeams", "logLevel", "replaceDevice",
    "restoreConfigToDevice", "searchDevices", "setBidcosInterface",
    "setInterfaceClock", "setMetadata", "setTeam", "setTempKey",
    "suppressServiceMessages", "updateFirmware",
)


class BidcosRpc:
    """Bestand, Beschreibungen und Rueckrufe der Schnittstelle `BidCos-RF`."""

    NOTIFY_MAX_FAILS = 5
    NOTIFY_TIMEOUT = 8.0

    def __init__(self, tables, radio=None, state_file=None, verbose=True,
                 version="QCCU", eigene_id=None, fremde_zentralen=()):
        self.t = tables
        self.radio = radio
        self.state_file = state_file
        self.verbose = verbose
        # ⚠️ NICHT „QCCU Homegear" o.ae. — die Zeichenkette entscheidet ueber
        # die Backend-Wahl der Gegenstelle (s. Kopf).
        self.version = version
        self.interface_name = INTERFACE_NAME
        self.devices = {}          # Adresse -> BidcosDevice
        self.subscribers = {}      # Kennung -> Rueckruf-Adresse
        self._notify_fails = {}
        self.lock = threading.Lock()
        self._notify_q = queue.Queue()
        self._notify_thread = None
        self.install_until = 0.0
        self._laden()
        eigen = (eigene_id or self._gemerkte_id
                 or zufaellige_adresse(gesperrt=fremde_zentralen))
        # ⚠️ Ein gemerkter Wert kann mit einer INZWISCHEN bekannten fremden
        # Zentrale kollidieren (jemand traegt `--bidcos-fremd` nach). Das darf
        # den Dienst nicht am Start hindern: die Kollision wird gemeldet und
        # eine neue Adresse gewuerfelt. Abbrechen waere die schlechtere Wahl —
        # dann faellt die ganze Schnittstelle aus, wegen einer Angabe, die
        # sich beheben laesst.
        if eigen.upper() in {f.upper() for f in fremde_zentralen}:
            print(f"  ! BidCoS: die gemerkte Adresse {eigen} gehoert einer "
                  f"fremden Zentrale — es wird eine neue gewuerfelt.")
            eigen = zufaellige_adresse(gesperrt=fremde_zentralen)
        self.zentrale = Zentrale(eigen, senden_erlaubt=False,
                                 fremde_zentralen=fremde_zentralen)
        self._sichern()

    # -- Zustand -----------------------------------------------------------

    def _laden(self):
        self._gemerkte_id = None
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                d = json.load(f)
        except Exception as ex:                       # noqa: BLE001
            print(f"  ! BidCoS-Zustand nicht lesbar: {ex}")
            return
        self._gemerkte_id = d.get("eigene_id")
        self.subscribers = dict(d.get("subscribers") or {})
        for adr, e in (d.get("devices") or {}).items():
            eintrag = self.t.typ_suchen(e.get("type", "")) if self.t else None
            if not eintrag:
                print(f"  ! BidCoS {adr}: Typ '{e.get('type')}' nicht in den "
                      f"Tabellen — uebersprungen")
                continue
            self.devices[adr.upper()] = BidcosDevice(
                adr, eintrag, self.t, firmware=e.get("firmware"),
                kanalzahlen=e.get("kanalzahlen"))

    def _sichern(self):
        if not self.state_file:
            return
        with self.lock:
            d = {
                "eigene_id": self.zentrale.eigene_id,
                "subscribers": dict(self.subscribers),
                "devices": {a: {"type": g.devtype,
                                "firmware": g.firmware_byte,
                                "kanalzahlen": g.kanalzahlen}
                            for a, g in self.devices.items()},
            }
        roh = json.dumps(d, indent=1).encode()
        # Atomar: der Zustand liegt bei einem Behaelterbetrieb auf einem
        # Datentraeger, den auch andere lesen.
        tmp = self.state_file + ".tmp"
        f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(f, roh)
        os.fsync(f)
        os.close(f)
        os.replace(tmp, self.state_file)

    def zustand(self):
        """Was die Oberflaeche ueber diese Schnittstelle wissen muss."""
        return {
            "interface": self.interface_name,
            "eigene_id": self.zentrale.eigene_id,
            "geraete": len(self.devices),
            "abonnenten": len(self.subscribers),
            "senden_erlaubt": self.zentrale.senden_erlaubt,
            "anlernen_offen": self.zentrale.anlernen_offen(),
            "tabellen": self.t.zustand() if self.t else None,
        }

    # -- Funkverkehr hereinreichen ----------------------------------------

    def a_zeile(self, zeile):
        """Eine `A`-Zeile des Sticks verarbeiten. Rueckgabe: die Vorgaenge.

        ⚠️ Der CUL-Zugang (Port 2000) bekommt dieselbe Zeile weiterhin — hier
        wird nur MITGELESEN. Solange `senden_erlaubt` zu ist, mischt sich
        diese Schnittstelle nicht in ein Netz ein, das FHEM fuehrt.
        """
        vorgaenge = self.zentrale.verarbeite(zeile)
        for v in vorgaenge:
            if v["art"] != "status":
                continue
            g = self.devices.get(v["geraet"])
            if g is None:
                continue
            kanal, an = v["kanal"], v["an"]
            g.values[(kanal, "STATE")] = an
            self._notify(f"{v['geraet']}:{kanal}", "STATE", an)
        return vorgaenge

    # -- Rueckrufe ---------------------------------------------------------

    def _notify(self, address, param, value):
        self._enqueue(("event", address, param, value))

    def _enqueue(self, job):
        if not self.subscribers:
            return
        if self._notify_thread is None or not self._notify_thread.is_alive():
            self._notify_thread = threading.Thread(
                target=self._notify_worker, name="bidcos-rueckrufe", daemon=True)
            self._notify_thread.start()
        self._notify_q.put(job)

    def _notify_worker(self):
        while True:
            job = self._notify_q.get()
            try:
                self._deliver(job)
            except Exception as ex:                   # noqa: BLE001
                print(f"  ! BidCoS-Rueckruf-Faden: {ex}")
            finally:
                self._notify_q.task_done()

    def _proxy(self, url):
        return xmlrpc.client.ServerProxy(url, allow_none=True)

    def _deliver(self, job):
        kind = job[0]
        for ident, url in list(self.subscribers.items()):
            try:
                p = self._proxy(url)
                if kind == "event":
                    p.event(ident, job[1], job[2], job[3])
                elif kind == "bestand":
                    if ident != job[1]:
                        continue
                    p.newDevices(ident, job[2])
                elif kind == "new":
                    p.newDevices(ident, job[2])
                elif kind == "delete":
                    p.deleteDevices(ident, job[2])
                self._notify_fails.pop(ident, None)
            except Exception as ex:                   # noqa: BLE001
                n = self._notify_fails.get(ident, 0) + 1
                self._notify_fails[ident] = n
                if self.verbose:
                    print(f"  ! BidCoS-Rueckruf an {url} scheiterte ({n}): {ex}")
                if n >= self.NOTIFY_MAX_FAILS:
                    self.subscribers.pop(ident, None)
                    self._notify_fails.pop(ident, None)
                    self._sichern()
                    print(f"  BidCoS-Rueckruf {ident} nach {n} Fehlschlaegen "
                          f"verworfen")

    # -- die XML-RPC-Flaeche ----------------------------------------------

    def init(self, url, interface_id=""):
        """Anmeldung eines Rueckrufs. Leere Kennung = Abmeldung.

        Wie im HmIP-Zweig wird der Bestand danach VON SICH AUS gemeldet — eine
        Gegenstelle, die den Rueckruf-Weg geht, fragt die Geraete nicht ab.
        """
        if interface_id:
            self.subscribers[interface_id] = url
            self._notify_fails.pop(interface_id, None)
            self._sichern()
            if self.verbose:
                print(f"  BidCoS init: {interface_id} -> {url}")
            with self.lock:
                bestand = [b for g in self.devices.values()
                           for b in g.descriptions()]
            self._enqueue(("bestand", interface_id, bestand))
        else:
            weg = [k for k, v in self.subscribers.items() if v == url]
            for k in weg:
                self.subscribers.pop(k, None)
            self._sichern()
            if self.verbose and weg:
                print(f"  BidCoS init: abgemeldet {weg}")
        return ""

    def ping(self, caller=""):
        """Lebendtest. Die Antwort ist ein Rueckruf, kein Rueckgabewert.

        ⚠️ Der Aufrufer muss WORTGETREU zurueck: die Gegenstelle zerlegt ihn
        am `#` und verwirft das PONG, wenn der Teil davor nicht ihrer eigenen
        Kennung gleicht.
        """
        self._notify("CENTRAL", "PONG", caller or self.interface_name)
        return True

    def clientServerInitialized(self, interface_id=""):
        return 1 if interface_id in self.subscribers else 0

    def getVersion(self):
        return self.version

    def listDevices(self, interface_id=None):
        with self.lock:
            out = [b for g in self.devices.values() for b in g.descriptions()]
        if self.verbose:
            print(f"  BidCoS listDevices -> {len(out)} Eintraege")
        return out

    def getDeviceDescription(self, address):
        addr = str(address).upper()
        base = addr.split(":")[0]
        with self.lock:
            g = self.devices.get(base)
        if not g:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        for b in g.descriptions():
            if b["ADDRESS"] == addr:
                return b
        raise xmlrpc.client.Fault(-2, "Unknown channel")

    def getParamsetDescription(self, address, paramset):
        addr = str(address).upper()
        base, _, kanal = addr.partition(":")
        with self.lock:
            g = self.devices.get(base)
        if not g:
            raise xmlrpc.client.Fault(-2, "Unknown device")
        return g.paramset_of(kanal if kanal else None, paramset)

    def getParamsetId(self, address, paramset):
        addr = str(address).upper()
        base, _, kanal = addr.partition(":")
        with self.lock:
            g = self.devices.get(base)
        if not g:
            return ""
        return f"{g.devtype}:{kanal or 'dev'}:{str(paramset).upper()}"

    def getParamset(self, address, paramset):
        addr = str(address).upper()
        base, _, kanal = addr.partition(":")
        with self.lock:
            g = self.devices.get(base)
        if not g or not kanal:
            return {}
        pset = str(paramset).upper()
        quelle = g.master if pset == "MASTER" else g.values
        return {p: v for (c, p), v in quelle.items() if c == int(kanal)}

    def getValue(self, address, param):
        addr = str(address).upper()
        base, _, kanal = addr.partition(":")
        with self.lock:
            g = self.devices.get(base)
        if not g or not kanal:
            raise xmlrpc.client.Fault(-2, "Unknown parameter")
        v = g.values.get((int(kanal), str(param)))
        if v is None:
            raise xmlrpc.client.Fault(-2, "Unknown parameter")
        return v

    def setValue(self, address, param, value, *rest):
        """Schalten. `rest` faengt das vierte Argument (`rx_mode`) ab."""
        addr = str(address).upper()
        base, _, kanal = addr.partition(":")
        with self.lock:
            g = self.devices.get(base)
        if not g or not kanal:
            raise xmlrpc.client.Fault(-2, "Unknown parameter")
        if str(param).upper() != "STATE":
            # Nur der Schaltzustand ist an Hardware belegt. Was wir nicht
            # koennen, wird gemeldet statt still verschluckt.
            print(f"  BidCoS setValue: {param} an {addr} nicht umgesetzt")
            return ""
        an = value in (True, 1, "1", "true", "True")
        vorgang = self.zentrale.schalten(base, int(kanal), an)
        if vorgang["gesendet"] and self.radio is not None:
            self.radio._submit(vorgang["befehl"], "ask")
        elif self.verbose:
            print(f"  BidCoS setValue {addr}={an} — NICHT gesendet "
                  f"(Riegel zu): {vorgang['befehl']}")
        return ""

    def putParamset(self, address, paramset, values, *rest):
        print(f"  BidCoS putParamset {address} {paramset} nicht umgesetzt")
        return ""

    def setInstallMode(self, on, seconds=60, mode=1, address=None):
        """Anlernfenster.

        ⚠️ Das dritte Argument ist positionsueberladen: die Gegenstelle
        schickt dort die GERAETEADRESSE, wenn sie ein bestimmtes Geraet meint,
        sonst die Zahl `mode`. Wer das verwechselt, legt eine Adresse in
        `mode` ab (der HmIP-Zweig tut das heute).
        """
        ziel = address
        if isinstance(mode, str) and not str(mode).isdigit():
            ziel, mode = mode, 1
        if on:
            self.zentrale.anlernen_oeffnen(int(seconds))
            self.install_until = time.time() + int(seconds)
            print(f"  BidCoS Anlernfenster offen fuer {seconds}s"
                  + (f" (nur {ziel})" if ziel else ""))
        else:
            self.zentrale.anlernen_schliessen()
            self.install_until = 0.0
            print("  BidCoS Anlernfenster zu")
        return ""

    def setInstallModeWithWhitelist(self, on, seconds=60, whitelist=None):
        return self.setInstallMode(on, seconds)

    def getInstallMode(self):
        return self.zentrale.anlernen_offen()

    def deleteDevice(self, address, flags=0):
        return self.deleteDevices(None, [address])

    def deleteDevices(self, interface_id=None, addresses=()):
        weg = []
        with self.lock:
            for a in addresses or []:
                a = str(a).upper().split(":")[0]
                if self.devices.pop(a, None) is not None:
                    weg.append(a)
        if weg:
            self._sichern()
            self._enqueue(("delete", None, weg))
        return ""

    def getServiceMessages(self):
        """UNREACH je Geraet — dieselbe Form wie im HmIP-Zweig."""
        out = []
        with self.lock:
            for a, g in self.devices.items():
                if g.unreach:
                    out.append([f"{a}:0", "UNREACH", True])
        return out

    def listBidcosInterfaces(self):
        zustand = {}
        try:
            zustand = self.radio.radio_state() if self.radio else {}
        except Exception:                             # noqa: BLE001
            zustand = {}
        return [{
            "ADDRESS": self.zentrale.eigene_id,
            "DESCRIPTION": f"{self.interface_name} {self.zentrale.eigene_id}",
            "CONNECTED": bool(self.radio),
            "DEFAULT": True,
            "FIRMWARE_VERSION": self.version,
            "TYPE": "QCCU",
            "DUTY_CYCLE": int(zustand.get("duty", 0) or 0),
        }]

    def _listMethods(self):
        return sorted({
            "init", "ping", "clientServerInitialized", "getVersion",
            "listDevices", "getDeviceDescription", "getParamsetDescription",
            "getParamsetId", "getParamset", "getValue", "setValue",
            "putParamset", "setInstallMode", "setInstallModeWithWhitelist",
            "getInstallMode", "deleteDevice", "deleteDevices",
            "getServiceMessages", "listBidcosInterfaces",
            "system.listMethods", "system.methodHelp",
            "system.methodSignature", "system.multicall",
        } | set(LEER_BEANTWORTET))

    def _dispatch(self, method, params):
        if method == "listMethods" or method in self._listMethods():
            m = getattr(self, method, None)
            if callable(m) and not method.startswith("_"):
                return m(*params)
        if method in LEER_BEANTWORTET:
            return ""
        print(f"  ? BidCoS {method}({', '.join(repr(p) for p in params)[:80]}) "
              f"— unbekannt, leer beantwortet")
        return ""
