#!/usr/bin/env python3
"""Funkanbindung der QCCU: verbindet den Quiche-Stick mit der Zentralen-Nachbildung."""
import json
import os
import queue
import re
import secrets
import sys
import threading
import time

import serial

try:
    from sticker_decode import sticker_to_local_key
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sticker_decode import sticker_to_local_key

DEC = re.compile(r"^PM([0-9A-Fa-f]+)\s+src=([0-9A-Fa-f]{6})\s+dst=([0-9A-Fa-f]{6})"
                 r"\s+sec=(\d+)\s+ct=(\d+)")
# ct=4 mit ack=: `ar=1` quittiert eine Quittung, `ar=3` einen inhaltlichen
# Frame — beides bestaetigt, dass unser Frame beim Geraet war.
ACK = re.compile(r"\bct=4\b.*\back=")
ACKSEQ = re.compile(r"\back=([0-9A-Fa-f]{8})")
# Kurzquittung (q-culfw >= 2.0.26): 6-Byte-Frame des Peers auf unseren letzten
# Frame — DAS ist die MAC-Quittung, die die echte Zentrale und die Geraete
# austauschen (Luftmitschnitt 18.08.2026); ct=4 folgt nur auf die CONFIRMATION.
KURZQUITTUNG = re.compile(r"^PK from=([0-9A-Fa-f]{6}) for=([0-9A-Fa-f]{6})")
FLAGS = re.compile(r"\bf=([0-9A-Fa-f]{2})\b")
SEQ = re.compile(r"\bsn=([0-9A-Fa-f]{8})")
# k6tx/k6rx (Kurzquittungen gesendet/empfangen) gibt es seit q-culfw 2.0.26;
# aeltere Sticks lassen die beiden Felder weg — beides muss passen.
# ⚠️ Die Zaehlerzeile WAECHST mit der Firmware. Jedes neue Feld steht in der
# Mitte, und eine Regex, die alle Felder verlangt, passt danach auf gar nichts
# mehr — dann steht in der Oberflaeche „—" statt Zahlen, ohne Fehlermeldung.
# Genau das geschah beim Sprung auf q-culfw 2.0.50: neu ist `akdop=` (die
# geschluckten Doppelquittungen des Aq-Weges) zwischen `k6rx=` und `fwd=`.
# Neue Felder deshalb IMMER als eigene, optionale Gruppe aufnehmen — dann
# lesen wir alte und neue Sticks.
CNT = re.compile(r"^Pm\s+rx=(\d+)\s+ok=(\d+)\s+mic=(\d+)\s+dup=(\d+)"
                 r"\s+acks=(\d+)(?:\s+k6tx=(\d+)\s+k6rx=(\d+))?"
                 r"(?:\s+akdop=(\d+))?"
                 r"\s+fwd=(\d+)\s+tx=(\d+)\s+txerr=(\d+)")
# Rauschboden und Spitzenwert, seit q-culfw 2.0.71 am ENDE derselben
# `Pm`-Zeile. Bewusst eine EIGENE Regex statt einer Erweiterung von `CNT`:
# so bleibt das Zaehlerwerk davon unberuehrt, ob der Stick neu genug ist —
# ein alter Stick liefert weiter seine Zahlen und hier eben nichts.
# `-127` heisst „noch keine Probe" und ist KEIN Messwert.
NOISE = re.compile(r"\bnoise=(-?\d+)\s+npk=(-?\d+)")
# Zustand des Oszillators: Lock-Verluste / Nachkalibrierungen / Aufgaben,
# und die erzwungenen Kalibrierlaeufe (einer je Viertelstunde).
PLL = re.compile(r"\bpll=(\d+)/(\d+)/(\d+)")
RECAL = re.compile(r"\brecal=(\d+)")
KEINE_PROBE = -127

RAW = re.compile(r"^P([0-9A-Fa-f]{4,})$")
STICKSEQ = re.compile(r"^Pm (?:ein|aus) .*\bsn=([0-9A-Fa-f]{8})")
BUDGET = re.compile(r"^Pm budget=(\d) credit=(\d+)/(\d+) lovf=(\d+)")
TX_OK = re.compile(r"^Pm tx ok")
TX_NO = re.compile(r"^(?:Pm (?:ERR|NUR-LESEN)|\?\s*$)")

CNT_KEYS = ("rx", "ok", "mic", "dup", "acks", "k6tx", "k6rx", "akdop",
            "fwd", "tx", "txerr")

FT_CONFIGURATION = 1
FT_ANSWER = 2
FT_STATUS = 5
# Wie lange nach dem Senden einer Quittung ihre Kurzquittung spaetestens da
# ist — danach darf ein Befehl an dasselbe Geraet hinaus, ohne dass die
# Kurzquittung des einen dem anderen zugerechnet wird. Gemessen: 23–61 ms.
ACK_NACHLAUF = 0.15

# Schaltbefehle eines Senders an seinen Verknuepfungspartner — so kommt ein
# Tastendruck bei der Zentrale an (ApplicationFrameType 8/9/10).
FT_SWITCH_UNCOND = 8
FT_SWITCH_COND = 9
FT_LEVEL_CMD = 10
# Untertyp eines CONFIGURATION-Rahmens (Byte nach dem Anwendungskopf), aus
# `ConfigurationRequestType` im HMIPServer-Jar. Vom Geraet kommt vor allem
# REQUEST_CONFIG_UPDATE (0x0F): „ich bin wach, schick mir, was du fuer mich
# hast" — am HmIP-SCI kurz nach dem Anlernen gemessen (03.09.2026).
KONFIG_ANFRAGEN = {
    1: "CREATE_LINK", 2: "REMOVE_LINK", 3: "REQUEST_LINK_PARTNER_LIST",
    4: "CONFIGURATION_DATA_REQUEST", 5: "START_PARAMETER_SETTING",
    6: "COMMIT_PARAMETER_SETTING", 7: "SET_PARAMETER_BY_OFFSET",
    8: "SET_PARAMETER_BY_INDEX", 9: "REQUEST_REPORT_SGTIN",
    10: "RESPONSE_LINK_PARTNER_LIST", 11: "RESPONSE_CONFIGURATION_DATA",
    12: "REPORT_CONFIGURATION_CHANGE", 14: "REPORT_LINK_PARTNER_PROBLEM",
    15: "REQUEST_CONFIG_UPDATE",
}
# ApplicationFrameType.TIME_INFO — Wert aus den Enums des HMIPServer-Jars.
# Ein frisch angelerntes Geraet fragt die Zeit an und verlangt Antwort; die
# CCU schickt ihr einen TIME_INFO-Frame zurueck, KEIN ANSWER.
FT_TIME_INFO = 35

# NetworkManagementFrameType — Werte aus denselben Jar-Enums. Der Ausschluss
# eines Geraets laeuft in drei Schritten, alle ohne Nutzlast.
NM_EXCLUDE_REQUEST = 0xF0
NM_EXCLUDE_READY = 0xF1
NM_EXCLUDE_CONCLUDE = 0xF2
SDT_BINARY_MIT_PROFIL = 8
SDT_PERIOD = 14

# ---------------------------------------------------------------------------
# Statuswerte deuten
#
# Eine Statusmeldung besteht aus Eintraegen <Statusdatentyp> <Kanal> <Rohwert>.
# WELCHE Parameter aus einem Rohwert werden, entscheidet bei eq-3
# `HMIPApplicationHandler.handleStatusFrame` (HMIPServer-Jar): ein `switch`
# ueber `StatusDataType`, das je Typ eine Reihe von
# `tryConvertParameter(Kanal, <Parameter>, <Teilbytes>)` absetzt. Das „try“
# heisst dort: gemeldet wird nur, was der Kanal ueberhaupt fuehrt — genau die
# Bedingung, die hier `_kanal_fuehrt` prueft.
#
# Die Tabellen unten sind diese Fallunterscheidung, Zeile fuer Zeile aus dem
# Jar abgeschrieben — nicht aus dem Verhalten erraten. Sie trennen drei Dinge,
# die eq-3 auch trennt:
#
#   1. WELCHE BYTES  — `SDT_REGELN`: je Statusdatentyp die Parameter und die
#      Bits, die sie tragen.
#   2. WIE GERECHNET — `UMRECHNUNG`: Faktor und Offset gehoeren dem PARAMETER,
#      nicht dem Statusdatentyp (`tryConvertParameter` holt den Konverter aus
#      der Beschreibung des Parameters). Derselbe Rohwert 0xC8 ist als LEVEL
#      1.0 und als DUTY_CYCLE_LEVEL 100.0 — wer die Rechnung am
#      Statusdatentyp festmacht, hat einen von beiden falsch.
#   3. WELCHE FORM   — der logische Typ aus der Gerätebeschreibung (`TYPE`):
#      BOOL, ENUM (Zeichenkette aus `VALUE_LIST`), INTEGER, FLOAT. Im Jar ist
#      das `LogicalType`; es entscheidet ueber die Gestalt des Wertes, nicht
#      der Statusdatentyp. Deshalb kann dieselbe Regel einen Kanal bedienen,
#      der WINDOW_STATE als ENUM fuehrt, und einen, der es als BOOL fuehrt.
#
# Was hier NICHT steht, bleibt RAW_SDT<n> — ungedeutet ist besser als falsch
# gedeutet.


class Sonder:
    """Ein reservierter Rohwert: keine Messung, sondern eine Auskunft.

    eq-3 belegt die obersten Werte jedes Bereichs mit Zustaenden
    (`StateParameterStatus`: UNKNOWN, OVERFLOW, UNDERFLOW, ERROR, EXTERNAL) und meldet
    sie im Nebenparameter `<NAME>_STATUS`. Der Messwert selbst wird dabei
    entweder geloescht (UNKNOWN, ERROR) oder auf einen Ersatzwert gesetzt
    (OVERFLOW, UNDERFLOW).

    `ersatz` ist der PHYSIKALISCHE Ersatzwert — er laeuft durch dieselbe
    Umrechnung wie ein Messwert. Bei den Temperaturen und Pegeln kommen so
    genau die Zahlen heraus, die das Jar direkt setzt (3276.8 / -3276.8,
    1.005 / -0.05).
    """

    __slots__ = ("status", "ersatz")

    def __init__(self, status, ersatz=None):
        self.status = status
        self.ersatz = ersatz


def _ganz(v, bits=None):
    """Rohbytes als Zahl. Vorzeichenlos, ausser der Konverter sagt anders.

    `DoubleToInteger`/`IntegerToInteger` lesen vorzeichenlos
    (`iValue |= physicalValue[i] & 0xFF`); nur `DoubleToSignedInteger`
    ergaenzt das Vorzeichen ueber die angegebene Bitbreite.
    """
    z = int.from_bytes(v, "big")
    if bits and z & (1 << (bits - 1)):
        z -= 1 << bits
    return z


def _temperatur(v):
    """2 Byte (handleActualTemperatureStatus).

    0x8000/0x8001/0x8002 sind Zustaende, kein Messwert — sonst waere
    „unbekannt“ eine Temperatur von -3276,8 °C.
    """
    if v[0] == 0x80 and v[1] in (0x00, 0x01, 0x02):
        return (Sonder("UNKNOWN"),
                Sonder("OVERFLOW", 32768),
                Sonder("UNDERFLOW", -32768))[v[1]]
    return _ganz(v, 16)


def _feuchte(v):
    """1 Byte (handleHumidityMoistureStatus)."""
    if v[0] == 0xFF:
        return Sonder("UNKNOWN")
    if v[0] == 0xFE:
        return Sonder("OVERFLOW", 101)
    if v[0] == 0xFD:
        return Sonder("UNDERFLOW", -1)
    return v[0]


def _spannung(v):
    """1 Byte (Fall OPERATING_VOLTAGE im Handler).

    Die drei obersten Werte sind Auskuenfte, keine Spannungen: 0xFF unbekannt
    (Wert geloescht), 0xFE Ueberlauf — das Jar setzt dann 25.3 V fest, hier
    als Rohwert 253, damit er durch dieselbe Zehntel-Umrechnung laeuft —,
    0xFD „extern versorgt“ (Wert geloescht, Status EXTERNAL). Alles darunter
    sind Zehntelvolt, 0 .. 25.2.
    """
    if v[0] == 0xFF:
        return Sonder("UNKNOWN")
    if v[0] == 0xFE:
        return Sonder("OVERFLOW", 253)
    if v[0] == 0xFD:
        return Sonder("EXTERNAL")
    return v[0]


def _beleuchtung(v):
    """2 Byte (Handler, Faelle ILLUMINATION/BRIGHTNESS): Gleitpunkt zu Fuss.

    Bit 7..6 des ersten Bytes sind der Exponent (0..3), die restlichen 14 Bit
    die Mantisse; Wert = Mantisse * 10^Exponent (`handleIlluminationStatus`).
    Zwei Mantissen sind Auskuenfte: 16383 unbekannt, 16382 Ueberlauf — bei
    beiden loescht das Jar den Wert und meldet nur den Status. Was
    herauskommt, ist noch der PHYSIKALISCHE Wert; die Umrechnung des
    Parameters (Zehntel-Lux, bei der Fassung `slo` Hundertstel) folgt.
    """
    mantisse = ((v[0] & 0x3F) << 8) | v[1]
    exponent = (v[0] >> 6) & 3
    if mantisse == 16383:
        return Sonder("UNKNOWN")
    if mantisse == 16382:
        return Sonder("OVERFLOW")
    return mantisse * (10 ** exponent)


def _zeitpunkt(p):
    """Vier Rohbyte eines Zeitpunkts als Text der Form DATUM_FORM.

    Die Umkehrung von `datum_roh`, abgeschrieben aus
    `DateStringToInteger.convertPhysicalToLogical`:
        Minuten des Tages = p0 * 10 (+5, wenn Bit 7 von p1)
        Tag   = p1 & 0x3F      Monat = p2 (1..12)      Jahr = 2000 + p3
    Das Jar formatiert mit `yyyy_MM_dd HH:mm`. Ein Monat 0 oder 13, ein
    Tag 0 oder eine Uhrzeit jenseits 23:55 sind kein Zeitpunkt — das Jar
    liesse den Kalender darueber hinwegrollen, hier wird nichts gemeldet.
    """
    p0, p1, p2, p3 = p
    minuten = p0 * 10 + (5 if p1 & 0x80 else 0)
    tag, monat, jahr = p1 & 0x3F, p2, 2000 + p3
    if not (1 <= monat <= 12 and 1 <= tag <= 31 and minuten < 24 * 60):
        return None
    return f"{jahr:04d}_{monat:02d}_{tag:02d} {minuten // 60:02d}:{minuten % 60:02d}"


def _pegel(v):
    """1 Byte (handleLevelStatus). Die vier obersten Werte sind Zustaende.

    ⚠️ Abweichung mit Grund: das Jar setzt als Ersatzwert 1.005 bzw. -0.05
    AUCH fuer die prozentskalierten Namen (DUTY_CYCLE_LEVEL,
    CARRIER_SENSE_LEVEL) — dieselbe Routine, dieselben Konstanten. Hier
    laeuft der Ersatz durch die Umrechnung des jeweiligen Parameters und
    ergibt dort 100.5 statt 1.005. Fuer die LEVEL-Namen ist das Ergebnis
    identisch; fuer die Prozentnamen halten wir die Einheit ein, statt eine
    Eigenart mitzuschleppen.
    """
    if v[0] == 0xFF:
        return Sonder("UNKNOWN")
    if v[0] == 0xFE:
        return Sonder("OVERFLOW", 201)
    if v[0] == 0xFD:
        return Sonder("UNDERFLOW", -10)
    if v[0] == 0xFC:
        return Sonder("ERROR")
    return v[0]


def _bit(nummer):
    """Ein einzelnes Bit als 0/1.

    Das Jar reicht je nach Stelle 1, 2 oder 16 weiter und laesst den
    Bitmasken-Konverter des Parameters darueber laufen; fuer BOOL („Rohwert
    ungleich 0“) und fuer ENUM (Platz in der `VALUE_LIST`) ist 0/1 dasselbe
    Ergebnis und die einzige Form, die zu beidem passt.
    """
    return lambda v: 1 if v[0] & (1 << nummer) else 0


# --- Umrechnung physikalisch -> logisch ------------------------------------
#
# (Faktor, Offset) je Parameter, aus den StateParameter-Fabriken des Jars.
# Die Rechnung ist immer `(roh - Offset) / Faktor` (`convertPhysicalToLogical`).
# Wer hier fehlt, wird unveraendert uebernommen — im Jar ist das der Fall
# „kein Konverter“ (null).
#
# Das VORZEICHEN steht nicht hier, sondern bei den Byteschnitten oben: nur
# dort ist die Breite bekannt (`DoubleToSignedInteger(..., 16)` gilt fuer die
# zwei Temperaturbytes, nicht fuer den Parameter an sich).
UMRECHNUNG = {
    # ClimateStateParameterFactory.createReadOnlyTemperature:
    #   DoubleToSignedInteger(10.0, 0.0, 16)
    "ACTUAL_TEMPERATURE": (10.0, 0.0),
    "SOIL_TEMPERATURE": (10.0, 0.0),
    # ClimateStateParameterFactory.createSetPointTemperatureParameter:
    #   DoubleToInteger(2.0, 0.0) — Halbgradschritte
    "SET_POINT_TEMPERATURE": (2.0, 0.0),
    "PARTY_SET_POINT_TEMPERATURE": (2.0, 0.0),
    # ClimateStateParameterFactory.createControlDifferentialTemperature…:
    #   DoubleToInteger(2.0, 0.0) — ebenfalls Halbgrad, aber MIN -10,0
    "CONTROL_DIFFERENTIAL_TEMPERATURE": (2.0, 0.0),
    # GeneralStateParameterFactory.createLevel: DoubleToInteger(200.0, 0.0)
    "LEVEL": (200.0, 0.0),
    "PWM_LEVEL": (200.0, 0.0),
    "SATURATION": (200.0, 0.0),
    # GeneralStateParameterFactory.createReadOnlyPercentage(name, 100.0):
    #   DoubleToInteger(2.0, 0.0) — derselbe Rohbereich 0..200, andere Einheit
    "DUTY_CYCLE_LEVEL": (2.0, 0.0),
    "CARRIER_SENSE_LEVEL": (2.0, 0.0),
    # ClimateStateParameterFactory.createActiveProfileParameter:
    #   IntegerToInteger(1.0, -1.0) — Profil 1 steht als 0 auf der Luft
    "ACTIVE_PROFILE": (1.0, -1.0),
    # StateParameterFactory.createOperatingVoltageParameter:
    #   DoubleToInteger(10.0, 0.0), MAX 25.2 — ein Byte, Zehntelvolt. Die
    #   Fassung `highRes` (DoubleToInteger(100.0), zwei Byte) fuehrt im
    #   Bestand nur der HmIPW-DRAP am Draht; ueber Funk kommt sie nicht vor.
    "OPERATING_VOLTAGE": (10.0, 0.0),
    # GeneralStateParameterFactory.createIllumination: DoubleToInteger(10.0)
    # — Zehntel-Lux; MAX 163830. Die Fassung `slo` (createHighResolution-
    # Illumination) rechnet in Hundertsteln, siehe `umrechnung()`.
    "ILLUMINATION": (10.0, 0.0),
    "CURRENT_ILLUMINATION": (10.0, 0.0),
    "ILLUMINATION@slo": (100.0, 0.0),
    "CURRENT_ILLUMINATION@slo": (100.0, 0.0),
    # Formen, die anders skalieren als der blanke Name (`umrechnung()`):
    # GeneralStateParameterFactory.createLevel16Bit: DoubleToInteger(50000.0)
    "LEVEL@shade": (50000.0, 0.0),
    # createLevelWindowDrive: StringEnumToInteger {NO_VENTILATION: 0,
    # VENTILATION: 200} — Platz in der Werteliste mal 200
    "LEVEL@VENTILATION": (200.0, 0.0),
}


# Aufzaehlungen, deren Rohwert KEIN Platz in der Werteliste ist, sondern ein
# fester Wert je Eintrag (`StringEnumToInteger(enumStrings, enumValues)` in
# GeneralStateParameterFactory). Der Fensterkontakt meldet seinen Zustand als
# LEVEL-Byte: 0 = CLOSED, 200 = OPEN — wer das als Platz 200 liest, meldet
# nichts.
# ⚠️ Geschluesselt nach PARAMETER UND Werteliste: dieselbe Liste CLOSED/OPEN
# gehoert beim WINDOW_STATE des Heizreglers zu einem Bit (Platz 0/1, am
# BWTH-A und eTRV gemessen), beim STATE des Fensterkontakts zu den festen
# Werten 0/200. Die Liste allein unterscheidet das nicht.
ENUM_WERTE = {
    # createStateWindowOpenClosed (SHUTTER_CONTACT_, DOOR_STATE_TRANSCEIVER)
    ("STATE", ("CLOSED", "OPEN")): (0, 200),
    # createStateWindowOpenTiltedClosed (ROTARY_HANDLE_, ACCELERATION_TRANSCEIVER)
    ("STATE", ("CLOSED", "TILTED", "OPEN")): (0, 100, 200),
    # createStateWindowOpenTiltedClosedUnknown
    ("STATE", ("CLOSED", "TILTED", "OPEN", "UNKNOWN")): (0, 100, 200, 255),
    # createLevelWindowDrive (WINDOW_DRIVE_RECEIVER, LEVEL@VENTILATION)
    ("LEVEL", ("NO_VENTILATION", "VENTILATION")): (0, 200),
}


def umrechnung(param, fassung="default"):
    """(Faktor, Versatz) eines Parameters in seiner Form."""
    if fassung and fassung != "default":
        eintrag = UMRECHNUNG.get(f"{param}@{fassung}")
        if eintrag is not None:
            return eintrag
    return UMRECHNUNG.get(param, (1.0, 0.0))


class Regel:
    """Eine Zeile aus dem `switch`: ein Parameter und die Bytes, die ihn tragen.

    `kanaltypen`/`ausser` bilden die Kanaltyp-Abfragen des Jars ab
    (`getChannelType() == ChannelType.…`). Die sind NICHT ueberfluessig neben
    `_kanal_fuehrt`: derselbe Parametername sitzt je nach Kanaltyp auf einem
    anderen Bit — FROST_PROTECTION liegt beim Fussbodenkanal auf Bit 1, beim
    Heizregler auf Bit 7. Ohne die Abfrage waere der Wert nicht ungedeutet,
    sondern falsch gedeutet.

    `braucht` ist die eine Abfrage des Jars, die nicht am Kanaltyp haengt,
    sondern am Bestand: `channel.getStateParameter("VALVE_STATE") != null`.
    `vorkommen` zaehlt gleiche Statusdatentypen hintereinander im selben Kanal
    (`statusDataTypeOccurence`) — beim zweiten LEVEL desselben Kanals meint
    eq-3 die Saettigung, nicht die Helligkeit.

    `wenn` sieht auf die GANZE Meldung (alle Statusdatentypen darin) und nicht
    nur auf den einzelnen Eintrag; das braucht genau eine Stelle, siehe
    `containsStatusType` bei der Solltemperatur.

    `status` sagt, ob eq-3 zu diesem Parameter auch den Nebenparameter
    `<NAME>_STATUS` fuehrt (NORMAL/UNKNOWN/OVERFLOW/UNDERFLOW). Das tut es
    nicht ueberall — `handleLevelStatus` bekommt fuer DUTY_CYCLE_LEVEL
    ausdruecklich einen leeren Statusnamen —, deshalb steht es je Zeile und
    wird nicht aus dem Namen geraten.

    `bits` ist die Bitlage, wenn sie NICHT am Kanaltyp haengt, sondern an der
    Geraetebeschreibung: ERROR_OVERHEAT liegt beim einen Geraet auf Bit 1,
    beim naechsten auf Bit 0 oder Bit 4 — bei DEMSELBEN Kanaltyp MAINTENANCE.
    Das Jar waehlt dafuer je Geraete-XML eine Fassung des Parameters
    (`subtype="bit4"`) mit dem passenden `BoolToInteger`-Konverter. Hier
    steht je Fassung das Bit; welche das Geraet fuehrt, sagt sein
    Katalogeintrag (`_variante`). Eine Fassung, die hier fehlt, wird nicht
    geraten, sondern nicht gedeutet.
    """

    __slots__ = ("param", "bytes_fn", "kanaltypen", "ausser", "braucht",
                 "vorkommen", "wenn", "status", "bits")

    def __init__(self, param, bytes_fn=None, kanaltypen=None, ausser=None,
                 braucht=None, vorkommen=None, wenn=None, status=False,
                 bits=None):
        assert (bytes_fn is None) != (bits is None), param
        self.param = param
        self.bytes_fn = bytes_fn
        self.bits = bits
        self.kanaltypen = (kanaltypen,) if isinstance(kanaltypen, str) else kanaltypen
        self.ausser = (ausser,) if isinstance(ausser, str) else ausser
        self.braucht = braucht
        self.vorkommen = vorkommen
        self.wenn = wenn
        self.status = status


# Kanaltypen, auf die einzelne Regeln hoeren. Die Schreibweisen stammen aus
# der Kanaltyp-Tabelle (`paramsets.json`), nicht aus dem Gedaechtnis —
# GENERIC_INPUT_TRANSMITER hat im Bestand von eq-3 nur ein T.
KT_HEIZREGLER = "HEATING_CLIMATECONTROL_TRANSCEIVER"
KT_FLOOR_DIRECT = "CLIMATECONTROL_FLOOR_DIRECT_TRANSMITTER"
KT_FLOOR = ("CLIMATECONTROL_FLOOR_PUMP_TRANSCEIVER",
            "CLIMATECONTROL_FLOOR_TRANSCEIVER")
KT_WARTUNG = "MAINTENANCE"
KT_EINGANG = "GENERIC_INPUT_TRANSMITER"
KT_BEWEGUNG = ("MOTIONDETECTOR_TRANSCEIVER", "MOTIONDETECTOR_VIRTUAL_TRANSCEIVER")
KT_ANWESENHEIT = "PRESENCEDETECTOR_TRANSCEIVER"
KT_UNILIGHT = "UNIVERSAL_LIGHT_RECEIVER"

