/*
 * Holt die Parameter-Beschreibungen JE KANALTYP aus dem HMIPServer-Archiv —
 * in der Form, die eine Zentrale ueber getParamsetDescription ausliefert.
 *
 * WAS DIESE FASSUNG GEGENUEBER DER ERSTEN GEWINNT
 * -----------------------------------------------
 * Die erste Fassung las die Felder des Parameter-Objekts selbst aus und kam auf
 * TYPE/MIN/MAX/DEFAULT/OPERATIONS. Drei Felder, die eine Zentrale ausliefert,
 * fehlten vollstaendig — UNIT, CONTROL und VALUE_LIST. Sie entstehen erst in
 * der RPC-Schicht:
 *
 *   VALUE_LIST  steckt im TypeConverter des Parameters (StringEnumToInteger
 *               traegt die Namensliste) — im Objekt vorhanden, nur nicht als
 *               eigenes Feld.
 *   UNIT        stehen NICHT im Archiv, sondern in der Datei
 *   CONTROL     /opt/HmIP/legacy-parameter-definition.config des installierten
 *               eq-3-Pakets, adressiert als <KANALTYP>.<PARAMETER>.Unit
 *               beziehungsweise .Control.
 *
 * Statt das nachzubauen ruft dieses Programm den Uebersetzer der RPC-Schicht
 * selbst auf: DeviceUtil.addDesriptionOfParameter (Schreibweise wie im
 * Original). Sein DRITTES Argument ist der KANALTYP — steht dort der
 * Parametername, bleiben UNIT und CONTROL leer, weil der Nachschlagbegriff
 * nicht passt. Genau daran sind die Felder vorher verlorengegangen.
 *
 * Zusaetzlich wird je Paramset EINE ParamsetDescription gefuellt statt je
 * Parameter eine eigene. Nur so greift DeviceUtil.addSpecialParameter, das
 * dem MAINTENANCE-Kanal INSTALL_TEST und UPDATE_PENDING anhaengt — zwei
 * Parameter, die in keiner Definition stehen und bisher aus einem
 * on-air-Mitschnitt nachgetragen werden mussten.
 *
 * NICHT GELOEST: die Gruppen ROUTER_FILTER_ und ROUTER_STATIC_ROUTE_ in
 * MAINTENANCE/MASTER (569 Parameter an der Vergleichszentrale) entstehen auf
 * einem dritten Weg, der einen konkreten DeviceType braucht. Wo sie fehlen,
 * fehlen sie sichtbar — geraten wird nichts.
 *
 * ⚠️ REIHENFOLGE: Kanaltypen VOR den Parameterfabriken laden — der Server
 * meldet sonst „Wrong initialization order".
 *
 * Ausgegeben werden ausschliesslich STRUKTURANGABEN, kein fremder
 * Programmtext. Die Definitionsdatei bleibt eq-3-Material und wird zur
 * Laufzeit aus dem installierten Paket gelesen, nicht mitgeliefert.
 *
 * Aufruf:
 *   java -cp HMIPServer.jar:. DumpParamsRpc <ausgabe.json> [<lookup.json>] \
 *        [<legacy-parameter-definition.config>]
 */
import java.io.*;
import java.lang.reflect.*;
import java.util.*;

public class DumpParamsRpc {

    static final String PKG = "de.eq3.cbcs.devicedescription.channelspecification.";
    static final String RPC = "de.eq3.cbcs.legacy.bidcos.rpc.";

    /* Paramset-Name -> (Feld im ChannelTypeReader, Fabrik, Fabrikmethode) */
    static final String[][] SETS = {
        {"VALUES", "channelTypeDefinitionsState",
         PKG + "StateParameterFactory", "getStateParameterList"},
        {"MASTER", "channelTypeDefinitionsConfiguration",
         PKG + "ConfigurationParameterFactory", "getConfigurationParameterList"},
        {"LINK", "channelTypeDefinitionsLinkConfiguration",
         PKG + "ConfigurationParameterFactory", "getConfigurationParameterList"},
    };

    static Method add, addSpecial;
    static Constructor<?> psdCtor, rsCtor;

