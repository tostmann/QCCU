# Die serielle Schnittstelle von q-culfw

Dieses Dokument beschreibt den Befehlssatz der Firmware **q-culfw**, so wie sie
in [`firmware/q-culfw-CUL_V3.hex`](../firmware/) mitgeliefert wird (Fassung
**2.0.92**, aus dem Quelltext dieser Fassung gelesen). Es richtet sich an alle,
die den Stick **ohne QCCU** ansprechen wollen — ein eigenes FHEM-Modul, ein
Skript, ein anderer Wirt. QCCU selbst ist ein Wirt wie jeder andere: es
spricht ausschließlich über diese Schnittstelle mit dem Stick.

Was QCCU davon an seinem CUL-Zugang (Port 2000) **nach außen** durchreicht, ist
eine Teilmenge und steht in [SCHNITTSTELLEN.md](SCHNITTSTELLEN.md); dort geht es
um den culfw-Dialekt für FHEM. Hier geht es um den Stick selbst.

> ⚠️ **Der Stick ist kein CUL mit culfw.** Er kennt die culfw-Befehle nur so
> weit, wie FHEMs `00_CUL.pm` sie für die Anmeldung und für `rfmode HomeMatic`
> braucht (`V`, `?`, `X`, `T01`, `C`, `Ar`/`Ax`/`As`). Alles Übrige — FS20, FHT,
> EM, HMS, wMBus, Moritz — gibt es nicht. Wer einen culfw-Befehl schickt, den
> es hier nicht gibt, bekommt `? ` zurück.

---

## Anschluss und Zeilenform

| | |
|---|---|
| USB | CDC-ACM (virtuelle serielle Schnittstelle), Baudrate ohne Bedeutung, 8N1 |
| Kennung | VID `03EB`, PID `2069` (Laborkennung — **nicht** die `03EB:204B` eines culfw-CUL), Hersteller `busware.de`, Produkt `q-culfw`, Seriennummer = Werkskennung des Bausteins; unter Linux `/dev/serial/by-id/usb-busware.de_q-culfw_<serial>-if00` |
| Befehl | ASCII, abgeschlossen mit CR **oder** LF (CR LF geht auch, die leere zweite Zeile wird verworfen); **höchstens 131 Zeichen**, was darüber hinausgeht, wird stillschweigend abgeschnitten |
| Antwort | Zeilen mit CR LF; kein Echo der Eingabe |
| Beim Start | der Stick meldet einmalig seine Fassung: `V q-culfw 2.0.92` |

