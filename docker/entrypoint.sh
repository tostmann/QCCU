#!/bin/sh
# Einstiegspunkt des QCCU-Containers.
# Copyright (c) 2026 Dirk Tostmann.
#
#   serve   Tabellen anlegen, falls sie fehlen, dann die Zentrale starten
#   setup   nur die Tabellen anlegen (erzwingt Neuaufbau)
#   flash   Stick-Firmware einspielen
#   shell   Eingabeaufforderung
set -e

TABLES=/data/tables
STATE=/data/state
QCCU=/opt/qccu

# --- Als Home-Assistant-Erweiterung: Einstellungen aus /data/options.json ---
# Der Supervisor uebergibt die Einstellungen NICHT ueber die Umgebung, sondern
# legt sie als Datei ab. Wer die Erweiterung benutzt, hat also keine Moeglich-
# keit, Umgebungsvariablen zu setzen — ohne dieses Stueck bliebe jede Eingabe
# in der Oberflaeche wirkungslos.
#
# Gelesen wird mit python3 (ist ohnehin im Abbild) statt mit `bashio`: das gibt
# es nur in den Basisabbildern von Home Assistant, und dieses Abbild soll
# genauso unter schlichtem `docker run` laufen. Gesetzte Umgebungsvariablen
# haben Vorrang — wer beides benutzt, meint die Umgebung ernst.
if [ -f /data/options.json ]; then
    _opts=$(python3 - <<'PY' 2>/dev/null || true
import json, shlex
try:
    o = json.load(open("/data/options.json"))
except Exception:
    raise SystemExit(0)
# Nur bekannte Schluessel, damit eine erweiterte Oberflaeche hier nichts
# Unerwartetes in die Umgebung schiebt.
for name in ("SERIAL", "OWN_ADDR", "RPC_PORT", "REGA_PORT",
             "WEB_PORT", "JSON_PORT", "CUL_PORT", "ADVERTISE"):
    wert = o.get(name.lower())
    if wert is None or wert == "":
        continue
    if isinstance(wert, bool):
        wert = "1" if wert else "0"
    print(f"{name}={shlex.quote(str(wert))}")
PY
)
    if [ -n "$_opts" ]; then
        # ⚠️ ZEILENWEISE lesen, nicht `for … in $(…)`: das teilt an
        # Leerzeichen, und ein Wert wie ein Rechnername mit Leerzeichen
        # zerlegte den Aufruf mitten im Anfuehrungszeichen — der Start brach
        # dann komplett ab statt nur diesen einen Wert zu verwerfen.
        # Nur setzen, was die Umgebung nicht schon vorgibt.
        # Das Hier-Dokument statt einer Pipe: `… | while read` liefe in einer
        # Unter-Shell, und die `export` waeren nach der Schleife wieder weg.
        while IFS= read -r _zeile; do
            [ -n "$_zeile" ] || continue
            _name=${_zeile%%=*}
            eval "_vorhanden=\${$_name:-}"
            [ -n "$_vorhanden" ] || eval "export $_zeile"
        done <<EOF
$_opts
EOF
        echo "[qccu] Einstellungen aus /data/options.json uebernommen."
    fi
    unset _opts _zeile _name _vorhanden
fi

# --- Einstellungen, alle über die Umgebung überschreibbar -------------------
# Leer lassen: QCCU sucht den Stick dann selbst (am Namen, nicht
# durch Ausprobieren). Wer mehrere hat, setzt SERIAL von Hand.
SERIAL=${SERIAL:-}
OWN_ADDR=${OWN_ADDR:-}
RPC_PORT=${RPC_PORT:-2010}
REGA_PORT=${REGA_PORT:-8181}
WEB_PORT=${WEB_PORT:-8080}
# JSON-RPC fuer Home Assistant (aiohomematic). 0 = aus.
JSON_PORT=${JSON_PORT:-8082}
# TCP-Zugang im culfw-Stil fuer BidCoS/AskSin; 0 = aus.
CUL_PORT=${CUL_PORT:-0}
ADVERTISE=${ADVERTISE:-}

