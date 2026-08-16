#!/usr/bin/env python3
"""Baut den Geraetekatalog: Geraetetyp -> Beschriftung und Kanal-Layout."""
import argparse
import json
import os
import re
import sys
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

        channels = {}
        for ctype, idx in re.findall(r'<channel type="([A-Z_0-9]+)"[^>]*index="(\d+)"', d):
            channels.setdefault(int(idx), ctype)
        if not channels:
            stats["skipped"] += 1
            continue

        for m in re.finditer(r'<devType\s+label="([^"]+)"\s+id="(\d+)"([^/>]*)', d):
            label, devid, rest = m.group(1), int(m.group(2)), m.group(3)
            fw = re.search(r'minVersion="(\d+)"[^>]*maxVersion="(\d+)"', rest)
            entry = {
                "label": label,
                "channels": {str(k): v for k, v in sorted(channels.items())},
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
