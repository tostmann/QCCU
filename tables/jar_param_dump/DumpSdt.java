/*
 * Schreibt die Tabelle der Statusdatentypen: Nummer und Laenge je Typ.
 *
 * Aufruf:  java -cp HMIPServer.jar:. DumpSdt <ausgabedatei>
 */
import java.io.*;
import java.lang.reflect.*;
import java.util.*;

public class DumpSdt {

    static final String CLS = "de.eq3.cbcs.protocol.hmip.application.StatusDataType";

    public static void main(String[] args) throws Exception {
        String outPath = args.length > 0 ? args[0] : "sdt_table.json";

        Class<?> c = Class.forName(CLS);
        Object[] konstanten = c.getEnumConstants();
        if (konstanten == null)
            throw new IllegalStateException(CLS + " ist keine Aufzaehlung");

        Field fValue = feld(c, "value");
        Field fLen = feld(c, "length");
        if (fValue == null || fLen == null)
            throw new IllegalStateException("Felder value/length nicht gefunden");

        Map<String, int[]> tab = new TreeMap<>();
        for (Object k : konstanten) {
            String name = ((Enum<?>) k).name();
            int value = ((Number) fValue.get(k)).intValue();
            int len = ((Number) fLen.get(k)).intValue();
            tab.put(name, new int[]{value, len});
        }

        StringBuilder out = new StringBuilder("{\n");
        boolean first = true;
        for (Map.Entry<String, int[]> e : tab.entrySet()) {
            if (!first) out.append(",\n");
            first = false;
            out.append(" \"").append(e.getKey()).append("\": {\"len\": ")
               .append(e.getValue()[1]).append(", \"type\": ").append(e.getValue()[0])
               .append("}");
        }
        out.append("\n}\n");

        try (Writer w = new OutputStreamWriter(new FileOutputStream(outPath), "UTF-8")) {
            w.write(out.toString());
        }
        System.err.println("  geschrieben: " + outPath + " (" + tab.size() + " Statusdatentypen)");
    }

    static Field feld(Class<?> c, String name) {
        for (Class<?> k = c; k != null; k = k.getSuperclass()) {
            try {
                Field f = k.getDeclaredField(name);
                f.setAccessible(true);
                return f;
            } catch (NoSuchFieldException ignored) {
            }
        }
        return null;
    }
}