    public static void main(String[] args) throws Exception {
        String outPath    = args.length > 0 ? args[0] : "paramsets_from_jar.json";
        String lookupPath = args.length > 1 ? args[1] : null;
        String defsPath   = args.length > 2 ? args[2] : "legacy-parameter-definition.config";

        /* 1) Kanaltypen zuerst — der Server besteht darauf. */
        Class<?> ctrCls = Class.forName(PKG + "ChannelTypeReader");
        Object ctr = ctrCls.getDeclaredConstructor().newInstance();
        ctrCls.getMethod("loadChannelTypeDefinitions").invoke(ctr);
        System.err.println("  Kanaltypen geladen");

        /* 2) Uebersetzer scharfstellen. Fehlt die Definitionsdatei, liefert er
         *    dieselben Felder wie die erste Fassung — nur ohne UNIT und
         *    CONTROL. Das wird gemeldet, nicht stillschweigend hingenommen. */
        Class<?> du = Class.forName(RPC + "internal.DeviceUtil");
        File defs = new File(defsPath);
        if (defs.isFile()) {
            Properties props = new Properties();
            try (InputStream in = new FileInputStream(defs)) { props.load(in); }
            du.getMethod("setLegacyParameters", Properties.class).invoke(null, props);
            System.err.println("  Definitionen: " + props.size() + " aus " + defsPath);
        } else {
            System.err.println("  ! KEINE Definitionsdatei (" + defsPath
                               + ") — UNIT und CONTROL bleiben leer");
        }

        Class<?> psdCls = Class.forName(RPC + "objects.ParamsetDescription");
        Class<?> rsCls  = Class.forName("de.eq3.cbcs.legacy.communication.rpc.RpcStruct");
        Class<?> apCls  = Class.forName("de.eq3.cbcs.devicedescription.AbstractParameter");
        add        = du.getMethod("addDesriptionOfParameter", psdCls, apCls, String.class);
        addSpecial = du.getMethod("addSpecialParameter", psdCls);
        psdCtor    = psdCls.getDeclaredConstructor(rsCls);
        rsCtor     = rsCls.getDeclaredConstructor();

        /* 3) Die Fabriken einmal anstossen, damit ihre Karten stehen. */
        Map<String, Map<?, ?>> factoryMaps = new LinkedHashMap<>();
        for (String[] s : SETS) {
            if (factoryMaps.containsKey(s[2])) continue;
            try {
                Class<?> fc = Class.forName(s[2]);
                Method m = findNoArgStatic(fc, s[3]);
                m.setAccessible(true);
                factoryMaps.put(s[2], (Map<?, ?>) m.invoke(null));
                System.err.println("  " + s[2].substring(PKG.length()) + ": "
                                   + factoryMaps.get(s[2]).size());
            } catch (Throwable t) {
                System.err.println("  ! " + s[2] + ": " + t);
            }
        }

        /* 4) Sammeln: Kanaltyp -> Paramset -> Parameterobjekte, IN DIESER
         *    Gruppierung, damit je Paramset eine ParamsetDescription entsteht. */
        Map<String, Map<String, List<Object>>> plan = new TreeMap<>();
        Map<String, String> reinerName = new HashMap<>();

        for (String[] s : SETS) {
            Map<?, ?> defsMap = (Map<?, ?>) field(ctr, s[1]);
            Map<?, ?> params  = factoryMaps.get(s[2]);
            if (defsMap == null || params == null) continue;
            int hits = 0, miss = 0;

            for (Map.Entry<?, ?> e : defsMap.entrySet()) {
                String chName = str(field(e.getKey(), "channelTypeName"));
                Object chVer  = field(e.getKey(), "channelTypeVersion");
                if (chName == null) chName = str(field(e.getKey(), "name"));
                if (chName == null) continue;
                String chType = chVer == null ? chName : chName + "/v" + chVer;
                reinerName.put(chType, chName);
                Object list = e.getValue();
                if (!(list instanceof Collection)) continue;

                for (Object pk : (Collection<?>) list) {
                    Object p = params.get(pk);
                    if (p == null) { miss++; continue; }
                    hits++;
                    plan.computeIfAbsent(chType, k -> new TreeMap<>())
                        .computeIfAbsent(s[0], k -> new ArrayList<>())
                        .add(p);
                }
            }
            System.err.println("  " + s[0] + ": " + hits + " aufgeloest, " + miss + " ohne Definition");
        }

        /* 5) Uebersetzen — je Paramset EIN Durchgang. */
        Map<String, Map<String, Map<String, Object>>> result = new TreeMap<>();
        int withUnit = 0, withCtrl = 0, withList = 0, special = 0;

        for (Map.Entry<String, Map<String, List<Object>>> ch : plan.entrySet()) {
            String chType = ch.getKey();
            /* Der REINE Kanaltypname ist der Nachschlagbegriff der
             * Definitionsdatei — die Version gehoert nicht hinein. */
            String chName = reinerName.get(chType);

            for (Map.Entry<String, List<Object>> se : ch.getValue().entrySet()) {
                String psName = se.getKey();
                Object psd;
                try {
                    psd = psdCtor.newInstance(rsCtor.newInstance());
                } catch (Throwable t) { continue; }

                for (Object p : se.getValue()) {
                    try { add.invoke(null, psd, p, chName); } catch (Throwable ignored) { }
                }

                /* Die Sonderparameter des Wartungskanals stehen in keiner
                 * Definition; die RPC-Schicht haengt sie an. Gegen eine echte
                 * Zentrale geprueft: sie erscheinen in MAINTENANCE/VALUES. */
                if (psName.equals("VALUES") && chName.startsWith("MAINTENANCE")) {
                    int before = ((Map<?, ?>) psd).size();
                    try { addSpecial.invoke(null, psd); } catch (Throwable ignored) { }
                    special += ((Map<?, ?>) psd).size() - before;
                }

                Map<String, Object> out = new TreeMap<>();
                for (Map.Entry<?, ?> pe : ((Map<?, ?>) psd).entrySet()) {
                    if (!(pe.getValue() instanceof Map)) continue;
                    Map<String, Object> pd = new TreeMap<>(cast(pe.getValue()));
                    pd.remove("ID");                    /* steht schon im Schluessel */
                    if (pd.containsKey("UNIT"))       withUnit++;
                    if (pd.containsKey("CONTROL"))    withCtrl++;
                    if (pd.containsKey("VALUE_LIST")) withList++;
                    out.put(String.valueOf(pe.getKey()), pd);
                }
                /* Adresse und Umrechner der Konfigurationsparameter — fuer den
                 * Schreibweg (START/SET_PARAMETER_BY_INDEX/COMMIT). Die
                 * Zentrale setzt aus ihnen die Listenbytes zusammen
                 * (`getConfigurationDataOfParameters`, `shiftLogicalToPhysical`).
                 * Nie an Klienten: `getParamsetDescription` streicht sie. */
                for (Object p : se.getValue()) {
                    String pn = str(field(p, "channelParameter"));
                    Object ln = field(p, "listNumber");
                    if (pn == null || ln == null) continue;
                    Object vorhanden = out.get(pn);
                    if (!(vorhanden instanceof Map)) continue;
                    Map<String, Object> pd = cast(vorhanden);
                    Map<String, Object> adr = new TreeMap<>();
                    adr.put("LISTE", ln);
                    adr.put("BYTE", field(p, "indexByte"));
                    adr.put("BIT", field(p, "indexBit"));
                    adr.put("LAENGE_BYTE", field(p, "lengthByte"));
                    adr.put("LAENGE_BIT", field(p, "lengthBit"));
                    pd.put("ADRESSE", adr);
                    Object conv = field(p, "typeConverter");
                    if (conv != null) pd.put("UMRECHNER", umrechner(conv));
                }
                result.computeIfAbsent(chType, k -> new TreeMap<>()).put(psName, out);
            }
        }
        System.err.println("  davon mit UNIT " + withUnit + ", CONTROL " + withCtrl
                           + ", VALUE_LIST " + withList
                           + "; Sonderparameter ergaenzt: " + special);

        write(outPath, renderChannels(result));
        System.err.println("  geschrieben: " + outPath + " (" + result.size() + " Kanaltypen)");

        /* 6) Flache Aufloesungstabelle (Name@subtype) fuer Parameter, die nur
         *    in den device_*.xml stehen. Ohne Kanaltyp gibt es hier kein
         *    UNIT/CONTROL — das ergaenzt die Zusammenbau-Stufe. */
        if (lookupPath != null) {
            StringBuilder flat = new StringBuilder("{\n");
            boolean f = true;
            for (Map.Entry<String, Map<?, ?>> fm : factoryMaps.entrySet()) {
                for (Map.Entry<?, ?> e : fm.getValue().entrySet()) {
                    String n   = str(call(e.getKey(), "getParameterID"));
                    String sub = str(call(e.getKey(), "getParameterSubtypeID"));
                    if (n == null || e.getValue() == null) continue;
                    String key = n + "@" + (sub == null || sub.isEmpty() ? "default" : sub);
                    if (!f) flat.append(",\n");
                    f = false;
                    flat.append("  ").append(q(key)).append(": ")
                        .append(renderParam(describeSingle(e.getValue(), n)));
                }
            }
            write(lookupPath, flat.append("\n}\n").toString());
            System.err.println("  Aufloesungstabelle: " + lookupPath);
        }
    }

