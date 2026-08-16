#!/usr/bin/env python3
"""Baut die Parametertabelle je Kanaltyp."""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET


"""Schreibweisen, die im Archiv anders heissen als an der Schnittstelle.

Das Archiv fuehrt den Datentyp unter dem Namen, den die Java-Fabriken
benutzen (`logicalType`); eine Zentrale gibt ueber ihre Schnittstelle einen
anderen heraus. Zwei Abweichungen, die erste betrifft rund ein Viertel
aller Parameter:

    BOOLEAN  (Archiv)  ->  BOOL    (Schnittstelle)
    LONG     (Archiv)  ->  STRING  (Schnittstelle)

Beide sind nicht geraten, sondern aus dem Uebersetzer der RPC-Schicht
abgelesen (`DumpParamsRpc`, der die eq-3-Routine selbst aufruft): dort
kommt `BOOL` heraus, und aus `LONG` wird `STRING` — mitsamt Grenzen als
Zeichenkette. Der Grund ist einleuchtend: ein 48-Bit-Wert wie
281474976710655 passt in keine XML-RPC-Ganzzahl. Deshalb werden bei dieser
Umschrift auch MIN/MAX/DEFAULT zu Zeichenketten; ein STRING-Parameter mit
Zahlgrenzen waere in sich widerspruechlich.

⚠️ Das ist keine Kosmetik. Wer die Tabelle ueber JSON-RPC ausliefert, bringt
Home Assistant mit `BOOLEAN` zum Abbruch — `aiohomematic` kennt nur BOOL und
scheitert beim Anlegen der Datenpunkte mit einem Schluesselfehler, OHNE dass
ein Aufruf fehlschlaegt: die Geraeteliste kommt an, die Datenpunkte bleiben
aus. Die on-air von einer echten Zentrale geernteten Vergleichswerte zeigen
denselben Namen (`BOOL`), sie sind hier der Massstab.

Die Umschrift gehoert HIERHER und nicht in die Ausgabeschicht: hier laufen
alle Quellen zusammen (Archiv, Geraete-XML, Ergaenzungen), und danach gibt es
genau eine Schreibweise.
"""
TYP_SCHREIBWEISE = {"BOOLEAN": "BOOL", "LONG": "STRING"}


def load(path):
    with open(path) as f:
        return json.load(f)


def _nullwert(typ):
    """Der neutrale Wert eines Datentyps."""
    t = str(typ).upper()
    if t in ("BOOL", "BOOLEAN", "ACTION"):
        return False
    if t == "FLOAT":
        return 0.0
    if t in ("INTEGER", "LONG"):
        return 0
    return ""


def vervollstaendige_grenzen(merged):
    """Ergaenzt fehlende DEFAULT/MIN/MAX.

    ⚠️ Eine Gegenstelle liest diese drei Felder beim Anlegen eines
    Datenpunktes UNGEPRUEFT. Fehlt eines, bricht sie ab — aiohomematic mit
    „Unable to create data_point: MIN", und zwar fuer das GANZE Geraet, nicht
    nur fuer den einen Parameter. Ein einziger unvollstaendiger Eintrag
    (`COMBINED_PARAMETER`) hat so die komplette Anbindung verhindert.

    Betroffen sind nur die wenigen Parameter, die aus dem on-air-Mitschnitt
    stammen und in keiner Definition des Archivs stehen. Ergaenzt wird
    konservativ und ohne etwas zu behaupten:

      * fehlt DEFAULT → MIN, sonst der neutrale Wert des Typs
      * fehlt MIN     → DEFAULT, sonst der neutrale Wert
      * fehlt MAX     → bei ENUM der letzte Eintrag der Werteliste,
                        sonst DEFAULT, sonst MIN, sonst der neutrale Wert

    Ein Wertebereich, der nichts einschraenkt, ist die zutreffende Auskunft
    fuer einen Parameter ohne Grenzen — geraten wird nichts.
    """
    ergaenzt = 0
    for sets in merged.values():
        for params in sets.values():
            if not isinstance(params, dict):
                continue
            for desc in params.values():
                if not isinstance(desc, dict):
                    continue
                typ = desc.get("TYPE", "STRING")
                if desc.get("DEFAULT") is None:
                    desc["DEFAULT"] = (desc.get("MIN") if desc.get("MIN") is not None
                                       else _nullwert(typ))
                    ergaenzt += 1
                if desc.get("MIN") is None:
                    desc["MIN"] = desc["DEFAULT"]
                    ergaenzt += 1
                if desc.get("MAX") is None:
                    wl = desc.get("VALUE_LIST")
                    if str(typ).upper() == "ENUM" and isinstance(wl, (list, tuple)) and wl:
                        desc["MAX"] = wl[-1]
                    else:
                        desc["MAX"] = desc["DEFAULT"]
                    ergaenzt += 1
    return ergaenzt


