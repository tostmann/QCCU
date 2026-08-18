#!/usr/bin/env python3
"""Die Integration „Homematic(IP) Local" mitliefern.

Wozu: Wer QCCU als Erweiterung installiert, braucht in Home Assistant zusaetzlich
die Integration von SukramJ — bisher ueber HACS, also ein zweiter Weg mit eigenem
Konto, eigener Suche, eigenem Neustart. Das Abbild bringt sie mit und legt sie
dorthin, wo Home Assistant sie sucht. Damit ist die Installation ein Schritt.

⚠️ Mitgeliefert wird sie UNVERAENDERT (MIT-Lizenz, LICENSE liegt daneben). Kein
Fork, keine eigene Domain: die Geraete-Kennungen in Home Assistant bleiben
dieselben, und wer spaeter doch HACS benutzt, wechselt einfach zurueck.

⚠️ **Eine neuere Fassung wird NIEMALS ueberschrieben.** Wer die Integration ueber
HACS pflegt, ist dort meist aktueller als unser Abbild — ihm eine aeltere
unterzuschieben waere ein Rueckschritt, den er nicht bestellt hat und der beim
naechsten HACS-Update wieder verschwindet. Im Zweifel: Finger weg.
"""
from __future__ import annotations

import json
import os
import re
import shutil

DOMAIN = "homematicip_local"

# Was der Aufruf zurueckgibt — und was die Oberflaeche daraus macht.
ST_AUS = "aus"                # Der Anwender will es nicht.
ST_FEHLT_QUELLE = "keine_quelle"   # Im Abbild ist nichts (fremder Bau).
ST_KEIN_HA = "kein_ha"        # Kein Zugriff auf das Konfig-Verzeichnis.
ST_KOPIERT = "kopiert"        # Frisch hingelegt.
ST_ERNEUERT = "erneuert"      # Aeltere Fassung ersetzt.
ST_AKTUELL = "aktuell"        # Liegt schon da, gleiche Fassung.
ST_FREMD_NEUER = "fremd_neuer"     # Dort liegt eine NEUERE — nichts angefasst.
ST_FREMD_UNKLAR = "fremd_unklar"   # Dort liegt etwas Unlesbares — nichts angefasst.
ST_FEHLER = "fehler"


def _teile(v):
    """Eine Fassung in etwas Vergleichbares zerlegen.

    Die Integration zaehlt wie Python: `2.9.1`, dazwischen Vorabfassungen wie
    `2.9.2b4`. Eine Vorabfassung ist AELTER als die fertige Fassung gleicher
    Nummer — `2.9.2b4 < 2.9.2`. Genau dafuer der zweite Teil des Schluessels.
    """
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*(?:([abrc]+|rc|dev|post)\.?(\d*))?\s*$",
                 str(v or ""))
    if not m:
        return None
    zahlen = tuple(int(x) for x in m.group(1).split("."))
    zahlen = zahlen + (0,) * (4 - len(zahlen)) if len(zahlen) < 4 else zahlen
    art, lauf = (m.group(2) or ""), int(m.group(3) or 0)
    rang = {"": 3, "post": 4, "rc": 2, "c": 2, "b": 1, "a": 0, "dev": -1}.get(art, 3)
    return (zahlen, rang, lauf)


def neuer(a, b):
    """Ist `a` neuer als `b`? Unvergleichbares gilt als „nicht neuer"."""
    ta, tb = _teile(a), _teile(b)
    if ta is None or tb is None:
        return None
    return ta > tb


def fassung_von(verzeichnis):
    """Die Fassung einer installierten Integration — oder None."""
    try:
        with open(os.path.join(verzeichnis, "manifest.json")) as f:
            return json.load(f).get("version")
    except Exception:                                # noqa: BLE001
        return None


