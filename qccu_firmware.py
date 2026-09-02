#!/usr/bin/env python3
"""Stick-Firmware: Zustand feststellen und einspielen."""
import os
import re
import subprocess
import time

MCU = "atmega32u4"
USB_BUS = "/dev/bus/usb"


def als_erweiterung():
    """Laeuft QCCU als Home-Assistant-Erweiterung?

    ⚠️ Das entscheidet, welchen RAT die Oberflaeche geben darf. Wer die
    Erweiterung benutzt, hat keine `docker run`-Zeile — ein Hinweis wie „mit
    -v /dev/bus/usb starten" schickt ihn zu einem Schalter, den es bei ihm
    nicht gibt. Erkannt am `SUPERVISOR_TOKEN`: das setzt der Supervisor in
    jedem Erweiterungs-Behaelter, und nur dort."""
    return bool(os.environ.get("SUPERVISOR_TOKEN"))

ST_RUNNING = "laeuft"
ST_DFU = "bootlader"
ST_ABSENT = "kein_stick"
ST_NOACCESS = "kein_zugang"


def hex_version(path):
    """Fassung aus einer .hex lesen — der Versionsstring steht im Abbild."""
    try:
        data = bytearray()
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line.startswith(":"):
                    continue
                n = int(line[1:3], 16)
                a = int(line[3:7], 16)
                t = int(line[7:9], 16)
                if t != 0:
                    continue
                if len(data) < a + n:
                    data.extend(b"\xff" * (a + n - len(data)))
                data[a:a + n] = bytes.fromhex(line[9:9 + 2 * n])
        m = re.search(rb"q-culfw [0-9]+\.[0-9]+\.[0-9]+", bytes(data))
        return m.group().decode() if m else None
    except Exception:
        return None


#
# Erkannt wird ausschliesslich der eigene Name. Ein CUL mit anderer Firmware
# (culfw meldet sich als `busware.de_CUL868_<Seriennummer>`) wird ABSICHTLICH
# nicht angefasst: er koennte an derselben Maschine fuer FS20 oder anderes in
# Betrieb sein, und ein ungefragt belegter Port oder gar ein ungefragtes
# Einspielen waere ein Uebergriff.
#
# Der Weg fuer so einen Stick fuehrt ueber den Bootlader: abziehen, Taster
# halten, einstecken. Diese Handlung IST die Zustimmung des Betreibers.
#
STICK_MUSTER = ("busware.de_q-culfw",)
BY_ID = "/dev/serial/by-id"


def stick_serial(pfad):
    """Seriennummer aus dem by-id-Namen — None bei Firmware ohne."""
    m = re.search(r"q-culfw_([0-9A-Za-z]+)-if", os.path.basename(pfad or ""))
    return m.group(1) if m else None


def stick_suchen(bekannt=None):
    """Serielle Schnittstelle des Sticks finden.

    `bekannt` ist die Seriennummer des Sticks, mit dem diese Anlage laeuft.
    Ist sie gesetzt, kommt nur genau dieser in Frage — ein zweiter Stick am
    selben Rechner wird dann nicht versehentlich uebernommen.
    """
    try:
        eintraege = sorted(os.listdir(BY_ID))
    except Exception:
        return None, []
    treffer = [os.path.join(BY_ID, e) for e in eintraege
               if any(m in e for m in STICK_MUSTER)]
    if bekannt:
        passend = [t for t in treffer if stick_serial(t) == bekannt]
        return (passend[0] if len(passend) == 1 else None), treffer
    return (treffer[0] if len(treffer) == 1 else None), treffer


def usb_access():
    return os.path.isdir(USB_BUS)


