#!/usr/bin/env python3
"""Webfrontend der QCCU."""
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- ⚠️ Diese eine Zeile macht die Oberflaeche pfad-unabhaengig, und daran
     haengt der Zugang aus dem Menue von Home Assistant (Ingress): dort liegt
     die Seite nicht unter „/", sondern unter
     „/api/hassio_ingress/<Kennung>/". Eine Adresse mit fuehrendem Schraegstrich
     wuerde dann NICHT bei QCCU landen, sondern bei Home Assistant selbst —
     `/api/state` traefe dessen eigene Schnittstelle. Mit dieser Grundadresse
     und durchweg relativen Adressen stimmt beides: der unmittelbare Aufruf
     auf Port 8080 und der Weg ueber das Menue. -->
<base href="./">
<title>QCCU</title>
<link rel="icon" type="image/png" href="favicon.png">
<link rel="apple-touch-icon" href="favicon.png"><style>
:root{
  --bg:#f4f6f8; --fg:#11161c; --mut:#5d6874; --line:#dde3ea; --card:#fff;
  --acc:#1f6feb; --acc-fg:#fff; --ok:#1a7f45; --warn:#b45309; --bad:#b42318;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0f1319; --fg:#e7ebf0; --mut:#98a3b3; --line:#242c38; --card:#161c25;
  --acc:#4b90f7; --ok:#3fbf78; --warn:#e0a33a; --bad:#f0685c;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:60rem;margin:0 auto;padding:1.25rem 1rem 3rem}

header{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:1.25rem}
header .logo{height:26px;width:auto;display:block}
header h1{font-size:1.25rem;margin:0;letter-spacing:-.01em}
header .ver{color:var(--mut);font-size:.85rem}
header .sp{flex:1}

.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  box-shadow:var(--shadow);margin-bottom:1rem}
.card>h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);margin:0;padding:.75rem 1rem;border-bottom:1px solid var(--line)}
.card>h2.mitknopf{display:flex;align-items:center;justify-content:space-between;
  padding-top:.3rem;padding-bottom:.3rem}
.card>.body{padding:1rem}
.card>.body.flush{padding:0}

table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:.6rem 1rem;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
th{color:var(--mut);font-weight:600;font-size:.78rem;text-transform:uppercase;
  letter-spacing:.04em}
td.right,th.right{text-align:right}

button{font:inherit;font-weight:500;padding:.5rem .85rem;border-radius:7px;
  border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--mut)}
button.primary{background:var(--acc);color:var(--acc-fg);border-color:transparent}
button.primary:hover{filter:brightness(1.08)}
button.quiet{border-color:transparent;background:transparent;color:var(--mut);padding:.35rem .5rem}
button.quiet:hover{color:var(--fg);background:var(--bg)}
button[disabled]{opacity:.5;cursor:default}
.an{display:flex;gap:.45rem;align-items:center;margin:.5rem 0}
.ports{width:auto}.ports td{padding:.15rem .6rem .15rem 0;border:0}
.bar{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}

/* ⚠️ Ausdruecklich NUR Textfelder. Ohne den Ausschluss bekommt auch eine
   Checkbox padding, width:100% und den Fokusrahmen — und sitzt dann als
   handtellergrosses blaues Feld in der Zeile. */
input:not([type=checkbox]):not([type=radio]){
  font:inherit;padding:.55rem .7rem;border:1px solid var(--line);border-radius:7px;
  background:var(--bg);color:var(--fg);width:100%}
input:not([type=checkbox]):not([type=radio]):focus{
  outline:2px solid var(--acc);outline-offset:-1px;border-color:transparent}
input[type=checkbox]{width:auto;margin:0;accent-color:var(--acc)}
label{display:block;font-size:.82rem;color:var(--mut);margin:0 0 .3rem}
.field{margin-bottom:.9rem}
.hint{font-size:.82rem;color:var(--mut);margin:.35rem 0 0}

code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
a{color:var(--acc)}
.mut{color:var(--mut)} .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
  background:var(--mut);margin-right:.4rem;vertical-align:.05rem}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}

.meter{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:.4rem;position:relative}
.meter>i{display:block;height:100%;background:var(--acc);transition:width .3s}
.meter>i.ok{background:var(--ok)}.meter>i.warn{background:var(--warn)}
.meter>i.bad{background:var(--bad)}
/* Der Spitzenwert als Marke auf derselben Leiste — er gehoert daneben,
   nicht in eine zweite Zeile: sonst verliert man den Bezug zum Boden. */
.meter>b{position:absolute;top:-2px;width:2px;height:9px;background:var(--fg);
  opacity:.55;border-radius:1px;transition:left .3s}
.skala{display:flex;justify-content:space-between;font-size:.7rem;
  color:var(--mut);margin-top:.15rem}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.3rem 1rem;font-size:.9rem}
.kv dt{color:var(--mut)} .kv dd{margin:0}

.notice{display:flex;gap:.75rem;align-items:flex-start;padding:.9rem 1rem;
  border-radius:10px;border:1px solid var(--line);background:var(--card);
  box-shadow:var(--shadow);margin-bottom:1rem}
.notice.act{border-color:var(--acc)}
.notice .txt{flex:1}
.notice b{display:block;margin-bottom:.15rem}

dialog{border:none;border-radius:12px;padding:0;max-width:min(34rem,94vw);width:100%;
  background:var(--card);color:var(--fg);box-shadow:0 12px 40px rgba(16,24,40,.24)}
dialog::backdrop{background:rgba(9,12,17,.55)}
dialog h3{margin:0;padding:1rem 1.15rem;font-size:1rem;border-bottom:1px solid var(--line)}
dialog .dbody{padding:1.15rem}
dialog .dfoot{display:flex;gap:.5rem;justify-content:flex-end;padding:.85rem 1.15rem;
  border-top:1px solid var(--line);background:var(--bg);border-radius:0 0 12px 12px}
pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:.7rem .85rem;margin:.9rem 0 0;font-size:.82rem;line-height:1.5;
  max-height:15rem;overflow:auto;white-space:pre-wrap}
.spin{display:inline-block;width:.85rem;height:.85rem;border:2px solid var(--line);
  border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;
  vertical-align:-.12rem;margin-right:.4rem}
@keyframes sp{to{transform:rotate(360deg)}}
.empty{padding:2rem 1rem;text-align:center;color:var(--mut)}
footer{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line);
  color:var(--mut);font-size:.82rem;text-align:center}
