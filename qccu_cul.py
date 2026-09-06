#!/usr/bin/env python3
"""CUL-Zugang ueber TCP fuer die BidCoS/AskSin-Seite."""
import re
import socket
import threading
import time

RE_SENDEN = re.compile(r"^As([0-9A-Fa-f]{4,})$")
RE_VERSION = re.compile(r"^V\b")
RE_ASKSIN = re.compile(r"^A[rRx]$")
RE_LAUFZEIT = re.compile(r"^t$")
RE_FHTID = re.compile(r"^T01([0-9A-Fa-f]{4})?$")
RE_FHTPUFFER = re.compile(r"^T03")
RE_MELDEFORM = re.compile(r"^X([0-9A-Fa-f]{2})?$")
RE_CCREG = re.compile(r"^C([0-9A-Fa-f]{2})$")

# ⚠️ Nur der Konfigurationsraum. Ab 0x30 liest der Stick mit gesetztem
# Burst-Bit (`cc1101_read_status`), und 0x3F ist der RX-FIFO: ein `C3F` von
# einem Klienten zoege ein Byte aus einem gerade laufenden Empfang — auf dem
# Funkteil, das die HmIP-Seite mitbenutzt. Dieselbe Grenze zieht q-culfw fuer
# seinen Schreibbefehl `W`. FHEM braucht ohnehin nur 0D, 0E, 0F, 10, 1B, 1D.
CCREG_HOECHSTE = 0x2E

# Antwort auf `?`. FHEM prueft sie gegen `.*Use one of( .)*` — je Eintrag ein
# Leerzeichen und GENAU EIN Zeichen (`00_CUL.pm`, %gets „cmds"); mehrbuchstabige
# Namen wie `Ar` lassen die Abfrage in ihren Zeitablauf laufen. Aus dieser Zeile
# liest FHEM das `A` heraus, ohne das es `attr <cul> rfmode HomeMatic`
# verweigert. Bewusst NICHT genannt: `Z` (Moritz) und `b` (wMBus) — FHEM
# schickt sonst deren Abschaltbefehle, die es hier nicht gibt — und `B`, der
# den Stick in den Bootlader schickt.
CMDS = "? (? is unknown) Use one of A C T V X t"

# culfw zaehlt in Ticks von 8 ms; FHEM rechnet `hex($msg)/125` in Sekunden um
# (`00_CUL.pm`, CUL_Get „uptime").
TICKS_JE_SEKUNDE = 125


