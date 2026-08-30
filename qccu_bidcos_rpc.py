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
Gesendet wird nur, wenn die Zentrale es erlaubt (`senden_erlaubt`, Vorgabe
AUS). Mit zugem Riegel liest die Schnittstelle den Funk mit und meldet, was
sie sieht, mischt sich aber nicht ein — die richtige Einstellung, solange im
selben Funknetz eine andere Zentrale die Geraete fuehrt.

Offen ist der Riegel, quittiert sie Rahmen an die eigene Adresse und schliesst
Anlernvorgaenge ab: die drei CONFIG-Rahmen hinaus, danach das Geraet ueber die
Tabellen erkennen und in den Bestand nehmen.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import xmlrpc.client

from qccu_bidcos import BidcosTables, BidcosDevice
from qccu_bidcos_mac import (Zentrale, zufaellige_adresse, status_aus, Frame,
                             statusabfrage)

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
                 version="QCCU", eigene_id=None, fremde_zentralen=(),
                 senden_erlaubt=False):
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
        self._anlern_offen = {}
        self._offene_sends = {}
        self._sende_wache = None
        self._werte_zuletzt = 0.0
        self._werte_offen = False
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
        # Wer gerade angelernt werden WILL — eine Momentaufnahme, absichtlich
        # nur im Arbeitsspeicher. Wer verstummt, faellt heraus.
        self._wuensche = {}
        # Adresse -> (Zeitpunkt, Pegel) des zuletzt gehoerten Rahmens.
        self._gehoert = {}
        # Haken in „Zuletzt geschehen". Wird von aussen gesetzt; ohne ihn
        # bleibt es beim Protokoll.
        self.ereignis = None
        self.zentrale = Zentrale(eigen, senden_erlaubt=bool(senden_erlaubt),
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
            g = BidcosDevice(adr, eintrag, self.t, firmware=e.get("firmware"),
                             kanalzahlen=e.get("kanalzahlen"))
            for feld, ziel in (("values", g.values), ("master", g.master)):
                for schluessel, wert in (e.get(feld) or {}).items():
                    kanal, _, par = schluessel.partition(":")
                    try:
                        ziel[(int(kanal), par)] = wert
                    except ValueError:
                        continue
            self.devices[adr.upper()] = g

    def _sichern(self):
        if not self.state_file:
            return
        with self.lock:
            d = {
                "eigene_id": self.zentrale.eigene_id,
                "subscribers": dict(self.subscribers),
                # Die zuletzt gemeldeten Werte gehoeren mit auf die Platte.
                # Sie sind nicht die Wahrheit — die steht im Geraet —, aber sie
                # sind der letzte bekannte Stand. Ohne sie beantwortet
                # `getValue` nach einem Neustart JEDE Frage mit „Unknown
                # parameter", bis das Geraet von sich aus meldet; bei einem
                # netzbetriebenen Aktor, der nur auf Aenderung sendet, kann das
                # beliebig lange dauern. Die Gegenstelle zeigt dann eine
                # Entitaet ohne Wert. Dieselbe Ueberlegung wie auf der
                # HmIP-Seite (`save_store`).
                "devices": {a: {"type": g.devtype,
                                "firmware": g.firmware_byte,
                                "kanalzahlen": g.kanalzahlen,
                                "values": {f"{c}:{par}": v
                                           for (c, par), v in g.values.items()},
                                "master": {f"{c}:{par}": v
                                           for (c, par), v in g.master.items()}}
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

    # ⚠️ Ein Schaltbefehl geht EINMAL hinaus und kann verlorengehen — auf
    # BidCoS ist das der Normalfall, nicht die Ausnahme. Ohne Wiederholung
    # wirkt er meistens, und wenn nicht, merkt es niemand.
    #
    # Die Geduld ist gemessen, nicht geraten: im Mitschnitt vom 20.08.2026
    # antwortete ein Aktor auf den Schaltbefehl nach **125 ms** und auf eine
    # Statusabfrage nach **149 ms**. 400 ms sind also rund das Dreifache der
    # beobachteten Umlaufzeit — knapp genug, um eine Wiederholung noch
    # sinnvoll zu machen, weit genug, um nicht in eine laufende Antwort
    # hineinzufunken.
    SEND_GEDULD = 0.4
    SEND_VERSUCHE = 3

    # ⚠️ Gedrosselt: ein Geraet, das oft meldet, wuerde sonst bei jeder
    # Meldung die Zustandsdatei neu schreiben — auf einem Behaelter-Datentraeger
    # ist das unnoetige Schreiblast. Beim Herunterfahren wird `sofort`
    # gesichert, damit die letzten Sekunden nicht fehlen.
    WERTE_ABSTAND = 20.0

    def _werte_sichern(self, sofort=False):
        """Die Werte sichern — hoechstens alle paar Sekunden."""
        jetzt = time.time()
        if not sofort and jetzt - self._werte_zuletzt < self.WERTE_ABSTAND:
            self._werte_offen = True
            return
        self._werte_zuletzt = jetzt
        self._werte_offen = False
        self._sichern()

    WUNSCH_GEDULD = 300.0

    # Wie lange auf die Antwort auf eine Statusabfrage gewartet wird. Am
    # Aufbau gemessen (20.08.2026): Abfrage -> Antwort in 149 ms.
    PING_WARTEN = 1.5

    def erreichbar(self, adresse):
        """Aktiv nachsehen, ob ein Geraet noch antwortet.

        Gesendet wird eine Statusabfrage; als Antwort zaehlt jeder Rahmen,
        der DANACH von dieser Adresse eintrifft. Rueckgabe: True/False, oder
        None, wenn die Adresse gar nicht im Bestand steht.
        """
        adresse = (adresse or "").upper()
        with self.lock:
            if adresse not in self.devices:
                return None
        vorher = self.zuletzt_gehoert(adresse) or 0.0
        # ⚠️ `statusabfrage` liefert einen Frame, `_senden` erwartet die
        # fertige `As`-Zeile. Ein Frame direkt hineingereicht laesst den
        # Schreibversuch scheitern — und QCCU wertet einen gescheiterten
        # Schreibversuch als toten Zugang und loest den Funk.
        befehl = statusabfrage(self.zentrale.eigene_id, adresse,
                               msgcnt=self.zentrale.naechster_zaehler()
                               ).as_befehl()
        if not self._senden(befehl):
            return False
        ende = time.time() + self.PING_WARTEN
        while time.time() < ende:
            if (self.zuletzt_gehoert(adresse) or 0.0) > vorher:
                return True
            time.sleep(0.02)
        return False

    def zuletzt_gehoert(self, adresse):
        """Wann zuletzt etwas von diesem Geraet kam (Zeitstempel oder None)."""
        with self.lock:
            e = self._gehoert.get((adresse or "").upper())
        return e[0] if e else None

    def pegel(self, adresse):
        """Empfangspegel des zuletzt gehoerten Rahmens (dBm oder None)."""
        with self.lock:
            e = self._gehoert.get((adresse or "").upper())
        return e[1] if e and isinstance(e[1], (int, float)) else None


    def _melde(self, art, text):
        """Einen Vorgang in „Zuletzt geschehen" eintragen, wenn moeglich."""
        haken = self.ereignis
        if haken is not None:
            try:
                haken(art, text)
            except Exception:
                pass

    def _wunsch_merken(self, v, lage):
        adresse = v["adresse"]
        with self.lock:
            e = self._wuensche.setdefault(adresse, {"anzahl": 0})
            e.update({"zuletzt": time.time(), "modell": v.get("modell"),
                      "klasse": v.get("klasse"), "firmware": v.get("firmware"),
                      "lage": lage})
            e["anzahl"] += 1
            neu_zu_melden = e.get("gemeldet") != lage
            e["gemeldet"] = lage
        if neu_zu_melden:
            self._melde("ok" if lage == "wird angelernt" else "warn",
                        f"BidCoS {adresse} will angelernt werden — {lage}")

    def anlernwuensche_liste(self):
        """Wer gerade angelernt werden WILL — der Ruf, nicht der Bestand.

        Gleiche Feldnamen wie im HmIP-Zweig, damit die Oberflaeche EINE
        gemischte Liste zeichnen kann; `interface` sagt, aus welcher Familie
        der Ruf kam.

        ⚠️ Anders als bei HmIP ist hier KEIN Schluessel noetig: ein
        BidCoS-Geraet laesst sich anlernen, sobald das Fenster offen ist.
        Der Hinweis muss das sagen, sonst sucht der Anwender einen Schluessel,
        den es nicht gibt.
        """
        jetzt = time.time()
        with self.lock:
            for a in [a for a, e in self._wuensche.items()
                      if jetzt - e.get("zuletzt", 0) > self.WUNSCH_GEDULD]:
                self._wuensche.pop(a, None)
            eintraege = sorted(self._wuensche.items(),
                               key=lambda kv: -kv[1].get("zuletzt", 0))
            bestand = set(self.devices)
        offen = self.zentrale.anlernen_offen()
        ziel = self.zentrale.anlern_ziel
        out = []
        for adresse, e in eintraege:
            if adresse in bestand:
                continue          # schon angelernt — kein offener Wunsch mehr
            typ = None
            if self.t is not None and e.get("modell") is not None:
                treffer = self.t.erkennen(modell=e["modell"],
                                          firmware=e.get("firmware"),
                                          klasse=e.get("klasse"), bits=None)
                typ = (treffer or {}).get("type")
            if not self.zentrale.senden_erlaubt:
                hinweis = "Schnittstelle sendet nicht."
            elif ziel and ziel != adresse:
                hinweis = f"Anlernfenster gilt nur {ziel}."
            elif offen:
                hinweis = "wird gerade angelernt."
            else:
                hinweis = "Anlernfenster ist zu — oben oeffnen."
            out.append({
                "id": adresse, "address": adresse,
                "name": f"{typ or 'Unbekanntes Geraet'} {adresse} — {hinweis}",
                "interface": self.interface_name,
                "hmid": adresse, "label": typ,
                "devtype": e.get("modell"), "hinweis": hinweis,
                "vor_sek": max(0.0, jetzt - e.get("zuletzt", jetzt)),
                "anzahl": e.get("anzahl", 1),
            })
        return out

    def zustand(self):
        """Was die Oberflaeche ueber diese Schnittstelle wissen muss."""
        return {
            "interface": self.interface_name,
            "eigene_id": self.zentrale.eigene_id,
            "geraete": len(self.devices),
            "abonnenten": len(self.subscribers),
            "senden_erlaubt": self.zentrale.senden_erlaubt,
            "anlernen_offen": self.zentrale.anlernen_offen(),
            "anlern_ziel": self.zentrale.anlern_ziel,
            "tabellen": self.t.zustand() if self.t else None,
        }

    # -- Funkverkehr hereinreichen ----------------------------------------

    def a_zeile(self, zeile):
        """Eine `A`-Zeile des Sticks verarbeiten. Rueckgabe: die Vorgaenge.

        ⚠️ Der CUL-Zugang (Port 2000) bekommt dieselbe Zeile weiterhin — hier
        wird nur MITGELESEN. Solange `senden_erlaubt` zu ist, mischt sich
        diese Schnittstelle nicht in ein Netz ein, das FHEM fuehrt.
        """
        self._anlern_aufraeumen()
        # Quittungen auf unsere Anlern-Rahmen abfangen, bevor sie als
        # gewoehnlicher Verkehr durchlaufen.
        try:
            f = Frame.von_a_zeile(zeile)
        except Exception:                             # noqa: BLE001
            f = None
        if (f is not None and f.mtype == 0x02
                and f.dst.upper() == self.zentrale.eigene_id):
            if f.src in self._anlern_offen:
                self._anlern_quittung(f)
            # Subtyp 0x01 = Quittung MIT Zustand; nur die beweist, dass der
            # Schaltbefehl angekommen UND ausgefuehrt ist.
            if f.subtyp == 0x01:
                self._send_quittiert(f)
        # Jeder gehoerte Rahmen ist ein Lebenszeichen — auch eine blosse
        # Quittung. Das ist die einzige Stelle, an der ALLE vorbeikommen.
        if f is not None:
            with self.lock:
                self._gehoert[f.src.upper()] = (time.time(), f.rssi)
        vorgaenge = self.zentrale.verarbeite(zeile)
        for v in vorgaenge:
            if v["art"] == "status":
                g = self.devices.get(v["geraet"])
                if g is None:
                    continue
                kanal, an = v["kanal"], v["an"]
                g.values[(kanal, "STATE")] = an
                self._notify(f"{v['geraet']}:{kanal}", "STATE", an)
                self._werte_sichern()
            elif v["art"] == "quittung_faellig" and v["gesendet"]:
                self._senden(v["befehl"])
            elif v["art"] == "anlernen_faellig" and v["gesendet"]:
                self._anlernen_abschliessen(v)
            elif v["art"] == "anlernruf":
                # ⚠️ IMMER melden, auch bei geschlossenem Fenster. Sonst ist
                # genau der Fall unsichtbar, den man beim Anlernen wissen
                # muss: hat das Geraet ueberhaupt gerufen?
                z = self.zentrale.anlern_ziel
                lage = ("Fenster ZU" if not self.zentrale.anlernen_offen()
                        else (f"Fenster gilt nur {z}" if z and z != v["adresse"]
                              else "wird angelernt"))
                print(f"  BidCoS Anlernruf von {v['adresse']} "
                      f"(Modell 0x{(v.get('modell') or 0):04X}, "
                      f"Klasse 0x{(v.get('klasse') or 0):02X}) — {lage}")
                self._wunsch_merken(v, lage)
            elif v["art"] == "anlernruf_fremd":
                print(f"  BidCoS: {v['geraet']} will angelernt werden — "
                      f"NICHT beantwortet, das Fenster gilt nur {v['ziel']}.")
        return vorgaenge

    def _senden(self, befehl):
        """Eine fertige `As`-Zeile in die Warteschlange des Funkpfads."""
        if self.radio is None:
            if self.verbose:
                print(f"  BidCoS: kein Funkpfad, nicht gesendet: {befehl}")
            return False
        try:
            self.radio._submit(befehl, "ask")
            return True
        except Exception as ex:                       # noqa: BLE001
            print(f"  ! BidCoS senden scheiterte: {ex}")
            return False

    def _anlernen_abschliessen(self, vorgang):
        """Die Anlernsequenz beginnen — EINEN Rahmen nach dem anderen.

        ⚠️ Die drei Rahmen duerfen NICHT am Stueck hinausgehen. Funk ist
        halbduplex: das Geraet quittiert den ersten Rahmen, und wer den
        zweiten sofort hinterherschickt, sendet WAEHREND das Geraet sendet —
        es kann uns dann gar nicht hoeren.

        Gemessen an einem gelungenen Anlernvorgang (20.08.2026): das Geraet
        quittiert nach rund 120 ms, und die funktionierende Zentrale liess
        zwischen den Rahmen **rund 480 ms** verstreichen — sie wartete auf
        jede Quittung, bevor sie weitermachte. Genau das wird hier
        nachgebaut: senden, auf die Quittung warten, dann der naechste.
        """
        adresse = vorgang["geraet"]
        with self.lock:
            if adresse in self._anlern_offen:
                return
            self._anlern_offen[adresse] = {
                "rahmen": list(vorgang["befehle"]), "schritt": 0,
                "versuche": 0, "vorgang": vorgang, "seit": time.time()}
        self._wache_starten()
        print(f"  BidCoS Anlernen {adresse}: beginne "
              f"(Modell 0x{(vorgang.get('modell') or 0):04X}, "
              f"Firmware 0x{(vorgang.get('firmware') or 0):02X}, "
              f"Klasse 0x{(vorgang.get('klasse') or 0):02X})")
        self._anlern_schritt(adresse)

    # Wie oft ein einzelner Anlern-Rahmen wiederholt wird, bevor aufgegeben
    # wird. Dieselbe Ueberlegung wie beim Schaltbefehl.
    ANLERN_VERSUCHE = 3

    def _anlern_schritt(self, adresse):
        """Den naechsten noch offenen Rahmen senden."""
        with self.lock:
            lauf = self._anlern_offen.get(adresse)
            if lauf is None:
                return
            i = lauf["schritt"]
            if i >= len(lauf["rahmen"]):
                vorgang = lauf["vorgang"]
                self._anlern_offen.pop(adresse, None)
                fertig = True
            else:
                befehl = lauf["rahmen"][i]
                lauf["versuche"] += 1
                lauf["faellig"] = time.time() + self.SEND_GEDULD
                try:
                    lauf["msgcnt"] = int(befehl[4:6], 16)
                except ValueError:
                    lauf["msgcnt"] = None
                fertig = False
        if fertig:
            print(f"  BidCoS {adresse}: alle drei Rahmen quittiert")
            self._anlernen_bestaetigen(vorgang)
            return
        print(f"    BidCoS -> {adresse}: {befehl}")
        self._senden(befehl)

    def _anlern_quittung(self, frame):
        """Eine Quittung des Geraets — der naechste Rahmen darf hinaus."""
        adresse = frame.src
        with self.lock:
            lauf = self._anlern_offen.get(adresse)
            if lauf is None or lauf.get("msgcnt") not in (None, frame.msgcnt):
                return
            lauf["schritt"] += 1
            lauf["versuche"] = 0
        self._anlern_schritt(adresse)

    def _anlern_aufraeumen(self):
        """Unquittierte Anlern-Rahmen wiederholen, sonst laut aufgeben."""
        jetzt = time.time()
        nochmal, tot = [], {}
        with self.lock:
            for a, lauf in list(self._anlern_offen.items()):
                if jetzt < lauf.get("faellig", 0):
                    continue
                if lauf["versuche"] >= self.ANLERN_VERSUCHE:
                    tot[a] = self._anlern_offen.pop(a)
                else:
                    nochmal.append(a)
        for a in nochmal:
            print(f"  BidCoS Anlernen {a}: Rahmen unbestaetigt, noch ein Versuch")
            self._anlern_schritt(a)
        for a, lauf in tot.items():
            v = (lauf or {}).get("vorgang") or {}
            modell = v.get("modell")
            typ = None
            if self.t is not None and modell is not None:
                e = self.t.erkennen(modell=modell, firmware=v.get("firmware"),
                                    klasse=v.get("klasse"), bits=v.get("bits"))
                typ = e.get("type") if e else None
            print(f"  ! BidCoS Anlernen {a} GESCHEITERT bei Rahmen "
                  f"{lauf.get('schritt', 0) + 1} von 3 — das Geraet hat nicht "
                  f"quittiert. Es steht NICHT im Bestand. "
                  f"(Modell 0x{(modell or 0):04X}"
                  + (f" = {typ}" if typ else " — in den Tabellen NICHT gefuehrt")
                  + f", Klasse 0x{(v.get('klasse') or 0):02X})")
            self._melde("bad", f"BidCoS {a}: Anlernen gescheitert bei Rahmen "
                               f"{lauf.get('schritt', 0) + 1} von 3 — das "
                               f"Geraet hat nicht quittiert.")

    def _anlernen_bestaetigen(self, vorgang):
        """Den bestaetigenden Schaltbefehl senden und das Geraet aufnehmen.

        Der Aktor antwortet darauf mit seinem Zustand (`0x02`/Subtyp `0x01`);
        diese Antwort laeuft ohnehin durch `a_zeile` und setzt den Wert. Wir
        tragen das Geraet hier ein, weil es die drei Rahmen quittiert HAT —
        der SET ist die Probe aufs Exempel, nicht die Bedingung.
        """
        adresse = vorgang["geraet"]
        self._anlernen_eintragen(vorgang)
        if adresse not in self.devices:
            return
        probe = self.zentrale.schalten(adresse, 1, False)
        if probe["gesendet"]:
            self._senden(probe["befehl"])
            print(f"    BidCoS {adresse}: bestaetigender Schaltbefehl gesendet")


    def _anlernen_eintragen(self, vorgang):
        """Das quittierte Geraet ueber die Tabellen erkennen und aufnehmen."""
        adresse = vorgang["geraet"]
        eintrag = self.t.erkennen(modell=vorgang.get("modell"),
                                  firmware=vorgang.get("firmware"),
                                  klasse=vorgang.get("klasse"),
                                  bits=vorgang.get("bits")) if self.t else None
        if eintrag is None:
            # Geschrieben ist geschrieben: das Geraet hoert jetzt auf uns, wir
            # koennen es nur nicht fuehren. Das gehoert gesagt, nicht verschwiegen.
            print(f"  ! BidCoS {adresse}: angelernt, aber Typ unbekannt "
                  f"(Modell 0x{(vorgang.get('modell') or 0):04X}, "
                  f"Klasse 0x{(vorgang.get('klasse') or 0):02X}) — "
                  f"es erscheint nicht im Bestand.")
            return
        geraet = BidcosDevice(adresse, eintrag, self.t,
                              firmware=vorgang.get("firmware"))
        # Die Kanalzahl steht im Anlernruf, nicht in der Tabelle: der Aufbau
        # sagt nur, WO sie steht (`count_from_sysinfo`).
        gesetzt = geraet.kanalzahlen_aus_sysinfo(vorgang.get("sysinfo"))
        if gesetzt:
            print(f"    Kanalzahl aus dem Anlernruf: "
                  + ", ".join(f"Kanal {k} x{v}" for k, v in sorted(gesetzt.items())))
        with self.lock:
            vorher = self.devices.get(adresse)
            alt_kanaele = len(vorher.channel_list()) if vorher else 0
            self.devices[adresse] = geraet
        self._sichern()
        print(f"  BidCoS angelernt: {adresse} = {geraet.devtype}")
        # ⚠️ Wird ein BEKANNTES Geraet neu angelernt und hat sich dabei sein
        # Aufbau geaendert (die Kanalzahl steht erst im Anlernruf), ist ein
        # blosses `newDevices` wirkungslos: die Gegenstelle kennt die Adresse
        # und nimmt ihre zwischengespeicherte Beschreibung. Erst abmelden,
        # dann neu melden — sonst bleibt sie auf dem alten Stand.
        if vorher is not None and alt_kanaele != len(geraet.channel_list()):
            print(f"    Aufbau geaendert ({alt_kanaele} -> "
                  f"{len(geraet.channel_list())} Kanaele) — erst abmelden, "
                  f"dann neu melden.")
            self._enqueue(("delete", None, [adresse]))
        self._melde("ok", f"BidCoS {adresse} angelernt = {geraet.devtype}")
        with self.lock:
            self._wuensche.pop(adresse, None)
        self._enqueue(("new", adresse, geraet.descriptions()))
        offen = geraet.dynamisch()
        if offen:
            print(f"    Hinweis: die Kanalzahl von {adresse} nennt erst das "
                  f"Geraet ({', '.join(n for n, _ in offen)}) — bis dahin ein Kanal.")

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
        if not vorgang["gesendet"]:
            if self.verbose:
                print(f"  BidCoS setValue {addr}={an} — NICHT gesendet "
                      f"(Riegel zu): {vorgang['befehl']}")
            return ""
        if not self._senden(vorgang["befehl"]):
            return ""
        # ⚠️ Der Beweis ist die Quittung MIT Zustand (`0x02`/Subtyp `0x01`),
        # nicht die blosse Empfangsquittung — so fuehrt es auch die
        # Geraete-Grundwahrheit. Bleibt sie aus, wird wiederholt.
        try:
            zaehler = int(vorgang["befehl"][4:6], 16)
        except ValueError:
            return ""
        with self.lock:
            self._offene_sends[(base, int(kanal))] = {
                "befehl": vorgang["befehl"], "msgcnt": zaehler, "an": an,
                "versuche": 1, "faellig": time.time() + self.SEND_GEDULD}
        self._wache_starten()
        return ""

    # -- Wiederholung unbestaetigter Schaltbefehle -------------------------

    def _wache_starten(self):
        if self._sende_wache is not None and self._sende_wache.is_alive():
            return
        self._sende_wache = threading.Thread(target=self._wache, name="bidcos-tx",
                                             daemon=True)
        self._sende_wache.start()

    def _wache(self):
        """Wiederholt, was keine Quittung bekam — Schaltbefehle wie Anlernrahmen.

        ⚠️ Sie muss ein eigener Faden sein. Frueher haing die Wiederholung am
        Eintreffen der naechsten Funkzeile; bleibt das Geraet aber stumm — und
        genau dann braucht man sie —, kommt keine Zeile, und nichts loest aus.
        """
        while True:
            time.sleep(self.SEND_GEDULD / 4)
            self._anlern_aufraeumen()
            jetzt = time.time()
            faellig = []
            with self.lock:
                if not self._offene_sends and not self._anlern_offen:
                    return
                for schluessel, e in list(self._offene_sends.items()):
                    if jetzt < e["faellig"]:
                        continue
                    if e["versuche"] >= self.SEND_VERSUCHE:
                        self._offene_sends.pop(schluessel, None)
                        faellig.append((schluessel, e, False))
                    else:
                        e["versuche"] += 1
                        e["faellig"] = jetzt + self.SEND_GEDULD
                        faellig.append((schluessel, e, True))
            for (geraet, kanal), e, nochmal in faellig:
                if nochmal:
                    if self.verbose:
                        print(f"  BidCoS {geraet}:{kanal} unbestaetigt — "
                              f"Versuch {e['versuche']}/{self.SEND_VERSUCHE}")
                    self._senden(e["befehl"])
                else:
                    # ⚠️ Laut sagen. Ein Schaltbefehl, der nach drei Versuchen
                    # keine Quittung hat, ist NICHT angekommen — wer das
                    # verschweigt, laesst die Gegenstelle einen Zustand
                    # anzeigen, den das Geraet nicht hat.
                    print(f"  ! BidCoS {geraet}:{kanal} = {e['an']} blieb nach "
                          f"{self.SEND_VERSUCHE} Versuchen unbestaetigt.")

    def _send_quittiert(self, frame):
        """Eine Quittung MIT Zustand raeumt den offenen Schaltbefehl ab."""
        if len(frame.payload) < 3:
            return
        kanal = frame.payload[1] & 0x3F
        with self.lock:
            e = self._offene_sends.pop((frame.src, kanal), None)
        if e is not None and self.verbose:
            print(f"  BidCoS {frame.src}:{kanal} bestaetigt "
                  f"(Versuch {e['versuche']})")

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
            self.zentrale.anlernen_oeffnen(int(seconds), ziel=ziel)
            self.install_until = time.time() + int(seconds)
            self._melde("ok", f"BidCoS Anlernfenster offen ({int(seconds)} s"
                              + (f", nur {str(ziel).upper()}" if ziel
                                 else ", jedes Geraet") + ")")
            if ziel:
                print(f"  BidCoS Anlernfenster offen fuer {seconds}s "
                      f"— NUR fuer {str(ziel).upper()}")
            else:
                # ⚠️ Das gehoert gesagt, nicht kleingedruckt: ein Anlernruf ist
                # ein Rundruf. Wer jetzt IRGENDWO in Funkreichweite seinen
                # Anlernknopf drueckt, bekommt unsere Konfigurationsrahmen —
                # auch der Nachbar.
                print(f"  BidCoS Anlernfenster offen fuer {seconds}s — fuer JEDES "
                      f"Geraet, das jetzt anlernen will (auch fremde!). "
                      f"Mit Geraeteadresse aufrufen, um es einzugrenzen.")
        else:
            # Nur melden, wenn wirklich eines offen war — die Gegenstelle
            # schickt `setInstallMode(False)` auch routinemaessig.
            war_offen = self.zentrale.anlernen_offen() > 0
            self.zentrale.anlernen_schliessen()
            self.install_until = 0.0
            print("  BidCoS Anlernfenster zu")
            if war_offen:
                self._melde("ok", "BidCoS Anlernfenster geschlossen")
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