footer a{color:var(--mut)} footer a:hover{color:var(--acc)}
.kacheln{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.kacheln>.card{margin:0}
</style></head><body><div class="wrap">

<header>
  <picture>
    <source id="logo_dark_src" srcset="/logo-dark.png" media="(prefers-color-scheme: dark)">
    <img src="logo.png" alt="busmatic" class="logo" onerror="this.parentNode.remove()">
  </picture>
  <h1>QUICHE</h1><span class="ver" id="ver"></span>
  <span class="sp"></span>
  <span id="hdrstat" class="mut"></span>
</header>

<div id="notices"></div>

<div class="card">
  <div class="body bar">
    <button class="primary" onclick="oeffneAnlernen()">HmIP anlernen</button>
    <button class="primary" id="btn_bidcos" style="display:none"
            onclick="oeffneBidcos()">BidCoS anlernen</button>
  </div>
</div>

<div class="card" id="karte_nwk" style="display:none">
  <h2>⚠ Der Stick hat keinen Netzwerkschlüssel</h2>
  <div class="body">
    <p>Ohne ihn schlägt <b>jedes Anlernen</b> fehl — der Schlüssel ist das,
      womit die Zentrale ihr Funknetz zusammenhält.</p>
    <!-- Zwei Lagen, zwei Ratschläge. Welcher zutrifft, hängt allein daran,
         ob schon Geräte eingetragen sind; bisher stand beides als ein Absatz
         da, und wer den Stick gewechselt hatte, las den falschen. Denselben
         Unterschied macht der Start im Protokoll. -->
    <p class="hint" id="nwk_leer">Er entsteht beim Einrichten von selbst.
      Bleibt diese Meldung stehen, hilft ein <b>Neustart</b> (Behälter oder
      Erweiterung); der Stick wird dabei erneut eingerichtet.</p>
    <p class="hint" id="nwk_geraete" style="display:none">Es sind
      <b id="nwk_anzahl">Geräte</b> eingetragen, deshalb wird
      <b>bewusst keiner erzeugt</b> — ein neuer Schlüssel würde sie alle
      aussperren, und zwar lautlos. Zwei Wege führen weiter: den
      <b>bisherigen Stick zurückstecken</b> (dann ist alles wie vorher), oder
      die Geräte hier <b>löschen und neu anlernen</b>. Ein Stick, der eben
      erst auf Werk zurückgesetzt oder ausgetauscht wurde, ist der häufigste
      Grund für diese Meldung.</p>
    <p id="nwk_knopf" style="display:none">
      <button class="bad" onclick="netzNeu()">Geräte verwerfen und neu beginnen</button>
      <span class="hint" id="nwk_msg"></span></p>
  </div>
</div>

<div class="card" id="karte_tabellen" style="display:none">
  <h2>⚠ Gerätebeschreibungen fehlen</h2>
  <div class="body">
    <p id="tab_text"></p>
    <p class="hint">Ohne sie kann QCCU keine Geräte führen — anlernen, lesen
      und schalten gehen erst danach. Die Beschreibungen entstehen aus einem
      Paketbezug beim ersten Start; scheitert der, wird es <b>bei jedem
      Start erneut versucht</b>: sobald das Netz steht, genügt ein Neustart.</p>
    <p class="hint" id="tab_docker">Von Hand nachholen:
      <code>docker run --rm --network host -v qccu-data:/data tostmann/qccu setup</code></p>
    <p class="hint"><b>Häufigste Ursache</b>, wenn eine Netzverbindung besteht:
      das Docker-Netz hat eine größere MTU als der Anschluss dieses Rechners
      (typisch bei DSL/PPPoE, VPN oder Overlay-Netzen). Dann bricht der Bezug
      mitten im Herunterladen ab. Das <code>--network host</code> oben umgeht
      genau das; dauerhaft hilft eine passende MTU für das Docker-Netz.</p>
  </div>
</div>

<div class="card">
  <h2>Home Assistant anbinden</h2>
  <div class="body">
    <p class="hint">Angelernt, geschaltet und angezeigt wird in Home Assistant
      über die Integration <b>Homematic(IP) Local</b>. Hier wird nur die
      Stick-Firmware eingespielt und der Aufkleber eines neuen Geräts
      hinterlegt — den Schlüssel darauf kann sich keine Zentrale selbst
      beschaffen ausser der von eQ-3.</p>
    <table><tbody>
      <tr><td>Host</td><td><code id="ha_host">—</code></td></tr>
      <tr><td>Benutzer / Kennwort</td><td><code>beliebig</code>
        <span class="hint">— die QCCU prüft keine Zugangsdaten</span></td></tr>
      <tr><td>Schnittstellen</td><td>nur <code id="ha_if">HmIP-RF</code> anhaken
        <p class="hint" id="ha_ifhint"></p></td></tr>
      <tr><td>Eigene Ports setzen</td><td><table class="ports"><tbody id="ha_ports">
        </tbody></table></td></tr>
    </tbody></table>
    <p class="hint" id="ha_warn"></p>
    <label class="an"><input type="checkbox" id="sw_alt"
      onchange="setzePorts()"> Ausweichports <code>+10000</code> — wenn auf
      dieser Maschine schon eine OCCU läuft</label>
    <p class="hint" id="ports_msg"></p>
  </div>
</div>

<div class="kacheln">
<div class="card" id="karte_hmip">
  <h2>HmIP-RF</h2>
  <div class="body">
    <table><tbody>
      <tr><td>Schnittstelle</td><td><code id="hm_if">HmIP-RF</code></td></tr>
      <tr><td>Adresse der Zentrale</td><td><code id="hm_addr">—</code></td></tr>
      <tr><td>Geräte</td><td><span id="hm_dev">—</span></td></tr>
      <tr><td>Anlernfenster</td><td><span id="hm_pair">—</span></td></tr>
      <tr><td>Schlafende Geräte wecken</td><td><span id="hm_weck">—</span></td></tr>
    </tbody></table>
    <p class="hint" id="hm_hint"></p>
  </div>
</div>

<div class="card" id="karte_bidcos" style="display:none">
  <h2>BidCos-RF</h2>
  <div class="body">
    <table><tbody>
      <tr><td>Schnittstelle</td><td><code id="bc_if">BidCos-RF</code>
        <span class="hint" id="bc_send"></span></td></tr>
      <tr><td>Adresse der Zentrale</td><td><code id="bc_addr">—</code></td></tr>
      <tr><td>Geräte</td><td><span id="bc_dev">—</span></td></tr>
      <tr><td>Anlernfenster</td><td><span id="bc_pair">—</span></td></tr>
    </tbody></table>
    <p class="hint" id="bc_hint"></p>
  </div>
</div>
</div>

<div class="card">
  <h2 class="mitknopf">Funk
    <span>
      <button class="quiet" id="knopf_luft" style="display:none"
              onclick="location.href='api/luft.log'">Rohmitschnitt laden</button>
      <button class="quiet" onclick="oeffneFirmware()">Stick-Firmware</button>
    </span>
  </h2>
  <div class="body" id="radio"></div>
</div>

<div class="card" id="karte_ereignisse" style="display:none">
  <h2 class="mitknopf">Zuletzt geschehen
    <button class="quiet" onclick="ereignisseLoeschen()">löschen</button></h2>
  <div class="body flush"><table id="ereignisse"><tbody></tbody></table></div>
</div>

<p id="meldung" style="display:none"></p>

<div class="card" id="karte_post" style="display:none">
  <h2>Anlernwünsche</h2>
  <div class="body">
    <p class="hint">Geräte, die gerade angelernt werden <b>wollen</b> — sie
      haben eben ihre Anlerntaste gesehen und rufen. QCCU kann sie nur mit dem
      Schlüssel vom Aufkleber aufnehmen; ohne ihn bleibt es beim Zusehen.
      Wer aufhört zu rufen, verschwindet hier wieder von selbst.</p>
    <div class="body flush"><table id="post"><thead><tr>
      <th>Adresse</th><th>Typ</th><th>Zuletzt gerufen</th>
      <th>Schnittstelle</th><th></th>
    </tr></thead><tbody></tbody></table></div>
  </div>
</div>

<div class="card" id="karte_verwaist" style="display:none">
  <h2>Funkt noch, gehört aber nicht dazu</h2>
  <div class="body">
    <p class="hint">Diese Geräte senden mit <b>unserem Netzschlüssel</b>, stehen
      aber nicht in der Geräteliste. Sie sind damit <b>nicht im Werkszustand</b>
      und lassen sich so auch nicht anlernen — ein angelerntes Gerät sendet
      keinen Anlernruf. Ein <b>Werksreset am Gerät</b> macht es wieder
      anlernbar; danach verschwindet es hier von selbst.</p>
    <div class="body flush"><table id="verwaist"><thead><tr>
      <th>Funkadresse</th><th>Zuletzt gehört</th><th>Sendungen</th>
    </tr></thead><tbody></tbody></table></div>
  </div>
</div>

<div class="card">
  <h2>Geräte</h2>
  <div class="body flush"><table id="devs"><thead><tr>
    <th>Name</th><th>Adresse</th><th>Schnittstelle</th><th>Typ</th>
    <th>Zuletzt gehört</th><th class="right">Pegel</th><th></th>
  </tr></thead><tbody></tbody></table></div>
</div>

<div class="card" id="karte_deutung" style="display:none">
  <h2>Statuswerte: gedeutet oder roh</h2>
  <div class="body flush"><table id="deutung"><thead><tr>
    <th>Datentyp</th><th>Beleg</th><th>Zeuge</th>
  </tr></thead><tbody></tbody></table>
  <p class="hint">Was hier nicht steht, meldet QCCU als <code>RAW_SDT&lt;n&gt;</code>:
    der Rohwert unter dem Namen, den eq-3 dafür führt — keine erfundene Skalierung.</p></div>
</div>

<dialog id="dlgPair">
  <h3>Gerät anlernen</h3>
  <div class="dbody">
    <div class="field">
      <label for="key">Key auf dem Aufkleber (nicht SGTIN)</label>
      <input id="key" placeholder="ABCEF-GHJKL-MNPQR-STUWX-YZ2345" autocomplete="off">
      <p class="hint">Der Aufkleber klebt auf dem Gerät: <b>26 Zeichen</b>,
         meist in fünf Gruppen. <b>Bindestriche und Leerzeichen sind egal</b> —
         abtippen wie aufgedruckt genügt. Wer den Schlüssel als 32 Hexziffern
         hat, kann ihn ebenso eingeben. Die Zeichen <b>D, I, O und V</b> gibt
         es dort nicht; was danach aussieht, ist 0, 1, 0 bzw. U.</p>
    </div>
    <p class="hint" id="pairinfo"></p>
    <div class="hint" id="nachher" style="display:none">
      <b>Und jetzt in Home Assistant:</b> das Gerät erscheint dort nicht von
      selbst. Unter <b>Einstellungen → System → Reparaturen</b> steht „Neues
      Gerät …" — dort einen Namen vergeben und bestätigen; erst danach gibt es
      Schalter und Sensoren. Der Name landet auch hier in der Zentrale.
      <br>Steht dort nichts, kennt die Integration die Adresse noch aus einem
      früheren Anlauf: Dienst <code>homematicip_local.clear_cache</code>
      aufrufen und die Integration neu laden.
    </div>
  </div>
  <div class="dfoot">
    <button onclick="dlgPair.close()">Schließen</button>
    <button class="primary" id="pairgo" onclick="pair()">Fenster öffnen</button>
  </div>
</dialog>

<dialog id="dlgBidcos">
  <h3>BidCoS-Gerät anlernen</h3>
  <div class="dbody">
    <p>Fenster öffnen, dann am Gerät den Anlernknopf drücken.</p>
    <p class="hint">Ohne Adresse wird jedes Gerät angenommen, das jetzt
      ruft — auch ein fremdes.</p>
    <label class="an"><input type="checkbox" id="bc_mitziel"
      onchange="bidcosZiel()"> Adresse vorgeben</label>
    <input id="bc_ziel" placeholder="z.B. 1A2B3C" maxlength="6"
           style="text-transform:uppercase;display:none">
    <p class="hint" id="bc_pairmsg"></p>
  </div>
  <div class="dfoot">
    <button class="quiet" onclick="$('#dlgBidcos').close()">Abbrechen</button>
    <button class="primary" onclick="bidcosAnlernen()">Fenster öffnen</button>
  </div>
</dialog>

<dialog id="dlgFw">
  <h3>Stick-Firmware</h3>
  <div class="dbody">
    <p class="hint" style="margin:0 0 .9rem">
      Erforderlich ist ein <b>busware CUL V3 (868&nbsp;MHz)</b> —
      <a href="https://shop.busware.de/cul868" target="_blank" rel="noopener">shop.busware.de</a>.
      Beim Einspielen wird die vorhandene <b>culfw ersetzt</b>. Der Stick spricht
      danach ausschließlich <b>BidCoS &amp; IP</b> — die übrigen
      culfw-Funkarten (FS20, IT, EM &c.) entfallen. Zurück geht es, indem culfw
      wieder eingespielt wird.
    </p>
    <p class="hint" style="margin:0 0 .9rem">Einen CUL, auf dem noch culfw
      läuft, fasst QCCU von sich aus <b>nicht</b> an — er könnte an diesem
      Rechner für etwas anderes in Betrieb sein. Um ihn zu übernehmen: abziehen,
      <b>die BL-Taste auf der Rückseite</b> gedrückt halten und wieder
      einstecken. Dann meldet er sich im Bootlader und erscheint hier. Ein
      fabrikneuer CUL tut das von selbst.</p>
    <p class="hint" style="margin:0 0 .9rem">Angelernte Geräte überstehen das
      Einspielen: Netzwerkschlüssel und Sendezähler liegen im EEPROM des Sticks
      und bleiben erhalten.</p>
    <dl class="kv" id="fwkv"></dl>
    <div id="fwmsg" style="margin-top:.9rem"></div>
    <pre class="log" id="fwlog" hidden></pre>
  </div>
  <div class="dfoot">
    <button id="fwclose" onclick="dlgFw.close()">Schließen</button>
    <button class="primary" id="fwgo" onclick="flashen()">Einspielen</button>
  </div>
</dialog>


<footer>
  <span>&copy; 2026 Dirk Tostmann</span> &middot;
  <span><a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" rel="noopener">Apache-2.0</a></span> &middot;
  <span><a href="https://github.com/tostmann/QCCU" target="_blank" rel="noopener">QCCU auf GitHub</a></span>
</footer>

</div><script>
const $=s=>document.querySelector(s);
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let ZUSTAND={}, fwOffen=false;

// ⚠️ Die Antwort wird AUSGEWERTET. Vorher flog sie weg: wer 25 statt 26
// Zeichen eintippte, bekam vom Dienst eine klare Auskunft („Aufkleber hat 25
// statt 26 Zeichen") und sah in der Oberflaeche — nichts. Der Knopf schien
// kaputt. Genau dieses Feld ist das erste, an dem jeder Neue sitzt.
// „vor 3 min" liest sich schneller als ein Zeitstempel — und sagt genau das,
// was hier zaehlt: meldet sich das Geraet noch.
function alterText(sek){
  if(sek==null) return '—';
  if(sek<90) return 'gerade eben';
  if(sek<5400) return 'vor '+Math.round(sek/60)+' min';
  if(sek<172800) return 'vor '+Math.round(sek/3600)+' h';
  return 'vor '+Math.round(sek/86400)+' Tagen';
}
// Anlernrufe kommen im Sekundentakt-Bereich — hier ist „gerade eben" zu
// grob: man will sehen, OB das Gerät noch ruft.
function rufAlter(sek){
  if(sek==null) return '—';
  if(sek<60) return 'vor '+Math.round(sek)+' s';
  return alterText(sek);
}
function uhr(t){
  const d=new Date(t*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)
        +':'+('0'+d.getSeconds()).slice(-2);
}

// „Zuletzt geschehen" leeren. Die Liste ist eine Erinnerungshilfe, kein
// Protokoll — wer sie gelesen hat, will sie wegräumen können.
async function ereignisseLoeschen(){ await post('api/ereignisse/loeschen',{}); }

async function post(u,b){
  let j=null;
  try{
    const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify(b||{})});
    try{ j=await r.json(); }catch(e){ j=null; }
    if(!r.ok || (j&&j.error)) melde((j&&j.error)||('Fehler '+r.status),'bad');
    else melde('', '');
  }catch(e){ melde('Keine Verbindung zur Zentrale.','bad'); }
  await laden();
  return j;
}