class CulDienst:
    """TCP-Zugang im culfw-Stil."""

    def __init__(self, radio, version="QCCU", bind="0.0.0.0", port=2000,
                 verbose=False):
        self.radio = radio
        self.version = version
        self.bind = bind
        self.port = port
        self.verbose = verbose
        self.klienten = []
        self.lock = threading.Lock()
        self.zaehler = {"rx": 0, "tx": 0, "verworfen": 0}
        self.fhtid = "0000"
        # Die Meldeform, die FHEM beim Anmelden setzt (`X21`). Nur ein
        # Rueckfallwert fuer den Fall, dass der Stick gerade nicht antwortet:
        # gefragt wird sonst er selbst, und dort steht dieselbe 0x21.
        self.meldeform = 0x21
        self.seit = time.time()
        self.letzte_unbekannt = None
        # Zuletzt gelesene Funkregister. Sie aendern sich im Betrieb nicht —
        # Schreibbefehle laesst dieser Zugang nicht durch —, taugen also als
        # Auskunft, wenn der Stick gerade nicht antwortet. Besser als keine
        # Antwort: FHEM legt bei Stille auf (siehe `_stick_frage`).
        self.ccreg = {}
        self._srv = None

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.bind, self.port))
        self._srv.listen(5)
        threading.Thread(target=self._annehmen, daemon=True).start()
        self.empfang_an()
        return self

    def empfang_an(self):
        """BidCoS-Empfang am Stick einschalten."""
        self._an_stick("Ar")

    def radio_wechsel(self, radio):
        """Auf einen neu aufgebauten Funkpfad umhängen."""
        self.radio = radio
        self.empfang_an()

    def stop(self):
        try:
            self._srv.close()
        except Exception:
            pass
        with self.lock:
            for s in self.klienten:
                try:
                    s.close()
                except Exception:
                    pass
            self.klienten.clear()

    def _annehmen(self):
        while True:
            try:
                sock, adr = self._srv.accept()
            except Exception:
                return
            with self.lock:
                self.klienten.append(sock)
            if self.verbose:
                print(f"  CUL-Zugang: {adr[0]} verbunden")
            threading.Thread(target=self._bedienen, args=(sock, adr),
                             daemon=True).start()

    def _bedienen(self, sock, adr):
        rest = b""
        try:
            sock.settimeout(None)
            while True:
                daten = sock.recv(4096)
                if not daten:
                    break
                rest += daten
                while b"\n" in rest:
                    zeile, rest = rest.split(b"\n", 1)
                    self._befehl(sock, zeile.decode("ascii", "replace").strip())
        except Exception:
            pass
        finally:
            with self.lock:
                if sock in self.klienten:
                    self.klienten.remove(sock)
            try:
                sock.close()
            except Exception:
                pass
            if self.verbose:
                print(f"  CUL-Zugang: {adr[0]} getrennt")

    def _befehl(self, sock, zeile):
        if not zeile:
            return
        if RE_VERSION.match(zeile):
            return self._an_klient(sock, self._version_zeile())
        if zeile == "?":
            return self._an_klient(sock, CMDS)

        if RE_LAUFZEIT.match(zeile):
            return self._an_klient(sock, self._laufzeit_zeile())

        m = RE_FHTID.match(zeile)
        if m:
            if m.group(1):
                self.fhtid = m.group(1).upper()
            return self._an_klient(sock, self.fhtid)

        if RE_FHTPUFFER.match(zeile):
            # `T03` fragt den freien Platz im FHT-Sendepuffer (culfw `fht.c`,
            # fhtsend: `DH2(fht_bufspace())`). Diese Zentrale kann kein FHT und
            # hat keinen Puffer — `00` ist die ehrliche Antwort und haelt FHEM
            # davon ab, hier FHT-Telegramme abzuladen. Ohne sie lief
            # `get <cul> fhtbuf` in den Zeitablauf.
            return self._an_klient(sock, "00")

        m = RE_MELDEFORM.match(zeile)
        if m:
            if m.group(1):
                # `X21` beim Anmelden („X21 is needed for RSSI reporting",
                # `00_CUL.pm`). Der Stick haengt den Pegel ohnehin an jede
                # Zeile; weitergereicht wird es trotzdem, damit beide Seiten
                # dasselbe wissen. Eine Antwort erwartet FHEM darauf nicht.
                self.meldeform = int(m.group(1), 16)
                self._an_stick(zeile)
                return
            return self._an_klient(sock, self._kredit_zeile())

        m = RE_CCREG.match(zeile)
        if m and int(m.group(1), 16) <= CCREG_HOECHSTE:
            antwort = self._ccreg_zeile(m.group(1).upper())
            if antwort:
                return self._an_klient(sock, antwort)
            # ⚠️ Was FHEM aus dem Schweigen macht, haengt am Weg: `get ccconf`
            # gibt bloss den Zeitablauf zurueck (`00_CUL.pm`, CUL_Get:
            # `return $err if($err)` im ccconf-Zweig), `get raw C0D` dagegen
            # landet im allgemeinen Zweig und ruft `DevIo_Disconnected` —
            # Verbindung weg, Neuanmeldung. Beides gehoert ins Protokoll,
            # sonst sucht jemand den Grund im Netz.
            print(f"  CUL-Zugang: Register {m.group(1)} nicht lesbar — "
                  f"`get ccconf` meldet einen Zeitablauf, `get raw` wirft "
                  f"die Verbindung weg")
            return

        if RE_ASKSIN.match(zeile):
            # `Ar` kommt bei JEDER Anmeldung (`initString` = "X21\nAr"), `Ax`
            # beim Wechsel des rfmode. Den Empfang schaltet QCCU selbst — und
            # darf ihn auf Zuruf eines Klienten auch nicht abschalten: am
            # selben Stick haengt die HmIP-Seite. Also `Ar` bestaetigen,
            # `Ax` bewusst uebergehen.
            if zeile[1] != "x":
                self.empfang_an()
            return

        m = RE_SENDEN.match(zeile)
        if m:
            self.zaehler["tx"] += 1
            self._an_stick("As" + m.group(1).upper())
            return

        self.zaehler["verworfen"] += 1
        self.letzte_unbekannt = zeile
        if self.verbose:
            print(f"  CUL-Zugang: '{zeile}' nicht weitergereicht")
        # ⚠️ Quittieren, nicht schweigen. `get <cul> raw <x>` nimmt jede
        # Antwort (`00_CUL.pm`, %gets: Muster `.*`), aber KEINE Antwort kostet
        # die Verbindung: `CUL_Get` ruft dann `DevIo_Disconnected`. Der Stick
        # selbst gibt auf Unbekanntes `? ` aus (q-culfw `main.c`, default im
        # Kommandoschalter) — dasselbe hier, damit ein Klient am Zugang
        # sieht, was er am Stick saehe.
        self._an_klient(sock, "? ")

    def _version_zeile(self):
        """Fassung im culfw-Format: `V <fassung> <name>`."""
        fw = None
        try:
            fw = self.radio.firmware_version()
        except Exception:
            pass
        if fw:
            teile = fw.split()
            if len(teile) == 2:
                return f"V {teile[1]} {teile[0]}"
            return f"V {fw}"
        return f"V 0.0.0 {self.version}"

    def _laufzeit_zeile(self):
        """`t` — Laufzeit als acht Hexziffern, in Ticks von 8 ms.

        Der Stick kennt `t` nicht (q-culfw `main.c` hat kein 't' in seinem
        Schalter), also antwortet die Zentrale mit IHRER Laufzeit. Das ist auch
        die Zahl, die an dieser Stelle gesucht wird: wie lange laeuft QCCU.
        """
        ticks = int((time.time() - self.seit) * TICKS_JE_SEKUNDE) & 0xFFFFFFFF
        return f"{ticks:08X}"

    def _kredit_zeile(self):
        """`X` ohne Argument — Meldeform und Restkonto, wie culfw.

        culfw gibt `DH2(TX_REPORT)` und `DU(credit_10ms, 5)` aus
        (`rf_receive.c`, set_txreport), q-culfw dasselbe mit einem Leerzeichen
        dazwischen (`main.c`, case 'X'). FHEM legt die Zahl als Reading
        `credit10ms` ab (`^.. *(\\d*)`, `00_CUL.pm`) — nicht mehr: weder
        `00_CUL.pm` noch `10_CUL_HM.pm` rechnen damit, FHEMs eigene Bremse ist
        `XMIT_TIME`/`NR_CMD_LAST_H`. Die Zahl ist also eine Auskunft ueber das
        Konto DES STICKS, das sich beide Funkfamilien teilen.

        Geantwortet wird immer, auch ohne Auskunft: bleibt die Antwort aus,
        legt FHEM auf (`DevIo_Disconnected`) statt bloss in den Zeitablauf zu
        laufen. Dann lieber die Meldeform allein — das Muster erlaubt eine
        leere Zahl, und ein leeres Reading luegt nicht.
        """
        m = self._stick_frage("X", r"^[0-9A-Fa-f]{2} +\d+$")
        if m:
            return (m.string or m.group(0)).strip()
        # Antwortet der Stick gerade nicht, taugt der letzte `Pm budget=`-
        # Bericht noch: dieselbe Zahl, nur aelter. Er steht allerdings nur,
        # wenn jemand vorher `mX` abgesetzt hat — im Regelbetrieb tut das
        # niemand, der Rueckfall ist also der seltene Fall.
        b = getattr(self.radio, "budget", None)
        if isinstance(b, dict) and b.get("max"):
            rest = b["credit"] if b.get("on") else b["max"]
            return f"{self.meldeform:02X} {rest}"
        return f"{self.meldeform:02X}"

    def _ccreg_zeile(self, reg):
        """`C<hh>` — ein CC1101-Register in der Schreibweise von culfw.

        Der Stick antwortet knapp (`C0D=21`, q-culfw `main.c`), culfw dagegen
        `C0D = 21 / 33`. Und genau das braucht FHEM: `get <cul> ccconf` liest
        die Register 0D, 0E, 0F, 10, 1B und 1D, prueft `^C.* = .*` und nimmt
        das FUENFTE durch Leerzeichen getrennte Feld — den DEZIMALwert
        (`00_CUL.pm`, CUL_Get). Ohne diese Umschrift passt die Antwort des
        Sticks nicht auf das Muster, und die Abfrage laeuft in ihren
        Zeitablauf.
        """
        m = self._stick_frage(f"C{reg}", rf"(?i)^C{reg}=([0-9A-Fa-f]{{2}})$")
        if m:
            self.ccreg[reg] = int(m.group(1), 16)
        elif reg not in self.ccreg:
            return None
        wert = self.ccreg[reg]
        return f"C{reg} = {wert:02X} / {wert:2d}"

    def _stick_frage(self, cmd, muster):
        """Eine Frage an den Stick, genau EIN Versuch.

        FHEM wartet auf eine Antwort drei Sekunden (`CUL_ReadAnswer`); der
        uebliche Weg `_ask` versucht es dreimal und braeuchte laenger als das.
        Und solange hier gewartet wird, liest dieser Klientenfaden nichts —
        ein `As…`, das gleich danach kommt, wuerde warten. Darum ein Versuch.
        """
        try:
            return self.radio._ask(cmd, muster, tries=1)
        except Exception as ex:                          # noqa: BLE001
            if self.verbose:
                print(f"  CUL-Zugang: '{cmd}' scheiterte: {ex}")
            return None

    def _an_stick(self, cmd):
        """Ueber die Warteschlange des Funkpfads senden."""
        try:
            self.radio._submit(cmd, "ask")
        except Exception as ex:
            if self.verbose:
                print(f"  CUL-Zugang: senden scheiterte: {ex}")

    def _an_klient(self, sock, text):
        try:
            sock.sendall((text + "\r\n").encode("ascii", "replace"))
        except Exception:
            pass

    def a_zeile(self, zeile):
        """Eine `A`-Zeile des Sticks an alle Klienten weitergeben."""
        self.zaehler["rx"] += 1
        with self.lock:
            ziele = list(self.klienten)
        for s in ziele:
            self._an_klient(s, zeile)

    def zustand(self):
        with self.lock:
            n = len(self.klienten)
        return {"port": self.port, "klienten": n, **self.zaehler,
                "letzte_unbekannt": self.letzte_unbekannt}
