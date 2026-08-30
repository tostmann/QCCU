#!/usr/bin/env python3
"""Die BidCoS/AskSin-Rahmenebene und die Zentralenlogik dazu.

WOFUER DAS DA IST
-----------------
`qccu_cul.py` reicht `A`-Zeilen durch, ohne sie zu verstehen — die
Zentralenrolle spielt heute CUL_HM in FHEM. Damit die QCCU BidCoS-Geraete
SELBST fuehren kann (und Home Assistant sie ohne FHEM sieht), braucht sie
genau drei Dinge: die Rahmen lesen, die richtigen quittieren, und schalten.
Dieses Modul ist die unterste Schicht davon.

WAS BELEGT IST UND WAS NICHT — die Trennlinie ist wichtig
----------------------------------------------------------
**Belegt (an eigener Hardware oder von `busware-groundtruth` on-air-verifiziert):**

* Der Rahmenaufbau. Eine `A`-Zeile von q-culfw ist `A` + Rahmen + EIN
  RSSI-Byte, das der Stick anhaengt. Der Rahmen selbst:
  `LEN MSGCNT FLAGS TYPE SRC(3) DST(3) NUTZLAST…` — gepruefte Beispiele
  stehen im Pruefstand (`test_bidcos_mac.py`), alle am 20.08.2026 aus der
  Luft mitgeschnitten.
* Die Statusmeldung: Typ `0x10`, Subtyp `0x06`, Kanal = `nutzlast[1] & 0x3F`
  (die Maske ist Pflicht — obere Bits tragen bei Batteriebauformen Flags),
  Zustand = `nutzlast[2]`, `0xC8` = ein, `0x00` = aus. An einem
  HM-LC-Sw1-Pl-2 nachgemessen: `… 10 <geraet> <zentrale> 06 01 00 00 33` bei
  ausgeschaltetem Relais.
* Der Schaltbefehl: Typ `0x11`, Nutzlast `02 <kanal> <pegel> 00 00` — **fuenf
  Byte**. ⚠️ Die verkuerzte Drei-Byte-Form wird von echten eq-3-Aktoren
  STILLSCHWEIGEND verworfen; das ist die on-air belegte Ursache von „quittiert
  die Konfiguration, ignoriert aber den Schaltbefehl".

**Ebenfalls belegt, seit dem 20.08. nachmittags:**

* Die **Quittung**: Typ `0x02`, Nutzlast `00`, Flags `0x80`, und sie traegt die
  ZAEHLERNUMMER des quittierten Rahmens. Gegen den Mitschnitt einer echten
  Zentrale geprueft — `quittung()` baut deren Zeile byte-identisch nach
  (`As 0A <zaehler> 80 02 <zentrale> <geraet> 00`).
* Die **CONFIG-Grammatik**, lesend wie schreibend: aus dem Mitschnitt einer
  echten Zentrale gewonnen und danach SELBST gesendet und vom Geraet
  quittiert:
  ```
  READ (peek)   01  <kanal> 04 <peer 3> <peerkanal> <liste>
  START         01  <kanal> 05 <peer 3> <peerkanal> <liste>
  WRITE_INDEX   01  <kanal> 08 <reg> <wert> [<reg> <wert> …]
  END           01  <kanal> 06
  ```
* **`pairCentral` liegt in Liste 0 auf den Registern 0x0A, 0x0B, 0x0C** — an
  eigener Hardware bestaetigt. Das Anlernen ist damit genau dieser
  Schreibvorgang mit der eigenen Adresse.

**Weiterhin NICHT belegt:**

* Der Weg vom **Anlernruf** zur Antwort: ob ein Geraet die Sequenz auch dann
  annimmt, wenn es noch KEINE Zentrale kennt (frisch zurueckgesetzt), ist
  ungeprueft — dafuer muss jemand den Knopf druecken. Der Reparaturfall lief
  ueber eine Adresse, die das Geraet bereits als Zentrale trug.
* Deshalb bleibt `Zentrale.senden_erlaubt` in der Vorgabe **False**. Der Riegel
  ist jetzt aber eine BETRIEBS-Entscheidung, keine Wissensluecke mehr.

⚠️ ZWEI ZENTRALEN AUF EINEM FUNK
--------------------------------
Wo an denselben Geraeten schon eine andere Zentrale haengt (typisch CUL_HM
in FHEM), zerlegt eine zweite Zentrale mit DERSELBEN Adresse beide Seiten. Diese Klasse quittiert deshalb ausschliesslich Rahmen an die EIGENE
Adresse und weigert sich, eine Adresse zu uebernehmen, die als fremd bekannt
ist (`fremde_zentralen`).
"""
from __future__ import annotations