    /* Einzelner Parameter ohne Kanaltyp-Kontext (fuer die flache Tabelle). */
    static Map<String, Object> describeSingle(Object p, String paramName) {
        try {
            Object psd = psdCtor.newInstance(rsCtor.newInstance());
            add.invoke(null, psd, p, paramName);
            for (Object v : ((Map<?, ?>) psd).values()) {
                if (v instanceof Map) {
                    Map<String, Object> pd = new TreeMap<>(cast(v));
                    pd.remove("ID");
                    if (pd.get("TYPE") != null) return pd;
                }
            }
        } catch (Throwable ignored) { }
        // ⚠️ RUECKFALL auf die Felder des Objekts selbst.
        //
        // Ohne Kanaltyp scheitert der Uebersetzer bei manchen Parametern ganz
        // (beobachtet an ALARM_MODE_*: Aufzaehlungen, deren Werteliste er nur
        // im Kanalzusammenhang bilden kann). Herausgekommen waere ein Eintrag
        // OHNE `TYPE` — und den liest die Gegenstelle ungeprueft, verwirft
        // daran das GANZE Geraet und sagt nicht warum.
        //
        // Die erste Fassung dieses Programms las immer direkt aus dem Objekt
        // und hatte deshalb wenigstens Typ und Grenzen. Genau darauf wird hier
        // zurueckgefallen: lieber die magere, aber richtige Auskunft als eine
        // luecken hafte, die alles mitreisst.
        return direktAusDemObjekt(p);
    }

