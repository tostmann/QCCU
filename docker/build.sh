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
if [ -n "$QCULFW" ] && [ -f "$QCULFW/q-culfw-CUL_V3.hex" ]; then
    echo "== Firmware aus $QCULFW uebernehmen =="
    cp "$QCULFW/q-culfw-CUL_V3.hex" firmware/
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
