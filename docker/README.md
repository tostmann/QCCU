# Abbild bauen und betreiben

Start und Einstellungen: [../README.md](../README.md); die Anbindung je
System unter [../docs/](../docs/).

## Bauen

```sh
./docker/build.sh                                  # tostmann/qccu:dev für diese Maschine
DEBMATIC_VERSION=3.85.8-124 ./docker/build.sh      # andere debmatic-Fassung
QCULFW=/pfad/zum/q-culfw ./docker/build.sh         # neuere Stick-Firmware übernehmen
TAG=tostmann/qccu:2026.8.12 ./docker/build.sh       # eigene Marke
```

`build.sh` nimmt `firmware/q-culfw-CUL_V3.hex` aus dem Repo; mit `QCULFW=<pfad>` wird `q-culfw-CUL_V3.hex` und `THIRD-PARTY-NOTICES.md` von dort kopiert. Bei Host-MTU < 1500 baut es mit `--network=host`.

Zwei Stufen: `javac` für die beiden Auslesewerkzeuge, danach das Abbild mit JRE, `python3-serial`, `dfu-programmer`. Das Abbild enthält keine Gerätetabellen und kein debmatic-Material; beides entsteht beim ersten `serve` in `/data`.

## Betriebsarten des Einstiegspunkts

| Aufruf | Wirkung |
|---|---|
| `serve` (Vorgabe) | Tabellen anlegen, falls sie fehlen, dann Zentrale starten |
| `setup` | Tabellen neu anlegen |
| `flash [datei.hex]` | Stick-Firmware von der Kommandozeile einspielen; normaler Weg ist die Weboberfläche |
| `shell` | Eingabeaufforderung |

Scheitert `setup` am Paketbezug, obwohl eine Netzverbindung besteht: MTU des
Docker-Netzes gegen die des Anschlusses prüfen (dieselbe Falle wie beim Bauen).
`--network host` umgeht sie für den einen Aufruf.

```sh
docker run --rm --network host -v qccu-data:/data tostmann/qccu setup
docker run --rm -v /dev:/dev --device-cgroup-rule='c 166:* rmw' --device-cgroup-rule='c 189:* rmw' tostmann/qccu flash
```

## Datenablage

| Pfad | Inhalt |
|---|---|
| `/data/tables/` | `catalog.json`, `paramsets.json`, `sdt_table.json` – aus dem debmatic-Paket erzeugt |
| `/data/state/` | `qccu_devices.json` (Geräte, Rückrufe), `qccu_state.json` (Zähler) |
| `/data/options.json` | nur als Home-Assistant-Erweiterung: die Einstellungen des Supervisors. Der Einstiegspunkt liest sie; gesetzte Umgebungsvariablen haben Vorrang. |
