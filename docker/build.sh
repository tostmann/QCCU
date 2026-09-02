#!/bin/sh
# Baut das QCCU-Abbild.
# Copyright (c) 2026 Dirk Tostmann.
#
#   ./docker/build.sh                      baut fuer diese Maschine
#   DEBMATIC_VERSION=3.85.8-124 ./docker/build.sh   andere Fassung
#
set -e
cd "$(dirname "$0")/.."

TAG=${TAG:-tostmann/qccu:dev}
DEBMATIC_VERSION=${DEBMATIC_VERSION:-3.85.7-123}

# QCULFW=<pfad> ./docker/build.sh uebernimmt eine frischere Firmware.
#
# ⚠️ Wer $QCULFW setzt, WILL die Firmware von dort. Findet sich dort keine,
# ist das ein Fehler und kein Grund weiterzumachen: bis zum 02.09.2026 stand
# hier nur der alte Pfad, und seit der AVR-Bau nach avr/ umgezogen ist, fiel
# die Bedingung still durch. Das Abbild behielt die mitgelieferte 2.0.72 —
# eine Firmware ohne `mb`, an der jedes HmIP-Batteriegeraet unstellbar ist,
# und die Pruefung eine Zeile weiter merkte nichts, weil die alte Datei ja
# noch dalag. Ein stilles Ueberspringen ist hier teurer als ein Abbruch.
if [ -n "$QCULFW" ]; then
    QHEX=""
    for k in "$QCULFW/avr/q-culfw-CUL_V3.hex" "$QCULFW/q-culfw-CUL_V3.hex"; do
        [ -f "$k" ] && { QHEX="$k"; break; }
    done
    if [ -z "$QHEX" ]; then
        echo "QCULFW=$QCULFW gesetzt, aber dort liegt keine Firmware." >&2
        echo "Gesucht: $QCULFW/avr/q-culfw-CUL_V3.hex und $QCULFW/q-culfw-CUL_V3.hex" >&2
        echo "Erst im q-culfw-Baum 'make' aufrufen." >&2
        exit 1
    fi
    echo "== Firmware aus $QHEX uebernehmen =="
    cp "$QHEX" firmware/q-culfw-CUL_V3.hex
    [ -f "$QCULFW/THIRD-PARTY-NOTICES.md" ] && cp "$QCULFW/THIRD-PARTY-NOTICES.md" firmware/
fi
[ -f firmware/q-culfw-CUL_V3.hex ] || { echo "Firmware fehlt: firmware/q-culfw-CUL_V3.hex" >&2; exit 1; }

# Bei Host-MTU < 1500 mit dem Netz des Hosts bauen.
NET=""
HOST_MTU=$(ip -o route get 1.1.1.1 2>/dev/null | grep -o 'dev [^ ]*' | head -1 | cut -d' ' -f2 \
           | xargs -r -I{} sh -c 'ip -o link show {} 2>/dev/null' | grep -o 'mtu [0-9]*' | cut -d' ' -f2)
if [ -n "$HOST_MTU" ] && [ "$HOST_MTU" -lt 1500 ]; then
    echo "== Host-MTU $HOST_MTU < 1500 — es wird mit dem Netz des Hosts gebaut =="
    NET="--network=host"
fi

echo "== Abbild bauen ($DEBMATIC_VERSION) =="
docker build $NET \
    -f docker/Dockerfile \
    --build-arg "DEBMATIC_VERSION=$DEBMATIC_VERSION" \
    -t "$TAG" \
    .

echo "== fertig: $TAG =="
docker images "$TAG" --format '   {{.Repository}}:{{.Tag}}  {{.Size}}'
