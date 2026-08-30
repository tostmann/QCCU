#!/usr/bin/env python3
"""Die BidCoS/AskSin-Gerätetabellen und die Geräte, die daraus entstehen.

WOFUER DAS DA IST
-----------------
`qccu.py` fuehrt HmIP-Geraete ueber `Tables` + `Device`. Fuer die klassische
BidCoS-Seite gilt dasselbe Muster, aber mit einer Stufe mehr davor: bei HmIP
sagt der Geraetetyp allein, welche Kanaele und Parameter es gibt — bei BidCoS
entscheidet erst die Erkennung, WELCHE Beschreibung ueberhaupt gilt.

  `BidcosTables`  laedt die drei Tabellen aus `build_bidcos.py` und beantwortet
                  die Frage „welcher Typ ist das?" aus dem DEVINFO
  `BidcosDevice`  ein erkanntes Geraet, das `listDevices`/`getDeviceDescription`
                  bedienen kann — dieselbe Form wie `Device` bei HmIP

DIE ERKENNUNG HAT ZWEI WEGE
---------------------------
So macht es eQ-3 (Attribut `priority` in den `rftypes`), und beide werden
gebraucht:

  1. **Konkretes Modell** — Modellkennung aus dem DEVINFO (Index 10, 2 Byte),
     zusammen mit dem Firmware-Fenster. ⚠️ Ein Typ kann in MEHREREN Fassungen
     vorliegen, die sich nur in der Firmware unterscheiden: `HM-LC-Sw1-Pl` gibt
     es fuer <=0x15, 0x16..0x23 und >=0x24, mit unterschiedlichen
     Parameterlisten. Der Labor-Schalter (Firmware 1.12 = 0x1C) faellt gerade
     NICHT in die naheliegendste Datei.
  2. **Gattung** — Klassenbyte aus dem DEVINFO (Index 22), teils mit einzelnen
     Bits. Damit bekommt ein Geraet, dessen Modellkennung die Tabelle nicht
     fuehrt, trotzdem einen brauchbaren Typ (`HM-LC-SwX`) statt gar keinem.
     **Ohne diesen Weg stehen genau die unbekannten Geraete im Regen.**

WAS HIER NICHT ENTSCHIEDEN WIRD
-------------------------------
Die Anzahl der Kanaele steht bei 92 Kanaldefinitionen NICHT in der Tabelle
(`count_from_sysinfo`) — sie nennt erst das Geraet beim Anlernen. Dieses Modul
nimmt sie entgegen und setzt sie NICHT auf gut Glueck: fehlt sie, bleibt es bei
einem Kanal, und der Aufrufer sieht an `dynamisch()`, wo er nachliefern muss.
"""
from __future__ import annotations

import json
import os
import sys

# Reihenfolge, in der eine Zentrale die Paramsets eines Kanals nennt.
PSET_FOLGE = ("MASTER", "VALUES", "LINK")


def firmware_text(byte):
    """Firmware-Byte -> Anzeige, wie CUL_HM und die Zentrale sie fuehren.

    Ein Byte, zwei Nibbles: 0x1C -> "1.12". Belegt in `10_CUL_HM.pm`,
    `CUL_HM_infoUpdtDevData`: `sprintf("%d.%d", hex($fw1), hex($fw2))`.
    """
    try:
        b = int(byte)
    except (TypeError, ValueError):
        return "0.0"
    return f"{(b >> 4) & 0xF}.{b & 0xF}"


