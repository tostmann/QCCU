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
# ⚠️ WER HIER FEHLT, WIRD STILL VERWORFEN. `bidcos_port` stand seit der
# Ports-Arbeit in `config.yaml`, wurde als Port angemeldet und beschrieben —
# und kam nie an: die Schnittstelle BidCos-RF liess sich als Erweiterung
# nicht einschalten, waehrend sie im nackten Container lief (dort haengt
# `--bidcos-port` am Aufruf). Eine neue Einstellung ist erst fertig, wenn ihr
# Name AUCH hier steht.
for name in ("SERIAL", "OWN_ADDR", "RPC_PORT", "REGA_PORT",
             "WEB_PORT", "JSON_PORT", "CUL_PORT", "ADVERTISE", "KENNUNG",
             "SOFORT_MELDEN", "ALT_PORTS", "LOCALHOST_ONLY",
             "BIDCOS_PORT", "BIDCOS_SENDEN", "BIDCOS_FREMD",
             "RAW_LOG"):
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
# Die zweite Schnittstelle (BidCos-RF). Vorgabe AUS — wer sie einschaltet,
# bekommt einen zweiten XML-RPC-Dienst, wie ihn eine Zentrale von eQ-3 dort
# anbietet. ⚠️ BIDCOS_SENDEN=1 laesst sie in den Funk eingreifen (quittieren
# und anlernen); ohne das liest sie nur mit. BIDCOS_FREMD nennt die Adressen
# fremder Zentralen im selben Funknetz, damit sie nicht als eigene gewuerfelt
# und ihr Verkehr nicht quittiert wird.
BIDCOS_PORT=${BIDCOS_PORT:-0}
# Ausweichports, wenn auf derselben Maschine schon eine OCCU laeuft.
ALT_PORTS=${ALT_PORTS:-0}
# Dienste nur ueber 127.0.0.1 — fuer FHEM/HA auf derselben Maschine.
LOCALHOST_ONLY=${LOCALHOST_ONLY:-0}
BIDCOS_SENDEN=${BIDCOS_SENDEN:-0}
BIDCOS_FREMD=${BIDCOS_FREMD:-}
ADVERTISE=${ADVERTISE:-}
KENNUNG=${KENNUNG:-}

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
    #
    # Der dritte Pfad traegt die KLASSISCHEN Geraetebeschreibungen (BidCoS).
    # Sie stehen NICHT im Archiv — das fuehrt nur Homematic IP —, sondern als
    # rund 230 XML-Dateien daneben. Genau diese liest auch der `rfd` einer
    # Zentrale von eQ-3 (`Device Description Dir = /firmware/rftypes`).
    ( cd "$WORK" && dpkg-deb --fsys-tarfile debmatic*.deb \
        | tar -x ./opt/HMServer/HMIPServer.jar \
               ./opt/HmIP/legacy-parameter-definition.config \
               ./firmware/rftypes ) 2>/dev/null
    JAR="$WORK/opt/HMServer/HMIPServer.jar"
    LPD="$WORK/opt/HmIP/legacy-parameter-definition.config"
    RFTYPES="$WORK/firmware/rftypes"
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
    if (b.startswith("device_") or b.startswith("channel_type_")) and b.endswith(".xml"):
        open(os.path.join(out, b), "wb").write(z.read(name)); n += 1
