#!/usr/bin/env python3
"""Baut den Geraetekatalog: Geraetetyp -> Beschriftung, Kanal-Layout und
was der Kanal bei DIESEM Geraet mitbringt (`chinfo`).

Zu jedem Kanal gehoeren zwei Angaben, die es nur hier gibt:

* **`v`** — die Fassung des Kanaltyps. Sie waehlt in `paramsets.json` die
  richtige Parameterliste aus; `MAINTENANCE/v2` und `MAINTENANCE/v10` sind
  verschiedene Dinge (330 gegen 602 Konfigurationsparameter).
* **`extra`** — die Parameter, die die Geraetebeschreibung selbst nennt
  (`<parameter type="config|state" subtype="…">NAME</parameter>`), als
  `NAME@subtype`. Sie stehen nicht in der Kanaltyp-Liste; aufgeloest werden
  sie ueber `extra_params.json`.

Zusammen mit der Kanaltyp-Liste ergibt das genau das Paramset, das eine
Zentrale von eQ-3 ausliefert (an einer HmIP-PS-2 ueber alle sieben Kanaele
geprueft, 18.08.2026).
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

SPEC = "de/eq3/cbcs/devicedescription/devicespecification/"


def build(jar_path):
    z = zipfile.ZipFile(jar_path)
    names = [n for n in z.namelist() if n.startswith(SPEC) and n.endswith(".xml")]

    catalog = {}
    varianten = {}
    stats = {"files": 0, "types": 0, "skipped": 0}

    for n in names:
        stats["files"] += 1
        try:
            d = z.read(n).decode("utf-8", "replace")
        except Exception:
            stats["skipped"] += 1
            continue

        channels, chinfo = {}, {}
        try:
            wurzel = ET.fromstring(d)
        except ET.ParseError:
            stats["skipped"] += 1
            continue
        for ch in wurzel.iter("channel"):
            ctype, idx = ch.get("type"), ch.get("index")
            if not ctype or idx is None or not idx.isdigit():
                continue
            i = int(idx)
            if i in channels:
                continue
            channels[i] = ctype
            eintrag = {}
            ver = ch.get("version")
            if ver and ver.isdigit():
                eintrag["v"] = int(ver)
            extra = {}
            for par in ch.iter("parameter"):
                name = (par.text or "").strip()
                pset = {"state": "VALUES", "config": "MASTER"}.get(par.get("type", ""))
                if not name or not pset:
                    continue
                sub = par.get("subtype") or "default"
                extra.setdefault(pset, []).append(f"{name}@{sub}")
            if extra:
                eintrag["extra"] = {k: sorted(set(v)) for k, v in extra.items()}
            if eintrag:
                chinfo[i] = eintrag
        if not channels:
            stats["skipped"] += 1
            continue

        # ⚠️ Die INTERNE VERDRAHTUNG des Geraets — die Ground Truth, nach der
        # eine Zentrale nach dem Anlernen die Verknuepfungen einrichtet. Sie
        # steht in der Beschreibung und muss NICHT geraten werden:
        #
        #     <internalLinks><internalLink sourceIndex="8" targetIndex="10"/>
        #
        # Gegenprobe an der HmIP-PS-2: dort steht 1 -> 3, und genau diese
        # beiden Kanaele hat die echte Zentrale im Referenzmitschnitt
        # verknuepft (Geraetetaste ch1 auf das eigene Relais ch3).
        #
        # 102 der 350 Beschreibungen fuehren solche Links, bis zu acht Stueck.
        # Wo keine stehen, richtet die Zentrale auch keine ein.
        links = []
        for il in wurzel.iter("internalLink"):
            quelle, ziel = il.get("sourceIndex"), il.get("targetIndex")
            if (quelle is None or ziel is None
                    or not quelle.isdigit() or not ziel.isdigit()):
                continue
            paar = [int(quelle), int(ziel)]
            if paar not in links:
                links.append(paar)

        for m in re.finditer(r'<devType\s+label="([^"]+)"\s+id="(\d+)"([^/>]*)', d):
            label, devid, rest = m.group(1), int(m.group(2)), m.group(3)
            # ⚠️ `minVersion` und `maxVersion` EINZELN lesen. Fehlt
            # `maxVersion`, ist das Band nach oben offen — bisher liess die
            # gemeinsame Regex solche Eintraege ganz ohne Band durchgehen,
            # und das sind gerade die JUENGSTEN Beschreibungen.
            mn = re.search(r'minVersion="(\d+)"', rest)
            mx = re.search(r'maxVersion="(\d+)"', rest)
            entry = {
                "label": label,
                "channels": {str(k): v for k, v in sorted(channels.items())},
                "chinfo": {str(k): v for k, v in sorted(chinfo.items())},
                "spec": n.rsplit("/", 1)[-1],
                "min": int(mn.group(1)) if mn else 0,
                "max": int(mx.group(1)) if mx else None,
            }
            if links:
                entry["links"] = links
            varianten.setdefault(str(devid), []).append(entry)

    # ⚠️ WELCHE Beschreibung fuer ein Geraet gilt, entscheidet die FASSUNG
    # seiner Firmware — nicht die Kanalzahl. Die Zentrale schlaegt sie ueber
    # `DeviceType.findDeviceTypeByVersion` nach: gewaehlt wird die
    # Beschreibung, deren `minVersion`/`maxVersion`-Band die Firmware aus dem
    # Anlernruf enthaelt (`maxVersion` fehlt = nach oben offen).
    #
    # QCCU nahm bisher „die mit den meisten Kanaelen". Beim HmIP-ASIR (Typ
    # 298) fuehrt das nachweislich in die Irre: die Fassung fuer alte Firmware
    # hat SECHS Kanaele und LEERE `<internalLinks>`, die fuer neue hat vier
    # Kanaele und den Link 1->2. Nach der Kanalzahl gewinnt immer die alte —
    # und ein Geraet ohne Verdrahtung meldet sich nach dem Anlernen wieder ab.
    #
    # ⚠️ Die alte Regel bleibt trotzdem die VORGABE, und zwar mit Absicht.
    # Sie gilt fuer alles, was die Firmware eines Geraets nicht kennt — also
    # fuer jedes Geraet, das vor dieser Fassung angelernt wurde. Sie durch
    # „juengstes Band" zu ersetzen, waere fuer die einen richtig und fuer die
    # anderen schlechter: der HmIP-BWTH-A im Bestand laeuft mit 2.8.10, und
    # die alte Regel trifft bei ihm zufaellig genau die richtige Beschreibung.
    # Wer die Firmware KENNT, waehlt ueber das Band; wer sie nicht kennt,
    # bleibt bei dem, was bisher lief.
    #
    # Eine Fassung, die sich in Kanaelen, Kanalfassungen und Verdrahtung NICHT
    # von der Vorgabe unterscheidet, wird auf ihr Band eingedampft — das ist
    # der Normalfall und haelt die Tabelle klein.
    for devid, liste in varianten.items():
        # Dieselbe Beschreibung steht mehrfach da, wenn ein Typ unter mehreren
        # Namen verkauft wird (eQ-3 und OEM). Fuer die Bandwahl ist das ein
        # Eintrag.
        gesehen = set()
        eindeutig = []
        for e in liste:
            schluessel = (e["min"], e["max"], e["spec"])
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            eindeutig.append(e)
        # ⚠️ Die Vorgabe wird in DATEIREIHENFOLGE bestimmt, nicht in
        # Bandreihenfolge. Bei gleicher Kanalzahl entschied bisher, wer zuerst
        # kam; sortiert man vorher um, aendern sich 86 Eintraege — nicht weil
        # sie besser wuerden, sondern weil ein Gleichstand anders ausgeht.
        # Solche Bewegung gehoert nicht in eine Aenderung, die etwas anderes
        # will.
        vorgabe = max(eindeutig, key=lambda e: len(e["channels"]))
        eindeutig = sorted(eindeutig, key=lambda e: (
            e["min"], e["max"] if e["max"] is not None else 1 << 40))
        eintrag = {k: v for k, v in vorgabe.items() if k not in ("min", "max")}
        baender = []
        for e in eindeutig:
            band = {"min": e["min"], "spec": e["spec"]}
            if e["max"] is not None:
                band["max"] = e["max"]
            for feld in ("channels", "chinfo", "links"):
                if e.get(feld) != vorgabe.get(feld):
                    band[feld] = e.get(feld)
            baender.append(band)
        if len(baender) > 1:
            eintrag["varianten"] = baender
        catalog[devid] = eintrag
        stats["types"] += 1

    return catalog, stats


def main():
    a = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--jar", required=True)
    a.add_argument("-o", "--out", default="catalog.json")
    g = a.parse_args()

    if not os.path.exists(g.jar):
        print("Archiv fehlt:", g.jar, file=sys.stderr)
        return 1

    cat, st = build(g.jar)
    os.makedirs(os.path.dirname(os.path.abspath(g.out)), exist_ok=True)
    tmp = g.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cat, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, g.out)

    print(f"  {g.out}: {len(cat)} Geraetetypen "
          f"(aus {st['files']} Beschreibungen, {st['skipped']} ohne Kanaele)")
    mit_links = sum(1 for e in cat.values() if e.get("links"))
    print(f"    davon {mit_links} mit interner Verdrahtung (internalLinks)")
    for devid in ("263", "490", "406", "516"):
        e = cat.get(devid)
        if e:
            ch = ", ".join(f"{k}:{v}" for k, v in list(e["channels"].items())[:4])
            lk = "".join(f" [{q}->{z}]" for q, z in e.get("links", ()))
            print(f"    {devid:>4} {e['label']:<22} {ch} ...{lk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