# --- Flags im Rahmenkopf ----------------------------------------------------
FLAG_WAKEUP = 0x01
FLAG_WAKEMEUP = 0x02
FLAG_BCAST = 0x04
FLAG_BURST = 0x10
FLAG_BIDI = 0x20      # erwartet eine Quittung
FLAG_RPTED = 0x40     # war eine Wiederholung
FLAG_RPTEN = 0x80     # darf wiederholt werden

# --- Nachrichtentypen, die hier vorkommen -----------------------------------
MT_DEVINFO = 0x00
MT_CONFIG = 0x01
MT_ACK = 0x02
MT_INFO = 0x10
MT_SET = 0x11

SUB_INFO_LEVEL = 0x06     # Statusmeldung
SUB_ACK_STATUS = 0x01     # Quittung MIT Zustand
SUB_ACK_L2 = 0x00         # blosse Empfangsquittung

PEGEL_EIN = 0xC8
PEGEL_AUS = 0x00

ANLERNEN_UNGEPRUEFT = """Die Anlern-Sequenz ist NICHT on-air belegt.
Zum Freischalten fehlt genau eines von beidem:
  (a) ein Mitschnitt der SENDESEITE einer echten Zentrale beim Anlernen
      (QCCU mit `--raw-log` starten, dann mit CUL_HM neu anlernen — die
      `>> As…`-Zeilen sind die gesuchten Bytes), oder
  (b) eine benennbare Quelle, die die Bytefolge belegt.
Bis dahin bleibt `senden_erlaubt=False`."""


class RahmenFehler(ValueError):
    """Eine Zeile war keine brauchbare `A`-Zeile."""


class Frame:
    """Ein BidCoS-Rahmen, so wie er auf dem Funk liegt."""

    __slots__ = ("msgcnt", "flags", "mtype", "src", "dst", "payload", "rssi")

    def __init__(self, msgcnt, flags, mtype, src, dst, payload, rssi=None):
        self.msgcnt = msgcnt & 0xFF
        self.flags = flags & 0xFF
        self.mtype = mtype & 0xFF
        self.src = src.upper()
        self.dst = dst.upper()
        self.payload = bytes(payload)
        self.rssi = rssi

    # -- lesen -------------------------------------------------------------

    @classmethod
    def von_a_zeile(cls, zeile):
        """`A<hex>` -> Frame. Wirft `RahmenFehler`, wenn nichts Brauchbares.

        ⚠️ Das LETZTE Byte ist der Pegel, den der Stick anhaengt — es gehoert
        NICHT zur Nutzlast. Wer es mitzaehlt, liest bei jedem Rahmen ein Byte
        zu viel; das Laengenbyte am Anfang ist der Schiedsrichter.
        """
        z = (zeile or "").strip()
        if not z or z[0] != "A":
            raise RahmenFehler("keine A-Zeile")
        roh = z[1:].strip()
        if len(roh) % 2 or len(roh) < 20:
            raise RahmenFehler(f"unbrauchbare Laenge: {len(roh)} Zeichen")
        try:
            b = bytes.fromhex(roh)
        except ValueError as ex:
            raise RahmenFehler(f"kein Hex: {ex}") from ex
        laenge = b[0]
        if laenge < 9:
            raise RahmenFehler(f"Laengenbyte zu klein: {laenge}")
        if len(b) < laenge + 1:
            raise RahmenFehler(f"Rahmen zu kurz: {len(b)} < {laenge + 1}")
        rssi = b[laenge + 1] if len(b) > laenge + 1 else None
        return cls(msgcnt=b[1], flags=b[2], mtype=b[3],
                   src=b[4:7].hex().upper(), dst=b[7:10].hex().upper(),
                   payload=b[10:laenge + 1], rssi=rssi)

    # -- schreiben ---------------------------------------------------------

    def bytes_ohne_pegel(self):
        rumpf = (bytes([self.msgcnt, self.flags, self.mtype])
                 + bytes.fromhex(self.src) + bytes.fromhex(self.dst)
                 + self.payload)
        return bytes([len(rumpf)]) + rumpf

    def as_befehl(self):
        """Die Zeile, die an den Stick geht (culfw-Stil)."""
        return "As" + self.bytes_ohne_pegel().hex().upper()

    # -- Eigenschaften -----------------------------------------------------

    @property
    def subtyp(self):
        return self.payload[0] if self.payload else None

    @property
    def ist_rundruf(self):
        return self.dst == "000000"

    def erwartet_quittung(self):
        return bool(self.flags & FLAG_BIDI)

    def an_uns(self, eigene_id):
        return self.dst.upper() == (eigene_id or "").upper()

    def __repr__(self):
        return (f"<Frame {self.src}->{self.dst} typ=0x{self.mtype:02X} "
                f"flags=0x{self.flags:02X} nutz={self.payload.hex().upper()}>")


