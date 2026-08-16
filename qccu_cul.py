#!/usr/bin/env python3
"""CUL-Zugang ueber TCP fuer die BidCoS/AskSin-Seite."""
import re
import socket
import threading
import time

RE_SENDEN = re.compile(r"^As([0-9A-Fa-f]{4,})$")
RE_VERSION = re.compile(r"^V\b")

CMDS = "? (? is unknown) Use one of A V X T"

RE_FHTID = re.compile(r"^T01([0-9A-Fa-f]{4})?$")


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

        m = RE_FHTID.match(zeile)
        if m:
            if m.group(1):
                self.fhtid = m.group(1).upper()
            return self._an_klient(sock, self.fhtid)

        m = RE_SENDEN.match(zeile)
        if m:
            self.zaehler["tx"] += 1
            self._an_stick("As" + m.group(1).upper())
            return

        self.zaehler["verworfen"] += 1
        if self.verbose:
            print(f"  CUL-Zugang: '{zeile}' nicht weitergereicht")

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
        return {"port": self.port, "klienten": n, **self.zaehler}