def verwirf_ohne_typ(merged):
    """Eintraege ohne `TYPE` entfernen — und zwar laut.

    ⚠️ Ein Parameter ohne Datentyp ist keine magere Auskunft, sondern eine
    gefaehrliche: die Gegenstelle liest das Feld ungeprueft, verwirft daran das
    GANZE Geraet und nennt keinen Grund. Ein einziger solcher Eintrag kostet
    alle Datenpunkte eines Geraets. Lieber fehlt der eine Parameter.

    Dass hier ueberhaupt etwas anfaellt, ist ein Hinweis auf die Auslesestufe
    davor — deshalb wird es gemeldet und nicht stillschweigend behoben.
    """
    entfernt = []
    for ct, sets in merged.items():
        for ps, params in sets.items():
            if not isinstance(params, dict):
                continue
            for pn in [k for k, v in params.items()
                       if not isinstance(v, dict) or v.get("TYPE") is None]:
                del params[pn]
                entfernt.append(f"{ct}/{ps}/{pn}")
    return entfernt


def vereinheitliche_typen(merged):
    """Setzt die Schreibweise der Datentypen auf die der Schnittstelle."""
    geaendert = 0
    for sets in merged.values():
        for params in sets.values():
            if not isinstance(params, dict):
                continue
            for desc in params.values():
                if not isinstance(desc, dict):
                    continue
                neu = TYP_SCHREIBWEISE.get(desc.get("TYPE"))
                if not neu:
                    continue
                desc["TYPE"] = neu
                geaendert += 1
                if neu == "STRING":
                    # Grenzen mitziehen: ein STRING-Parameter mit Zahlgrenzen
                    # waere in sich widerspruechlich, und die Gegenstelle
                    # wandelt die Werte anhand von TYPE um.
                    for feld in ("DEFAULT", "MIN", "MAX"):
                        if isinstance(desc.get(feld), (int, float)) and not isinstance(
                            desc.get(feld), bool
                        ):
                            desc[feld] = str(desc[feld])
    return geaendert


def device_channel_params(devdir):
    """Aus den device_*.xml: Kanaltyp -> Menge von (Parametername, subtype)."""
    out = {}
    files = 0
    for root, _, names in os.walk(devdir):
        for name in names:
            if not (name.startswith("device_") and name.endswith(".xml")):
                continue
            files += 1
            try:
                tree = ET.parse(os.path.join(root, name))
            except ET.ParseError:
                continue
            for ch in tree.iter("channel"):
                ctype = ch.get("type")
                if not ctype:
                    continue
                for p in ch.iter("parameter"):
                    pname = (p.text or "").strip()
                    if not pname:
                        continue
                    pset = {"state": "VALUES", "config": "MASTER"}.get(p.get("type", ""), None)
                    if pset is None:
                        continue
                    sub = p.get("subtype") or "default"
                    out.setdefault(ctype, set()).add((pset, pname, sub))
    return out, files