# --- Deutungen, die on-air belegt sind --------------------------------------

def status_aus(frame):
    """Statusmeldung -> (Kanal, an?) oder None.

    Gilt fuer `0x10`/Subtyp `0x06` (Statusmeldung) und `0x02`/Subtyp `0x01`
    (Quittung mit Zustand). ⚠️ Die Maske `& 0x3F` auf dem Kanalbyte ist
    PFLICHT: die oberen Bits tragen bei batteriebetriebenen Bauformen Flags
    (0x80 = schwache Batterie). Ohne Maske meldet so ein Geraet einen
    Geisterkanal 129 statt Kanal 1.
    """
    if frame.mtype == MT_INFO and frame.subtyp == SUB_INFO_LEVEL:
        pass
    elif frame.mtype == MT_ACK and frame.subtyp == SUB_ACK_STATUS:
        pass
    else:
        return None
    if len(frame.payload) < 3:
        return None
    return (frame.payload[1] & 0x3F, frame.payload[2] >= 1)


def devinfo_aus(frame):
    """Anlernruf (`0x00`) -> was das Geraet ueber sich sagt.

    Die Felder liegen dort, wo die `<type>`-Bedingungen der `rftypes` sie
    suchen (Index gezaehlt ohne Laengenbyte, Nutzlast ab Index 9):
    Firmware bei 9, Modellkennung bei 10..11, Seriennummer 12..21,
    Klassenbyte bei 22. Genau diese Zuordnung waehlt beim Tabellenbau die
    richtige Beschreibung aus.
    """
    p = frame.payload
    if frame.mtype != MT_DEVINFO or len(p) < 14:
        return None
    daten = {
        "firmware": p[0],
        "modell": (p[1] << 8) | p[2],
        "serial": p[3:13].decode("ascii", "replace").rstrip("\x00"),
        "adresse": frame.src,
        # Index 23 des Anlernrufs. Aus diesem Byte holen die rftypes die
        # Kanalzahl (`count_from_sysinfo`) — mal ganz, mal nur die unteren
        # Bits. Roh mitnehmen, ausgewertet wird es dort, wo der Aufbau des
        # Geraetetyps bekannt ist.
        "sysinfo": p[14] if len(p) > 14 else None,
    }
    if len(p) >= 14:
        daten["klasse"] = p[13]
    if len(p) >= 16:
        # Die Bits, die manche Gattungen unterscheiden (Index 23.x).
        daten["bits"] = {"23.5": (p[14] >> 5) & 1, "23.7": (p[14] >> 7) & 1}
    return daten


def schaltbefehl(zentrale, geraet, kanal, an, msgcnt):
    """Schalten: Typ 0x11, Nutzlast `02 <kanal> <pegel> 00 00`.

    ⚠️ Die zwei Null-Byte am Ende (Rampenzeit) sind PFLICHT — die verkuerzte
    Drei-Byte-Form verwerfen echte Aktoren stillschweigend. On-air belegt
    (`busware-groundtruth`, Bank-Aufbau an einem HM-LC-Sw1-Pl-2), und die
    `rftypes` sagen dasselbe: `LEVEL_SET` hat bei Index 12 zwei feste Byte.
    """
    pegel = PEGEL_EIN if an else PEGEL_AUS
    return Frame(msgcnt=msgcnt, flags=FLAG_BIDI | FLAG_RPTEN, mtype=MT_SET,
                 src=zentrale, dst=geraet,
                 payload=bytes([0x02, kanal & 0xFF, pegel, 0x00, 0x00]))


# --- Konfiguration schreiben (Liste 0/1/…) ----------------------------------