    /** Typ, Zugriffsrechte, Vorgabe und Grenzen unmittelbar aus dem Parameter. */
    static Map<String, Object> direktAusDemObjekt(Object p) {
        Map<String, Object> pd = new TreeMap<>();
        String typ = str(field(p, "logicalType"));
        if (typ == null) return pd;
        pd.put("TYPE", typ);
        pd.put("OPERATIONS", opsWert(field(p, "ioOperations")));
        Object def = field(p, "defaultValue");
        Object min = field(p, "minValue");
        Object max = field(p, "maxValue");
        if (def != null) pd.put("DEFAULT", def);
        if (min != null) pd.put("MIN", min);
        if (max != null) pd.put("MAX", max);
        return pd;
    }

    /** Der Typumwandler eines Parameters als Tabelle: Klasse und die Zahlen,
     *  die seine Rechnung bestimmen (Faktor/Offset, Wahr/Falsch-Bytes,
     *  Aufzaehlungswerte, Bitzahl). Was eine Klasse nicht hat, fehlt. */
    static Map<String, Object> umrechner(Object conv) {
        Map<String, Object> u = new TreeMap<>();
        u.put("KLASSE", conv.getClass().getSimpleName());
        Object ps = field(conv, "paramsSet");
        if (!(ps instanceof Boolean) || (Boolean) ps) {
            Object f = field(conv, "factor"); if (f != null) u.put("FAKTOR", f);
            Object o = field(conv, "offset"); if (o != null) u.put("OFFSET", o);
        }
        Object tv = field(conv, "trueValue");  if (tv instanceof byte[]) u.put("WAHR", bytes((byte[]) tv));
        Object fv = field(conv, "falseValue"); if (fv instanceof byte[]) u.put("FALSCH", bytes((byte[]) fv));
        Object bm = field(conv, "bitmask");    if (bm instanceof Byte) u.put("MASKE", ((Byte) bm) & 0xFF);
        Object es = field(conv, "enumStrings"); if (es instanceof String[]) u.put("WERTE", Arrays.asList((String[]) es));
        Object ev = field(conv, "enumValues");  if (ev instanceof int[]) { List<Object> l = new ArrayList<>(); for (int x : (int[]) ev) l.add(x); u.put("ZAHLEN", l); }
        Object nb = field(conv, "numberOfBits"); if (nb != null) u.put("BITS", nb);
        return u;
    }

    static List<Object> bytes(byte[] b) {
        List<Object> l = new ArrayList<>();
        for (byte x : b) l.add(x & 0xFF);
        return l;
    }

