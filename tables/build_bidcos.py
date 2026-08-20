#!/usr/bin/env python3
"""Baut die BidCoS-Gerätetabellen aus den `rftypes`-Beschreibungen von eQ-3.

WOFUER DAS DA IST
-----------------
Der HmIP-Zweig holt seine Parameterlisten aus `HMIPServer.jar` (siehe
`build_paramsets.py`). Fuer die klassische BidCoS-Seite gibt es das Archiv
nicht — dort liegen die Beschreibungen als XML unter `firmware/rftypes/` im
DEMSELBEN debmatic-Paket. Der echte `rfd` liest genau diese Dateien
(`Device Description Dir = /firmware/rftypes`); dieses Werkzeug leitet daraus
dieselben Angaben ab, die eine Zentrale ueber `getParamsetDescription`
ausgibt.

DREI TABELLEN, WEIL DIE AUSWAHL ZWEISTUFIG IST
----------------------------------------------
Ein HmIP-Geraet waehlt seine Parameterliste ueber Kanaltyp + Fassung. Bei
BidCoS haengt schon die DATEI vom Geraet ab:

  bidcos_types.json      Modellkennung (DEVINFO) -> Geraetetyp + Firmware-Fenster
                         -> welcher Aufbau gilt
  bidcos_layouts.json    Aufbau (= eine Quelldatei): Kanaele, Richtung,
                         Empfangsart, Verweise auf die Paramsets
  bidcos_paramsets.json  die Paramsets selbst, nach Inhalt zusammengelegt

⚠️ **Die Firmware waehlt die Datei.** 64 der Typkennungen stehen in mehr als
einer Datei, und die `<type>`-Bedingungen zerlegen den Firmware-Raum:
`HM-LC-Sw1-Pl` gibt es fuer <=0x15, 0x16..0x23 und >=0x24 — mit
unterschiedlichen Parameterlisten. Wer die Firmware ignoriert, baut fuer ein
echtes Geraet die falsche Liste. (Ein Geraet mit Firmware 1.12 = 0x1C faellt
gerade NICHT in die naheliegende `rf_s.xml`, sondern in die Fassung darunter.)

Gelesen wird am DEVINFO, gezaehlt ohne Laengenbyte:
  Index 9      Firmware-Byte      (Bedingungen GE/LE/EQ)
  Index 10..11 Modellkennung      (2 Byte)

WAS HIER NICHT PASSIERT
-----------------------
Es wird nichts geraten. Ein Kanal, dessen Anzahl erst das Geraet nennt
(`count_from_sysinfo`), wird als solcher gekennzeichnet statt auf 1 gesetzt;
eine Empfangsart, die die CCU-Bitmaske nicht kennt (`TRIPLE_BURST`), wird
gemeldet statt eingepasst. Beides gehoert an die Stelle, die das Geraet
wirklich kennt — den Anlernvorgang.
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET

# --- Abbildung auf die Schreibweise der CCU-Schnittstelle --------------------

# `<logical type=…>` -> `TYPE`. Der Bestand kennt genau diese sechs.
TYPEN = {"boolean": "BOOL", "integer": "INTEGER", "float": "FLOAT",
         "option": "ENUM", "action": "ACTION", "string": "STRING"}

# `operations="read,write,event"` -> `OPERATIONS`. `determine` ist keine
# CCU-Operation (kommt 10x vor) und traegt kein Bit.
OPERATIONEN = {"read": 1, "write": 2, "event": 4, "determine": 0}

# `ui_flags` -> `FLAGS`. Ohne Angabe ist ein Parameter sichtbar; `invisible`
# ist die Ausnahme und loescht das Bit wieder.
FLAGS = {"visible": 1, "internal": 2, "transform": 4, "service": 8,
         "sticky": 16, "invisible": 0}

# `rx_modes` -> `RX_MODE`. ⚠️ `TRIPLE_BURST` hat in der CCU-Bitmaske keine
# Entsprechung, die belegbar waere — es wird gemeldet, nicht erfunden.
EMPFANG = {"ALWAYS": 1, "BURST": 2, "CONFIG": 4, "WAKEUP": 8, "LAZY_CONFIG": 16}


class Bericht:
    """Sammelt, was auffiel — nichts wird still verschluckt."""

    def __init__(self):
        self.punkte = collections.Counter()
        self.beispiele = collections.defaultdict(list)

    def __call__(self, art, wo=""):
        self.punkte[art] += 1
        if wo and len(self.beispiele[art]) < 3:
            self.beispiele[art].append(wo)

    def ausgeben(self, strom=sys.stdout):
        if not self.punkte:
            print("  keine Auffaelligkeiten", file=strom)
            return
        for art, anzahl in self.punkte.most_common():
            print(f"  {anzahl:6d}  {art}", file=strom)
            for w in self.beispiele[art]:
                print(f"            z.B. {w}", file=strom)


def zahl(text, art, ersatz):
    """`min`/`max`/`default` aus dem XML — fehlt oder unlesbar -> Ersatzwert."""
    if text is None:
        return ersatz
    try:
        return float(text) if art == "FLOAT" else int(float(text))
    except (TypeError, ValueError):
        return ersatz


def parameter_bauen(p, bericht, wo):
    """Ein `<parameter>` -> ein Eintrag, wie ihn `getParamsetDescription` liefert."""
    logisch = p.find("logical")
    if logisch is None:
        # Bedingungs-Parameter in <type> und <frame> haben kein <logical> —
        # die gehoeren nicht hierher und werden vom Aufrufer ferngehalten.
        return None
    art = TYPEN.get(logisch.get("type"))
    if art is None:
        bericht(f"unbekannter logical-Typ '{logisch.get('type')}'", wo)
        return None

    # OPERATIONS. ⚠️ Eine Stelle im Bestand schreibt "read, event" MIT
    # Leerzeichen (rf_cc_rt_dn.xml/ACTUAL_TEMPERATURE) — wer stumpf an ','
    # trennt, verliert dort `event`.
    ops = 0
    for wort in (p.get("operations") or "read,event").replace(" ", "").split(","):
        if wort not in OPERATIONEN:
            bericht(f"unbekannte operation '{wort}'", wo)
        ops |= OPERATIONEN.get(wort, 0)

    roh = [w for w in (p.get("ui_flags") or "").replace(" ", "").split(",") if w]
    flags = 0 if "invisible" in roh else FLAGS["visible"]
    for wort in roh:
        if wort not in FLAGS:
            bericht(f"unbekanntes ui_flag '{wort}'", wo)
        flags |= FLAGS.get(wort, 0)

    eintrag = {"TYPE": art, "OPERATIONS": ops, "FLAGS": flags,
               "ID": p.get("id"), "TAB_ORDER": 0}

    if art == "ENUM":
        werte = [o.get("id") for o in logisch.findall("option")]
        if not werte:
            bericht("ENUM ohne <option>", wo)
        vorgabe = [i for i, o in enumerate(logisch.findall("option"))
                   if o.get("default") == "true"]
        eintrag["VALUE_LIST"] = werte
        eintrag["MIN"], eintrag["MAX"] = 0, max(len(werte) - 1, 0)
        eintrag["DEFAULT"] = vorgabe[0] if vorgabe else 0
    elif art in ("BOOL", "ACTION"):
        eintrag["MIN"], eintrag["MAX"] = False, True
        eintrag["DEFAULT"] = (logisch.get("default") == "true")
    elif art == "STRING":
        eintrag["MIN"] = eintrag["MAX"] = eintrag["DEFAULT"] = ""
    else:
        eintrag["MIN"] = zahl(logisch.get("min"), art, 0.0 if art == "FLOAT" else 0)
        eintrag["MAX"] = zahl(logisch.get("max"), art, 1.0 if art == "FLOAT" else 1)
        eintrag["DEFAULT"] = zahl(logisch.get("default"), art,
                                  0.0 if art == "FLOAT" else 0)

    # Einheit und Bedienrolle stehen — anders als bei HmIP — direkt im XML.
    # Die Zeichenketten werden NICHT aufgeraeumt: die CCU liefert genau diese
    # (auch '100%', 'von 180°' und ein einzelnes Leerzeichen).
    if logisch.get("unit"):
        eintrag["UNIT"] = logisch.get("unit")
    if p.get("control"):
        eintrag["CONTROL"] = p.get("control")

    sonder = [{"ID": s.get("id"), "VALUE": zahl(s.get("value"), art, 0)}
              for s in logisch.findall("special_value")]
    if sonder:
        eintrag["SPECIAL"] = sonder
    return eintrag


def paramset_index(wurzel):
    """id -> `<paramset>`. ⚠️ AUCH leere aufnehmen: ein `<subset ref=…>` darf
    auf ein legitim leeres Paramset zeigen. Wer nur gefuellte indiziert, meldet
    19 Verweise faelschlich als kaputt (davon 4 in eq-3-Dateien)."""
    tabelle = {}
    for ps in wurzel.iter("paramset"):
        if ps.get("id"):
            tabelle.setdefault(ps.get("id"), ps)
    return tabelle


def parameter_sammeln(ps, index, bericht, wo, tiefe=0, gesehen=frozenset()):
    """Parameter eines Paramsets, `<subset ref=…>` rekursiv mitgezogen."""
    raus = []
    if tiefe > 6:
        bericht("subset-Verschachtelung zu tief", wo)
        return raus
    for kind in ps:
        if kind.tag == "parameter":
            raus.append(kind)
        elif kind.tag == "subset":
            ref = kind.get("ref")
            if ref in gesehen:
                bericht("subset-Zyklus", f"{wo} ref={ref}")
                continue
            ziel = index.get(ref)
            if ziel is None:
                # Kommt im Bestand genau EINMAL vor: rf_cm.xml (HM-OU-CM-PCB),
                # Kanal 1 MASTER -> `signal_led_paramset` existiert nicht. Ein
                # Defekt in den eq-3-Daten, kein Lesefehler.
                bericht("subset-ref laeuft ins Leere", f"{wo} ref={ref}")
                continue
            raus.extend(parameter_sammeln(ziel, index, bericht, wo,
                                          tiefe + 1, gesehen | {ref}))
    return raus


def richtung(kanal):
    """1 = Sender, 2 = Empfaenger, 0 = keines — aus `<link_roles>`.

    Der HmIP-Zweig raet die Richtung am Namenssuffix (`_RECEIVER`); hier steht
    sie in den Daten, das ist besser."""
    if kanal.get("direction") == "sender":
        return 1
    rollen = kanal.find("link_roles")
    if rollen is None:
        return 0
    if rollen.find("source") is not None:
        return 1
    if rollen.find("target") is not None:
        return 2
    return 0


def empfangsart(wurzel, bericht, wo):
    """`rx_modes` -> CCU-Bitmaske, oder None wenn nichts angegeben ist."""
    roh = wurzel.get("rx_modes")
    if not roh:
        return None
    maske = 0
    for wort in roh.replace(" ", "").split(","):
        if wort not in EMPFANG:
            bericht(f"Empfangsart '{wort}' ohne CCU-Entsprechung — weggelassen", wo)
            continue
        maske |= EMPFANG[wort]
    return maske or None


def bedingungen_lesen(typ, bericht, wo):
    """Die `<type>`-Bedingungen -> wie dieser Typ erkannt wird.

    eQ-3 kennt ZWEI Wege, und `priority` sagt welchen:

      priority=2  konkretes Modell — Modellkennung an Index 10 (2 Byte)
      priority=1  Gattungs-Rueckfall — Klassenbyte an Index 22, dazu
                  gelegentlich einzelne Bits an Index 23.x
      ohne        gar keine Bedingung: `HM-RCV-50`/`CENTRAL` in `rf_central.xml`
                  — die virtuelle Fernbedienung der Zentrale, die nie aus einem
                  DEVINFO erkannt wird

    ⚠️ Der Rueckfall ist kein Beiwerk: ein Geraet, dessen Modellkennung wir
    nicht fuehren, bekommt darueber trotzdem einen brauchbaren Typ
    (`HM-LC-SwX` statt gar nichts). Wer nur nach Modellkennung baut, laesst
    genau die unbekannten Geraete im Regen.

    Rueckgabe: (art, spezifikation) mit art aus {"model","class","intern"}.
    """
    modell = klasse = klasse_size = None
    fw_min = fw_max = None
    bits = []
    for p in typ.findall("parameter"):
        index, roh, op = p.get("index"), p.get("const_value"), p.get("cond_op", "EQ")
        # ⚠️ Die Community-Dateien schreiben "E" statt "EQ".
        if op == "E":
            op = "EQ"
        try:
            wert = int(str(roh), 16) if str(roh).lower().startswith("0x") else int(roh)
        except (TypeError, ValueError):
            bericht(f"Bedingung mit unlesbarem const_value '{roh}'", wo)
            continue
        if index == "10.0":
            modell = wert
        elif index == "22.0":
            klasse, klasse_size = wert, p.get("size", "1.0")
        elif index == "9.0":
            if op == "GE":
                fw_min = wert
            elif op == "LE":
                fw_max = wert
            elif op == "EQ":
                fw_min = fw_max = wert
            else:
                bericht(f"unbekannter cond_op '{op}' auf der Firmware", wo)
        elif index and index.startswith("23."):
            bits.append({"index": index, "size": p.get("size", "0.1"), "value": wert})
        else:
            # Nicht still verschlucken: eine unbeachtete Bedingung heisst,
            # dass ein Typ moeglicherweise falsch zugeordnet wird.
            bericht(f"unbeachtete Bedingung index={index} {op} {roh}", wo)

    gemeinsam = {}
    if fw_min is not None:
        gemeinsam["fw_min"] = fw_min
    if fw_max is not None:
        gemeinsam["fw_max"] = fw_max

    if modell is not None:
        if klasse is not None:
            bericht("Typ mit Modellkennung UND Klassenbyte", wo)
        return "model", dict(gemeinsam, key=f"0x{modell:04X}")
    if klasse is not None:
        spez = dict(gemeinsam, key=f"0x{klasse:04X}", class_size=klasse_size)
        if bits:
            spez["bits"] = bits
        return "class", spez
    if typ.findall("parameter"):
        bericht("Typ mit Bedingungen, aber weder Modell noch Klasse", wo)
        return None, gemeinsam
    return "intern", gemeinsam


def bauen(quelle, bericht):
    typen = {"by_model": {}, "by_class": {}, "internal": []}
    aufbauten = {}   # Dateiname ohne .xml -> Kanalaufbau
    psets = {}       # Inhaltsschluessel -> Paramset
    unlesbar = []
    zahlen = collections.Counter()

    for pfad in sorted(os.listdir(quelle)):
        if not pfad.endswith(".xml"):
            continue
        name = pfad[:-4]
        try:
            wurzel = ET.parse(os.path.join(quelle, pfad)).getroot()
        except ET.ParseError as ex:
            # Vier Dateien im Bestand sind nicht wohlgeformt — ausschliesslich
            # Community-Geraete (`hb-*`): einmal Latin-1 in einer als UTF-8
            # erklaerten Datei, dreimal ein doppeltes Anfuehrungszeichen.
            unlesbar.append((pfad, str(ex)))
            continue
        zahlen["dateien"] += 1
        index = paramset_index(wurzel)

        # --- Kanalaufbau dieser Datei ---
        kanaele = {}
        for kanal in wurzel.iter("channel"):
            nr = kanal.get("index")
            if nr is None:
                bericht("channel ohne index", pfad)
                continue
            if len(kanal) == 0 and kanal.get("type") is None:
                zahlen["platzhalter-kanaele"] += 1   # leer, wird uebersprungen
                continue
            eintrag = {"type": kanal.get("type"), "direction": richtung(kanal)}
            if kanal.get("count_from_sysinfo"):
                # Die Anzahl nennt erst das Geraet — beim Anlernen aufloesen.
                eintrag["count_from_sysinfo"] = kanal.get("count_from_sysinfo")
            else:
                eintrag["count"] = int(kanal.get("count") or 1)
            if kanal.get("aes_default") == "true":
                eintrag["aes_default"] = True

            verweise = {}
            for ps in kanal:
                if ps.tag != "paramset":
                    continue
                art = ps.get("type")
                if art not in ("MASTER", "VALUES", "LINK"):
                    bericht(f"paramset-Typ '{art}' am Kanal", pfad)
                    continue
                wo = f"{pfad}#{nr}/{art}"
                inhalt = {}
                for p in parameter_sammeln(ps, index, bericht, wo):
                    e = parameter_bauen(p, bericht, wo)
                    if e:
                        inhalt[p.get("id")] = e
                        zahlen["parameter"] += 1
                roh = json.dumps(inhalt, sort_keys=True, ensure_ascii=False)
                schluessel = hashlib.sha1(roh.encode("utf-8")).hexdigest()[:12]
                psets.setdefault(schluessel, inhalt)
                verweise[art] = schluessel
                zahlen["paramsets"] += 1
            eintrag["paramsets"] = verweise
            kanaele[nr] = eintrag
            zahlen["kanaele"] += 1

        # --- das Paramset des GERAETES selbst (Adresse ohne Kanal) ---
        # Jede der 225 Dateien fuehrt eines als direktes Kind von <device>.
        # ⚠️ Fuenf Dateien (Gong-/LED-Signalgeraete: rf_cf, rf_cfm, rf_cfm_tw,
        # rf_cm, hb-ou-mp3-led) fuehren zwei oder drei nebeneinander, ohne dass
        # eines davon als Definition referenziert wuerde — welches das Geraet
        # meint, ist aus den Daten nicht zu entscheiden. Genommen wird das
        # erste, und der Fall wird GEMELDET statt stillschweigend geraten.
        gp = [k for k in wurzel if k.tag == "paramset" and k.get("type") == "MASTER"]
        geraet_pset = None
        if gp:
            if len(gp) > 1:
                bericht(f"{len(gp)} geraeteweite MASTER-paramsets — erstes genommen", pfad)
            inhalt = {}
            for p in parameter_sammeln(gp[0], index, bericht, f"{pfad}#dev/MASTER"):
                e = parameter_bauen(p, bericht, f"{pfad}#dev/MASTER")
                if e:
                    inhalt[p.get("id")] = e
                    zahlen["parameter"] += 1
            roh = json.dumps(inhalt, sort_keys=True, ensure_ascii=False)
            geraet_pset = hashlib.sha1(roh.encode("utf-8")).hexdigest()[:12]
            psets.setdefault(geraet_pset, inhalt)
            zahlen["paramsets"] += 1

        maske = empfangsart(wurzel, bericht, pfad)
        aufbauten[name] = {"channels": kanaele}
        if geraet_pset:
            aufbauten[name]["device_paramsets"] = {"MASTER": geraet_pset}
        if maske is not None:
            aufbauten[name]["rx_mode"] = maske
        if wurzel.get("supports_aes") == "true":
            aufbauten[name]["aes"] = True

        # --- welche Geraetetypen auf diesen Aufbau zeigen ---
        for typ in wurzel.iter("type"):
            kennung = typ.get("id")
            art, spez = bedingungen_lesen(typ, bericht, f"{pfad}/{kennung}")
            if art is None:
                continue
            fassung = {"type": kennung, "label": typ.get("name") or kennung,
                       "layout": name}
            fassung.update({k: v for k, v in spez.items() if k != "key"})
            if typ.get("updatable") == "true":
                fassung["updatable"] = True
            if art == "intern":
                typen["internal"].append(fassung)
            else:
                ziel = typen["by_model"] if art == "model" else typen["by_class"]
                ziel.setdefault(spez["key"], []).append(fassung)
            zahlen[f"typ-{art}"] += 1

    # Fassungen mit engerem Firmware-Fenster zuerst: wer sucht, nimmt die
    # erste passende und liegt damit richtig.
    def enge(f):
        return (f.get("fw_min") is None and f.get("fw_max") is None,
                -(f.get("fw_min") or 0))
    for gruppe in ("by_model", "by_class"):
        for liste in typen[gruppe].values():
            liste.sort(key=enge)
    return typen, aufbauten, psets, unlesbar, zahlen


def schreiben(pfad, daten):
    """Atomar + fsync — die Tabellen liegen auf NFS und werden von einem
    anderen Prozess gelesen; ein halb geschriebener Stand waere still."""
    roh = json.dumps(daten, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    tmp = pfad + ".tmp"
    f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(f, roh)
    os.fsync(f)
    os.close(f)
    os.replace(tmp, pfad)
    d = os.open(os.path.dirname(pfad) or ".", os.O_RDONLY)
    os.fsync(d)
    os.close(d)
    if open(pfad, "rb").read() != roh:
        raise SystemExit(f"! {pfad} kam anders zurueck als geschrieben")
    return len(roh)


def main():
    a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    a.add_argument("--rftypes", required=True,
                   help="Verzeichnis firmware/rftypes aus dem debmatic-Paket")
    a.add_argument("-o", "--out", default="tables", help="Zielverzeichnis")
    g = a.parse_args()

    bericht = Bericht()
    typen, aufbauten, psets, unlesbar, zahlen = bauen(g.rftypes, bericht)

    os.makedirs(g.out, exist_ok=True)
    groessen = {
        "bidcos_types.json": schreiben(os.path.join(g.out, "bidcos_types.json"), typen),
        "bidcos_layouts.json": schreiben(os.path.join(g.out, "bidcos_layouts.json"), aufbauten),
        "bidcos_paramsets.json": schreiben(os.path.join(g.out, "bidcos_paramsets.json"), psets),
    }

    print(f"  Dateien gelesen        : {zahlen['dateien']}")
    print(f"  Geraetetypen           : {zahlen['typ-model']} nach Modellkennung "
          f"({len(typen['by_model'])} Kennungen), {zahlen['typ-class']} als "
          f"Gattungs-Rueckfall ({len(typen['by_class'])} Klassen), "
          f"{zahlen['typ-intern']} intern")
    print(f"  Aufbauten              : {len(aufbauten)}")
    print(f"  Kanaele                : {zahlen['kanaele']} "
          f"(+{zahlen['platzhalter-kanaele']} leere uebersprungen)")
    print(f"  Paramsets              : {zahlen['paramsets']} Verweise auf "
          f"{len(psets)} verschiedene")
    print(f"  Parameter              : {zahlen['parameter']}")
    for n, b in groessen.items():
        print(f"    {n:24s} {b/1024:8.1f} KiB")
    if unlesbar:
        print(f"  Nicht wohlgeformt      : {len(unlesbar)}")
        for n, ex in unlesbar:
            print(f"            {n}: {ex}")
    print("  Auffaelligkeiten:")
    bericht.ausgeben()


if __name__ == "__main__":
    main()