CFG_PEER_LIST = 0x03
CFG_READ = 0x04
CFG_START = 0x05
CFG_END = 0x06
CFG_WRITE_INDEX = 0x08
CFG_STATUS_REQ = 0x0E

# Wo die Adresse der Zentrale im Geraet steht — Liste 0, drei Byte.
REG_PAIR_CENTRAL = 0x0A


def konfig_schreiben(zentrale, geraet, register, kanal=0, liste=0, msgcnt=1,
                     peer="000000", peerkanal=0):
    """START / WRITE_INDEX / END — die Sequenz, mit der eine Zentrale schreibt.

    `register` ist eine Abbildung {Registeradresse: Wert}. Zurueck kommen die
    drei Rahmen in Sendereihenfolge; das Geraet quittiert jeden einzeln.

    Belegt: die Grammatik stammt aus dem Mitschnitt einer echten Zentrale
    (20.08.2026) und wurde anschliessend selbst gesendet — ein
    HM-LC-Sw1-Pl-2 hat alle drei Rahmen angenommen und quittiert.
    """
    p = bytes.fromhex(peer)
    # ⚠️ Die Reihenfolge bleibt, wie der Aufrufer sie angibt — NICHT sortiert.
    # Eine funktionierende Zentrale schreibt beim Anlernen die Zentralenadresse
    # ZUERST und die Zugabe danach; ob das Geraet darauf besteht, ist nicht
    # geklaert. Solange es nicht geklaert ist, wird das Belegte nachgebaut und
    # nicht umsortiert.
    paare = b"".join(bytes([r & 0xFF, w & 0xFF]) for r, w in register.items())
    return [
        Frame(msgcnt, FLAG_BIDI | FLAG_RPTEN, MT_CONFIG, zentrale, geraet,
              bytes([kanal, CFG_START]) + p + bytes([peerkanal, liste])),
        Frame((msgcnt + 1) & 0xFF, FLAG_BIDI | FLAG_RPTEN, MT_CONFIG, zentrale,
              geraet, bytes([kanal, CFG_WRITE_INDEX]) + paare),
        Frame((msgcnt + 2) & 0xFF, FLAG_BIDI | FLAG_RPTEN, MT_CONFIG, zentrale,
              geraet, bytes([kanal, CFG_END])),
    ]


# Zwei Register, die eine funktionierende Zentrale beim Anlernen MITSCHREIBT.
# ⚠️ Ohne sie hat ein frisch zurueckgesetztes Geraet den Schreibvorgang hier
# nicht quittiert — mit ihnen quittiert es jeden der drei Rahmen. Belegt am
# 20.08.2026 durch den Mitschnitt eines Anlernvorgangs der busware-Rule-Engine
# an einem HM-LC-Sw1-Pl-2; deren Schreibrahmen lautet
# `00 08 0A <hi> 0B <mid> 0C <lo> 01 01 02 01`, unserer trug nur die ersten
# drei Paare. Was die beiden bewirken, ist damit NICHT geklaert — nur, dass
# sie dazugehoeren.
REG_ANLERN_ZUGABE = {0x01: 0x01, 0x02: 0x01}


def anlern_folge(zentrale, geraet, msgcnt=1):
    """Anlernen = die eigene Adresse in Liste 0 auf 0x0A/0x0B/0x0C schreiben.

    ⚠️ Was hier NICHT passiert: ein Schluessel. BidCoS/AskSin kennt kein
    Anlerngeheimnis wie HmIP — wer schreiben darf, ist angelernt. Genau
    deshalb ist die Adresswahl der Zentrale eine Sicherheitsfrage und keine
    Formalie.

    ⚠️ Die drei Rahmen allein sind NICHT der Anlernvorgang. Danach gehoert ein
    bestaetigender Schaltbefehl (`schaltbefehl`), und der Erfolgsbeweis ist die
    Antwort des Geraets darauf — nicht die Quittung auf die Konfigurations-
    rahmen. So macht es die busware-Rule-Engine, und so ist es on-air belegt.
    """
    a = bytes.fromhex(zentrale.upper())
    register = {REG_PAIR_CENTRAL: a[0], REG_PAIR_CENTRAL + 1: a[1],
                REG_PAIR_CENTRAL + 2: a[2]}
    register.update(REG_ANLERN_ZUGABE)
    return konfig_schreiben(zentrale, geraet, register,
                            kanal=0, liste=0, msgcnt=msgcnt)