// Eine Stelle fuer Rueckmeldungen — sichtbar, wo gerade gearbeitet wird:
// steht ein Dialog offen, gehoert sie dorthin, sonst nach oben.
function melde(text,art){
  const im=$('#pairinfo'), oben=$('#meldung');
  const offen=$('#dlgPair')&&$('#dlgPair').open;
  const ziel = offen ? im : oben;
  if(oben && !offen){ oben.innerHTML = text ? '<span class="'+(art||'')+'">'+esc(text)+'</span>' : '';
                      oben.style.display = text ? '' : 'none'; }
  if(offen && im) im.innerHTML = text ? '<span class="'+(art||'')+'">'+esc(text)+'</span>' : '';
  if(text && ziel===oben && oben) oben.scrollIntoView({block:'nearest'});
}

function oeffneAnlernen(){ $('#dlgPair').showModal(); }
function oeffneBidcos(){
  const m=$('#bc_pairmsg'); if(m) m.textContent='';
  $('#dlgBidcos').showModal();
}

function bidcosZiel(){
  const an=$('#bc_mitziel').checked, f=$('#bc_ziel');
  f.style.display = an ? '' : 'none';
  // Beim Abwählen leeren: ein unsichtbares, aber gefülltes Feld würde das
  // Fenster stillschweigend eingrenzen.
  if(an) f.focus(); else f.value='';
}

// ⚠️ Beim Laden ist JEDER Dialog zu. Ein Dialog, der nach einem Neuladen
// offen stehen bliebe, verdeckt die Seite und sieht aus wie ein Hänger.
function dialogeSchliessen(){
  document.querySelectorAll('dialog[open]').forEach(d=>d.close());
}

async function bidcosAnlernen(){
  const el=$('#bc_ziel'), m=$('#bc_pairmsg');
  const ziel=($('#bc_mitziel').checked && el ? el.value : '').trim().toUpperCase();
  if(m) m.textContent='';
  const r=await fetch('api/bidcos/pair',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({seconds:ANLERNDAUER, address:ziel||null})});
  let d={}; try{ d=await r.json(); }catch(e){}
  // ⚠️ Die Antwort auswerten, nicht bloss neu laden: ein abgewiesener
  // Aufruf (kein Senden erlaubt, krumme Adresse) saehe sonst genauso aus
  // wie ein erfolgreicher, nur dass nichts passiert.
  if(r.ok){ $('#dlgBidcos').close(); }
  else if(m){ m.textContent = d.error || 'Das Anlernfenster ließ sich nicht öffnen.'; }
  laden();
}
function oeffneFirmware(){ fwOffen=true; zeichneFirmware(); $('#dlgFw').showModal(); }
$('#dlgFw')?.addEventListener('close',()=>{fwOffen=false;});

// Die Funkadresse vergibt die Zentrale — sie ist keine Eingabe. Ein Feld dafür
// lädt nur dazu ein, eine bereits belegte zu erraten.
function pair(){
  // Nur zum Erkennen bereinigen; gesendet wird, was der Anwender getippt hat —
  // der Dienst wirft Trennzeichen selbst weg und kann sauberer melden.
  const v=$('#key').value.trim(), plain=v.replace(/[^0-9a-zA-Z]/g,'');
  if(!plain){ $('#pairinfo').innerHTML='<span class="bad">Bitte Aufkleber oder Schlüssel eingeben.</span>'; return; }
  const istHex=/^[0-9a-fA-F]{32}$/.test(plain);
  post('api/pair',{sticker:istHex?null:v, local_key:istHex?plain:null,
                    seconds:ANLERNDAUER})
    // ⚠️ Nur bei Erfolg schliessen. Eine abgewiesene Eingabe (unbrauchbarer
    // Aufkleber) muss im Dialog stehen bleiben, sonst verschwindet die
    // Begruendung mit dem Fenster. Die Restzeit steht danach oben.
    .then(j => { if(j && !j.error) $('#dlgPair').close(); });
}

// ⚠️ Die Geräteliste führt beide Familien. Prüfen und Entfernen müssen
// deshalb wissen, welche gemeint ist — sonst landet ein BidCoS-Gerät in der
// HmIP-Zentrale, die seine Adresse gar nicht kennt: die Antwort ist „ok",
// passiert ist nichts.
// Wie lange ein Anlernfenster offen bleibt — dieselbe Dauer fuer beide
// Familien. 254 s ist der Vorgabewert von Zigbee2MQTT und zugleich der
// groesste Wert, den die Zigbee-Spezifikation fuer ein befristetes Fenster
// zulaesst (255 hiesse dauerhaft). Ein Eingabefeld gab es dafuer; abbrechen
// laesst sich das Fenster ohnehin jederzeit oben.
const ANLERNDAUER = 254;

async function setzePorts(){
  const j = await post('api/ports', {alt_ports: $('#sw_alt').checked});
  const pm=$('#ports_msg');
  if(j && j.error && pm) pm.innerHTML='<b class="bad">'+esc(j.error)+'</b>';
}

function bidcosIf(){ return (ZUSTAND.bidcos||{}).interface; }

// Eine Funkadresse, wie sie in beiden Kacheln steht.
function adr(a){ return a ? String(a).toUpperCase() : '—'; }
function anzahlText(n){ return n ? String(n) : 'noch keines angelernt'; }

// Aktiv nachsehen, ob ein Geraet noch da ist. Kostet EINE Sendung.
async function pruefe(addr, iface){
  const weg = (iface && iface===bidcosIf()) ? 'api/bidcos/ping' : 'api/device/ping';
  const j = await post(weg, {address: addr});
  if(j && j.ok) melde(j.antwortet ? 'Das Gerät antwortet.'
                                  : 'Keine Antwort — das Gerät meldet sich nicht.',
                      j.antwortet ? '' : 'warn');
}

async function entferne(addr, iface, name){
  const bc = iface && iface===bidcosIf();
  const nachsatz = bc
    ? '\n\nEs bleibt auf unsere Zentrale angelernt, bis es neu angelernt '
      +'oder zurückgesetzt wird.'
    : '\n\nEs bekommt dabei einen Funk-Ausschluss und lässt sich danach '
      +'ohne Werksreset neu anlernen.';
  if(!confirm('Gerät '+name+' entfernen?'+nachsatz)) return;
  await post('api/device/delete', {address: addr, interface: iface||null});
}

// Der zweite der beiden Wege aus der Warnkarte — als Knopf. Zweimal fragen,
// denn danach braucht jedes Geraet einen Werksreset: rueckgaengig gibt es das
// hier nicht.
async function netzNeu(){
  const m=$('#nwk_msg'), n=(ZUSTAND.devices||[]).length;
  if(!confirm('Alle '+n+' Gerät(e) verwerfen und dem Stick einen neuen '
      +'Netzwerkschlüssel geben?\n\nDie Geräte lassen sich danach nur nach '
      +'einem Werksreset am Gerät wieder anlernen. Wer stattdessen den '
      +'bisherigen Stick zurücksteckt, behält alles.')) return;
  if(m){ m.textContent='läuft …'; m.className='hint'; }
  try{
    const r=await fetch('api/netz/neu',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({bestaetigt:true})});
    const j=await r.json();
    if(m){ m.textContent=j.meldung||j.error||''; m.className=j.ok?'ok':'bad'; }
  }catch(e){ if(m){ m.textContent='Aufruf fehlgeschlagen: '+e; m.className='bad'; } }
  laden();
}

function flashen(){
  const f=ZUSTAND.firmware||{};
  const frage = f.zustand==='bootlader'
    ? 'Firmware auf den neuen Stick einspielen?'
    : 'Firmware auf dem Stick ersetzen?\n\nEine vorhandene culfw wird dabei '
     +'überschrieben — der Stick spricht danach ausschließlich '
     +'BidCoS & IP.';
  if(!confirm(frage+'\n\nDer Funk ruht so lange. Den Stick dabei nicht abziehen.')) return;
  post('api/firmware/flash');
}

function zeichneFirmware(){
  const f=ZUSTAND.firmware;
  const kv=$('#fwkv'), msg=$('#fwmsg'), log=$('#fwlog'),
        go=$('#fwgo'), zu=$('#fwclose');
  if(!f){ kv.innerHTML=''; msg.textContent='Keine Firmware im Abbild.';
          go.hidden=true; return; }
  kv.innerHTML='<dt>Auf dem Stick</dt><dd><code>'+esc(f.installiert||'—')+'</code></dd>'
              +'<dt>Mitgeliefert</dt><dd><code>'+esc(f.mitgeliefert||'—')+'</code></dd>';
  let m='', knopf=false;
  if(f.laeuft){
    m='<span class="spin"></span>Wird eingespielt … <span class="mut">Den Stick '
     +'jetzt nicht abziehen.</span>';
  } else if(f.zustand==='bootlader'){
    m='<b class="warn">Neuer Stick im Bootlader.</b> So wird er ausgeliefert; '
     +'er braucht einmalig die Firmware.'; knopf=true;
  } else if(f.zustand==='laeuft'){
    if(f.aktualisierbar){ m='<b>Neuere Fassung verfügbar.</b> '+esc(f.hinweis); knopf=true; }
    else m='<span class="ok">Der Stick ist auf dem aktuellen Stand.</span>';
  } else if(f.zustand==='kein_zugang'){
    m='<b class="bad">Kein Zugriff auf den USB-Bus.</b> '+esc(f.hinweis);
  } else {
    m='<b class="bad">Kein Stick gefunden.</b> '+esc(f.hinweis);
  }
  msg.innerHTML=m;
  go.hidden=!knopf; go.disabled=!!f.laeuft;
  go.textContent=f.zustand==='bootlader'?'Firmware einspielen':'Aktualisieren';
  zu.disabled=!!f.laeuft;
  const p=f.protokoll||[];
  log.hidden=!p.length;
  if(p.length){ log.textContent=p.join('\n'); log.scrollTop=log.scrollHeight; }
}