**Das Öffnen der Schnittstelle setzt den Stick nicht zurück.** DTR/RTS werden
quittiert, sonst nichts. Ein Wirt findet also den Zustand vor, den die vorige
Sitzung hinterlassen hat — oder den Startzustand, wenn der Stick seither neu
angesteckt wurde. Darum setzt ein Wirt **jeden Schalter, auf den er sich
verlässt, bei jeder Anmeldung selbst** (siehe [Mindestfolgen](#mindestfolgen-für-einen-wirt)).

**Die Ausgabe ist asynchron.** Empfangene Frames (`A…`, `P…`, `PM…`, `PK…`,
`PH…`) kommen jederzeit, auch zwischen Befehl und Antwort. Ein Wirt liest
zeilenweise und ordnet nach dem Zeilenanfang zu; „die nächste Zeile ist die
Antwort" ist ein Fehler. Auf `m` antwortet der Stick mit **drei** Zeilen — wer
nach der ersten aufhört, hat die beiden anderen als Antwort auf seinen
nächsten Befehl.

Das Präfix `P` ist vor den Befehlen `m V ? T X B C W` zulässig und wird
abgestreift: `Pm…` ist `m…`, `PV` ist `V`. Damit bedient ein Werkzeug, das für
den HmIP-Dialekt von culfw (`P…`) geschrieben ist, diesen Stick mit denselben
Zeilen. **Nicht** vor `A…`: `PAr` ergibt `? `.

---

## Startzustand

Nach Reset oder Anstecken gilt:

| Was | Zustand | Befehl |
|---|---|---|
| HmIP-MAC-Schicht | **aus** | `mE1` |
| Rolle / Funkadresse | Zentrale / `000000` | `mC` bzw. `mD`, `mA<6hex>` |
| Netzwerkschlüssel | aus dem EEPROM, falls je einer angelegt wurde (`key=1` in der Statuszeile), sonst keiner | `mC` erzeugt beim ersten Mal einen — **nur wenn eine Kennung da ist** (`mG`) |
| Meldung BidCoS (`A`-Zeilen) | aus | `Ar` |
| Meldung HmIP roh (`P`-Zeilen) | aus | `Pr` |
| Nur-Lesen-Riegel | **zu** — der Stick sendet nichts, bis ihn jemand öffnet | `mL0` oder `Ar` |
| Quittungen HmIP | `mQ1` (wie die Zentrale) | |
| Netz-Haushalt quittieren | an | `mN0` |
| Router-Rolle | aus | `mF1` |
| Frequenzdiagnose (`PH`-Zeilen) | aus | `mH1` |
| Sendezeit-Konto | an, halb voll (450 von 900) | `mX` |
| BidCoS-Vorlauf | an, 360 ms | `mU` |
| HmIP-Vorlauf | 360 / 360 ms, Weckkanal `21717A` (869,52 MHz), Zustellabstand 30 ms | `mU` |
| BidCoS-Selbstquittung | aus, keine eigene Adresse | `Aa`, `Aq` |
| Meldeform (culfw `X`) | `21` | `X<hh>` |
| FHT-Hauscode | `0000` | `T01<hhhh>` |

**Was den Reset überlebt (EEPROM):** Kennung (SGTIN) und Aufkleberschlüssel,
der Hauptschlüssel, Netzwerkschlüssel samt Merker, der Sendezähler (in einem
Ring, geschrieben beim Start und danach alle 1024 Schritte — ohne Schalter,
immer). Die beim Anlernen zugeteilte Adresse und die Zentralenadresse werden
zwar abgelegt, nach einem Reset aber **nicht wieder geladen**: der Stick
startet immer als Zentrale mit `000000`. Wer ihn als Gerät betreibt, setzt
nach dem Anstecken `mD` und `mA<addr aus ANGELERNT>` nach; die Rückmeldung des
Relais an die Zentrale bleibt bis zum nächsten Anlernen aus (bei Taster und
`mO`) bzw. geht an `000000` (auf einen Funk-Schaltbefehl). **Alles andere ist
flüchtig:** jeder Schalter oben, Rolle und Adresse, Registerwerte über `W`.

---

## Befehle

### culfw-kompatibel (für die Anmeldung von FHEM)

| Befehl | Wirkung | Antwort |
|---|---|---|
| `V` | Fassung | `V q-culfw 2.0.92` |
| `?` | Befehlsbuchstaben im culfw-Format | `? (? is unknown) Use one of A B C P T V W X m` |
| `X` | Meldeform und Restkonto (FHEM: `credit10ms`) | `21 <konto>` — dezimal, in 10-ms-Einheiten; bei abgeschaltetem Konto (`mX0`) steht dort `900` |
| `X<hh>` | Meldeform setzen (FHEM schickt `X21`). Wird geführt, aber nicht ausgewertet: der Empfangspegel hängt hier an jeder Zeile | keine; bei ungültigem Argument `X ERR` |
| `T01` | FHT-Hauscode lesen | `0000` (vier Hexziffern) |
| `T01<hhhh>` | FHT-Hauscode setzen — nur damit FHEMs Anmeldung durchläuft, es gibt kein FHT | keine; ein ungültiges Argument wird ignoriert und der aktuelle Hauscode ausgegeben |
| `T…` sonst | | `? ` |
| `C<hh>` | CC1101-Register lesen; ab `30` die Statusregister (`C32` = FREQEST, `C34` = RSSI …) | `C<hh>=<hh>` — z. B. `C0D=21`; ungültiges Argument `C ERR` |
| `W<hh><hh>` | CC1101-Register **flüchtig** schreiben, nur `00`…`2E`; Reset stellt den Registersatz wieder her | `W<hh>=<hh>` mit dem zurückgelesenen Wert, sonst `W ERR` |
| `B01` | in den Bootlader springen (DFU) | `B bootlader`, danach ist der Stick weg |
| `B…` sonst | | `B ERR (B01)` |
| leere Zeile | | keine |
| alles Unbekannte | | `? ` |

`C<hh>` liest ab `30` mit gesetztem Burst-Bit; **`C3F` ist der RX-FIFO** und
zöge ein Byte aus einem laufenden Empfang — nicht im Betrieb abfragen.

### BidCoS / AskSin (`A…`)

| Befehl | Wirkung | Antwort |
|---|---|---|
| `Ar` | BidCoS-Meldung ein **und Nur-Lesen-Riegel auf** — wer `Ar` schickt, betreibt den Stick als CUL und hat das Senden damit erklärt | keine (wie culfw) |
| `Ax` | BidCoS-Meldung aus. Der Riegel bleibt offen | keine |
| `As<hex>` | Frame senden: `<len><frame…>` in Hex, das Längenbyte muss zur Zahl der folgenden Bytes passen. Ist im Flag-Byte (drittes Byte hinter dem Längenbyte) **Bit `0x10`** gesetzt, geht ein Vorlauf von 360 ms voraus (schlafendes Batteriegerät) | **bei Erfolg nichts**; `LENERR` (Länge passt nicht), `NUR-LESEN (mL0 gibt frei)` (Riegel zu), `Ps ERR LOVF` (Konto leer, nichts gesendet), `Ps ERR` (Funkfehler) |
| `Aa<6hex>` / `Aa` | eigene BidCoS-Adresse setzen / zeigen — der Stick kennt sie sonst nicht, sie steht in keinem Frame | `Aa <6hex>` |
| `Aq1` / `Aq0` / `Aq` | Selbstquittung an / aus / zeigen (Vorgabe **aus**, s. u.) | `Aq 1`, ergänzt um ` OHNE ADRESSE (Aa)`, solange keine Adresse gesetzt ist |
| `Aw<6hex>` / `Aw000000` / `Aw` | Wach-Bit: Quittungen an diese Gegenstelle tragen `0x01` („bleib wach") / aus / zeigen | `Aw <6hex>` |

**Selbstquittung (`Aq1`).** Frames an die eigene Adresse (`Aa`), die eine
Quittung verlangen (Flag `0x20`), quittiert der Stick 100 ms nach dem Empfang
selbst — außer auf Quittungen (`02`), AES (`03`) und Geräteinfo (`00`). Sie
greift nur, solange die BidCoS-Meldung an ist (`Ar`): sie hängt am selben
Empfangspfad wie die `A`-Zeile. Baut
der Wirt dieselbe Quittung trotzdem (gleiche Nummer, gleiches Ziel, binnen
1,5 s), **schluckt der Stick sie stillschweigend** und zählt sie als `akdop`.
Die Vorgabe ist aus, weil FHEMs `CUL_HM` selbst quittiert und das binnen 100 ms
schafft; `Aq1` ist für Wirte gedacht, die die Frist nicht halten können.
`Aw` gehört dazu, sobald Wakeup-Geräte im Spiel sind: ob für ein Gerät noch
etwas ansteht, weiß nur der Wirt — er setzt `Aw<addr>`, solange seine
Warteschlange für dieses Gerät nicht leer ist, und `Aw000000` danach.

Die Quittung selbst — und AES — bleibt bei diesem Weg Sache des Wirts, so wie
`CUL_HM` es für einen CUL vorsieht. Der Stick sendet, was `As` ihm gibt, und
prüft nur die Länge.

### HmIP roh (`P…`)

| Befehl | Wirkung | Antwort |
|---|---|---|
| `Pr` | rohe HmIP-Meldung ein (`P`-Zeilen). Öffnet den Riegel **nicht** | `Pr ok` |
| `Px` | rohe HmIP-Meldung aus | `Px ok` |
| `Ps<hex>` | rohen Luftframe senden, wie `As`, aber nie mit Vorlauf | wie `As` |

`Ps` ist für Wiedergabe und für Sonderfälle gedacht. Wer HmIP spricht, sendet
über die MAC-Schicht (`ms`, `mT`, `mb`): dort baut der Stick Kopf, Zähler,
Verschlüsselung und Prüfsumme selbst.

### HmIP-MAC-Schicht (`m…`)

Alle `m`-Befehle antworten mit Zeilen, die mit `Pm ` beginnen. Ein unbekannter
Unterbefehl gibt `Pm ERR`. Bei den Ein/Aus-Schaltern (`mE`, `mQ`, `mP`, `mF`,
`mN`, `mH`, `mL`) gilt **jedes Zeichen außer `0` als „an"** — `mE` ohne Ziffer
wirkt wie `mE1`.

#### Zustand und Konfiguration

| Befehl | Wirkung | Antwort |
|---|---|---|
| `m` (auch `m?`) | Zustand, Konto und Zählwerke | **drei Zeilen**, siehe [Statuszeilen](#statuszeilen-m) |
| `mE1` / `mE0` | MAC-Schicht ein / aus. Erst damit werden HmIP-Frames geprüft, entschlüsselt, quittiert und als `PM`-Zeile gemeldet | `Pm E=1` |
| `mC` / `mD` | Rolle Zentrale / Gerät. `mC` legt beim **ersten** Aufruf einen Netzwerkschlüssel an — **nur, wenn eine Kennung da ist** (`mG`): der Schlüssel wird gegen die Kennung verpackt. Ohne Kennung meldet `mC` die Rolle und legt **stillschweigend nichts** an; `m` zeigt dann weiter `key=0` | `Pm rolle=Zentrale` / `Pm rolle=Geraet`; beim ersten Anlegen **danach** die Zeile `Pm Netzwerkschluessel erzeugt` |
| `mA<6hex>` | eigene Funkadresse | `Pm addr=<6hex>`, sonst `Pm ERR addr` |
| `mQ0` / `mQ1` / `mQ2` | Quittungen: aus / **wie die Zentrale** (Kurzquittung auf jeden Unicast an uns; ct=4 nur auf die Anlernbestätigung und quittungspflichtige Rundrufe) / ct=4 zusätzlich auf jeden verschlüsselten Frame an uns | `Pm Q=1` |
| `mP1` / `mP0` | piggybackACK-Bit im Kopf künftiger Sendungen | `Pm P=1` |
| `mF1` / `mF0` | Router-Rolle: Durchgangsverkehr an sein Ziel weiterreichen (zählt `fwd`) | `Pm F=1` |
| `mN1` / `mN0` | Netz-Haushalt: die Nachbarschaftsmeldung eines Geräts an die Sammeladresse quittieren, wie die echte Zentrale (Vorgabe an) | `Pm N=1` |
| `mS<8hex>` | MAC-Sequenzzähler setzen | `Pm sn=<8hex>`, sonst `Pm ERR sn` |
| `mKX` | Netzwerkschlüssel **verwerfen** — danach legt `mC` einen neuen an, und jedes angelernte Gerät ist ausgesperrt. Wirkt auf die Ablage; im Arbeitsspeicher bleibt der alte Schlüssel bis zum nächsten `mC` oder Reset (`key=1` steht bis dahin weiter in der Statuszeile) | `Pm Netzwerkschluessel verworfen — alle Geraete neu anlernen` |
| `mK…` sonst | einen Schlüssel von außen **setzen gibt es nicht** — er entsteht in der Zentrale oder kommt beim Anlernen | `Pm key wird nicht von aussen gesetzt — Anlernen (mGP)` |
| `mL0` / `mL1` | Nur-Lesen-Riegel auf / zu. Sendende Verben (`ms`/`mT`/`mR`/`mb`, `As`/`Ps`, `mGP`) sind bei geschlossenem Riegel gesperrt | `Pm L=0 SENDEN FREIGEGEBEN` / `Pm L=1 nur lesen, Senden gesperrt` |
| `mX` / `mX0` / `mX1` | Sendezeit-Konto zeigen / Bremse aus / an. Aus ist für den Messplatz, nicht für den Betrieb | `Pm budget=<0\|1> credit=<n>/900 lovf=<n>` |
| `mZ` | Zählwerke und Rauschboden zurücksetzen | `Pm zaehler=0` |

#### Senden

| Befehl | Wirkung |
|---|---|
| `ms<dst6hex><hex>` | Anwendungsdaten (ct=0) an `dst`, verschlüsselt (sec=1) |
| `mT<ct><sec><dst6hex><hex>` | beliebiger Frame: `ct` und `sec` je **eine** Hexziffer (siehe [Rahmentypen](#rahmentypen-ct-und-sicherung-sec)) |
| `mR<dst6hex><via6hex><hex>` | geroutet: an `dst` über den Router `via` |
| `mb<stufe><verb>…` | dieselbe Sendung **mit Vorlauf**: `stufe` = `0` (ohne — für die Gegenprobe), `1`, `3`; `verb` = `s`, `T` oder `R` mit demselben Rest wie oben, z. B. `mb1s<dst><hex>` |
| `mbL<hh><verb>…` | wie `mb`, die Stufe aber aus dem **Hörertyp** abgeleitet — `<hh>` ist Byte 31 des Anlernrufs, unverändert (die oberen vier Bit werden ausgeblendet): Typ 1 und 9 → Stufe 1, Typ 3 und 11 → Stufe 3, alles andere → 0 |

**Genau eine Urteilszeile je Auftrag**, damit der Wirt sie seinem Befehl
zuordnen kann:

| Antwort | Bedeutung |
|---|---|
| `Pm tx ok` | gesendet (bei `mb` mit Weckkanal: beide Rahmen) |
| `Pm ERR LOVF` | Sendezeit-Konto reicht nicht — **nichts** ging hinaus |
| `Pm ERR tx` | Funkfehler |
| `Pm NUR-LESEN (mL0 gibt frei)` | Riegel zu |
| `Pm ERR bauen` | die MAC-Schicht hat aus den Angaben keinen Frame gebaut |
| `Pm ERR dst` / `Pm ERR ct/sec` / `Pm ERR stufe` / `Pm ERR verb` / `Pm ERR hoerertyp` | Syntax |

Die Quittung des Geräts kommt danach von selbst als `PM`-Zeile (`ct=4`,
`ack=<sn>`); die Kurzquittung als `PK`-Zeile. Beides ist **nicht** Teil des
Urteils — `tx ok` heißt „hinausgegangen", nicht „angekommen".

Bei `ct=3` (ROUTE_MGMT) setzt der Stick das Kopfbyte auf `0x83` (Sprungweite 1),
wie die echte Zentrale; mit der Unicast-Vorgabe `0x8E` wiederholte ein Gerät
seine Anlernbestätigung und suchte einen Router.

#### Kennung und Anlernen (der Stick als Gerät)

| Befehl | Wirkung | Antwort |
|---|---|---|
| `mG` | Kennung zeigen | `Pm sgtin=<24hex>`, `Pm herkunft fassung=… nehmer=… geraeteart=…`, `Pm werkskennung=<hex>`, `Pm key=<32hex>` (Aufkleberschlüssel), `Pm beide Werte in der EIGENEN Zentrale eintragen (Geraet anlernen)`; ohne Kennung `Pm keine Kennung — mGN erzeugt eine` |
| `mGN` | Kennung und Aufkleberschlüssel **neu würfeln** (Zufall aus dem Funkrauschen) — die alte ist danach weg, auch aus Sicht einer Zentrale, in der sie schon steht. ⚠️ **Verwirft zugleich den Netzwerkschlüssel** (die Kennung ist das Salz seiner Ablage) — Wirkung wie `mKX`, jedes angelernte Gerät ist danach ausgesperrt. Nur auf einem Stick ohne Kennung, oder bewusst | `Pm erzeuge Kennung...`, dann wie `mG` |
| `mGP` | Anlernen anstoßen, wie ein langer Tastendruck am Gerät: eine Minute lang alle acht Sekunden einen Anlernruf; Kennung und Schlüssel müssen vorher in der Zentrale eingetragen sein. Setzt die MAC-Schicht neu auf: Rolle Gerät, Adresse aus der Kennung, Schicht an (`mE1` implizit); der Schlüssel im Arbeitsspeicher ist bis zum Angebot der Zentrale weg — bleibt das Angebot aus, bis zum Reset (`m` zeigt dann `key=0`, obwohl einer im EEPROM liegt) | `Pm Anlernen laeuft — Kennung muss in der Zentrale stehen`; dann `Pm ANGELERNT addr=<6hex>` und `Pm Netzwerkschluessel gesichert — mitlesen ist jetzt moeglich`, gefolgt von `Pm >> Anlernbestaetigung`, `Pm >> Startmeldung`, `Pm >> Kanalzustaende`, `Pm angemeldet`; oder `Pm Anlernen: keine Antwort`. Ohne Kennung `Pm keine Kennung — erst mGN`, bei zu Riegel `Pm NUR-LESEN (mL0 gibt frei)` |
| `mGK` | den abgelegten Netzwerkschlüssel **verschlüsselt** ausgeben (gegen den Aufkleberschlüssel, mit Salz und MIC) — im Klartext gibt der Stick ihn nie heraus | `Pm packe ein...`, `Pm nwk.enc=<salz 8><ct 32><mic 8>`, `Pm auspacken mit dem Aufkleberschluessel`; ohne Schlüssel `Pm kein Netzwerkschluessel — erst anlernen (mGP)` |
| `mO` / `mO0` / `mO1` / `mOT` | das nachgebildete Relais zeigen / aus / ein / umschalten (Gerät-Rolle: ein verschlüsselter Schaltbefehl `86 <n> 02 <kanal> <wert>` an uns schaltet es ebenfalls und meldet den Kanalzustand zurück) | `Pm relais=<0\|1>` |
| `mB` | Taster suchen: 20 s lang jede Änderung an den Eingangsregistern melden (Werkstatt) | `Pm ruhe B=… C=… D=… E=… F=…  -- 20 s Zeit, jetzt druecken`, `Pm AEND …`, `Pm Suche beendet` |

#### Anlern-Krypto als Dienst (der Stick als Zentrale)

Der **Ablauf** des Anlernens auf Zentralenseite — welche Frames in welcher
Reihenfolge, Adressvergabe, Wartezeiten — ist Sache des Wirts. Was der Stick
dafür liefert, ist die Rechnung, die man nicht in Perl nachbauen möchte:

| Befehl | Wirkung | Antwort |
|---|---|---|
| `mWK<32hex>` | Arbeitsschlüssel für die folgenden `mW`-Aufrufe setzen (erst der Aufkleberschlüssel des Ziels, dann der Einmalschlüssel) | `Pm w schluessel gesetzt`, sonst `Pm ERR key` |
| `mW<sgtin 24hex><vier 8hex><nutz 32hex>` | eine Stufe packen: Nutzinhalt gegen SGTIN und Zufallsanteil mit dem Arbeitsschlüssel verschlüsseln | `Pm w <ergebnis 32hex> <mic 8hex>`, sonst `Pm ERR arg` |
| `mW<sgtin 24hex><vier 8hex>*` | dasselbe mit dem **abgelegten Netzwerkschlüssel** als Nutzinhalt — der Wirt muss ihn dafür nicht kennen und bekommt ihn nicht zu sehen | wie oben; `Pm kein Netzwerkschluessel` |
| `mY<geraet 16hex><zentrale 16hex>` | Einmalschlüssel aus beiden Anteilen zusammensetzen | `Pm y <32hex>`, sonst `Pm ERR arg` |

Zweigeteilt, weil eine Befehlszeile höchstens 131 Zeichen fasst. Eine
vollständige Umsetzung des Zentralen-Anlernens mit genau diesen Bausteinen
steht in `qccu_radio.py` (Suche nach `mWK`, `mW`, `mY`).

#### Vorlauf, Weckkanal, Zustellung (`mU…`)

| Befehl | Wirkung |
|---|---|
| `mU` | melden |
| `mU0` / `mU1` | **BidCoS**-Vorlauf (`As` mit Bit `0x10`) aus / an — nur zum Messen. Gilt **nicht** für `mb`, dort entscheidet die Stufe |
| `mU1<ms>` / `mU3<ms>` | Dauer des HmIP-Vorlaufs für Stufe 1 bzw. 3, dezimal, **10…2000** ms (Vorgabe 360). Außerhalb: `Pm ERR bereich 10..2000`. `mU1` ohne Ziffern bleibt der Schalter |
| `mUK<6hex>` / `mUK` | Weckkanal als `FREQ2 FREQ1 FREQ0` setzen / nur melden (Vorgabe `21717A` = 869,52 MHz). `mUK000000` schaltet den Kanalwechsel ab: der Vorlauf geht dann auf dem Empfangskanal hinaus, und die Zustellung entfällt |
| `mUZ<ms>` | Zustellabstand nach dem Wecken, dezimal, **5…500** ms (Vorgabe 30). Außerhalb: `Pm ERR bereich 5..500` |

Antwort in jedem Fall die Meldezeile:

    Pm vorlauf=<0|1> (As) ms=<stufe1>/<stufe3> (mb1/mb3) weckkanal=<6hex>[(aus)] zustell=<ms>

Fehlerhafte Argumente: `Pm ERR`.

#### Diagnose und Werkstatt

| Befehl | Wirkung | Antwort |
|---|---|---|
| `mH1` / `mH0` | Frequenzdiagnose: je empfangenem Frame mit gültiger Prüfsumme eine `PH`-Zeile — **auch fremde Netze**, mit dem ganzen Luftframe | `Pm H=1` |
| `mV` | Löschmarke dieses Sticks zeigen (aus der Werkskennung, je Stick anders) | `Pm marke=<8hex> — loeschen mit mV<8hex> (ALLES weg: Kennung, Schluessel, Zaehler)`; ohne lesbare Werkskennung `Pm ERR keine Seriennummer` |
| `mV<marke 8hex>` | **Urzustand**: Kennung, Aufkleber- und Netzwerkschlüssel, Adresse, Sendezähler löschen, dann Neustart. Die Werkskennung bleibt | `Pm Urzustand — Kennung, Schluessel und Zaehler geloescht`; falsche Marke `Pm ERR marke — erst mV fragen` |

---

## Ausgabezeilen

### Empfang

| Zeile | Wann | Aufbau |
|---|---|---|
| `A<len><frame…><rssi>` | `Ar` an; Frame 10…30 Byte, der **nicht** bewiesen zum eigenen HmIP-Netz gehört | wie culfw: Längenbyte, Bytes, dann der **Rohwert** des RSSI-Registers — umrechnen wie bei culfw (`r>127 ? (r-256)/2-74 : r/2-74`) |
| `P<len><frame…><rssi>` | `Pr` an; Frame, den die MAC-Schicht als HmIP erkannt hat (mit `mE1`), bzw. jeder Frame ab 18 Byte (ohne `mE1`) | wie `A`, aber der Pegel ist bereits **dBm** als vorzeichenbehaftetes Byte (Zweierkomplement, `D3` = −45 dBm) |
| `PM<len><nutzlast…><rssi><lqi> src=… dst=… sec=<d> ct=<d> sn=<8hex> [via=…] [ack=<8hex> ar=<d>] f=<2hex> [a=1]` | `mE1`; Frame entschlüsselt und geprüft | Nutzlast im Klartext, Pegel in dBm (Zweierkomplement), LQI-Register; die Felder siehe unten |
| `PK from=<6hex> for=<6hex> rssi=<dBm>` | `mE1`; die 6-Byte-Kurzquittung eines Geräts auf **unseren** Frame | `from` = das Gerät, `for` = unsere Adresse |
| `PH fe=<n> first=<n> min=<n> max=<n> n=<k> rssi=<dBm> len=<n> raw=<hex>` | `mH1`; jeder Frame mit gültiger Prüfsumme, vor jeder Familientrennung | `fe` = FREQEST-Restversatz am Paketende, `first`/`min`/`max` über das Paket, `n` = Zahl der Lesungen (sättigt bei 255), `raw` = der ganze entwürfelte Luftframe |

Beispiele (Adressen erfunden, Aufbau echt):

    A100102030405060708090A0B0C0D0E0F1005
    PM0D850104800201C803001E060001D38E src=ABCDEF dst=A1B2C3 sec=1 ct=0 sn=00000065 f=09
    PM06000100007D81D193 src=ABCDEF dst=A1B2C3 sec=1 ct=4 sn=00000066 ack=00007D81 ar=1 f=0D
    PK from=ABCDEF for=A1B2C3 rssi=-49
    PH fe=-1 first=-1 min=-1 max=-1 n=5 rssi=-62 len=34 raw=…

Was ein Wirt daraus wissen muss:

* **Die Familientrennung macht der Stick.** Mit `mE1` gilt: geht der Frame
  durch Prüfung und Entschlüsselung, ist es HmIP (`P`/`PM`); nur ein Frame, der
  das nicht **beweist** (verschlüsselt, an uns oder Rundruf), bekommt zusätzlich
  eine `A`-Zeile — ein BidCoS-Anlernruf geht an `000000`, ist unverschlüsselt
  und sähe sonst niemand. Ohne `mE1` bleibt nur die Länge: Frames von 18 bis
  30 Byte erscheinen dann bei `Ar` **und** `Pr`.
* Frames eines **fremden** HmIP-Netzes (anderer Schlüssel) sind nicht als
  solche erkennbar und kommen als `A`-Zeile, wenn `Ar` an ist.
* Die 6-Byte-Kurzquittungen erscheinen **nie** als `A`- oder `P`-Zeile; die an
  uns gerichteten kommen als `PK`.
* `a=1` in der `PM`-Zeile heißt: der Stick hat diesen Frame mit einer
  ct=4-Quittung beantwortet. ⚠️ In 2.0.92 steht es auch dann, wenn die
  Quittung am leeren Sendezeit-Konto scheiterte — der Rückgabewert `LOVF` des
  Sendewegs wird von den internen Aufrufern (ct=4-Quittung, Kurzquittung,
  Weiterleitung, BidCoS-Selbstquittung) als Erfolg gewertet; `acks`, `k6tx`
  und `fwd` zählen dann mit, und bei `Aq1` gilt die nicht gesendete
  Selbstquittung als gegeben — die des Wirts wird dann als Doppel geschluckt,
  das Gerät bekommt keine. Nur bei erschöpftem Konto von Belang.
* Unicasts an uns quittiert der Stick (`mQ1`) mit der Kurzquittung **vor** dem
  Entschlüsseln und **vor** der Ausgabe — die Frist des Geräts liegt bei
  wenigen Dutzend Millisekunden, die serielle Ausgabe eines langen Frames
  allein dauert einige.

#### Felder der `PM`-Zeile

| Feld | Bedeutung |
|---|---|
| `src`, `dst` | Adressen aus dem Kopf; bei geroutet ist `dst` der Weiterleiter |
| `via` | nur geroutet: das eigentliche Ziel bzw. die eigentliche Quelle |
| `sec` | Sicherung, siehe unten |
| `ct` | Rahmentyp, siehe unten |
| `sn` | MAC-Sequenzzähler des Absenders |
| `ack`, `ar` | nur bei ct=4: quittierte Sequenz; `ar` = **was** quittiert wird (3 = ein inhaltlicher Frame, 1 = eine Quittung) — nicht die Rolle des Senders |
| `f` | Flags, bitweise: `01` an uns gerichtet · `02` Quittung fällig · `04` ist selbst eine Quittung · `08` war verschlüsselt und ist geprüft · `10` Rundruf/Sammeladresse (`F000xx`/`E000xx`) · `20` geroutet (`via` gefüllt) · `40` piggybackACK-Bit im Kopf · `80` Wiederholung (Absender, Sequenz schon gesehen) |

#### Rahmentypen (`ct`) und Sicherung (`sec`)

| `ct` | | `sec` | |
|---|---|---|---|
| 0 | Anwendung | 0 | Klartext, kein Zähler, kein MIC |
| 1 | Netzverwaltung | 1 | Zähler (4) + MIC (4) — der Normalfall |
| 2 | ICMP (komprimiert) | 2 | Zähler (4) + MIC (8) — **nicht unterstützt** |
| 3 | Wegeverwaltung (ROUTE_MGMT) | | |
| 4 | MAC-Steuerung (Quittung) | | |
| 7 | UDP (komprimiert) | | |

### Statuszeilen (`m`)

    Pm ein rolle=Zentrale addr=A1B2C3 key=1 sn=00007D84 ack=1 icmpack=1 router=0 nurlesen=0 relais=0
    Pm budget=1 credit=900/900 lovf=0
    Pm rx=56033 ok=5486 mic=50547 dup=0 acks=20 k6tx=1192 k6rx=263 akdop=0 fwd=0 tx=2912 txerr=0 burst=350 pll=0/0/0 recal=669 noise=-106 npk=-21

| Zeile 1 | |
|---|---|
| `ein` / `aus` | MAC-Schicht (`mE`) |
| `rolle`, `addr`, `key` | Rolle, Funkadresse, Netzwerkschlüssel vorhanden |
| `sn` | eigener MAC-Sequenzzähler |
| `ack` | `mQ`-Stufe 0/1/2 |
| `icmpack` | `mN` |
| `router` | `mF` |
| `nurlesen` | Riegel (`mL`) |
| `relais` | das nachgebildete Relais |

| Zeile 2 | |
|---|---|
| `budget` | Konto-Bremse an/aus (`mX`) |
| `credit` | Restvorrat / Maximum, 10-ms-Einheiten |
| `lovf` | wie oft ein Sendeauftrag am Konto scheiterte |

| Zeile 3 | seit Start bzw. letztem `mZ` — `mZ` lässt `pll`, `recal` und das `lovf` der zweiten Zeile stehen |
|---|---|
| `rx` | Frames, die die MAC-Schicht gesehen hat |
| `ok` / `mic` | davon geprüft und entschlüsselt / durchgefallen (fremder Schlüssel, Prüfsumme) — im Mischbetrieb mit fremden Netzen ist `mic` groß und kein Fehler |
| `dup` | Wiederholungen |
| `acks` | gesendete Quittungen (ct=4 HmIP und BidCoS-Selbstquittungen) |
| `k6tx` / `k6rx` | Kurzquittungen gesendet / für uns empfangen |
| `akdop` | Quittungen des Wirts, die der Stick geschluckt hat (`Aq1`) |
| `fwd` | weitergeleitete Frames (`mF1`) |
| `tx` / `txerr` | gelungene Sendeaufträge (`As`/`Ps`, `ms`/`mT`/`mR`/`mb`, eigene Anlern- und Anmelderahmen) / gescheiterte, dazu gescheiterte ct=4-Quittungen und Weiterleitungen. Quittungen und Weiterleitungen haben ihre eigenen Zähler (`acks`, `k6tx`, `fwd`) |
| `burst` | Sendungen mit Vorlauf |
| `pll` | PLL-Wache: Lock verloren / neu kalibriert / aufgegeben — jede Sekunde wird FSCAL1 geprüft |
| `recal` | Zwangskalibrierungen (alle 15 Minuten) |
| `noise` / `npk` | Rauschboden: nachlaufendes Mittel des Leerlaufpegels, eine Probe je Sekunde, **nach unten schnell** (ein Viertel der Differenz je Probe), **nach oben träge** (ein Zweiunddreißigstel — ein einziehender Störer wird erst nach etwa einer halben Minute sichtbar) / lautester Leerlaufwert seit `mZ`, dBm; `-127` = noch keine Probe |

Auf dem CUL32 folgt eine vierte Zeile `Pm pass=…`; der CUL V3 hat sie nicht.

### Weitere Meldungen

| Zeile | Wann |
|---|---|
| `Pm relais=<0\|1>` | bei jedem Schalten des Relais — auch per Taster oder Funk, nicht nur auf `mO` |
| `Pm Zufallsquelle tot — Funkchip pruefen (mZ), nichts erzeugt` | `mGN`/`mGP`/`mGK`: das Rauschen des Funkchips liefert keinen Zufall |
| `Pm keine Werkskennung — laufende Nummer gewuerfelt` | `mGN` auf einem Baustein ohne lesbare Werkskennung |
| `Pm Angebot: Pruefsumme falsch` | Anlernen: das Angebot der Zentrale passte nicht zum Aufkleberschlüssel |

---

## Sendezeit-Konto

Ein Konto für den ganzen Stick, beide Familien, alle Wege — auch die eigenen
Quittungen. **900 Einheiten** zu 10 ms (9 s Luftzeit), Auffüllung **eine Einheit
je Sekunde** (1 %), Start mit 450.

Ein Frame kostet ⌈(len + 11) · 0,8 ms / 10 ms⌉ Einheiten — auf der Luft liegen
4 Byte Präambel, 4 Byte Synchronwort, Längenbyte und 2 Byte Prüfsumme um die
Nutzlast, bei rund 10 kbit/s sind das 0,8 ms je Byte. Ein BidCoS-Frame von
12 Byte kostet also 2 Einheiten, **ein Vorlauf von 360 ms weitere 36**. Mit
Weckkanal und Zustellung (`mb1`/`mb3`) bucht das Konto Vorlauf **und beide
Rahmen**.

Reicht der Vorrat nicht, sendet der Stick **nicht** und sagt es: `Ps ERR LOVF`
(`As`/`Ps`) bzw. `Pm ERR LOVF` (`ms`/`mT`/`mR`/`mb`), gezählt in `lovf`. Für
FHEM steht der Vorrat hinter `X` (`credit10ms`); FHEM hält seine Sendungen
danach selbst zurück.

---

## Vorlauf für schlafende Geräte

**BidCoS (`As`):** ist im Flag-Byte Bit `0x10` gesetzt, sendet der Stick 360 ms
Präambel und dann den Frame — auf dem Empfangskanal, ohne Kanalwechsel, wie
culfw. Fest; abschaltbar nur mit `mU0` (Messplatz).

**HmIP (`mb<stufe>…`):** der Wirt nennt die Stufe (das Burst-Byte, das die
echte Zentrale ihrem Funkmodul mitgibt) oder mit `mbL<hh>` gleich den Hörertyp
aus Byte 31 des Anlernrufs. Der Stick sendet den Rahmen mit Vorlauf auf dem
**Weckkanal** 869,52 MHz und nach dem Zustellabstand (Vorgabe 30 ms) denselben
Rahmen ohne Vorlauf auf 868,30 MHz — ein Auftrag, ein Urteil, eine Buchung;
`tx ok` kommt entsprechend später. Die Antwort des Geräts folgt auf 868,30.

Was der Stick **nicht** macht und beim Wirt bleibt: die Wiederholung des Paars
nach 700 ms, wenn keine Antwort kommt; und das **Wartefach** für Hörertypen 4,
5 und 8 — dort bewirkt ein Vorlauf nichts, der Befehl muss liegen bleiben, bis
das Gerät sich meldet.

| Hörertyp (Byte 31, untere vier Bit) | Stufe |
|---|---|
| 0, 12, 14 — PERMANENT_LISTENER (auch WIRED, BACKBONE) | 0 |
| 1 — SINGLE_BURST_LISTENER | 1 |
| 3 — TRIPLE_BURST_LISTENER | 3 |
| 4, 5 — EVENT_LISTENER (ohne / mit POWER_SAVE) | 0, Wartefach |
| 8 — CYCLIC_LISTENER | 0, Wartefach |
| 9 — CYCLIC_AND_SINGLE_BURST_LISTENER | 1 |
| 11 — CYCLIC_AND_TRIPLE_BURST_LISTENER | 3 |

---

## Funkteil

Registersatz des eq-3-Coprozessors: 868,30 MHz, 2-FSK, rund 10 kbit/s,
doppeltes Synchronwort, 4 Byte Präambel, Verwürfelung und Prüfsumme im Chip,
**keine** Kanalprüfung vor dem Senden (die Sendezeit begrenzt das Konto, nicht
der Chip). Einzige Abweichung: **`FSCTRL0 = 0x11`** (+17 Schritte, +27 kHz)
gleicht den Quarz des CUL V3 gegen eq-3-Geräte aus.

Wer den Versatz für einen eigenen Stick prüfen will: `mH1`, dann die
`PH fe=`-Werte der Geräte ansehen (ein Schritt = 26 MHz / 2¹⁴ = 1,587 kHz;
positiv = der Sender liegt über der eigenen Frequenz; Anschlag bei ±16). `fe`
ist der **Rest**versatz bei der aktuellen Einstellung, ein neuer Wert wird also
auf `C0C` **addiert**: `W0C<hex>`. Flüchtig — nach einem Reset steht wieder
`0x11`. `C32` (FREQEST) direkt zu lesen taugt dafür nicht: der Wert gilt nur
während des Pakets, danach steht das Register auf 0.

PLL-Wache: jede Sekunde wird FSCAL1 auf einen gescheiterten Lock geprüft und
notfalls neu kalibriert (bis zu drei Anläufe), alle 15 Minuten wird ohne Anlass
kalibriert; beides zählt `pll` und `recal`.

---

## Mindestfolgen für einen Wirt

**BidCoS, wie FHEM mit einem CUL** (`00_CUL.pm` macht genau das):

    V          -> V q-culfw 2.0.92
    X21        (keine Antwort)
    Ar         (keine Antwort; öffnet den Riegel)
    T01        -> 0000
    ?          -> ? (? is unknown) Use one of A B C P T V W X m
    X          -> 21 <konto>      (z. B. 21 450 direkt nach dem Start, füllt 1/s auf)

Danach `As<hex>` senden, `A…`-Zeilen lesen, Pegel wie bei culfw umrechnen.
Quittungen und AES macht der Wirt (`CUL_HM`); wer sie nicht in 100 ms schafft,
setzt `Aa<eigene>` und `Aq1` und pflegt `Aw`.

**HmIP als Zentrale** (so richtet QCCU den Stick bei jeder Anmeldung ein):

    mL0        -> Pm L=0 SENDEN FREIGEGEBEN
    mG         -> die Kennung — oder: Pm keine Kennung — mGN erzeugt eine
    mGN        -> NUR in diesem Fall (verwirft sonst den Netzwerkschlüssel!)
    mC         -> Pm rolle=Zentrale   (beim allerersten Mal folgt: Pm Netzwerkschluessel erzeugt)
    mA<6hex>   -> Pm addr=<6hex>
    mQ1        -> Pm Q=1
    mE1        -> Pm E=1
    mH0        -> Pm H=0           (eine Diagnose aus der vorigen Sitzung liefe sonst weiter)
    Pr         -> Pr ok            (nur, wenn die rohen P-Zeilen gewünscht sind)
    m          -> drei Zeilen; key=1 muss darin stehen

Steht dort `key=0`, fehlte beim `mC` die Kennung — `mC` meldet das nicht.

Senden mit `ms`/`mb`, lesen der `PM`-Zeilen; Anlernen neuer Geräte mit
`mWK`/`mW`/`mY` und `mT` — der Ablauf steht in `qccu_radio.py`.

**HmIP als Gerät** (der Stick an einer fremden Zentrale, z. B. um deren Netz
mitzulesen):

    mL0
    mGN        -> Kennung und Aufkleberschlüssel; beides in der Zentrale eintragen
    mGP        -> Pm Anlernen laeuft … Pm ANGELERNT addr=…
    mE1

Danach meldet der Stick die Frames dieses Netzes als `PM`-Zeilen und quittiert,
was an ihn gerichtet ist. Nach einem Neustart sind Rolle und Adresse weg
(s. [Startzustand](#startzustand)): dann `mD`, `mA<addr aus ANGELERNT>`, `mE1`
— der Schlüssel kommt aus dem EEPROM von selbst.

---

## Was der Stick nicht tut

Damit ein Wirt weiß, was er selbst bauen muss:

* **Keine Anwendungsschicht.** Was in der Nutzlast steht — Gerätetypen,
  Kanäle, Parameter, Wochenprogramme — deutet der Wirt. Der Stick liefert
  Klartext und Kopf.
* **Kein Anlernablauf als Zentrale**, nur die Krypto dafür (`mW`/`mY`).
* **Keine Wiederholung** ausgebliebener Antworten, kein Wartefach.
* **Keine BidCoS-Quittung und kein AES**, außer der optionalen Selbstquittung
  (`Aq1`).
* **Kein Schlüssel von außen.** Der Netzwerkschlüssel entsteht in der Zentrale
  (`mC`) oder kommt beim Anlernen (`mGP`); im Klartext verlässt er den Stick
  nie (`mGK` gibt ihn verpackt).
* **Keine zweite Familie gleichzeitig senden.** Ein Stick, ein Funkteil, ein
  Konto — BidCoS und HmIP teilen es sich.