def statusabfrage(zentrale, geraet, kanal=1, msgcnt=1):
    """Den Zustand erfragen: `01 <kanal> 0E`.

    Deckt sich byte-genau mit `LEVEL_GET` aus den `rftypes` (`type="0x01"`,
    `channel_field="9"`, Index 10.0 fester Wert 14) — und mit dem, was eine
    echte Zentrale sendet.
    """
    return Frame(msgcnt, FLAG_BIDI | FLAG_RPTEN, MT_CONFIG, zentrale, geraet,
                 bytes([kanal, CFG_STATUS_REQ]))


def quittung(frame, zentrale):
    """Die Empfangsquittung auf einen Rahmen an uns: Typ 0x02, Nutzlast `00`.

    ⚠️ NICHT on-air belegt, nur die Form: `busware-groundtruth` nennt
    „mt=0x02 subtype 0x00" als blosse Empfangsquittung. Dass die Zaehlernummer
    des quittierten Rahmens uebernommen wird, ist AskSin-Brauch und hier
    Annahme — vor dem ersten Senden zu bestaetigen.
    """
    return Frame(msgcnt=frame.msgcnt, flags=FLAG_RPTEN, mtype=MT_ACK,
                 src=zentrale, dst=frame.src, payload=bytes([SUB_ACK_L2]))


def zufaellige_adresse(gesperrt=()):
    """Eine BidCoS-Adresse fuer die eigene Zentrale wuerfeln.

    ⚠️ Ausgeschlossen wird mehr als nur die Sperrliste: `000000` ist der
    Rundruf, und Adressen, die im Labor bereits eine Zentrale bezeichnen,
    duerfen nicht herauskommen — zwei Zentralen mit derselben Adresse zerlegen
    beide Seiten. Dieselbe Haltung wie bei der HmIP-Adresse der QCCU, die beim
    ersten Start gewuerfelt und dann gemerkt wird.
    """
    import secrets
    tabu = {"000000", "FFFFFF"} | {g.upper() for g in gesperrt}
    while True:
        a = f"{secrets.randbelow(0x1000000):06X}"
        if a not in tabu:
            return a