function zeichneHinweise(){
  const f=ZUSTAND.firmware||{}, p=ZUSTAND.pairing||{}, n=[];
  if(f.laeuft) n.push(['act','<span class="spin"></span>',
    '<b>Firmware wird eingespielt</b>Der Funk ruht, bis der Stick wieder da ist.',
    'Fortschritt', 'oeffneFirmware()']);
  else if(f.zustand==='bootlader') n.push(['act','',
    '<b>Neuer Stick gefunden</b>Er wird im Bootlader ausgeliefert und braucht '
    +'einmalig die Firmware.','Einrichten','oeffneFirmware()']);
  else if(f.aktualisierbar) n.push(['','',
    '<b>Neuere Stick-Firmware verfügbar</b>'+esc(f.hinweis),'Ansehen','oeffneFirmware()']);
  else if(f.zustand==='kein_stick'||f.zustand==='kein_zugang') n.push(['','',
    '<b>Kein Funk</b>'+esc(f.hinweis),'Ansehen','oeffneFirmware()']);
  if(p.open) n.push(['act','','<b>HmIP: Anlernfenster offen</b>Noch '+p.seconds_left
    +' s. '+esc(p.last||''),'Abbrechen',"post('api/pair/stop')"]);
  // Der zweite Timer. Beide gehoeren nach oben: wer ein Fenster geoeffnet
  // hat, steht mit dem Geraet in der Hand davor und will die Restzeit sehen,
  // ohne zur Kachel zu scrollen.
  const bcz=ZUSTAND.bidcos||{};
  if(bcz.anlernen_offen>0) n.push(['act','',
    '<b>BidCoS: Anlernfenster offen</b>Noch '+Math.round(bcz.anlernen_offen)+' s'
    +(bcz.anlern_ziel?(' — nur für '+esc(bcz.anlern_ziel)):' — für jedes Gerät')
    +'. Jetzt am Gerät den Knopf drücken.',
    'Abbrechen',"post('api/bidcos/pair/stop')"]);
  $('#notices').innerHTML=n.map(x=>
    '<div class="notice '+x[0]+'">'+x[1]+'<div class="txt">'+x[2]+'</div>'
    +'<button onclick="'+x[4]+'">'+x[3]+'</button></div>').join('');
}