print(f"[qccu] {n} Geraete- und Kanaltypbeschreibungen entnommen")
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
        --extra-out     "$TABLES/extra_params.json" \
        -o "$TABLES/paramsets.json" || die "Zusammenführen fehlgeschlagen."

    # --- die klassische Seite (BidCoS) --------------------------------------
    # ⚠️ Kein `die` bei Fehlschlag: die BidCoS-Schnittstelle ist in der Vorgabe
    # AUS. Wer sie nicht einschaltet, soll wegen ihrer Tabellen keine Zentrale
    # verlieren — er bekommt eine Meldung, mehr nicht. Beim Einschalten sagt
    # QCCU dann selbst, dass sie fehlen.
    if [ -d "$RFTYPES" ]; then
        if python3 "$QCCU/build_bidcos.py" --rftypes "$RFTYPES" -o "$TABLES" \
             2>&1 | sed 's/^/       /'; then
            log "BidCoS-Tabellen gebaut."
        else
            log "BidCoS-Tabellen NICHT gebaut — die Schnittstelle BidCos-RF
       bliebe ohne Geraetetypen. Alles Uebrige ist davon unberuehrt."
        fi
    else
        log "Keine rftypes im Bündel — ohne sie gibt es keine BidCoS-Tabellen."
    fi

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
# Fehlt eine Tabelle — oder stammt sie aus einer Fassung bis 2026.8.12, die
# die Kanaltyp-Fassungen noch verschmolzen hat (erkennbar an fehlendem
# `extra_params.json`) —, wird neu gebaut. Alte Tabellen liest QCCU zwar
# weiter, liefert damit aber viel zu grosse Paramsets: an einer
# Schaltsteckdose 1087 Konfigurationsparameter statt 345, samt Farbverlaeufen.
#
# ⚠️ Die BidCoS-Tabellen zaehlen NUR mit, wenn die Schnittstelle eingeschaltet
# ist. Sonst muesste jede bestehende Anlage das 236-MB-Bündel erneut laden —
# fuer eine Schnittstelle, die sie gar nicht benutzt.
need_tables() {
    [ -f "$TABLES/paramsets.json" ] && [ -f "$TABLES/catalog.json" ] \
        && [ -f "$TABLES/extra_params.json" ] || return 1
    # ⚠️ Hier reicht die EXISTENZ der Datei nicht. Ein Katalog bis 2026.8.34
    # fuehrt die interne Verdrahtung der Geraete nicht (`links`), und ohne die
    # richtet QCCU nach dem Anlernen keine Verknuepfungen ein — ein Geraet
    # meldet sich dann kurz darauf wieder ab (am HmIP-BWTH-A gemessen). Der
    # Unterschied ist von aussen nicht zu sehen, deshalb wird in die Datei
    # geschaut statt auf ihren Namen.
    grep -q '"links"' "$TABLES/catalog.json" 2>/dev/null || return 1
    # Dasselbe fuer die Firmware-Baender (`varianten`, ab 2026.8.39): ohne sie
    # waehlt QCCU die Geraetebeschreibung nach der Kanalzahl statt nach der
    # Fassung des Geraets — und trifft damit bei Typen mit mehreren
    # Beschreibungen die falsche (HmIP-ASIR: die ohne interne Verdrahtung).
    grep -q '"varianten"' "$TABLES/catalog.json" 2>/dev/null || return 1
    # Und die Form der Stellparameter (`FASSUNG`, ab 2026.8.45): ohne sie
    # baut QCCU fuer einen Rollladen oder eine Markise das Pegelbyte des
    # Schaltaktors — wohlgeformt und falsch. Alte Tabellen tragen das Feld
    # nicht; sie werden deshalb neu gebaut.
    grep -q '"FASSUNG"' "$TABLES/paramsets.json" 2>/dev/null || return 1
    # Und die Adresse der Konfigurationsparameter (`ADRESSE`, ab 2026.9.2):
    # ohne sie kann putParamset MASTER nichts an das Gerät schreiben.
    grep -q '"ADRESSE"' "$TABLES/paramsets.json" 2>/dev/null || return 1
    # Link-Rollen der Kanaltypen (ab 2026.9.2): ohne sie kann QCCU keine
    # Verknuepfung zur Zentrale anlegen — ein Fensterkontakt meldet dann
    # keine Ereignisse.
    [ -f "$TABLES/kanalrollen.json" ] || return 1
    if [ "${BIDCOS_PORT:-0}" != "0" ]; then
        [ -f "$TABLES/bidcos_types.json" ] \
            && [ -f "$TABLES/bidcos_layouts.json" ] \
            && [ -f "$TABLES/bidcos_paramsets.json" ] || return 1
    fi
    return 0
}

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
        if [ -f "$TABLES/paramsets.json" ]; then
            log "die Gerätetabellen stammen aus einer älteren Fassung und werden"
            log "neu angelegt (Kanalfassungen je Gerätetyp seit 2026.8.13,"
            log "interne Verdrahtung der Geräte seit 2026.8.35)."
        else
            log "erster Start — die Gerätetabellen werden angelegt."
        fi
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
        --cul-port "$CUL_PORT" \
        --bidcos-port "$BIDCOS_PORT"
    [ "$ALT_PORTS" = "1" ] && set -- "$@" --alt-ports
    [ "$LOCALHOST_ONLY" = "1" ] && set -- "$@" --localhost
    [ "$BIDCOS_SENDEN" = "1" ] && set -- "$@" --bidcos-senden
    [ -n "$BIDCOS_FREMD" ] && set -- "$@" --bidcos-fremd "$BIDCOS_FREMD"
    [ -n "$ADVERTISE" ] && set -- "$@" --advertise "$ADVERTISE"
    [ -n "$KENNUNG" ] && set -- "$@" --kennung "$KENNUNG"
    # Ohne Posteingang (FHEM/HMCCU): frisch Angelerntes sofort melden.
    [ "$SOFORT_MELDEN" = "1" ] && set -- "$@" --sofort-melden
    # Rohmitschnitt zur Fehlersuche. Er liegt neben den Tabellen in /data und
    # bricht bei 8 MB auf `.1` um — hoechstens zwei Dateien.
    [ "$RAW_LOG" = "1" ] && set -- "$@" --raw-log /data/luft.log
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