SDT_REGELN = {
    # 0 TEMPERATURE (2 Byte) — handleActualTemperatureStatus
    0: (
        Regel("ACTUAL_TEMPERATURE", _temperatur, status=True),
        Regel("SOIL_TEMPERATURE", _temperatur, status=True),
    ),
    # 1 TEMPERATURE_HUMIDITY (3 Byte) — Temperatur wie oben, danach ein Byte
    #   Feuchte (handleHumidityMoistureStatus)
    1: (
        Regel("ACTUAL_TEMPERATURE", lambda v: _temperatur(v[0:2]), status=True),
        Regel("SOIL_TEMPERATURE", lambda v: _temperatur(v[0:2]), status=True),
        Regel("HUMIDITY", lambda v: _feuchte(v[2:3]), status=True),
        Regel("SOIL_MOISTURE", lambda v: _feuchte(v[2:3]), status=True),
    ),
    # 2 LEVEL (1 Byte)
    2: (
        Regel("LEVEL", _pegel, vorkommen=0, status=True),
        Regel("PWM_LEVEL", _pegel, vorkommen=0, status=True),
        Regel("SATURATION", _pegel, vorkommen=1, status=True),
        # STATE/MOTION bekommen den Rohpegel: der Bitmasken-Konverter des
        # Parameters macht daraus „ungleich 0“. Beim Fussbodenkanal laesst das
        # Jar beide aus — dort ist der Pegel kein Zustand.
        Regel("STATE", lambda v: None if v[0] == 0xFF else v[0], ausser=KT_FLOOR_DIRECT),
        Regel("MOTION", lambda v: None if v[0] == 0xFF else v[0], ausser=KT_FLOOR_DIRECT),
        Regel("DUTY_CYCLE_LEVEL", _pegel, vorkommen=0),
        Regel("CARRIER_SENSE_LEVEL", _pegel, vorkommen=1),
    ),
    # 3 OPERATING_VOLTAGE (1 Byte) — Zehntelvolt, drei Auskunftswerte oben.
    # Den Nebenparameter OPERATING_VOLTAGE_STATUS fuehrt das Jar hier
    # ausdruecklich (NORMAL / UNKNOWN / OVERFLOW / EXTERNAL).
    3: (
        Regel("OPERATING_VOLTAGE", _spannung, status=True),
    ),
    # 4 HEATING_CONTROLLER_STATE (3 Byte)
    #
    # Byte 0: Bit 7..6 Betriebsart, Bit 5..0 Solltemperatur in Halbgrad
    # Byte 1: Bit 7..5 aktives Profil, Bit 4 Schaltpunkt erreicht,
    #         Bit 3 Schnellwahl statt Boost, Bit 2..0 obere Bits der Dauer
    # Byte 2: untere Bits der Dauer
    4: (
        # Fuehrt DIESELBE Meldung auch einen PERIOD-Eintrag, meint der Wert
        # die Party-Temperatur und NICHT die gewoehnliche Solltemperatur
        # (`containsStatusType(statusFrame, PERIOD)`). Ausserdem gilt er
        # zusaetzlich als Party-Wert, wenn die Betriebsart 2 (Party) ist.
        Regel("SET_POINT_TEMPERATURE", lambda v: v[0] & 0x3F,
              wenn=lambda typen, v: SDT_PERIOD not in typen),
        Regel("PARTY_SET_POINT_TEMPERATURE", lambda v: v[0] & 0x3F,
              wenn=lambda typen, v: SDT_PERIOD in typen or (v[0] >> 6) & 3 == 2),
        Regel("SET_POINT_MODE", lambda v: (v[0] >> 6) & 3),
        Regel("ACTIVE_PROFILE", lambda v: (v[1] >> 5) & 7),
        Regel("SWITCH_POINT_OCCURED", lambda v: (v[1] >> 4) & 1),
        # Bit 3 sagt, WOFUER die Dauer gilt: gesetzt = Schnellwahl, sonst
        # Boost. Der jeweils andere Wert wird auf 0 gesetzt — aber nur, wenn
        # die Dauer selbst 0 ist; eine laufende Boost-Zeit darf die
        # Schnellwahl nicht ueberschreiben.
        Regel("QUICK_VETO_TIME",
              lambda v: (((v[1] & 7) << 8) | v[2]) if v[1] & 8
              else (0 if (((v[1] & 7) << 8) | v[2]) == 0 else None)),
        Regel("BOOST_TIME",
              lambda v: 0 if v[1] & 8 else ((v[1] & 7) << 8) | v[2]),
    ),
    # 5 VALVE_STATE (1 Byte) — ein Byte, fuenf Angaben. Den schickt nur, wer
    # einen Stellantrieb hat: die Heizkoerperthermostate. Kein Kanaltyp-Filter
    # noetig, das Jar hat hier keinen — es ist derselbe Aufbau, egal an
    # welchem Kanal er ankommt.
    #
    # Die Ventilstellung selbst ist ein ENUM mit neun Eintraegen
    # (STATE_NOT_AVAILABLE … ERROR_POSITION); ein Halbbyte, das darueber
    # hinausgeht, wird nicht gemeldet statt in die Liste gebogen.
    5: (
        Regel("FROST_PROTECTION", _bit(7)),
        Regel("PARTY_MODE", _bit(6)),
        Regel("WINDOW_STATE", _bit(5)),
        Regel("BOOST_MODE", _bit(4)),
        Regel("VALVE_STATE", lambda v: v[0] & 0x0F),
    ),
    # 6 ERROR_CODE (1 Byte). Das Jar reicht dasselbe Byte an rund sechzig
    # Fehlermerkmale weiter und laesst jeden Kanal nehmen, was er fuehrt
    # (`tryConvertParameter` schweigt, wo der Parameter fehlt — hier tut das
    # `_beschreibung`). Die Bitlage steckt im BoolToInteger-Konverter des
    # jeweiligen Parameters (GeneralStateParameterFactory,
    # createReadEventBooleanBit<n>Parameter). Wo ein Parameter mehrere
    # Fassungen hat, waehlt sie die Geraetebeschreibung, nicht der Kanaltyp —
    # daher `bits` statt `kanaltypen`.
    6: (
        Regel("ERROR_CODE", lambda v: v[0]),
        Regel("ERROR_OVERHEAT", bits={"default": 1, "bit0": 0, "bit4": 4}),
        Regel("ERROR_OVERLOAD", bits={"default": 0, "bit1": 1, "bit2": 2, "bit3": 3}),
        Regel("ERROR_UPDATE", bits={"default": 2, "bit1": 1}),
        Regel("SABOTAGE", _bit(0)),
        Regel("SENSOR_ERROR", bits={"default": 0, "bit1": 1}),
        Regel("ERROR_COMMUNICATION_SENSOR", bits={"default": 1, "bit0": 0}),
        Regel("TEMPERATURE_OUT_OF_RANGE", _bit(0)),
        Regel("ERROR_WIND_NORTH", _bit(6)),
        Regel("ERROR_WIND_COMMUNICATION", _bit(7)),
        Regel("ERROR_UNDERVOLTAGE", bits={"default": 1, "bit0": 0}),
        Regel("ERROR_POWER_FAILURE", bits={"default": 1, "bit0": 0}),
        Regel("ERROR_NON_FLAT_POSITIONING", _bit(0)),
        Regel("ERROR_COPROCESSOR", bits={"default": 2, "bit0": 0}),
        Regel("ERROR_RESTART_NEEDED", _bit(3)),
        Regel("ERROR_BAD_RECHARGEABLE_BATTERY_HEALTH", _bit(1)),
        Regel("ERROR_NOT_RECHARGEABLE_BATTERY", _bit(1)),
        Regel("ERROR_CAN_BUS", _bit(2)),
        Regel("ERROR_MAX_WATER_FLOW", _bit(4)),
        Regel("ERROR_MAX_WATER_FLOW_DURATION", _bit(5)),
        Regel("ERROR_POWER_SHORT_CIRCUIT_BUS_1", _bit(2)),
        Regel("ERROR_POWER_SHORT_CIRCUIT_BUS_2", _bit(3)),
        Regel("ERROR_SHORT_CIRCUIT_DATA_LINE_BUS_1", _bit(4)),
        Regel("ERROR_SHORT_CIRCUIT_DATA_LINE_BUS_2", _bit(5)),
        Regel("ERROR_BUS_CONFIG_MISMATCH", _bit(6)),
        # Die vier STATUS_FLAG_* gibt es nur in der Fassung des MP3-Spielers.
        Regel("STATUS_FLAG_PLAYING_FILE_ACTIVE", bits={"mp3p": 7}),
        Regel("STATUS_FLAG_PLAYLIST_ACTIVE", bits={"mp3p": 6}),
        Regel("STATUS_FLAG_ERROR", bits={"mp3p": 5}),
        Regel("STATUS_FLAG_LOW_BAT", bits={"mp3p": 4}),
        Regel("ERROR_TEMP_OR_HUMIDITY_MEASUREMENT", _bit(0)),
        Regel("ERROR_PARTICULATE_MATTER_MEASUREMENT", _bit(1)),
        Regel("ERROR_COMMUNICATION_PARTICULATE_MATTER_SENSOR", _bit(2)),
        Regel("ERROR_COMMUNICATION_TEMP_AND_HUMIDITY_SENSOR", _bit(3)),
        Regel("ERROR_DEGRADED_CHAMBER", _bit(0)),
        Regel("SABOTAGE_STICKY", _bit(1)),
        Regel("BLOCKED_PERMANENT", _bit(3)),
        Regel("BLOCKED_TEMPORARY", _bit(2)),
        Regel("ERROR_JAMMED", _bit(0)),
        Regel("SABOTAGE_MAGNETIC_FIELD", _bit(6)),
        Regel("ERROR_DOOR_OPENED_WHILE_LOCKED", _bit(5)),
        Regel("ERROR_DOOR_LOCKED_WHILE_OPEN", _bit(4)),
        Regel("SABOTAGE_ACCELERATION", _bit(3)),
        Regel("SABOTAGE_VERTICAL", _bit(2)),
        Regel("SABOTAGE_BATTERY", _bit(1)),
        Regel("ERROR_LOAD_TOO_LOW", _bit(1)),
        Regel("ERROR_NO_END_STOP_LOCK", _bit(2)),
        Regel("ERROR_NO_END_STOP_UNLOCK", _bit(3)),
        Regel("ERROR_DRIVE", _bit(1)),
        Regel("ERROR_COMMUNICATION", _bit(2)),
        Regel("ERROR_DRIVE_MODE", _bit(3)),
        Regel("ERROR_FROST_PROTECTION", _bit(1)),
        Regel("ERROR_VALVE_FAILURE", bits={"default": 2, "bit3": 3}),
        Regel("ERROR_WATER_FAILURE", _bit(3)),
        Regel("ERROR_MODBUS_FAULT", _bit(2)),
        Regel("ERROR_FILTER_EXCHANGE", _bit(3)),
        Regel("ERROR_COMMUNICATION_TEMP_SENSOR", _bit(0)),
        Regel("ERROR_COMMUNICATION_MOISTURE_SENSOR", _bit(1)),
        Regel("ERROR_TEMP_SENSOR", _bit(0)),
        Regel("ERROR_TEMP_SENSOR_2", _bit(1)),
        # Zwei Aufzaehlungen aus dem unteren Halbbyte bzw. den unteren zwei
        # Bits; ein Wert jenseits der Werteliste wird nicht gemeldet.
        Regel("ERROR_CODE_STATUS", lambda v: v[0] & 0x0F),
        Regel("ERROR_DALI_BUS", lambda v: v[0] & 0x03),
        # Nur der Universal-Lichtempfaenger: dort ist UNREACH ein Bit im
        # Fehlerbyte (Fassung `uniLight`), sonst ein eigener Wert.
        Regel("UNREACH", bits={"uniLight": 0}, kanaltypen=KT_UNILIGHT),
        Regel("ERROR_CONTROL_GEAR_FAILURE", _bit(1), kanaltypen=KT_UNILIGHT),
        Regel("ERROR_LAMP_FAILURE", _bit(2), kanaltypen=KT_UNILIGHT),
        Regel("ERROR_LIMIT", _bit(3), kanaltypen=KT_UNILIGHT),
    ),
    # 8 BINARY_WITH_PROFILE (1 Byte): Bit 7 Vorgang laeuft, Bit 6 Zustand,
    # Bit 3..0 Abschnitt des Wochenprogramms (handleProfileInformation).
    8: (
        Regel("LEVEL", lambda v: ((v[0] >> 6) & 1) * 200),
        Regel("STATE", lambda v: ((v[0] >> 6) & 1) * 200),
        Regel("PERMISSION_STATE", lambda v: ((v[0] >> 6) & 1) * 200),
        Regel("AUTO_RELOCK_STATE", lambda v: ((v[0] >> 6) & 1) * 200),
        Regel("PROCESS", _bit(7)),
        # 0x0F heisst „unbekannt“ und ist keine Abschnittsnummer.
        Regel("SECTION", lambda v: Sonder("UNKNOWN") if (v[0] & 0x0F) == 0x0F
              else v[0] & 0x0F, status=True),
    ),
    # 13 BINARY (1 Byte) — je Kanaltyp eine andere Bitbelegung.
    #
    # Nicht uebernommen sind die Zweige fuer WATER_DETECTION_TRANSMITTER,
    # RAIN_DETECTION_TRANSMITTER, ALARM_SWITCH_VIRTUAL_RECEIVER,
    # PASSAGE_DETECTOR_DIRECTION_TRANSMITTER, BLIND_/SHUTTER_TRANSMITTER,
    # POWER_MAINS_TRANSMITTER, MAINTENANCE_BAT_EL und
    # UNIVERSAL_LIGHT_RECEIVER: kein solches Geraet zur Hand, an dem sich die
    # Zeilen gegenpruefen liessen. Sie bleiben RAW_SDT13.
    13: (
        Regel("MOTION", _bit(1), kanaltypen=KT_BEWEGUNG),
        Regel("MOTION_DETECTION_ACTIVE", _bit(0), kanaltypen=KT_BEWEGUNG),

        Regel("STATE", _bit(0), kanaltypen=KT_EINGANG),
        Regel("CHANGE_OVER", _bit(7), kanaltypen=KT_EINGANG),
        Regel("TEMPERATURE_LIMITER", _bit(6), kanaltypen=KT_EINGANG),
        Regel("EXTERNAL_CLOCK", _bit(5), kanaltypen=KT_EINGANG),
        Regel("HUMIDITY_LIMITER", _bit(4), kanaltypen=KT_EINGANG),
        Regel("TACTILE_SWITCH", _bit(3), kanaltypen=KT_EINGANG),

        Regel("PRE_HUMIDITY_LIMITER", _bit(6), kanaltypen=KT_FLOOR),
        Regel("HUMIDITY_LIMITER", _bit(5), kanaltypen=KT_FLOOR),
        Regel("EXTERNAL_CLOCK", _bit(4), kanaltypen=KT_FLOOR),
        Regel("DEW_POINT_ALARM", _bit(3), kanaltypen=KT_FLOOR),
        Regel("EMERGENCY_OPERATION", _bit(2), kanaltypen=KT_FLOOR),
        Regel("FROST_PROTECTION", _bit(1), kanaltypen=KT_FLOOR),
        Regel("STATE", _bit(0), kanaltypen=KT_FLOOR),

        Regel("TEMPERATURE_LIMITER", _bit(3), kanaltypen=KT_WARTUNG),
        Regel("HUMIDITY_ALARM", _bit(2), kanaltypen=KT_WARTUNG),
        Regel("HEATING_COOLING", _bit(1), kanaltypen=KT_WARTUNG),
        Regel("DATE_TIME_UNKNOWN", _bit(0), kanaltypen=KT_WARTUNG),

        # Heizregler MIT Stellantrieb: das Byte traegt nur die Betriebsart.
        Regel("HEATING_COOLING", _bit(0), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", True)),
        # Heizregler OHNE Stellantrieb (Wandthermostat): volle Belegung.
        Regel("FROST_PROTECTION", _bit(7), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("PARTY_MODE", _bit(6), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("WINDOW_STATE", _bit(5), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("BOOST_MODE", _bit(4), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("SYSTEM_OPERATION_MODE_OFF", _bit(3), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("ROTARY_PUSH_WHEEL_USED", _bit(1), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),
        Regel("HEATING_COOLING", _bit(0), kanaltypen=KT_HEIZREGLER,
              braucht=("VALVE_STATE", False)),

        Regel("CONTROL_MODE_CENTRAL", _bit(4), kanaltypen=KT_FLOOR_DIRECT),
        Regel("HUMIDITY_ALARM", _bit(3), kanaltypen=KT_FLOOR_DIRECT),
        Regel("EMERGENCY_OPERATION", _bit(2), kanaltypen=KT_FLOOR_DIRECT),
        Regel("FROST_PROTECTION", _bit(1), kanaltypen=KT_FLOOR_DIRECT),
        Regel("STATE", _bit(0), kanaltypen=KT_FLOOR_DIRECT),

        Regel("PRESENCE_DETECTION_STATE", _bit(1), kanaltypen=KT_ANWESENHEIT),
        Regel("PRESENCE_DETECTION_ACTIVE", _bit(0), kanaltypen=KT_ANWESENHEIT),
    ),
    # 14 PERIOD (7 Byte) — zwei Zeitpunkte, der Monat beider in den Nibbles
    # von Byte 4 (Handler, Fall PERIOD):
    #     Start = [b0, b1, b4 >> 4, b5]      Ende = [b2, b3, b4 & 0xF, b6]
    # Dieselbe Ablage baut `daten_bytes` fuer die Senderichtung.
    14: (
        Regel("PARTY_TIME_START",
              lambda v: _zeitpunkt((v[0], v[1], (v[4] >> 4) & 0x0F, v[5]))),
        Regel("PARTY_TIME_END",
              lambda v: _zeitpunkt((v[2], v[3], v[4] & 0x0F, v[6]))),
    ),
    # 15 ILLUMINATION und 33 BRIGHTNESS (je 2 Byte) — derselbe Handlerzweig.
    # Das ZWEITE Vorkommen im selben Kanal ist die aktuelle Helligkeit
    # (`statusDataTypeOccurence == 1` -> CURRENT_ILLUMINATION).
    15: (
        Regel("ILLUMINATION", _beleuchtung, vorkommen=0, status=True),
        Regel("CURRENT_ILLUMINATION", _beleuchtung, vorkommen=1, status=True),
    ),
    33: (
        Regel("ILLUMINATION", _beleuchtung, vorkommen=0, status=True),
        Regel("CURRENT_ILLUMINATION", _beleuchtung, vorkommen=1, status=True),
    ),
    # 22 FLAG_REGISTER_24 (3 Byte) — INTEGER 0..2^24-1, unveraendert.
    22: (
        Regel("WEEK_PROGRAM_CHANNEL_LOCKS", lambda v: _ganz(v)),
    ),
}

# Woher wir wissen, dass die Regeln eines Statusdatentyps stimmen — nach der
# Leiter: `on-air` (selbst auf der Luft gemessen, Wert am Geraet
# gegengeprueft) > `mitschnitt` (aufgezeichnete Rahmen, die Handrechnung des
# Jars als Orakel) > `fhem-zeuge` > `beschreibungstreu` (nur aus dem
# Dekompilat) > `kein-zeuge`. Gedeutet wird, was mindestens dem Dekompilat
# folgt; nur `kein-zeuge` bleibt RAW_SDT<n>, auch wenn eine Regel dafuer da
# steht (Dirk, 02.09.2026: eine beschreibungstreue Regel ist kein Ratespiel,
# das Dekompilat IST die Beschreibung). Der Zeuge steht daneben, damit
# nachpruefbar bleibt, WAS die Stufe traegt — und wo noch keiner ist.
SDT_BELEGSTUFE = {
    0:  ("mitschnitt", "eTRV-C, 12 Rahmen (Gegenprobe: Handrechnung des Jars, am Geraet belegt)"),
    1:  ("on-air", "BWTH-A, 317 Rahmen: 23,3 °C / 68 % rF; dazu 85 Rahmen Gegenprobe: Handrechnung des Jars, am Geraet belegt"),
    2:  ("mitschnitt", "eTRV-C und BWTH-A (Gegenprobe: Handrechnung des Jars, am Geraet belegt)"),
    3:  ("mitschnitt", "eTRV-C, 11 Rahmen 2,8 .. 3,0 V (Gegenprobe: Handrechnung des Jars, am Geraet belegt)"),
    4:  ("on-air", "BWTH-A: Soll, Betriebsart, Profil; dazu Gegenprobe: Handrechnung des Jars, am Geraet belegt"),
    5:  ("on-air", "eTRV-C / eTRV-E-S: Ventilzustand, Boost, Fenster (Bench 31.08.2026)"),
    6:  ("mitschnitt", "eTRV-C: ERROR_CODE und SABOTAGE (Gegenprobe: Handrechnung des Jars, am Geraet belegt); "
                       "Bitlagen aus GeneralStateParameterFactory"),
    8:  ("on-air", "BWTH-A, 317 Rahmen: die vier Schaltkanaele (Fassung 2026.8.10)"),
    13: ("mitschnitt", "BWTH-A (Gegenprobe: Handrechnung des Jars, am Geraet belegt); Kanaltyp-Zweige ohne Geraet ausgelassen"),
    14: ("mitschnitt", "eTRV-F meldet den Abwesenheitszeitraum zurueck, den QCCU selbst gesetzt hatte "
                       "(31.08.2026, vier Rahmen; Start 17:09 -> 17:10 wie im Jar gerundet)"),
    15: ("on-air", "SMI55-A (03.09.2026): 0x39C0 = 1478,4 lx bei Tag, abgedeckt 0x01C3 = 45,1 lx — "
                   "die Skala stimmt am Geraet"),
    33: ("beschreibungstreu", "BRIGHTNESS: derselbe Handlerzweig wie ILLUMINATION, kein Geraet gesehen"),
    22: ("beschreibungstreu", "kein Geraet mit WEEK_PROGRAM_CHANNEL_LOCKS bisher auf der Luft"),
}
DEUTEN_AB = ("on-air", "mitschnitt", "fhem-zeuge", "beschreibungstreu")


# --- Stellbefehle ----------------------------------------------------------
#
# Ein Stellbefehl geht als DIRECT_EXECUTION_COMMAND hinaus
# (`ApplicationFrameType 6`; auf der Luft `0x86`, das obere Bit ist der
# Antwortwunsch). Der Rumpf ist `<Aktion> <Kanal> …` — so baut ihn
# `DirectExecutionCommandFrame.generatePayload()` und so liest ihn
# `setPayload()` zurueck:
#
#     Aktion 2 (EXECUTION_START)      danach EIN Pegelbyte
#     Aktion 128..159 (geraetespez.)  danach Paare <Datentyp> <Daten>
#
# Aktion, Lage und Datentyp sind die letzten Argumente der StateParameter-
# Fabrik im HMIPServer-Jar — `(directExecutionCode, dataIndex, dataType)`:
#
#   STATE                  createStateParameter        → 2, 0
#                          BoolToInteger({0,0,0,-56}) → WAHR ist 0xC8, denn
#                          das Byte einer EXECUTION_START ist ein PEGEL.
#   LEVEL                  createLevel                 → 2, 0
#                          DoubleToInteger(200.0, 0.0) → 0..1 wird 0..200
#   SET_POINT_TEMPERATURE  createSetPointTemperature…  → -128, 1, 2
#                          128 THERMOSTATIC_RADIATOR_VALVE,
#                          Datentyp 2 TEMPERATURE_SET_POINT (1 Byte)
#   BOOST_MODE             createBoostModeParameter    → -128, 1, 0
#                          Datentyp 0 LOGIC (1 Byte)
#   CONTROL_MODE           createControlModeParameter  → -128, 0, 4
#   CONTROL_DIFFERENTIAL_…  createControlDifferentialT… → -128, 1, 4
#                          DoubleToInteger(2.0, 0.0), MIN -10.0
#   ACTIVE_PROFILE         createActiveProfileParameter → -128, 0, 5
#                          IntegerToInteger(1.0, -1.0) → Profil 1 ist 0
#   PARTY_TIME_START       createPartyTimeParameter    → -128, 0, 3
#   PARTY_TIME_END         createPartyTimeParameter    → -128, 1, 3
#                          DateStringToInteger → VIER Byte je Zeitpunkt;
#                          Datentyp 3 PERIOD ist SIEBEN Byte und traegt BEIDE
#
# ⚠️ Bei CONTROL_MODE heisst der Datentyp 4 im Jar `DIFFERENTIAL_TEMPERATURE`
# — der Name passt nicht zum Parameter, die Zahl steht aber genau so in der
# Fabrik. Uebernommen wie sie dasteht und NICHT nach dem Namen zurechtgelegt;
# belegt ist damit die Herkunft, nicht die Wirkung. Das zeigt erst ein Geraet.
#
# Was hier fehlt, wird NICHT gesendet: ein Stellbefehl, dessen Aufbau wir uns
# ausdenken, ist schlimmer als gar keiner — er kommt beim Geraet an.
AKTION_START = 0x02
AKTION_THERMOSTAT = 0x80

# Die Datentypen aus `DirectExecutionDataType` (Wert, Laenge in Byte).
DT_LOGIC = 0x00                    # 1
DT_TEMPERATURE_SET_POINT = 0x02    # 1
DT_PERIOD = 0x03                   # 7
DT_DIFFERENTIAL_TEMPERATURE = 0x04  # 1
DT_UNSIGNED_INTEGER_16BIT = 0x05   # 2
DT_LEVEL = 0x08                    # 1  (DirectExecutionDataType.LEVEL)
DT_LEVEL_16BIT = 0x0F              # 2  (DirectExecutionDataType.LEVEL_16BIT)

# ⚠️ Ein Datentyp traegt oft MEHRERE Parameter in EINEM Byte, und `dataIndex`
# sagt, wo einer davon liegt. Das steht nicht im Rahmen — der Rahmen fuehrt
# nur `<Datentyp> <Daten>` (`DirectExecutionCommandFrame.setPayload`) —,
# sondern in `TransactionTaskFactory`, die den Wert VOR dem Einsetzen an seine
# Stelle schiebt und Parameter mit gleichem <Code, Datentyp, Occurrence> in
# dasselbe Byte verodert:
#
#   LOGIC                  0 -> (v&1)<<6 | 0x80    jeder Platz hat ein
#                          1 -> (v&1)<<4 | 0x20    MARKIERBIT, darum laesst
#                          2 -> (v&1)<<2 | 0x08    sich einer allein setzen
#                       sonst -> (v&1)    | 0x02
#   TEMPERATURE_SET_POINT  0 -> v<<6               Bits 7..6
#                       sonst -> v & 0x3F          Bits 5..0
#   DIFFERENTIAL_TEMPERATURE  ebenso
#   UNSIGNED_INTEGER_16BIT    zwei Byte, hohes zuerst
#
# ⚠️ QCCU hat den Wert bisher ROH eingesetzt. Fuer den Sollwert ging das gut:
# 4,5..30,5 °C sind 9..61 und liegen ohnehin in den unteren sechs Bit, und der
# Modus stand dann auf 0 — der am 30.08.2026 belegte Rahmen `80 01 02 2C` ist
# genau `Modus 0 | 44`. Fuer BOOST_MODE ging es NICHT: 0x01 statt 0x30 traf
# das Feld nicht, und das Geraet antwortete mit NAK (am HmIP-BWTH-A gemessen).
#
# ⚠️ HIER FEHLT DIE OCCURRENCE — und das geht nur gut, solange sie 0 ist.
# Der StateParameter-Konstruktor hat vier Laengen; die drei letzten Argumente
# sind `(directExecutionCode, dataIndex, dataType)`, die vierstellige Form
# haengt `dataTypeOccurrence` an (die fuenfstellige noch
# `dataTypeCharacteristic`). Die Occurrence sagt, das WIEVIELTE Feld dieses
# Datentyps im Rahmen gemeint ist: `TransactionTaskFactory` schluesselt seine
# TreeMap mit `code<<16 | dataType<<8 | occurrence`, und im LOGIC-Zweig legt
# sie fuer jede kleinere Occurrence ein LEERES Byte an — ein Parameter mit
# Occurrence 2 macht aus einem Feld also drei.
#
# Alle zehn Eintraege unten stehen auf der DREIstelligen Form, Occurrence
# also 0; `_rumpf` darf deshalb allein nach (Aktion, Datentyp) verodern. Wer
# hier einen Parameter aus der vierstelligen Form nachtraegt (z.B.
# COLOR_TEMPERATURE), muss beides nachziehen — sonst landet der Wert im
# falschen Feld, und das faellt nicht auf: der Rahmen bleibt wohlgeformt.
#
# Aufbau: (Aktion, Datentyp, dataIndex, Wert-fuer-WAHR)
# ⚠️ UND DIE TABELLE KENNT KEINE UNTERART. Sie ist allein nach dem
# Parameternamen geschluesselt — die Fabrik im Jar fuehrt denselben Namen aber
# mehrfach, je Kanal-Unterart, mit VERSCHIEDENER Aktion und verschiedenem
# Datentyp. Bei LEVEL sind es sechs Formen (aus der Kanaltyp-Tabelle des
# H2M-Projekts gegengeprueft, 31.08.2026):
#
#     LEVEL@default / @VENTILATION   Aktion 2,   Pegelbyte      <- das hier
#     LEVEL@blind                    Aktion 134, Datentyp 8
#     LEVEL@withColor                Aktion 141, Datentyp 8
#     LEVEL@shade                    Aktion 142, Datentyp 15
#     LEVEL@uniLight                 Aktion 144, Datentyp 8
#     LEVEL@withRoom                 Aktion 145, Datentyp 8
#
# Seit dem 02.09.2026 ist die Tabelle nach `NAME@form` geschluesselt; der
# blanke Name ist die Form `default`. Welche Form ein Kanal fuehrt, sagt die
# Kanaltyp-XML des Jars (`<parameter subtype="blind">LEVEL</parameter>`,
# im Paramset als `FASSUNG`) oder die Geraete-XML (`NAME@form` im Katalog) —
# beides liest `_variante`. Die Eintraege stammen aus den Fabriken
# (`createLevel(name, code, occurrence)` -> Datentyp 8, `createLevel16Bit`
# -> Datentyp 15, `createStateParameter(name, code, occurrence)` -> Datentyp
# 8 mit 0xC8 fuer wahr); alle mit Occurrence 0 und Index 0. Eine Form, die
# hier fehlt, wird NICHT auf `default` zurueckgebogen, sondern laut
# abgelehnt (`_form_unbekannt`) — ein wohlgeformter falscher Rahmen faellt
# sonst nie auf.
STELLBEFEHLE = {
    "STATE":                 (AKTION_START, None, 0, 0xC8),
    "STATE@watering":        (141, DT_LEVEL, 0, 0xC8),
    "LEVEL":                 (AKTION_START, None, 0, 0xC8),
    # Fensterantrieb: ENUM NO_VENTILATION/VENTILATION als Pegelbyte 0/200
    # (`createLevelWindowDrive`, StringEnumToInteger {0, 200}).
    "LEVEL@VENTILATION":     (AKTION_START, None, 0, 0xC8),
    "LEVEL@blind":           (134, DT_LEVEL, 0, 0xC8),
    "LEVEL@withColor":       (141, DT_LEVEL, 0, 0xC8),
    "LEVEL@shade":           (142, DT_LEVEL_16BIT, 0, 0xC8),
    "LEVEL@uniLight":        (144, DT_LEVEL, 0, 0xC8),
    "LEVEL@withRoom":        (145, DT_LEVEL, 0, 0xC8),
    "SET_POINT_TEMPERATURE": (AKTION_THERMOSTAT, DT_TEMPERATURE_SET_POINT, 1, 1),
    "SET_POINT_MODE":        (AKTION_THERMOSTAT, DT_TEMPERATURE_SET_POINT, 0, 1),
    "BOOST_MODE":            (AKTION_THERMOSTAT, DT_LOGIC, 1, 1),
    "CONTROL_MODE":          (AKTION_THERMOSTAT, DT_DIFFERENTIAL_TEMPERATURE, 0, 1),
    "CONTROL_DIFFERENTIAL_TEMPERATURE":
                             (AKTION_THERMOSTAT, DT_DIFFERENTIAL_TEMPERATURE, 1, 1),
    "ACTIVE_PROFILE":        (AKTION_THERMOSTAT, DT_UNSIGNED_INTEGER_16BIT, 0, 1),
    "ACTIVE_PROFILE@wth2":   (AKTION_THERMOSTAT, DT_UNSIGNED_INTEGER_16BIT, 0, 1),
    "PARTY_TIME_START":      (AKTION_THERMOSTAT, DT_PERIOD, 0, 1),
    "PARTY_TIME_END":        (AKTION_THERMOSTAT, DT_PERIOD, 1, 1),
}

# ⚠️ Die Betriebsart wird NIE allein geschrieben. Am 30.08.2026 am HmIP-BWTH-A
# gemessen: `80 01 02 00` (Modus 0 in Bit 7..6, sonst nichts) liess die
# Betriebsart auf 1 stehen und zog den SOLLWERT auf 5,0 °C — das Geraet las das
# ganze Byte als Sollwert. Der Grund steht im Jar, in
# `de/eq3/cbcs/statemanagement/rules/evaluation/stateRules.json`
# (`stateRestoreDefinitions`): die Zentrale schreibt einen SATZ von Parametern,
# und der eigentliche Umschalter ist CONTROL_MODE im Datentyp 4 — nicht die
# oberen zwei Bit des Datentyps 2.
#
#   SET_POINT_MODE == 1  ->  CONTROL_MODE, CONTROL_DIFFERENTIAL_TEMPERATURE 0,
#                            SET_POINT_MODE, SET_POINT_TEMPERATURE
#   SET_POINT_MODE == 0  ->  CONTROL_MODE, CONTROL_DIFFERENTIAL_TEMPERATURE 0,
#                            ACTIVE_PROFILE
#
# `None` heisst „den heutigen Wert des Geraets nehmen" — genau das meint
# `{"type": "STATE_PARAMETER_VALUE"}` in der Regel.
# ⚠️ Was hier steht, darf NUR gemeinsam hinaus. Faellt ein Wert durch die
# Satz-Tabellen, ist das KEIN Grund, ihn als Einzelfeld zu schicken — beim
# Modus waere das genau der Rahmen, der am Geraet den Sollwert zerreisst
# (`80 01 02 80` fuer AWAY).
#
# Den DRITTEN Satz — den Abwesenheitsmodus — kennt `stateRules.json` NICHT;
# dort stehen nur 0 und 1. Ihn stellt die Gegenstelle selbst zusammen:
# `aiohomematic` schickt SET_POINT_MODE=2 zusammen mit PARTY_TIME_START und
# PARTY_TIME_END als EIN putParamset, beim Einschalten zusaetzlich mit
# SET_POINT_TEMPERATURE (`model/custom/climate.py`,
# `enable_away_mode_by_calendar` / `disable_away_mode`; AWAY = 2). Daraus
# baut die Zentrale EINEN Rahmen: Datentyp 2 traegt Modus (Bit 7..6) und
# Sollwert, Datentyp 3 die beiden Zeitpunkte. Kommt der Modus 2 ohne sie,
# bleibt es beim Korb.
#
# ⚠️ Die beiden Zeitpunkte teilen sich ebenfalls EIN Feld — sieben Byte, der
# Monat in Nibbles. Einer allein setzt den anderen auf den 0. Tag des
# 0. Monats; darum verlangen sie einander.
#
# Aufbau: Parameter -> {Wert: was mitgeschickt sein MUSS}. `None` als
# Schluessel gilt fuer jeden Wert.
NUR_IM_SATZ = {
    "SET_POINT_MODE":   {2: ("PARTY_TIME_START", "PARTY_TIME_END")},
    "PARTY_TIME_START": {None: ("PARTY_TIME_END",)},
    "PARTY_TIME_END":   {None: ("PARTY_TIME_START",)},
}

KOMPOSITE = {
    ("SET_POINT_MODE", 1): (("CONTROL_MODE", 1),
                            ("CONTROL_DIFFERENTIAL_TEMPERATURE", 0.0),
                            ("SET_POINT_MODE", 1),
                            ("SET_POINT_TEMPERATURE", None)),
    ("SET_POINT_MODE", 0): (("CONTROL_MODE", 0),
                            ("CONTROL_DIFFERENTIAL_TEMPERATURE", 0.0),
                            ("ACTIVE_PROFILE", None)),
}

# Die Form, in der ein Zeitpunkt hereinkommt und wieder hinausgeht — dieselbe
# Zeichenkette, die `DateStringToInteger.DATE_TIME_PATTERN` im Jar fuehrt
# ("yyyy_MM_dd HH:mm") und die `aiohomematic` schreibt.
DATUM_FORM = "%Y_%m_%d %H:%M"


def satz_wert(value):
    """Der Wert, unter dem ein Satz in `KOMPOSITE`/`NUR_IM_SATZ` steht.

    Ueber XML-RPC kommt die Betriebsart als Zahl herein, aus FHEM/HMCCU aber
    auch als Zeichenkette. Ohne diese Angleichung faende `KOMPOSITE` den Satz
    zu "1" nicht — und der Modus ginge als Einzelfeld hinaus, also genau als
    der Rahmen, der am Geraet den Sollwert zerreisst.
    """
    try:
        zahl = float(value)
    except (TypeError, ValueError):
        return value
    return int(zahl) if zahl == int(zahl) else zahl


def datum_roh(text):
    """Einen Zeitpunkt in seine VIER Rohbytes bringen.

    Abgeschrieben aus `DateStringToInteger.convertLogicalToPhysical`
    (HMIPServer-Jar, `devicedescription/typeconversion/converter`):

        zeit    = Stunde * 12 + Minute / 5, aufgerundet ab Rest 3
                  (also FUENF-Minuten-Schritte, 0..288)
        data[0] = zeit >> 1
        data[1] = (zeit & 1) << 7 | Tag
        data[2] = Monat (1..12)
        data[3] = Jahr - 2000

    Rueckgabe: vier Byte, oder None, wenn der Text kein Zeitpunkt dieser Form
    ist oder das Jahr nicht in ein Byte passt.
    """
    try:
        t = time.strptime(str(text), DATUM_FORM)
    except (TypeError, ValueError):
        return None
    zeit = t.tm_hour * 12 + t.tm_min // 5
    if t.tm_min % 5 > 2:
        zeit += 1
    jahr = t.tm_year - 2000
    if not 0 <= jahr <= 0xFF:
        return None
    return bytes((zeit >> 1, ((zeit & 1) << 7) | t.tm_mday, t.tm_mon, jahr))


def daten_bytes(datentyp, dataindex, roh):
    """Den Rohwert an seine Stelle im Datenfeld schieben.

    Rueckgabe: Hexziffern, oder None, wenn der Wert dort nicht hineinpasst.
    Die Faelle sind die aus `TransactionTaskFactory` — kein eigener Entwurf.
    """
    if datentyp is None:                       # EXECUTION_START: ein Pegelbyte
        return f"{roh:02X}" if 0 <= roh <= 0xFF else None
    if datentyp == DT_LOGIC:
        if not 0 <= roh <= 1:
            return None
        lage = {0: (6, 0x80), 1: (4, 0x20), 2: (2, 0x08)}.get(dataindex, (0, 0x02))
        return f"{((roh & 1) << lage[0]) | lage[1]:02X}"
    if datentyp in (DT_TEMPERATURE_SET_POINT, DT_DIFFERENTIAL_TEMPERATURE):
        if dataindex == 0:
            # ⚠️ Nur zwei Bit. Wer hier den Sollwert einsetzt, schreibt einen
            # Modus und loescht den Sollwert.
            return f"{(roh << 6) & 0xFF:02X}" if 0 <= roh <= 3 else None
        # ⚠️ Der untere Platz ist SECHS Bit im Zweierkomplement. Das Jar
        # maskiert ein Java-`byte` (`(byte)(v & 0x3F)`); Pythons `&` liefert
        # fuer negative Zahlen dasselbe Ergebnis. Ohne die negative Haelfte
        # waere CONTROL_DIFFERENTIAL_TEMPERATURE (MIN -10,0 -> roh -20) nicht
        # zu senden.
        return f"{roh & 0x3F:02X}" if -0x20 <= roh <= 0x3F else None
    if datentyp == DT_LEVEL:
        # LEVEL(8, 1): das Byte selbst, kein Index. Der Wandler liefert vier
        # Byte big-endian, der Rahmen traegt davon so viele, wie der Datentyp
        # lang ist (`DirectExecutionCommandFrame.setPayload`: `readBytes(…,
        # dataType.getLength())`).
        return f"{roh:02X}" if 0 <= roh <= 0xFF else None
    if datentyp == DT_LEVEL_16BIT:
        # LEVEL_16BIT(15, 2): zwei Byte big-endian, 0..50500 bei Faktor 50000.
        return f"{roh:04X}" if 0 <= roh <= 0xFFFF else None
    if datentyp == DT_PERIOD:
        # ⚠️ Hier ist `roh` KEINE Zahl, sondern die vier Byte aus `datum_roh`.
        # Zwei Zeitpunkte teilen sich SIEBEN Byte; `dataIndex` sagt, welcher.
        # Aus `TransactionTaskFactory`, Zweig PERIOD:
        #     Lage 0 (Start)  [d0, d1,  0,  0, d2<<4, d3,  0]
        #     Lage 1 (Ende)   [ 0,  0, d0, d1, d2&0xF,  0, d3]
        # Der Monat liegt also in NIBBLES, Uhrzeit/Tag und Jahr in ganzen
        # Byte — verodert ergeben beide zusammen ein Feld.
        if not isinstance(roh, (bytes, bytearray)) or len(roh) != 4:
            return None
        d0, d1, d2, d3 = roh
        if dataindex == 0:
            feld = bytes((d0, d1, 0, 0, (d2 << 4) & 0xFF, d3, 0))
        else:
            feld = bytes((0, 0, d0, d1, d2 & 0x0F, 0, d3))
        return feld.hex().upper()
    if datentyp == DT_UNSIGNED_INTEGER_16BIT:
        return f"{roh:04X}" if 0 <= roh <= 0xFFFF else None
    return None

APP_RESP_REQ = 0x80
# ⚠️ Das erste Byte des Anwendungskopfes traegt DREI Dinge, nicht zwei:
#
#     Bit 7 (0x80)  Antwort erwuenscht
#     Bit 6 (0x40)  WACH BLEIBEN
#     Bit 5..0      Rahmentyp
#
# Belegt aus `ApplicationHeader.parse`/`generate` (HMIPServer-Jar):
# `stayAwakeBit = (data[index] & 0x40) != 0`, und beim Bauen
# `tempvalue |= 0x40`. QCCU hat das obere Bit immer gesetzt und das mittlere
# nie — fuer ein Geraet, das dauernd hoert, ohne Folgen. Fuer eines, das nur
# kurz aufwacht, ist es der Unterschied zwischen „hoert den naechsten Frame"
# und „schlaeft wieder ein".
APP_STAY_AWAKE = 0x40

# Hoerertypen aus dem Betriebsmodus des Anlernrufs (untere vier Bit).
# `ListenerMode` im Jar fuehrt zehn Werte; `isNonPermanentListener()` gibt
# fuer GENAU DREI wahr zurueck — nachgelesen, nicht abgeleitet:
#
#     4  EVENT_LISTENER
#     5  EVENT_LISTENER_WITH_POWER_SAVE
#     8  CYCLIC_LISTENER
#
# Alles Uebrige (0 permanent, 1/3 Burst, 9/11 zyklisch MIT Burst, 12/14
# permanent an Draht/Backbone) hoert dauerhaft oder laesst sich mit einem
# Burst wecken. Nur diese drei muss man abwarten.
LM_NICHT_STAENDIG = (4, 5, 8)

# Hoerertypen, die einen VORLAUF brauchen, damit sie einen Befehl ueberhaupt
# hoeren. `HMIPAbstractWriterWorker.class:162-178` bildet ab:
#     1  SINGLE_BURST_LISTENER              -> BurstMode.Burst        (Byte 1)
#     9  CYCLIC_AND_SINGLE_BURST_LISTENER   -> BurstMode.Burst        (Byte 1)
#     3  TRIPLE_BURST_LISTENER              -> BurstMode.TrippleBurst (Byte 3)
#    11  CYCLIC_AND_TRIPLE_BURST_LISTENER   -> BurstMode.TrippleBurst (Byte 3)
# alles Uebrige auf BurstMode.Normal. Auf dem Draht zum Coprozessor der echten
# Zentrale: Opcode 0x03, dann dieses Byte, dann der Rahmen
# (`HMIPStack.sendProtocolFrame`; dort kommt `LowLevelMacOption` — und damit
# die Frequenzwahl — ueberhaupt nicht vor).
#
# ⚠️ Nicht zu verwechseln mit `LM_NICHT_STAENDIG`: das sind die Hoerer, fuer
# die die Zentrale einen Befehl ZURUECKLEGT (`isNonPermanentListener()`), das
# hier sind die, denen sie ihn mit Vorlauf HINTERHERSCHICKT. Kein Wert steht
# in beiden Listen ausser der 8 in keiner von beiden — ein CYCLIC_LISTENER
# ohne Burst wird zurueckgelegt, ein zyklischer MIT Burst wird geweckt.
#
# ⚠️ `isCyclicListener() = {8,9,11}` ist im ganzen Jar TOTER CODE (kein
# Aufrufer). Wer daraus eine Wartefach-Menge ableitet, baut etwas nach, das
# das Original nicht tut.
#
# GEMESSEN am HmIP-eTRV-C (Typ 325, Hoerertyp 11), 01.09.2026, an einem
# eq-3-Dual-Copro: zwei Rahmen, die sich in genau einem Byte unterscheiden,
# je sechs Versuche abwechselnd, jeweils fruehestens 5 s nach dem letzten
# Geraeterahmen — Byte 0 null Antworten, Byte 3 sechs von sechs
# (Fisher exakt, zweiseitig p = 0,0022). Auf der Luft ist der Unterschied ein
# rund 356 ms laengerer Vorlauf (385,5 ms gegen 29,5 ms Huellkurve) auf
# DERSELBEN Frequenz — 868,28..868,32 MHz bei beiden Stufen, gegengeprueft
# durch Verschieben der Aufnahmemitte.
LM_BURST = (1, 3, 9, 11)
# Byte 3 fuer die Dreifach-Stufe, Byte 1 fuer die Einfach-Stufe.
LM_BURST_STUFE = {1: 1, 9: 1, 3: 3, 11: 3}

# Wie lange ein Vorlauf jede Wartezeit verlaengert. Gemessen am
# eq-3-Coprozessor (HackRF, Huellkurve auf 868,3 MHz): 386 ms mit Vorlauf
# gegen 29,5 ms ohne, also rund 356 ms mehr. Fuer BEIDE Stufen derselbe Wert —
# je drei Messungen, auf 0,5 ms gleich (02.09.2026). Ein Unterschied zwischen
# Stufe 1 und Stufe 3 ist damit auf der Luft nicht nachweisbar; der Name
# „TrippleBurst" beschreibt also nicht die Dauer.
# 0,5 s laesst Luft fuer die serielle Uebertragung und den Programmpfad im
# Stick. Zu wenig zu warten ist der teurere Fehler: es erzeugt ein
# „kein Urteil" fuer eine Sendung, die noch laeuft.
BURST_ZUSCHLAG = 0.5


def burst_zuschlag(stufe):
    return BURST_ZUSCHLAG if stufe else 0.0

RXF_FOR_US = 0x01

# Das erste Nutzlastbyte eines STATUS-Frames traegt neben dem Inhaltsformat
# vier Zustandsbits. Belegt aus `StatusFrame.setPayload()` (HMIPServer-Jar):
#
#     lowBat          = (payload[0] & 0x80) != 0
#     configPending   = (payload[0] & 0x40) != 0
#     piggybackAppACK = (payload[0] & 0x20) != 0
#     dutyCycle       = (payload[0] & 0x10) != 0
#     booted          = (payload[0] & 0x04) != 0
#     contentFormat   = payload[0] & 0x03
#
# ⚠️ `configPending` ist die Auskunft des GERAETS, dass seine Konfiguration
# unvollstaendig ist — es wartet auf die Zentrale. Es einfach mit ACK zu
# quittieren, wie QCCU es tut, laesst das Geraet in diesem Zustand stehen.
# Gemeldet wird es hier, damit der Zustand ueberhaupt sichtbar ist; einen
# Schreibweg fuer die Konfiguration gibt es weiterhin nicht.
# Nach dem Anlernen richtet die Zentrale die INTERNE VERDRAHTUNG des Geraets
# ein: je Paar ein CONFIGURATION-Frame (`ApplicationFrameType.CONFIGURATION(1)`)
# mit `ConfigurationRequestType.CREATE_LINK(1)`, in beide Richtungen. Die
# Nutzlast beginnt laut `ConfigurationFrame.parsePayload()` mit Kanalnummer und
# Anfragetyp, danach folgen Partneradresse und Partnerkanal.
#
# ⚠️ WELCHE Kanaele verknuepft werden, wird NICHT geraten: es steht in der
# Geraetebeschreibung des Herstellers und liegt seit 2026.8.35 im Katalog
# (`Tables.links_of`, Feld `links`, aus `<internalLink sourceIndex targetIndex>`).
# 80 der 304 Geraetetypen fuehren solche Links, bis zu acht Stueck.
#
# Gegenprobe: die Beschreibung der HmIP-PS-2 sagt 1 -> 3 — genau die beiden
# Kanaele, die im Referenzmitschnitt verknuepft wurden (Geraetetaste ch1 auf
# das eigene Relais ch3). Die HmIP-BWTH-A sagt 8 -> 10, also Heizbedarf auf das
# eigene Relais; die fest verdrahteten PS-2-Zahlen trafen dort ch1 und ch3 und
# damit die falschen Kanaele.
#
# ⚠️ OHNE DIESE FRAMES BRICHT DAS GERAET DEN ANLERNVORGANG AB. An der BWTH-A
# gemessen (30.08.2026, `luft_bwth.log`): Join sauber, zwei Statusmeldungen —
# dann Stille, nach 39,7 s ein SERVICE-Rundruf an `f00001` und nach 47,1 s
# wieder Anlernrufe alle 10 s aus einer NEUEN Funkadresse, gleiche SGTIN.
# Sobald die Frames gesendet wurden, blieb dasselbe Geraet — in einem Lauf
# sogar OHNE dass es sie quittierte (`luft_exp3.log`). Belegt ist also, dass
# sie RAUSGEHEN muessen; dass das Geraet sie annimmt, ist dafuer nicht noetig.
#
# Die appSeq beginnt bei 5 und geht in Zweierschritten — so stand es im
# Mitschnitt der PS-2 (0x05 und 0x07).
LINK_APPSEQ_START = 0x05
LINK_APPSEQ_SCHRITT = 2
LINK_ERSTE_PAUSE = 2.35    # zu frueh gesendet nimmt das Geraet sie nicht an
LINK_PAUSE = 0.3
# Wiederholung, wenn die ANSWER des Geraets ausbleibt. Die Zentrale
# wiederholt im 220-ms-Takt (`Transaction.Retry.Interval`, SendFrameTask) und
# gibt die Konfigurationstransaktion erst nach 600 s auf. So lange halten wir
# das Anlernfenster nicht auf; drei Anlaeufe decken den einzelnen
# Empfangsverlust ab, um den es hier geht.
# Die ANSWER des Geraets deuten — `AnswerType` im HMIPServer-Jar:
#
#     ACK                              0    angenommen
#     ACK_DEPRECATED_WITH_STATUS_INFO  1    angenommen, Zustand liegt bei
#     NAK                            128    abgelehnt
#     NAK_BUSY                       129
#     NAK_OUT_OF_MEMORY              130
#     NAK_PEER_UNKNOWN               132
#     NAK_INVALID_CHANNEL            133
#     NAK_OPERATION_NOT_ALLOWED      134
#
# ⚠️ NICHT das ganze Byte vergleichen. `AnswerType.getByValue` maskiert mit
# 0x8F, und `AnswerFrame.setPayload` liest Bit 6 (0x40) als `configPending` —
# ein Geraet mit anstehender Konfiguration quittiert also mit 0x40, und wer
# „alles ausser 0 ist eine Ablehnung" rechnet, haelt genau dieses ACK fuer
# einen Korb und wiederholt einen Befehl, der angekommen ist.
ANTWORT_MASKE = 0x8F
ANTWORT_CONFIG_PENDING = 0x40
ANTWORT_NAMEN = {
    0x00: "ACK", 0x01: "ACK_MIT_ZUSTAND",
    0x80: "NAK", 0x81: "NAK_BUSY", 0x82: "NAK_OUT_OF_MEMORY",
    0x84: "NAK_PEER_UNKNOWN", 0x85: "NAK_INVALID_CHANNEL",
    0x86: "NAK_OPERATION_NOT_ALLOWED",
}


def antwort_deuten(roh):
    """(angenommen, Klartext) zu einem ANSWER-Byte."""
    art = (roh or 0) & ANTWORT_MASKE
    name = ANTWORT_NAMEN.get(art, f"unbekannt 0x{art:02X}")
    if (roh or 0) & ANTWORT_CONFIG_PENDING:
        name += "+configPending"
    return art in (0x00, 0x01), name


class Stellauftrag:
    """Ein angenommener Stellbefehl und sein Ausgang.

    `on_set` kehrt sofort zurueck — der Arbeitsfaden sendet. Wer wissen will,
    was aus dem Befehl wurde, wartet HIER, statt den Wert vorwegzunehmen.
    Drei Ebenen, getrennt gefuehrt, weil sie Verschiedenes bedeuten:

      mac      True/False — die Kurzquittung („angekommen"); None = nie
               hinausgegangen (keine Funkadresse, kein belegter Rahmen)
      antwort  True/False/None — ANSWER bzw. Huckepack-Quittung
               („angenommen" / „abgelehnt"); None = keine Auskunft auf
               Anwendungsebene
      klartext der Grund in Worten (Art der Ablehnung, „keine ANSWER",
               „nicht stellbar")
    """

    __slots__ = ("ev", "mac", "antwort", "klartext")

    def __init__(self):
        self.ev = threading.Event()
        self.mac = None
        self.antwort = None
        self.klartext = "offen"

    def fertig(self, mac, antwort, klartext):
        self.mac, self.antwort, self.klartext = mac, antwort, klartext
        self.ev.set()

    def warten(self, sekunden):
        """True, wenn der Ausgang binnen `sekunden` feststeht."""
        return self.ev.wait(sekunden)


LINK_VERSUCHE = 3
LINK_WIEDERHOLUNG = 0.22
# Wie lange auf die ANSWER gewartet wird. Im Mitschnitt antwortete das Geraet
# nach 150–180 ms (`luft_exp4.log`); 600 ms lassen Luft, ohne den Anlernvorgang
# spuerbar zu dehnen.
LINK_ANTWORT_ZEIT = 0.6
# Wie lange ein Anlernvorgang ohne Bestaetigung offen bleibt. Die Zentrale
# behaelt das Geraet unbegrenzt (der Eintrag entsteht schon beim Anlernruf);
# hier ist es ein Fenster, damit ein wirklich abgesprungenes Geraet nicht
# spaeter aus Versehen auftaucht. Zwei Minuten decken den Fall ab, um den es
# geht: das Geraet meldet sich nach dem Annehmen binnen Sekunden.
NACHTRAG_FENSTER = 120.0

SF_LOWBAT = 0x80
SF_CONFIG_PENDING = 0x40
SF_DUTYCYCLE = 0x10
SF_BOOTED = 0x04

CT_NETWORK_MGMT = 1
CT_ICMP = 2
CT_MAC_CONTROL = 4
ICMP_ECHO_REPLY = 0x80
ICMP_ECHO_REQUEST = 0x81
ICMP_ROUTER_SOLICITATION = 0x85
ICMP_ROUTER_ADVERTISEMENT = 0x86
ICMP_NEIGHBOR_SOLICITATION = 0x87
ICMP_NEIGHBOR_ADVERTISEMENT = 0x88
ICMP_NAME = {
    0x01: "DESTINATION_UNREACHABLE", 0x02: "PACKET_TOO_BIG",
    0x03: "TIME_EXCEEDED", 0x04: "PARAMETER_PROBLEM",
    ICMP_ECHO_REPLY: "ECHO_REPLY", ICMP_ECHO_REQUEST: "ECHO_REQUEST",
    ICMP_ROUTER_SOLICITATION: "ROUTER_SOLICITATION",
    ICMP_ROUTER_ADVERTISEMENT: "ROUTER_ADVERTISEMENT",
    ICMP_NEIGHBOR_SOLICITATION: "NEIGHBOR_SOLICITATION",
    ICMP_NEIGHBOR_ADVERTISEMENT: "NEIGHBOR_ADVERTISEMENT",
    0x89: "REDIRECT_MESSAGE",
}
ADDR_ALL_DEVICES = "f00001"
ADDR_ALL_ROUTERS = "f00002"
ADDR_ANY_ROUTER = "e00002"
GROUP_ADDR = {
    ADDR_ALL_DEVICES: "ALL_DEVICES", ADDR_ALL_ROUTERS: "ALL_ROUTERS",
    "f00003": "ALL_ACCESS_CONTROLLERS", "f00004": "ALL_THERMOSTATS",
    "f00005": "ALL_SMOKE_SENSORS", "f00081": "ALL_WIRED_DEVICES",
    ADDR_ANY_ROUTER: "ANY_ROUTER",
}
MAC_ROLE_CENTRAL = 3


class _Job:
    """Ein Schreibauftrag auf der seriellen Leitung."""
    __slots__ = ("cmd", "kind", "verdict", "written", "done", "not_before",
                 "expect", "reply", "burst")

    def __init__(self, cmd, kind, not_before=0.0, expect=None, burst=0):
        self.cmd = cmd
        self.kind = kind
        # Burst-Stufe (0/1/3), mit der dieser Auftrag hinausgeht. Steht am
        # Job, weil der Sender daran seine Wartezeit bemisst: ein Vorlauf
        # haelt den Stick rund 360 ms zusaetzlich fest.
        self.burst = burst
        self.verdict = None
        self.not_before = not_before
        self.expect = expect
        self.reply = None
        self.written = threading.Event()
        self.done = threading.Event()

    @property
    def expects_verdict(self):
        return self.kind in ("cmd", "answer")


def _nur_zeichen(text):
    """Alles wegwerfen, was kein Buchstabe und keine Ziffer ist.

    ⚠️ Der Aufkleber wird abgetippt oder aus einer Nachricht kopiert — mit
    Bindestrichen, Leerzeichen, manchmal beidem. Frueher fielen nur die
    Bindestriche weg: wer ihn mit Leerzeichen eingab, bekam „hat 30 statt 26
    Zeichen" und suchte den Fehler bei sich (Anwendermeldung 19.08.2026). Was
    zaehlt, sind die 26 Zeichen — wie sie gruppiert sind, geht uns nichts an.
    """
    return "".join(c for c in (text or "") if c.isalnum())


def zufaellige_adresse(gesperrt=()):
    """Eine Funkadresse wuerfeln, die weder Rundruf noch Sammeladresse ist
    und keiner der uebergebenen gleicht (Vergleich auf die oberen 16 Bit)."""
    while True:
        b = secrets.token_bytes(3)
        if b[0] in (0x00, 0xE0, 0xF0, 0xFF):
            continue
        adr = b.hex()
        if any(adr[:4] == g[:4] for g in gesperrt if g):
            continue
        return adr


class Radio:
    SEQ_STEP = 64
    MAC_SEQ_LEAP = 256

    def __init__(self, port, qccu, tables, baud=38400, verbose=True,
                 state_file=None, raw_log=None, answer=True,
                 answer_delay=0.075, icmp_answer=True):
        self.qccu = qccu
        self.t = tables
        self.verbose = verbose
        self.icmp_answer = icmp_answer
        self.icmp_seen = {}
        self.mac_seq = 0
        self.answer_enabled = answer
        self.answer_delay = answer_delay
        # Den Anschluss merken: der Waechter prueft daran, ob der Stick noch
        # da ist (ein verschwundenes Geraet laesst das Objekt bestehen).
        self.port = port
        self.ser = serial.serial_for_url(port, baud, timeout=0.3, exclusive=True)
        # Ist der Zugang unbrauchbar geworden? Das Objekt bleibt danach
        # bestehen (andere Faeden halten es noch), taugt aber zu nichts mehr;
        # der Waechter in qccu.py sieht hier nach und bindet neu an.
        self.tot = False
        self.tot_grund = None
        self._lesefehler = 0
        self.by_hmid = {}
        # Fremde Absender, ueber die schon einmal berichtet wurde.
        self._fremd_gemeldet = set()
        self.appseq = {}
        self.devseq = {}
        self.lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = False
        self.own_addr = None
        self.netzschluessel_fehlt = False
        self._acked = {}
        # Funkadresse -> zuletzt gemeldeter Empfangspegel (dBm).
        self._rssi = {}
        # Funkadresse -> Ereignis: das Geraet hat den Ausschluss angenommen.
        self._exclude_ready = {}
        # (Funkadresse, appSeq) -> Ereignis: die ANWENDUNGS-Quittung des
        # Geraets. ⚠️ Nicht dasselbe wie `_acked`: das ist die MAC-Quittung
        # („angekommen"), dies hier die ANSWER der Anwendungsschicht
        # („angenommen"). Das Jar unterscheidet beides ebenfalls und ordnet
        # die ANSWER ueber die appSeq zu (`ApplicationTask.
        # evaluateTaskResponse`: fremde appSeq wird ignoriert).
        self._app_ack = {}
        # Funkadresse -> Befehle, die auf ein Lebenszeichen warten. Fuer
        # Geraete, die nicht staendig hoeren; im Jar der
        # `PendingDeviceCommandsHolder`.
        self._wartend = {}
        self._zustellung = set()       # Geraete, an die gerade nachgereicht wird
        self._antwort_offen = {}       # hmid -> letzter Quittungs-/Zeitauftrag an das Geraet
        self._zuletzt = {}             # hmid -> zuletzt an das Geraet GESCHRIEBENER Auftrag
        self._acked_job = {}           # hmid -> Auftrag, dessen Kurzquittung erwartet wird
        self._tastenzaehler = {}       # (hmid, Kanal) -> letzter Tastenzaehler eines langen Drucks
        self._letzter_druck = {}       # (hmid, Kanal) -> (Zaehler, lang, respReq) — gegen Wiederholungen
        # Funkadresse -> nachzuholender Anlernabschluss, wenn die
        # Bestaetigung ausgeblieben ist.
        self._nachtrag = {}
        # (Geraetetyp, Kanal) -> VALUES-Paramset. Beim Deuten einer
        # Statusmeldung wird bis zu drei Dutzend Mal nach einer Beschreibung
        # gefragt; `paramset_of` baut jedes Mal ein neues Woerterbuch aus
        # Kanaltyp-Liste und Geraete-Ergaenzungen. Die Tabellen stehen ab dem
        # Start fest, also wird das einmal gerechnet.
        self._pset_cache = {}

        self.vlen = {}
        for name, e in (self.t.sdt or {}).items():
            if "type" in e and "len" in e and e["type"] >= 0 and e["len"] > 0:
                self.vlen[e["type"]] = e["len"]
        self._sdt_unbekannt = set()
        # Zuletzt gemeldeter Stand von CONFIG_PENDING je Geraet — nur damit
        # der Wechsel einmal im Protokoll steht statt in jedem Frame.
        self._config_pending = {}

        self.tx_timeout = 0.4
        self.tx_tries = 3
        self.verdict_timeout = 1.2
        self.ask_timeout = 1.5

        self.pair_until = 0.0
        self.pair_key = None
        self.pair_key_src = ""
        self._pair_busy = False
        self._pair_expect = None
        self._pair_ok = threading.Event()
        self.pair_next_addr = None
        self.pair_last = ""
        # Geraete, die angelernt werden WOLLEN — der Posteingang.
        # Funkadresse -> {"sgtin", "devtype", "zuerst", "zuletzt", "anzahl"}
        self._fremde = {}
        # Geraete, die mit unserem Netzschluessel funken, aber nicht (mehr)
        # im Bestand stehen — siehe `_pruefe_verwaist`.
        self._verwaist = {}
        # Kennung -> zuletzt bekannte Funkadresse eines ausgeschlossenen
        # Geraets, damit ein Wiederanlernen die alte Spur mitnimmt.
        self._ehemals = {}
        self._pair_q = queue.Queue()
        self._cmdq = queue.Queue()
        self.cul = None
        # Zweiter Mitleser fuer die BidCoS-Schnittstelle (`qccu_bidcos_rpc`).
        # ⚠️ Er bekommt dieselbe Zeile wie der CUL-Zugang, NICHT statt seiner:
        # solange FHEM ueber Port 2000 die Zentrale spielt, liest die eigene
        # BidCoS-Seite nur mit.
        self.bidcos = None

        self._txq = queue.Queue()
        self._ansq = queue.Queue()
        self._ans_hold = None
        self._pending = None
        self._pending_lock = threading.Lock()
        self._gate = threading.Event()
        self._gate.set()

        self._raw = None
        self._log_lock = threading.Lock()
        self.measure = bool(raw_log)
        self._raw_zeilen = 0
        self._raw_umbruch_aus = False
        if raw_log:
            self._raw = open(raw_log, "a", buffering=1)
            self._log("##", f"--- Start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        self.counters = None
        # Rauschboden, Spitzenwert, PLL- und Kalibrierzaehler — None, solange
        # der Stick sie nicht liefert (aeltere Firmware als 2.0.71).
        self.funkgute = None
        # Die vollstaendige Antwort des Sticks auf `V`.
        self.v_banner = None
        # Kann der Stick einen Vorlauf fuer HmIP-Rahmen (`mb`)? None = noch
        # nicht gefragt bzw. keine deutbare Antwort; beides wird wie „nein"
        # behandelt, aber nur „nein" ist eine Auskunft.
        self.burst_faehig = None
        self.weckkanal = None          # gesetzter Weckkanal (q-culfw ab 2.0.92), sonst None
        self.budget = None
        self._cnt_ev = threading.Event()

        self.state_file = state_file
        self._load_state()

        qccu.on_set = self.on_set
        qccu.on_set_many = self.on_set_many
        qccu.on_put_master = self.konfig_schreiben
        qccu.kann_stellen = self.kann_stellen
        qccu.on_install = self.on_install

    # ⚠️ Der Rohmitschnitt waechst unbegrenzt — am Pruefstand rund 380 kB an
    # einem Tag, mit mehr Geraeten entsprechend mehr. Als Dauerbetrieb auf
    # einem fremden Rechner ist das eine Falle: er liegt in `/data`, und das
    # ist bei einer Erweiterung dieselbe Partition wie alles andere. Darum
    # zwei Dateien und ein Deckel; mehr Geschichte braucht niemand, der einen
    # Funkfehler sucht.
    RAW_MAX = 8 * 1024 * 1024
    RAW_PRUEF_JE = 500          # nicht bei jeder Zeile die Groesse messen

    def _log(self, direction, text):
        """Eine Zeile in den Rohmitschnitt."""
        if not self._raw:
            return
        t = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int(t % 1 * 1000)
        with self._log_lock:
            self._raw.write(f"{ts} {direction} {text}\n")
            self._raw_zeilen += 1
            if self._raw_zeilen % self.RAW_PRUEF_JE == 0:
                self._raw_umbrechen()

    def raw_dateien(self):
        """Die Mitschnitt-Dateien, aelteste zuerst — oder eine leere Liste.

        Fuer den Download in der Oberflaeche: als Erweiterung liegt der
        Mitschnitt in `/data` und ist von aussen NICHT erreichbar (das SSH-
        Add-on sieht den Datenspeicher fremder Erweiterungen nicht, und
        `docker exec` sperrt der Protection Mode). Ein Mitschnitt, den
        niemand holen kann, ist ein halbes Werkzeug.
        """
        if not self._raw:
            return []
        name = self._raw.name
        return [p for p in (name + ".1", name) if os.path.exists(p)]

    def _raw_umbrechen(self):
        """Ist der Mitschnitt voll, eine Runde weiterschieben.

        Aufrufer haelt `_log_lock`. Scheitert das Umbenennen, wird
        weitergeschrieben statt den Funk anzuhalten — ein voller Mitschnitt
        ist ein Aergernis, ein stehengebliebener Funk ein Ausfall.
        """
        if self._raw_umbruch_aus:
            return
        try:
            if self._raw.tell() < self.RAW_MAX:
                return
            name = self._raw.name
            self._raw.close()
            os.replace(name, name + ".1")
            self._raw = open(name, "a", buffering=1)
        except Exception as ex:                              # noqa: BLE001
            # ⚠️ EINMAL melden, dann Ruhe geben. Scheitert das Umbenennen
            # (volle Platte, schreibgeschuetztes Verzeichnis), scheitert es
            # beim naechsten Mal genauso — wer es bei jeder Pruefung neu
            # meldet, ertraenkt das Protokoll in derselben Zeile.
            if not self._raw_umbruch_aus:
                print(f"  ! Rohmitschnitt nicht umgebrochen ({ex}) — er "
                      f"waechst jetzt ungebremst weiter.")
            self._raw_umbruch_aus = True
            if self._raw is not None and self._raw.closed:
                try:
                    self._raw = open(name, "a", buffering=1)
                except Exception:                            # noqa: BLE001
                    self._raw = None

    def _load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
        except Exception as ex:
            defekt = self.state_file + ".defekt"
            try:
                os.replace(self.state_file, defekt)
            except Exception:
                defekt = "(nicht verschiebbar)"
            print(f"  ! ZAEHLERDATEI UNLESBAR ({ex}) — beiseite gelegt als {defekt}. "
                  f"Zaehler starten neu; der Stick haelt seinen eigenen Stand.")
            return
        if "appseq" in saved or "mac_seq" in saved:
            per_dev = saved.get("appseq", {})
            self.mac_seq = int(saved.get("mac_seq", 0))
        else:
            per_dev = saved
        for hmid, seq in per_dev.items():
            self.appseq[hmid] = (int(seq) + self.SEQ_STEP) & 0xFF
        wartend = saved.get("wartend") if isinstance(saved, dict) else None
        if isinstance(wartend, dict):
            for hmid, eintraege in wartend.items():
                gueltig = []
                for e in eintraege:
                    if not (isinstance(e, dict) and "cmd" in e and "appseq" in e):
                        continue
                    g = {"cmd": str(e["cmd"]), "appseq": int(e["appseq"]),
                         "versuche": int(e.get("versuche", 0))}
                    # Eine Konfigurationskette behaelt ihre Glieder-Eigenschaft
                    # und den Abschluss (Bestand uebernehmen) ueber den Neustart.
                    if e.get("kette"):
                        g["kette"] = True
                    if isinstance(e.get("danach"), dict):
                        g["danach"] = e["danach"]
                    gueltig.append(g)
                if gueltig:
                    self._wartend[str(hmid).lower()] = gueltig
            if self.verbose and any(self._wartend.values()):
                print("  Wartende Rahmen geladen: "
                      + ", ".join(f"{h}: {len(v)}" for h, v in self._wartend.items() if v))
        if self.verbose and per_dev:
            print(f"  Zaehlerstaende geladen: "
                  + ", ".join(f"{h}={self.appseq[h]:#04x}" for h in per_dev)
                  + (f", Stick sn=0x{self.mac_seq:08X}" if self.mac_seq else ""))

    def _save_state(self):
        if not self.state_file:
            return
        with self.lock:
            daten = {"appseq": dict(self.appseq), "mac_seq": self.mac_seq,
                     # Was auf ein Lebenszeichen wartet, ueberlebt einen
                     # Neustart: ein Ereignismelder, der seine Verknuepfung
                     # nie bekommt, meldet nie etwas (SMI55-A, 03.09.2026 —
                     # vier Rahmen beim Neustart verloren).
                     "wartend": {h: [dict({"cmd": e["cmd"], "appseq": e["appseq"],
                                           "versuche": e.get("versuche", 0)},
                                          **({"kette": True} if e.get("kette") else {}),
                                          **({"danach": e["danach"]} if isinstance(e.get("danach"), dict) else {}))
                                     for e in v]
                                 for h, v in self._wartend.items() if v}}
        with self._state_lock:
            try:
                tmp = self.state_file + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(daten, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.state_file)
            except Exception as ex:
                print(f"  ! ZAEHLERSTAND NICHT GESICHERT: {ex}")

    def bind(self, hmid, ccu_address):
        """Funkadresse und Zentralen-Adresse einander zuordnen.

        Ein Geraet hat genau EINE Funkadresse. Wird dieselbe Kennung neu
        angelernt (Werksreset, dann wieder Taste), bekommt sie eine neue
        Adresse — die alte Zuordnung muss weg, sonst gehen Befehle weiter an
        die tote Adresse (18.08.2026: ecd412 statt ecd413, „keine Quittung
        nach 3 Versuchen", obwohl das Geraet gerade sauber angelernt war).
        """
        with self.lock:
            ccu = ccu_address.upper()
            for alt in [h for h, a in self.by_hmid.items()
                        if a == ccu and h != hmid.lower()]:
                del self.by_hmid[alt]
                if self.verbose:
                    print(f"  Funk {alt} <-> {ccu} ersetzt")
            self.by_hmid[hmid.lower()] = ccu
        if hasattr(self.qccu, "note_rf"):
            self.qccu.note_rf(ccu_address, hmid)
        if self.verbose:
            print(f"  Funk {hmid.lower()} <-> {ccu_address.upper()}")

    def setup(self, own_addr):
        """Stick als Zentrale einrichten."""
        self.own_addr = own_addr.lower()
        # ⚠️ Es gibt KEINEN Weg mehr, dem Stick einen fertigen
        # Netzwerkschluessel von aussen zu geben (`mK<hex>` ist ab q-culfw
        # 2.0.16 verschwunden, QCCU kennt den Schalter gar nicht mehr). Ein
        # Stick, der jeden mitgebrachten Schluessel annimmt, waere ein
        # Werkzeug zum Mitlesen fremder Anlagen.
        #
        # Verloren geht dadurch nichts. Der Schluessel kommt auf genau zwei
        # Wegen herein, beide ohne Tastatur:
        #   * die Zentrale erzeugt ihn selbst (`mC` -> nwk_ensure), oder
        #   * sie bekommt ihn beim Anlernen zugeteilt — gegen SGTIN und
        #     Aufkleberschluessel verpackt, over-air. So uebernimmt man auch
        #     eine laufende Anlage: der Stick lernt sich als Geraet an ihrer
        #     alten Zentrale an, deren Schluessel wandert dabei mit.
        folge = ["mL0", "mC", f"mA{own_addr}", "mQ1", "mE1", "Pr"]
        for cmd in folge:
            self.ser.write(cmd.encode() + b"\r\n")
            self.ser.flush()
            self._log(">>", cmd)
            time.sleep(0.25)

        self._netzschluessel_sicherstellen()
        self._seq_angleichen()
        self._burst_probe()

        self.ser.reset_input_buffer()
        if self.verbose:
            print(f"  Stick eingerichtet (Adresse {own_addr})")

    def _stick_zustand(self):
        """Die Zustandszeile des Sticks holen (`m` -> `Pm ein …`).

        ⚠️ Der Stick antwortet auf `m` mit DREI Zeilen: Zustand, Sendekonto,
        Zaehlwerk. Wer nach der ersten aufhoert, laesst zwei liegen — und die
        naechste Frage geht unter, weil der Stick noch sendet. Genau daran
        scheiterte auf einem FABRIKNEUEN Stick die Frage nach der Kennung:
        sie blieb „unklar", also wurde keine erzeugt, also legte der Stick
        keinen Netzwerkschluessel an, also schlug spaeter jedes Anlernen fehl
        — und nirgends stand ein Fehler; die Einrichtung meldete „Stick
        eingerichtet". An echter Hardware gefunden (18.08.2026, q-culfw
        2.0.29, parallele mCCU-Session).

        Deshalb wird die Antwort zu Ende gelesen, bevor es weitergeht.
        """
        self.ser.reset_input_buffer()
        self.ser.write(b"m\r\n")
        self.ser.flush()
        gefunden = None
        ende = time.time() + 1.5
        while time.time() < ende:
            try:
                z = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:
                return gefunden
            if not z:
                if gefunden is not None:
                    break            # Ruhe auf der Leitung: die Antwort ist durch
                continue
            if z.startswith("Pm ein") or z.startswith("Pm aus"):
                gefunden = z
        return gefunden

    # Kommandos, die dieser Weg durchlaesst. ENG, und mit Absicht:
    #
    # ⚠️ Ein freier Durchgriff auf den Stick waere gefaehrlich — `mKX` verwirft
    # den Netzwerkschluessel, und danach ist JEDES angelernte Geraet ausgesperrt
    # und muss neu angelernt werden. Bei einem Heizungsregler kostet das die
    # Ventiladaption. Deshalb keine schwarze Liste (die vergisst man zu
    # pflegen), sondern eine weisse: nur die `mU`-Familie, also Vorlaufdauer
    # und Weckkanal. Die aendern nichts Bleibendes und sind genau das, was am
    # Pruefstand zwischen zwei Messreihen umgestellt werden muss.
    ROH_ERLAUBT = ("mU",)

    # Grenzen fuer `pruefstand_setzen`. Eng, damit ein Vertipper nicht den
    # Normalbetrieb verstellt: tx_timeout unter 20 ms waere kuerzer als die
    # MAC-Latenz des Geraets (~55 ms gemessen), ueber 5 s laenger als jede
    # sinnvolle Geduld; tx_tries hoeher als 5 verbrennt nur Sendezeit.
    PRUEFSTAND_GRENZEN = {"tx_timeout": (0.02, 5.0), "tx_tries": (1, 5)}

    def pruefstand_setzen(self, werte):
        """Sendeparameter fuer eine MESSREIHE stellen.

        ⚠️ Nur fuer den Pruefstand. `tx_timeout` ist der Abstand, nach dem eine
        unquittierte Sendung wiederholt wird — und damit, bei einem Burst, der
        Abstand zwischen Weckruf und dem Rahmen auf dem Empfangskanal. Der
        eq-3-Coprozessor schickt ihn nach 45-53 ms, QCCU bisher nach rund
        410 ms. Ob das den Unterschied macht, laesst sich nur messen, wenn man
        es stellen kann.

        Liefert (gesetzte_werte, fehler_oder_None).
        """
        if not isinstance(werte, dict):
            return {}, "Erwartet ein Objekt {name: wert}"
        gesetzt = {}
        for name, wert in werte.items():
            if name not in self.PRUEFSTAND_GRENZEN:
                return gesetzt, (f"unbekannt: {name} — stellbar sind "
                                 f"{', '.join(sorted(self.PRUEFSTAND_GRENZEN))}")
            lo, hi = self.PRUEFSTAND_GRENZEN[name]
            if isinstance(wert, bool) or not isinstance(wert, (int, float)):
                return gesetzt, f"{name}: Zahl erwartet, nicht {type(wert).__name__}"
            if not (lo <= wert <= hi):
                return gesetzt, f"{name}={wert} ausserhalb {lo}..{hi}"
            wert = int(wert) if name == "tx_tries" else float(wert)
            setattr(self, name, wert)
            gesetzt[name] = wert
            self._log("##", f"PRUEFSTAND {name}={wert}")
        return gesetzt, None

    def roh_kommando(self, cmd):
        """Ein Kommando aus der weissen Liste an den Stick geben.

        Fuer den Pruefstand: den Weckkanal (`mUK…`) oder die Vorlaufdauer
        (`mU1…`/`mU3…`) umstellen, OHNE QCCU anzuhalten. Den Anschluss zu
        oeffnen waere ein Reset des Sticks — und der setzt beides auf die
        Vorgabe zurueck, womit die Messreihe unbemerkt zweimal denselben Arm
        fuehre.

        Liefert None bei Erfolg, sonst den Grund als Text.
        """
        # ⚠️ Erst den Typ. `{"cmd": 5}` liess frueher `.strip()` mit einem
        # AttributeError durchschlagen — der Client bekam kein JSON, sondern
        # eine abgebrochene Verbindung.
        if cmd is not None and not isinstance(cmd, str):
            return f"cmd muss Text sein, nicht {type(cmd).__name__}"
        cmd = (cmd or "").strip()
        if not cmd:
            return "leeres Kommando"
        if any(c in cmd for c in "\r\n"):
            return "Zeilenumbruch im Kommando"
        if not cmd.startswith(self.ROH_ERLAUBT):
            return (f"nicht erlaubt: {cmd!r} — durchgelassen wird nur "
                    f"{'/'.join(self.ROH_ERLAUBT)}…")
        if self.ser is None:
            return "kein Anschluss"
        # ⚠️ NICHT dazwischenfunken, solange ein Sendeauftrag auf sein Urteil
        # wartet. Der Stick beantwortet ein fehlerhaftes `mU…` mit `Pm ERR`,
        # und diese Zeile passt auf TX_NO — `_handle` schriebe sie dem gerade
        # anhaengigen Job als „nicht gesendet" zu. Der wiederholte dann einen
        # Rahmen, der laengst hinausgegangen ist, und das echte `Pm tx ok`
        # fiele der Wiederholung zu. Ein Auftrag, ein Urteil: hier wird
        # gewartet, und wenn der Auftrag nicht fertig wird, abgewiesen.
        ende = time.time() + 3.0
        while time.time() < ende:
            with self._pending_lock:
                frei = self._pending is None
            if frei:
                break
            time.sleep(0.05)
        else:
            return ("gerade ist ein Sendeauftrag unterwegs — bitte gleich "
                    "noch einmal (ein Auftrag, ein Urteil)")
        try:
            with self._pending_lock:
                self.ser.write(cmd.encode() + b"\r\n")
                self.ser.flush()
        except Exception as ex:                       # noqa: BLE001
            return f"Schreiben scheiterte: {ex}"
        self._log(">>", f"{cmd}  [roh]")
        return None

    def _burst_probe(self):
        """Kann dieser Stick einen Vorlauf fuer HmIP-Rahmen? (`mU` fragt.)

        Ein Stick vor q-culfw 2.0.76 antwortet auf `mU` mit `Pm vorlauf=<0|1>`,
        ein neuerer haengt ` ms=<v1>/<v3>` an — die Vorlaufdauer je Stufe. Das
        Anhaengsel ist die Auskunft: nur wer sie gibt, kennt auch `mb`.

        ⚠️ Der Unterschied darf nicht stillschweigend bleiben. Ein alter Stick
        wuerde `mb3s…` mit `?` beantworten und NICHT senden; ohne diese Probe
        faende der Anwender einen Stellbefehl vor, der spurlos verschwindet,
        und suchte den Fehler beim Geraet. Mit ihr geht der Befehl wenigstens
        ohne Vorlauf hinaus — er wirkt bei einem schlafenden Geraet zwar
        nicht, aber der Grund steht im Protokoll.
        """
        self.ser.reset_input_buffer()
        self.ser.write(b"mU\r\n")
        self.ser.flush()
        ende = time.time() + 1.5
        while time.time() < ende:
            try:
                z = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:                            # noqa: BLE001
                break
            if not z:
                continue
            if z.startswith("Pm vorlauf="):
                self.burst_faehig = " ms=" in z
                self._log("##", f"BURST {'moeglich' if self.burst_faehig else 'NICHT moeglich'}"
                                f" — Stick meldet: {z}")
                if self.verbose and not self.burst_faehig:
                    print("  ! Der Stick kann keinen Vorlauf für HmIP-Rahmen "
                          "(q-culfw älter als 2.0.76). Ein Batteriegerät, das "
                          "nur zyklisch hört, ist damit nicht stellbar.")
                if "weckkanal=" in z:
                    self._weckkanal_setzen()
                return
        # Keine deutbare Antwort: NICHT raten. `None` heisst „ungefragt", und
        # `_submit` behandelt das wie „kann es nicht".
        self._log("##", "BURST unklar — der Stick antwortet nicht auf mU")

    # Der Weckkanal der Homematic-IP-Zentrale und der Abstand, nach dem der
    # Befehl auf dem Standardkanal folgt. Beides am eq-3-Coprozessor
    # mitgelesen (SPI: FREQ2/1/0 = 21 71 7A vor dem Burst = 869,52 MHz, danach
    # zurueck auf 868,30) und in der Herstellerdokumentation bestaetigt
    # („Burst auf 869 MHz, Standard auf 868 MHz"); der Abstand ist der
    # abgenommene Wert (7 von 10 an einem eTRV-E-S, 30 gegen 55 ms ohne
    # Unterschied).
    WECKKANAL_HMIP = "21717A"
    ZUSTELL_ABSTAND_MS = 30

    def _weckkanal_setzen(self):
        """Weckkanal und Zustellabstand auf dem Stick einstellen (q-culfw
        ab 2.0.92, erkennbar am `weckkanal=` in der `mU`-Antwort).

        ⚠️ Frisch geflasht steht der Stick auf `weckkanal=000000(aus)`: der
        Burst ginge dann auf 868,30 hinaus, und ein schlafendes Geraet hoert
        ihn nicht (gemessen 3 von 7 statt 7 von 10). Bis 2026.8.45 setzte das
        nur der Pruefstand — auf einer frisch aufgesetzten Zentrale war der
        Weckkanal damit nie an. Darum hier, bei jedem Anbinden.
        """
        for cmd in (f"mUK{self.WECKKANAL_HMIP}", f"mUZ{self.ZUSTELL_ABSTAND_MS}"):
            self.ser.write((cmd + "\r\n").encode())
            self.ser.flush()
            time.sleep(0.1)
        self.ser.write(b"mU\r\n")
        self.ser.flush()
        ende = time.time() + 1.0
        while time.time() < ende:
            try:
                z = self.ser.readline().decode("ascii", "replace").strip()
            except Exception:                            # noqa: BLE001
                break
            if z.startswith("Pm vorlauf="):
                self._log("##", f"WECKKANAL {self.WECKKANAL_HMIP} und Zustellabstand "
                                f"{self.ZUSTELL_ABSTAND_MS} ms gesetzt — Stick meldet: {z}")
                self.weckkanal = self.WECKKANAL_HMIP if f"weckkanal={self.WECKKANAL_HMIP}".lower() in z.lower() else None
                return
        self._log("##", f"WECKKANAL {self.WECKKANAL_HMIP} gesetzt — keine Rueckmeldung des Sticks")
        self.weckkanal = None

    def _kennung_vorhanden(self):
        """Hat der Stick schon eine Kennung? (`mG` zeigt sie.)

        Ein fabrikneuer Stick hat noch keine — und OHNE Kennung legt er auch
        keinen Netzwerkschluessel ab (`nwk_ensure` steigt bei `!id_valid`
        aus). Genau daran scheiterte das Anlernen auf einem neuen Stick,
        ohne dass es jemandem auffiel: `mC` antwortet auch dann freundlich
        mit `Pm rolle=Zentrale`, nur eben ohne `Netzwerkschluessel erzeugt`.

        Rueckgabe True/False, oder None wenn die Antwort nicht zu deuten ist
        — dann wird NICHT gewuerfelt.
        """
        # ⚠️ Zweimal fragen, und vorher aufraeumen. Die Frage folgt direkt auf
        # die Zustandsabfrage, deren Antwort mehrzeilig ist; deren Reste laufen
        # dieser hier in die Quere. Bleibt es bei „unklar", wird KEINE Kennung
        # erzeugt (so soll es sein) — und der Stick bekommt in der Folge auch
        # keinen Netzwerkschluessel. Einzeln gefragt antwortet er zuverlaessig,
        # im Ablauf nicht (am Geraet beobachtet, 18.08.2026).
        for _ in range(2):
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"mG\r\n")
                self.ser.flush()
            except Exception:                            # noqa: BLE001
                return None
            ende = time.time() + 2.0
            while time.time() < ende:
                try:
                    z = self.ser.readline().decode("ascii", "replace").strip()
                except Exception:                        # noqa: BLE001
                    return None
                if "keine Kennung" in z:
                    return False
                if z.startswith("Pm sgtin="):
                    return True
                # Alles andere sind Reste der vorigen Frage — weiterlesen.
            time.sleep(0.3)
        return None

    def _netzschluessel_sicherstellen(self):
        """Ohne Netzwerkschluessel im Stick laesst sich kein Geraet anlernen.

        Ein fabrikneuer Stick hat noch keinen. Aeltere Firmware (bis 2.0.10)
        meldete `key=0` auch dann, wenn einer im EEPROM lag — sie holte ihn
        beim Start nicht zurueck. In beiden Faellen erzeugt die Rolle Zentrale
        allein keinen: die Ablage gilt als belegt, solange ihr Merker steht.
        Erst das Verwerfen raeumt das Feld.

        Erzeugt wird nur, solange NICHTS zu verlieren ist. Sind Geraete
        eingetragen, waere ein neuer Schluessel das Ende jeder Verbindung zu
        ihnen — und zwar lautlos: sie wuerden weiter senden, nur verstuende sie
        niemand mehr. Diese Entscheidung gehoert dem Betreiber, nicht uns.
        """
        z = self._stick_zustand()
        if z is None:
            print("  ! Stick antwortet nicht auf die Zustandsfrage — "
                  "Netzwerkschluessel nicht pruefbar")
            return
        m = re.search(r"\bkey=(\d)", z)
        if not m or m.group(1) == "1":
            return

        angelernt = len(getattr(self.qccu, "devices", None) or {})
        if angelernt:
            print(f"  ! Der Stick hat keinen brauchbaren Netzwerkschluessel, "
                  f"es sind aber {angelernt} Geraete eingetragen.")
            print( "    Es wird KEINER erzeugt — das wuerde alle aussperren.")
            print( "    Entweder den bisherigen Stick zurueckstecken, oder die "
                   "Geraete loeschen und neu anlernen.")
            self.netzschluessel_fehlt = True
            return

        print("  Der Stick hat noch keinen Netzwerkschluessel — es wird einer "
              "erzeugt (es ist kein Geraet eingetragen).")

        # ⚠️ ERST die Kennung, DANN der Schluessel. Ohne Kennung legt der Stick
        # keinen ab, und zwar lautlos — `mC` quittiert trotzdem. Gewuerfelt
        # wird nur, wenn nachweislich KEINE da ist: `mGN` wirft eine
        # vorhandene weg, und die steht dann schon in der Zentrale des
        # Betreibers.
        if self._kennung_vorhanden() is False:
            print("  Der Stick hat noch keine Kennung — sie wird erzeugt.")
            self.ser.write(b"mGN\r\n")
            self.ser.flush()
            self._log(">>", "mGN")
            time.sleep(2.0)

        for cmd in ("mKX", "mC"):
            self.ser.write(cmd.encode() + b"\r\n")
            self.ser.flush()
            self._log(">>", cmd)
            time.sleep(1.0)

        z = self._stick_zustand()
        m = re.search(r"\bkey=(\d)", z or "")
        if m and m.group(1) == "1":
            print("  Netzwerkschluessel liegt im Stick.")
        else:
            self.netzschluessel_fehlt = True
            print("  ! Es konnte kein Netzwerkschluessel erzeugt werden — "
                  "Anlernen wird scheitern.")

    def netz_neu_beginnen(self):
        """Alle Geraete verwerfen und dem Stick einen Schluessel geben lassen.

        Der Ausweg aus der Lage „Stick ohne Netzwerkschluessel, aber Geraete
        eingetragen" — der zweite der beiden Wege, die die Oberflaeche nennt.
        Von selbst tut QCCU das NIE (siehe `_netzschluessel_sicherstellen`):
        ein neuer Schluessel sperrt jedes angelernte Geraet aus, und diese
        Entscheidung gehoert dem Betreiber.

        ⚠️ Ohne den alten Schluessel ist kein Funk-Ausschluss mehr moeglich —
        die Geraete hoeren uns nicht mehr. Sie brauchen daher jeweils einen
        WERKSRESET am Geraet, bevor sie neu angelernt werden koennen. Deshalb
        wird hier auch nicht `deleteDevices` benutzt: dessen Ausschlussversuch
        ginge dreimal ins Leere und kostete nur Zeit.

        Rueckgabe: (True, Meldung) oder (False, Grund).
        """
        weg = []
        q = self.qccu
        try:
            with q.lock:
                weg = list(getattr(q, "devices", {}) or {})
                for k in weg:
                    q.devices.pop(k, None)
                    if hasattr(q, "rf"):
                        q.rf.pop(k, None)
            for k in weg:
                self.by_hmid = {h: a for h, a in self.by_hmid.items() if a != k}
            if hasattr(q, "save_store"):
                q.save_store()
            for k in weg:
                if hasattr(q, "_enqueue"):
                    q._enqueue(("delete", k, [k]))
        except Exception as ex:                          # noqa: BLE001
            return False, f"Die Geraete konnten nicht entfernt werden: {ex}"

        print(f"  Netz neu begonnen: {len(weg)} Geraet(e) verworfen.")
        self.netzschluessel_fehlt = False
        try:
            self._netzschluessel_sicherstellen()
        except Exception as ex:                          # noqa: BLE001
            self.netzschluessel_fehlt = True
            return False, f"Der Schluessel konnte nicht erzeugt werden: {ex}"

        if self.netzschluessel_fehlt:
            return False, ("Der Stick hat weiterhin keinen Netzwerkschluessel. "
                           "Sitzt er richtig, und antwortet er auf `V`?")
        return True, (f"{len(weg)} Geraet(e) verworfen, der Stick hat einen "
                      f"neuen Netzwerkschluessel. Die Geraete brauchen jetzt "
                      f"je einen Werksreset, dann koennen sie neu angelernt "
                      f"werden.")

    SEQ_RESERVE = 2048

    def _seq_angleichen(self):
        """Zaehler des Sticks anheben, falls er hinter unserem Stand liegt."""
        if not self.mac_seq:
            return
        self.ser.write(b"m\r\n")
        self.ser.flush()
        time.sleep(0.4)
        stick = None
        try:
            for zeile in self.ser.read(4000).decode("ascii", "replace").splitlines():
                m = re.search(r"sn=([0-9A-Fa-f]{8})", zeile)
                if m:
                    stick = int(m.group(1), 16)
        except Exception:
            return
        if stick is None or stick >= self.mac_seq:
            return
        neu = self.mac_seq + self.SEQ_RESERVE
        self.ser.write(f"mS{neu:08X}".encode() + b"\r\n")
        self.ser.flush()
        time.sleep(0.25)
        print(f"  Stick-Zaehler stand auf 0x{stick:08X}, angehoben auf 0x{neu:08X} "
              f"(anderer Stick?)")
        self.mac_seq = neu

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()
        threading.Thread(target=self._pair_worker, daemon=True).start()
        threading.Thread(target=self._cmd_worker, daemon=True).start()

    def stop(self):
        self._stop = True
        if self._raw:
            with self._log_lock:
                self._raw.close()
            self._raw = None

    def firmware_version(self):
        """Fassung, wie der Stick sie selbst meldet."""
        m = self._ask("V", r"(?:q-culfw|hmip-mac-avr)\s+[0-9]+\.[0-9]+\.[0-9]+")
        if not m:
            return None
        # Die GANZE Zeile mitnehmen, nicht nur die Fassungsnummer: das ist die
        # Kennung, die ein CUL auf `V` ausgibt, und genau die will man in der
        # Oberflaeche sehen, wenn man wissen will, was da eigentlich haengt.
        self.v_banner = (m.string or "").strip() or m.group(0).strip()
        return m.group(0).strip()

    def release_for_flash(self):
        """Stick in den Bootlader schicken und den Port GANZ loslassen."""
        try:
            self._submit("B01", "ask")
        except Exception:
            pass
        time.sleep(0.4)
        self.stop()
        try:
            if getattr(self, "ser", None):
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # Wie viele Lesefehler in Folge geduldet werden, wenn der Fehler nicht
    # schon von sich aus eindeutig ist. Je Fehler wird eine halbe Sekunde
    # gewartet — nach fuenf Sekunden ohne einen einzigen brauchbaren Zugriff
    # ist der Anschluss nicht bloss kurz beschaeftigt.
    LESEFEHLER_GRENZE = 10

    @staticmethod
    def _zugang_hin(ex):
        """Sagt dieser Fehler schon fuer sich, dass der Anschluss weg ist?"""
        if isinstance(ex, serial.SerialException):
            return True
        if isinstance(ex, AttributeError):        # ser ist None (Einspielen)
            return True
        if isinstance(ex, OSError):
            # 5 Ein-/Ausgabefehler · 6 kein solches Geraet · 9 falscher
            # Deskriptor · 19 Geraet existiert nicht · 77 Deskriptor kaputt
            return getattr(ex, "errno", None) in (5, 6, 9, 19, 77)
        return False

    def _abmelden(self, grund):
        """Den Zugang aufgeben und den Anschluss loslassen.

        ⚠️ Nach einem Neustart des Sticks — Werksreset, Wachhund, Einspielen,
        kurz abgezogen — ist der geoeffnete Anschluss unbrauchbar, AUCH WENN
        das Geraet gleich darauf wieder da ist: der alte Deskriptor gehoert
        zu einer Verbindung, die es nicht mehr gibt, und der Pfad kann beim
        Wiederkommen sogar eine andere Nummer tragen. Wer hier weiterliest,
        bekommt bis in alle Ewigkeit denselben Fehler — und QCCU meldete
        derweil munter „Funk laeuft", waehrend nichts mehr ankam. (Am Aufbau
        nachgestellt: nach `mV` und nach dem Wachhund-Neustart.)

        Das Anbinden macht absichtlich NICHT dieser Faden, sondern der
        Waechter in qccu.py — dort liegt sie schon, samt Suche nach der
        gemerkten Seriennummer, Einrichtung und CUL-Zugang.
        """
        if self.tot:
            return
        self.tot = True
        self.tot_grund = str(grund)
        print(f"  ! Funkzugang verloren ({grund}) — der Stick wird neu gesucht.")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            merke("bad", "Funkzugang zum Stick verloren — er wird neu gesucht")
        try:
            self.stop()
        except Exception:                            # noqa: BLE001
            pass
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:                            # noqa: BLE001
            pass

    def _loop(self):
        while not self._stop:
            try:
                line = self.ser.readline().decode("ascii", "replace").strip()
                self._lesefehler = 0
            except Exception as ex:                  # noqa: BLE001
                self._lesefehler += 1
                if self._zugang_hin(ex) or self._lesefehler >= self.LESEFEHLER_GRENZE:
                    self._abmelden(f"Lesen: {ex}")
                    return
                time.sleep(0.5)
                continue
            if line:
                self._log("<<", line)
                try:
                    self._handle(line)
                except Exception as ex:
                    if self.verbose:
                        print(f"  ! Auswertung scheiterte: {ex}")

    def _ist_eigener_frame(self, line):
        """Gehoert die A-Zeile zum eigenen Netz (Ziel Rundruf oder Sammeladresse, Absender bekannt)?"""
        if len(line) < 21:
            return False
        src = line[9:15].lower()
        dst = line[15:21].lower()
        eigene = set(self.by_hmid)
        if self.own_addr:
            eigene.add(self.own_addr)
        if src in eigene or dst in eigene:
            return True
        return dst[:2] in ("f0", "e0") and src in eigene

    def _handle(self, line):
        if line[:1] == "A" and (self.cul is not None or self.bidcos is not None):
            if not self._ist_eigener_frame(line):
                if self.cul is not None:
                    try:
                        self.cul.a_zeile(line)
                    except Exception:
                        pass
                if self.bidcos is not None:
                    try:
                        self.bidcos.a_zeile(line)
                    except Exception:
                        pass
            return

        with self._pending_lock:
            job = self._pending
        if job is not None and job.expect is not None and not job.done.is_set():
            m = job.expect.search(line)
            if m:
                job.reply = m
                job.done.set()
                return

        if TX_OK.match(line) or TX_NO.match(line):
            with self._pending_lock:
                job = self._pending
            if job is not None:
                job.verdict = "ok" if TX_OK.match(line) else "err"
                job.done.set()
            return

        mk = KURZQUITTUNG.match(line)
        if mk:
            peer = mk.group(1).lower()
            ev = self._acked.get(peer)
            # Die Kurzquittung traegt keine Sequenznummer — sie gilt dem
            # Rahmen, den wir dem Geraet ZULETZT geschrieben haben. Wer auf
            # eine wartet, hinterlegt darum seinen Auftrag (`_acked_job`);
            # kam seither ein anderer hinaus, gehoert die Quittung nicht ihm.
            # ⚠️ NICHT an der Auftragsart festmachen: die Erreichbarkeitsprobe
            # wartet auf die Quittung ihrer eigenen ZEITAUSKUNFT, und eine
            # Pruefung auf „nur cmd zaehlt" erklaerte jedes Geraet fuer tot
            # (eingebaut und wieder entfernt am 03.09.2026).
            erwartet = self._acked_job.get(peer)
            if ev and erwartet is not None and self._zuletzt.get(peer) is not erwartet:
                anderer = self._zuletzt.get(peer)
                self._log("##", f"Kurzquittung von {peer} — gilt dem "
                                f"{anderer.kind if anderer else '?'}-Auftrag, nicht dem erwarteten")
                return
            if ev:
                self._log("##", f"Kurzquittung von {peer}")
                ev.set()
            return

        msq = STICKSEQ.match(line)
        if msq:
            v = int(msq.group(1), 16)
            if v > self.mac_seq:
                self.mac_seq = v
                self._save_state()
            return

        mb = BUDGET.match(line)
        if mb:
            self.budget = {"on": mb.group(1) == "1", "credit": int(mb.group(2)),
                           "max": int(mb.group(3)), "lovf": int(mb.group(4))}
            return

        mc = CNT.match(line)
        if mc:
            self.counters = dict(zip(CNT_KEYS, (int(x or 0) for x in mc.groups())))
            # Getrennt gelesen, damit ein alter Stick die Zaehler behaelt.
            mn = NOISE.search(line)
            if mn:
                boden, spitze = int(mn.group(1)), int(mn.group(2))
                self.funkgute = {
                    "noise": None if boden == KEINE_PROBE else boden,
                    "npk": None if spitze == KEINE_PROBE else spitze}
            mp = PLL.search(line)
            mrc = RECAL.search(line)
            if mp or mrc:
                g = self.funkgute or {}
                if mp:
                    g.update({"pll_lost": int(mp.group(1)),
                              "pll_relock": int(mp.group(2)),
                              "pll_fail": int(mp.group(3))})
                if mrc:
                    g["recal"] = int(mrc.group(1))
                self.funkgute = g
            self._cnt_ev.set()
            return

        mr = RAW.match(line)
        if mr:
            try:
                raw = bytes.fromhex(mr.group(1))
                air = raw[1:1 + raw[0]]
            except Exception:
                return
            if len(air) >= 49 and air[9] == 0x10:
                # ⚠️ Ein Geraet, das angelernt werden WILL — der Knopf wurde
                # gedrueckt. Das wird IMMER vermerkt, auch bei geschlossenem
                # Fenster: genau dann ist es die Auskunft, die dem Anwender
                # fehlt. Verarbeitet wird nur bei offenem Fenster.
                self._merke_anlernwunsch(air)
                if time.time() < self.pair_until:
                    self._pair_q.put(air)
            return

        m = DEC.match(line)
        if not m:
            return
        payhex, src, dst, sec, ct = m.groups()

        # Empfangspegel mitnehmen, bevor irgendein Zweig zurueckspringt: er
        # steht in JEDER Zeile — auch in Quittungen und Rundrufmeldungen, und
        # gerade die kommen regelmaessig, wenn sonst nichts passiert.
        self._rssi_merken(src.lower(), payhex)

        # ⚠️ Ein VERSCHLUESSELTER Frame, den der Stick entschluesseln konnte,
        # kommt aus unserem Netz: der Netzschluessel gilt je Anlage, und eine
        # PM-Zeile entsteht nur bei gelungener Entschluesselung (q-culfw
        # `emit()` nach `rx_ok++`; sonst zaehlt `rx_mic`). Steht sein Absender
        # nicht im Bestand, ist das eine Auskunft und kein Rauschen — deshalb
        # wird sie hier abgegriffen, bevor die Verarbeitung sie verwirft.
        if int(sec) >= 1:
            self._pruefe_verwaist(src.lower(), dst.lower())

        if int(ct) == 4 and ACK.search(line):
            ev = self._acked.get(src.lower())
            if ev:
                ms = ACKSEQ.search(line)
                self._log("##", f"Quittung von {src.lower()} fuer sn="
                                f"{ms.group(1) if ms else '?'}")
                ev.set()
            return

        # HUCKEPACK-Quittung: hat das Geraet auf unseren Frame gleich etwas
        # zu sagen (Statusmeldung auf einen Schaltbefehl), setzt es im Kopf
        # seines naechsten Frames das piggybackACK-Bit statt eine eigene
        # Kurzquittung zu senden — so macht es das auch bei der eq-3-Zentrale
        # (Luftmitschnitt 18.08.2026). Der Frame ist damit Quittung UND
        # Inhalt: vermerken und normal weiterverarbeiten.
        mf = FLAGS.search(line)
        if mf and int(mf.group(1), 16) & 0x40:
            ev = self._acked.get(src.lower())
            if ev:
                self._log("##", f"Huckepack-Quittung von {src.lower()}")
                ev.set()

        if (self._pair_expect and src.lower() == self._pair_expect
                and int(sec) >= 1):
            self._pair_ok.set()

        if int(ct) == CT_NETWORK_MGMT:
            # Die Bereitmeldung zum Ausschluss ist das Einzige, was uns hier
            # interessiert. Sie MUSS ueber ein eigenes Ereignis laufen: ein
            # wartender Auftrag hilft nicht, weil die laufenden Statusmeldungen
            # des Geraets laufend Antwort-Auftraege erzeugen und den
            # wartenden ueberschreiben.
            b = bytes.fromhex(payhex) if len(payhex) % 2 == 0 else b""
            if len(b) >= 2 and b[0] == 1 and b[1] == NM_EXCLUDE_READY:
                ev = self._exclude_ready.get(src.lower())
                self._log("<<", f"Ausschluss bestaetigt von {src.lower()}")
                if ev:
                    ev.set()
            return

        if int(ct) == CT_ICMP:
            # ⚠️ Ein UNVERSCHLUESSELTER Frame von einem fremden Absender ist
            # hier fast immer gar kein HmIP-Verkehr. Der Stick zeigt einen
            # Frame, der nur DEM FORMAT NACH HmIP ist, absichtlich BEIDEN
            # Familien (sonst saehe niemand einen BidCoS-Anlernruf, der an
            # 000000 geht und unverschluesselt ist) — er kommt also einmal als
            # `A`- und einmal als `P`-Zeile herein. Wer das nicht trennt,
            # deutet BidCoS-Verkehr als HmIP-Netzhaushalt; im Protokoll stand
            # dann „ICMP von <BidCoS-Adresse>", und der Anwender sucht einen
            # Fehler, den es nicht gibt.
            #
            # Verschluesselt und fremd ist etwas anderes — das ist ein
            # verwaistes Geraet und wird weiter oben abgegriffen.
            if int(sec) == 0:
                with self.lock:
                    bekannt = src.lower() in self.by_hmid
                if not bekannt:
                    if src.lower() not in self._fremd_gemeldet:
                        self._fremd_gemeldet.add(src.lower())
                        self._log("##", f"fremder unverschluesselter Verkehr von "
                                        f"{src.lower()} — nicht als HmIP gedeutet")
                    return
            ms = SEQ.search(line)
            # `a=1`: der Stick hat den Rundruf schon selbst quittiert
            # (q-culfw icmp_ack, Vorgabe an) — dann keine zweite Quittung
            # von hier; die Zentrale von eq-3 schickt genau eine.
            self._icmp(payhex, src.lower(), dst.lower(),
                       ms.group(1) if ms else None,
                       stick_acked=(" a=1" in line))
            return

        if int(ct) != 0 or int(sec) < 1:
            return

        with self.lock:
            addr = self.by_hmid.get(src.lower())
        if not addr and self._nachtrag.get(src.lower()):
            # ⚠️ DAS Lebenszeichen. Das Geraet funkt gesichert unter der
            # Adresse, die wir ihm angeboten haben — es HAT das Angebot also
            # angenommen, nur die Bestaetigung ist uns entgangen. Genau hier
            # heilt die Zentrale den Fall: `handleApplicationFrame` befoerdert
            # ein Geraet bei JEDEM gesicherten Anwendungsframe zu INCLUDED,
            # nicht nur bei der Bestaetigung. Wer stattdessen weiter „fremd"
            # sagt, treibt ein angelerntes Geraet in die Router-Suche, aus der
            # es ohne Werksreset nicht herauskommt (gemessen 17.08.2026).
            self._nachtrag_einloesen(src.lower())
            with self.lock:
                addr = self.by_hmid.get(src.lower())
        if not addr:
            # Fremder Absender ohne Anlernwunsch: das ist der Funkverkehr der
            # Nachbarschaft und geht uns nichts an. NICHT in den Posteingang —
            # dort gehoert nur hinein, was angelernt werden will (siehe
            # `_merke_anlernwunsch`), sonst fuellt er sich in einem
            # Mehrfamilienhaus mit fremden Geraeten, denen der Anwender
            # keinen Aufkleber zuordnen kann.
            return

        mf = FLAGS.search(line)
        for_us = bool(int(mf.group(1), 16) & RXF_FOR_US) if mf else True

        b = [int(payhex[i:i + 2], 16) for i in range(0, len(payhex), 2)]
        if len(b) < 4:
            return
        ln = b[0]
        if len(b) < 1 + ln + 2:
            return
        pt = b[1:1 + ln]
        if len(pt) < 2:
            return

        with self.lock:
            self.devseq[src.lower()] = pt[1]

        # Ein CONFIGURATION-Rahmen vom Geraet: der Untertyp steht im Byte
        # nach dem Anwendungskopf. Der Handler des Jars tut bei
        # REQUEST_CONFIG_UPDATE nichts Eigenes — die Zentrale quittiert und
        # schickt, was fuer das Geraet wartet; hier uebernimmt das
        # `_nachreichen` ueber das Lebenszeichen. Benannt wird er trotzdem,
        # damit im Protokoll steht, WAS das Geraet wollte.
        if (pt[0] & 0x3F) == FT_CONFIGURATION and len(pt) > 3:
            art = KONFIG_ANFRAGEN.get(pt[3], f"Untertyp 0x{pt[3]:02X}")
            self._log("<<", f"KONFIGURATION {art} von {src} appSeq=0x{pt[1]:02X}"
                            + (f" Daten={pt[4:].hex().upper()}" if len(pt) > 4 else ""))

        # Ein Tastendruck: der Sender schickt seinem Verknuepfungspartner —
        # uns, seit die Zentralenverknuepfung steht — einen Schaltbefehl.
        # Nutzlast nach dem Kopf: Byte 0 = LOW_BAT (0x80) | lang (0x40) |
        # Kanal (0x3F), Byte 1 = Tastenzaehler (`UnconditionalSwitchCommandFrame`).
        # Die Zentrale macht daraus (`handleCentralDeviceCommand`): kurz ->
        # PRESS_SHORT; lang: neuer Zaehler -> PRESS_LONG_START, jede
        # Wiederholung -> PRESS_LONG, der letzte Rahmen mit Antwortwunsch ->
        # PRESS_LONG_RELEASE. Am HmIP-WRC6-A gemessen (03.09.2026, 12:25):
        # kurz `88 09 01 02`, lang `08 0A..0F 41 03` im 250-ms-Takt, Ende
        # `88 0F 41 03` — dieselbe appSeq wie der letzte laufende Rahmen. Um
        # 12:59 dagegen: lang `08 11..16 41 05`, Ende `88 17 41 05` mit NEUER
        # appSeq. Beides kommt vor; die Deutung haengt nur am Antwortwunsch.
        # Live gedeutet 12:59: PRESS_SHORT, dann START + 7x LONG + RELEASE,
        # 32 ms nach dem Rahmen.
        if (pt[0] & 0x3F) in (FT_SWITCH_UNCOND, FT_SWITCH_COND, FT_LEVEL_CMD) and len(pt) > 3:
            self._tastendruck(src.lower(), pt)

        # Die ANSWER des Geraets — die Auskunft, ob es einen Frame ANGENOMMEN
        # hat. Zugeordnet ueber die appSeq, wie im Jar; Nutzlast 0 heisst ACK,
        # alles andere ist eine Ablehnung.
        if (pt[0] & 0x3F) == FT_ANSWER:
            self._app_quittung(src.lower(), pt[1],
                               pt[2] if len(pt) > 2 else 0)

        # ⚠️ Ein Geraet quittiert NICHT immer mit ANSWER. Es kann die Quittung
        # auch seinem naechsten STATUS aufsatteln — dann traegt dessen
        # `payload[0]` das Bit `piggybackAppACK` (0x20) und `pt[1]` das Echo
        # UNSERER appSeq. Die Zentrale wertet beides gleichwertig aus:
        #
        #   } else if (frame.getFrameType() == HMIP_APP_ANSWER
        #           || frame.getFrameType() == HMIP_APP_STATUS
        #              && ((StatusFrame)frame).isPiggybackAppACK()) {
        #       deviceCommandsHolder.checkAndRemoveCommand(
        #               header.getApplicationSequencenumber());
        #   }
        #                       (`HMIPApplicationHandler`, Zeile 203-205)
        #
        # ⚠️ GEMESSEN, dass das der Regelfall ist und nicht die Ausnahme: an
        # einem HmIP-eTRV-C (Hoerertyp 11) antwortete das Geraet auf sechs von
        # sechs Burst-Stellbefehlen mit einem STATUS-Rahmen im Antwortfenster
        # — mit KEINEM ANSWER (01.09.2026, Nachbarsitzung an einem
        # eq-3-Dual-Copro). Wer nur auf ANSWER hoert, sieht bei einem
        # Batteriethermostat also gar keine Quittung und meldet „keine
        # ANSWER", obwohl der Befehl angekommen und angenommen ist.
        #
        # Ein Huckepack-ACK traegt keinen Ablehnungsgrund — es ist ein
        # blankes Ja. Darum `ergebnis=0` (ACK), wie es `checkAndRemoveCommand`
        # ohne jede Deutung tut.
        elif ((pt[0] & 0x3F) == FT_STATUS and len(pt) > 2
                and (pt[2] & 0x20)):
            self._log("<<", f"HUCKEPACK-QUITTUNG von {src} "
                            f"appSeq=0x{pt[1]:02X}")
            self._app_quittung(src.lower(), pt[1], 0)

        # ⚠️ Die Zeitanfrage wird beantwortet, AUCH OHNE respReq-Bit. Sie ist
        # die einzige Ausnahme, und sie ist gemessen: der HmIP-BWTH-A fragt
        # mit `0x23` (TIME_INFO ohne Antwortwunsch) und WIEDERHOLT die Frage
        # mit verdoppelndem Abstand — 24 s, 48 s, 96 s (30.08.2026,
        # `luft_exp3.log`, appSeq 01/07/08/0B, danach 192 s). Ein Turnus sieht
        # anders aus; das ist ein Wiederholungsverhalten, also wartet das
        # Geraet auf eine Antwort, ohne sie formal anzufordern. Nach der
        # Antwort blieb die Wiederholung aus (`luft_exp4.log`), und am Display
        # stand die Antenne fest.
        #
        # ⚠️ NICHT weiter deuten: in einem Lauf OHNE Zeitantwort hat dasselbe
        # Geraet einen CREATE_LINK trotzdem quittiert (`luft_exp2.log`). Ein
        # Zusammenhang zwischen Zeitantwort und Annahme der Konfiguration ist
        # damit NICHT belegt, auch wenn er naheliegt.
        #
        # Der Inhalt der Frage ist `timeInfoType=1` — laut Jar
        # (`TimeInfoFrame.setPayload`, `TimeMasterRequestData`) die Bitte um
        # einen Zeitgeber, mitsamt der gewuenschten Zeitzonenregel; die
        # Nutzlast `04 5A 00 0C | 08 53 00 08` ist in Viertelstunden genau die
        # europaeische: UTC+1 ab letztem Sonntag im Oktober, UTC+2 ab letztem
        # Sonntag im Maerz. Geantwortet wird mit der UHR (`timeInfoType=0`) —
        # so hat es die echte Zentrale im Referenzmitschnitt getan.
        if for_us and (pt[0] & 0x3F) == FT_TIME_INFO:
            self._time_info(src.lower(), pt[1])
        elif for_us and (pt[0] & APP_RESP_REQ) and (pt[0] & 0x3F) != FT_ANSWER:
            # Alles Uebrige nur auf ausdruecklichen Wunsch: eine Quittung, die
            # niemand angefordert hat, kostet nur Sendezeit.
            self._answer(src.lower(), pt[1])

        # Das Geraet ist WACH — jetzt geht hinaus, was auf es gewartet hat.
        # Die Reihenfolge ist die der Zentrale: erst die Quittung mit dem
        # Wach-Bit, dann der Befehl. Die Quittung liegt `answer_delay` in der
        # Zukunft und der Befehlsweg hat auf der Leitung Vorrang — der
        # Nachreich-Faden wartet sie deshalb ausdruecklich ab
        # (`_antwort_abwarten`), sonst ueberholt der Befehl die Quittung.
        if self._wartend.get(src.lower()):
            self._nachreichen(src.lower())

        if len(pt) < 4 or (pt[0] & 0x3F) != FT_STATUS:
            return

        flags = pt[2]
        # ⚠️ VOR der Formatpruefung. Die Zustandsbits liegen in payload[0] und
        # gelten fuer JEDES Inhaltsformat — auch fuer 3
        # (`parseDeviceSpecificPayload`), dessen Eintraege QCCU nicht zerlegt.
        # Gerade dort sind sie das Einzige, was aus dem Frame zu holen ist.
        self._statusflaggen(addr, flags)
        fmt = flags & 0x03
        if fmt > 2:
            return

        i = 4
        shared_ch = shared_type = None
        if fmt == 1:
            shared_ch = pt[i]; i += 1
        if fmt == 2:
            shared_type = pt[i]; i += 1

        # ⚠️ Erst ZERLEGEN, dann deuten. Zwei Regeln von eq-3 lassen sich am
        # einzelnen Eintrag nicht entscheiden:
        #   * `containsStatusType(statusFrame, PERIOD)` sieht auf die ganze
        #     Meldung (Solltemperatur oder Party-Temperatur),
        #   * `statusDataTypeOccurence` zaehlt, das wievielte Mal derselbe
        #     Statusdatentyp im selben Kanal kommt (zweites LEVEL = Saettigung).
        eintraege = []
        while i < len(pt):
            if fmt == 0:
                if i + 1 >= len(pt):
                    break
                typ, ch = pt[i], pt[i + 1]; i += 2
            elif fmt == 1:
                typ, ch = pt[i], shared_ch; i += 1
            else:
                typ, ch = shared_type, pt[i]; i += 1
            if typ is None or ch is None:
                break
            vl = self.vlen.get(typ)
            if not vl:
                if typ not in self._sdt_unbekannt:
                    self._sdt_unbekannt.add(typ)
                    print(f"  ! Statustyp {typ} ohne bekannte Laenge — der Rest "
                          f"dieser Meldung wird verworfen, nicht geraten.")
                break
            if i + vl > len(pt):
                break
            eintraege.append((typ, ch, pt[i:i + vl]))
            i += vl

        typen = {typ for typ, _ch, _val in eintraege}
        # Zaehlung wie im Jar: der Wechsel des KANALS setzt sie zurueck, ein
        # anderer Statusdatentyp ebenso.
        letzter_ch = letzter_typ = None
        vorkommen = 0
        for typ, ch, val in eintraege:
            if ch != letzter_ch:
                letzter_typ = None
                letzter_ch = ch
            if typ == letzter_typ:
                vorkommen += 1
            else:
                letzter_typ = typ
                vorkommen = 0
            self._emit(addr, ch, typ, val, typen, vorkommen)

    def _statusflaggen(self, addr, flags):
        """Die Zustandsbits eines STATUS-Frames auf den Wartungskanal legen.

        `CONFIG_PENDING`, `LOW_BAT` und `DUTY_CYCLE` fuehrt MAINTENANCE/VALUES
        ohnehin — und aiohomematic zaehlt `CONFIG_PENDING` zu den
        `RELEVANT_INIT_PARAMETERS`. Bisher warf QCCU die drei Bits weg:
        `flags` ging nur als Formatangabe in die Schleife.

        ⚠️ MIT UNTERSTRICH. `LOWBAT`/`DUTYCYCLE` ohne ist die BidCoS-Schreibung
        aus den rftypes; in den HmIP-Paramsets gibt es sie nicht (alle 16
        MAINTENANCE-Fassungen nachgesehen: `DUTY_CYCLE` in 9, `LOW_BAT` in 5,
        die Formen ohne Unterstrich in keiner). Mit dem falschen Namen filtert
        `_kanal_fuehrt` still weg, und es sieht aus wie „das Geraet meldet es
        nicht". aiohomematic fuehrt beide Schreibweisen als VERSCHIEDENE
        Parameter — der Name muss stimmen, nicht ungefaehr stimmen.

        Ein Wechsel von `CONFIG_PENDING` wird zusaetzlich protokolliert. Das
        ist der einzige Weg, an dem sich von aussen ablesen laesst, ob ein
        frisch angelerntes Geraet noch auf seine Konfiguration wartet.
        """
        for bit, param in ((SF_CONFIG_PENDING, "CONFIG_PENDING"),
                           (SF_LOWBAT, "LOW_BAT"),
                           (SF_DUTYCYCLE, "DUTY_CYCLE")):
            wert = bool(flags & bit)
            if not self._kanal_fuehrt(addr, 0, param):
                continue
            if param == "CONFIG_PENDING":
                vorher = self._config_pending.get(addr)
                if vorher != wert:
                    self._config_pending[addr] = wert
                    self._log("<<", f"{addr} CONFIG_PENDING={wert}")
                    if self.verbose:
                        print(f"  <- {addr}:0 CONFIG_PENDING={wert}")
            self.qccu.set_value_internal(addr, 0, param, wert)

    def _paramset(self, d, channel):
        """Das VALUES-Paramset eines Kanals — gemerkt, nicht neu gebaut.

        ⚠️ Gefragt wird das GERAET, nicht der Typ: welche Beschreibung gilt,
        haengt an seiner Firmwarefassung. Zwei Geraete desselben Typs mit
        verschiedenen Fassungen koennen verschiedene Kanalfassungen haben —
        deshalb steht die Adresse im Schluessel.
        """
        schluessel = (d.address, int(channel))
        eintrag = self._pset_cache.get(schluessel)
        if eintrag is None:
            holen = getattr(d, "paramset", None)
            eintrag = (holen(channel, "VALUES") if holen
                       else self.t.paramset_of(d.devtype, channel, "VALUES")) or {}
            self._pset_cache[schluessel] = eintrag
        return eintrag

    def _kanal_fuehrt(self, addr, channel, param):
        """Fuehrt dieser Kanal diesen Parameter ueberhaupt?

        Die Zuordnung Statusdatentyp -> Parameter gilt nur, wo der Kanaltyp den
        Parameter auch kennt. Sonst wuerde ein Wert unter einem Namen landen,
        den der Klient nicht kennt — und ein unbekannter Datenpunkt kippt
        drueben schnell das ganze Geraet.
        """
        # Reiner Lesezugriff: die Kanalliste eines Geraets ist unveraenderlich,
        # solange es eingetragen ist — dafuer braucht es kein Schloss.
        d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
        if not d or not hasattr(d, "channel_list"):
            return False
        if not dict(d.channel_list()).get(int(channel)):
            return False
        return param in self._paramset(d, channel)

    def _kanaltyp(self, addr, channel):
        """Kanaltyp eines Kanals — oder None, wenn das Geraet ihn nicht hat."""
        d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
        if not d or not hasattr(d, "channel_list"):
            return None
        return dict(d.channel_list()).get(int(channel))

    def _variante(self, addr, channel, param):
        """Welche Fassung eines Parameters DIESES Geraet fuehrt.

        Die Geraetebeschreibung nennt sie als `NAME@fassung` unter den
        geraeteeigenen Parametern des Kanals; nennt sie den Parameter nicht
        eigens, gilt die Fassung des Kanaltyps — `default`.
        """
        d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
        if not d:
            return "default"
        tabellen = getattr(d, "tables", None) or self.t
        holen = getattr(tabellen, "chinfo_of", None)
        if not holen:
            return "default"
        try:
            info = holen(d.devtype, channel, getattr(d, "eintrag", None)) or {}
        except Exception:                                # noqa: BLE001
            return "default"
        for schluessel in (info.get("extra") or {}).get("VALUES", []):
            name, _, fassung = str(schluessel).partition("@")
            if name == param:
                return fassung or "default"
        # Zweite Quelle: die Kanaltyp-XML (`subtype`), im Paramset als FASSUNG.
        desc = self._beschreibung(addr, channel, param)
        if isinstance(desc, dict) and desc.get("FASSUNG"):
            return str(desc["FASSUNG"])
        return "default"

    def _tastendruck(self, hmid, pt):
        """Einen Schaltbefehl des Senders als Tastenereignis melden."""
        addr = self.by_hmid.get(hmid)
        if not addr:
            return
        lang = bool(pt[2] & 0x40)
        kanal = pt[2] & 0x3F
        zaehler = pt[3]
        resp = bool(pt[0] & 0x80)
        art = {FT_SWITCH_UNCOND: "Schaltbefehl", FT_SWITCH_COND: "bedingter Schaltbefehl",
               FT_LEVEL_CMD: "Pegelbefehl"}[pt[0] & 0x3F]
        schluessel = (hmid, kanal)
        # Eine Wiederholung desselben Rahmens ist kein zweiter Druck. Der Jar
        # erkennt sie an appSeq, Rahmenart und Antwortwunsch
        # (`FrameHistoryEntry.isDuplicate`) — NICHT am Tastenzaehler: der bleibt
        # waehrend eines langen Drucks ueber alle Rahmen gleich, und der
        # Schlussrahmen traegt sogar dieselbe appSeq wie der letzte laufende,
        # nur mit Antwortwunsch.
        marke = (pt[1], pt[0] & 0x3F, resp)
        if self._letzter_druck.get(schluessel) == marke:
            self._log("<<", f"TASTE {addr}:{kanal} {art} wiederholt (appSeq 0x{pt[1]:02X}) — kein neues Ereignis")
            return
        self._letzter_druck[schluessel] = marke
        if pt[2] & 0x80:
            self.qccu.set_value_internal(addr, 0, "LOW_BAT", True)
        ereignisse = []
        if not lang:
            ereignisse.append("PRESS_SHORT")
        else:
            letzter = self._tastenzaehler.get(schluessel)
            if letzter != zaehler:
                self._tastenzaehler[schluessel] = zaehler
                ereignisse.append("PRESS_LONG_START")
            ereignisse.append("PRESS_LONG")
            if resp:
                ereignisse.append("PRESS_LONG_RELEASE")
                self._tastenzaehler.pop(schluessel, None)
        gemeldet = []
        for name in ereignisse:
            if self._beschreibung(addr, kanal, name) is None:
                continue
            # Ereignis, kein Wert — wie die Zentrale (`event_internal`).
            self.qccu.event_internal(addr, kanal, name, True)
            gemeldet.append(name)
        self._log("<<", f"TASTE {addr}:{kanal} {art} {'lang' if lang else 'kurz'} "
                        f"Zaehler {zaehler}{' Ende' if lang and resp else ''} -> "
                        + (", ".join(gemeldet) if gemeldet else "kein Tastenkanal, nicht gemeldet"))
        if self.verbose and gemeldet:
            print(f"  <- {addr}:{kanal} {' '.join(gemeldet)}")

    def _form_unbekannt(self, ccu_address, channel, param, fassung):
        """LAUT ablehnen: Form ohne belegten Stellbefehl — nichts senden.

        Leise auf `default` zurueckzufallen hiesse, einem Rollladen den
        Rahmen eines Schaltaktors zu schicken: wohlgeformt, falsch, und es
        faellt nie auf. Darum steht es im Protokoll UND in der Oberflaeche.
        """
        text = (f"{param} auf {ccu_address}:{channel} hat die Form "
                f"'{fassung}', fuer die QCCU keinen belegten Stellbefehl "
                f"kennt — nichts gesendet")
        self._log("##", f"ABGELEHNT {text}")
        if self.verbose:
            print(f"  ! {text}")
        merken = getattr(self.qccu, "merke_ereignis", None)
        if merken:
            try:
                merken("bad", text)
            except Exception:                                # noqa: BLE001
                pass

    def _beschreibung(self, addr, channel, param):
        """Die Beschreibung eines Parameters am Kanal — oder None.

        Dasselbe wie `_kanal_fuehrt`, nur dass der Eintrag selbst zurueckkommt:
        `TYPE` und `VALUE_LIST` entscheiden ueber die FORM des Wertes.
        """
        d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
        if not d or not hasattr(d, "channel_list"):
            return None
        if not dict(d.channel_list()).get(int(channel)):
            return None
        desc = self._paramset(d, channel).get(param)
        return desc if isinstance(desc, dict) else None

    def _form(self, desc, zahl, param=None):
        """Physikalische Zahl in die Form bringen, die der Kanal ankuendigt.

        Im Jar entscheidet darueber `LogicalType` des Parameters, hier `TYPE`
        aus derselben Gerätebeschreibung:

          BOOL    -> `BoolToInteger`: „Rohwert ungleich 0“ (Standardmaske 0xFF)
          ENUM    -> `StringEnumToInteger`: der Eintrag der Werteliste, also
                     eine ZEICHENKETTE — nicht der Zahlenwert. Dazu passt, dass
                     unsere Paramsets bei ENUM auch MIN/MAX/DEFAULT als
                     Zeichenketten fuehren.
          INTEGER -> ganze Zahl (`IntegerToInteger` rundet)
          FLOAT   -> Gleitkomma

        Passt der Wert nicht in die Werteliste, wird NICHTS gemeldet: eine
        Zahl unter einem Namen, den der Klient als ENUM fuehrt, kippt drueben
        den ganzen Datenpunkt.
        """
        typ = (desc or {}).get("TYPE")
        if typ == "BOOL":
            return bool(zahl)
        if typ == "ENUM":
            liste = (desc or {}).get("VALUE_LIST") or ()
            z = int(round(zahl))
            werte = ENUM_WERTE.get((param, tuple(liste))) if param else None
            if werte is not None:
                # Fester Wert je Eintrag, genau getroffen oder nichts — so
                # haelt es `StringEnumToInteger.convertPhysicalToLogical`.
                return liste[werte.index(z)] if z in werte else None
            return liste[z] if 0 <= z < len(liste) else None
        if typ == "INTEGER":
            return int(round(zahl))
        if typ == "FLOAT":
            return float(zahl)
        return zahl

    def _emit(self, addr, channel, sdt, value, typen=(), vorkommen=0):
        """Einen Statuseintrag melden. Gedeutet wird nur, was belegt ist."""
        stufe = SDT_BELEGSTUFE.get(sdt, ("kein-zeuge", ""))[0]
        regeln = SDT_REGELN.get(sdt) if stufe in DEUTEN_AB else None
        value = bytes(value)
        gemeldet = False
        for regel in regeln or ():
            if regel.vorkommen is not None and regel.vorkommen != vorkommen:
                continue
            if regel.kanaltypen or regel.ausser:
                kt = self._kanaltyp(addr, channel)
                if regel.kanaltypen and kt not in regel.kanaltypen:
                    continue
                if regel.ausser and kt in regel.ausser:
                    continue
            if regel.braucht:
                name, soll = regel.braucht
                if self._kanal_fuehrt(addr, channel, name) is not soll:
                    continue
            if regel.wenn and not regel.wenn(typen, value):
                continue
            desc = self._beschreibung(addr, channel, regel.param)
            if desc is None:
                continue

            if regel.bits is not None:
                fassung = self._variante(addr, channel, regel.param)
                bit = regel.bits.get(fassung)
                if bit is None:
                    if self.verbose:
                        print(f"  <- {addr}:{channel} {regel.param}: Fassung "
                              f"{fassung!r} ohne bekannte Bitlage, nicht gedeutet")
                    continue
                roh = 1 if value[0] & (1 << bit) else 0
            else:
                roh = regel.bytes_fn(value)
            if roh is None:
                continue

            status = None
            if isinstance(roh, Sonder):
                status, roh = roh.status, roh.ersatz
                # Der Nebenparameter sagt, WARUM kein Messwert kommt. Er heisst
                # ueberall `<NAME>_STATUS` — fuehrt der Kanal ihn nicht, faellt
                # die Auskunft weg, der Messwert aber trotzdem nicht falsch aus.
                if regel.status and self._kanal_fuehrt(
                        addr, channel, regel.param + "_STATUS"):
                    self.qccu.set_value_internal(
                        addr, channel, regel.param + "_STATUS", status)
            elif regel.status and self._kanal_fuehrt(
                    addr, channel, regel.param + "_STATUS"):
                self.qccu.set_value_internal(
                    addr, channel, regel.param + "_STATUS", "NORMAL")

            if roh is None:
                # UNKNOWN/ERROR: den Wert loeschen, nicht stehen lassen.
                self.qccu.set_value_internal(addr, channel, regel.param, None)
                gemeldet = True
                if self.verbose:
                    print(f"  <- {addr}:{channel} {regel.param}=— ({status})")
                continue

            if isinstance(roh, str):
                # Ein Zeitpunkt (PERIOD) ist schon der logische Wert — das
                # Jar liefert ihn als Zeichenkette, ohne Zahl dazwischen.
                wert = roh
            else:
                faktor, versatz = umrechnung(
                    regel.param, self._variante(addr, channel, regel.param)
                    if f"{regel.param}@" in "".join(k + " " for k in UMRECHNUNG) else "default")
                wert = self._form(desc, (roh - versatz) / faktor, regel.param)
            if wert is None:
                continue
            self.qccu.set_value_internal(addr, channel, regel.param, wert)
            gemeldet = True
            if self.verbose:
                einheit = desc.get("UNIT") or ""
                zusatz = f" ({status})" if status else ""
                print(f"  <- {addr}:{channel} {regel.param}={wert}"
                      f"{(' ' + einheit) if einheit else ''}{zusatz}")
        if gemeldet:
            # Ein Rohwert aus einer Zeit ohne Regel darf nicht neben dem
            # gedeuteten Wert stehenbleiben (HmIP-SCI, 03.09.2026: RAW_SDT2
            # und STATE nebeneinander) — der Klient fuehrt sonst zwei
            # Datenpunkte fuer dieselbe Sache.
            d = (getattr(self.qccu, "devices", None) or {}).get(addr.upper())
            werte = getattr(d, "values", None)
            if isinstance(werte, dict) and (int(channel), f"RAW_SDT{sdt}") in werte:
                schloss = getattr(self.qccu, "lock", None)
                if schloss is not None:
                    with schloss:
                        werte.pop((int(channel), f"RAW_SDT{sdt}"), None)
                else:
                    werte.pop((int(channel), f"RAW_SDT{sdt}"), None)
                self._log("##", f"RAW_SDT{sdt} an {addr}:{channel} geraeumt — jetzt gedeutet")
            return

        # Nicht gedeutet — dann wenigstens beim Namen nennen, unter dem eq-3
        # den Wert fuehrt. Der Wert selbst bleibt roh: eine Skalierung, die
        # wir nicht an einem Geraet gemessen haben, waere geraten (und in
        # Volt oder Prozent faellt so etwas erst spaet auf).
        raw = "".join(f"{x:02X}" for x in value)
        self.qccu.set_value_internal(addr, channel, f"RAW_SDT{sdt}", raw)
        if self.verbose:
            name = None
            try:
                name = self.t.sdt_name(sdt)
            except Exception:                            # noqa: BLE001
                pass
            wie = f"SDT{sdt} ({name})" if name else f"SDT{sdt}"
            print(f"  <- {addr}:{channel} {wie}={raw} (ungedeutet, {stufe})")

    def _sender(self):
        """Einziger Schreiber auf der Leitung."""
        while not self._stop:
            job = None
            try:
                job = self._txq.get_nowait()
            except queue.Empty:
                pass
            if job is None and self._gate.is_set():
                if self._ans_hold is None:
                    try:
                        self._ans_hold = self._ansq.get_nowait()
                    except queue.Empty:
                        pass
                if self._ans_hold is not None and self._ans_hold.not_before <= time.time():
                    job = self._ans_hold
                    self._ans_hold = None
            if job is None:
                time.sleep(0.01)
                continue

            with self._pending_lock:
                self._pending = job
            try:
                # `_submit` schreibt fuer den Vorlauf `ms…`/`mT…` zu
                # `mb<stufe>s…`/`mb<stufe>T…` um — die Adresse rueckt dabei
                # um ein Zeichen weiter. Beide Formen einzeln behandeln, sonst
                # landet bei `mb3T11<hmid>` der Laengenteil in der Adresse.
                c = job.cmd
                if c.startswith("mb"):
                    c = c[3:]                        # Stufe weg: wieder ms…/mT…
                ziel = (c[2:8] if c.startswith("ms") else
                        c[4:10] if c.startswith("mT") else "")
                if len(ziel) == 6:
                    self._zuletzt[ziel.lower()] = job
            except Exception:                    # noqa: BLE001
                pass
            try:
                self.ser.write(job.cmd.encode() + b"\r\n")
                self.ser.flush()
                self._log(">>", f"{job.cmd}  [{job.kind}]")
            except Exception as ex:                  # noqa: BLE001
                self._log("##", f"Schreiben scheiterte: {ex}")
                job.verdict = "err"
                # Ein geschlossener oder verschwundener Anschluss meldet sich
                # auch hier — und zwar frueher als beim Lesen, wenn gerade
                # niemand sendet.
                if self._zugang_hin(ex):
                    job.written.set()
                    job.done.set()
                    self._abmelden(f"Schreiben: {ex}")
                    return
            job.written.set()

            if job.expect is not None:
                job.done.wait(self.ask_timeout)
            elif job.expects_verdict and job.verdict is None:
                # ⚠️ Ein Vorlauf haelt den Stick fest, bevor er ueberhaupt
                # sendet — gemessen 385,5 ms Huellkurve gegen 29,5 ms ohne.
                # Das Urteil kann also nicht frueher kommen. Der Zuschlag muss
                # an BEIDEN Wartestellen stehen (hier und in `_do_set`), sonst
                # kippt die Ordnung, mit der `_do_set` heute sicher nach dem
                # Sender aufwacht.
                job.done.wait(self.verdict_timeout + burst_zuschlag(job.burst))
            with self._pending_lock:
                self._pending = None
            job.done.set()

    def _burst_stufe(self, cmd):
        """Welche Burst-Stufe dieser Stick-Befehl braucht — 0, 1 oder 3.

        Die Entscheidung haengt am ZIEL, nicht am Rahmen: die echte Zentrale
        baut jedes `SendFrameCommand` mit `device.getListenerMode()`
        (`TransactionTaskFactory:269-330`). Also Zieladresse aus dem Befehl
        lesen, Geraet suchen, Hoerertyp fragen.

        ⚠️ Diese Methode laeuft im LESEFADEN (`_handle` -> `_answer`) und darf
        deshalb unter keinen Umstaenden werfen. Der Pruefstand reicht seit dem
        14.08. ausdruecklich einen zerhackten Befehl ein (`_submit("mTdefekt",
        "cmd")`, am echten Stick bei gleichzeitigem Empfang beobachtet) — ein
        `int(…, 16)` darauf waere eine Ausnahme mitten im Empfang. Darum reine
        Zeichenkettenpruefung und ein Fangnetz um alles.
        """
        try:
            if cmd.startswith("ms"):
                ziel = cmd[2:8]
            elif cmd.startswith("mT"):
                ziel = cmd[4:10]
            else:
                return 0
            if len(ziel) != 6 or any(c not in "0123456789abcdefABCDEF"
                                     for c in ziel):
                return 0
            # Rundrufe und Sammeladressen hoert ohnehin niemand schlafend.
            if ziel[:2].lower() in ("00", "e0", "f0", "ff"):
                return 0
            ccu = self.by_hmid.get(ziel.lower())
            if not ccu:
                return 0
            d = (getattr(self.qccu, "devices", None) or {}).get(ccu)
            hoerer = getattr(d, "hoerer", None)
            return LM_BURST_STUFE.get(hoerer, 0)
        except Exception:                                # noqa: BLE001
            return 0

    def _submit(self, cmd, kind, not_before=0.0, expect=None, burst=None):
        """Einen Befehl an den Stick einreihen.

        `burst=None` heisst „selbst entscheiden" und ist der Normalfall.
        Ausdruecklich `burst=0` setzt, wer weiss, dass das Geraet gerade WACH
        ist — eine Quittung oder Zeitauskunft geht an ein Geraet, das eben
        selbst gesendet hat, und braucht keinen Vorlauf. Das ist keine
        Sparsamkeit, sondern Genauigkeit: 360 ms Vorlauf sind 360 ms
        Sendezeit, und das Konto des Sticks ist die knappste Groesse im Haus.
        """
        if burst is None:
            burst = self._burst_stufe(cmd) if kind == "cmd" else 0
        if burst and not self.burst_faehig:
            # Ein alter Stick kann es nicht — dann lieber ohne Vorlauf senden
            # als gar nicht, aber es muss im Protokoll stehen. Sonst sucht
            # jemand den Fehler beim Geraet.
            self._log("##", f"BURST NICHT MOEGLICH (Stick kann kein mb) — "
                            f"{cmd[:10]} geht ohne Vorlauf hinaus")
            burst = 0
        if burst:
            # `ms…`/`mT…` wird zu `mb<stufe>s…`/`mb<stufe>T…`.
            cmd = f"mb{burst}{cmd[1:]}"
        job = _Job(cmd, kind, not_before, expect, burst)
        if kind in ("answer", "zeit") and cmd.startswith("ms"):
            # Merken, damit ein nachgereichter Befehl der Quittung den Vortritt
            # laesst (`_antwort_abwarten`).
            self._antwort_offen[cmd[2:8].lower()] = job
        # Antworten auf einen empfangenen Frame gehoeren in die Antwort-Schlange:
        # nur dort wird `not_before` eingehalten. Wer sofort zurueckfunkt, redet
        # womoeglich, bevor das Geraet wieder zuhoert — die echte Zentrale
        # laesst rund 130 ms verstreichen.
        (self._ansq if kind in ("answer", "zeit") else self._txq).put(job)
        return job

    def _antwort_abwarten(self, hmid, frist=0.6):
        """Warten, bis die Quittung an dieses Geraet draussen ist.

        Die Zentrale reiht erst die ANSWER (mit dem Wach-Bit) ein und stoesst
        dann die wartenden Daten an (`handleApplicationFrame`: das
        `createApplicationResponseTransaction` steht vor dem
        `PENDING_DATA_COMMAND`). Auf der Leitung hier hatte der Befehlsweg
        Vorrang vor dem Antwortweg, und die Quittung liegt ohnehin
        `answer_delay` zurueck — ein nachgereichter CREATE_LINK ging darum VOR
        der Quittung hinaus. Zwei Geraete haben ihn dann nicht angenommen:
        der HmIP-SCI nie (sechs Sendungen, keine einzige Kurzquittung, nur
        unsere ANSWER-Rahmen wurden quittiert; 03.09.2026 11:40 und 11:52),
        der HmIP-SMI55-A erst bei der Wiederholung nach der Quittung (12:08:39,
        Kurzquittung 7DA0 galt der ANSWER, 7DA1 dem zweiten Link). Der WRC6-A
        nahm auch den frueheren an — auf ihn ist kein Verlass fuer die Regel.
        """
        job = self._antwort_offen.get(hmid.lower())
        if job is None:
            return
        if not job.done.is_set():
            job.done.wait(frist)
        # Und dann noch die Kurzquittung des Geraets auf DIESE Quittung
        # verstreichen lassen: die PK-Meldung des Sticks traegt keine
        # Sequenznummer, `_link_senden` erkennt eine Kurzquittung ereignisweise
        # — kaeme die des ANSWER-Rahmens erst nach dem `mac.clear()` des
        # Links, gaelte sie dem Link (SMI55-A: 23 ms, WRC6-A: 61 ms nach dem
        # Schreiben). Die Zeit ist kurz gegen das Wachfenster.
        time.sleep(ACK_NACHLAUF)

    def _wach_bit(self, hmid):
        """`APP_STAY_AWAKE`, wenn fuer dieses Geraet noch etwas aussteht.

        So macht es die Zentrale: `createAnswer(..., stayAwake, ...)` und
        `createCurrentTimeFrame(..., stayAwake, ...)` bekommen beide
        `dataPending` herein. Ein Geraet, das nur kurz aufwacht, bleibt
        dadurch wach, bis der wartende Befehl draussen ist — sonst schlaeft
        es zwischen Quittung und Befehl wieder ein.
        """
        with self.lock:
            return APP_STAY_AWAKE if self._wartend.get(hmid.lower()) else 0

    def _answer(self, hmid, appseq):
        """ANSWER auf einen Frame, der eine Antwort angefordert hat."""
        if not self.answer_enabled:
            self._log("##", f"ANTWORT UNTERDRUECKT appSeq=0x{appseq:02X}")
            return
        kopf = FT_ANSWER | self._wach_bit(hmid)
        # Nutzlast wie `AnswerFrame.generatePayload`: AnswerType 0 (angenommen)
        # | 0x40, wenn eine Konfigurationsuebertragung fuer das Geraet aussteht.
        with self.lock:
            konfig = any(e.get("kette") for e in (self._wartend.get(hmid.lower()) or []))
        self._submit(f"ms{hmid.upper()}{kopf:02X}{appseq:02X}{0x40 if konfig else 0:02X}",
                     "answer", time.time() + self.answer_delay)

    @staticmethod
    def zeit_payload(t):
        """Die sieben Bytes einer TIME_INFO-Nutzlast aus einer Ortszeit.

        Aufbau, an einem Frame der echten CCU verifiziert
        (`00 1a 08 4b 96 19 19` = Dienstag, 11.08.2026 22:25:25):

            [0] 0x00      Bedeutung offen — steht im Referenzframe so
            [1] Jahr-2000     (Jar: `TimeInfoFrame.setPayload`, 2000+p[1])
            [2] Monat
            [3] Wochentag<<5 | Tag   (Wochentag: Sonntag=0, Dienstag=2)
            [4] Sommerzeit<<6 | Stunde
            [5] Minute
            [6] Sekunde

        Geliefert wird ORTSZEIT: im Referenzframe stand die Uhrzeit der
        Aufnahme, nicht UTC.
        """
        wochentag = (t.tm_wday + 1) % 7          # Python Montag=0 -> CCU Sonntag=0
        # ⚠️ Die oberen zwei Bits des Stundenbytes sind der Sommerzeit-Zustand,
        # nicht „offen": `TimeInfoFrame.setPayload` liest
        # `daylightSavingState = (byte)(temp >> 6 & 3)`, und
        # `HomeMaticIPFrameFactory.createCurrentTimeFrame` setzt ihn auf 2 bei
        # Sommerzeit, sonst auf 1. Der Referenzframe (August, MESZ) trug
        # `0x96` = 2<<6 | 22 — das passt und war der Grund, warum hier lange
        # ein festes `0x80` stand. Fest waere es ab der Zeitumstellung falsch.
        sommerzeit = 2 if t.tm_isdst else 1
        return bytes((0x00,
                      t.tm_year - 2000,
                      t.tm_mon,
                      (wochentag << 5) | t.tm_mday,
                      (sommerzeit << 6) | t.tm_hour,
                      t.tm_min,
                      t.tm_sec))

    def _time_info(self, hmid, appseq):
        """Die Zeitanfrage eines Geraets mit der Ortszeit beantworten."""
        p = self.zeit_payload(time.localtime())
        kopf = FT_TIME_INFO | self._wach_bit(hmid)
        job = self._submit(f"ms{hmid.upper()}{kopf:02X}{appseq:02X}{p.hex().upper()}",
                           "zeit", time.time() + self.answer_delay)
        if self.verbose:
            print(f"  Zeit an {hmid}: {time.strftime('%a %d.%m.%Y %H:%M:%S')}")
        return job

    # Ab welcher Aenderung ein neuer Pegel gemeldet wird. Der Wert schwankt
    # von Frame zu Frame um ein bis zwei dB; jede Zuckung zu melden fuellte
    # die Aufzeichnung der Gegenstelle, ohne etwas zu sagen.
    RSSI_SCHWELLE = 3

    def _rssi_merken(self, src, payhex):
        """Empfangspegel eines Geraeteframes festhalten und melden.

        Der Stick haengt an jede Zeile `<rssi><lqi>` hinter die Nutzlast; der
        Pegel ist ein Zweierkomplement in dBm, wie der CC1101 ihn nach
        Datenblatt 17.3 liefert (die Umrechnung macht die Firmware).

        Gemeldet wird als **`RSSI_DEVICE`** an Kanal 0 — der Parameter, den
        eine Zentrale von eQ-3 dort fuehrt (an der HmIP-PS-2 nachgesehen:
        `RSSI_DEVICE` und `RSSI_PEER`, beide INTEGER −128…127) und den die
        Gegenstelle als Signalstaerke des Geraets liest
        (`aiohomematic … device.signal_strength`).

        ⚠️ Was hier steht, ist **unsere Messung: wie stark WIR das Geraet
        hoeren.** Den umgekehrten Weg — was das Geraet von uns empfaengt —
        meldet es uns nicht; in keinem Statusframe steckt so ein Wert.
        `RSSI_PEER` bleibt daher leer, statt eine Zahl zu erfinden.
        """
        q = self.qccu
        addr = self.by_hmid.get(src)
        if not addr or q is None or not hasattr(q, "set_value_internal"):
            return
        try:
            b = bytes.fromhex(payhex)
        except ValueError:
            return
        if not b:
            return
        ln = b[0]
        if len(b) < 1 + ln + 2:            # ohne <rssi><lqi> nichts zu holen
            return
        wert = b[1 + ln] - 256 if b[1 + ln] > 127 else b[1 + ln]
        if wert == 0 or wert < -128 or wert > 0:
            # 0 und positive Werte sind kein Pegel, sondern eine Zeile ohne
            # Messung (oder eine Firmware vor 2.0.22, die falsch rechnete).
            return
        vorher = self._rssi.get(src)
        self._rssi[src] = wert
        if vorher is not None and abs(wert - vorher) < self.RSSI_SCHWELLE:
            return
        try:
            q.set_value_internal(addr, 0, "RSSI_DEVICE", wert)
        except Exception as ex:                          # noqa: BLE001
            if self.verbose:
                print(f"  ! Pegel nicht vermerkt: {ex}")

    def _icmp(self, payhex, src, dst, sn, stick_acked=False):
        """Ein ICMPv6-Frame: auswerten, vermerken, beantworten."""
        try:
            raw = bytes.fromhex(payhex)
        except Exception:
            return
        if not raw or len(raw) < 1 + raw[0]:
            return
        pl = raw[1:1 + raw[0]]
        if not pl:
            return
        t = pl[0]
        code = pl[1] if len(pl) > 1 else None
        name = ICMP_NAME.get(t, f"ICMP 0x{t:02X}")
        self.icmp_seen[name] = self.icmp_seen.get(name, 0) + 1
        where = GROUP_ADDR.get(dst, dst)
        self._log("<<", f"ICMP {name} {src}->{where}"
                        + (f" code=0x{code:02X}" if code is not None else ""))
        if self.verbose:
            print(f"  Netz-Haushalt: {name} von {src} an {where}")

        addr = self.by_hmid.get(src)
        if addr:
            try:
                self.qccu.note_reachable(addr, True)
            except Exception as ex:
                print(f"  ! Lebenszeichen nicht vermerkt: {ex}")

        if not self.icmp_answer:
            return

        to_group = dst in GROUP_ADDR

        if t == ICMP_NEIGHBOR_ADVERTISEMENT and to_group and sn and not stick_acked:
            self._submit(f"mT41{src.upper()}00{MAC_ROLE_CENTRAL:02X}{sn.upper()}",
                         "answer")
            return

        if t == ICMP_ECHO_REQUEST:
            self._submit(f"mT21{src.upper()}{ICMP_ECHO_REPLY:02X}"
                         f"{(code or 0):02X}", "answer")
            return

        if t == ICMP_NEIGHBOR_SOLICITATION:
            self._log("##", "NEIGHBOR_SOLICITATION unbeantwortet "
                            "(Antwortform nicht belegt)")
            return

        if t == ICMP_ROUTER_SOLICITATION:
            self._log("##", "ROUTER_SOLICITATION unbeantwortet "
                            "(Antwortform nicht belegt)")

    def _read_counters(self, timeout=0.6):
        """Zaehlwerke des Sticks abholen (`m` -> `Pm rx=… ok=… …`)."""
        self._cnt_ev.clear()
        job = self._submit("m", "probe")
        if not job.written.wait(timeout):
            return None
        if not self._cnt_ev.wait(timeout):
            return None
        return dict(self.counters)

    def _next_seq(self, hmid):
        """Eigener, fortlaufender UNGERADER Zaehler je Geraet."""
        with self.lock:
            if hmid not in self.appseq and hmid in self.devseq:
                self.appseq[hmid] = self.devseq[hmid]
            s = (self.appseq.get(hmid, 0) + 2) & 0xFF
            if not s % 2:
                s = (s + 1) & 0xFF
            if not s:
                s = 1
            self.appseq[hmid] = s
        self._save_state()
        return s

    def on_set(self, ccu_address, channel, param, value):
        """Befehl ANNEHMEN und sofort zurueckkehren — mit dem Auftrag, an dem
        sich der Ausgang abwarten laesst (`Stellauftrag`)."""
        auftrag = Stellauftrag()
        self._cmdq.put((ccu_address, channel, ((param, value),), auftrag))
        return auftrag

    def on_set_many(self, ccu_address, channel, werte):
        """Einen SATZ annehmen — er geht in EINEM Rahmen hinaus."""
        auftrag = Stellauftrag()
        self._cmdq.put((ccu_address, channel, tuple(werte), auftrag))
        return auftrag

    def kann_stellen(self, ccu_address, channel, werte):
        """Laesst sich fuer diesen Satz ein belegter Rahmen bauen?

        Fuer `putParamset`: was hier durchfaellt, darf nicht als „gesetzt"
        zurueckgemeldet werden.
        """
        try:
            return self._rumpf(ccu_address, channel, tuple(werte)) is not None
        except Exception:                                    # noqa: BLE001
            return False

    def _cmd_worker(self):
        while not self._stop:
            try:
                job = self._cmdq.get(timeout=0.2)
            except queue.Empty:
                continue
            auftrag = job[3] if len(job) > 3 else None
            try:
                acked = self._do_set(job[0], job[1], job[2], auftrag)
            except Exception as ex:
                print(f"  ! Sendepfad scheiterte: {ex}")
                if auftrag is not None:
                    auftrag.fertig(None, None, f"Sendepfad scheiterte: {ex}")
                continue
            if acked is not None:
                try:
                    self.qccu.note_reachable(job[0], bool(acked))
                except Exception as ex:
                    print(f"  ! Erreichbarkeit nicht vermerkt: {ex}")

    def _stellbefehl(self, ccu_address, channel, param, value):
        """EIN Datenfeld bauen — (Aktion, Datentyp, Hexdaten, Klartext).

        Den fertigen Rumpf baut `_rumpf`; hier entsteht nur der Beitrag EINES
        Parameters, weil sich mehrere ein Byte teilen koennen.

        JEDER Stellbefehl ist ein DIRECT_EXECUTION_COMMAND
        (`ApplicationFrameType 6`; auf der Luft `0x86`, das obere Bit ist der
        Antwortwunsch). Der Rumpf beginnt immer mit
        `<Aktion> <Kanal>` — so baut ihn `DirectExecutionCommandFrame.
        generatePayload()` im HMIPServer-Jar, und so liest ihn `setPayload()`
        wieder ein. Danach unterscheidet die Aktion zwei Formen:

            EXECUTION_START (2)        ein einzelnes Pegelbyte
            geraetespezifisch (128..)  Paare <Datentyp> <Daten>

        Welche Aktion und welcher Datentyp zu einem Parameter gehoeren, steht
        in seiner StateParameter-Fabrik als die drei letzten Argumente
        `(directExecutionCode, dataIndex, dataType)` — abgeschrieben, nicht
        geraten (`STELLBEFEHLE`).

        Der Wert wird mit DERSELBEN Umrechnung zurueckgerechnet, mit der ein
        gemeldeter Wert hereinkommt (`UMRECHNUNG`, hier rueckwaerts:
        `roh = wert * Faktor + Offset`) — eine zweite Zahlentabelle fuer die
        Gegenrichtung wuerde frueher oder spaeter auseinanderlaufen.

        ⚠️ Gegen die Grenzen des Parameters geprueft, bevor etwas hinausgeht.
        Ein Sollwert von 40 °C ist an einem Geraet mit MAX 30.5 kein Befehl,
        sondern ein Byte, das es verwerfen oder missdeuten kann.
        """
        fassung = self._variante(ccu_address, channel, param)
        eintrag = STELLBEFEHLE.get(f"{param}@{fassung}")
        if eintrag is None and fassung == "default":
            eintrag = STELLBEFEHLE.get(param)
        if eintrag is None:
            if fassung != "default" and (param in STELLBEFEHLE
                                         or any(k.startswith(param + "@")
                                                for k in STELLBEFEHLE)):
                self._form_unbekannt(ccu_address, channel, param, fassung)
            elif self.verbose:
                print(f"  ! {param} wird nicht gesendet — kein belegter "
                      f"Stellbefehl fuer diesen Parameter")
            return None
        aktion, datentyp, dataindex, wahr = eintrag

        desc = self._beschreibung(ccu_address, channel, param)
        if desc is None:
            if self.verbose:
                print(f"  ! {ccu_address}:{channel} fuehrt {param} nicht")
            return None

        # ⚠️ Was die Geraetebeschreibung nicht als SCHREIBBAR ausweist, geht
        # nicht hinaus — `OPERATIONS` Bit 1 (Wert 2) ist WRITE. Ohne diese
        # Pruefung baut QCCU einen wohlgeformten Rahmen fuer einen Wert, den
        # das Geraet nur MELDET, und der Fehler faellt nicht auf.
        #
        # Es trifft echte Faelle, nicht nur gedachte: STATE steht am
        # HmIP-BWTH-A auf den Schaltkanaelen 8..12 als OPERATIONS 5
        # (SWITCH_VIRTUAL_RECEIVER **v3**), waehrend dieselbe Kanalgattung in
        # v1/v2/v4/v5/v6 die 7 traegt. Das ist kein Tabellenfehler: die
        # Referenz einer echten Zentrale meldet fuer die Relaiskanaele der
        # HmIP-PS-2 ebenfalls 7, und der BWTH-A schaltet sein Relais ueber die
        # interne Verdrahtung 8->10 selbst — die Zentrale hat da nichts zu
        # suchen. Neun Kanaltypen fuehren aus demselben Grund LEVEL als reinen
        # Messwert (BLIND_/DIMMER_/SHUTTER_/SERVO_TRANSMITTER …).
        #
        # Eine echte Zentrale weist so einen Schreibversuch schon an der
        # XML-RPC-Grenze ab; QCCU meldet ihn hier als „kein belegter Weg",
        # womit `putParamset` einen Fault gibt und nichts eingetragen wird.
        ops = desc.get("OPERATIONS")
        if isinstance(ops, int) and not ops & 2:
            if self.verbose:
                print(f"  ! {param} ist auf {ccu_address}:{channel} nicht "
                      f"schreibbar (OPERATIONS {ops}) — nicht gesendet")
            return None

        # Ein Zeitpunkt ist keine Zahl: PARTY_TIME_START/_END kommen als
        # Zeichenkette "2026_09_01 18:00" herein (LogicalType STRING mit
        # DateStringToInteger) und werden nicht umgerechnet, sondern zerlegt.
        if datentyp == DT_PERIOD:
            roh = datum_roh(value)
            daten = None if roh is None else daten_bytes(datentyp, dataindex, roh)
            if daten is None:
                if self.verbose:
                    print(f"  ! {param}={value!r} ist kein Zeitpunkt der Form "
                          f"'{DATUM_FORM}' — nicht gesendet")
                return None
            return (aktion, datentyp, daten,
                    f"{param}={value!r} (Feld 0x{daten})")

        # BOOL kommt als Wahrheitswert herein, ENUM als Zeichenkette aus der
        # Werteliste — beides erst in eine Zahl bringen, dann umrechnen.
        typ = desc.get("TYPE")
        try:
            if typ == "BOOL":
                # Der Wert fuer WAHR steht in der Tabelle: STATE traegt 0xC8,
                # weil das Byte einer EXECUTION_START ein PEGEL ist und ein
                # eingeschalteter Schaltkanal auf 100 % steht.
                zahl = wahr if value in (True, 1, "1", "true", "True", "on") else 0
            elif typ == "ENUM":
                liste = list(desc.get("VALUE_LIST") or ())
                zahl = liste.index(value) if value in liste else int(value)
            else:
                zahl = float(value)
        except (TypeError, ValueError):
            if self.verbose:
                print(f"  ! {param}={value!r} ist kein brauchbarer Wert")
            return None

        if typ in ("FLOAT", "INTEGER"):
            mn, mx = desc.get("MIN"), desc.get("MAX")
            if (isinstance(mn, (int, float)) and isinstance(mx, (int, float))
                    and not mn <= zahl <= mx):
                if self.verbose:
                    print(f"  ! {param}={value!r} liegt ausserhalb "
                          f"{mn}..{mx} — nicht gesendet")
                return None

        faktor, versatz = umrechnung(param, fassung)
        roh = int(round(zahl * faktor + versatz))
        daten = daten_bytes(datentyp, dataindex, roh)
        if daten is None:
            if self.verbose:
                print(f"  ! {param}={value!r} ergibt {roh} und passt nicht in "
                      f"das Datenfeld (Datentyp {datentyp}, Lage {dataindex}) "
                      f"— nicht gesendet")
            return None

        return (aktion, datentyp, daten,
                f"{param}={value!r} (roh 0x{roh & 0xFF:02X}, Feld 0x{daten})")

    def _heutiger_wert(self, ccu_address, channel, param):
        """Was das Geraet fuer diesen Parameter zuletzt gemeldet hat."""
        d = (getattr(self.qccu, "devices", None) or {}).get(ccu_address.upper())
        werte = getattr(d, "values", None) or {}
        return werte.get((int(channel), param))

    def _rumpf(self, ccu_address, channel, paare):
        """Den Rumpf EINES DIRECT_EXECUTION_COMMAND bauen.

        `paare` ist eine Folge von (Parameter, Wert) — eines fuer `setValue`,
        mehrere fuer `putParamset`. Ein Parameter kann weitere nach sich
        ziehen (`KOMPOSITE`), und mehrere Werte koennen sich EIN Byte teilen.
        Das Jar legt sie in eine `TreeMap` unter <Code, Datentyp, Occurrence>
        und VERODERT, was auf denselben Schluessel faellt
        (`TransactionTaskFactory`, „oldData"-Zweig); die Reihenfolge im Rahmen
        ist die des Schluessels, also nach Datentyp aufsteigend.

        ⚠️ Darum darf ein Satz NICHT in mehrere Rahmen zerfallen. Genau das
        tat `putParamset` bisher — es schleifte die Werte einzeln durch
        `setValue`. Sichtbar am 30.08.2026: `{CONTROL_MODE: 1,
        SET_POINT_TEMPERATURE: 22.0}` ging als `80 01 04 40` UND
        `80 01 02 2C` hinaus, wo die Zentrale `80 01 02 6C 04 40` schickt —
        dazwischen sah das Geraet „manuell mit dem alten Sollwert".
        """
        satz = []
        mitgeschickt = {str(p) for p, _ in paare}
        for param, value in paare:
            wert = satz_wert(value)
            teil = KOMPOSITE.get((param, wert))
            if teil is None and param in NUR_IM_SATZ:
                # Kein eigener Satz — dann muss der Aufrufer die Partner
                # selbst mitgeschickt haben (Abwesenheitsmodus).
                regeln = NUR_IM_SATZ[param]
                noetig = regeln.get(wert, regeln.get(None))
                if noetig is None or not mitgeschickt.issuperset(noetig):
                    if self.verbose:
                        print(f"  ! {param}={value!r} kennt keinen belegten "
                              f"Satz — einzeln geschickt wuerde dieser "
                              f"Parameter am Geraet Schaden anrichten, es "
                              f"geht nichts hinaus")
                    return None
                if self.verbose:
                    print(f"  {param}={value!r} geht zusammen mit "
                          f"{', '.join(noetig)} in EINEM Rahmen hinaus")
            if teil is None:
                satz.append((param, value))
            else:
                if self.verbose:
                    print(f"  {param}={value!r} wird als Satz geschrieben "
                          f"({', '.join(p for p, _ in teil)}) — so haelt es "
                          f"die Zentrale in stateRules.json")
                satz.extend(teil)

        bloecke = {}
        klartexte = []
        for p, v in satz:
            if v is None:
                v = self._heutiger_wert(ccu_address, channel, p)
                if v is None:
                    if self.verbose:
                        print(f"  ! {p} ist am Geraet nicht bekannt — der "
                              f"Satz bleibt ungesendet")
                    return None
            feld = self._stellbefehl(ccu_address, channel, p, v)
            if feld is None:
                return None
            aktion, datentyp, daten, klartext = feld
            klartexte.append(klartext)
            schluessel = (aktion, datentyp)
            roh = bytes.fromhex(daten)
            schon = bloecke.get(schluessel)
            if schon is not None:
                breite = max(len(schon), len(roh))
                roh = roh.rjust(breite, b"\x00")
                schon = schon.rjust(breite, b"\x00")
                roh = bytes(a | b for a, b in zip(roh, schon))
            bloecke[schluessel] = roh

        aktionen = {a for a, _ in bloecke}
        if len(aktionen) != 1:
            if self.verbose:
                print(f"  ! der Satz mischt Aktionen {aktionen} — das baut "
                      f"die Zentrale nicht so, nicht gesendet")
            return None
        aktion = aktionen.pop()

        kanal = int(channel)
        rumpf = f"{aktion:02X}{kanal:02X}"
        for (_, datentyp) in sorted(bloecke, key=lambda k: (k[1] is not None,
                                                            k[1] or 0)):
            daten = bloecke[(aktion, datentyp)].hex().upper()
            rumpf += daten if datentyp is None else f"{datentyp:02X}{daten}"
        return rumpf, ", ".join(klartexte)

    def _do_set(self, ccu_address, channel, paare, auftrag=None):
        """Der eigentliche Sendevorgang — laeuft im Arbeitsfaden.

        Rueckgabe wie bisher die Kurzquittung; der volle Ausgang (angekommen /
        angenommen / Grund) geht an den `auftrag`, falls einer mitkommt.
        """
        paare = tuple(paare)
        param = "+".join(p for p, _ in paare)
        hmid = None
        with self.lock:
            for h, a in self.by_hmid.items():
                if a == ccu_address.upper():
                    hmid = h
                    break
        if not hmid:
            if self.verbose:
                print(f"  ! keine Funkadresse zu {ccu_address}")
            if auftrag is not None:
                auftrag.fertig(None, None, "keine Funkadresse")
            return

        gebaut = self._rumpf(ccu_address, channel, paare)
        if gebaut is None:
            if auftrag is not None:
                auftrag.fertig(None, None, "kein belegter Rahmen")
            return
        rumpf, klartext = gebaut

        seq = self._next_seq(hmid)
        cmd = f"ms{hmid.upper()}86{seq:02X}{rumpf}"

        before = self._read_counters() if self.measure else None
        self._log("##", f"BEFEHL {ccu_address}:{channel} {klartext} "
                        f"appSeq=0x{seq:02X}")

        ev = threading.Event()
        self._acked[hmid] = ev
        # ⚠️ Die Kurzquittung sagt „angekommen", nicht „angenommen". Das Jar
        # trennt beides (`ApplicationTask.evaluateTaskResponse` wertet die
        # ANSWER ueber die appSeq aus), und `920536e` hat diese Auswertung nur
        # fuer die Verdrahtung nachgezogen. Am 30.08.2026 am HmIP-BWTH-A
        # gemessen: BOOST_MODE ging hinaus, das Geraet antwortete
        # `PM03020980C685` — appSeq 09, AnswerType 0x80 = NAK —, und QCCU
        # meldete `urteile=['ok']`. Der Wert stand derweil in der Oberflaeche.
        #
        # ⚠️ Ein ausbleibender ANSWER ist KEINE Ablehnung. Auf
        # SET_POINT_TEMPERATURE antwortet dasselbe Geraet mit einem
        # STATUS-Rahmen (FrameType 5) statt mit einer ANSWER — gemessen im
        # selben Mitschnitt. Darum wird nur gedeutet, was tatsaechlich kommt.
        antwort = {"ev": threading.Event(), "ergebnis": None}
        with self.lock:
            self._app_ack[(hmid, seq)] = antwort
        angenommen = None
        klartext = "keine ANSWER"
        acked = False
        after = None
        verdicts = []
        try:
            self._gate.clear()
            for attempt in range(1, self.tx_tries + 1):
                ev.clear()
                # ⚠️ JEDER Anlauf weckt. Bis zum 02.09.2026 bekam nur der erste
                # den Vorlauf — „ist das Geraet wach, hoert es die Wiederholung
                # ohnehin". Das stimmt nur, wenn der erste Anlauf es geweckt
                # HAT. Hat er das Wachfenster verfehlt, schlaeft das Geraet
                # weiter, und eine vorlauflose Wiederholung erreicht es genauso
                # wenig wie ein Anlauf, der wegen `Pm ERR LOVF` nie hinausging.
                # Gemessen: Weckrahmen ohne Zustellung 0 von 5, und die
                # vorlauflosen Wiederholungen der alten Logik trafen an einem
                # schlafenden eTRV-E-S in 0 von 14 Faellen.
                #
                # Der eq-3-Coprozessor wiederholt bei ausbleibender Antwort das
                # GANZE Paar — Wecken auf 869,52 plus Zustellen auf 868,30 —
                # 700 ms nach dem ersten Burst-Start (am SPI mitgelesen,
                # `p2_spi.sr`). Seit q-culfw 2.0.91 IST `mb` dieses Paar; ein
                # zweites `mb` ist also die Wiederholung des Copro.
                #
                # Was es kostet: je Anlauf Vorlauf (36) plus zwei Rahmen (~10)
                # vom 900er-Konto des Sticks, Nachfuellung eine Einheit je
                # Sekunde — drei Anlaeufe sind rund 140 Einheiten, also gut
                # zwei Minuten Erholung. Das ist der Preis, den der Copro
                # ebenfalls zahlt; ein billigerer Anlauf, der nichts erreicht,
                # spart nichts.
                #
                # `burst=None` heisst „der Stick entscheidet nach dem Hoerertyp"
                # — fuer ein Geraet ohne Burst-Hoerertyp bleibt jeder Anlauf
                # vorlauflos, dort aendert sich nichts.
                job = self._submit(cmd, "cmd", burst=None)
                job.done.wait(self.verdict_timeout + 0.5
                              + burst_zuschlag(job.burst))
                verdict = job.verdict or "kein Urteil"
                verdicts.append(verdict)
                if self.verbose:
                    extra = f"  (Versuch {attempt})" if attempt > 1 else ""
                    print(f"  -> {cmd}{extra}  Stick: {verdict}")
                if job.verdict == "err":
                    self._log("##", "Stick hat NICHT gesendet")
                    continue
                if ev.wait(self.tx_timeout):
                    acked = True
                    if self.verbose and attempt > 1:
                        print(f"     quittiert nach Versuch {attempt}")
                    break
            after = self._read_counters() if before else None

            # ⚠️ Noch INNERHALB des try: eine Ausnahme weiter oben darf den
            # Eintrag nicht stehenlassen. `_cmd_worker` faengt sie und meldet
            # nur „Sendepfad scheiterte" — der Eintrag bliebe sonst liegen.
            if antwort["ev"].wait(LINK_ANTWORT_ZEIT if acked else 0):
                angenommen, klartext = antwort_deuten(antwort["ergebnis"])
        finally:
            self._acked.pop(hmid, None)
            with self.lock:
                self._app_ack.pop((hmid, seq), None)
            self._gate.set()

        if not acked and self.verbose:
            print(f"     keine Quittung nach {self.tx_tries} Versuchen")
        if angenommen is False:
            self._log("##", f"BEFEHL {ccu_address}:{channel} {param} vom "
                            f"Geraet ABGELEHNT ({klartext})")
            if self.verbose:
                print(f"  ! {ccu_address}:{channel} {param} abgelehnt "
                      f"({klartext}) — der Wert gilt am Geraet NICHT")
        self._log("##", f"ERGEBNIS quittiert={acked} urteile={verdicts} "
                        f"antwort={klartext}")

        if before and after:
            d = {k: after[k] - before[k] for k in CNT_KEYS}
            txt = " ".join(f"{k}+{v}" for k, v in d.items() if v)
            self._log("##", f"ZAEHLWERKE {txt or 'unveraendert'}")
            if self.verbose:
                print(f"     Zaehlwerke {txt or 'unveraendert'}")
        if auftrag is not None:
            auftrag.fertig(acked, angenommen, klartext)
        return acked


    def radio_state(self):
        """Zustand fuer die Oberflaeche."""
        self._read_counters()
        return {"counters": self.counters, "funkgute": self.funkgute,
                "v_banner": self.v_banner, "pfad": self.port,
                "budget": self.budget,
                "own_addr": self.own_addr,
                "tot": bool(self.tot), "tot_grund": self.tot_grund,
                # Ohne Netzwerkschluessel schlaegt jedes Anlernen fehl. Der
                # Zustand stand frueher nur im Protokoll — die Oberflaeche
                # meldete derweil „Firmware aktuell", und der Anwender suchte
                # den Fehler beim Geraet.
                "netzschluessel_fehlt": bool(self.netzschluessel_fehlt),
                # Kann der Stick wecken, und auf welchem Kanal? Auf einer
                # Zentrale ohne Mitschnitt ist das sonst nirgends ablesbar
                # (03.09.2026 auf dem HAOS-Rechner erlebt).
                "burst_faehig": self.burst_faehig,
                "weckkanal": self.weckkanal,
                # Damit die Oberflaeche den Download nur zeigt, wenn es auch
                # etwas zu holen gibt.
                "mitschnitt": sum(os.path.getsize(p) for p in self.raw_dateien()
                                  if os.path.exists(p)) if self._raw else None,
                "icmp": dict(self.icmp_seen),
                "devices": {h: a for h, a in self.by_hmid.items()}}

    # Wie lange auf die Kurzquittung gewartet wird. Die Gegenstelle antwortet
    # binnen weniger Millisekunden; eine Sekunde ist reichlich und haelt den
    # Knopf in der Oberflaeche flott.
    PING_WARTEN = 1.0

    def erreichbarkeit_pruefen(self, hmid, warten=None):
        """Aktiv nachsehen, ob ein Geraet noch antwortet.

        ⚠️ Ohne das faellt Stille erst auf, wenn jemand vergeblich schaltet:
        `unreach` wird gesetzt, wenn ein Befehl dreimal unquittiert bleibt —
        und ein Geraet, das niemand schaltet, gilt beliebig lange als in
        Ordnung. Am Aufbau gemessen (19.08.2026): eine PS-2 war zwei Stunden
        aus dem Netz, die Oberflaeche meldete unveraendert „erreichbar".

        Gesendet wird die **Uhrzeit** — dieselbe Auskunft, die die Zentrale
        einem fragenden Geraet ohnehin gibt. Sie aendert am Geraet nichts und
        ist die harmloseste Sendung, die es gibt. Die Antwort ist nicht der
        Inhalt, sondern die **Kurzquittung**: jeden Unicast quittiert ein
        HmIP-Geraet mit sechs Byte, ohne Zaehler und ohne Verschluesselung.
        Kommt sie, lebt das Geraet und hoert uns.

        ⚠️ Das kostet Sendezeit — sparsam benutzen, nicht im Minutentakt.

        Rueckgabe: True (antwortet), False (keine Antwort), None (kein Funk
        oder Geraet unbekannt).
        """
        hmid = (hmid or "").lower()
        if not hmid or hmid not in self.by_hmid:
            return None
        ev = threading.Event()
        self._acked[hmid] = ev
        try:
            # Der eigene Auftrag wird vermerkt, damit seine Kurzquittung auch
            # als seine erkannt wird (siehe Waechter in `_handle`).
            self._acked_job[hmid] = self._time_info(hmid, self._next_seq(hmid))
            antwortet = ev.wait(warten or self.PING_WARTEN)
        finally:
            self._acked.pop(hmid, None)
            self._acked_job.pop(hmid, None)
        addr = self.by_hmid.get(hmid)
        if addr:
            try:
                self.qccu.note_reachable(addr, bool(antwortet))
            except Exception as ex:                  # noqa: BLE001
                print(f"  ! Erreichbarkeit nicht vermerkt: {ex}")
        if self.verbose:
            print(f"  Erreichbarkeit {hmid}: "
                  + ("antwortet" if antwortet else "keine Antwort"))
        self._log("##", f"PING {hmid} -> {'ok' if antwortet else 'keine Antwort'}")
        return bool(antwortet)

    def funk_exclude(self, hmid, warten=2.5):
        """Ein Geraet ueber Funk ausschliessen — wie die CCU beim Loeschen.

        Ohne diesen Anruf erfaehrt das Geraet nie, dass es entlassen wurde: es
        funkt weiter zu einer Zentrale, die es nicht mehr kennt, und laesst
        sich erst nach einem Werksreset von Hand wieder anlernen. Mit ihm geht
        es von selbst in den Anlernzustand zurueck.

        Die Folge steht im Referenzmitschnitt der echten CCU:

            ZENTRALE->GERAET  DEVICE_EXCLUDE_REQUEST   (0xF0)
            GERAET->ZENTRALE  DEVICE_EXCLUDE_READY     (0xF1)
            ZENTRALE->GERAET  DEVICE_EXCLUDE_CONCLUDE  (0xF2)

        Alle drei sind NETWORK_MGMT (Inhaltsart 1, daher `mT11`), ohne
        Nutzlast — die Aussage steckt allein im Typ-Byte.

        Rueckgabe: True, wenn das Geraet mit READY geantwortet hat. Der
        Abschluss geht in jedem Fall raus; ein Geraet, das gerade stromlos
        ist, soll den Ausschluss nicht dauerhaft blockieren.
        """
        dst = hmid.upper()
        ev = threading.Event()
        self._exclude_ready[hmid.lower()] = ev
        try:
            self._submit(f"mT11{dst}{NM_EXCLUDE_REQUEST:02X}", "cmd")
            bestaetigt = ev.wait(warten)
        finally:
            self._exclude_ready.pop(hmid.lower(), None)

        # Der Abschluss folgt kurz darauf — bei der echten CCU 77 ms nach der
        # Bereitmeldung.
        time.sleep(0.1)
        self._submit(f"mT11{dst}{NM_EXCLUDE_CONCLUDE:02X}", "cmd")
        m = bestaetigt
        with self.lock:
            # ⚠️ Wer hier hinausfaellt, kann gleich wieder anklopfen — dasselbe
            # Geraet, neue Funkadresse. Die alte Zuordnung merken wir uns, um
            # sie beim Wiederanlernen aus der Verwaisten-Liste zu raeumen:
            # sonst steht das Geraet dort als „funkt noch, gehoert nicht dazu",
            # obwohl es laengst wieder angelernt ist (19.08.2026 gesehen —
            # c5410d blieb neben dem frisch angelernten c5410e stehen).
            kennung = self.by_hmid.pop(hmid.lower(), None)
            if kennung:
                self._ehemals[kennung.upper()] = hmid.lower()
        if self.verbose:
            print(f"  Ausschluss {hmid}: "
                  + ("bestaetigt" if m else "ohne Antwort abgeschlossen"))
        return bool(m)

    def _ask(self, cmd, pattern, tries=3):
        """Frage an den Stick, Antwort abwarten."""
        pat = re.compile(pattern)
        for _ in range(tries):
            job = self._submit(cmd, "ask", expect=pat)
            if job.done.wait(self.ask_timeout + 0.5) and job.reply:
                return job.reply
        return None

    def start_pairing(self, sticker=None, seconds=60, next_addr=None,
                      local_key=None):
        """Anlernfenster oeffnen."""
        key = None
        src = ""
        lk = _nur_zeichen(local_key)
        st = _nur_zeichen(sticker).upper()

        if lk:
            if len(lk) != 32:
                return f"LocalKey hat {len(lk)} statt 32 Hexziffern"
            try:
                key = bytes.fromhex(lk)
            except ValueError:
                return "LocalKey ist keine gueltige Hexzahl"
            src = "LocalKey"
        elif st:
            # Wer die 32 Hexziffern zur Hand hat, soll sie hier eintragen
            # duerfen — die Anleitung nennt beide Formen, und ein Anwender
            # kann nicht wissen, dass sie intern in zwei Feldern liegen.
            if len(st) == 32 and all(c in "0123456789ABCDEF" for c in st):
                key = bytes.fromhex(st)
                src = "LocalKey"
            else:
                if len(st) != 26:
                    return (f"Aufkleber hat {len(st)} statt 26 Zeichen "
                            f"(Form ABCEF-GHJKL-MNPQR-STUWX-YZ2345) — "
                            f"oder 32 Hexziffern. Trennzeichen sind egal.")
                try:
                    key = sticker_to_local_key(st)
                except ValueError as ex:
                    # ⚠️ Das Alphabet des Aufklebers kennt kein D, I, O und V —
                    # gerade weil sie sich mit 0, 1 und U verwechseln lassen.
                    # Wer eines davon eintippt, hat fast immer abgelesen statt
                    # sich vertan; das gehoert in die Meldung, sonst sucht er
                    # den Fehler beim Geraet.
                    verwechselt = sorted({c for c in st if c in "DIOV"})
                    rat = (f" — die Zeichen {', '.join(verwechselt)} gibt es "
                           f"auf dem Aufkleber nicht; gemeint sind vermutlich "
                           f"{', '.join({'D': '0', 'I': '1', 'O': '0', 'V': 'U'}[c] for c in verwechselt)}"
                           if verwechselt else "")
                    return f"Aufkleber enthaelt ein unzulaessiges Zeichen ({ex}){rat}"
                src = "Aufkleber"
        else:
            return "Aufkleber oder LocalKey noetig"

        if next_addr:
            self.pair_next_addr = next_addr.lower()
        if not self.pair_next_addr:
            self.pair_next_addr = zufaellige_adresse((self.own_addr,))[:4] + "01"
        self.pair_key = key
        self.pair_key_src = src
        self.pair_until = time.time() + float(seconds)
        self.pair_last = (f"Fenster offen ({int(seconds)} s), {src}, "
                          f"Adresse {self.pair_next_addr}")
        self._log("##", f"ANLERNEN offen {seconds}s -> {self.pair_next_addr}")
        # Dieselbe Liste wie bei BidCoS: wer den Knopf drueckt, soll die
        # Wirkung sehen, ohne ins Protokoll zu steigen.
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            merke("ok", f"HmIP Anlernfenster offen ({int(seconds)} s)")
        if self.verbose:
            print(f"  Anlernen: Fenster {int(seconds)} s offen, "
                  f"neue Adresse {self.pair_next_addr}")
        return None

    def stop_pairing(self):
        # Nur melden, wenn wirklich eines offen war — sonst erzeugt jeder
        # routinemaessige Aufruf einen Eintrag.
        war_offen = self.pair_until > time.time()
        self.pair_until = 0.0
        self.pair_key = None
        self.pair_last = "Fenster zu"
        merke = getattr(self.qccu, "merke_ereignis", None)
        if war_offen and merke:
            merke("ok", "HmIP Anlernfenster geschlossen")

    def pair_state(self):
        rest = self.pair_until - time.time()
        return {"open": rest > 0, "seconds_left": int(rest) if rest > 0 else 0,
                "next_addr": self.pair_next_addr, "last": self.pair_last,
                "key_src": self.pair_key_src if self.pair_key else ""}

    def _pair_worker(self):
        """Bearbeitet Anlern-Anfragen."""
        while not self._stop:
            try:
                air = self._pair_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._pair_do(air)
            except Exception as ex:
                self.pair_last = f"Anlernen scheiterte: {ex}"
                self._log("##", f"ANLERNEN FEHLER {ex}")
                if self.verbose:
                    print(f"  ! Anlernen scheiterte: {ex}")

    def _pair_do(self, air):
        src     = air[3:6]
        sgtin   = air[10:22]
        devtype = int.from_bytes(air[24:28], "big")
        # ⚠️ Der Betriebsmodus steht offen im Anlernruf, zwischen Firmware und
        # Schluessel-Flags: `AbstractInclusionRequestFrame` liest SGTIN(12),
        # Herstellercode(2), Geraetetyp(4), Firmware(3), Betriebsmodus(1),
        # Schluessel-Flags(1), Nonce(4), MIC(4), Einmalschluessel(8) — die
        # Nachbarfelder oben und unten belegen die Lage. Bisher las QCCU ihn
        # nicht und behauptete stattdessen fuer JEDES Geraet `0x40`.
        # ⚠️ Die Firmware entscheidet, WELCHE Beschreibung fuer dieses Geraet
        # gilt: die Zentrale waehlt sie ueber das `minVersion`/`maxVersion`-
        # Band (`DeviceType.findDeviceTypeByVersion`). QCCU las die drei Bytes
        # bisher nicht und nahm „die Beschreibung mit den meisten Kanaelen" —
        # beim HmIP-ASIR ist das die alte Fassung OHNE interne Verdrahtung.
        # Gezaehlt wird `major.minor.patch`; als Zahl `major<<16|minor<<8|patch`,
        # genau die Form der Baender (66048 = 1.2.0).
        fassung = f"{air[28]}.{air[29]}.{air[30]}"
        opmode  = air[31]
        nonce   = air[33:37]
        otk     = air[41:49]

        if not self.pair_key or time.time() >= self.pair_until:
            return
        if self._pair_busy:
            return

        self._pair_busy = True
        self.pair_last = f"Anfrage von {src.hex()}, Typ {devtype} — Angebot wird gebaut"
        if self.verbose:
            print(f"  Anlernen: Anfrage von {src.hex()}, Kennung {sgtin.hex()}, "
                  f"Typ {devtype}")

        devkey  = self.pair_key
        ap_otk  = secrets.token_bytes(8)
        nonce_ks = secrets.token_bytes(4)
        newa    = bytes.fromhex(self.pair_next_addr)

        m = self._ask("mY" + otk.hex().upper() + ap_otk.hex().upper(),
                      r"Pm y ([0-9A-F]{32})")
        if not m:
            raise RuntimeError("Stick antwortet nicht auf mY")
        ext = bytes.fromhex(m.group(1))

        def wrap(key, four, inp):
            """`inp=None`: der Stick nimmt seinen abgelegten Schluessel."""
            if not self._ask("mWK" + key.hex().upper(), r"gesetzt"):
                raise RuntimeError("Stick nimmt den Arbeitsschluessel nicht an")
            nutz = "*" if inp is None else inp.hex().upper()
            r = self._ask("mW" + sgtin.hex().upper() + four.hex().upper() + nutz,
                          r"Pm w ([0-9A-F]{32}) ([0-9A-F]{8})")
            if not r:
                raise RuntimeError("Stick antwortet nicht auf mW")
            return bytes.fromhex(r.group(1)), bytes.fromhex(r.group(2))

        # `None` heisst: den Netzwerkschluessel setzt der STICK ein — QCCU
        # kennt ihn nicht und soll ihn nicht kennen.
        otk_enc, otk_mic = wrap(ext, nonce, None)
        enc_nwk, nwk_mic = wrap(devkey, nonce_ks, otk_enc)

        acc  = bytes([0x01, 0x00, 0x8E]) + bytes.fromhex(self.own_addr) + src
        acc += bytes([0x20, 0x02]) + nonce_ks + otk_mic + ap_otk + enc_nwk
        acc += newa + (100).to_bytes(4, "big") + nwk_mic
        if len(acc) != 54:
            raise RuntimeError(f"Angebot {len(acc)} statt 54 Byte")

        self._pair_ok.clear()
        self._pair_expect = newa.hex().lower()

        # ⚠️ `As` ist das EINZIGE Kommando, auf das der Stick kein `Pm tx ok`
        # gibt (culfw-Stil: rohes Senden, kein Echo). Als "cmd" eingereiht
        # wartete der Sender 1,2 s vergeblich auf das Urteil — und in genau
        # dieser Zeit fragt das frisch angelernte Geraet schon nach der Zeit.
        # Der Beweis, dass das Angebot raus war, ist ohnehin die Bestaetigung.
        self._submit("As" + bytes([len(acc)]).hex().upper() + acc.hex().upper(), "raw")
        self.pair_last = f"Angebot gesendet, Geraet bekommt {newa.hex()}"
        self._log("##", f"ANLERNEN Angebot -> {newa.hex()}")

        got = self._pair_ok.wait(5.0)
        self._pair_expect = None
        ccu_addr = sgtin.hex()[-14:].upper()
        if not got:
            # ⚠️ NICHT alles wegwerfen. Die ausbleibende Bestaetigung heisst
            # nicht, dass das Geraet das Angebot verworfen hat — sie kann
            # schlicht verlorengegangen sein. Das Geraet ist dann angelernt,
            # funkt gesichert unter der angebotenen Adresse, und wir wuerden
            # es als „fremd" abweisen: Router-Suche, Werksreset. Deshalb
            # bleibt der Vorgang offen und wird beim ersten gesicherten
            # Lebenszeichen nachgeholt (`_nachtrag_einloesen`). Die Zentrale
            # macht dasselbe, nur noch grosszuegiger: sie traegt das Geraet
            # schon beim Anlernruf ein und behaelt es auch im Fehlerfall.
            with self.lock:
                self._nachtrag[newa.hex()] = {
                    "ccu_addr": ccu_addr, "devtype": devtype,
                    "opmode": opmode, "src": src.hex(), "fassung": fassung,
                    "bis": time.time() + NACHTRAG_FENSTER}
            self._pair_busy = False
            self.pair_last = (f"Bestätigung blieb aus — {newa.hex()} wird "
                              f"noch {int(NACHTRAG_FENSTER)} s lang "
                              f"angenommen, falls das Gerät sich meldet")
            self._log("##", f"ANLERNEN keine Bestaetigung — {newa.hex()} "
                            f"bleibt {int(NACHTRAG_FENSTER)} s offen")
            if self.verbose:
                print(f"  ! Anlernen: keine Bestätigung. Meldet sich "
                      f"{newa.hex()} trotzdem, wird nachgetragen.")
            return

        self._anlernen_eintragen(newa.hex(), ccu_addr, devtype, fassung,
                                 opmode)
        self._anlernen_rest(newa.hex(), ccu_addr, devtype, opmode, src.hex(),
                            fassung)

    # -- Abschluss des Anlernens, aus zwei Wegen erreichbar ---------------
    #
    # Weg 1: die Bestaetigung kam (`_pair_do`).
    # Weg 2: sie kam nicht, aber das Geraet funkt trotzdem unter der
    #        angebotenen Adresse (`_nachtrag_einloesen`).

    def _anlernen_eintragen(self, hmid, ccu_addr, devtype, fassung=None,
                            opmode=None):
        """Geraet fuehren und Funkadresse binden — das MUSS zuerst geschehen.

        Die erste Anfrage des Geraets (Zeit, Zustand) kommt rund eine Sekunde
        nach der Bestaetigung; wer sie als „fremden Absender" verwirft, treibt
        das Geraet in die Router-Suche (E00002), und aus der kommt es ohne
        Werksreset nicht mehr heraus — es wiederholt dann JEDE Sendung
        dreifach. Am Geraet gemessen, 17.08.2026.
        """
        # ⚠️ Der Betriebsmodus gehoert HIER hinein, nicht erst in
        # `_anlernen_rest`. Der Anlernruf ist die einzige Gelegenheit, bei der
        # ein Geraet ihn ansagt; wer ihn nur durchreicht, hat ihn nach dem
        # naechsten Neustart nicht mehr.
        self.qccu.add_device(ccu_addr, devtype, neu_angelernt=True,
                             **({"firmware": fassung, "fassung": fassung}
                                if fassung else {}),
                             **({"opmode": opmode,
                                 "opmode_quelle": "anlernruf"}
                                if opmode is not None else {}))
        self.bind(hmid, ccu_addr)
        # Der Vorgang ist erledigt — eine noch offene Vormerkung waere von
        # jetzt an nur noch eine Fussangel.
        with self.lock:
            self._nachtrag.pop(hmid, None)

    def _anlernen_rest(self, hmid, ccu_addr, devtype, opmode, src,
                       fassung=None):
        """Wegemeldung, Verdrahtung, Aufraeumen — alles nach dem Eintrag."""
        # Wegemeldung wie die echte Zentrale: rund 185 ms nach ihrer Quittung.
        # ⚠️ Diese Zahl steht NICHT im Jar — dort ist die Wegemeldung schlicht
        # die naechste serialisierte Transaktion nach der Bestaetigung. Sie
        # stammt aus den Referenzmitschnitten (184/186/186 ms in drei
        # goldenen Zyklen). Nicht 2,5 s — in der Luecke suchte das Geraet
        # bereits einen Router.
        # ⚠️ Der Betriebsmodus im ROUTE_RESPONSE ist DER DES GERAETS, nicht der
        # der Zentrale: `RouteResponse.setOperationMode()` zerlegt genau dieses
        # Byte in `acessController` (0x80), `router` (0x40), `portableDevice`
        # (0x20) und `ListenerMode` (0x0F). QCCU hatte hier `40` fest stehen —
        # den Wert der PS-2 aus dem Referenzzyklus. Damit erklaerte es jedes
        # Geraet zum Router, auch eines, das keiner ist. Gemeldet wird jetzt,
        # was das Geraet im Anlernruf selbst angesagt hat.
        time.sleep(0.15)
        self._log("##", f"ANLERNEN Betriebsmodus des Geraets 0x{opmode:02x}")
        if self.verbose and opmode != 0x40:
            print(f"  Anlernen: Betriebsmodus 0x{opmode:02x} "
                  f"(Router={'ja' if opmode & 0x40 else 'nein'}, "
                  f"ListenerMode={opmode & 0x0F})")
        self._submit("mT31f00002" + "01" + hmid + "000000"
                     + f"{opmode:02x}" + "17", "cmd")

        # Die Verknuepfungen erst spaeter — zu frueh gesendet nimmt das Geraet
        # sie nicht an (am echten Geraet gemessen).
        self._verdrahten(bytes.fromhex(hmid), devtype, opmode, fassung)
        # Und die Verknuepfung der Sender-Kanaele zur Zentrale — sonst
        # schickt ein Ereignismelder seine Ereignisse nicht (HmIP-SCI).
        try:
            self.zentralen_verknuepfen(ccu_addr, opmode=opmode,
                                       devtype=devtype, fassung=fassung)
        except Exception as ex:                          # noqa: BLE001
            self._log("##", f"ZENTRALENVERKNUEPFUNG scheiterte: {ex}")

        self.spuren_raeumen(src, ccu_addr)

        self.pair_last = f"{ccu_addr} angelernt als {hmid} (Typ {devtype})"
        self._log("##", f"ANLERNEN fertig {ccu_addr} -> {hmid}")
        if self.verbose:
            print(f"  Anlernen abgeschlossen: {ccu_addr} -> {hmid}")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            merke("ok", f"{ccu_addr} angelernt (Funkadresse {hmid})")
        frisch = getattr(self.qccu, "merke_frisch_angelernt", None)
        if frisch:
            frisch(ccu_addr)

        # ⚠️ Nur weiterzaehlen, wenn dieses Angebot noch das offene ist. Beim
        # Nachtrag kann inzwischen ein anderes Geraet drangewesen sein.
        if self.pair_next_addr == hmid:
            nxt = (int(hmid, 16) + 1) & 0xFFFFFF
            self.pair_next_addr = f"{nxt:06x}"
            if hasattr(self.qccu, "pair_next_addr"):
                self.qccu.pair_next_addr = self.pair_next_addr
                self.qccu.save_store()
            self.pair_key = None
            self.pair_until = 0.0
        self._pair_busy = False

    def _nachtrag_einloesen(self, hmid):
        """Ein Geraet nachtragen, das sich ohne Bestaetigung gemeldet hat."""
        with self.lock:
            e = self._nachtrag.pop(hmid, None)
        if not e:
            return
        if time.time() > e["bis"]:
            self._log("##", f"NACHTRAG {hmid} verfallen")
            return
        self._log("##", f"NACHTRAG {hmid} eingeloest — das Geraet funkt "
                        f"gesichert unter der angebotenen Adresse")
        if self.verbose:
            print(f"  Anlernen nachgetragen: {e['ccu_addr']} meldet sich als "
                  f"{hmid} — die Bestätigung war verlorengegangen.")
        # Eintragen SOFORT, damit der gerade laufende Frame nicht doch noch
        # als fremd durchfaellt; der Rest im eigenen Faden, weil er wartet.
        self._anlernen_eintragen(hmid, e["ccu_addr"], e["devtype"],
                                 e.get("fassung"), e.get("opmode"))
        threading.Thread(
            target=self._anlernen_rest,
            args=(hmid, e["ccu_addr"], e["devtype"], e["opmode"], e["src"],
                  e.get("fassung")),
            daemon=True).start()

    def _app_quittung(self, hmid, appseq, ergebnis):
        """Eine ANSWER des Geraets einer gesendeten appSeq zuordnen.

        Eine appSeq, auf die niemand wartet, wird stillschweigend verworfen —
        so haelt es auch `ApplicationTask.evaluateTaskResponse`.
        """
        with self.lock:
            eintrag = self._app_ack.get((hmid, appseq))
            offen = self._wartend.get(hmid) or []
            erledigt = [e for e in offen if e["appseq"] == appseq]
            self._wartend[hmid] = [e for e in offen if e["appseq"] != appseq]
            if not self._wartend[hmid]:
                self._wartend.pop(hmid, None)
            geaendert = len(offen) != len(self._wartend.get(hmid) or [])
        if geaendert:
            self._save_state()
        if eintrag is not None:
            eintrag["ergebnis"] = ergebnis
            eintrag["ev"].set()
        # Angenommen (Nutzlast 0) — was an dem Rahmen hing, jetzt tun; auch
        # dann, wenn die ANSWER erst nach dem Antwortfenster des Senders kam.
        if ergebnis == 0:
            for e in erledigt:
                self._abschluss(hmid, e)
        else:
            # Abgelehnt: der Rahmen ist entfernt (das Geraet will ihn nicht,
            # Wiederholen bringt nichts) — aber der Rest seiner Kette darf
            # nicht ohne ihn weiterlaufen.
            for e in erledigt:
                if e.get("kette"):
                    self._kette_aufgeben(hmid, e["kette"], "vom Geraet abgelehnt")

    def _kette_aufgeben(self, hmid, kid, grund):
        """Eine Konfigurationskette als GANZES verwerfen.

        ⚠️ Einzelne Glieder aufzugeben geht nicht: START, SET und COMMIT sind
        eine Sitzung am Geraet. Faellt eines aus, ist der Rest wertlos — und
        ginge er beim naechsten Lebenszeichen trotzdem hinaus, kaeme ein SET
        ohne START (das Geraet lehnt es ab). Darum: alles weg, `CONFIG_PENDING`
        zurueck und laut melden, statt den Auftrag still verschwinden zu lassen.
        """
        with self.lock:
            offen = self._wartend.get(hmid) or []
            rest = [e for e in offen if e.get("kette") != kid]
            weg = len(offen) - len(rest)
            if rest:
                self._wartend[hmid] = rest
            else:
                self._wartend.pop(hmid, None)
            noch = any(x.get("kette") for x in rest)
        if not weg:
            return
        self._save_state()
        addr = self.by_hmid.get(hmid)
        self._log("##", f"KONFIG {kid} aufgegeben ({grund}) — {weg} Rahmen verworfen")
        if addr:
            self.qccu.merke_ereignis(
                "bad", f"{self.qccu.name_of(addr, addr)}: Konfiguration nicht "
                       f"geschrieben ({grund}) — die Werte stehen weiter auf dem alten Stand")
            if not noch:
                self.qccu.set_value_internal(addr, 0, "CONFIG_PENDING", False)

    def _abschluss(self, hmid, e):
        """Was nach der Annahme eines Warterahmens zu tun ist — genau einmal."""
        danach = e.pop("danach", None) if isinstance(e, dict) else None
        if not danach:
            return
        if "master" in danach:
            a, k, w = danach["master"]
            self.qccu.master_uebernehmen(a, k, w)
            self._log("##", f"KONFIG {a}:{k} angenommen — Bestand uebernommen "
                            f"({', '.join(sorted(w))})")
            with self.lock:
                offen = any(x.get("kette") for x in (self._wartend.get(hmid) or []))
            if not offen:
                self.qccu.set_value_internal(a, 0, "CONFIG_PENDING", False)

    # Der „Kanal" der Zentrale in einer Verknuepfung — so fuehrt ihn das Jar
    # (`channel.getLinkPartner("CENTRAL_DEVICE", 63)`).
    ZENTRALE_KANAL = 0x3F

    def _zentralen_rahmen(self, hmid, kanal, appseq):
        """CREATE_LINK vom Geraetekanal zur Zentrale, wie
        `createCreateCentralDeviceLinkTransaction` ihn baut: Partner ist die
        Adresse der Zentrale (HM_ADDRESS, drei Byte ohne Typbyte), Daten A der
        Kanal 63 der Zentrale, Daten B 0, Betriebsart PERMANENT_LISTENER mit
        gesetztem Bit 7 (0x80)."""
        return (f"ms{hmid.upper()}C1{appseq:02X}{kanal:02X}01"
                f"{self.own_addr.upper()}{self.ZENTRALE_KANAL:02X}0080")

    def zentralen_verknuepfen(self, ccu_address, kanaele=None, opmode=None,
                              devtype=None, fassung=None):
        """Die Verknuepfung Geraetekanal -> Zentrale anlegen.

        ⚠️ Ohne sie meldet ein Sender-Kanal sein Ereignis NICHT als STATUS.
        Am HmIP-SCI gemessen (03.09.2026): zwei Kontaktwechsel nach dem
        Anlernen ergaben zwei REQUEST_CONFIG_UPDATE und keinen Statusrahmen.
        Die Zentrale von eq-3 legt diese Verknuepfung nicht beim Anlernen an,
        sondern sobald ein Klient den Wert beobachtet (`reportValueUsage`);
        da QCCU keine ReGa hat, die das fuer jeden Datenpunkt tut, geschieht
        es hier beim Anlernen fuer alle Kanaele mit Link-Rolle SENDER — und
        auf Zuruf fuer aeltere Geraete.

        Welche Kanaele: `kanaele`, sonst alle mit Rolle SENDER laut Tabelle.
        Ein Geraet, das nicht staendig hoert, bekommt die Rahmen ueber die
        Warteliste beim naechsten Lebenszeichen — genau dann, wenn es mit
        REQUEST_CONFIG_UPDATE danach fragt.
        Rueckgabe: Liste der Kanaele, fuer die ein Rahmen gebaut wurde.
        """
        addr = ccu_address.upper()
        hmid = None
        with self.lock:
            for h, a in self.by_hmid.items():
                if a == addr:
                    hmid = h
                    break
        if not hmid:
            self._log("##", f"ZENTRALENVERKNUEPFUNG {addr}: keine Funkadresse")
            return []
        d = (getattr(self.qccu, "devices", None) or {}).get(addr)
        if devtype is None and d is not None:
            devtype = d.devtype
        if fassung is None and d is not None:
            fassung = getattr(d, "fassung", None)
        if opmode is None and d is not None:
            opmode = getattr(d, "opmode", None)
        eintrag = None
        try:
            eintrag = self.t.fuer_geraet(devtype, fassung)
        except Exception:                                # noqa: BLE001
            pass
        if kanaele is None:
            kanaele = []
            unbekannt = []
            try:
                for idx in self.t.channels_of(devtype, eintrag):
                    rolle = self.t.rolle_of(devtype, idx, eintrag)
                    if rolle is None:
                        unbekannt.append(int(idx))
                    elif rolle == "SENDER":
                        kanaele.append(int(idx))
            except Exception as ex:                      # noqa: BLE001
                self._log("##", f"ZENTRALENVERKNUEPFUNG {addr}: Rollen nicht lesbar: {ex}")
                return []
            if unbekannt and not kanaele:
                self._log("##", f"ZENTRALENVERKNUEPFUNG {addr}: Link-Rollen unbekannt "
                                f"(Tabelle ohne kanalrollen.json) — nichts angelegt")
                merke = getattr(self.qccu, "merke_ereignis", None)
                if merke:
                    merke("warn", "Die Gerätetabellen kennen die Link-Rollen der "
                                  "Kanäle nicht — Ereignismelder melden ohne "
                                  "Verknüpfung zur Zentrale keine Ereignisse. "
                                  "Tabellen neu anlegen.")
                return []
        kanaele = [int(k) for k in kanaele]
        if not kanaele:
            self._log("##", f"ZENTRALENVERKNUEPFUNG {addr}: kein Sender-Kanal")
            return []
        with self.lock:
            appseq = (self.appseq.get(hmid, LINK_APPSEQ_START - LINK_APPSEQ_SCHRITT)
                      + LINK_APPSEQ_SCHRITT) & 0xFF
            rahmen = []
            for k in kanaele:
                rahmen.append((appseq, self._zentralen_rahmen(hmid, k, appseq)))
                appseq = (appseq + LINK_APPSEQ_SCHRITT) & 0xFF
            self.appseq[hmid] = (appseq - LINK_APPSEQ_SCHRITT) & 0xFF
        self._save_state()
        hoerer = (opmode & 0x0F) if opmode is not None else None
        if hoerer is None or hoerer in LM_NICHT_STAENDIG:
            with self.lock:
                self._wartend.setdefault(hmid, []).extend(
                    {"cmd": cmd, "appseq": seq, "versuche": 0} for seq, cmd in rahmen)
            self._save_state()
            self._log("##", f"ZENTRALENVERKNUEPFUNG {addr} Kanal "
                            f"{', '.join(map(str, kanaele))}: {len(rahmen)} Rahmen "
                            f"warten auf ein Lebenszeichen (Hoerertyp {hoerer})")
            return kanaele
        angenommen = 0
        for seq, cmd in rahmen:
            time.sleep(LINK_PAUSE)
            if self._link_senden(hmid, seq, cmd):
                angenommen += 1
        self._log("##", f"ZENTRALENVERKNUEPFUNG {addr} Kanal "
                        f"{', '.join(map(str, kanaele))}: {angenommen}/{len(rahmen)} quittiert")
        return kanaele

    # ConfigurationRequestType (Jar) fuer das Schreiben einer Liste.
    KONFIG_START = 5
    KONFIG_COMMIT = 6
    KONFIG_SET_INDEX = 8
    KONFIG_MAX_PAARE = 14           # `maxConfigurationDataLengthPerFrame`

    def _konfig_rahmen(self, hmid, kanal, appseq, art, nutzlast=""):
        """CONFIGURATION mit Antwortwunsch und Wach-Bit (Kopf C1), wie
        `createStartParameterSetting`/`createSetParameterByIndex`/
        `createCommitParameterSetting` (respReq true, stayAwake true)."""
        return (f"ms{hmid.upper()}C1{appseq:02X}{kanal:02X}{art:02X}{nutzlast}")

    def konfig_schreiben(self, ccu_address, kanal, liste, paare, werte):
        """Eine Konfigurationsliste an das Geraet schreiben — drei Rahmen als
        Kette im Wartefach: START (Partner 000000 = Geraetekonfiguration,
        Partnerkanal 0, Liste, Betriebsart 0), SET_PARAMETER_BY_INDEX in
        Portionen zu 14 Paaren, COMMIT (`createDeviceConfigurationTransaction`).
        Nach dem angenommenen COMMIT werden `werte` in den Bestand
        uebernommen. Schlafende Geraete bekommen die Kette beim naechsten
        Lebenszeichen, staendige Hoerer sofort."""
        addr = ccu_address.upper()
        hmid = None
        with self.lock:
            for h, a in self.by_hmid.items():
                if a == addr:
                    hmid = h
                    break
        if not hmid:
            self.qccu.merke_ereignis("bad", f"{addr}: kein Funkgeraet fuer das Konfigschreiben")
            return False
        d = self.qccu.devices.get(addr)
        opmode = getattr(d, "opmode", None)
        with self.lock:
            appseq = self.appseq.get(hmid, 0)
            rahmen = []
            def naechste():
                nonlocal appseq
                appseq = (appseq + LINK_APPSEQ_SCHRITT) & 0xFF
                if not appseq % 2:
                    appseq = (appseq + 1) & 0xFF
                if not appseq:
                    appseq = 1
                return appseq
            rahmen.append((naechste(), self._konfig_rahmen(
                hmid, kanal, appseq, self.KONFIG_START, f"000000{0:02X}{int(liste):02X}00")))
            paare = list(paare)
            for i in range(0, len(paare), self.KONFIG_MAX_PAARE):
                teil = paare[i:i + self.KONFIG_MAX_PAARE]
                rahmen.append((naechste(), self._konfig_rahmen(
                    hmid, kanal, appseq, self.KONFIG_SET_INDEX,
                    "".join(f"{int(ix):02X}{int(b) & 0xFF:02X}" for ix, b in teil))))
            rahmen.append((naechste(), self._konfig_rahmen(hmid, kanal, appseq, self.KONFIG_COMMIT)))
            self.appseq[hmid] = appseq
            kid = f"{addr}:{kanal}:{liste}:{rahmen[0][0]:02X}"
            eintraege = [{"cmd": cmd, "appseq": seq, "versuche": 0, "kette": kid} for seq, cmd in rahmen]
            eintraege[-1]["danach"] = {"master": [addr, int(kanal), dict(werte)]}
            self._wartend.setdefault(hmid, []).extend(eintraege)
        self._save_state()
        self.qccu.set_value_internal(addr, 0, "CONFIG_PENDING", True)
        self._log("##", f"KONFIG {addr}:{kanal} Liste {liste}: {len(paare)} Byte(s) in "
                        f"{len(rahmen)} Rahmen ({', '.join(sorted(werte))})")
        hoerer = (opmode & 0x0F) if opmode is not None else None
        if hoerer is None or hoerer in LM_NICHT_STAENDIG:
            self._log("##", f"KONFIG {addr}:{kanal} wartet auf ein Lebenszeichen (Hoerertyp {hoerer})")
            return True
        self._nachreichen(hmid)
        return True

    def _nachreichen(self, hmid):
        """Was auf ein Lebenszeichen gewartet hat, jetzt senden.

        ⚠️ Die Eintraege bleiben stehen, bis ihre ANSWER kommt. Ein Geraet,
        das nur kurz aufwacht, bekommt seinen Befehl damit beim naechsten
        Aufwachen erneut — das ist der Ersatz fuer den Wiederholungstakt, den
        die Zentrale bei staendigen Hoerern faehrt. Nach `LINK_VERSUCHE`
        vergeblichen Anlaeufen wird aufgegeben, statt ein Geraet endlos
        anzusprechen.
        """
        with self.lock:
            # ⚠️ Nur EINE Zustellung je Geraet zur Zeit. Der WRC6-A gab beim
            # Anlernen zwei Lebenszeichen kurz hintereinander; zwei Faeden
            # schickten dieselben Rahmen doppelt, und die ANSWER des Geraets
            # landete beim falschen (03.09.2026, 12:23).
            if hmid in self._zustellung:
                return
            offen = list(self._wartend.get(hmid) or [])
            uebrig = []
            if offen:
                self._zustellung.add(hmid)
            senden = []
            aufgeben = {e.get("kette") for e in offen
                        if e.get("kette") and e["versuche"] >= LINK_VERSUCHE}
            for e in offen:
                if e["versuche"] >= LINK_VERSUCHE:
                    continue
                if e.get("kette") in aufgeben:
                    continue          # Glied einer Kette, deren Kopf aufgegeben hat
                # Der Anlauf zaehlt erst beim Senden (in `lauf`) — ein Glied
                # hinter einer gerissenen Kette hat keinen Anlauf verbraucht.
                senden.append(e)
                uebrig.append(e)
            aufgegeben = len(offen) - len(uebrig)
            if uebrig:
                self._wartend[hmid] = uebrig
            else:
                self._wartend.pop(hmid, None)
        self._save_state()
        for kid in aufgeben:
            self._kette_aufgeben(hmid, kid, f"nach {LINK_VERSUCHE} Anlaeufen ohne Annahme")
        if aufgegeben:
            self._log("##", f"WARTEND {aufgegeben} Befehl(e) an {hmid} "
                            f"aufgegeben (nach {LINK_VERSUCHE} Anlaeufen)")
        if not senden:
            with self.lock:
                self._zustellung.discard(hmid)
            return
        self._log("##", f"WARTEND {len(senden)} Befehl(e) an {hmid} "
                        f"nachgereicht (Geraet ist wach)")

        # ⚠️ NACHEINANDER, mit Warten auf die ANSWER — nicht alle auf einmal.
        # Am HmIP-SCI gesehen (03.09.2026, 11:40): zwei CREATE_LINK und die
        # Quittung binnen 100 ms hinaus, vom Geraet EINE Kurzquittung und
        # keine ANSWER. So haelt es auch die Zentrale: ein Rahmen je
        # Transaktion, die naechste erst nach der Antwort. Im eigenen Faden,
        # damit der Empfang nicht steht, waehrend wir warten; die Quittung
        # auf das Lebenszeichen (mit Wach-Bit) geht dadurch VOR den Befehlen
        # hinaus — das Geraet weiss dann, dass noch etwas kommt.
        def lauf():
          try:
            self._antwort_abwarten(hmid)
            for e in senden:
                with self.lock:
                    if not any(x is e for x in (self._wartend.get(hmid) or [])):
                        continue            # inzwischen quittiert (ANSWER kam vorab)
                    e["versuche"] += 1
                self._save_state()          # der Anlauf zaehlt auch ueber einen Neustart
                gut = self._link_senden(hmid, e["appseq"], e["cmd"])
                self._log("##", f"WARTEND appSeq=0x{e['appseq']:02X} an {hmid}: "
                                + ("quittiert" if gut is True else
                                   "Kurzquittung, gilt als zugestellt" if gut else
                                   f"keine Annahme (Anlauf {e['versuche']}/{LINK_VERSUCHE})"))
                if gut:
                    # Zugestellt — nicht beim naechsten Lebenszeichen erneut.
                    with self.lock:
                        rest = [x for x in (self._wartend.get(hmid) or [])
                                if x["appseq"] != e["appseq"]]
                        if rest:
                            self._wartend[hmid] = rest
                        else:
                            self._wartend.pop(hmid, None)
                    self._save_state()
                    self._abschluss(hmid, e)      # bei „mac" noch offen, bei ANSWER schon geschehen
                elif e.get("kette"):
                    # Eine Kette (START → SET → COMMIT) bricht beim ersten
                    # Glied ab, das nicht angenommen wurde: die Reihenfolge
                    # muss stimmen, der Rest kommt beim naechsten Anlauf.
                    self._log("##", f"WARTEND Kette an {hmid} unterbrochen bei "
                                    f"appSeq=0x{e['appseq']:02X}")
                    break
          finally:
            with self.lock:
                self._zustellung.discard(hmid)
        threading.Thread(target=lauf, name=f"nachreichen-{hmid}", daemon=True).start()

    def _link_senden(self, hmid, appseq, cmd):
        """Einen Konfigurationsrahmen senden und auf die ANSWER warten.

        Das Jar wertet die ANSWER aus und wiederholt bei Ausbleiben
        (`SendFrameTask.getRetryTaskActionIfPossible`, Takt 220 ms). QCCU hat
        bisher einmal gesendet und `Pm tx ok` fuer Erfolg gehalten — das
        belegt aber nur, dass der Stick gesendet hat, nicht dass das Geraet
        es angenommen hat. Auf verlustreicher Strecke blieb das Geraet dann
        unverdrahtet und meldete sich kurz darauf wieder ab.
        """
        eintrag = {"ev": threading.Event(), "ergebnis": None}
        with self.lock:
            self._app_ack[(hmid, appseq)] = eintrag
        # ⚠️ Die Kurzquittung zaehlt. HmIP-SCI und HmIP-SMI55-A (03.09.2026)
        # beantworten einen CREATE_LINK zur Zentrale NIE mit einer ANSWER,
        # quittieren ihn aber auf MAC-Ebene — und nehmen ihn an (danach kamen
        # die Statusrahmen). Wer nur auf die ANSWER wartet, schickt denselben
        # Rahmen dreimal und haelt ihn dann fuer verloren.
        mac = threading.Event()
        self._acked[hmid] = mac
        try:
            for versuch in range(1, LINK_VERSUCHE + 1):
                eintrag["ev"].clear()
                mac.clear()
                job = self._submit(cmd, "cmd")
                self._acked_job[hmid] = job
                # Das Antwortfenster beginnt, wenn der Rahmen die Leitung
                # verlassen hat — nicht beim Einreihen. Steht davor noch ein
                # Auftrag in der Schlange, liefe es sonst leer, bevor das
                # Geraet den Rahmen ueberhaupt gesehen hat (Test 03.09.2026:
                # SET doppelt, COMMIT „ohne Antwort" vor dem Senden).
                job.written.wait(LINK_ANTWORT_ZEIT * 4)
                if not eintrag["ev"].wait(LINK_ANTWORT_ZEIT) and mac.is_set():
                    self._log("##", f"VERDRAHTUNG appSeq=0x{appseq:02X} Kurzquittung, "
                                    f"keine ANSWER — gilt als zugestellt")
                    return "mac"
                if eintrag["ev"].is_set():
                    gut, klartext = antwort_deuten(eintrag["ergebnis"])
                    if not gut:
                        self._log("##", f"VERDRAHTUNG appSeq=0x{appseq:02X} "
                                        f"abgelehnt ({klartext})")
                        return False
                    if versuch > 1:
                        self._log("##", f"VERDRAHTUNG appSeq=0x{appseq:02X} "
                                        f"angenommen im Anlauf {versuch}")
                    return True
                if versuch < LINK_VERSUCHE:
                    time.sleep(LINK_WIEDERHOLUNG)
        finally:
            with self.lock:
                self._app_ack.pop((hmid, appseq), None)
            if self._acked.get(hmid) is mac:
                self._acked.pop(hmid, None)
            self._acked_job.pop(hmid, None)
        self._log("##", f"VERDRAHTUNG appSeq=0x{appseq:02X} ohne Antwort nach "
                        f"{LINK_VERSUCHE} Anlaeufen")
        return None

    def _verdrahten(self, newa, devtype, opmode=0, fassung=None):
        """Die interne Verdrahtung des Geraets einrichten — beide Richtungen.

        Quelle ist die Geraetebeschreibung des Herstellers, nicht eine Liste
        im Code. Ein Typ ohne Eintrag bekommt nichts; das ist eine Aussage der
        Beschreibung und keine Luecke.

        ⚠️ WANN gesendet wird, haengt am Hoerertyp im Betriebsmodus. Ein
        Geraet, das dauernd hoert, bekommt die Rahmen sofort — mit
        Auswertung der ANSWER und Wiederholung. Ein Geraet, das nur kurz
        aufwacht (EVENT/CYCLIC), bekaeme davon nichts mit: es schlaeft
        waehrend unserer Pause. Fuer das wandern sie in die Warteliste und
        gehen hinaus, sobald es sich meldet — genau der
        `PendingDeviceCommandsHolder` der Zentrale, und unsere Quittung traegt
        bis dahin das Wach-Bit.
        """
        links = []
        try:
            # ⚠️ Ueber die FASSUNG aufloesen, nicht ueber den Typ allein: bei
            # mehreren Beschreibungen desselben Typs haengt die Verdrahtung am
            # Firmware-Band (HmIP-ASIR: alte Fassung keine, neue 1->2).
            links = self.t.links_of(devtype,
                                    self.t.fuer_geraet(devtype, fassung))
        except Exception as ex:                          # noqa: BLE001
            self._log("##", f"ANLERNEN Verdrahtung nicht lesbar: {ex}")

        if not links:
            # ⚠️ Die beiden Faelle auseinanderhalten. Eine alte Tabelle kennt
            # das Feld gar nicht — dann ist „keine Links" keine Auskunft,
            # sondern Unwissen, und das Geraet meldet sich hinterher wieder
            # ab. Das gehoert gesagt, nicht stillschweigend hingenommen.
            if not getattr(self.t, "links_bekannt", False):
                self._log("##", "ANLERNEN VERDRAHTUNG UNBEKANNT — alte Tabellen")
                print("  ! Die Gerätetabellen führen die interne Verdrahtung "
                      "nicht (Fassung bis 2026.8.34). Das Gerät wird angelernt, "
                      "kann sich aber danach wieder abmelden. Abhilfe: Tabellen "
                      "neu anlegen (Behälter neu starten oder `setup`).")
                merke = getattr(self.qccu, "merke_ereignis", None)
                if merke:
                    merke("warn", "Die Gerätetabellen kennen die interne "
                                  "Verdrahtung nicht — bitte neu anlegen, sonst "
                                  "meldet sich ein neues Gerät wieder ab.")
            else:
                self._log("##", f"ANLERNEN Typ {devtype} ohne interne Verdrahtung")
            return

        hmid = newa.hex()
        appseq = LINK_APPSEQ_START
        rahmen = []
        for quelle, ziel in links:
            for a, b in ((quelle, ziel), (ziel, quelle)):
                rahmen.append((appseq,
                               f"ms{hmid.upper()}C1{appseq:02X}"
                               f"{a:02X}01{hmid.upper()}"
                               f"{b:02X}0000"))
                appseq = (appseq + LINK_APPSEQ_SCHRITT) & 0xFF

        # ⚠️ Den eigenen Zaehler nachziehen. Die Verknuepfungen laufen an
        # `_next_seq` vorbei (5, 7, 9 …), und `_handle` uebernimmt die appSeq
        # AUCH aus den Quittungen des Geraets — das ist das Echo unserer
        # eigenen Nummer. Ohne das hier koennte der naechste Befehl dieselbe
        # Nummer noch einmal vergeben.
        with self.lock:
            self.appseq[hmid] = (appseq - LINK_APPSEQ_SCHRITT) & 0xFF
        self._save_state()

        hoerer = opmode & 0x0F
        if hoerer in LM_NICHT_STAENDIG:
            with self.lock:
                self._wartend[hmid] = [
                    {"cmd": cmd, "appseq": seq, "versuche": 0}
                    for seq, cmd in rahmen]
            self._save_state()
            self._log("##", f"ANLERNEN Hoerertyp {hoerer} — {len(rahmen)} "
                            f"Verknuepfungsrahmen warten auf ein Lebenszeichen")
            if self.verbose:
                print(f"  Anlernen: Gerät hört nicht ständig (Hörertyp "
                      f"{hoerer}) — die Verdrahtung geht hinaus, sobald es "
                      f"sich meldet.")
            return

        erste = True
        angenommen = 0
        for seq, cmd in rahmen:
            time.sleep(LINK_ERSTE_PAUSE if erste else LINK_PAUSE)
            erste = False
            if self._link_senden(hmid, seq, cmd):
                angenommen += 1

        self._log("##", "ANLERNEN verdrahtet: "
                        + ", ".join(f"{q}<->{z}" for q, z in links)
                        + f" ({angenommen}/{len(rahmen)} quittiert)")
        if self.verbose:
            print("  Anlernen: interne Verdrahtung "
                  + ", ".join(f"Kanal {q} <-> {z}" for q, z in links)
                  + f" — {angenommen} von {len(rahmen)} quittiert")
        if angenommen < len(rahmen):
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("warn", f"Verdrahtung nur teilweise quittiert "
                              f"({angenommen}/{len(rahmen)}) — das Gerät kann "
                              f"sich wieder abmelden.")

    # -- Anlernwuensche: gehoerte, aber unbekannte Geraete ----------------
    #
    # ⚠️ NICHT „Posteingang". In einer Zentrale von eq-3 heisst so die Liste
    # der bereits ANGELERNTEN, nur noch nicht in Betrieb genommenen Geraete.
    # Was hier steht, ist das Gegenteil: Geraete, die noch gar nicht dazu
    # gehoeren und darum bitten. Denselben Namen fuer beides zu benutzen,
    # laesst den Anwender vergeblich nach einem Geraet suchen, das er zu
    # besitzen glaubt (Dirk, 19.08.2026).
    FREMDE_GRUNDFRIST = 300.0     # untere Schranke — auch beim ersten Ruf
    FREMDE_FEHLRUFE = 3           # so viele ausgebliebene Rufe = verstummt
    FREMDE_HOECHSTFRIST = 3600.0  # obere Schranke
    FREMDE_MAX = 20

    def _merke_anlernwunsch(self, air):
        """Ein Geraet will angelernt werden — in den Posteingang.

        ⚠️ NUR das gehoert hier hinein. Ein Anlernwunsch heisst: an diesem
        Geraet wurde der Knopf gedrueckt. Jeden fremden Absender zu sammeln
        waere das Gegenteil einer Hilfe — in einem Mehrfamilienhaus stuenden
        dort die Geraete der Nachbarn, denen der Anwender keinen Aufkleber
        zuordnen kann.

        Die Anfrage traegt Kennung und Typ offen; beides ist ohne Schluessel
        lesbar und macht den Eintrag erst brauchbar."""
        try:
            hmid = air[3:6].hex()
            sgtin = air[10:22].hex().upper()
            devtype = int.from_bytes(air[24:28], "big")
            # Der Betriebsmodus liegt offen daneben (siehe `_pair_do`). Er ist
            # hier festzuhalten, weil er sonst nirgends stehenbleibt: der
            # Anlernruf ist die EINZIGE Gelegenheit, an der ein Geraet ihn
            # ansagt — danach ist er nur noch aus dem Verhalten zu erraten.
            opmode = air[31]
        except Exception:                            # noqa: BLE001
            return
        jetzt = time.time()
        with self.lock:
            e = self._fremde.get(hmid)
            if e is None:
                if len(self._fremde) >= self.FREMDE_MAX:
                    aeltester = min(self._fremde, key=lambda k: self._fremde[k]["zuletzt"])
                    del self._fremde[aeltester]
                self._fremde[hmid] = {"sgtin": sgtin, "devtype": devtype,
                                      "opmode": opmode,
                                      "zuerst": jetzt, "zuletzt": jetzt, "anzahl": 1}
                neu = True
            else:
                e.update(sgtin=sgtin, devtype=devtype, opmode=opmode, zuletzt=jetzt)
                e["anzahl"] += 1
                neu = False
        if neu:
            self._log("<<", f"Anlernwunsch von {hmid} (Typ {devtype}, "
                            f"Betriebsmodus 0x{opmode:02x}) — Posteingang")
            if self.verbose:
                print(f"  Anlernwunsch: {hmid}, Typ {devtype}, "
                      f"Betriebsmodus 0x{opmode:02x} "
                      f"(Router={'ja' if opmode & 0x40 else 'nein'}, "
                      f"ListenerMode={opmode & 0x0F})")

    # -- Verwaiste Geraete: funken mit unserem Schluessel, gehoeren nicht mehr
    #
    # ⚠️ Der Fall, der diesen Code ausgeloest hat (19.08.2026): eine PS-2 wurde
    # ausgeschlossen, quittierte den Ausschluss auf Anwendungsebene
    # (NM_EXCLUDE_READY) — und funkte danach weiter unter ihrer alten
    # Funkadresse mit unserem Netzschluessel. Sie war also NICHT im
    # Werkszustand und liess sich folglich auch nicht neu anlernen: ein
    # angelerntes Geraet sendet keinen Anlernruf. In der Oberflaeche war davon
    # nichts zu sehen — die Frames wurden still verworfen, und der Anwender
    # drueckte vergeblich die Taste. Die Auskunft war da, wir haben sie
    # weggeworfen.
    VERWAIST_GRUNDFRIST = 1800.0     # der Herzschlag kommt alle ~10 min
    VERWAIST_FEHLRUFE = 3
    VERWAIST_HOECHSTFRIST = 21600.0
    VERWAIST_MELDEPAUSE = 3600.0     # nicht oefter als stuendlich melden
    VERWAIST_MAX = 20

    def _pruefe_verwaist(self, hmid, ziel=""):
        """Ein entschluesselbarer Frame von jemandem, den wir nicht fuehren.

        ⚠️ Gemeldet wird NUR, was an UNS gerichtet ist (`ziel == own_addr`).
        Ein Geraet, das noch glaubt zu uns zu gehoeren, schickt seine
        Meldungen an die Zentralenadresse — das ist das Signal. Rundrufe
        genuegen NICHT: am 19.08.2026 stand nach einer Viertelstunde die
        Funkadresse einer ZWEITEN Zentrale aus dem Labor in der Liste, mit dem
        Rat „Werksreset am Geraet" — falsch und irrefuehrend. Warum deren
        Rundruf sich hier entschluesseln liess, ist offen; die Lehre ist
        unabhaengig davon: fremder Rundruf ist kein Beleg dafuer, dass jemand
        uns fuer seine Zentrale haelt.
        """
        jetzt = time.time()
        if not ziel or ziel != (self.own_addr or ""):
            return
        with self.lock:
            if hmid in self.by_hmid or hmid == (self.own_addr or ""):
                return
            # Waehrend eines Anlernvorgangs ist genau das der Normalfall: das
            # Geraet funkt schon verschluesselt, steht aber noch nicht im
            # Bestand. Kein Grund zur Meldung.
            if jetzt < self.pair_until or hmid == (self._pair_expect or ""):
                return
            e = self._verwaist.get(hmid)
            if e is None:
                if len(self._verwaist) >= self.VERWAIST_MAX:
                    aeltester = min(self._verwaist,
                                    key=lambda k: self._verwaist[k]["zuletzt"])
                    del self._verwaist[aeltester]
                e = {"zuerst": jetzt, "zuletzt": jetzt, "anzahl": 1,
                     "gemeldet": 0.0}
                self._verwaist[hmid] = e
            else:
                e["zuletzt"] = jetzt
                e["anzahl"] += 1
            faellig = jetzt - e["gemeldet"] > self.VERWAIST_MELDEPAUSE
            if faellig:
                e["gemeldet"] = jetzt
        if not faellig:
            return
        self._log("<<", f"{hmid} funkt mit unserem Netzschluessel, "
                        f"ist aber nicht angelernt")
        merke = getattr(self.qccu, "merke_ereignis", None)
        if merke:
            # ⚠️ KEINE Ursachenbehauptung: das Geraet kann ausgeschlossen
            # worden sein, es kann aber auch aus einem verworfenen Bestand
            # stammen („Geraete verwerfen und neu beginnen"). Belegt ist nur
            # der Zustand — und was daraus folgt.
            merke("warn", f"{hmid} funkt mit unserem Netzschluessel, ist hier "
                          f"aber nicht angelernt. Es ist damit NICHT im "
                          f"Werkszustand und laesst sich so auch nicht "
                          f"anlernen — ein Werksreset am Geraet hilft.")

    def verwaiste_aufraeumen(self):
        """Wer verstummt, faellt heraus — wie bei den Anlernwuenschen."""
        jetzt = time.time()
        weg = []
        with self.lock:
            for hmid, e in list(self._verwaist.items()):
                anzahl, spanne = e["anzahl"], e["zuletzt"] - e["zuerst"]
                if anzahl < 2 or spanne <= 0:
                    frist = self.VERWAIST_GRUNDFRIST
                else:
                    frist = max(self.VERWAIST_GRUNDFRIST,
                                min(self.VERWAIST_HOECHSTFRIST,
                                    self.VERWAIST_FEHLRUFE * spanne / (anzahl - 1)))
                if jetzt - e["zuletzt"] > frist:
                    weg.append(hmid)
                    del self._verwaist[hmid]
        for hmid in weg:
            self._log("<<", f"{hmid} funkt nicht mehr — nicht mehr verwaist gemeldet")
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("ok", f"{hmid} funkt nicht mehr mit unserem "
                            f"Netzschluessel — vermutlich zurueckgesetzt")
        return len(weg)

    def verwaiste_liste(self):
        """Was die Oberflaeche zeigt: wer funkt, ohne dazuzugehoeren."""
        self.verwaiste_aufraeumen()
        jetzt = time.time()
        with self.lock:
            eintraege = sorted(self._verwaist.items(),
                               key=lambda kv: kv[1]["zuletzt"], reverse=True)
            eintraege = [(k, dict(v)) for k, v in eintraege]
        return [{"hmid": h, "vor_sek": max(0.0, jetzt - e["zuletzt"]),
                 "anzahl": e["anzahl"], "seit_sek": max(0.0, jetzt - e["zuerst"])}
                for h, e in eintraege]

    def spuren_raeumen(self, ruf_adresse, kennung):
        """Nach dem Anlernen aufraeumen, was das Geraet hinterlassen hat.

        ⚠️ Zwei Spuren bleiben sonst liegen und widersprechen dem, was gerade
        geschehen ist (beides am 19.08.2026 am Aufbau gesehen):

        1. **Der Anlernwunsch.** Das Geraet rief unter einer Ruf-Adresse; nach
           dem Anlernen heisst es anders. Blieb der Eintrag stehen, stand
           dasselbe Geraet gleichzeitig als angelernt UND als „will angelernt
           werden" da — mit einem Hinweis, der ins Leere zeigt. Dirks Wort
           dafuer: „Paradox?"
        2. **Die alte Funkadresse.** Wurde dieselbe Kennung vorher
           ausgeschlossen, funkte sie unter der alten Adresse vielleicht noch
           und stand deshalb unter „funkt noch, gehoert nicht dazu". Nach dem
           Werksreset ist diese Adresse Geschichte.
        """
        with self.lock:
            self._fremde.pop((ruf_adresse or "").lower(), None)
            alte = self._ehemals.pop((kennung or "").upper(), None)
            if alte:
                self._verwaist.pop(alte, None)

    def _fremde_frist(self, e):
        """Wie lange ein Anlernruf nachhallt — aus seiner eigenen Kadenz.

        ⚠️ Ein Anlernwunsch ist ein WIEDERHOLTER Ruf, kein Zustand. Wer den
        Knopf gedrueckt hat und aufgibt — oder sich anderswo anlernt —, hoert
        auf zu rufen. Bleibt der Eintrag trotzdem stehen, zeigt die Liste
        Geraete an, die niemand mehr anbietet, und der Anwender wartet auf
        etwas, das nicht mehr kommt (Dirk, 19.08.2026).

        Wie lange „ausgeblieben" dauert, misst der Eintrag selbst: der
        mittlere Abstand seiner bisherigen Rufe, mal FREMDE_FEHLRUFE. Ein
        langsam rufendes Geraet bekommt dadurch von allein mehr Zeit als ein
        schnelles — ohne dass wir eine Ruf-Kadenz behaupten muessten, die wir
        nicht gemessen haben. Beim ersten Ruf gibt es keinen Abstand; dann
        gilt die Grundfrist.
        """
        anzahl = e.get("anzahl", 1)
        spanne = e["zuletzt"] - e["zuerst"]
        if anzahl < 2 or spanne <= 0:
            return self.FREMDE_GRUNDFRIST
        abstand = spanne / (anzahl - 1)
        return max(self.FREMDE_GRUNDFRIST,
                   min(self.FREMDE_HOECHSTFRIST, self.FREMDE_FEHLRUFE * abstand))

    def fremde_aufraeumen(self):
        """Verstummte Anlernrufe wegwerfen — und sagen, dass sie weg sind.

        Frueher wurden zu alte Eintraege beim Anzeigen nur uebersprungen: sie
        blieben im Speicher, zaehlten gegen FREMDE_MAX und verdraengten
        aktuelle Rufe. Jetzt werden sie wirklich entfernt, und das Wegfallen
        steht unter „Zuletzt geschehen" — sonst ist die Liste ohne Erklaerung
        leer, und das sieht aus wie ein Fehler.
        """
        jetzt = time.time()
        weg = []
        with self.lock:
            for hmid, e in list(self._fremde.items()):
                if jetzt - e["zuletzt"] > self._fremde_frist(e):
                    weg.append((hmid, e))
                    del self._fremde[hmid]
        for hmid, e in weg:
            typ = self._typname(e["devtype"])
            self._log("<<", f"Anlernwunsch {hmid} verstummt — aus der Liste")
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("info", f"{typ or 'Geraet'} {hmid} ruft nicht mehr — "
                              f"aus den Anlernwuenschen entfernt")
        return len(weg)

    def _typname(self, devtype):
        """Der Name aus dem Katalog, wenn er dort steht — sonst nichts.

        Erfunden wird kein Typname; ein leerer ist ehrlicher als ein falscher.
        """
        try:
            return self.t.label_of(devtype) or ""
        except Exception:                            # noqa: BLE001
            return ""

    def anlernwuensche_liste(self):
        """Wer gerade angelernt werden WILL — der Ruf, nicht der Bestand.

        ⚠️ Der Name jedes Eintrags traegt die Handlungsanweisung. Eine
        Zentrale von eq-3 kann ein HmIP-Geraet ohne Zutun aufnehmen, weil sie
        an dessen Schluessel kommt; wir koennen das nicht (nachgewiesen am
        12.08.2026 — der Riegel ist der Hauptschluessel im Silizium). Ohne
        Schluessel bleibt es beim Zusehen, und genau das soll der Anwender
        lesen, anstatt vergeblich auf ein Geraet zu warten.

        Der Hinweis nennt BEIDE Wege, den Schluessel beizubringen — die
        QCCU-Oberflaeche und das Feld `key` am Anlern-Aufruf. Nur einen zu
        nennen hiesse, den anderen zu verschweigen; wer aus Home Assistant
        heraus arbeitet, wuerde in eine Oberflaeche geschickt, die er nicht
        braucht.

        ⚠️ Die Liste lebt im Arbeitsspeicher: ein Neustart der Zentrale leert
        sie. Das ist beabsichtigt — ein Anlernwunsch ist eine Momentaufnahme
        („hier wurde gerade der Knopf gedrueckt"), keine Bestandsliste. Wer
        verstummt, faellt heraus (`fremde_aufraeumen`).

        Zwei Namensfamilien in einem Eintrag, mit Absicht: `id/address/name/
        type/interface` ist das, was die Haussteuerung liest (Feldnamen einer
        eq-3-Zentrale, nicht unsere Wahl); `hmid/label/devtype/hinweis/
        vor_sek/anzahl` ist das, was die eigene Oberflaeche zeigt.
        """
        self.fremde_aufraeumen()
        jetzt = time.time()
        hat_schluessel = bool(self.pair_key)
        # ⚠️ Der Text sagt, was ZU TUN ist — er behauptet nicht, dass etwas
        # fehlt. „Schluessel vom Aufkleber fehlt" las sich wie ein Vorwurf und
        # stand obendrein noch da, wenn gerade erfolgreich angelernt worden war
        # (der Schluessel wird nach dem Anlernen verworfen): ein Geraet stand
        # als angelernt UND als „Schluessel fehlt" in derselben Liste
        # (Dirk, 19.08.2026: „Paradox?"). Das Anlernen selbst laeuft in der
        # QCCU-Oberflaeche — nicht aus Bequemlichkeit, sondern weil ein
        # HmIP-Geraet ohne seinen Aufkleber-Schluessel nicht anlernbar ist und
        # die Haussteuerung kein Feld dafuer hat.
        hinweis = ("will angelernt werden — Schluessel liegt vor, "
                   "Anlernfenster in QCCU oeffnen" if hat_schluessel
                   else "will angelernt werden — dafuer den 26-stelligen "
                        "Schluessel vom Aufkleber in der QCCU-Oberflaeche "
                        "eintragen und dort anlernen")
        out = []
        with self.lock:
            eintraege = sorted(self._fremde.items(),
                               key=lambda kv: kv[1]["zuletzt"], reverse=True)
            eintraege = [(k, dict(v)) for k, v in eintraege]
        for hmid, e in eintraege:
            typ = self._typname(e["devtype"])
            out.append({
                "id": hmid,
                "address": hmid.upper(),
                "name": f"{typ or 'Unbekanntes Geraet'} {hmid} — {hinweis}",
                "type": typ,
                "interface": getattr(self.qccu, "interface_name", "HmIP-RF"),
                # ab hier: nur fuer die eigene Oberflaeche
                "hmid": hmid,
                "label": typ,
                "devtype": e["devtype"],
                "hinweis": hinweis,
                "vor_sek": max(0.0, jetzt - e["zuletzt"]),
                "anzahl": e.get("anzahl", 1),
                # Betriebsmodus, wie das Geraet ihn selbst ansagt — Router-Bit
                # 0x40, ListenerMode in 0x0F. Steht nur hier, weil er nur im
                # Anlernruf ueberhaupt vorkommt.
                "opmode": e.get("opmode"),
            })
        return out

    def on_install(self, on, seconds):
        """Anlernbetrieb ueber die Zentralen-Schnittstelle (`setInstallMode`)."""
        if not on:
            self.stop_pairing()
            if self.verbose:
                print("  Anlernbetrieb zu")
            return
        if self.pair_key:
            self.pair_until = time.time() + float(seconds)
            self.pair_last = f"Fenster offen ({int(seconds)} s) ueber setInstallMode"
        else:
            self.pair_last = ("Anlernbetrieb angefordert, aber kein Schluessel "
                              "hinterlegt — Aufkleber oder LocalKey im "
                              "Webfrontend eintragen")
            if self.verbose:
                print(f"  ! {self.pair_last}")