def dfu_present(timeout=4):
    """Meldet sich ein Bootlader?"""
    try:
        r = subprocess.run(["dfu-programmer", MCU, "get", "bootloader-version"],
                           capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def stick_version(radio):
    """Fassung des laufenden Sticks ueber seine eigene Auskunft (`V`)."""
    if radio is None:
        return None
    try:
        ant = radio.firmware_version()
    except Exception:
        return None
    if not ant:
        return None
    m = re.search(r"(?:q-culfw|hmip-mac-avr) [0-9]+\.[0-9]+\.[0-9]+", ant)
    return m.group() if m else ant


def _zahlen(text):
    """`q-culfw 2.0.89` -> (2, 0, 89); None, wenn keine Fassung erkennbar."""
    if not text:
        return None
    letzte = text.split()[-1]
    teile = letzte.split(".")
    if not (2 <= len(teile) <= 4):
        return None
    try:
        return tuple(int(t) for t in teile)
    except ValueError:
        return None


def _ist_neuer(mitgeliefert, installiert):
    """Ist die mitgelieferte Fassung NEUER als die installierte?

    True / False / None (nicht vergleichbar). Verglichen wird zahlenweise,
    nicht als Zeichenkette: „2.0.9" ist NEUER als „2.0.10", wenn man Text
    vergleicht, und aelter, wenn man rechnet. Ausserdem muss der Name vor der
    Nummer uebereinstimmen — eine a-culfw 1.29.1 ist gegen eine q-culfw 2.0.89
    nicht „aelter", sondern etwas anderes.
    """
    a, b = _zahlen(mitgeliefert), _zahlen(installiert)
    if a is None or b is None:
        return None
    if mitgeliefert.split()[0] != installiert.split()[0]:
        return None
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def status(hex_path, radio=None, serial_path=None):
    """Der vollstaendige Zustand fuer die Oberflaeche."""
    out = {
        "mitgeliefert": hex_version(hex_path),
        "installiert": None,
        "zustand": ST_NOACCESS,
        "aktualisierbar": False,
        "hinweis": "",
    }
    if radio is not None:
        out["installiert"] = stick_version(radio)
        out["zustand"] = ST_RUNNING
        mit, ist = out["mitgeliefert"], out["installiert"]
        if mit and ist and mit != ist:
            # ⚠️ NUR vorwaerts. Bis zum 02.09.2026 stand hier ein blosses
            # „ungleich" — damit bot die Oberflaeche einem Stick mit
            # NEUERER Firmware den Rueckschritt auf die mitgelieferte an,
            # freundlich beschriftet als „Aktualisieren auf 2.0.72". Ein Klick
            # nahm dem Stick das Kommando `mb`, und danach ging jeder
            # Stellbefehl an ein Batteriegeraet ohne Vorlauf hinaus.
            neuer = _ist_neuer(mit, ist)
            if neuer is True:
                out["aktualisierbar"] = True
                out["hinweis"] = f"Aktualisieren auf {mit.split()[-1]}"
            elif neuer is False:
                out["hinweis"] = (f"Stick ist neuer ({ist.split()[-1]}) als die "
                                  f"mitgelieferte {mit.split()[-1]}")
            else:
                # Nicht vergleichbar (fremde Firmware, anderes Namensschema):
                # anbieten, aber ehrlich benennen, dass es ein WECHSEL ist.
                out["aktualisierbar"] = True
                out["hinweis"] = f"Wechseln auf {mit.split()[-1]}"
        elif mit and ist:
            out["hinweis"] = "aktuell"
        else:
            out["hinweis"] = "Fassung nicht feststellbar"
        return out

    if not usb_access():
        # ⚠️ Zwei Betriebsarten, zwei Raete. In der Erweiterung ist der Zugriff
        # bereits angefordert (`usb: true` in config.yaml) — fehlt er trotzdem,
        # liegt es am Rechner darunter, nicht an einer vergessenen Option.
        if als_erweiterung():
            out["hinweis"] = (
                "Kein Zugriff auf den USB-Bus — ohne ihn laesst sich die "
                "Stick-Firmware nicht einspielen. Die Erweiterung fordert ihn "
                "an; fehlt er dennoch, hat der Rechner darunter keinen: bei "
                "Home Assistant in einer virtuellen Maschine muss der Stick "
                "erst dorthin durchgereicht werden.")
        else:
            out["hinweis"] = ("Der Behaelter hat keinen Zugriff auf den "
                              "USB-Bus. Mit -v /dev/bus/usb:/dev/bus/usb "
                              "starten.")
        return out

    if dfu_present():
        out["zustand"] = ST_DFU
        out["aktualisierbar"] = True
        out["hinweis"] = "Neuer Stick im Bootlader — Firmware einspielen"
        return out

    # ⚠️ Bevor „kein Stick" gemeldet wird: liegt vielleicht doch einer am
    # seriellen Anschluss, nur noch nicht angebunden? Das kommt vor, wenn er
    # nach dem Start dazukam. Frueher stand hier trotzdem „Kein Stick
    # gefunden" — eine Meldung, die den Anwender an der falschen Stelle
    # suchen laesst (am Geraet statt an der Anbindung).
    liegt_da = [n for n in _by_id_namen() if "q-culfw" in n]
    if liegt_da:
        out["zustand"] = ST_ABSENT
        out["hinweis"] = ("Ein Stick liegt am Anschluss (" + liegt_da[0] +
                          "), ist aber noch nicht angebunden. QCCU sieht "
                          "regelmaessig nach; wenn es dabei bleibt, hilft ein "
                          "Neustart.")
        return out

    out["zustand"] = ST_ABSENT
    out["hinweis"] = ("Kein Stick gefunden. Ein fabrikneuer CUL meldet sich "
                      "von selbst im Bootlader. Einen bereits benutzten "
                      "abziehen, die BL-Taste auf der Rueckseite gedrueckt "
                      "halten und wieder einstecken"
                      + (f" (erwartet: {serial_path})" if serial_path else "")
                      + ".")
    return out


def _by_id_namen():
    """Die Namen unter /dev/serial/by-id, ohne Fehler bei fehlendem Pfad."""
    try:
        return sorted(os.listdir(BY_ID))
    except OSError:
        return []


def to_bootloader(radio, serial_path, wait=10):
    """Laufenden Stick in den Bootlader schicken und den Port freigeben."""
    if radio is None:
        return dfu_present()
    try:
        radio.release_for_flash()
    except Exception:
        pass
    ende = time.time() + wait
    while time.time() < ende:
        if serial_path and not os.path.exists(serial_path):
            break
        time.sleep(0.3)
    ende = time.time() + wait
    while time.time() < ende:
        if dfu_present():
            return True
        time.sleep(0.5)
    return dfu_present()


def flash(hex_path, log=None):
    """erase / flash / start. Gibt (Erfolg, Protokoll) zurueck."""
    zeilen = []

    def sag(s):
        zeilen.append(s)
        if log:
            log(s)

    if not os.path.isfile(hex_path):
        sag(f"Firmware-Datei fehlt: {hex_path}")
        return False, zeilen
    if not dfu_present():
        sag("Der Bootlader meldet sich nicht. Stick abziehen, Taster halten, "
            "einstecken — dann erneut.")
        return False, zeilen

    for schritt in (["erase"], ["flash", hex_path], ["start"]):
        sag("dfu-programmer " + " ".join(schritt))
        try:
            r = subprocess.run(["dfu-programmer", MCU] + schritt,
                               capture_output=True, timeout=120)
        except Exception as ex:
            sag(f"fehlgeschlagen: {ex}")
            return False, zeilen
        aus = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        for l in aus.splitlines():
            if l.strip():
                sag("  " + l.strip())
        if r.returncode != 0:
            # ⚠️ `start` ist der EINE Schritt, dessen Rueckgabe nichts wert
            # ist: er beendet den Bootlader, das Geraet verschwindet dabei
            # sofort vom Bus und meldet sich mit einer ANDEREN USB-Kennung
            # zurueck (2ff4 -> 2069). Das Werkzeug sieht dann nur noch, dass
            # sein Geraet weg ist, und liefert 1 — obwohl genau das der
            # gewuenschte Ausgang ist. Am Aufbau belegt: `erase` und `flash`
            # liefen sauber („25106 bytes used"), `start` meldete 1, und der
            # Stick war danach als q-culfw da.
            #
            # Also nicht der Zahl glauben, sondern NACHSEHEN: taucht der
            # Stick auf, war es ein Erfolg. Nur wenn er ausbleibt, ist es
            # wirklich schiefgegangen.
            if schritt[0] == "start" and _stick_kam_hoch():
                sag("  (der Bootlader meldet beim Start seinen eigenen "
                    "Abgang als Fehler — der Stick ist da)")
                break
            sag(f"Abbruch (Rueckgabe {r.returncode})")
            return False, zeilen

    sag("Fertig — der Stick startet neu.")
    return True, zeilen


def _stick_kam_hoch(sekunden=12.0):
    """Wartet, bis sich der frisch geflashte Stick meldet.

    Gesucht wird derselbe Name wie beim Suchen im Betrieb; die Neu-Anmeldung
    am USB dauert einen Moment, deshalb wird wiederholt nachgesehen."""
    ende = time.time() + sekunden
    while time.time() < ende:
        try:
            for name in os.listdir(BY_ID):
                if "q-culfw" in name:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False