let LOGO_GESETZT=false;
async function laden(){
  let s; try{ s=await (await fetch('api/state')).json(); }catch(e){ return; }
  ZUSTAND=s;
  $('#ver').textContent=s.version||'';
  // Die Fassung an die Bild-Adressen haengen — beim Fassungswechsel ist die
  // Adresse damit neu, und kein Zwischenspeicher haelt ein altes Zeichen fest.
  if(s.version && !LOGO_GESETZT){
    LOGO_GESETZT=true;
    const v=encodeURIComponent(s.version);
    const im=document.querySelector('header img.logo');
    if(im) im.src='logo.png?v='+v;
    const dk=$('#logo_dark_src');
    if(dk) dk.srcset='logo-dark.png?v='+v;
    const ic=document.querySelector('link[rel="icon"]');
    if(ic) ic.href='favicon.png?v='+v;
  }
  // Die Angaben, die Home Assistant beim Einrichten verlangt. Sie kommen aus
  // dem laufenden Dienst, nicht aus der Anleitung — sonst stimmen sie nicht
  // mehr, sobald jemand einen Port aendert.
  // Fehlen die Gerätebeschreibungen, ist das die wichtigste Nachricht der
  // Seite — sie steht deshalb ganz oben und nur dann.
  // Fehlt der Netzwerkschluessel, ist ueberhaupt kein Anlernen moeglich —
  // das gehoert genauso nach oben wie fehlende Beschreibungen.
  const kn=$('#karte_nwk');
  if(kn){
    kn.style.display = (s.radio&&s.radio.netzschluessel_fehlt) ? '' : 'none';
    // Welcher der beiden Ratschläge gilt, entscheidet die Zahl der Geräte.
    const n=(s.devices||[]).length, leer=$('#nwk_leer'), mit=$('#nwk_geraete');
    if(leer) leer.style.display = n ? 'none' : '';
    if(mit)  mit.style.display  = n ? '' : 'none';
    const za=$('#nwk_anzahl');
    if(za) za.textContent = (n===1) ? 'ein Gerät' : (n+' Geräte');
    const kb=$('#nwk_knopf');
    if(kb) kb.style.display = n ? '' : 'none';
  }
  const tb=s.tabellen||{};
  const kt=$('#karte_tabellen');
  if(kt){
    if(tb.ok===false){
      kt.style.display='';
      $('#tab_text').innerHTML='Es fehlen: <code>'+esc((tb.fehlend||[]).join(', '))
        +'</code>. Geladen sind '+(tb.geraetetypen||0)+' Gerätetypen und '
        +(tb.kanaltypen||0)+' Kanaltypen.';
      // In der Erweiterung gibt es keine Kommandozeile — dort waere der
      // docker-Aufruf ein Rat ins Leere.
      const td=$('#tab_docker');
      if(td) td.style.display = s.erweiterung ? 'none' : '';
    } else { kt.style.display='none'; }
  }
  const ab=s.anbindung||{};
  const setz=(id,v)=>{const e=$('#'+id); if(e) e.textContent=v;};
  setz('ha_host', ab.host||location.hostname||'—');
  // ⚠️ Laeuft die BidCoS-Schnittstelle, muessen BEIDE angehakt werden —
  // sonst sieht die Haussteuerung die klassischen Geraete nicht.
  const bc = s.bidcos;
  setz('ha_if', bc ? ((ab.interface||'HmIP-RF')+' und '+bc.interface) : (ab.interface||'HmIP-RF'));
  // Alle Dienste mit ihren laufenden Ports. Was aus ist, steht als „aus"
  // da — sonst sucht man einen Dienst, den niemand eingeschaltet hat.
  const dienste=[
    ['JSON-RPC', ab.json_port, 'Home Assistant'],
    [ab.interface||'HmIP-RF', ab.rpc_port, 'XML-RPC'],
    [bc?bc.interface:null, ab.bidcos_port, 'XML-RPC'],
    ['ReGa', ab.rega_port, 'FHEM/HMCCU'],
    ['CUL-Zugang', ab.cul_port, 'culfw-Stil'],
    ['Weboberfläche', ab.web_port, 'diese Seite'],
  ];
  const pt=$('#ha_ports');
  if(pt) pt.innerHTML=dienste.filter(d=>d[0]).map(d=>
    '<tr><td>'+esc(d[0])+' <span class="mut">'+esc(d[2])+'</span></td>'
   +'<td><code>'+(d[1]?d[1]:'aus')+'</code></td></tr>').join('');
  // Die Haekchen zeigen den GESPEICHERTEN Wunsch. Weicht er vom laufenden
  // Zustand ab, steht darunter, dass ein Neustart fehlt.
  const sa=$('#sw_alt'), pm=$('#ports_msg');
  if(sa && document.activeElement!==sa) sa.checked = !!ab.wunsch_alt_ports;
  if(pm){
    const offen = (!!ab.wunsch_alt_ports !== !!ab.alt_ports);
    if(!offen){ pm.innerHTML=''; }
    else {
      // ⚠️ Die Oberfläche wandert MIT. Nach dem Neustart ist diese Seite
      // unter der alten Adresse tot — wer das nicht liest, hält die
      // Zentrale für kaputt. Deshalb die neue Adresse ausrechnen und
      // hinschreiben.
      const d = ab.wunsch_alt_ports ? 10000 : -10000;
      const neuWeb = (ab.web_port||0) + d;
      const host = location.hostname || 'localhost';
      pm.innerHTML='<b class="warn">Wirkt erst nach einem Neustart der '
        +'Zentrale</b> — Ports werden einmal gebunden. <b>Auch diese Seite '
        +'wandert mit:</b> danach erreichbar unter <code>http://'
        +esc(host)+':'+neuWeb+'/</code>. Die Ports in Home Assistant und '
        +'FHEM müssen ebenfalls nachgezogen werden.'
        // ⚠️ Im Behälter reicht das Verschieben allein nicht: was nicht
        // veröffentlicht ist, ist von außen nicht erreichbar. Genau so
        // haben wir uns beim Erproben selbst ausgesperrt (20.08.2026).
        +((s.netz||{}).behaelter
          ? ' <b>Im Container außerdem die veröffentlichten Ports '
           +'(<code>-p</code>) mitziehen</b> — sonst ist danach nichts '
           +'erreichbar.'
          : '');
    }
  }
  // ⚠️ „nur" ist hier keine Floskel. Home Assistant bietet vier weitere
  // Schnittstellen an, die QCCU nicht bedient — und BidCos-Wired liegt auf
  // Port 2000, also genau auf dem CUL-Zugang. Ein Haken dort schickt die
  // Haussteuerung nicht ins Leere, sondern auf einen Dienst, der antwortet
  // und etwas völlig anderes ist.
  const ih=$('#ha_ifhint');
  if(ih) ih.innerHTML='<code>BidCos-Wired</code>, <code>VirtualDevices</code>, '
    +'<code>CCU-Jack</code>, <code>CUxD</code> gibt es hier nicht. '
    +'<b><code>BidCos-Wired</code> nicht anhaken</b> — Port 2000 ist der '
    +'CUL-Zugang.';
  setz('ha_json', ab.json_port ? String(ab.json_port) : 'aus');
  setz('ha_rpc', ab.rpc_port ? String(ab.rpc_port) : '—');
  const rr=s.radio||{};
  setz('hm_if', (s.anbindung||{}).interface || 'HmIP-RF');
  // ⚠️ Beide Kacheln beschriften dasselbe gleich — sonst liest man einen
  // Unterschied, wo keiner ist. Adressen durchgehend in Großbuchstaben,
  // Gerätezahl durchgehend als Zahl bzw. „noch keines".
  const bcIf = bc ? bc.interface : null;
  setz('hm_addr', adr(rr.own_addr));
  setz('hm_dev', anzahlText((s.devices||[]).filter(x=>x.interface!==bcIf).length));
  const hp=s.pairing||{};
  setz('hm_pair', hp.open ? ('offen, noch '+hp.seconds_left+' s') : 'zu');
  // Kann der Stick wecken, und steht der Weckkanal? Ohne Mitschnitt war das
  // nirgends ablesbar — auf dem ersten frisch geflashten Stick stand er auf aus.
  setz('hm_weck', rr.burst_faehig===true
        ? ('ja' + (rr.weckkanal ? ', Weckkanal '+rr.weckkanal : ', Weckkanal NICHT gesetzt'))
        : rr.burst_faehig===false ? 'nein — Stick-Firmware zu alt' : '—');

  const kb=$('#karte_bidcos');
  if(bc){
    kb.style.display='';
    setz('bc_if', bc.interface);
    setz('bc_addr', adr(bc.eigene_id));
    setz('bc_dev', anzahlText(bc.geraete));
    setz('bc_pair', bc.anlernen_offen>0
      ? ('offen, noch '+Math.round(bc.anlernen_offen)+' s'
         +(bc.anlern_ziel?(' — nur '+bc.anlern_ziel):''))
      : 'zu');
    const se=$('#bc_send');
    if(se) se.textContent = bc.senden_erlaubt ? '' : '— liest nur mit, sendet nicht';
    const bb=$('#btn_bidcos');
    if(bb) bb.style.display = bc.senden_erlaubt ? '' : 'none';
    const bh=$('#bc_hint');
    if(bh){
      let t='';
      if(bc.tabellen && bc.tabellen.ok===false)
        t='Die BidCoS-Gerätetabellen fehlen ('+(bc.tabellen.fehlend||[]).join(', ')
          +') — ohne sie können keine Geräte geführt werden.';
      else if(!bc.senden_erlaubt)
        t='Die Schnittstelle liest den Funk mit und meldet, was sie sieht, '
          +'sendet aber nichts. So stört sie eine andere Zentrale im selben '
          +'Funknetz nicht.';
      bh.textContent=t;
    }
  } else { kb.style.display='none'; }

  const w=$('#ha_warn');
  if(w){
    if(!ab.json_port){
      w.innerHTML='<span class="bad">Die JSON-Auskunft ist abgeschaltet — '
        +'Home Assistant kann die Zentrale so nicht finden. '
        +'Mit <code>--json-port</code> einschalten.</span>';
    } else if(!ab.host){
      w.innerHTML='<span class="warn">Ohne <code>--advertise</code> muss die '
        +'Adresse dieses Rechners von Hand eingetragen werden; sonst geht der '
        +'Rückruf ins Leere.</span>';
    } else { w.textContent=''; }
  }
  const dv=s.devices||[];
  $('#devs').tBodies[0].innerHTML = dv.length ? dv.map(d=>
    '<tr><td>'+esc(d.name||d.label)
   +(d.unreach?' <span class="warn" title="meldet sich nicht">·</span>':'')
   +(d.wartet?'<div class="warn" style="font-size:.85em">wartet auf Aufnahme — '
     +'in der Haussteuerung im Posteingang aufnehmen (dabei benennen)</div>':'')+'</td>'
   +'<td><code>'+esc(d.address)+'</code>'
   +'<span class="mut" style="font-size:.85em"> · '+esc(d.rf||'—')+'</span></td>'
   +'<td class="mut">'+esc(d.interface||'—')+'</td>'
   +'<td class="mut">'+esc(d.label)+' · '+d.channels+' Kan.'
   +(d.hoerer_text?'<div class="mut" style="font-size:.85em" '
     +'title="wie das Gerät zuhört — ein schlafendes hört einen Befehl erst, '
     +'wenn es von selbst aufwacht">hört '+esc(d.hoerer_text)+'</div>':'')+'</td>'
   +'<td class="mut">'+alterText(d.vor_sek)+'</td>'
   +'<td class="right mut">'+(d.rssi==null?'—':(d.rssi+' dBm'))+'</td>'
   +'<td class="right">'
   +(d.wartet?'<button class="quiet" title="meldet das Gerät jetzt an die '
     +'Haussteuerung — nötig nur, wenn diese keinen Posteingang hat (FHEM)" '
     +'onclick="post(\'api/device/aufnehmen\',{address:\''+esc(d.address)+'\'})">'
     +'melden</button> ':'')
   +'<button class="quiet" title="fragt das Gerät und wartet auf die Antwort" '
   +'onclick="pruefe(\''+esc(d.address)+'\',\''+esc(d.interface||'')+'\')">'
   +'prüfen</button> '
   +'<button class="quiet" onclick="entferne(\''+esc(d.address)+'\',\''
   +esc(d.interface||'')+'\',\''+esc(d.name||d.address)+'\')">'
   +'entfernen</button></td></tr>').join('')
   : '<tr><td colspan="7" class="empty">Noch kein Gerät angelernt.<br>'
    +'<span style="font-size:.9em">Über „Gerät anlernen" beginnen.</span></td></tr>';

  // Anlernwünsche: nur zeigen, wenn wirklich jemand ruft. Wer verstummt,
  // fällt in der Zentrale von allein heraus — hier wird nichts gemerkt.
  const pe=s.anlernwuensche||[], kp=$('#karte_post');
  if(kp){
    kp.style.display = pe.length ? '' : 'none';
    if(pe.length) $('#post').tBodies[0].innerHTML = pe.map(e=>
      '<tr><td><code>'+esc(e.hmid||e.address||'—')+'</code></td>'
     +'<td>'+esc(e.label||('Typ '+(e.devtype||'?')))
     +'<div class="mut" style="font-size:.85em">'+esc(e.hinweis||'')+'</div></td>'
     +'<td class="mut" style="white-space:nowrap">'+rufAlter(e.vor_sek)
     +(e.anzahl>1?' <span style="font-size:.85em">('+e.anzahl+'×)</span>':'')+'</td>'
     +'<td class="mut">'+esc(e.interface||'—')+'</td>'
     +'<td class="right"><button class="quiet" onclick="'
     +(e.interface==='BidCos-RF'?'oeffneBidcos()':'oeffneAnlernen()')
     +'">anlernen</button></td></tr>').join('');
  }

  // Geräte aus unserem Netz, die nicht (mehr) dazugehören.
  const vw=s.verwaist||[], kv=$('#karte_verwaist');
  if(kv){
    kv.style.display = vw.length ? '' : 'none';
    if(vw.length) $('#verwaist').tBodies[0].innerHTML = vw.map(e=>
      '<tr><td><code>'+esc(e.hmid)+'</code></td>'
     +'<td class="mut" style="white-space:nowrap">'+rufAlter(e.vor_sek)+'</td>'
     +'<td class="mut">'+e.anzahl+'</td></tr>').join('');
  }

  // Was zuletzt geschah — die kurze Fassung dessen, was sonst im Protokoll
  // zwischen tausenden Abrufen der Haussteuerung untergeht.
  // Belegstufe je Statusdatentyp: was gedeutet wird, und woher wir es wissen.
  const de=s.deutung||[], kd=$('#karte_deutung');
  if(kd){
    kd.style.display = de.length ? '' : 'none';
    if(de.length) $('#deutung').tBodies[0].innerHTML = de.map(d=>
      '<tr><td>'+esc(d.name)+' <span class="mut">('+d.sdt+')</span></td>'
      +'<td>'+esc(d.stufe)+(d.gedeutet ? '' : ' <span class="mut">— bleibt roh</span>')+'</td>'
      +'<td class="mut">'+esc(d.zeuge||'')+'</td></tr>').join('');
  }
  const ev=s.ereignisse||[], ke=$('#karte_ereignisse');
  if(ke){
    ke.style.display = ev.length ? '' : 'none';
    if(ev.length) $('#ereignisse').tBodies[0].innerHTML = ev.map(e=>
      '<tr><td class="mut" style="white-space:nowrap">'+uhr(e.zeit)+'</td>'
     +'<td'+(e.art==='bad'?' class="bad"':e.art==='warn'?' class="warn"':'')
     +'>'+esc(e.text)+'</td></tr>').join('');
  }

  const r=s.radio||{}, f=s.firmware||{};
  const anJa = f.zustand==='laeuft';
  $('#hdrstat').innerHTML='<span class="dot '+(anJa?'on':'off')+'"></span>'
    +(anJa?'Funk bereit':'kein Funk')+' · '+dv.length+' Gerät'+(dv.length==1?'':'e');

  // ⚠️ Die eigene Adresse steht in der Kachel der Schnittstelle, nicht hier.
  // Diese Karte beschreibt den STICK — was an ihm hängt, gilt für beide
  // Familien gemeinsam.
  let h='<dl class="kv">';
  if(r.v_banner) h+='<dt>Stick meldet</dt><dd><code>'+esc(r.v_banner)+'</code></dd>';
  if(r.pfad) h+='<dt>Angeschlossen</dt><dd><code class="mut">'+esc(r.pfad)+'</code></dd>';
  const c=r.counters;
  if(c){
    // ⚠️ Der Stick zählt VOR der Familienbestimmung: jeder gehörte Rahmen
    // erhöht `rx`, gleich ob HmIP oder BidCoS. Was sich nicht entschlüsseln
    // lässt, landet in `mic` — bei einem Stick, der beide Familien sieht,
    // ist das überwiegend der BidCoS-Verkehr und KEIN Fehler. Ohne diesen
    // Satz liest man den Zähler als Störungsanzeige.
    h+='<dt>Empfangen</dt><dd>'+c.rx+' <span class="mut">beide Familien</span>'
      +' · entschlüsselt '+c.ok
      +' · Quittungen '+c.acks+' · gesendet '+c.tx
      +(c.txerr?' · <span class="bad">Fehler '+c.txerr+'</span>':'')+'</dd>';
    if(c.mic) h+='<dt>Nicht für uns</dt><dd>'+c.mic
      +' <span class="mut">— meist BidCoS, kein Fehler</span></dd>';
  }
  // Rauschboden: die einzige Zahl, die einen einziehenden Störer meldet,
  // BEVOR schwache Batteriegeräte ausfallen. Fehlt sie, ist der Stick älter
  // als q-culfw 2.0.71 — dann steht hier nichts, statt einer Null zu lügen.
  // Der Mitschnitt ist nur zu holen, wenn `raw_log` an ist. Als Erweiterung
  // liegt er in /data und ist von aussen sonst NICHT erreichbar.
  const kl=document.getElementById('knopf_luft');
  if(kl){
    const mb=r.mitschnitt;
    kl.style.display = (mb===null||mb===undefined) ? 'none' : '';
    if(mb!==null&&mb!==undefined)
      kl.textContent='Rohmitschnitt laden ('+(mb/1048576).toFixed(1)+' MB)';
  }
  const fg=r.funkgute;
  if(fg && (fg.pll_fail||fg.pll_lost)){
    h+='<dt>Oszillator</dt><dd class="'+(fg.pll_fail?'bad':'warn')+'">'
      +'Lock verloren '+fg.pll_lost+' · nachgeregelt '+fg.pll_relock
      +' · aufgegeben '+fg.pll_fail+'</dd>';
  }
  const ic=r.icmp||{}, ik=Object.keys(ic);
  if(ik.length) h+='<dt>Netz-Haushalt</dt><dd class="mut">'
    +ik.map(k=>esc(k.toLowerCase().replace(/_/g,' '))+' '+ic[k]).join(' · ')+'</dd>';
  h+='</dl>';
  const b=r.budget;
  if(b){ const pct=Math.round(100*b.credit/b.max);
    // Viel Guthaben ist gut, wenig ist schlecht — die Farbe sagt das, statt
    // den Anwender zwei Zahlen ins Verhältnis setzen zu lassen.
    const kl = pct>40 ? 'ok' : (pct>15 ? 'warn' : 'bad');
    h+='<div style="margin-top:.9rem"><div class="bar" style="justify-content:space-between">'
      +'<span class="mut" style="font-size:.85rem">Sendezeit</span>'
      +'<span style="font-size:.85rem">'+b.credit+' / '+b.max
      +(b.lovf?' <span class="warn">· LOVF '+b.lovf+'</span>':'')
      +(b.on?'':' <span class="warn">· Bremse aus</span>')+'</span></div>'
      +'<div class="meter"><i class="'+kl+'" style="width:'+pct+'%"></i></div></div>'; }

  // Rauschboden auf derselben Art Leiste. Die Skala geht von −110 dBm
  // (still) bis −70 dBm (zu). Gefüllt wird, was das Rauschen vom Kanal
  // wegnimmt: je voller, desto weniger Luft bleibt für schwache Geräte.
  // Der Spitzenwert steht als Marke DARAUF — wer nur den Boden zeigt,
  // verliert den kurzen lauten Störer, den der Mittelwert nicht mitnimmt.
  if(fg && fg.noise!=null){
    const LEISE=-110, LAUT=-70;
    const anteil = v => Math.max(0, Math.min(100,
      Math.round(100*(v-LEISE)/(LAUT-LEISE))));
    const pn=anteil(fg.noise);
    const kl = fg.noise<=-100 ? 'ok' : (fg.noise<=-90 ? 'warn' : 'bad');
    let rechts='<b>'+fg.noise+' dBm</b>';
    if(fg.npk!=null) rechts+=' <span class="mut">· Spitze '+fg.npk+' dBm</span>';
    h+='<div style="margin-top:.9rem"><div class="bar" style="justify-content:space-between">'
      +'<span class="mut" style="font-size:.85rem">Rauschboden</span>'
      +'<span style="font-size:.85rem">'+rechts+'</span></div>'
      +'<div class="meter"><i class="'+kl+'" style="width:'+pn+'%"></i>'
      +(fg.npk!=null?'<b title="lautester Ausschlag im Leerlauf" style="left:'
        +anteil(fg.npk)+'%"></b>':'')
      +'</div>'
      +'<div class="skala"><span>−110 still</span><span>−90</span>'
      +'<span>−70 zu</span></div>';
    if(fg.noise > -90) h+='<p class="hint warn" style="margin:.35rem 0 0">Sehr '
      +'laut — wenig Luft für schwache Geräte.</p>';
    h+='</div>';
  }
  $('#radio').innerHTML=h;

  const p=s.pairing||{};
  if(!$('#dlgPair').open || !$('#pairinfo').querySelector('.bad'))
    $('#pairinfo').textContent = p.open ? ('Fenster offen, noch '+p.seconds_left+' s.')
                                        : (p.last||'');
  // ⚠️ „angelernt" ist NICHT das Ende des Weges. Home Assistant stellt jedes
  // frisch angelernte Geraet zurueck, bis es in den Reparaturen bestaetigt
  // wurde — wer das nicht weiss, sucht den Fehler beim Funk. Der Hinweis
  // steht deshalb genau dort, wo die Erfolgsmeldung steht.
  const nh=$('#nachher');
  if(nh){
    const fertig = !p.open && /angelernt als/.test(p.last||'');
    nh.style.display = fertig ? '' : 'none';
  }
  $('#pairgo').disabled=!!p.open;
  if(fwOffen) zeichneFirmware();
  zeichneHinweise();
}
dialogeSchliessen(); laden(); setInterval(laden,2000);
</script></body></html>
"""


FLASH_LOCK = threading.Lock()
FLASH_STATE = {"laeuft": False, "protokoll": []}


def netz_lage():
    """Wo laufen wir, und taugt „nur 127.0.0.1" hier ueberhaupt?

    ⚠️ In einem Behaelter mit eigenem Netz (Docker-Standard, `-p 2010:2010`)
    macht ein Bind auf 127.0.0.1 den Dienst UNERREICHBAR — auch von derselben
    Maschine. Der docker-proxy nimmt die Verbindung zwar an, leitet sie aber
    an die Behaelter-Adresse, wo niemand lauscht. Am Aufbau geprueft
    (20.08.2026): `bind 0.0.0.0` -> Antwort, `bind 127.0.0.1` -> verbunden,
    aber keine Daten. Eine Portpruefung meldet dabei „offen" — der Fehler
    sieht also nicht wie einer aus.

    Mit `--network host` teilt der Behaelter das Netz der Maschine; dann
    wirkt der Bind wie ausserhalb.

    Erkennung ohne Werkzeuge: `/.dockerenv` sagt „im Behaelter",
    `/sys/class/net` zeigt im eigenen Netz nur `lo` und `eth0`, im Netz der
    Maschine dagegen auch `docker0`.
    """
    import os as _os
    behaelter = _os.path.exists("/.dockerenv")
    try:
        netze = set(_os.listdir("/sys/class/net"))
    except OSError:
        netze = set()
    hostnetz = "docker0" in netze
    return {"behaelter": behaelter, "hostnetz": hostnetz,
            "localhost_moeglich": (not behaelter) or hostnetz}


def _ist_lokal(adresse):
    """Kommt der Zugriff von der Maschine selbst?"""
    return str(adresse or "").split("%")[0] in ("127.0.0.1", "::1", "localhost")


def _IST_BIDCOS_ADRESSE(text):
    """Sechs Hexziffern — die Funkadresse eines BidCoS-Geraets."""
    return len(text) == 6 and all(c in "0123456789ABCDEF" for c in text)


class WebHandler(BaseHTTPRequestHandler):
    bidcos = None
    anbindung = {}
    server_version = "QCCU-Web"
    qccu = None
    radio = None
    version = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    _HIER = os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def _bild(name):
        """Ein Bild finden — im Abbild liegt es neben dem Programm, im
        Arbeitsbaum unter assets/. Ohne beide Orte fehlt beim Lauf aus dem
        Quellverzeichnis lautlos das Logo."""
        hier = WebHandler._HIER
        for pfad in (os.path.join(hier, name), os.path.join(hier, "assets", name)):
            if os.path.exists(pfad):
                return pfad
        return os.path.join(hier, name)

    # Schriftzug hell/dunkel getrennt, weil er in beiden Faellen lesbar sein
    # muss; das Zeichen (das gruene „b") ist in beiden Marken dasselbe und
    # dient weiter als Favicon.

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path.split("?")[0] in ("/logo.png", "/logo-dark.png",
                                       "/favicon.png"):
            datei = self._bild({"/logo-dark.png": "busmatic_logo_dark.png",
                                "/favicon.png": "busware_icon.png"}
                               .get(self.path.split("?")[0],
                                    "busmatic_logo.png"))
            try:
                with open(datei, "rb") as f:
                    daten = f.read()
            except Exception:
                return self._json({"error": "kein Logo"}, 404)
            # ⚠️ Bilder liegen unter einem festen Namen, ihr INHALT kann sich
            # aber aendern — beim Wechsel von busware auf busmatic ist genau
            # das passiert, und die Browser zeigten einen Tag lang weiter das
            # alte Zeichen (`max-age=86400`, kein Merkmal zum Vergleichen).
            # Deshalb ein Fingerabdruck: der Browser fragt kurz nach, bekommt
            # in aller Regel „unveraendert" zurueck und laedt nichts — sieht
            # eine Aenderung aber sofort.
            marke = '"%s"' % hashlib.md5(daten).hexdigest()
            if self.headers.get("If-None-Match") == marke:
                self.send_response(304)
                self.send_header("ETag", marke)
                self.end_headers()
                return None
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("ETag", marke)
            self.send_header("Cache-Control", "max-age=300, must-revalidate")
            self.end_headers()
            return self.wfile.write(daten)
        if self.path.split("?")[0] == "/api/luft.log":
            # ⚠️ Erst die umgebrochene Runde, dann die laufende — sonst steht
            # die Datei verkehrt herum in der Zeit, und wer einen Fehler
            # sucht, liest die Vorgeschichte hinter dem Ereignis.
            hole = getattr(self.radio, "raw_dateien", None) if self.radio else None
            dateien = hole() if hole else []
            if not dateien:
                return self._json({"error": "Kein Rohmitschnitt — die "
                                            "Einstellung `raw_log` ist aus."}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="qccu-luft.log"')
            self.end_headers()
            for d in dateien:
                try:
                    with open(d, "rb") as f:
                        while True:
                            block = f.read(65536)
                            if not block:
                                break
                            self.wfile.write(block)
                except Exception:                            # noqa: BLE001
                    # Abbruch mitten im Strom: die Laenge steht nicht im Kopf,
                    # der Browser bekommt eben weniger. Besser als ein Fehler
                    # nach schon gesendeten Daten.
                    break
            return None

        if self.path == "/api/state":
            return self._json(self._state())
        self._json({"error": "unbekannt"}, 404)

    def _state(self):
        lc = self.qccu
        devs = []
        with lc.lock:
            items = list(lc.devices.items())
        rf = {}
        if self.radio:
            with self.radio.lock:
                rf = {a: h for h, a in self.radio.by_hmid.items()}
        jetzt = time.time()
        wartet = getattr(lc, "wartet", None)
        for addr, d in items:
            # Was der Anwender wissen will, steht bisher nur in Home
            # Assistant: lebt das Geraet noch, wie stark kommt es an, wann hat
            # es zuletzt etwas gesagt. Die Angaben liegen alle hier.
            pegel = d.values.get((0, "RSSI_DEVICE"))
            # Behutsam abfragen: die Auskunft laeuft auch gegen Zentralen, die
            # nicht jede dieser Buecher fuehren (Pruefstaende, Schwesterlinie).
            zuletzt = (getattr(d, "last_seen", None)
                       or getattr(lc, "_wert_zeit", {}).get(addr))
            benannt = getattr(lc, "name_of", None)
            devs.append({"address": addr, "rf": rf.get(addr),
                         "interface": getattr(lc, "interface_name", "HmIP-RF"),
                         "label": d.label,
                         # Die Typnummer, nicht nur die Beschriftung: Werkzeuge
                         # ordnen darueber zu (der Hoerertyp haengt am Typ,
                         # nicht am einzelnen Geraet), und aus der Beschriftung
                         # laesst sie sich nicht zurueckrechnen.
                         # ⚠️ Behutsam wie die Nachbarzeilen: eine fehlende
                         # Eigenschaft laesst diesen Handler mit AttributeError
                         # sterben, und der Aufrufer sieht dann nicht etwa
                         # einen Fehler, sondern eine abgerissene Verbindung.
                         "devtype": getattr(d, "devtype", None),
                         "name": benannt(addr, d.label) if benannt else d.label,
                         "channels": len(d.channel_list()),
                         "rssi": pegel if isinstance(pegel, (int, float)) else None,
                         "unreach": bool(getattr(d, "unreach", False)),
                         "wartet": bool(wartet(addr)) if wartet else False,
                         # ⚠️ Nur die HmIP-Schleife fuehrt das Feld. Die
                         # BidCoS-Geraete kommen weiter unten aus einem
                         # EIGENEN Wortverzeichnis; dort fehlt es, und die
                         # Vorlage zeigt es folgerichtig nur, wenn es da ist.
                         # Ein Hoerertyp ist eine HmIP-Groesse — bei einer
                         # BidCoS-Zeile waere „unbekannt" keine Auskunft,
                         # sondern eine falsche Warnung.
                         "hoerer": getattr(d, "hoerer", None),
                         "hoerer_text": getattr(d, "hoerer_text", None),
                         "vor_sek": int(jetzt - zuletzt) if zuletzt else None})
        ereignisse = getattr(lc, "ereignis_liste", None)
        wuensche = getattr(self.radio, "anlernwuensche_liste", None) if self.radio else None
        verwaist = getattr(self.radio, "verwaiste_liste", None) if self.radio else None
        out = {"version": self.version, "devices": devs,
               "ereignisse": ereignisse() if ereignisse else [],
               "anlernwuensche": wuensche() if wuensche else [],
               "verwaist": verwaist() if verwaist else [],
               "pairing": self.radio.pair_state() if self.radio
                          else {"open": False, "seconds_left": 0,
                                "next_addr": "—", "last": "kein Funk angebunden"}}
        if self.radio:
            out["radio"] = self.radio.radio_state()
        out["firmware"] = self._firmware_state()
        out["netz"] = netz_lage()
        out["anbindung"] = dict(self.anbindung)
        # ⚠️ `anbindung` ist eine Momentaufnahme vom Start — die Ports stehen
        # dort fest, und das ist richtig so. Was der Anwender seither
        # UMGESTELLT hat, liegt aber in den Einstellungen und muss von dort
        # kommen; sonst springt jedes Haekchen beim naechsten Abruf zurueck
        # und sieht aus, als bewirke es nichts (Dirk 20.08.2026).
        e = getattr(lc, "einstellungen", None)
        if isinstance(e, dict):
            for feld in ("wunsch_alt_ports", "wunsch_localhost"):
                if feld in e:
                    out["anbindung"][feld] = bool(e[feld])
        # ⚠️ Nur wenn die Schnittstelle wirklich laeuft. Eine Karte fuer einen
        # Dienst, den es nicht gibt, ist schlimmer als keine — sie laesst den
        # Anwender nach einem Schalter suchen, den niemand umlegen kann.
        # Behutsam abfragen: `_state` wird auch von Pruefstaenden und moeglichen
        # Einbettungen benutzt, die den Handler nicht vollstaendig nachbauen.
        bidcos = getattr(self, "bidcos", None)
        if bidcos is not None:
            b = bidcos.zustand()
            with bidcos.lock:
                b["geraeteliste"] = [
                    {"address": a, "label": g.label, "type": g.devtype,
                     "channels": len(g.channel_list())}
                    for a, g in bidcos.devices.items()]
            out["bidcos"] = b
            # ⚠️ Eine Liste fuer beide Familien. Zwei getrennte Tabellen
            # zwingen den Anwender, sich zu merken, wo er nachsehen muss —
            # und die Schnittstelle steht ohnehin in jeder Zeile.
            jetzt_bc = time.time()
            with bidcos.lock:
                bc_devs = list(bidcos.devices.items())
            for a, g in bc_devs:
                zuletzt = bidcos.zuletzt_gehoert(a)
                devs.append({
                    "address": a, "rf": a, "interface": b.get("interface"),
                    "label": g.typname, "name": g.typname + " " + a[-4:],
                    "channels": len(g.channel_list()),
                    "rssi": bidcos.pegel(a), "unreach": False, "wartet": False,
                    "vor_sek": int(jetzt_bc - zuletzt) if zuletzt else None})
            bc_wuensche = getattr(bidcos, "anlernwuensche_liste", None)
            if bc_wuensche:
                out["anlernwuensche"] = (out.get("anlernwuensche") or []) + bc_wuensche()
        t = getattr(lc, "t", None)
        if t is not None and hasattr(t, "zustand"):
            out["tabellen"] = t.zustand()
        # Der CUL-Zugang meldet seine Zaehler und die zuletzt VERWORFENE Zeile.
        # Das ist die Antwort auf „mein FHEM bekommt hier keine Antwort": man
        # sieht ohne Mitschnitt, was der Klient geschickt hat.
        cul = getattr(lc, "cul", None)
        if cul is not None and hasattr(cul, "zustand"):
            out["cul"] = cul.zustand()
        # Wovon die Oberflaeche abhaengt, wenn sie einen Rat gibt: in der
        # Erweiterung gibt es keine `docker run`-Zeile.
        try:
            from qccu_firmware import als_erweiterung
            out["erweiterung"] = als_erweiterung()
        except Exception:                            # noqa: BLE001
            out["erweiterung"] = False
        # Welche Statusdatentypen QCCU deutet — und woher es das weiss. Was
        # nicht hier steht, kommt als RAW_SDT<n> an; das soll der Anwender
        # sehen, ohne im Protokoll zu suchen (Belegstufe je Datentyp).
        try:
            from qccu_radio import SDT_BELEGSTUFE, DEUTEN_AB
            namen = getattr(t, "sdt_name", None)
            deutung = []
            for n, (stufe, zeuge) in sorted(SDT_BELEGSTUFE.items()):
                try:
                    name = (namen(n) if namen else None) or f"SDT{n}"
                except Exception:                    # noqa: BLE001
                    name = f"SDT{n}"
                deutung.append({"sdt": n, "name": name, "stufe": stufe,
                                "zeuge": zeuge, "gedeutet": stufe in DEUTEN_AB})
            out["deutung"] = deutung
        except Exception:                            # noqa: BLE001
            out["deutung"] = []
        return out

    def _firmware_state(self):
        """Zustand der Stick-Firmware."""
        hexp = getattr(self.qccu, "firmware_hex", None)
        if not hexp:
            return None
        try:
            import qccu_firmware as fw
        except Exception:
            return None
        with FLASH_LOCK:
            laeuft = FLASH_STATE.get("laeuft")
            protokoll = list(FLASH_STATE.get("protokoll", []))
        st = fw.status(hexp, radio=self.qccu.radio,
                       serial_path=getattr(self.qccu, "serial_path", None))
        st["laeuft"] = bool(laeuft)
        st["protokoll"] = protokoll
        return st

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "kein JSON"}, 400)

        if self.path == "/api/pair":
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            err = self.radio.start_pairing(body.get("sticker"),
                                           body.get("seconds", 60),
                                           body.get("next_addr") or None,
                                           body.get("local_key"))
            return self._json({"error": err} if err else {"ok": True},
                              409 if err else 200)

        if self.path == "/api/pruefstand":
            # Nur fuer Messreihen: Sendeparameter zur Laufzeit stellen.
            # Grenzen und Begruendung stehen in `qccu_radio.Radio`.
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            gesetzt, err = self.radio.pruefstand_setzen(body)
            return self._json({"error": err, "gesetzt": gesetzt} if err
                              else {"ok": True, "gesetzt": gesetzt},
                              409 if err else 200)

        if self.path == "/api/pruefstand/statusfrage":
            # Einen Kanal nach seinem Zustand fragen — die trennscharfe Probe,
            # wenn ein Geraet auf Befehle schweigt.
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            adresse = str(body.get("address") or "").upper()
            with self.radio.lock:
                rf = {a: h for h, a in self.radio.by_hmid.items()}.get(adresse)
            if not rf:
                return self._json({"error": "Geraet hat keine Funkadresse"}, 409)
            try:
                kanal = int(body.get("channel", 0))
            except (TypeError, ValueError):
                return self._json({"error": "channel ist keine Zahl"}, 400)
            erg = self.radio.status_anfragen(rf, kanal)
            return self._json({"ok": True, "antwortet": erg})

        if self.path == "/api/pruefstand/zentralenlink":
            # Die Verknuepfung zur Zentrale fuer ein schon angelerntes Geraet
            # nachholen (Sender-Kanaele laut Tabelle, oder `channels`).
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            kanaele = self.radio.zentralen_verknuepfen(
                str(body.get("address", "")), body.get("channels"))
            return self._json({"ok": True, "kanaele": kanaele})

        if self.path == "/api/stick/roh":
            # Nur fuer den Pruefstand: ein Kommando aus der weissen Liste an
            # den Stick. Die Liste steht in `qccu_radio.Radio`, damit die
            # Begruendung bei dem steht, was sie schuetzt.
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            err = self.radio.roh_kommando(body.get("cmd"))
            return self._json({"error": err} if err else {"ok": True},
                              409 if err else 200)

        if self.path == "/api/pair/stop":
            if self.radio:
                self.radio.stop_pairing()
            return self._json({"ok": True})

        if self.path == "/api/ports":
            e = getattr(self.qccu, "einstellungen", None)
            if e is None:
                return self._json({"error": "nicht verfuegbar"}, 409)
            lokal = bool(body.get("localhost"))
            lage = netz_lage()
            if lokal and not lage["localhost_moeglich"]:
                return self._json({"error": "In diesem Behaelter wuerde das "
                                            "die Dienste unerreichbar machen "
                                            "— auch von dieser Maschine. Nur "
                                            "sinnvoll mit --network host."},
                                  409)
            # ⚠️ Wer von aussen zugreift und „nur 127.0.0.1" einschaltet,
            # sperrt beim naechsten Start seine eigene Gegenstelle aus. Die
            # Oberflaeche bleibt zwar erreichbar, die Dienste nicht — und
            # genau die benutzt er gerade.
            von = (self.client_address or ("",))[0]
            if lokal and not e.get("localhost") and not _ist_lokal(von):
                return self._json({"error": "Nicht von aussen einschaltbar: "
                                            f"dieser Zugriff kommt von {von}. "
                                            "Danach waeren die Dienste nur "
                                            "noch auf der Zentrale selbst "
                                            "erreichbar."}, 409)
            e["wunsch_alt_ports"] = bool(body.get("alt_ports"))
            e["wunsch_localhost"] = lokal
            sichern = getattr(self.qccu, "save_store", None)
            if sichern:
                sichern()
            return self._json({"ok": True, **e})

        if self.path in ("/api/bidcos/pair", "/api/bidcos/pair/stop"):
            bidcos = getattr(self, "bidcos", None)
            if bidcos is None:
                return self._json({"error": "Die Schnittstelle BidCos-RF ist "
                                            "nicht eingeschaltet."}, 409)
            if self.path.endswith("/stop"):
                bidcos.setInstallMode(False)
                return self._json({"ok": True, "offen": 0})
            # ⚠️ Ohne Sendeerlaubnis waere das Fenster eine Luege: die
            # Schnittstelle hoerte den Anlernruf, koennte aber nicht
            # antworten. Lieber hier abweisen als den Benutzer warten lassen.
            if not bidcos.zentrale.senden_erlaubt:
                return self._json({"error": "Die Schnittstelle sendet nicht "
                                            "und kann nicht anlernen."}, 409)
            ziel = str(body.get("address") or "").strip().upper() or None
            if ziel and not _IST_BIDCOS_ADRESSE(ziel):
                return self._json({"error": "Die Geraeteadresse besteht aus "
                                            "sechs Hexziffern, z.B. 1A2B3C."}, 400)
            try:
                sek = max(1, min(3600, int(body.get("seconds", 60))))
            except (TypeError, ValueError):
                return self._json({"error": "Die Dauer ist keine Zahl."}, 400)
            bidcos.setInstallMode(True, sek, 1, ziel)
            return self._json({"ok": True, "ziel": ziel,
                               "offen": bidcos.zentrale.anlernen_offen()})

        if self.path == "/api/device/aufnehmen":
            # Der Notausgang fuer Gegenstellen ohne Posteingang (FHEM/HMCCU):
            # was hier freigegeben wird, geht als `newDevices` hinaus.
            adresse = str(body.get("address") or "").upper()
            aufnehmen = getattr(self.qccu, "aufnehmen", None)
            if aufnehmen is None:
                return self._json({"error": "nicht verfuegbar"}, 409)
            return self._json({"ok": True, "gemeldet": bool(aufnehmen(adresse))})

        if self.path == "/api/device/hoerer":
            # Den Hoerertyp eines BESTANDSGERAETS nachtragen. Er steht nur im
            # Anlernruf; ein Geraet neu anzulernen, nur um ihn zu erfahren,
            # kostet bei einem Heizungsregler die Ventiladaption. Darum dieser
            # Weg — bedient von `scripts/hoerertyp_aus_mitschnitt.py`.
            adresse = str(body.get("address") or "").upper()
            setzen = getattr(self.qccu, "set_hoerer", None)
            if setzen is None:
                return self._json({"error": "nicht verfuegbar"}, 409)
            roh = body.get("opmode")
            if roh is None:
                opmode = None
            else:
                try:
                    opmode = int(roh, 16) if isinstance(roh, str) else int(roh)
                except (TypeError, ValueError):
                    return self._json({"error": "Der Betriebsmodus ist "
                                                "keine Zahl."}, 400)
            quelle = str(body.get("quelle") or "hand")
            if not setzen(adresse, opmode, quelle):
                return self._json({"error": "Gerät unbekannt oder "
                                            "Betriebsmodus ungültig."}, 400)
            return self._json({"ok": True, "address": adresse,
                               "opmode": opmode})

        if self.path == "/api/ereignisse/loeschen":
            # „Zuletzt geschehen" ist eine Erinnerungshilfe, kein Protokoll:
            # sie darf geleert werden. Das Protokoll der Zentrale bleibt
            # davon unberuehrt — dort steht der Verlauf weiter.
            leeren = getattr(self.qccu, "ereignisse_leeren", None)
            if leeren is None:
                return self._json({"error": "nicht verfuegbar"}, 409)
            return self._json({"ok": True, "geloescht": leeren()})

        if self.path == "/api/firmware/flash":
            return self._json(self._flash_start())

        if self.path == "/api/device/delete":
            addr = (body.get("address") or "").upper()
            # ⚠️ Seit die Geraeteliste beide Familien fuehrt, muss der
            # Loeschweg sie auseinanderhalten. Ohne das lief JEDER Aufruf in
            # die HmIP-Zentrale, die eine BidCoS-Adresse gar nicht kennt: die
            # Antwort war „ok", geloescht wurde nichts.
            bidcos = getattr(self, "bidcos", None)
            if bidcos is not None and body.get("interface") == bidcos.interface_name:
                with bidcos.lock:
                    bekannt = addr in bidcos.devices
                if not bekannt:
                    return self._json({"error": f"{addr} steht nicht im "
                                                f"BidCoS-Bestand."}, 409)
                bidcos.deleteDevices(None, [addr])
                return self._json({"ok": True})
            self.qccu.deleteDevices(addr)
            return self._json({"ok": True})

        # Der Ausweg aus „Stick ohne Schluessel, aber Geraete eingetragen":
        # alles verwerfen und neu beginnen. Mit Sicherung — der Aufruf
        # verlangt `bestaetigt`, damit ihn kein Fehlgriff ausloest.
        if self.path == "/api/bidcos/ping":
            bidcos = getattr(self, "bidcos", None)
            if bidcos is None:
                return self._json({"error": "BidCos-RF ist nicht "
                                            "eingeschaltet."}, 409)
            if not bidcos.zentrale.senden_erlaubt:
                return self._json({"error": "Die Schnittstelle sendet nicht."},
                                  409)
            adresse = str(body.get("address") or "").upper()
            antwortet = bidcos.erreichbar(adresse)
            if antwortet is None:
                return self._json({"error": f"{adresse} steht nicht im "
                                            f"BidCoS-Bestand."}, 409)
            merke = getattr(self.qccu, "merke_ereignis", None)
            if merke:
                merke("ok" if antwortet else "warn",
                      f"BidCoS {adresse} "
                      + ("antwortet" if antwortet else "antwortet nicht"))
            return self._json({"ok": True, "antwortet": bool(antwortet)})

        if self.path == "/api/device/ping":
            # Aktiv nachsehen, ob ein Geraet noch antwortet. Gesendet wird die
            # Uhrzeit; die Antwort ist die Kurzquittung des Geraets.
            if not self.radio:
                return self._json({"error": "kein Funk angebunden"}, 409)
            adresse = str(body.get("address") or "").upper()
            with self.radio.lock:
                rf = {a: h for h, a in self.radio.by_hmid.items()}.get(adresse)
            if not rf:
                return self._json({"error": "Geraet hat keine Funkadresse — "
                                            "es wurde noch nie gehoert."}, 409)
            # ⚠️ Ein Batteriegeraet beantwortet die Probe nicht (es hoert nur
            # in seinem eigenen Takt) — das ist keine Aussage ueber seine
            # Erreichbarkeit und darf nicht als Fehler aussehen.
            if not self.radio.ping_moeglich(rf):
                return self._json(
                    {"ok": True, "antwortet": None,
                     "hinweis": "Das Geraet hoert nur in seinem eigenen Takt und "
                                "beantwortet keine Nachfrage. Ob es lebt, zeigt "
                                "sein naechster eigener Rahmen."})
            antwortet = self.radio.erreichbarkeit_pruefen(rf)
            if antwortet is None:
                return self._json({"error": "Geraet ist dem Funkpfad unbekannt."}, 409)
            name = self.qccu.name_of(adresse, adresse)
            self.qccu.merke_ereignis("ok" if antwortet else "warn",
                                     f"{name} " + ("antwortet" if antwortet
                                                   else "antwortet nicht"))
            return self._json({"ok": True, "antwortet": bool(antwortet)})

        if self.path == "/api/netz/neu":
            # Die Bestaetigung wird ZUERST geprueft: ein unbestaetigter Aufruf
            # soll immer abgewiesen werden, auch wenn gerade gar kein Funkpfad
            # da ist. Sonst haengt die Sicherung am Zustand der Anlage.
            if not body.get("bestaetigt"):
                return self._json({"ok": False, "meldung": "nicht bestaetigt"}, 400)
            r = getattr(self.qccu, "radio", None)
            if r is None or not hasattr(r, "netz_neu_beginnen"):
                return self._json({"ok": False, "meldung": "Kein Funkpfad"}, 409)
            ok, meldung = r.netz_neu_beginnen()
            return self._json({"ok": ok, "meldung": meldung}, 200 if ok else 500)

        self._json({"error": "unbekannt"}, 404)


    def _flash_start(self):
        """Firmware einspielen — im Hintergrund, damit die Seite antwortet."""
        with FLASH_LOCK:
            if FLASH_STATE.get("laeuft"):
                return {"error": "laeuft bereits"}
            FLASH_STATE["laeuft"] = True
            FLASH_STATE["protokoll"] = []

        qccu_obj, klasse = self.qccu, type(self)

        def sag(zeile):
            with FLASH_LOCK:
                FLASH_STATE["protokoll"].append(zeile)
            print(f"  [Firmware] {zeile}", flush=True)

        def lauf():
            import qccu_firmware as fw
            lc = qccu_obj
            hexp = getattr(lc, "firmware_hex", None)
            spath = getattr(lc, "serial_path", None)
            # War der Stick VOR dem Einspielen angebunden, ist es eine
            # Aktualisierung DESSELBEN Sticks — dann bleibt seine Seriennummer
            # verbindlich. Sonst greift die Suche waehrend seiner
            # Bootlader-Phase den erstbesten anderen q-culfw am Rechner, merkt
            # sich DEN als Zentrale und die Anlage haengt stillschweigend an
            # einem fremden Stick mit fremdem Netzwerkschluessel (17.08.2026,
            # zwei Sticks am Rechner). Nur beim Erstflash aus dem Bootlader
            # (kein Funk vorher) darf ohne Seriennummer gesucht werden.
            aktualisierung = lc.radio is not None
            try:
                if lc.radio is not None:
                    sag("Funk wird angehalten, Stick geht in den Bootlader …")
                    if not fw.to_bootloader(lc.radio, spath):
                        sag("Der Bootlader meldet sich nicht — abgebrochen.")
                        return
                    lc.radio = None
                    klasse.radio = None
                ok, _ = fw.flash(hexp, log=sag)
                if not ok:
                    return
                sag("Warte auf den Stick …")
                # Erstflash aus dem Bootlader: wer hier eingespielt hat, meint
                # DIESEN Stick — auch wenn die Anlage bisher an einem anderen
                # hing; die gemerkte Seriennummer wuerde ihn sonst ausschliessen.
                # Bei einer AKTUALISIERUNG dagegen bleibt sie stehen: gewartet
                # wird auf genau den Stick, der eben noch lief.
                if not aktualisierung:
                    lc.stick_serial = None
                rb = getattr(lc, "rebind_radio", None)
                neu = rb() if rb else None
                # ⚠️ Der Verlust wird in „Zuletzt geschehen" vermerkt
                # (qccu_radio), die Rueckkehr stand bisher NUR im
                # Einspiel-Protokoll. Damit blieb „Funkzugang zum Stick
                # verloren" als juengster Eintrag stehen, waehrend der Funk
                # laengst wieder lief und der Punkt oben gruen zeigte — ein
                # Widerspruch, den niemand aufloesen kann (Dirk 20.08.2026).
                merke = getattr(lc, "merke_ereignis", None)
                if neu:
                    klasse.radio = neu
                    sag("Funk wieder angebunden.")
                    if merke:
                        merke("ok", "Stick wieder angebunden")
                else:
                    sag("Firmware eingespielt. Der Stick meldet sich noch "
                        "nicht — bitte kurz warten.")
                    if merke:
                        merke("warn", "Firmware eingespielt, aber der Stick "
                                      "meldet sich noch nicht.")
            except Exception as ex:
                sag(f"Fehler: {ex}")
            finally:
                with FLASH_LOCK:
                    FLASH_STATE["laeuft"] = False

        threading.Thread(target=lauf, daemon=True).start()
        return {"ok": True}


def serve(qccu, radio, version, bind="0.0.0.0", port=8080, anbindung=None,
          bidcos=None):
    """`anbindung` traegt die Angaben, die Home Assistant beim Einrichten
    verlangt (Host, JSON-Port, XML-RPC-Port, Schnittstellenname). Sie stehen
    nur hier zur Verfuegung — die Oberflaeche kennt die uebrigen Dienste
    sonst nicht, und geraten wird nichts."""
    WebHandler.qccu = qccu
    WebHandler.radio = radio
    WebHandler.version = version
    WebHandler.anbindung = anbindung or {}
    # Die zweite Schnittstelle. Ohne sie bleibt die Karte verborgen und der
    # Hinweis fuer Home Assistant nennt wie bisher nur HmIP-RF.
    WebHandler.bidcos = bidcos
    srv = ThreadingHTTPServer((bind, port), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
