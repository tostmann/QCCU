#!/usr/bin/env python3
"""Funkanbindung der QCCU: verbindet den Quiche-Stick mit der Zentralen-Nachbildung."""
import json
import os
import queue
import re
import secrets
import sys
import threading
import time

import serial

try:
    from sticker_decode import sticker_to_local_key
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sticker_decode import sticker_to_local_key

DEC = re.compile(r"^PM([0-9A-Fa-f]+)\s+src=([0-9A-Fa-f]{6})\s+dst=([0-9A-Fa-f]{6})"
                 r"\s+sec=(\d+)\s+ct=(\d+)")
# ct=4 mit ack=: `ar=1` quittiert eine Quittung, `ar=3` einen inhaltlichen
# Frame — beides bestaetigt, dass unser Frame beim Geraet war.
ACK = re.compile(r"\bct=4\b.*\back=")
ACKSEQ = re.compile(r"\back=([0-9A-Fa-f]{8})")
# Kurzquittung (q-culfw >= 2.0.26): 6-Byte-Frame des Peers auf unseren letzten
# Frame — DAS ist die MAC-Quittung, die die echte Zentrale und die Geraete
# austauschen (Luftmitschnitt 18.08.2026); ct=4 folgt nur auf die CONFIRMATION.
KURZQUITTUNG = re.compile(r"^PK from=([0-9A-Fa-f]{6}) for=([0-9A-Fa-f]{6})")
FLAGS = re.compile(r"\bf=([0-9A-Fa-f]{2})\b")
SEQ = re.compile(r"\bsn=([0-9A-Fa-f]{8})")
# k6tx/k6rx (Kurzquittungen gesendet/empfangen) gibt es seit q-culfw 2.0.26;
# aeltere Sticks lassen die beiden Felder weg — beides muss passen.
CNT = re.compile(r"^Pm\s+rx=(\d+)\s+ok=(\d+)\s+mic=(\d+)\s+dup=(\d+)"
                 r"\s+acks=(\d+)(?:\s+k6tx=(\d+)\s+k6rx=(\d+))?"
                 r"\s+fwd=(\d+)\s+tx=(\d+)\s+txerr=(\d+)")
RAW = re.compile(r"^P([0-9A-Fa-f]{4,})$")
STICKSEQ = re.compile(r"^Pm (?:ein|aus) .*\bsn=([0-9A-Fa-f]{8})")
BUDGET = re.compile(r"^Pm budget=(\d) credit=(\d+)/(\d+) lovf=(\d+)")
TX_OK = re.compile(r"^Pm tx ok")
TX_NO = re.compile(r"^(?:Pm (?:ERR|NUR-LESEN)|\?\s*$)")

CNT_KEYS = ("rx", "ok", "mic", "dup", "acks", "k6tx", "k6rx", "fwd", "tx", "txerr")

FT_ANSWER = 2
FT_STATUS = 5
# ApplicationFrameType.TIME_INFO — Wert aus den Enums des HMIPServer-Jars.
# Ein frisch angelerntes Geraet fragt die Zeit an und verlangt Antwort; die
# CCU schickt ihr einen TIME_INFO-Frame zurueck, KEIN ANSWER.
FT_TIME_INFO = 35

# NetworkManagementFrameType — Werte aus denselben Jar-Enums. Der Ausschluss
# eines Geraets laeuft in drei Schritten, alle ohne Nutzlast.
NM_EXCLUDE_REQUEST = 0xF0
NM_EXCLUDE_READY = 0xF1
NM_EXCLUDE_CONCLUDE = 0xF2
SDT_BINARY = 8

# Statusdatentyp -> (Parameter, Umrechnung, Einheit fuer die Anzeige).
#
# Jede Zeile ist BELEGT, nicht geraten — die Grenzen des Parameters in den
# Gerätebeschreibungen geben die Umrechnung selbst her:
#
#   0  TEMPERATURE       2 Byte  -> ACTUAL_TEMPERATURE: FLOAT, MIN -3276.8,
#                                  MAX 3276.7 = int16 durch 10 (vorzeichenbehaftet)
#   6  ERROR_CODE        1 Byte  -> ERROR_CODE: INTEGER 0..255, unverändert
#   22 FLAG_REGISTER_24  3 Byte  -> WEEK_PROGRAM_CHANNEL_LOCKS: INTEGER
#                                  0..16777215 = 2^24-1, also genau die 3 Byte
#
# Gemeldet wird nur, wo der Kanaltyp den Parameter auch führt (_kanal_fuehrt);
# alles Übrige bleibt RAW_SDT<n> — ungedeutet ist besser als falsch gedeutet.
SDT_DEUTUNG = {
    0:  ("ACTUAL_TEMPERATURE",
         lambda v: int.from_bytes(v, "big", signed=True) / 10.0, " °C"),
    6:  ("ERROR_CODE", lambda v: v[0], ""),
    22: ("WEEK_PROGRAM_CHANNEL_LOCKS", lambda v: int.from_bytes(v, "big"), ""),
}
APP_RESP_REQ = 0x80
RXF_FOR_US = 0x01

CT_NETWORK_MGMT = 1
CT_ICMP = 2
CT_MAC_CONTROL = 4
ICMP_ECHO_REPLY = 0x80
ICMP_ECHO_REQUEST = 0x81
ICMP_ROUTER_SOLICITATION = 0x85
ICMP_ROUTER_ADVERTISEMENT = 0x86
ICMP_NEIGHBOR_SOLICITATION = 0x87
ICMP_NEIGHBOR_ADVERTISEMENT = 0x88
ICMP_NAME = {
    0x01: "DESTINATION_UNREACHABLE", 0x02: "PACKET_TOO_BIG",
    0x03: "TIME_EXCEEDED", 0x04: "PARAMETER_PROBLEM",
    ICMP_ECHO_REPLY: "ECHO_REPLY", ICMP_ECHO_REQUEST: "ECHO_REQUEST",
    ICMP_ROUTER_SOLICITATION: "ROUTER_SOLICITATION",
    ICMP_ROUTER_ADVERTISEMENT: "ROUTER_ADVERTISEMENT",
    ICMP_NEIGHBOR_SOLICITATION: "NEIGHBOR_SOLICITATION",
    ICMP_NEIGHBOR_ADVERTISEMENT: "NEIGHBOR_ADVERTISEMENT",
    0x89: "REDIRECT_MESSAGE",
}
ADDR_ALL_DEVICES = "f00001"
ADDR_ALL_ROUTERS = "f00002"
ADDR_ANY_ROUTER = "e00002"
GROUP_ADDR = {
    ADDR_ALL_DEVICES: "ALL_DEVICES", ADDR_ALL_ROUTERS: "ALL_ROUTERS",
    "f00003": "ALL_ACCESS_CONTROLLERS", "f00004": "ALL_THERMOSTATS",
    "f00005": "ALL_SMOKE_SENSORS", "f00081": "ALL_WIRED_DEVICES",
    ADDR_ANY_ROUTER: "ANY_ROUTER",
}
MAC_ROLE_CENTRAL = 3


class _Job:
    """Ein Schreibauftrag auf der seriellen Leitung."""
    __slots__ = ("cmd", "kind", "verdict", "written", "done", "not_before",
                 "expect", "reply")

    def __init__(self, cmd, kind, not_before=0.0, expect=None):
        self.cmd = cmd
        self.kind = kind
        self.verdict = None
        self.not_before = not_before
        self.expect = expect
        self.reply = None
        self.written = threading.Event()
        self.done = threading.Event()

    @property
    def expects_verdict(self):
        return self.kind in ("cmd", "answer")


def _nur_zeichen(text):
    """Alles wegwerfen, was kein Buchstabe und keine Ziffer ist.

    ⚠️ Der Aufkleber wird abgetippt oder aus einer Nachricht kopiert — mit
    Bindestrichen, Leerzeichen, manchmal beidem. Frueher fielen nur die
    Bindestriche weg: wer ihn mit Leerzeichen eingab, bekam „hat 30 statt 26
    Zeichen" und suchte den Fehler bei sich (Anwendermeldung 19.08.2026). Was
    zaehlt, sind die 26 Zeichen — wie sie gruppiert sind, geht uns nichts an.
    """
    return "".join(c for c in (text or "") if c.isalnum())


def zufaellige_adresse(gesperrt=()):
    """Eine Funkadresse wuerfeln, die weder Rundruf noch Sammeladresse ist
    und keiner der uebergebenen gleicht (Vergleich auf die oberen 16 Bit)."""
    while True:
        b = secrets.token_bytes(3)
        if b[0] in (0x00, 0xE0, 0xF0, 0xFF):
            continue
        adr = b.hex()
        if any(adr[:4] == g[:4] for g in gesperrt if g):
            continue
        return adr