def main():
    a = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--jar-paramsets", required=True, help="paramsets_from_jar.json")
    a.add_argument("--lookup", required=True, help="param_lookup.json")
    a.add_argument("--devicedir", required=True, help="Verzeichnis mit device_*.xml")
    a.add_argument("-o", "--out", default="paramsets.json")
    a.add_argument("--compare", help="Ergaenzungen, die in keiner Definition stehen (optional)")
    a.add_argument("--catalog", help="catalog.json — fuehrt auch parameterlose Kanaltypen")
    g = a.parse_args()

    jar = load(g.jar_paramsets)
    lookup = load(g.lookup)
    dev, nfiles = device_channel_params(g.devicedir)

    merged = {}
    for key in sorted(jar):
        name = key.split("/v")[0]
        ver = key.split("/v")[1] if "/v" in key else "0"
        tgt = merged.setdefault(name, {})
        for pset, params in jar[key].items():
            dst = tgt.setdefault(pset, {})
            for pn, desc in params.items():
                if pn not in dst or ver.isdigit():
                    dst[pn] = desc

    added, unresolved = 0, {}
    for ctype, entries in dev.items():
        tgt = merged.setdefault(ctype, {})
        for pset, pname, sub in sorted(entries):
            dst = tgt.setdefault(pset, {})
            if pname in dst:
                continue
            desc = lookup.get(f"{pname}@{sub}") or lookup.get(f"{pname}@default")
            if desc is None:
                unresolved.setdefault(pname, 0)
                unresolved[pname] += 1
                continue
            dst[pname] = desc
            added += 1

    verified_added = 0
    if g.compare and os.path.exists(g.compare):
        old = load(g.compare)
        for ct, sets in old.items():
            tgt = merged.setdefault(ct, {})
            for pset, params in sets.items():
                dst = tgt.setdefault(pset, {})
                for pn, desc in params.items():
                    if pn not in dst:
                        dst[pn] = desc
                        verified_added += 1
        print(f"  aus den Ergaenzungen:              {verified_added} Parameter")

    empty = 0
    if g.catalog and os.path.exists(g.catalog):
        cat = load(g.catalog)
        for e in cat.values():
            for ct in e.get("channels", {}).values():
                if ct not in merged:
                    merged[ct] = {"VALUES": {}, "MASTER": {}}
                    empty += 1
        print(f"  ohne eigene Parameter gefuehrt:  {empty} Kanaltypen")

    merged = {k: v for k, v in sorted(merged.items())}

    ohne_typ = verwirf_ohne_typ(merged)
    umgeschrieben = vereinheitliche_typen(merged)
    ergaenzt = vervollstaendige_grenzen(merged)

    print(f"  Kanaltyp-Fassungen aus dem Archiv: {len(jar)}")
    print(f"  Datentyp-Schreibweise angeglichen: {umgeschrieben} Parameter")
    if ohne_typ:
        print(f"  ⚠ OHNE Datentyp verworfen:         {len(ohne_typ)} Parameter "
              f"(z.B. {', '.join(sorted(ohne_typ)[:3])})")
    print(f"  fehlende Grenzen ergaenzt:         {ergaenzt} Felder")
    print(f"  device_*.xml gelesen:              {nfiles}")
    print(f"  Kanaltypen gesamt:                 {len(merged)}")
    print(f"  aus den Geraete-XML ergaenzt:      {added} Parameter")
    if unresolved:
        print(f"  ⚠ ohne Definition geblieben:       {len(unresolved)} Namen "
              f"(z.B. {sorted(unresolved)[:5]})")

    if g.compare and os.path.exists(g.compare):
        old = load(g.compare)
        print(f"\n  Abgleich mit {os.path.basename(g.compare)} "
              f"({len(old)} Kanaltypen):")
        for ct in sorted(old):
            if ct not in merged:
                print(f"    ⚠ {ct}: FEHLT")
                continue
            for pset in ("VALUES", "MASTER"):
                o = old[ct].get(pset, {})
                if not o:
                    continue
                n = merged[ct].get(pset, {})
                miss = sorted(set(o) - set(n))
                mark = "✅" if not miss else "⚠"
                print(f"    {mark} {ct}/{pset}: erwartet {len(o)}, neu {len(n)}"
                      + (f", fehlend {miss}" if miss else ""))

    os.makedirs(os.path.dirname(os.path.abspath(g.out)), exist_ok=True)
    tmp = g.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, g.out)
    print(f"\n  geschrieben: {g.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