    /** IOOperations traegt die Bits READ=1, WRITE=2, EVENT=4. */
    static int opsWert(Object ops) {
        if (ops == null) return 0;
        Object v = field(ops, "operations");
        if (v instanceof Number) return ((Number) v).intValue() & 0xFF;
        int n = 0;
        if (Boolean.TRUE.equals(call(ops, "isReadable"))) n |= 1;
        if (Boolean.TRUE.equals(call(ops, "isWritable"))) n |= 2;
        if (Boolean.TRUE.equals(call(ops, "isEventable"))) n |= 4;
        return n;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> cast(Object o) { return (Map<String, Object>) o; }

    static String renderChannels(Map<String, Map<String, Map<String, Object>>> r) {
        StringBuilder out = new StringBuilder("{\n");
        boolean firstCh = true;
        for (Map.Entry<String, Map<String, Map<String, Object>>> ch : r.entrySet()) {
            if (!firstCh) out.append(",\n");
            firstCh = false;
            out.append("  ").append(q(ch.getKey())).append(": {\n");
            boolean firstSet = true;
            for (Map.Entry<String, Map<String, Object>> se : ch.getValue().entrySet()) {
                if (!firstSet) out.append(",\n");
                firstSet = false;
                out.append("    ").append(q(se.getKey())).append(": {\n");
                List<String> lines = new ArrayList<>();
                for (Map.Entry<String, Object> pe : se.getValue().entrySet())
                    lines.add("      " + q(pe.getKey()) + ": " + renderParam(cast(pe.getValue())));
                out.append(String.join(",\n", lines)).append("\n    }");
            }
            out.append("\n  }");
        }
        return out.append("\n}\n").toString();
    }

    static String renderParam(Map<String, Object> pd) {
        StringBuilder b = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> e : pd.entrySet()) {
            if (!first) b.append(", ");
            first = false;
            b.append(q(e.getKey())).append(": ").append(val(e.getValue()));
        }
        return b.append("}").toString();
    }

    static void write(String path, String s) throws IOException {
        try (Writer w = new OutputStreamWriter(new FileOutputStream(path), "UTF-8")) {
            w.write(s);
        }
    }

    // -- Reflexions-Helfer ---------------------------------------------
    static Method findNoArgStatic(Class<?> c, String name) {
        for (Method m : c.getDeclaredMethods())
            if (m.getName().equals(name) && m.getParameterCount() == 0
                && Modifier.isStatic(m.getModifiers())) return m;
        return null;
    }

    static Object field(Object o, String name) {
        if (o == null) return null;
        for (Class<?> c = o.getClass(); c != null; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f.get(o);
            } catch (NoSuchFieldException ignored) {
            } catch (Throwable t) { return null; }
        }
        return null;
    }

    static Object call(Object o, String name) {
        if (o == null) return null;
        for (Class<?> c = o.getClass(); c != null; c = c.getSuperclass()) {
            try {
                Method m = c.getDeclaredMethod(name);
                m.setAccessible(true);
                return m.invoke(o);
            } catch (NoSuchMethodException ignored) {
            } catch (Throwable t) { return null; }
        }
        return null;
    }

    static String str(Object o) { return o == null ? null : String.valueOf(o); }

    static String val(Object o) {
        if (o instanceof Number || o instanceof Boolean) return String.valueOf(o);
        if (o instanceof String[]) {
            StringBuilder b = new StringBuilder("[");
            String[] a = (String[]) o;
            for (int i = 0; i < a.length; i++) {
                if (i > 0) b.append(", ");
                b.append(q(a[i]));
            }
            return b.append("]").toString();
        }
        if (o instanceof Collection) {
            StringBuilder b = new StringBuilder("[");
            boolean first = true;
            for (Object x : (Collection<?>) o) {
                if (!first) b.append(", ");
                first = false;
                b.append(val(x));             /* Zahlen bleiben Zahlen, Text wird zitiert */
            }
            return b.append("]").toString();
        }
        if (o instanceof Map) {               /* verschachtelte Tabellen (ADRESSE, UMRECHNER) */
            StringBuilder b = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                if (!first) b.append(", ");
                first = false;
                b.append(q(String.valueOf(e.getKey()))).append(": ").append(val(e.getValue()));
            }
            return b.append("}").toString();
        }
        return q(String.valueOf(o));
    }

    static String q(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '\\': b.append("\\\\"); break;
                case '"':  b.append("\\\""); break;
                case '\n': b.append("\\n");  break;
                case '\r': b.append("\\r");  break;
                case '\t': b.append("\\t");  break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append("\"").toString();
    }
}