def _atomar_ablegen(quelle, ziel):
    """Erst vollstaendig danebenlegen, dann in einem Zug tauschen.

    ⚠️ Ein halb kopiertes `custom_components/<domain>` ist schlimmer als gar
    keines: Home Assistant laedt beim Start, was da ist, und scheitert dann an
    einer fehlenden Datei. Deshalb wird nebenan aufgebaut und erst am Ende
    umgehaengt; geht dabei etwas schief, steht der alte Stand wieder da.
    """
    neu_pfad, alt_pfad = ziel + ".neu", ziel + ".alt"
    for p in (neu_pfad, alt_pfad):
        shutil.rmtree(p, ignore_errors=True)
    hatte_alt = False
    try:
        shutil.copytree(quelle, neu_pfad)
        hatte_alt = os.path.isdir(ziel)
        if hatte_alt:
            os.rename(ziel, alt_pfad)
        try:
            os.rename(neu_pfad, ziel)
        except Exception:
            if hatte_alt and not os.path.isdir(ziel):
                os.rename(alt_pfad, ziel)      # zurueck auf den alten Stand
            raise
    finally:
        # ⚠️ Auch wenn schon das Kopieren scheitert (volle Platte, Abbruch
        # mittendrin), darf kein halbes Verzeichnis liegen bleiben: beim
        # naechsten Start stuende es im Weg, und `custom_components` ist kein
        # Ort fuer Bauschutt.
        shutil.rmtree(alt_pfad, ignore_errors=True)
        shutil.rmtree(neu_pfad, ignore_errors=True)
    # Das Verzeichnis muss auf der Platte stehen, bevor Home Assistant startet.
    try:
        d = os.open(os.path.dirname(ziel), os.O_RDONLY)
        os.fsync(d)
        os.close(d)
    except Exception:                                # noqa: BLE001
        pass


def bereitstellen(quelle, ha_config, mitliefern=True):
    """Die Integration bereitstellen. Gibt (Zustand, Meldung, Fassungen) zurueck.

    `quelle` ist das Verzeichnis im Abbild, `ha_config` das
    Konfigurationsverzeichnis von Home Assistant (in der Erweiterung
    `/homeassistant` — NICHT `/config`, das ist das eigene der Erweiterung).
    """
    mit = fassung_von(quelle)
    if not mitliefern:
        return ST_AUS, "Mitliefern ist abgeschaltet.", (mit, None)
    if not mit:
        return (ST_FEHLT_QUELLE,
                "Im Abbild liegt keine Integration zum Mitliefern.", (None, None))
    if not ha_config or not os.path.isdir(ha_config):
        return (ST_KEIN_HA,
                "Kein Zugriff auf das Konfigurationsverzeichnis von Home "
                "Assistant — die Integration bleibt liegen.", (mit, None))

    ziel = os.path.join(ha_config, "custom_components", DOMAIN)
    da = fassung_von(ziel)

    if os.path.isdir(ziel) and da is None:
        return (ST_FREMD_UNKLAR,
                "Dort liegt bereits eine Integration, deren Fassung sich nicht "
                "lesen laesst — sie wird NICHT angetastet.", (mit, None))
    if da:
        if da == mit:
            return ST_AKTUELL, f"Integration {da} liegt bereits vor.", (mit, da)
        if neuer(da, mit):
            return (ST_FREMD_NEUER,
                    f"Dort liegt {da}, mitgeliefert ist {mit} — die neuere "
                    f"bleibt. (So gehoert es sich, wenn HACS sie pflegt.)",
                    (mit, da))

    try:
        os.makedirs(os.path.join(ha_config, "custom_components"), exist_ok=True)
        _atomar_ablegen(quelle, ziel)
    except Exception as ex:                          # noqa: BLE001
        return ST_FEHLER, f"Integration konnte nicht abgelegt werden: {ex}", (mit, da)

    if da:
        return (ST_ERNEUERT,
                f"Integration von {da} auf {mit} gebracht — Home Assistant "
                f"muss dafuer neu starten.", (mit, mit))
    return (ST_KOPIERT,
            f"Integration {mit} eingerichtet — Home Assistant muss dafuer neu "
            f"starten.", (mit, mit))


# Nach diesen Zustaenden hat sich auf der Platte etwas geaendert, und Home
# Assistant laedt Integrationen NUR beim Start.
NEUSTART_NOETIG = (ST_KOPIERT, ST_ERNEUERT)


if __name__ == "__main__":
    import sys
    quelle = sys.argv[1] if len(sys.argv) > 1 else "/opt/qccu/integration/" + DOMAIN
    ha = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("HA_CONFIG", "/homeassistant")
    an = (os.environ.get("BUNDLE_INTEGRATION", "true").lower()
          not in ("false", "0", "no", "nein"))
    zustand, text, (mit, da) = bereitstellen(quelle, ha, an)
    print(f"{zustand}\t{text}")
    sys.exit(0 if zustand != ST_FEHLER else 1)
