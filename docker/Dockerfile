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
COPY qccu.py qccu_radio.py qccu_web.py qccu_jsonrpc.py sticker_decode.py qccu_firmware.py qccu_cul.py qccu_integration.py ./
# Die Werkzeuge, die die Tabellen erzeugen
COPY tables/build_paramsets.py tables/build_catalog.py ./
COPY --from=uebersetzer /uebersetzt ./jarparam/
COPY assets/busmatic_logo.png assets/busmatic_logo_dark.png assets/busware_icon.png ./
# Ergaenzungen: die wenigen Parameter, die in keiner Definition stehen.
COPY tables/paramsets_ergaenzung.json ./
# Die Stick-Firmware, die `flash` einspielt — samt Lizenz und Vermerken
COPY firmware/q-culfw-CUL_V3.hex firmware/LICENSE firmware/THIRD-PARTY-NOTICES.md ./firmware/

# ── Die Integration „Homematic(IP) Local" mitliefern ──────────────────────
# Wer QCCU als Erweiterung installiert, braucht in Home Assistant zusaetzlich
# diese Integration. Sie liegt hier UNVERAENDERT im Abbild (MIT, LICENSE
# daneben) und wird beim Start nach `custom_components/` gelegt, wenn dort
# nichts Neueres steht — damit ist die Installation ein Schritt statt zwei.
#
# ⚠️ Fassung fest verdrahtet, nicht `master`: ausgeliefert wird nur, was wir
# gegen unsere Zentrale geprueft haben. Wer sie hochzieht, prueft erneut —
# Home Assistant ≥ 2026.7.0 ist Bedingung der Integration selbst.
# Die vier Python-Abhaengigkeiten (aiohomematic &c.) holt Home Assistant beim
# Laden selbst von PyPI; sie gehoeren NICHT hierher.
ARG HMIP_LOCAL_VERSION=2.9.1
# ⚠️ Bricht die Pruefsumme, wird NICHT still weitergebaut: entweder wurde am
# Tag gedreht, oder GitHub hat das Archiv anders gepackt. Beides will man
# sehen, bevor fremder Code ins Abbild wandert.
ARG HMIP_LOCAL_SHA256=8d6c1e4cb593b01739d6b6584c125dee71c1b1ef0e6418810f51ef7bb32a148c
RUN set -eu; \
    url="https://github.com/SukramJ/homematicip_local/archive/refs/tags/${HMIP_LOCAL_VERSION}.tar.gz"; \
    curl -fsSL "$url" -o /tmp/hmip.tgz; \
    echo "${HMIP_LOCAL_SHA256}  /tmp/hmip.tgz" | sha256sum -c -; \
    mkdir -p /tmp/hmip /opt/qccu/integration; \
    tar xzf /tmp/hmip.tgz -C /tmp/hmip; \
    src="/tmp/hmip/homematicip_local-${HMIP_LOCAL_VERSION}"; \
    cp -a "$src/custom_components/homematicip_local" /opt/qccu/integration/; \
    cp "$src/LICENSE" /opt/qccu/integration/homematicip_local/LICENSE; \
    rm -rf /opt/qccu/integration/homematicip_local/.idea; \
    find /opt/qccu/integration -name '__pycache__' -type d -prune -exec rm -rf {} +; \
    rm -rf /tmp/hmip /tmp/hmip.tgz; \
    python3 -c "import json,sys; v=json.load(open('/opt/qccu/integration/homematicip_local/manifest.json'))['version']; sys.exit(0 if v=='${HMIP_LOCAL_VERSION}' else 1)"

COPY docker/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME /data
EXPOSE 2000 2010 8181 8080 8082

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