class Radio:
    SEQ_STEP = 64
    MAC_SEQ_LEAP = 256

    def __init__(self, port, qccu, tables, baud=38400, verbose=True,
                 state_file=None, raw_log=None, answer=True,
                 answer_delay=0.075, icmp_answer=True):
        self.qccu = qccu
        self.t = tables
        self.verbose = verbose
        self.icmp_answer = icmp_answer
        self.icmp_seen = {}
        self.mac_seq = 0
        self.answer_enabled = answer
        self.answer_delay = answer_delay
        # Den Anschluss merken: der Waechter prueft daran, ob der Stick noch
        # da ist (ein verschwundenes Geraet laesst das Objekt bestehen).
        self.port = port
        self.ser = serial.serial_for_url(port, baud, timeout=0.3, exclusive=True)
        # Ist der Zugang unbrauchbar geworden? Das Objekt bleibt danach
        # bestehen (andere Faeden halten es noch), taugt aber zu nichts mehr;
        # der Waechter in qccu.py sieht hier nach und bindet neu an.
        self.tot = False
        self.tot_grund = None
        self._lesefehler = 0
        self.by_hmid = {}
        self.appseq = {}
        self.devseq = {}
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = False
        self.own_addr = None
        self.netzschluessel_fehlt = False
        self._acked = {}
        # Funkadresse -> zuletzt gemeldeter Empfangspegel (dBm).
        self._rssi = {}
        # Funkadresse -> Ereignis: das Geraet hat den Ausschluss angenommen.
        self._exclude_ready = {}

        self.vlen = {}
        for name, e in (self.t.sdt or {}).items():
            if "type" in e and "len" in e and e["type"] >= 0 and e["len"] > 0:
                self.vlen[e["type"]] = e["len"]
        self._sdt_unbekannt = set()

        self.tx_timeout = 0.4
        self.tx_tries = 3
        self.verdict_timeout = 1.2
        self.ask_timeout = 1.5

        self.pair_until = 0.0
        self.pair_key = None
        self.pair_key_src = ""
        self._pair_busy = False
        self._pair_expect = None
        self._pair_ok = threading.Event()
        self.pair_next_addr = None
        self.pair_last = ""
        # Geraete, die angelernt werden WOLLEN — der Posteingang.
        # Funkadresse -> {"sgtin", "devtype", "zuerst", "zuletzt", "anzahl"}
        self._fremde = {}
        # Geraete, die mit unserem Netzschluessel funken, aber nicht (mehr)
        # im Bestand stehen — siehe `_pruefe_verwaist`.
        self._verwaist = {}
        # Kennung -> zuletzt bekannte Funkadresse eines ausgeschlossenen
        # Geraets, damit ein Wiederanlernen die alte Spur mitnimmt.
        self._ehemals = {}
        self._pair_q = queue.Queue()
        self._cmdq = queue.Queue()
        self.cul = None

        self._txq = queue.Queue()
        self._ansq = queue.Queue()
        self._ans_hold = None
        self._pending = None
        self._pending_lock = threading.Lock()
        self._gate = threading.Event()
        self._gate.set()

        self._raw = None
        self._log_lock = threading.Lock()
        self.measure = bool(raw_log)
        if raw_log:
            self._raw = open(raw_log, "a", buffering=1)
            self._log("##", f"--- Start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        self.counters = None
        self.budget = None
        self._cnt_ev = threading.Event()

        self.state_file = state_file
        self._load_state()

        qccu.on_set = self.on_set
        qccu.on_install = self.on_install

    def _log(self, direction, text):
        """Eine Zeile in den Rohmitschnitt."""
        if not self._raw:
            return
        t = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)
        with self._log_lock:
            self._raw.write(f"{ts} {direction} {text}\n")

    def _load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
        except Exception as ex:
            defekt = self.state_file + ".defekt"
            try:
                os.replace(self.state_file, defekt)
            except Exception:
                defekt = "(nicht verschiebbar)"
            print(f"  ! ZAEHLERDATEI UNLESBAR ({ex}) — beiseite gelegt als {defekt}. "
                  f"Zaehler starten neu; der Stick haelt seinen eigenen Stand.")
            return
        if "appseq" in saved or "mac_seq" in saved:
            per_dev = saved.get("appseq", {})
            self.mac_seq = int(saved.get("mac_seq", 0))
        else:
            per_dev = saved
        for hmid, seq in per_dev.items():
            self.appseq[hmid] = (int(seq) + self.SEQ_STEP) & 0xFF
        if self.verbose and per_dev:
            print(f"  Zaehlerstaende geladen: "
                  + ", ".join(f"{h}={self.appseq[h]:#04x}" for h in per_dev)
                  + (f", Stick sn=0x{self.mac_seq:08X}" if self.mac_seq else ""))

    def _save_state(self):
        if not self.state_file:
            return
        with self.lock:
            daten = {"appseq": dict(self.appseq), "mac_seq": self.mac_seq}
        with self._state_lock:
            try:
                tmp = self.state_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(daten, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.state_file)
            except Exception as ex:
                print(f"  ! ZAEHLERSTAND NICHT GESICHERT: {ex}")

    def bind(self, hmid, ccu_address):
        """Funkadresse und Zentralen-Adresse einander zuordnen.

        Ein Geraet hat genau EINE Funkadresse. Wird dieselbe Kennung neu
        angelernt (Werksreset, dann wieder Taste), bekommt sie eine neue
        Adresse — die alte Zuordnung muss weg, sonst gehen Befehle weiter an
        die tote Adresse (18.08.2026: ecd412 statt ecd413, „keine Quittung
        nach 3 Versuchen", obwohl das Geraet gerade sauber angelernt war).
        """
        with self.lock:
            ccu = ccu_address.upper()
            for alt in [h for h, a in self.by_hmid.items()
                        if a == ccu and h != hmid.lower()]:
                del self.by_hmid[alt]
                if self.verbose:
                    print(f"  Funk {alt} <-> {ccu} ersetzt")
            self.by_hmid[hmid.lower()] = ccu
        if hasattr(self.qccu, "note_rf"):
            self.qccu.note_rf(ccu_address, hmid)
        if self.verbose:
            print(f"  Funk {hmid.lower()} <-> {ccu_address.upper()}")

    def setup(self, own_addr):
        """Stick als Zentrale einrichten."""
        self.own_addr = own_addr.lower()
        # ⚠️ Es gibt KEINEN Weg mehr, dem Stick einen fertigen
        # Netzwerkschluessel von aussen zu geben (`mK<hex>` ist ab q-culfw
        # 2.0.16 verschwunden, QCCU kennt den Schalter gar nicht mehr). Ein
        # Stick, der jeden mitgebrachten Schluessel annimmt, waere ein
        # Werkzeug zum Mitlesen fremder Anlagen.
        #
        # Verloren geht dadurch nichts. Der Schluessel kommt auf genau zwei
        # Wegen herein, beide ohne Tastatur:
        #   * die Zentrale erzeugt ihn selbst (`mC` -> nwk_ensure), oder
        #   * sie bekommt ihn beim Anlernen zugeteilt — gegen SGTIN und
        #     Aufkleberschluessel verpackt, over-air. So uebernimmt man auch
        #     eine laufende Anlage: der Stick lernt sich als Geraet an ihrer
        #     alten Zentrale an, deren Schluessel wandert dabei mit.
        folge = ["mL0", "mC", f"mA{own_addr}", "mQ1", "mE1", "Pr"]
        for cmd in folge:
            self.ser.write(cmd.encode() + b"\r\n")
            self.ser.flush()
            self._log(">>", cmd)
            time.sleep(0.25)

        self._netzschluessel_sicherstellen()
        self._seq_angleichen()

        self.ser.reset_input_buffer()
        if self.verbose:
            print(f"  Stick eingerichtet (Adresse {own_addr})")

    def _stick_zustand(self):
        """Die Zustandszeile des Sticks holen (`m` -> `Pm ein …`).

        ⚠️ Der Stick antwortet auf `m` mit DREI Zeilen: Zustand, Sendekonto,
        Zaehlwerk. Wer nach der ersten aufhoert, laesst zwei liegen — und die
        naechste Frage geht unter, weil der Stick noch sendet. Genau daran
        scheiterte auf einem FABRIKNEUEN Stick die Frage nach der Kennung:
        sie blieb „unklar", also wurde keine erzeugt, also legte der Stick
        keinen Netzwerkschluessel an, also schlug spaeter jedes Anlernen fehl
        — und nirgends stand ein Fehler; die Einrichtung meldete „Stick
        eingerichtet". An echter Hardware gefunden (18.08.2026, q-culfw
        2.0.29, parallele mCCU-Session).

        Deshalb wird die Antwort zu Ende gelesen, bevor es weitergeht.
        """
        self.ser.reset_input_buffer()
        self.ser.write(b"m\r\n")
        self.ser.flush()
        gefunden = None
        ende = time.time() + 1.5
        while time.time() < ende:
            try:
                z = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:
                return gefunden
            if not z:
                if gefunden is not None:
                    break            # Ruhe auf der Leitung: die Antwort ist durch
                continue
            if z.startswith("Pm ein") or z.startswith("Pm aus"):
                gefunden = z
        return gefunden

    def _kennung_vorhanden(self):
        """Hat der Stick schon eine Kennung? (`mG` zeigt sie.)

        Ein fabrikneuer Stick hat noch keine — und OHNE Kennung legt er auch
        keinen Netzwerkschluessel ab (`nwk_ensure` steigt bei `!id_valid`
        aus). Genau daran scheiterte das Anlernen auf einem neuen Stick,
        ohne dass es jemandem auffiel: `mC` antwortet auch dann freundlich
        mit `Pm rolle=Zentrale`, nur eben ohne `Netzwerkschluessel erzeugt`.

        Rueckgabe True/False, oder None wenn die Antwort nicht zu deuten ist
        — dann wird NICHT gewuerfelt.
        """
        # ⚠️ Zweimal fragen, und vorher aufraeumen. Die Frage folgt direkt auf
        # die Zustandsabfrage, deren Antwort mehrzeilig ist; deren Reste laufen
        # dieser hier in die Quere. Bleibt es bei „unklar", wird KEINE Kennung
        # erzeugt (so soll es sein) — und der Stick bekommt in der Folge auch
        # keinen Netzwerkschluessel. Einzeln gefragt antwortet er zuverlaessig,
        # im Ablauf nicht (am Geraet beobachtet, 18.08.2026).
        for _ in range(2):
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"mG\r\n")
                self.ser.flush()
            except Exception:                            # noqa: BLE001
                return None
            ende = time.time() + 2.0
            while time.time() < ende:
                try:
                    z = self.ser.readline().decode("ascii", "replace").strip()
                except Exception:                        # noqa: BLE001
                    return None
                if "keine Kennung" in z:
                    return False
                if z.startswith("Pm sgtin="):
                    return True
                # Alles andere sind Reste der vorigen Frage — weiterlesen.
            time.sleep(0.3)
        return None

    def _netzschluessel_sicherstellen(self):
        """Ohne Netzwerkschluessel im Stick laesst sich kein Geraet anlernen.

        Ein fabrikneuer Stick hat noch keinen. Aeltere Firmware (bis 2.0.10)
        meldete `key=0` auch dann, wenn einer im EEPROM lag — sie holte ihn
        beim Start nicht zurueck. In beiden Faellen erzeugt die Rolle Zentrale
        allein keinen: die Ablage gilt als belegt, solange ihr Merker steht.
        Erst das Verwerfen raeumt das Feld.

        Erzeugt wird nur, solange NICHTS zu verlieren ist. Sind Geraete
        eingetragen, waere ein neuer Schluessel das Ende jeder Verbindung zu
        ihnen — und zwar lautlos: sie wuerden weiter senden, nur verstuende sie
        niemand mehr. Diese Entscheidung gehoert dem Betreiber, nicht uns.
        """
        z = self._stick_zustand()
        if z is None:
            print("  ! Stick antwortet nicht auf die Zustandsfrage — "
                  "Netzwerkschluessel nicht pruefbar")
            return
        m = re.search(r"\bkey=(\d)", z)
        if not m or m.group(1) == "1":
            return

        angelernt = len(getattr(self.qccu, "devices", None) or {})
        if angelernt:
            print(f"  ! Der Stick hat keinen brauchbaren Netzwerkschluessel, "
                  f"es sind aber {angelernt} Geraete eingetragen.")
            print( "    Es wird KEINER erzeugt — das wuerde alle aussperren.")
            print( "    Entweder den bisherigen Stick zurueckstecken, oder die "
                   "Geraete loeschen und neu anlernen.")
            self.netzschluessel_fehlt = True
            return

        print("  Der Stick hat noch keinen Netzwerkschluessel — es wird einer "
              "erzeugt (es ist kein Geraet eingetragen).")

        # ⚠️ ERST die Kennung, DANN der Schluessel. Ohne Kennung legt der Stick
        # keinen ab, und zwar lautlos — `mC` quittiert trotzdem. Gewuerfelt
        # wird nur, wenn nachweislich KEINE da ist: `mGN` wirft eine
        # vorhandene weg, und die steht dann schon in der Zentrale des
        # Betreibers.
        if self._kennung_vorhanden() is False:
            print("  Der Stick hat noch keine Kennung — sie wird erzeugt.")
            self.ser.write(b"mGN\r\n")
            self.ser.flush()
            self._log(">>", "mGN")
            time.sleep(2.0)

        for cmd in ("mKX", "mC"):
            self.ser.write(cmd.encode() + b"\r\n")
            self.ser.flush()
            self._log(">>", cmd)
            time.sleep(1.0)

        z = self._stick_zustand()
        m = re.search(r"\bkey=(\d)", z or "")
        if m and m.group(1) == "1":
            print("  Netzwerkschluessel liegt im Stick.")
        else:
            self.netzschluessel_fehlt = True
            print("  ! Es konnte kein Netzwerkschluessel erzeugt werden — "
                  "Anlernen wird scheitern.")

    def netz_neu_beginnen(self):
        """Alle Geraete verwerfen und dem Stick einen Schluessel geben lassen.

        Der Ausweg aus der Lage „Stick ohne Netzwerkschluessel, aber Geraete
        eingetragen" — der zweite der beiden Wege, die die Oberflaeche nennt.
        Von selbst tut QCCU das NIE (siehe `_netzschluessel_sicherstellen`):
        ein neuer Schluessel sperrt jedes angelernte Geraet aus, und diese
        Entscheidung gehoert dem Betreiber.

        ⚠️ Ohne den alten Schluessel ist kein Funk-Ausschluss mehr moeglich —
        die Geraete hoeren uns nicht mehr. Sie brauchen daher jeweils einen
        WERKSRESET am Geraet, bevor sie neu angelernt werden koennen. Deshalb
        wird hier auch nicht `deleteDevices` benutzt: dessen Ausschlussversuch
        ginge dreimal ins Leere und kostete nur Zeit.

        Rueckgabe: (True, Meldung) oder (False, Grund).
        """
        weg = []
        q = self.qccu
        try:
            with q.lock:
                weg = list(getattr(q, "devices", {}) or {})
                for k in weg:
                    q.devices.pop(k, None)
                    if hasattr(q, "rf"):
                        q.rf.pop(k, None)
            for k in weg:
                self.by_hmid = {h: a for h, a in self.by_hmid.items() if a != k}
            if hasattr(q, "save_store"):
                q.save_store()
            for k in weg:
                if hasattr(q, "_enqueue"):
                    q._enqueue(("delete", k, [k]))
        except Exception as ex:                          # noqa: BLE001
            return False, f"Die Geraete konnten nicht entfernt werden: {ex}"

        print(f"  Netz neu begonnen: {len(weg)} Geraet(e) verworfen.")
        self.netzschluessel_fehlt = False
        try:
            self._netzschluessel_sicherstellen()
        except Exception as ex:                          # noqa: BLE001
            self.netzschluessel_fehlt = True
            return False, f"Der Schluessel konnte nicht erzeugt werden: {ex}"

        if self.netzschluessel_fehlt:
            return False, ("Der Stick hat weiterhin keinen Netzwerkschluessel. "
                           "Sitzt er richtig, und antwortet er auf `V`?")
        return True, (f"{len(weg)} Geraet(e) verworfen, der Stick hat einen "
                      f"neuen Netzwerkschluessel. Die Geraete brauchen jetzt "
                      f"je einen Werksreset, dann koennen sie neu angelernt "
                      f"werden.")

    SEQ_RESERVE = 2048

    def _seq_angleichen(self):
        """Zaehler des Sticks anheben, falls er hinter unserem Stand liegt."""
        if not self.mac_seq:
            return
        self.ser.write(b"m\r\n")
        self.ser.flush()
        time.sleep(0.4)
        stick = None
        try:
            for zeile in self.ser.read(4000).decode("ascii", "replace").splitlines():
                m = re.search(r"sn=([0-9A-Fa-f]{8})", zeile)
                if m:
                    stick = int(m.group(1), 16)
        except Exception:
            return
        if stick is None or stick >= self.mac_seq:
            return
        neu = self.mac_seq + self.SEQ_RESERVE
        self.ser.write(f"mS{neu:08X}".encode() + b"\r\n")
        self.ser.flush()
        time.sleep(0.25)
        print(f"  Stick-Zaehler stand auf 0x{stick:08X}, angehoben auf 0x{neu:08X} "
              f"(anderer Stick?)")
        self.mac_seq = neu

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._pair_worker, daemon=True).start()
        threading.Thread(target=self._cmd_worker, daemon=True).start()

    def stop(self):
        self._stop = True
        if self._raw:
            with self._log_lock:
                self._raw.close()
            self._raw = None

    def firmware_version(self):
        """Fassung, wie der Stick sie selbst meldet."""
        m = self._ask("V", r"(?:q-culfw|hmip-mac-avr)\s+[0-9]+\.[0-9]+\.[0-9]+")
        return m.group(0).strip() if m else None

    def release_for_flash(self):
        """Stick in den Bootlader schicken und den Port GANZ loslassen."""
        try:
            self._submit("B01", "ask")
        except Exception:
            pass
        time.sleep(0.4)
        self.stop()
        try:
            if getattr(self, "ser", None):
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # Wie viele Lesefehler in Folge geduldet werden, wenn der Fehler nicht
    # schon von sich aus eindeutig ist. Je Fehler wird eine halbe Sekunde
    # gewartet — nach fuenf Sekunden ohne einen einzigen brauchbaren Zugriff
    # ist der Anschluss nicht bloss kurz beschaeftigt.
    LESEFEHLER_GRENZE = 10

    @staticmethod
    def _zugang_hin(ex):
        """Sagt dieser Fehler schon fuer sich, dass der Anschluss weg ist?"""
        if isinstance(ex, serial.SerialException):
            return True
        if isinstance(ex, AttributeError):        # ser ist None (Einspielen)
            return True
        if isinstance(ex, OSError):
            # 5 Ein-/Ausgabefehler · 6 kein solches Geraet · 9 falscher
            # Deskriptor · 19 Geraet existiert nicht · 77 Deskriptor kaputt
            return getattr(ex, "errno", None) in (5, 6, 9, 19, 77)
        return False

    def _abmelden(self, grund):
        """Den Zugang aufgeben und den Anschluss loslassen.

        ⚠️ Nach einem Neustart des Sticks — Werksreset, Wachhund, Einspielen,
        kurz abgezogen — ist der geoeffnete Anschluss unbrauchbar, AUCH WENN
        das Geraet gleich darauf wieder da ist: der alte Deskriptor gehoert
        zu einer Verbindung, die es nicht mehr gibt, und der Pfad kann beim
        Wiederkommen sogar eine andere Nummer tragen. Wer hier weiterliest,
        bekommt bis in alle Ewigkeit denselben Fehler — und QCCU meldete
        derweil munter „Funk laeuft", waehrend nichts mehr ankam. (Am Aufbau
        nachgestellt: nach `mV` und nach dem Wachhund-Neustart.)

        Das Anbinden macht absichtlich NICHT dieser Faden, sondern der
        Waechter in qccu.py — dort liegt sie schon, samt Suche nach der
        gemerkten Seriennummer, Einrichtung und CUL-Zugang.
        """
        if self.tot:
            return
        self.tot = True
        self.tot_grund = str(grund)
        print(f"  ! Funkzugang verloren ({grund}) — der Stick wird neu gesucht.")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            merke("bad", "Funkzugang zum Stick verloren — er wird neu gesucht")
        try:
            self.stop()
        except Exception:                            # noqa: BLE001
            pass
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:                            # noqa: BLE001
            pass

    def _loop(self):
        while not self._stop:
            try:
                line = self.ser.readline().decode("ascii", "replace").strip()
                self._lesefehler = 0
            except Exception as ex:                  # noqa: BLE001
                self._lesefehler += 1
                if self._zugang_hin(ex) or self._lesefehler >= self.LESEFEHLER_GRENZE:
                    self._abmelden(f"Lesen: {ex}")
                    return
                time.sleep(0.5)
                continue
            if line:
                self._log("<<", line)
                try:
                    self._handle(line)
                except Exception as ex:
                    if self.verbose:
                        print(f"  ! Auswertung scheiterte: {ex}")

    def _ist_eigener_frame(self, line):
        """Gehoert die A-Zeile zum eigenen Netz (Ziel Rundruf oder Sammeladresse, Absender bekannt)?"""
        if len(line) < 21:
            return False
        src = line[9:15].lower()
        dst = line[15:21].lower()
        eigene = set(self.by_hmid)
        if self.own_addr:
            eigene.add(self.own_addr)
        if src in eigene or dst in eigene:
            return True
        return dst[:2] in ("f0", "e0") and src in eigene

    def _handle(self, line):
        if line[:1] == "A" and self.cul is not None:
            if not self._ist_eigener_frame(line):
                try:
                    self.cul.a_zeile(line)
                except Exception:
                    pass
            return

        with self._pending_lock:
            job = self._pending
        if job is not None and job.expect is not None and not job.done.is_set():
            m = job.expect.search(line)
            if m:
                job.reply = m
                job.done.set()
                return

        if TX_OK.match(line) or TX_NO.match(line):
            with self._pending_lock:
                job = self._pending
            if job is not None:
                job.verdict = "ok" if TX_OK.match(line) else "err"
                job.done.set()
            return

        mk = KURZQUITTUNG.match(line)
        if mk:
            peer = mk.group(1).lower()
            ev = self._acked.get(peer)
            if ev:
                self._log("##", f"Kurzquittung von {peer}")
                ev.set()
            return

        msq = STICKSEQ.match(line)
        if msq:
            v = int(msq.group(1), 16)
            if v > self.mac_seq:
                self.mac_seq = v
                self._save_state()
            return

        mb = BUDGET.match(line)
        if mb:
            self.budget = {"on": mb.group(1) == "1", "credit": int(mb.group(2)),
                           "max": int(mb.group(3)), "lovf": int(mb.group(4))}
            return

        mc = CNT.match(line)
        if mc:
            self.counters = dict(zip(CNT_KEYS, (int(x or 0) for x in mc.groups())))
            self._cnt_ev.set()
            return

        mr = RAW.match(line)
        if mr:
            try:
                raw = bytes.fromhex(mr.group(1))
                air = raw[1:1 + raw[0]]
            except Exception:
                return
            if len(air) >= 49 and air[9] == 0x10:
                # ⚠️ Ein Geraet, das angelernt werden WILL — der Knopf wurde
                # gedrueckt. Das wird IMMER vermerkt, auch bei geschlossenem
                # Fenster: genau dann ist es die Auskunft, die dem Anwender
                # fehlt. Verarbeitet wird nur bei offenem Fenster.
                self._merke_anlernwunsch(air)
                if time.time() < self.pair_until:
                    self._pair_q.put(air)
            return

        m = DEC.match(line)
        if not m:
            return
        payhex, src, dst, sec, ct = m.groups()

        # Empfangspegel mitnehmen, bevor irgendein Zweig zurueckspringt: er
        # steht in JEDER Zeile — auch in Quittungen und Rundrufmeldungen, und
        # gerade die kommen regelmaessig, wenn sonst nichts passiert.
        self._rssi_merken(src.lower(), payhex)

        # ⚠️ Ein VERSCHLUESSELTER Frame, den der Stick entschluesseln konnte,
        # kommt aus unserem Netz: der Netzschluessel gilt je Anlage, und eine
        # PM-Zeile entsteht nur bei gelungener Entschluesselung (q-culfw
        # `emit()` nach `rx_ok++`; sonst zaehlt `rx_mic`). Steht sein Absender
        # nicht im Bestand, ist das eine Auskunft und kein Rauschen — deshalb
        # wird sie hier abgegriffen, bevor die Verarbeitung sie verwirft.
        if int(sec) >= 1:
            self._pruefe_verwaist(src.lower())

        if int(ct) == 4 and ACK.search(line):
            ev = self._acked.get(src.lower())
            if ev:
                ms = ACKSEQ.search(line)
                self._log("##", f"Quittung von {src.lower()} fuer sn="
                                f"{ms.group(1) if ms else '?'}")
                ev.set()
            return

        # HUCKEPACK-Quittung: hat das Geraet auf unseren Frame gleich etwas
        # zu sagen (Statusmeldung auf einen Schaltbefehl), setzt es im Kopf
        # seines naechsten Frames das piggybackACK-Bit statt eine eigene
        # Kurzquittung zu senden — so macht es das auch bei der eq-3-Zentrale
        # (Luftmitschnitt 18.08.2026). Der Frame ist damit Quittung UND
        # Inhalt: vermerken und normal weiterverarbeiten.
        mf = FLAGS.search(line)
        if mf and int(mf.group(1), 16) & 0x40:
            ev = self._acked.get(src.lower())
            if ev:
                self._log("##", f"Huckepack-Quittung von {src.lower()}")
                ev.set()

        if (self._pair_expect and src.lower() == self._pair_expect
                and int(sec) >= 1):
            self._pair_ok.set()

        if int(ct) == CT_NETWORK_MGMT:
            # Die Bereitmeldung zum Ausschluss ist das Einzige, was uns hier
            # interessiert. Sie MUSS ueber ein eigenes Ereignis laufen: ein
            # wartender Auftrag hilft nicht, weil die laufenden Statusmeldungen
            # des Geraets laufend Antwort-Auftraege erzeugen und den
            # wartenden ueberschreiben.
            b = bytes.fromhex(payhex) if len(payhex) % 2 == 0 else b""
            if len(b) >= 2 and b[0] == 1 and b[1] == NM_EXCLUDE_READY:
                ev = self._exclude_ready.get(src.lower())
                self._log("<<", f"Ausschluss bestaetigt von {src.lower()}")
                if ev:
                    ev.set()
            return

        if int(ct) == CT_ICMP:
            ms = SEQ.search(line)
            # `a=1`: der Stick hat den Rundruf schon selbst quittiert
            # (q-culfw icmp_ack, Vorgabe an) — dann keine zweite Quittung
            # von hier; die Zentrale von eq-3 schickt genau eine.
            self._icmp(payhex, src.lower(), dst.lower(),
                       ms.group(1) if ms else None,
                       stick_acked=(" a=1" in line))
            return

        if int(ct) != 0 or int(sec) < 1:
            return

        with self.lock:
            addr = self.by_hmid.get(src.lower())
        if not addr:
            # Fremder Absender ohne Anlernwunsch: das ist der Funkverkehr der
            # Nachbarschaft und geht uns nichts an. NICHT in den Posteingang —
            # dort gehoert nur hinein, was angelernt werden will (siehe
            # `_merke_anlernwunsch`), sonst fuellt er sich in einem
            # Mehrfamilienhaus mit fremden Geraeten, denen der Anwender
            # keinen Aufkleber zuordnen kann.
            return

        mf = FLAGS.search(line)
        for_us = bool(int(mf.group(1), 16) & RXF_FOR_US) if mf else True

        b = [int(payhex[i:i + 2], 16) for i in range(0, len(payhex), 2)]
        if len(b) < 4:
            return
        ln = b[0]
        if len(b) < 1 + ln + 2:
            return
        pt = b[1:1 + ln]
        if len(pt) < 2:
            return

        with self.lock:
            self.devseq[src.lower()] = pt[1]

        if for_us and (pt[0] & APP_RESP_REQ) and (pt[0] & 0x3F) != FT_ANSWER:
            # Die Zeitanfrage will die ZEIT, nicht bloss eine Quittung: die
            # echte CCU beantwortet sie mit einem TIME_INFO-Frame (Referenz-
            # mitschnitt, 10 Zyklen). Ein ANSWER darauf liesse das Geraet
            # ohne Uhr — und ohne Uhr kann es kein Wochenprofil ausfuehren.
            if (pt[0] & 0x3F) == FT_TIME_INFO:
                self._time_info(src.lower(), pt[1])
            else:
                self._answer(src.lower(), pt[1])

        if len(pt) < 4 or (pt[0] & 0x3F) != FT_STATUS:
            return

        flags = pt[2]
        fmt = flags & 0x03
        if fmt > 2:
            return

        i = 4
        shared_ch = shared_type = None
        if fmt == 1:
            shared_ch = pt[i]; i += 1
        if fmt == 2:
            shared_type = pt[i]; i += 1

        while i < len(pt):
            if fmt == 0:
                if i + 1 >= len(pt):
                    break
                typ, ch = pt[i], pt[i + 1]; i += 2
            elif fmt == 1:
                typ, ch = pt[i], shared_ch; i += 1
            else:
                typ, ch = shared_type, pt[i]; i += 1
            if typ is None or ch is None:
                break
            vl = self.vlen.get(typ)
            if not vl:
                if typ not in self._sdt_unbekannt:
                    self._sdt_unbekannt.add(typ)
                    print(f"  ! Statustyp {typ} ohne bekannte Laenge — der Rest "
                          f"dieser Meldung wird verworfen, nicht geraten.")
                break
            if i + vl > len(pt):
                break
            val = pt[i:i + vl]
            i += vl
            self._emit(addr, ch, typ, val, flags)

    def _kanal_fuehrt(self, addr, channel, param):
        """Fuehrt dieser Kanal diesen Parameter ueberhaupt?

        Die Zuordnung Statusdatentyp -> Parameter gilt nur, wo der Kanaltyp den
        Parameter auch kennt. Sonst wuerde ein Wert unter einem Namen landen,
        den der Klient nicht kennt — und ein unbekannter Datenpunkt kippt
        drueben schnell das ganze Geraet.
        """
        # Reiner Lesezugriff: die Kanalliste eines Geraets ist unveraenderlich,
        # solange es eingetragen ist — dafuer braucht es kein Schloss.
        d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
        if not d or not hasattr(d, "channel_list"):
            return False
        if not dict(d.channel_list()).get(int(channel)):
            return False
        return param in (self.t.paramset_of(d.devtype, channel, "VALUES") or {})

    def _emit(self, addr, channel, sdt, value, flags):
        """Einen Eintrag melden. Gedeutet wird nur, was belegt ist."""
        if sdt == SDT_BINARY:
            # Ein Byte, drei Aussagen — Bitlage aus der Ground-Truth, und genau
            # diese drei Parameter fuehrt SWITCH_TRANSMITTER auch:
            #   bit7 PROCESS · bit6 STATE (das Relais) · bits3..0 SECTION
            state = (value[0] & 0x40) != 0
            self.qccu.set_value_internal(addr, channel, "STATE", state)
            for param, wert in (("PROCESS", 1 if value[0] & 0x80 else 0),
                                ("SECTION", value[0] & 0x0F)):
                if self._kanal_fuehrt(addr, channel, param):
                    self.qccu.set_value_internal(addr, channel, param, wert)
            if self.verbose:
                print(f"  <- {addr}:{channel} STATE={state}")
            return

        deutung = SDT_DEUTUNG.get(sdt)
        if deutung:
            param, wandeln, einheit = deutung
            if self._kanal_fuehrt(addr, channel, param):
                wert = wandeln(bytes(value))
                self.qccu.set_value_internal(addr, channel, param, wert)
                if self.verbose:
                    print(f"  <- {addr}:{channel} {param}={wert}{einheit}")
                return

        # Nicht gedeutet — dann wenigstens beim Namen nennen, unter dem eq-3
        # den Wert fuehrt. Der Wert selbst bleibt roh: eine Skalierung, die
        # wir nicht an einem Geraet gemessen haben, waere geraten (und in
        # Volt oder Prozent faellt so etwas erst spaet auf).
        raw = "".join(f"{x:02X}" for x in value)
        self.qccu.set_value_internal(addr, channel, f"RAW_SDT{sdt}", raw)
        if self.verbose:
            name = None
            try:
                name = self.t.sdt_name(sdt)
            except Exception:                            # noqa: BLE001
                pass
            wie = f"SDT{sdt} ({name})" if name else f"SDT{sdt}"
            print(f"  <- {addr}:{channel} {wie}={raw} (ungedeutet)")

    def _sender(self):
        """Einziger Schreiber auf der Leitung."""
        while not self._stop:
            job = None
            try:
                job = self._txq.get_nowait()
            except queue.Empty:
                pass
            if job is None and self._gate.is_set():
                if self._ans_hold is None:
                    try:
                        self._ans_hold = self._ansq.get_nowait()
                    except queue.Empty:
                        pass
                if self._ans_hold is not None and self._ans_hold.not_before <= time.time():
                    job = self._ans_hold
                    self._ans_hold = None
            if job is None:
                time.sleep(0.01)
                continue

            with self._pending_lock:
                self._pending = job
            try:
                self.ser.write(job.cmd.encode() + b"\r\n")
                self.ser.flush()
                self._log(">>", f"{job.cmd}  [{job.kind}]")
            except Exception as ex:                  # noqa: BLE001
                self._log("##", f"Schreiben scheiterte: {ex}")
                job.verdict = "err"
                # Ein geschlossener oder verschwundener Anschluss meldet sich
                # auch hier — und zwar frueher als beim Lesen, wenn gerade
                # niemand sendet.
                if self._zugang_hin(ex):
                    job.written.set()
                    job.done.set()
                    self._abmelden(f"Schreiben: {ex}")
                    return
            job.written.set()

            if job.expect is not None:
                job.done.wait(self.ask_timeout)
            elif job.expects_verdict and job.verdict is None:
                job.done.wait(self.verdict_timeout)
            with self._pending_lock:
                self._pending = None
            job.done.set()

    def _submit(self, cmd, kind, not_before=0.0, expect=None):
        job = _Job(cmd, kind, not_before, expect)
        # Antworten auf einen empfangenen Frame gehoeren in die Antwort-Schlange:
        # nur dort wird `not_before` eingehalten. Wer sofort zurueckfunkt, redet
        # womoeglich, bevor das Geraet wieder zuhoert — die echte Zentrale
        # laesst rund 130 ms verstreichen.
        (self._ansq if kind in ("answer", "zeit") else self._txq).put(job)
        return job

    def _answer(self, hmid, appseq):
        """ANSWER auf einen Frame, der eine Antwort angefordert hat."""
        if not self.answer_enabled:
            self._log("##", f"ANTWORT UNTERDRUECKT appSeq=0x{appseq:02X}")
            return
        self._submit(f"ms{hmid.upper()}02{appseq:02X}00", "answer",
                     time.time() + self.answer_delay)

    @staticmethod
    def zeit_payload(t):
        """Die sieben Bytes einer TIME_INFO-Nutzlast aus einer Ortszeit.

        Aufbau, an einem Frame der echten CCU verifiziert
        (`00 1a 08 4b 96 19 19` = Dienstag, 11.08.2026 22:25:25):

            [0] 0x00      Bedeutung offen — steht im Referenzframe so
            [1] Jahr-2000     (Jar: `TimeInfoFrame.setPayload`, 2000+p[1])
            [2] Monat
            [3] Wochentag<<5 | Tag   (Wochentag: Sonntag=0, Dienstag=2)
            [4] 0x80 | Stunde        (Bit 7 aus dem Referenzframe, offen)
            [5] Minute
            [6] Sekunde

        Geliefert wird ORTSZEIT: im Referenzframe stand die Uhrzeit der
        Aufnahme, nicht UTC.
        """
        wochentag = (t.tm_wday + 1) % 7          # Python Montag=0 -> CCU Sonntag=0
        return bytes((0x00,
                      t.tm_year - 2000,
                      t.tm_mon,
                      (wochentag << 5) | t.tm_mday,
                      0x80 | t.tm_hour,
                      t.tm_min,
                      t.tm_sec))

    def _time_info(self, hmid, appseq):
        """Die Zeitanfrage eines Geraets mit der Ortszeit beantworten."""
        p = self.zeit_payload(time.localtime())
        self._submit(f"ms{hmid.upper()}{FT_TIME_INFO:02X}{appseq:02X}{p.hex().upper()}",
                     "zeit", time.time() + self.answer_delay)
        if self.verbose:
            print(f"  Zeit an {hmid}: {time.strftime('%a %d.%m.%Y %H:%M:%S')}")

    # Ab welcher Aenderung ein neuer Pegel gemeldet wird. Der Wert schwankt
    # von Frame zu Frame um ein bis zwei dB; jede Zuckung zu melden fuellte
    # die Aufzeichnung der Gegenstelle, ohne etwas zu sagen.
    RSSI_SCHWELLE = 3

    def _rssi_merken(self, src, payhex):
        """Empfangspegel eines Geraeteframes festhalten und melden.

        Der Stick haengt an jede Zeile `<rssi><lqi>` hinter die Nutzlast; der
        Pegel ist ein Zweierkomplement in dBm, wie der CC1101 ihn nach
        Datenblatt 17.3 liefert (die Umrechnung macht die Firmware).

        Gemeldet wird als **`RSSI_DEVICE`** an Kanal 0 — der Parameter, den
        eine Zentrale von eQ-3 dort fuehrt (an der HmIP-PS-2 nachgesehen:
        `RSSI_DEVICE` und `RSSI_PEER`, beide INTEGER −128…127) und den die
        Gegenstelle als Signalstaerke des Geraets liest
        (`aiohomematic … device.signal_strength`).

        ⚠️ Was hier steht, ist **unsere Messung: wie stark WIR das Geraet
        hoeren.** Den umgekehrten Weg — was das Geraet von uns empfaengt —
        meldet es uns nicht; in keinem Statusframe steckt so ein Wert.
        `RSSI_PEER` bleibt daher leer, statt eine Zahl zu erfinden.
        """
        q = self.qccu
        addr = self.by_hmid.get(src)
        if not addr or q is None or not hasattr(q, "set_value_internal"):
            return
        try:
            b = bytes.fromhex(payhex)
        except ValueError:
            return
        if not b:
            return
        ln = b[0]
        if len(b) < 1 + ln + 2:            # ohne <rssi><lqi> nichts zu holen
            return
        wert = b[1 + ln] - 256 if b[1 + ln] > 127 else b[1 + ln]
        if wert == 0 or wert < -128 or wert > 0:
            # 0 und positive Werte sind kein Pegel, sondern eine Zeile ohne
            # Messung (oder eine Firmware vor 2.0.22, die falsch rechnete).
            return
        vorher = self._rssi.get(src)
        self._rssi[src] = wert
        if vorher is not None and abs(wert - vorher) < self.RSSI_SCHWELLE:
            return
        try:
            q.set_value_internal(addr, 0, "RSSI_DEVICE", wert)
        except Exception as ex:                          # noqa: BLE001
            if self.verbose:
                print(f"  ! Pegel nicht vermerkt: {ex}")

    def _icmp(self, payhex, src, dst, sn, stick_acked=False):
        """Ein ICMPv6-Frame: auswerten, vermerken, beantworten."""
        try:
            raw = bytes.fromhex(payhex)
        except Exception:
            return
        if not raw or len(raw) < 1 + raw[0]:
            return
        pl = raw[1:1 + raw[0]]
        if not pl:
            return
        t = pl[0]
        code = pl[1] if len(pl) > 1 else None
        name = ICMP_NAME.get(t, f"ICMP 0x{t:02X}")
        self.icmp_seen[name] = self.icmp_seen.get(name, 0) + 1
        where = GROUP_ADDR.get(dst, dst)
        self._log("<<", f"ICMP {name} {src}->{where}"
                        + (f" code=0x{code:02X}" if code is not None else ""))
        if self.verbose:
            print(f"  Netz-Haushalt: {name} von {src} an {where}")

        addr = self.by_hmid.get(src)
        if addr:
            try:
                self.qccu.note_reachable(addr, True)
            except Exception as ex:
                print(f"  ! Lebenszeichen nicht vermerkt: {ex}")

        if not self.icmp_answer:
            return

        to_group = dst in GROUP_ADDR

        if t == ICMP_NEIGHBOR_ADVERTISEMENT and to_group and sn and not stick_acked:
            self._submit(f"mT41{src.upper()}00{MAC_ROLE_CENTRAL:02X}{sn.upper()}",
                         "answer")
            return

        if t == ICMP_ECHO_REQUEST:
            self._submit(f"mT21{src.upper()}{ICMP_ECHO_REPLY:02X}"
                         f"{(code or 0):02X}", "answer")
            return

        if t == ICMP_NEIGHBOR_SOLICITATION:
            self._log("##", "NEIGHBOR_SOLICITATION unbeantwortet "
                            "(Antwortform nicht belegt)")
            return

        if t == ICMP_ROUTER_SOLICITATION:
            self._log("##", "ROUTER_SOLICITATION unbeantwortet "
                            "(Antwortform nicht belegt)")

    def _read_counters(self, timeout=0.6):
        """Zaehlwerke des Sticks abholen (`m` -> `Pm rx=… ok=… …`)."""
        self._cnt_ev.clear()
        job = self._submit("m", "probe")
        if not job.written.wait(timeout):
            return None
        if not self._cnt_ev.wait(timeout):
            return None
        return dict(self.counters)

    def _next_seq(self, hmid):
        """Eigener, fortlaufender UNGERADER Zaehler je Geraet."""
        with self.lock:
            if hmid not in self.appseq and hmid in self.devseq:
                self.appseq[hmid] = self.devseq[hmid]
            s = (self.appseq.get(hmid, 0) + 2) & 0xFF
            if not s % 2:
                s = (s + 1) & 0xFF
            if not s:
                s = 1
            self.appseq[hmid] = s
        self._save_state()
        return s

    def on_set(self, ccu_address, channel, param, value):
        """Befehl ANNEHMEN und sofort zurueckkehren."""
        self._cmdq.put((ccu_address, channel, param, value))
        return None

    def _cmd_worker(self):
        while not self._stop:
            try:
                job = self._cmdq.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                acked = self._do_set(*job)
            except Exception as ex:
                print(f"  ! Sendepfad scheiterte: {ex}")
                continue
            if acked is not None:
                try:
                    self.qccu.note_reachable(job[0], bool(acked))
                except Exception as ex:
                    print(f"  ! Erreichbarkeit nicht vermerkt: {ex}")

    def _do_set(self, ccu_address, channel, param, value):
        """Der eigentliche Sendevorgang — laeuft im Arbeitsfaden."""
        hmid = None
        with self.lock:
            for h, a in self.by_hmid.items():
                if a == ccu_address.upper():
                    hmid = h
                    break
        if not hmid:
            if self.verbose:
                print(f"  ! keine Funkadresse zu {ccu_address}")
            return

        if param != "STATE":
            if self.verbose:
                print(f"  ! {param} wird noch nicht gesendet")
            return

        on = value in (True, 1, "1", "true", "True", "on")
        seq = self._next_seq(hmid)
        payload = f"86{seq:02X}02{int(channel):02X}{0xC8 if on else 0x00:02X}"
        cmd = f"ms{hmid.upper()}{payload}"

        before = self._read_counters() if self.measure else None
        self._log("##", f"BEFEHL {ccu_address}:{channel} STATE={bool(on)} "
                        f"appSeq=0x{seq:02X}")

        ev = threading.Event()
        self._acked[hmid] = ev
        acked = False
        after = None
        verdicts = []
        try:
            self._gate.clear()
            for attempt in range(1, self.tx_tries + 1):
                ev.clear()
                job = self._submit(cmd, "cmd")
                job.done.wait(self.verdict_timeout + 0.5)
                verdict = job.verdict or "kein Urteil"
                verdicts.append(verdict)
                if self.verbose:
                    extra = f"  (Versuch {attempt})" if attempt > 1 else ""
                    print(f"  -> {cmd}{extra}  Stick: {verdict}")
                if job.verdict == "err":
                    self._log("##", "Stick hat NICHT gesendet")
                    continue
                if ev.wait(self.tx_timeout):
                    acked = True
                    if self.verbose and attempt > 1:
                        print(f"     quittiert nach Versuch {attempt}")
                    break
            after = self._read_counters() if before else None
        finally:
            self._acked.pop(hmid, None)
            self._gate.set()

        if not acked and self.verbose:
            print(f"     keine Quittung nach {self.tx_tries} Versuchen")
        self._log("##", f"ERGEBNIS quittiert={acked} urteile={verdicts}")

        if before and after:
            d = {k: after[k] - before[k] for k in CNT_KEYS}
            txt = " ".join(f"{k}+{v}" for k, v in d.items() if v)
            self._log("##", f"ZAEHLWERKE {txt or 'unveraendert'}")
            if self.verbose:
                print(f"     Zaehlwerke {txt or 'unveraendert'}")
        return acked


    def radio_state(self):
        """Zustand fuer die Oberflaeche."""
        self._read_counters()
        return {"counters": self.counters, "budget": self.budget,
                "own_addr": self.own_addr,
                "tot": bool(self.tot), "tot_grund": self.tot_grund,
                # Ohne Netzwerkschluessel schlaegt jedes Anlernen fehl. Der
                # Zustand stand frueher nur im Protokoll — die Oberflaeche
                # meldete derweil „Firmware aktuell", und der Anwender suchte
                # den Fehler beim Geraet.
                "netzschluessel_fehlt": bool(self.netzschluessel_fehlt),
                "icmp": dict(self.icmp_seen),
                "devices": {h: a for h, a in self.by_hmid.items()}}

    # Wie lange auf die Kurzquittung gewartet wird. Die Gegenstelle antwortet
    # binnen weniger Millisekunden; eine Sekunde ist reichlich und haelt den
    # Knopf in der Oberflaeche flott.
    PING_WARTEN = 1.0

    def erreichbarkeit_pruefen(self, hmid, warten=None):
        """Aktiv nachsehen, ob ein Geraet noch antwortet.

        ⚠️ Ohne das faellt Stille erst auf, wenn jemand vergeblich schaltet:
        `unreach` wird gesetzt, wenn ein Befehl dreimal unquittiert bleibt —
        und ein Geraet, das niemand schaltet, gilt beliebig lange als in
        Ordnung. Am Aufbau gemessen (19.08.2026): eine PS-2 war zwei Stunden
        aus dem Netz, die Oberflaeche meldete unveraendert „erreichbar".

        Gesendet wird die **Uhrzeit** — dieselbe Auskunft, die die Zentrale
        einem fragenden Geraet ohnehin gibt. Sie aendert am Geraet nichts und
        ist die harmloseste Sendung, die es gibt. Die Antwort ist nicht der
        Inhalt, sondern die **Kurzquittung**: jeden Unicast quittiert ein
        HmIP-Geraet mit sechs Byte, ohne Zaehler und ohne Verschluesselung.
        Kommt sie, lebt das Geraet und hoert uns.

        ⚠️ Das kostet Sendezeit — sparsam benutzen, nicht im Minutentakt.

        Rueckgabe: True (antwortet), False (keine Antwort), None (kein Funk
        oder Geraet unbekannt).
        """
        hmid = (hmid or "").lower()
        if not hmid or hmid not in self.by_hmid:
            return None
        ev = threading.Event()
        self._acked[hmid] = ev
        try:
            self._time_info(hmid, self._next_seq(hmid))
            antwortet = ev.wait(warten or self.PING_WARTEN)
        finally:
            self._acked.pop(hmid, None)
        addr = self.by_hmid.get(hmid)
        if addr:
            try:
                self.qccu.note_reachable(addr, bool(antwortet))
            except Exception as ex:                  # noqa: BLE001
                print(f"  ! Erreichbarkeit nicht vermerkt: {ex}")
        if self.verbose:
            print(f"  Erreichbarkeit {hmid}: "
                  + ("antwortet" if antwortet else "keine Antwort"))
        self._log("##", f"PING {hmid} -> {'ok' if antwortet else 'keine Antwort'}")
        return bool(antwortet)

    def funk_exclude(self, hmid, warten=2.5):
        """Ein Geraet ueber Funk ausschliessen — wie die CCU beim Loeschen.

        Ohne diesen Anruf erfaehrt das Geraet nie, dass es entlassen wurde: es
        funkt weiter zu einer Zentrale, die es nicht mehr kennt, und laesst
        sich erst nach einem Werksreset von Hand wieder anlernen. Mit ihm geht
        es von selbst in den Anlernzustand zurueck.

        Die Folge steht im Referenzmitschnitt der echten CCU:

            ZENTRALE->GERAET  DEVICE_EXCLUDE_REQUEST   (0xF0)
            GERAET->ZENTRALE  DEVICE_EXCLUDE_READY     (0xF1)
            ZENTRALE->GERAET  DEVICE_EXCLUDE_CONCLUDE  (0xF2)

        Alle drei sind NETWORK_MGMT (Inhaltsart 1, daher `mT11`), ohne
        Nutzlast — die Aussage steckt allein im Typ-Byte.

        Rueckgabe: True, wenn das Geraet mit READY geantwortet hat. Der
        Abschluss geht in jedem Fall raus; ein Geraet, das gerade stromlos
        ist, soll den Ausschluss nicht dauerhaft blockieren.
        """
        dst = hmid.upper()
        ev = threading.Event()
        self._exclude_ready[hmid.lower()] = ev
        try:
            self._submit(f"mT11{dst}{NM_EXCLUDE_REQUEST:02X}", "cmd")
            bestaetigt = ev.wait(warten)
        finally:
            self._exclude_ready.pop(hmid.lower(), None)

        # Der Abschluss folgt kurz darauf — bei der echten CCU 77 ms nach der
        # Bereitmeldung.
        time.sleep(0.1)
        self._submit(f"mT11{dst}{NM_EXCLUDE_CONCLUDE:02X}", "cmd")
        m = bestaetigt
        with self.lock:
            # ⚠️ Wer hier hinausfaellt, kann gleich wieder anklopfen — dasselbe
            # Geraet, neue Funkadresse. Die alte Zuordnung merken wir uns, um
            # sie beim Wiederanlernen aus der Verwaisten-Liste zu raeumen:
            # sonst steht das Geraet dort als „funkt noch, gehoert nicht dazu",
            # obwohl es laengst wieder angelernt ist (19.08.2026 gesehen —
            # c5410d blieb neben dem frisch angelernten c5410e stehen).
            kennung = self.by_hmid.pop(hmid.lower(), None)
            if kennung:
                self._ehemals[kennung.upper()] = hmid.lower()
        if self.verbose:
            print(f"  Ausschluss {hmid}: "
                  + ("bestaetigt" if m else "ohne Antwort abgeschlossen"))
        return bool(m)

    def _ask(self, cmd, pattern, tries=3):
        """Frage an den Stick, Antwort abwarten."""
        pat = re.compile(pattern)
        for _ in range(tries):
            job = self._submit(cmd, "ask", expect=pat)
            if job.done.wait(self.ask_timeout + 0.5) and job.reply:
                return job.reply
        return None

    def start_pairing(self, sticker=None, seconds=60, next_addr=None,
                      local_key=None):
        """Anlernfenster oeffnen."""
        key = None
        src = ""
        lk = _nur_zeichen(local_key)
        st = _nur_zeichen(sticker).upper()

        if lk:
            if len(lk) != 32:
                return f"LocalKey hat {len(lk)} statt 32 Hexziffern"
            try:
                key = bytes.fromhex(lk)
            except ValueError:
                return "LocalKey ist keine gueltige Hexzahl"
            src = "LocalKey"
        elif st:
            # Wer die 32 Hexziffern zur Hand hat, soll sie hier eintragen
            # duerfen — die Anleitung nennt beide Formen, und ein Anwender
            # kann nicht wissen, dass sie intern in zwei Feldern liegen.
            if len(st) == 32 and all(c in "0123456789ABCDEF" for c in st):
                key = bytes.fromhex(st)
                src = "LocalKey"
            else:
                if len(st) != 26:
                    return (f"Aufkleber hat {len(st)} statt 26 Zeichen "
                            f"(Form ABCEF-GHJKL-MNPQR-STUWX-YZ2345) — "
                            f"oder 32 Hexziffern. Trennzeichen sind egal.")
                try:
                    key = sticker_to_local_key(st)
                except ValueError as ex:
                    # ⚠️ Das Alphabet des Aufklebers kennt kein D, I, O und V —
                    # gerade weil sie sich mit 0, 1 und U verwechseln lassen.
                    # Wer eines davon eintippt, hat fast immer abgelesen statt
                    # sich vertan; das gehoert in die Meldung, sonst sucht er
                    # den Fehler beim Geraet.
                    verwechselt = sorted({c for c in st if c in "DIOV"})
                    rat = (f" — die Zeichen {', '.join(verwechselt)} gibt es "
                           f"auf dem Aufkleber nicht; gemeint sind vermutlich "
                           f"{', '.join({'D': '0', 'I': '1', 'O': '0', 'V': 'U'}[c] for c in verwechselt)}"
                           if verwechselt else "")
                    return f"Aufkleber enthaelt ein unzulaessiges Zeichen ({ex}){rat}"
                src = "Aufkleber"
        else:
            return "Aufkleber oder LocalKey noetig"

        if next_addr:
            self.pair_next_addr = next_addr.lower()
        if not self.pair_next_addr:
            self.pair_next_addr = zufaellige_adresse((self.own_addr,))[:4] + "01"
        self.pair_key = key
        self.pair_key_src = src
        self.pair_until = time.time() + float(seconds)
        self.pair_last = (f"Fenster offen ({int(seconds)} s), {src}, "
                          f"Adresse {self.pair_next_addr}")
        self._log("##", f"ANLERNEN offen {seconds}s -> {self.pair_next_addr}")
        if self.verbose:
            print(f"  Anlernen: Fenster {int(seconds)} s offen, "
                  f"neue Adresse {self.pair_next_addr}")
        return None

    def stop_pairing(self):
        self.pair_until = 0.0
        self.pair_key = None
        self.pair_last = "Fenster zu"

    def pair_state(self):
        rest = self.pair_until - time.time()
        return {"open": rest > 0, "seconds_left": int(rest) if rest > 0 else 0,
                "next_addr": self.pair_next_addr, "last": self.pair_last,
                "key_src": self.pair_key_src if self.pair_key else ""}

    def _pair_worker(self):
        """Bearbeitet Anlern-Anfragen."""
        while not self._stop:
            try:
                air = self._pair_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._pair_do(air)
            except Exception as ex:
                self.pair_last = f"Anlernen scheiterte: {ex}"
                self._log("##", f"ANLERNEN FEHLER {ex}")
                if self.verbose:
                    print(f"  ! Anlernen scheiterte: {ex}")

    def _pair_do(self, air):
        src     = air[3:6]
        sgtin   = air[10:22]
        devtype = int.from_bytes(air[24:28], "big")
        nonce   = air[33:37]
        otk     = air[41:49]

        if not self.pair_key or time.time() >= self.pair_until:
            return
        if self._pair_busy:
            return

        self._pair_busy = True
        self.pair_last = f"Anfrage von {src.hex()}, Typ {devtype} — Angebot wird gebaut"
        if self.verbose:
            print(f"  Anlernen: Anfrage von {src.hex()}, Kennung {sgtin.hex()}, "
                  f"Typ {devtype}")

        devkey  = self.pair_key
        ap_otk  = secrets.token_bytes(8)
        nonce_ks = secrets.token_bytes(4)
        newa    = bytes.fromhex(self.pair_next_addr)

        m = self._ask("mY" + otk.hex().upper() + ap_otk.hex().upper(),
                      r"Pm y ([0-9A-F]{32})")
        if not m:
            raise RuntimeError("Stick antwortet nicht auf mY")
        ext = bytes.fromhex(m.group(1))

        def wrap(key, four, inp):
            """`inp=None`: der Stick nimmt seinen abgelegten Schluessel."""
            if not self._ask("mWK" + key.hex().upper(), r"gesetzt"):
                raise RuntimeError("Stick nimmt den Arbeitsschluessel nicht an")
            nutz = "*" if inp is None else inp.hex().upper()
            r = self._ask("mW" + sgtin.hex().upper() + four.hex().upper() + nutz,
                          r"Pm w ([0-9A-F]{32}) ([0-9A-F]{8})")
            if not r:
                raise RuntimeError("Stick antwortet nicht auf mW")
            return bytes.fromhex(r.group(1)), bytes.fromhex(r.group(2))

        # `None` heisst: den Netzwerkschluessel setzt der STICK ein — QCCU
        # kennt ihn nicht und soll ihn nicht kennen.
        otk_enc, otk_mic = wrap(ext, nonce, None)
        enc_nwk, nwk_mic = wrap(devkey, nonce_ks, otk_enc)

        acc  = bytes([0x01, 0x00, 0x8E]) + bytes.fromhex(self.own_addr) + src
        acc += bytes([0x20, 0x02]) + nonce_ks + otk_mic + ap_otk + enc_nwk
        acc += newa + (100).to_bytes(4, "big") + nwk_mic
        if len(acc) != 54:
            raise RuntimeError(f"Angebot {len(acc)} statt 54 Byte")

        self._pair_ok.clear()
        self._pair_expect = newa.hex().lower()

        # ⚠️ `As` ist das EINZIGE Kommando, auf das der Stick kein `Pm tx ok`
        # gibt (culfw-Stil: rohes Senden, kein Echo). Als "cmd" eingereiht
        # wartete der Sender 1,2 s vergeblich auf das Urteil — und in genau
        # dieser Zeit fragt das frisch angelernte Geraet schon nach der Zeit.
        # Der Beweis, dass das Angebot raus war, ist ohnehin die Bestaetigung.
        self._submit("As" + bytes([len(acc)]).hex().upper() + acc.hex().upper(), "raw")
        self.pair_last = f"Angebot gesendet, Geraet bekommt {newa.hex()}"
        self._log("##", f"ANLERNEN Angebot -> {newa.hex()}")

        got = self._pair_ok.wait(5.0)
        self._pair_expect = None
        if not got:
            self._pair_busy = False
            self.pair_last = (f"Geraet hat das Angebot nicht angenommen — "
                              f"stimmt der Schluessel? Fenster bleibt offen")
            self._log("##", "ANLERNEN keine Bestaetigung — nichts eingetragen")
            if self.verbose:
                print("  ! Anlernen: keine Bestaetigung, nichts eingetragen")
            return

        # ZUERST das Geraet eintragen und die Funkadresse binden — vor allem
        # anderen. Die erste Anfrage des Geraets (Zeit, Zustand) kommt rund
        # eine Sekunde nach der Bestaetigung; wer sie als „fremden Absender"
        # verwirft, treibt das Geraet in die Router-Suche (E00002), und aus
        # der kommt es ohne Werksreset nicht mehr heraus — es wiederholt dann
        # JEDE Sendung dreifach. Am Geraet gemessen, 17.08.2026.
        ccu_addr = sgtin.hex()[-14:].upper()
        self.qccu.add_device(ccu_addr, devtype)
        self.bind(newa.hex(), ccu_addr)

        # Wegemeldung wie die echte Zentrale: rund 185 ms nach ihrer Quittung
        # (fester Takt, in allen Referenzzyklen gleich). Nicht 2,5 s — in der
        # Luecke suchte das Geraet bereits einen Router.
        time.sleep(0.15)
        self._submit("mT31f00002" + "01" + newa.hex() + "0000004017", "cmd")

        # Die beiden Verknuepfungen wie bisher spaeter — zu frueh gesendet
        # nimmt das Geraet sie nicht an (am echten Geraet gemessen).
        time.sleep(2.35)
        self._submit("ms" + newa.hex().upper() + "C1050101"
                     + newa.hex().upper() + "030000", "cmd")
        time.sleep(0.3)
        self._submit("ms" + newa.hex().upper() + "C1070301"
                     + newa.hex().upper() + "010000", "cmd")

        self.spuren_raeumen(src.hex(), ccu_addr)

        self.pair_last = f"{ccu_addr} angelernt als {newa.hex()} (Typ {devtype})"
        self._log("##", f"ANLERNEN fertig {ccu_addr} -> {newa.hex()}")
        if self.verbose:
            print(f"  Anlernen abgeschlossen: {ccu_addr} -> {newa.hex()}")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            merke("ok", f"{ccu_addr} angelernt (Funkadresse {newa.hex()})")
        frisch = getattr(self.qccu, "merke_frisch_angelernt", None)
        if frisch:
            frisch(ccu_addr)

        nxt = (int(self.pair_next_addr, 16) + 1) & 0xFFFFFF
        self.pair_next_addr = f"{nxt:06x}"
        if hasattr(self.qccu, "pair_next_addr"):
            self.qccu.pair_next_addr = self.pair_next_addr
            self.qccu.save_store()
        self.pair_key = None
        self.pair_until = 0.0
        self._pair_busy = False

    # -- Anlernwuensche: gehoerte, aber unbekannte Geraete ----------------
    #
    # ⚠️ NICHT „Posteingang". In einer Zentrale von eq-3 heisst so die Liste
    # der bereits ANGELERNTEN, nur noch nicht in Betrieb genommenen Geraete.
    # Was hier steht, ist das Gegenteil: Geraete, die noch gar nicht dazu
    # gehoeren und darum bitten. Denselben Namen fuer beides zu benutzen,
    # laesst den Anwender vergeblich nach einem Geraet suchen, das er zu
    # besitzen glaubt (Dirk, 19.08.2026).
    FREMDE_GRUNDFRIST = 300.0     # untere Schranke — auch beim ersten Ruf
    FREMDE_FEHLRUFE = 3           # so viele ausgebliebene Rufe = verstummt
    FREMDE_HOECHSTFRIST = 3600.0  # obere Schranke
    FREMDE_MAX = 20

    def _merke_anlernwunsch(self, air):
        """Ein Geraet will angelernt werden — in den Posteingang.

        ⚠️ NUR das gehoert hier hinein. Ein Anlernwunsch heisst: an diesem
        Geraet wurde der Knopf gedrueckt. Jeden fremden Absender zu sammeln
        waere das Gegenteil einer Hilfe — in einem Mehrfamilienhaus stuenden
        dort die Geraete der Nachbarn, denen der Anwender keinen Aufkleber
        zuordnen kann.

        Die Anfrage traegt Kennung und Typ offen; beides ist ohne Schluessel
        lesbar und macht den Eintrag erst brauchbar."""
        try:
            hmid = air[3:6].hex()
            sgtin = air[10:22].hex().upper()
            devtype = int.from_bytes(air[24:28], "big")
        except Exception:                            # noqa: BLE001
            return
        jetzt = time.time()
        with self.lock:
            e = self._fremde.get(hmid)
            if e is None:
                if len(self._fremde) >= self.FREMDE_MAX:
                    aeltester = min(self._fremde, key=lambda k: self._fremde[k]["zuletzt"])
                    del self._fremde[aeltester]
                self._fremde[hmid] = {"sgtin": sgtin, "devtype": devtype,
                                      "zuerst": jetzt, "zuletzt": jetzt, "anzahl": 1}
                neu = True
            else:
                e.update(sgtin=sgtin, devtype=devtype, zuletzt=jetzt)
                e["anzahl"] += 1
                neu = False
        if neu:
            self._log("<<", f"Anlernwunsch von {hmid} (Typ {devtype}) — Posteingang")
            if self.verbose:
                print(f"  Anlernwunsch: {hmid}, Typ {devtype}")

    # -- Verwaiste Geraete: funken mit unserem Schluessel, gehoeren nicht mehr
    #
    # ⚠️ Der Fall, der diesen Code ausgeloest hat (19.08.2026): eine PS-2 wurde
    # ausgeschlossen, quittierte den Ausschluss auf Anwendungsebene
    # (NM_EXCLUDE_READY) — und funkte danach weiter unter ihrer alten
    # Funkadresse mit unserem Netzschluessel. Sie war also NICHT im
    # Werkszustand und liess sich folglich auch nicht neu anlernen: ein
    # angelerntes Geraet sendet keinen Anlernruf. In der Oberflaeche war davon
    # nichts zu sehen — die Frames wurden still verworfen, und der Anwender
    # drueckte vergeblich die Taste. Die Auskunft war da, wir haben sie
    # weggeworfen.
    VERWAIST_GRUNDFRIST = 1800.0     # der Herzschlag kommt alle ~10 min
    VERWAIST_FEHLRUFE = 3
    VERWAIST_HOECHSTFRIST = 21600.0
    VERWAIST_MELDEPAUSE = 3600.0     # nicht oefter als stuendlich melden
    VERWAIST_MAX = 20

    def _pruefe_verwaist(self, hmid):
        """Ein entschluesselbarer Frame von jemandem, den wir nicht fuehren."""
        jetzt = time.time()
        with self.lock:
            if hmid in self.by_hmid or hmid == (self.own_addr or ""):
                return
            # Waehrend eines Anlernvorgangs ist genau das der Normalfall: das
            # Geraet funkt schon verschluesselt, steht aber noch nicht im
            # Bestand. Kein Grund zur Meldung.
            if jetzt < self.pair_until or hmid == (self._pair_expect or ""):
                return
            e = self._verwaist.get(hmid)
            if e is None:
                if len(self._verwaist) >= self.VERWAIST_MAX:
                    aeltester = min(self._verwaist,
                                    key=lambda k: self._verwaist[k]["zuletzt"])
                    del self._verwaist[aeltester]
                e = {"zuerst": jetzt, "zuletzt": jetzt, "anzahl": 1,
                     "gemeldet": 0.0}
                self._verwaist[hmid] = e
            else:
                e["zuletzt"] = jetzt
                e["anzahl"] += 1
            faellig = jetzt - e["gemeldet"] > self.VERWAIST_MELDEPAUSE
            if faellig:
                e["gemeldet"] = jetzt
        if not faellig:
            return
        self._log("<<", f"{hmid} funkt mit unserem Netzschluessel, "
                        f"ist aber nicht angelernt")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            # ⚠️ KEINE Ursachenbehauptung: das Geraet kann ausgeschlossen
            # worden sein, es kann aber auch aus einem verworfenen Bestand
            # stammen („Geraete verwerfen und neu beginnen"). Belegt ist nur
            # der Zustand — und was daraus folgt.
            merke("warn", f"{hmid} funkt mit unserem Netzschluessel, ist hier "
                          f"aber nicht angelernt. Es ist damit NICHT im "
                          f"Werkszustand und laesst sich so auch nicht "
                          f"anlernen — ein Werksreset am Geraet hilft.")

    def verwaiste_aufraeumen(self):
        """Wer verstummt, faellt heraus — wie bei den Anlernwuenschen."""
        jetzt = time.time()
        weg = []
        with self.lock:
            for hmid, e in list(self._verwaist.items()):
                anzahl, spanne = e["anzahl"], e["zuletzt"] - e["zuerst"]
                if anzahl < 2 or spanne <= 0:
                    frist = self.VERWAIST_GRUNDFRIST
                else:
                    frist = max(self.VERWAIST_GRUNDFRIST,
                                min(self.VERWAIST_HOECHSTFRIST,
                                    self.VERWAIST_FEHLRUFE * spanne / (anzahl - 1)))
                if jetzt - e["zuletzt"] > frist:
                    weg.append(hmid)
                    del self._verwaist[hmid]
        for hmid in weg:
            self._log("<<", f"{hmid} funkt nicht mehr — nicht mehr verwaist gemeldet")
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("ok", f"{hmid} funkt nicht mehr mit unserem "
                            f"Netzschluessel — vermutlich zurueckgesetzt")
        return len(weg)

    def verwaiste_liste(self):
        """Was die Oberflaeche zeigt: wer funkt, ohne dazuzugehoeren."""
        self.verwaiste_aufraeumen()
        jetzt = time.time()
        with self.lock:
            eintraege = sorted(self._verwaist.items(),
                               key=lambda kv: kv[1]["zuletzt"], reverse=True)
            eintraege = [(k, dict(v)) for k, v in eintraege]
        return [{"hmid": h, "vor_sek": max(0.0, jetzt - e["zuletzt"]),
                 "anzahl": e["anzahl"], "seit_sek": max(0.0, jetzt - e["zuerst"])}
                for h, e in eintraege]

    def spuren_raeumen(self, ruf_adresse, kennung):
        """Nach dem Anlernen aufraeumen, was das Geraet hinterlassen hat.

        ⚠️ Zwei Spuren bleiben sonst liegen und widersprechen dem, was gerade
        geschehen ist (beides am 19.08.2026 am Aufbau gesehen):

        1. **Der Anlernwunsch.** Das Geraet rief unter einer Ruf-Adresse; nach
           dem Anlernen heisst es anders. Blieb der Eintrag stehen, stand
           dasselbe Geraet gleichzeitig als angelernt UND als „will angelernt
           werden" da — mit einem Hinweis, der ins Leere zeigt. Dirks Wort
           dafuer: „Paradox?"
        2. **Die alte Funkadresse.** Wurde dieselbe Kennung vorher
           ausgeschlossen, funkte sie unter der alten Adresse vielleicht noch
           und stand deshalb unter „funkt noch, gehoert nicht dazu". Nach dem
           Werksreset ist diese Adresse Geschichte.
        """
        with self.lock:
            self._fremde.pop((ruf_adresse or "").lower(), None)
            alte = self._ehemals.pop((kennung or "").upper(), None)
            if alte:
                self._verwaist.pop(alte, None)

    def _fremde_frist(self, e):
        """Wie lange ein Anlernruf nachhallt — aus seiner eigenen Kadenz.

        ⚠️ Ein Anlernwunsch ist ein WIEDERHOLTER Ruf, kein Zustand. Wer den
        Knopf gedrueckt hat und aufgibt — oder sich anderswo anlernt —, hoert
        auf zu rufen. Bleibt der Eintrag trotzdem stehen, zeigt die Liste
        Geraete an, die niemand mehr anbietet, und der Anwender wartet auf
        etwas, das nicht mehr kommt (Dirk, 19.08.2026).

        Wie lange „ausgeblieben" dauert, misst der Eintrag selbst: der
        mittlere Abstand seiner bisherigen Rufe, mal FREMDE_FEHLRUFE. Ein
        langsam rufendes Geraet bekommt dadurch von allein mehr Zeit als ein
        schnelles — ohne dass wir eine Ruf-Kadenz behaupten muessten, die wir
        nicht gemessen haben. Beim ersten Ruf gibt es keinen Abstand; dann
        gilt die Grundfrist.
        """
        anzahl = e.get("anzahl", 1)
        spanne = e["zuletzt"] - e["zuerst"]
        if anzahl < 2 or spanne <= 0:
            return self.FREMDE_GRUNDFRIST
        abstand = spanne / (anzahl - 1)
        return max(self.FREMDE_GRUNDFRIST,
                   min(self.FREMDE_HOECHSTFRIST, self.FREMDE_FEHLRUFE * abstand))

    def fremde_aufraeumen(self):
        """Verstummte Anlernrufe wegwerfen — und sagen, dass sie weg sind.

        Frueher wurden zu alte Eintraege beim Anzeigen nur uebersprungen: sie
        blieben im Speicher, zaehlten gegen FREMDE_MAX und verdraengten
        aktuelle Rufe. Jetzt werden sie wirklich entfernt, und das Wegfallen
        steht unter „Zuletzt geschehen" — sonst ist die Liste ohne Erklaerung
        leer, und das sieht aus wie ein Fehler.
        """
        jetzt = time.time()
        weg = []
        with self.lock:
            for hmid, e in list(self._fremde.items()):
                if jetzt - e["zuletzt"] > self._fremde_frist(e):
                    weg.append((hmid, e))
                    del self._fremde[hmid]
        for hmid, e in weg:
            typ = self._typname(e["devtype"])
            self._log("<<", f"Anlernwunsch {hmid} verstummt — aus der Liste")
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("info", f"{typ or 'Geraet'} {hmid} ruft nicht mehr — "
                              f"aus den Anlernwuenschen entfernt")
        return len(weg)

    def _typname(self, devtype):
        """Der Name aus dem Katalog, wenn er dort steht — sonst nichts.

        Erfunden wird kein Typname; ein leerer ist ehrlicher als ein falscher.
        """
        try:
            return self.t.label_of(devtype) or ""
        except Exception:                            # noqa: BLE001
            return ""

    def anlernwuensche_liste(self):
        """Wer gerade angelernt werden WILL — der Ruf, nicht der Bestand.

        ⚠️ Der Name jedes Eintrags traegt die Handlungsanweisung. Eine
        Zentrale von eq-3 kann ein HmIP-Geraet ohne Zutun aufnehmen, weil sie
        an dessen Schluessel kommt; wir koennen das nicht (nachgewiesen am
        12.08.2026 — der Riegel ist der Hauptschluessel im Silizium). Ohne
        Schluessel bleibt es beim Zusehen, und genau das soll der Anwender
        lesen, anstatt vergeblich auf ein Geraet zu warten.

        Der Hinweis nennt BEIDE Wege, den Schluessel beizubringen — die
        QCCU-Oberflaeche und das Feld `key` am Anlern-Aufruf. Nur einen zu
        nennen hiesse, den anderen zu verschweigen; wer aus Home Assistant
        heraus arbeitet, wuerde in eine Oberflaeche geschickt, die er nicht
        braucht.

        ⚠️ Die Liste lebt im Arbeitsspeicher: ein Neustart der Zentrale leert
        sie. Das ist beabsichtigt — ein Anlernwunsch ist eine Momentaufnahme
        („hier wurde gerade der Knopf gedrueckt"), keine Bestandsliste. Wer
        verstummt, faellt heraus (`fremde_aufraeumen`).

        Zwei Namensfamilien in einem Eintrag, mit Absicht: `id/address/name/
        type/interface` ist das, was die Haussteuerung liest (Feldnamen einer
        eq-3-Zentrale, nicht unsere Wahl); `hmid/label/devtype/hinweis/
        vor_sek/anzahl` ist das, was die eigene Oberflaeche zeigt.
        """
        self.fremde_aufraeumen()
        jetzt = time.time()
        hat_schluessel = bool(self.pair_key)
        # ⚠️ Der Text sagt, was ZU TUN ist — er behauptet nicht, dass etwas
        # fehlt. „Schluessel vom Aufkleber fehlt" las sich wie ein Vorwurf und
        # stand obendrein noch da, wenn gerade erfolgreich angelernt worden war
        # (der Schluessel wird nach dem Anlernen verworfen): ein Geraet stand
        # als angelernt UND als „Schluessel fehlt" in derselben Liste
        # (Dirk, 19.08.2026: „Paradox?"). Das Anlernen selbst laeuft in der
        # QCCU-Oberflaeche — nicht aus Bequemlichkeit, sondern weil ein
        # HmIP-Geraet ohne seinen Aufkleber-Schluessel nicht anlernbar ist und
        # die Haussteuerung kein Feld dafuer hat.
        hinweis = ("will angelernt werden — Schluessel liegt vor, "
                   "Anlernfenster in QCCU oeffnen" if hat_schluessel
                   else "will angelernt werden — dafuer den 26-stelligen "
                        "Schluessel vom Aufkleber in der QCCU-Oberflaeche "
                        "eintragen und dort anlernen")
        out = []
        with self.lock:
            eintraege = sorted(self._fremde.items(),
                               key=lambda kv: kv[1]["zuletzt"], reverse=True)
            eintraege = [(k, dict(v)) for k, v in eintraege]
        for hmid, e in eintraege:
            typ = self._typname(e["devtype"])
            out.append({
                "id": hmid,
                "address": hmid.upper(),
                "name": f"{typ or 'Unbekanntes Geraet'} {hmid} — {hinweis}",
                "type": typ,
                "interface": getattr(self.qccu, "interface_name", "HmIP-RF"),
                # ab hier: nur fuer die eigene Oberflaeche
                "hmid": hmid,
                "label": typ,
                "devtype": e["devtype"],
                "hinweis": hinweis,
                "vor_sek": max(0.0, jetzt - e["zuletzt"]),
                "anzahl": e.get("anzahl", 1),
            })
        return out

    def on_install(self, on, seconds):
        """Anlernbetrieb ueber die Zentralen-Schnittstelle (`setInstallMode`)."""
        if not on:
            self.stop_pairing()
            if self.verbose:
                print("  Anlernbetrieb zu")
            return
        if self.pair_key:
            self.pair_until = time.time() + float(seconds)
            self.pair_last = f"Fenster offen ({int(seconds)} s) ueber setInstallMode"
        else:
            self.pair_last = ("Anlernbetrieb angefordert, aber kein Schluessel "
                              "hinterlegt — Aufkleber oder LocalKey im "
                              "Webfrontend eintragen")
            if self.verbose:
                print(f"  ! {self.pair_last}")