log() { echo "[qccu] $*"; }
die() { echo "[qccu] FEHLER: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Tabellen anlegen. Läuft einmal; danach liegt das Ergebnis in /data.
# ---------------------------------------------------------------------------
setup() {
    # ⚠️ Kein Verlass auf `set -e` in dieser Funktion: `serve` ruft sie als
    # `if ( setup )` auf, und in einer if-Bedingung ist errexit AUSSER KRAFT —
    # auch in der Unter-Shell und in allem, was sie aufruft (POSIX; mit dash
    # und bash nachgeprueft). Jeder Schritt, an dem es haengt, prueft deshalb
    # selbst mit `|| die`. Ein stiller Weiterlauf nach einem Fehlschlag
    # ergaebe halbe Tabellen, die `need_tables` fuer fertig hielte.
    WORK=$(mktemp -d) || die "kein Arbeitsverzeichnis anlegbar."
    trap 'rm -rf "$WORK"' EXIT
    mkdir -p "$TABLES" || die "Tabellenverzeichnis $TABLES nicht anlegbar."

    log "lade Gerätebeschreibungen (debmatic=${DEBMATIC_VERSION}) …"
    log "  Das Paket enthält die HomeMatic-Software von eQ-3. Für deren Nutzung"
    log "  gelten die HomeMatic Software Lizenzbedingungen (HMSL):"
    log "  https://github.com/eq-3/occu/blob/master/LicenseDE.txt"
    ( cd "$WORK" && apt-get update -qq \
      && apt-get download "debmatic=${DEBMATIC_VERSION}" 2>/dev/null \
      || apt-get download debmatic ) \
      || die "Bündel nicht ladbar.
    Erreicht dieser Rechner das Netz, ist die häufigste Ursache eine zu große
    MTU im Docker-Netz: liegt sie über der des Anschlusses (typisch bei
    DSL/PPPoE, VPN oder Overlay-Netzen), bricht der Bezug mitten im
    Herunterladen ab. Abhilfe für diesen einen Aufruf:
      docker run --rm --network host -v qccu-data:/data tostmann/qccu setup
    dauerhaft: dem Docker-Netz die passende MTU geben."

    # NUR die zwei Dateien herausholen, nicht das ganze Paket entpacken: von den
    # 236 MB Inhalt brauchen wir 32 MB. `dpkg-deb -x` legte alles ab —
    # Firmware-Abbilder, Weboberflaeche, Bibliotheken —, was niemand liest.
    #
    # Die zweite Datei traegt Einheit und Bedienrolle der Parameter (`UNIT`,
    # `CONTROL`). Sie steht NICHT im Archiv — ohne sie blieben Temperaturen
    # ohne „°C" und Sensoren ohne Rolle.
    ( cd "$WORK" && dpkg-deb --fsys-tarfile debmatic*.deb \
        | tar -x ./opt/HMServer/HMIPServer.jar \
               ./opt/HmIP/legacy-parameter-definition.config ) 2>/dev/null
    JAR="$WORK/opt/HMServer/HMIPServer.jar"
    LPD="$WORK/opt/HmIP/legacy-parameter-definition.config"
    rm -f "$WORK"/debmatic*.deb      # das Paket selbst wird nicht mehr gebraucht
    [ -f "$JAR" ] || die "HMIPServer.jar nicht im Bündel gefunden."
    log "Bündel entpackt, lese Beschreibungen aus …"

    # ⚠️ DumpParamsRpc, nicht DumpParams: es reicht jeden Parameter durch den
    # Uebersetzer der RPC-Schicht des Herstellers und liefert damit dieselben
    # Felder wie eine Zentrale — einschliesslich `VALUE_LIST` (steckt im
    # Typumwandler) sowie `UNIT`/`CONTROL` aus der Definitionsdatei. Home
    # Assistant braucht genau die: ohne Werteliste kein Auswahlfeld, ohne
    # Einheit keine Temperatur.
    [ -f "$LPD" ] || log "Definitionsdatei fehlt im Bündel — UNIT/CONTROL bleiben leer."
    ( cd "$WORK" && java -cp "$JAR:$QCCU/jarparam" DumpParamsRpc \
        "$WORK/paramsets_from_jar.json" "$WORK/param_lookup.json" "$LPD" ) \
        || die "Auslesen fehlgeschlagen."

    # 2) Gerätebeschreibungen aus dem Archiv holen (Kanal-Layouts)
    python3 "$QCCU/build_catalog.py" --jar "$JAR" -o "$TABLES/catalog.json" \
        || die "Katalog fehlgeschlagen."

    # 3) Die device_*.xml für den Abgleich bereitlegen
    mkdir -p "$WORK/devicedir"
    python3 - "$JAR" "$WORK/devicedir" <<'PY'
import sys, zipfile, os
jar, out = sys.argv[1], sys.argv[2]
z = zipfile.ZipFile(jar)
n = 0
for name in z.namelist():
    b = os.path.basename(name)
    if b.startswith("device_") and b.endswith(".xml"):
        open(os.path.join(out, b), "wb").write(z.read(name)); n += 1
print(f"[qccu] {n} Geraetebeschreibungen entnommen")
if n == 0:
    raise SystemExit("keine device_*.xml im Archiv")
PY
    [ $? -eq 0 ] || die "Geraetebeschreibungen nicht entnehmbar."

    # 4) Statusdatentypen: Nummer und Laenge je Typ
    log "lese Statusdatentypen …"
    java -cp "$JAR:$QCCU/jarparam" DumpSdt "$TABLES/sdt_table.json" \
        || log "Statusdatentypen fehlgeschlagen — weiter ohne."

    # 5) Zusammenführen
    # --catalog ergaenzt Kanaltypen, die im Katalog stehen, aber im Archiv
    # keine eigene Definition haben (Klima/Heizung). --compare traegt die
    # wenigen Parameter nach, die in keiner Definition stehen und die die
    # Zentrale trotzdem meldet; die Datei liegt im Abbild.
    ERGAENZUNG=""
    [ -f "$QCCU/paramsets_ergaenzung.json" ] && ERGAENZUNG="--compare $QCCU/paramsets_ergaenzung.json"
    # shellcheck disable=SC2086
    python3 "$QCCU/build_paramsets.py" \
        --jar-paramsets "$WORK/paramsets_from_jar.json" \
        --lookup        "$WORK/param_lookup.json" \
        --devicedir     "$WORK/devicedir" \
        --catalog       "$TABLES/catalog.json" \
        $ERGAENZUNG \
        -o "$TABLES/paramsets.json" || die "Zusammenführen fehlgeschlagen."

    log "Tabellen liegen in $TABLES:"
    ls -la "$TABLES" | sed 's/^/       /'

    # ⚠️ HIER loeschen, nicht dem trap ueberlassen: `serve` beendet die Shell
    # nie, sondern ersetzt sie mit `exec python3 …` — ein EXIT-trap feuert dann
    # NICHT. Ohne diese Zeile blieben rund 240 MB (Paket + Entpacktes) im
    # Container liegen, und die Meldung unten waere schlicht falsch.
    rm -rf "$WORK"
    trap - EXIT
    log "Bündel verworfen ($(du -sh "$TABLES" 2>/dev/null | cut -f1) Tabellen behalten)."
}

# ---------------------------------------------------------------------------
need_tables() { [ -f "$TABLES/paramsets.json" ] && [ -f "$TABLES/catalog.json" ]; }

serve() {
    # ⚠️ Der Tabellenbau laeuft in einer UNTER-SHELL, damit sein `die` (also
    # ein `exit 1`) nur ihn selbst beendet und nicht den ganzen Start. Denn
    # scheitert er — kein Netz, Sperre, zu grosse MTU —, ist Abbrechen die
    # schlechteste Antwort: der Behaelter startet in einer Schleife neu, die
    # Oberflaeche kommt nie hoch, und der Anwender sieht den Grund nur in
    # `docker logs`. Ohne Tabellen fuehrt QCCU keine Geraete, aber die
    # Oberflaeche sagt, was fehlt und wie es nachzuholen ist. Dieselbe
    # Haltung wie beim fehlenden Stick eine Zeile weiter unten.
    if ! need_tables; then
        log "erster Start — die Gerätetabellen werden angelegt."
        if ( setup ); then
            :
        else
            log "Die Gerätebeschreibungen konnten NICHT angelegt werden."
            log "QCCU startet trotzdem — die Oberfläche auf Port $WEB_PORT sagt,"
            log "was fehlt. Bei jedem Start wird es erneut versucht (Behälter"
            log "oder Erweiterung neu starten); von Hand nachholen mit:"
            log "  docker run --rm --network host -v qccu-data:/data tostmann/qccu setup"
        fi
    fi
    mkdir -p "$STATE"
    # KEIN Abbruch ohne Stick: der CUL wird im Bootlader ausgeliefert und
    # meldet sich dann gar nicht seriell. Wer hier abbricht, sperrt den Nutzer
    # aus der Oberflaeche aus — und genau dort soll er die Firmware einspielen.
    [ -z "$SERIAL" ] || [ -e "$SERIAL" ] || log "kein Stick an $SERIAL — es wird selbst gesucht."
    [ -n "$ADVERTISE" ] || log "ADVERTISE fehlt — HMCCU findet den XML-RPC-Dienst nur mit der IP dieses Rechners."

    set -- python3 -u "$QCCU/qccu.py" \
        --tables "$TABLES" \
        --firmware "$QCCU/firmware/q-culfw-CUL_V3.hex" \
        --devices "$STATE/qccu_devices.json" \
        --state   "$STATE/qccu_state.json" \
        ${SERIAL:+--serial "$SERIAL"} ${OWN_ADDR:+--own-addr "$OWN_ADDR"} \
        --rpc-port "$RPC_PORT" --rega-port "$REGA_PORT" --web-port "$WEB_PORT" \
        --json-port "$JSON_PORT" \
        --cul-port "$CUL_PORT"
    [ -n "$ADVERTISE" ] && set -- "$@" --advertise "$ADVERTISE"
    log "Zentrale startet — Web auf $WEB_PORT, XML-RPC auf $RPC_PORT, JSON-RPC auf $JSON_PORT."
    exec "$@"
}

# ---------------------------------------------------------------------------
# Firmware einspielen. Der Stick wird über die serielle Schnittstelle in den
# Bootlader geschickt; dort meldet er sich mit ANDERER USB-Kennung zurück,
# weshalb der Container Zugriff auf den USB-Bus braucht:
#   -v /dev/bus/usb:/dev/bus/usb --device-cgroup-rule='c 189:* rmw'
# ---------------------------------------------------------------------------
flash() {
    HEX=${1:-$QCCU/firmware/q-culfw-CUL_V3.hex}
    [ -f "$HEX" ] || die "Firmware-Datei fehlt: $HEX"
    [ -d /dev/bus/usb ] || die "kein Zugriff auf den USB-Bus — siehe README."

    if [ -e "$SERIAL" ]; then
        log "schicke den Stick in den Bootlader …"
        printf 'B01\r\n' > "$SERIAL" 2>/dev/null || true
        i=0
        while [ -e "$SERIAL" ] && [ $i -lt 20 ]; do i=$((i+1)); sleep 0.5; done
    else
        log "kein serieller Zugang — Stick muss bereits im Bootlader sein."
    fi

    i=0
    until dfu-programmer atmega32u4 get bootloader-version >/dev/null 2>&1; do
        i=$((i+1)); [ $i -gt 20 ] && die "Bootlader meldet sich nicht.
       Stick abziehen, Taster halten, einstecken — dann erneut."
        sleep 0.5
    done

    log "spiele $(basename "$HEX") ein …"
    dfu-programmer atmega32u4 erase
    dfu-programmer atmega32u4 flash "$HEX"
    dfu-programmer atmega32u4 start
    log "fertig — der Stick startet neu."
}

case "${1:-serve}" in
    serve) serve ;;
    setup) rm -f "$TABLES"/*.json; setup ;;
    flash) shift; flash "$@" ;;
    shell) shift; exec "${@:-/bin/sh}" ;;
    *)     exec "$@" ;;
esac
