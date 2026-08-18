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

        for m in re.finditer(r'<devType\s+label="([^"]+)"\s+id="(\d+)"([^/>]*)', d):
            label, devid, rest = m.group(1), int(m.group(2)), m.group(3)
            fw = re.search(r'minVersion="(\d+)"[^>]*maxVersion="(\d+)"', rest)
            entry = {
                "label": label,
                "channels": {str(k): v for k, v in sorted(channels.items())},
                "chinfo": {str(k): v for k, v in sorted(chinfo.items())},
                "spec": n.rsplit("/", 1)[-1],
            }
            if fw:
                entry["firmware"] = [int(fw.group(1)), int(fw.group(2))]
            old = catalog.get(str(devid))
            if old is None or len(entry["channels"]) > len(old["channels"]):
                catalog[str(devid)] = entry
                if old is None:
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
    for devid in ("263", "490", "406"):
        e = cat.get(devid)
        if e:
            ch = ", ".join(f"{k}:{v}" for k, v in list(e["channels"].items())[:4])
            print(f"    {devid:>4} {e['label']:<22} {ch} ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
