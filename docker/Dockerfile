# QCCU im Container — Zentralen-Nachbildung für den Quiche-Funkstack.
# Copyright (c) 2026 Dirk Tostmann.
#
# Die Gerätetabellen entstehen beim ersten Start auf dem Rechner des Nutzers.
#
# Der Nutzer muss dafür nichts tun und nichts besorgen.

# ── Stufe 1: die Ausleseprogramme übersetzen ─────────────────────────
# Eigene Stufe, damit im Auslieferungsabbild nur das JRE liegt und kein JDK.
FROM debian:bookworm-slim AS uebersetzer
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        default-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
COPY tables/jar_param_dump/DumpParamsRpc.java tables/jar_param_dump/DumpSdt.java /quelle/
# -encoding UTF-8: im Abbild ist kein UTF-8-Locale gesetzt, javac nähme sonst
# US-ASCII an und scheitert an den deutschen Kommentaren.
RUN javac -encoding UTF-8 -d /uebersetzt \
        /quelle/DumpParamsRpc.java /quelle/DumpSdt.java \
    && ls -l /uebersetzt

# ── Stufe 2: das Abbild ─────────────────────────────────────────────────────
FROM debian:bookworm-slim

# Feste Fassung, nicht "latest".
ARG DEBMATIC_VERSION=3.85.7-123
ENV DEBMATIC_VERSION=${DEBMATIC_VERSION}

# python3-serial für den Stick, das JRE für den einmaligen Auslesevorgang,
# dfu-programmer zum Einspielen der Stick-Firmware.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-serial \
        default-jre-headless \
        dfu-programmer \
        curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Bezugsquelle eintragen — die Konfiguration ist kein fremder Bestandteil,
# geladen wird erst zur Laufzeit.
# -4: der Bezugsserver hat A- und AAAA-Eintrag; in Containern ohne IPv6
# scheitert der Verbindungsaufbau sonst am AAAA-Eintrag.
RUN mkdir -p /etc/apt/keyrings \
    && curl -4 -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 \
         https://apt.debmatic.de/debmatic/public.key \
         > /etc/apt/keyrings/debmatic.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/debmatic.asc] https://apt.debmatic.de/debmatic stable main" \
         > /etc/apt/sources.list.d/debmatic.list

WORKDIR /opt/qccu

# Die Zentralen-Nachbildung
COPY qccu.py qccu_radio.py qccu_web.py qccu_jsonrpc.py sticker_decode.py qccu_firmware.py qccu_cul.py ./
# Die Werkzeuge, die die Tabellen erzeugen
COPY tables/build_paramsets.py tables/build_catalog.py ./
COPY --from=uebersetzer /uebersetzt ./jarparam/
COPY assets/busware_logo.png assets/busware_logo_dark.png assets/busware_icon.png ./
# Ergaenzungen: die wenigen Parameter, die in keiner Definition stehen.
COPY tables/paramsets_ergaenzung.json ./
# Die Stick-Firmware, die `flash` einspielt
COPY firmware/q-culfw-CUL_V3.hex firmware/THIRD-PARTY-NOTICES.md ./firmware/

COPY docker/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME /data
EXPOSE 2000 2010 8181 8080 8082

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