class BidcosTables:
    """Die drei BidCoS-Tabellen. Fehlen sie, laeuft QCCU trotzdem an.

    Dieselbe Haltung wie bei `Tables` fuer HmIP: ohne Tabellen koennen keine
    Geraete gefuehrt werden, aber die Oberflaeche kann sagen, was fehlt.
    """

    DATEIEN = ("bidcos_types.json", "bidcos_layouts.json", "bidcos_paramsets.json")

    def __init__(self, base):
        self.base = base
        self.fehlend = []
        self.types = self._load("bidcos_types.json")
        self.layouts = self._load("bidcos_layouts.json")
        self.psets = self._load("bidcos_paramsets.json")

    def _load(self, name):
        p = os.path.join(self.base, name)
        if not os.path.exists(p):
            print(f"  ! BidCoS-Tabelle fehlt: {p}", file=sys.stderr)
            self.fehlend.append(name)
            return {}
        with open(p) as f:
            return json.load(f)

    def zustand(self):
        """Was die Oberflaeche ueber die Tabellen wissen muss."""
        return {
            "ok": not self.fehlend,
            "fehlend": list(self.fehlend),
            "modellkennungen": len(self.types.get("by_model", {})),
            "gattungen": len(self.types.get("by_class", {})),
            "aufbauten": len(self.layouts),
            "paramsets": len(self.psets),
        }

    # -- Erkennung ---------------------------------------------------------

    @staticmethod
    def _passt(eintrag, firmware):
        """Liegt die Firmware im Fenster dieser Fassung?"""
        if firmware is None:
            # Ohne Firmware ist nur eine Fassung OHNE Fenster sicher richtig.
            return eintrag.get("fw_min") is None and eintrag.get("fw_max") is None
        if eintrag.get("fw_min") is not None and firmware < eintrag["fw_min"]:
            return False
        if eintrag.get("fw_max") is not None and firmware > eintrag["fw_max"]:
            return False
        return True

    def erkennen(self, modell=None, firmware=None, klasse=None, bits=None):
        """DEVINFO -> Typ-Eintrag, oder None.

        `modell` und `klasse` als Zahl, `firmware` als Byte, `bits` als
        Abbildung `{"23.5": 1}` fuer die Feinunterscheidung der Gattungen.

        Erst das konkrete Modell, dann die Gattung — genau die Reihenfolge, die
        `priority` in den `rftypes` vorgibt.
        """
        if modell is not None:
            for e in self.types.get("by_model", {}).get(f"0x{int(modell):04X}", []):
                if self._passt(e, firmware):
                    return e
        if klasse is not None:
            for e in self.types.get("by_class", {}).get(f"0x{int(klasse):04X}", []):
                if not self._passt(e, firmware):
                    continue
                # Einzelne Bits unterscheiden zwei Gattungen derselben Klasse
                # (z.B. HM-ES-PMSwX: Klasse 0x51 UND Bit 23.5). Ist das Bit
                # nicht bekannt, wird der Eintrag NICHT genommen — lieber kein
                # Typ als der falsche.
                erwartet = e.get("bits") or []
                if erwartet:
                    if not bits:
                        continue
                    if any(bits.get(b["index"]) != b["value"] for b in erwartet):
                        continue
                return e
        return None

    def typ_suchen(self, typname):
        """Ein Typ-Eintrag ueber seinen Namen — fuer Pruefstand und Oberflaeche."""
        for gruppe in ("by_model", "by_class"):
            for liste in self.types.get(gruppe, {}).values():
                for e in liste:
                    if e.get("type") == typname:
                        return e
        for e in self.types.get("internal", []):
            if e.get("type") == typname:
                return e
        return None

    # -- Beschreibungen ----------------------------------------------------

    def aufbau(self, name):
        return self.layouts.get(name) or {}

    def paramset(self, layout, kanal, pset="VALUES"):
        """Die Parameterliste eines Kanals — `kanal=None` meint das Geraet selbst."""
        a = self.aufbau(layout)
        if kanal is None:
            verweise = a.get("device_paramsets") or {}
        else:
            ch = (a.get("channels") or {}).get(str(kanal))
            verweise = (ch or {}).get("paramsets") or {}
        schluessel = verweise.get(str(pset).upper())
        return dict(self.psets.get(schluessel) or {})


def _aus_sysinfo(spec, byte):
    """`"23.0:1.0"` -> Wert aus dem Sysinfo-Byte.

    Links vom Doppelpunkt steht die Lage (Byte.Bit), rechts die Groesse
    (Byte.Bit). Fehlt die Groesse, gilt ein ganzes Byte.
    """
    lage, _, groesse = str(spec).partition(":")
    try:
        idx, _, bit = lage.partition(".")
        # ⚠️ Wir tragen aus dem Anlernruf NUR das Byte an Index 23 mit. Ein
        # Aufbau, der woanders hinzeigt, wuerde sonst stillschweigend das
        # falsche Byte lesen. In allen 230 Dateien steht heute 23 — aber
        # „heute" ist kein Grund, es nicht zu pruefen.
        if int(idx) != 23:
            return 0
        bit = int(bit or 0)
        if not groesse:
            bytes_, bits = 1, 0
        else:
            b, _, r = groesse.partition(".")
            bytes_, bits = int(b or 0), int(r or 0)
    except ValueError:
        return 0
    breite = bytes_ * 8 + bits
    if breite <= 0 or breite > 8:
        return 0
    return (int(byte) >> bit) & ((1 << breite) - 1)


