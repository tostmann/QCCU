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
ACK = re.compile(r"\bct=4\b.*\bar=1\b")
ACKSEQ = re.compile(r"\back=([0-9A-Fa-f]{8})")
FLAGS = re.compile(r"\bf=([0-9A-Fa-f]{2})\b")
SEQ = re.compile(r"\bsn=([0-9A-Fa-f]{8})")
CNT = re.compile(r"^Pm\s+rx=(\d+)\s+ok=(\d+)\s+mic=(\d+)\s+dup=(\d+)"
                 r"\s+acks=(\d+)\s+fwd=(\d+)\s+tx=(\d+)\s+txerr=(\d+)")
RAW = re.compile(r"^P([0-9A-Fa-f]{4,})$")
STICKSEQ = re.compile(r"^Pm (?:ein|aus) .*\bsn=([0-9A-Fa-f]{8})")
BUDGET = re.compile(r"^Pm budget=(\d) credit=(\d+)/(\d+) lovf=(\d+)")
TX_OK = re.compile(r"^Pm tx ok")
TX_NO = re.compile(r"^(?:Pm (?:ERR|NUR-LESEN)|\?\s*$)")

CNT_KEYS = ("rx", "ok", "mic", "dup", "acks", "fwd", "tx", "txerr")

FT_ANSWER = 2
FT_STATUS = 5
SDT_BINARY = 8
APP_RESP_REQ = 0x80
RXF_FOR_US = 0x01

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
        self.by_hmid = {}
        self.appseq = {}
        self.devseq = {}
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = False
        self.own_addr = None
        self.netzschluessel_fehlt = False
        self._acked = {}

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
        """Funkadresse und Zentralen-Adresse einander zuordnen."""
        with self.lock:
            self.by_hmid[hmid.lower()] = ccu_address.upper()
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
        """Die Zustandszeile des Sticks holen (`m` -> `Pm ein …`)."""
        self.ser.reset_input_buffer()
        self.ser.write(b"m\r\n")
        self.ser.flush()
        ende = time.time() + 1.5
        while time.time() < ende:
            try:
                z = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:
                return None
            if z.startswith("Pm ein") or z.startswith("Pm aus"):
                return z
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

    def _loop(self):
        while not self._stop:
            try:
                line = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:
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
            self.counters = dict(zip(CNT_KEYS, (int(x) for x in mc.groups())))
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

        if int(ct) == 4 and ACK.search(line):
            ev = self._acked.get(src.lower())
            if ev:
                ms = ACKSEQ.search(line)
                self._log("##", f"Quittung von {src.lower()} fuer sn="
                                f"{ms.group(1) if ms else '?'}")
                ev.set()
            return

        if (self._pair_expect and src.lower() == self._pair_expect
                and int(sec) >= 1):
            self._pair_ok.set()

        if int(ct) == CT_ICMP:
            ms = SEQ.search(line)
            self._icmp(payhex, src.lower(), dst.lower(),
                       ms.group(1) if ms else None)
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

    def _emit(self, addr, channel, sdt, value, flags):
        """Einen Eintrag melden. Gedeutet wird nur, was belegt ist."""
        if sdt == SDT_BINARY:
            state = (value[0] & 0x40) != 0
            self.qccu.set_value_internal(addr, channel, "STATE", state)
            if self.verbose:
                print(f"  <- {addr}:{channel} STATE={state}")
            return

        raw = "".join(f"{x:02X}" for x in value)
        self.qccu.set_value_internal(addr, channel, f"RAW_SDT{sdt}", raw)
        if self.verbose:
            print(f"  <- {addr}:{channel} SDT{sdt}={raw} (ungedeutet)")

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
            except Exception as ex:
                self._log("##", f"Schreiben scheiterte: {ex}")
                job.verdict = "err"
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
        (self._ansq if kind == "answer" else self._txq).put(job)
        return job

    def _answer(self, hmid, appseq):
        """ANSWER auf einen Frame, der eine Antwort angefordert hat."""
        if not self.answer_enabled:
            self._log("##", f"ANTWORT UNTERDRUECKT appSeq=0x{appseq:02X}")
            return
        self._submit(f"ms{hmid.upper()}02{appseq:02X}00", "answer",
                     time.time() + self.answer_delay)

    def _icmp(self, payhex, src, dst, sn):
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

        if t == ICMP_NEIGHBOR_ADVERTISEMENT and to_group and sn:
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
                "icmp": dict(self.icmp_seen),
                "devices": {h: a for h, a in self.by_hmid.items()}}

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
        lk = (local_key or "").strip().replace(" ", "").replace("-", "")
        st = (sticker or "").strip().replace("-", "").upper()

        if lk:
            if len(lk) != 32:
                return f"LocalKey hat {len(lk)} statt 32 Hexziffern"
            try:
                key = bytes.fromhex(lk)
            except ValueError:
                return "LocalKey ist keine gueltige Hexzahl"
            src = "LocalKey"
        elif st:
            if len(st) != 26:
                return (f"Aufkleber hat {len(st)} statt 26 Zeichen "
                        f"(Form ABCEF-GHJKL-MNPQR-STUWX-YZ2345)")
            try:
                key = sticker_to_local_key(st)
            except ValueError as ex:
                return f"Aufkleber enthaelt ein unzulaessiges Zeichen ({ex})"
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

        self._submit("As" + bytes([len(acc)]).hex().upper() + acc.hex().upper(), "cmd")
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

        time.sleep(2.5)
        self._submit("mT31f00002" + "01" + newa.hex() + "0000004017", "cmd")
        time.sleep(0.3)
        self._submit("ms" + newa.hex().upper() + "C1050101"
                     + newa.hex().upper() + "030000", "cmd")
        time.sleep(0.3)
        self._submit("ms" + newa.hex().upper() + "C1070301"
                     + newa.hex().upper() + "010000", "cmd")

        ccu_addr = sgtin.hex()[-14:].upper()
        self.qccu.add_device(ccu_addr, devtype)
        self.bind(newa.hex(), ccu_addr)
        self.pair_last = f"{ccu_addr} angelernt als {newa.hex()} (Typ {devtype})"
        self._log("##", f"ANLERNEN fertig {ccu_addr} -> {newa.hex()}")
        if self.verbose:
            print(f"  Anlernen abgeschlossen: {ccu_addr} -> {newa.hex()}")

        nxt = (int(self.pair_next_addr, 16) + 1) & 0xFFFFFF
        self.pair_next_addr = f"{nxt:06x}"
        if hasattr(self.qccu, "pair_next_addr"):
            self.qccu.pair_next_addr = self.pair_next_addr
            self.qccu.save_store()
        self.pair_key = None
        self.pair_until = 0.0
        self._pair_busy = False

    # -- Posteingang: gehoerte, aber unbekannte Geraete -------------------
    FREMDE_HALTBARKEIT = 3600.0   # eine Stunde; danach war es wohl nichts
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

    def inbox_liste(self):
        """Der Posteingang, wie ihn eine Zentrale ausweist.

        ⚠️ Der Name traegt die Handlungsanweisung. Eine Zentrale von eq-3 kann
        ein HmIP-Geraet ohne Zutun aufnehmen, weil sie an dessen Schluessel
        kommt; wir koennen das nicht (nachgewiesen am 12.08.2026 — der Riegel
        ist der Hauptschluessel im Silizium). Ohne Schluessel bleibt es beim
        Zusehen, und genau das soll der Anwender lesen, anstatt vergeblich auf
        ein Geraet zu warten.

        Der Hinweis nennt BEIDE Wege, den Schluessel beizubringen — die
        Oberflaeche und das Feld `key` am Anlern-Aufruf. Nur einen zu nennen
        hiesse, den anderen zu verschweigen; wer aus Home Assistant heraus
        arbeitet, wuerde in eine Oberflaeche geschickt, die er nicht braucht.

        ⚠️ Der Posteingang lebt im Arbeitsspeicher: ein Neustart der Zentrale
        leert ihn. Das ist beabsichtigt — ein Anlernwunsch ist eine Momentaufnahme
        („hier wurde gerade der Knopf gedrueckt"), keine Bestandsliste."""
        jetzt = time.time()
        hat_schluessel = bool(self.pair_key)
        hinweis = ("bereit zum Anlernen" if hat_schluessel
                   else "Schluessel vom Aufkleber fehlt — in der "
                        "QCCU-Oberflaeche eintragen oder am Anlern-Aufruf "
                        "im Feld `key` mitgeben")
        out = []
        with self.lock:
            eintraege = sorted(self._fremde.items(),
                               key=lambda kv: kv[1]["zuletzt"], reverse=True)
        for hmid, e in eintraege:
            if jetzt - e["zuletzt"] > self.FREMDE_HALTBARKEIT:
                continue
            # Den Typnamen aus dem Katalog, wenn er dort steht — sonst die
            # blosse Nummer. Erfunden wird nichts.
            try:
                typ = self.t.label_of(e["devtype"])
            except Exception:                        # noqa: BLE001
                typ = ""
            out.append({
                "id": hmid,
                "address": hmid.upper(),
                "name": f"{typ or 'Unbekanntes Geraet'} {hmid} — {hinweis}",
                "type": typ,
                "interface": getattr(self.qccu, "interface_name", "HmIP-RF"),
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