class Zentrale:
    """Die BidCoS-Zentralenrolle — vorerst als Mitleser mit Absichtserklaerung.

    `verarbeite()` liefert zu jedem Rahmen eine Liste von Vorgaengen. Ob
    daraus wirklich gesendet wird, entscheidet `senden_erlaubt` — und das
    steht aus gutem Grund auf False (siehe Kopf des Moduls).
    """

    def __init__(self, eigene_id, senden_erlaubt=False, fremde_zentralen=()):
        eigen = (eigene_id or "").upper()
        fremd = {f.upper() for f in fremde_zentralen}
        if eigen in fremd:
            raise ValueError(
                f"{eigen} ist als fremde Zentrale bekannt — zwei Zentralen mit "
                f"derselben Adresse zerlegen beide Seiten")
        self.eigene_id = eigen
        self.senden_erlaubt = bool(senden_erlaubt)
        self.fremde_zentralen = fremd
        self.geraete = {}          # Adresse -> zuletzt gesehener Zustand
        self.gesehen = {}          # Adresse -> Anzahl Rahmen
        self.angelernt = {}        # Adresse -> was der Anlernruf sagte
        self._msgcnt = 0
        self._anlernen_bis = 0.0
        # Wenn gesetzt: NUR dieses Geraet wird angelernt.
        self.anlern_ziel = None

    def naechster_zaehler(self, schritte=1):
        self._msgcnt = (self._msgcnt + schritte) & 0xFF
        return self._msgcnt

    # -- Anlernfenster -----------------------------------------------------

    def anlernen_oeffnen(self, sekunden=60, ziel=None):
        """Das Fenster oeffnen — wahlweise NUR fuer ein bestimmtes Geraet.

        ⚠️ Ohne Ziel wird genommen, wer immer gerade seinen Knopf drueckt.
        Das ist das Verhalten einer Zentrale von eQ-3, aber ein Anlernruf ist
        ein RUNDRUF, und in Funkreichweite steht selten nur die eigene
        Wohnung: wer bei offenem Fenster irgendwo in der Nachbarschaft seinen
        Anlernknopf drueckt, bekommt unsere Konfigurationsrahmen — und wenn er
        sie annimmt, gehoert sein Geraet uns statt ihm. Mit Ziel passiert das
        nicht.
        """
        import time as _t
        self._anlernen_bis = _t.time() + max(0, int(sekunden))
        self.anlern_ziel = (ziel or "").upper() or None
        return int(sekunden)

    def anlernen_erlaubt(self, adresse):
        """Darf DIESES Geraet jetzt angelernt werden?"""
        if not self.anlernen_offen():
            return False
        return self.anlern_ziel is None or self.anlern_ziel == adresse.upper()

    def anlernen_schliessen(self):
        self._anlernen_bis = 0.0
        self.anlern_ziel = None

    def anlernen_offen(self):
        """Restzeit in Sekunden, 0 = zu."""
        import time as _t
        rest = self._anlernen_bis - _t.time()
        return int(rest) if rest > 0 else 0

    def verarbeite(self, zeile):
        """Eine `A`-Zeile -> Liste von Vorgaengen.

        Jeder Vorgang ist ein Verzeichnis mit `art` und den Angaben dazu.
        Arten: `status`, `anlernruf`, `quittung_faellig`, `fremd`.
        """
        try:
            f = Frame.von_a_zeile(zeile)
        except RahmenFehler:
            return []
        self.gesehen[f.src] = self.gesehen.get(f.src, 0) + 1
        vorgaenge = []

        if (st := status_aus(f)) is not None:
            kanal, an = st
            self.geraete.setdefault(f.src, {})[kanal] = an
            vorgaenge.append({"art": "status", "geraet": f.src,
                              "kanal": kanal, "an": an, "rssi": f.rssi})

        if f.mtype == MT_DEVINFO and (info := devinfo_aus(f)):
            vorgaenge.append({"art": "anlernruf", **info})
            # ⚠️ Nur bei OFFENEM Fenster antworten. Ein Anlernruf ist ein
            # Rundruf — jedes Geraet in Reichweite sendet ihn, wenn jemand
            # seinen Knopf drueckt, auch das des Nachbarn. Wer ungefragt
            # antwortet, reisst fremde Geraete an sich.
            if self.anlernen_offen() and not self.anlernen_erlaubt(f.src):
                vorgaenge.append({"art": "anlernruf_fremd", "geraet": f.src,
                                  "ziel": self.anlern_ziel})
            elif self.anlernen_erlaubt(f.src):
                rahmen = anlern_folge(self.eigene_id, f.src,
                                      self.naechster_zaehler(3) - 2)
                self.angelernt[f.src] = info
                vorgaenge.append({
                    "art": "anlernen_faellig", "geraet": f.src,
                    "modell": info.get("modell"), "firmware": info.get("firmware"),
                    # Klasse und Bits gehoeren mit: ohne sie kann der Aufrufer
                    # ein Geraet, dessen Modellkennung er nicht fuehrt, nicht
                    # ueber die Gattung erkennen — und genau dafuer gibt es sie.
                    "klasse": info.get("klasse"), "bits": info.get("bits"),
                    # Aus diesem Byte kommt die Kanalzahl.
                    "sysinfo": info.get("sysinfo"),
                    "befehle": [r.as_befehl() for r in rahmen],
                    "gesendet": self.senden_erlaubt})

        # Fremder Verkehr: an eine andere Zentrale gerichtet. Das ist KEIN
        # Fehler, sondern der Normalfall im Labor — und die Stelle, an der
        # sich zeigt, dass wir uns dort nicht einmischen duerfen.
        if not f.ist_rundruf and not f.an_uns(self.eigene_id):
            vorgaenge.append({"art": "fremd", "geraet": f.src, "ziel": f.dst,
                              "fremde_zentrale": f.dst in self.fremde_zentralen})

        if f.erwartet_quittung() and f.an_uns(self.eigene_id):
            q = quittung(f, self.eigene_id)
            vorgaenge.append({"art": "quittung_faellig", "an": f.src,
                              "befehl": q.as_befehl(),
                              "gesendet": self.senden_erlaubt})
        return vorgaenge

    def schalten(self, geraet, kanal, an):
        """Den Schaltbefehl BAUEN. Gesendet wird er nur, wenn es erlaubt ist."""
        f = schaltbefehl(self.eigene_id, geraet.upper(), kanal, an,
                         self.naechster_zaehler())
        return {"art": "schalten", "geraet": geraet.upper(), "kanal": kanal,
                "an": an, "befehl": f.as_befehl(),
                "gesendet": self.senden_erlaubt}