class BidcosDevice:
    """Ein erkanntes BidCoS-Geraet, wie die Gegenstelle es sieht.

    Gleiche Aussenseite wie `Device` (HmIP): `descriptions()` liefert Geraet
    und Kanaele in der Form, die `listDevices` erwartet.
    """

    def __init__(self, address, eintrag, tables, firmware=None, kanalzahlen=None):
        self.address = address.upper()
        self.eintrag = eintrag                    # aus BidcosTables.erkennen()
        self.tables = tables
        self.firmware_byte = firmware
        # Kanalzahlen, die erst das Geraet nennt: {"1": 20}. Was fehlt, bleibt 1.
        self.kanalzahlen = {str(k): int(v) for k, v in (kanalzahlen or {}).items()}
        self.values = {}
        self.master = {}
        self.unreach = None
        self.last_seen = 0.0

    # -- Stammdaten --------------------------------------------------------

    @property
    def devtype(self):
        """Der Typ, wie ihn eine Zentrale nennt — NICHT die Beschriftung.

        ⚠️ Hier unterscheiden sich die Familien: bei HmIP ist die Beschriftung
        zugleich der Typ, bei BidCoS sind es zwei Dinge (`HM-LC-Sw1-Pl-2`
        gegenueber „radio-controlled socket adapter switch actuator 1-channel").
        aiohomematic leitet aus dem TYPE seine Produktgruppe und die
        Geraetezuordnung ab — es muss der Typ sein.
        """
        return self.eintrag.get("type", "")

    @property
    def label(self):
        return self.eintrag.get("label") or self.devtype

    @property
    def typname(self):
        """Der Typ, wie er im Namen erscheint — `HM-LC-Sw1-Pl-2`.

        ⚠️ NICHT `label`. Der Langtext der rftypes
        („radio-controlled socket adapter switch actuator 1-channel") ist als
        Anzeigename unbrauchbar.
        """
        return self.devtype or self.label

    @property
    def layout(self):
        return self.eintrag.get("layout", "")

    @property
    def firmware(self):
        return firmware_text(self.firmware_byte) if self.firmware_byte is not None else "0.0"

    def kanalzahlen_aus_sysinfo(self, sysinfo):
        """Kanalzahlen aus dem Sysinfo-Byte des Anlernrufs setzen.

        Beleg aus den rftypes selbst (`firmware/rftypes`, debmatic): der
        Aufbau traegt am Kanal ein Attribut

            <channel index="1" type="SWITCH" count_from_sysinfo="23.0:1.0">

        Die Notation ist im selben Verzeichnis durchgaengig `Byte.Bit`:
        `size="0.1"` ist EIN Bit, `size="1.0"` ein Byte, `size="2.0"` zwei
        Byte (so steht die Modell-Kennung an Index 10). `23.0:1.0` heisst
        also: ein ganzes Byte an Index 23 — und dieses Byte IST die Zahl der
        Kanaele.

        Ueber alle 230 Dateien kommen vor: `23.0:1.0` (49x, ganzes Byte),
        `23.0:0.3` (30x, drei Bit), `0.4`, `0.5`, `0.2` und zweimal `23.0`
        ohne Groesse. Die Bit-Varianten stehen genau dort, wo Bit 23.5 oder
        23.7 zur Geraeteerkennung dient — die oberen Bits sind dann belegt
        und gehoeren nicht zum Zaehler.

        ⚠️ Eine 0 wird NICHT uebernommen: kein Geraet hat null Kanaele, und
        ein Byte, das wir falsch lesen, soll das Geraet nicht unsichtbar
        machen. Dann bleibt es bei der bisherigen Annahme.
        """
        if sysinfo is None:
            return {}
        gesetzt = {}
        for nr, ch in (self.tables.aufbau(self.layout).get("channels") or {}).items():
            spec = ch.get("count_from_sysinfo")
            if not spec:
                continue
            anzahl = _aus_sysinfo(spec, sysinfo)
            if anzahl:
                gesetzt[str(nr)] = anzahl
        self.kanalzahlen.update(gesetzt)
        return gesetzt

    def dynamisch(self):
        """Kanaele, deren Anzahl das Geraet nennen muss und die noch fehlt."""
        offen = []
        for nr, ch in (self.tables.aufbau(self.layout).get("channels") or {}).items():
            if ch.get("count_from_sysinfo") and nr not in self.kanalzahlen:
                offen.append((nr, ch["count_from_sysinfo"]))
        return sorted(offen, key=lambda x: int(x[0]))

    def channel_list(self):
        """Alle Kanaele als (Nummer, Typ) — `count` aufgefaltet.

        Ein Eintrag `index=1 count=20` sind die Kanaele 1 bis 20 desselben Typs.
        """
        raus = []
        for nr, ch in (self.tables.aufbau(self.layout).get("channels") or {}).items():
            start = int(nr)
            if ch.get("count_from_sysinfo"):
                anzahl = self.kanalzahlen.get(nr, 1)
            else:
                anzahl = int(ch.get("count") or 1)
            for i in range(anzahl):
                raus.append((start + i, ch))
        return sorted(raus, key=lambda x: x[0])

    # -- Beschreibungen ----------------------------------------------------

    def _paramset_namen(self, verweise):
        return [p for p in PSET_FOLGE if p in (verweise or {})]

    def descriptions(self):
        """Geraet und Kanaele, wie `listDevices` sie erwartet."""
        a = self.tables.aufbau(self.layout)
        chans = self.channel_list()
        geraet = {
            "ADDRESS": self.address,
            "TYPE": self.devtype,
            "SUBTYPE": "",
            "FIRMWARE": self.firmware,
            "AVAILABLE_FIRMWARE": "",
            "UPDATABLE": bool(self.eintrag.get("updatable")),
            "FIRMWARE_UPDATE_STATE": "",
            "VERSION": 1,
            "FLAGS": 1,
            "PARAMSETS": self._paramset_namen(a.get("device_paramsets")),
            "INTERFACE": "",
            "PARENT": "",
            "PARENT_TYPE": "",
            "INDEX": 0,
            "AES_ACTIVE": 0,
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
        }
        # RX_MODE nur, wenn die Beschreibung eine Empfangsart nennt. Sonst gar
        # nicht: eine erfundene 0 laesst ein Batteriegeraet wie ein
        # dauerempfangendes aussehen.
        if a.get("rx_mode"):
            geraet["RX_MODE"] = a["rx_mode"]
        out = [geraet]

        for idx, ch in chans:
            eintrag = {
                "ADDRESS": f"{self.address}:{idx}",
                "TYPE": ch.get("type") or "",
                "SUBTYPE": "",
                "PARENT": self.address,
                "PARENT_TYPE": self.devtype,
                "INDEX": idx,
                "VERSION": 1,
                "FLAGS": 1,
                "PARAMSETS": self._paramset_namen(ch.get("paramsets")),
                "AES_ACTIVE": 1 if ch.get("aes_default") else 0,
                "DIRECTION": ch.get("direction", 0),
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
            }
            out.append(eintrag)
        return out

    def paramset_of(self, kanal, pset="VALUES"):
        """Die Parameterliste eines Kanals dieses Geraets.

        Aufgefaltete Kanaele teilen sich die Beschreibung ihres Ursprungs:
        Kanal 7 eines `index=1 count=20`-Blocks nimmt die Liste von Kanal 1.
        """
        # Kein Kanal = das Geraet selbst. ⚠️ Kanal 0 ist ein ECHTER Kanal
        # (MAINTENANCE) und darf hier nicht mit „kein Kanal" zusammenfallen.
        if kanal is None or kanal == "":
            return self.tables.paramset(self.layout, None, pset)
        ziel = int(kanal)
        quelle = None
        for nr, ch in (self.tables.aufbau(self.layout).get("channels") or {}).items():
            start = int(nr)
            if ch.get("count_from_sysinfo"):
                anzahl = self.kanalzahlen.get(nr, 1)
            else:
                anzahl = int(ch.get("count") or 1)
            if start <= ziel < start + anzahl:
                quelle = nr
                break
        if quelle is None:
            return {}
        return self.tables.paramset(self.layout, quelle, pset)
