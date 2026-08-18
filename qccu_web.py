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
.bar{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}

input{font:inherit;padding:.55rem .7rem;border:1px solid var(--line);border-radius:7px;
  background:var(--bg);color:var(--fg);width:100%}
input:focus{outline:2px solid var(--acc);outline-offset:-1px;border-color:transparent}
label{display:block;font-size:.82rem;color:var(--mut);margin:0 0 .3rem}
.field{margin-bottom:.9rem}
.hint{font-size:.82rem;color:var(--mut);margin:.35rem 0 0}

code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
a{color:var(--acc)}
.mut{color:var(--mut)} .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
  background:var(--mut);margin-right:.4rem;vertical-align:.05rem}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}

.meter{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:.4rem}
.meter>i{display:block;height:100%;background:var(--acc);transition:width .3s}
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
    <button class="primary" onclick="oeffneAnlernen()">Gerät anlernen</button>
    <button onclick="oeffneFirmware()">Stick-Firmware</button>
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
      <tr><td>Schnittstellen</td><td>nur <code id="ha_if">HmIP-RF</code> anhaken</td></tr>
      <tr><td>Eigene Ports setzen</td><td>JSON <code id="ha_json">—</code>,
        <code id="ha_if2">HmIP-RF</code> <code id="ha_rpc">—</code></td></tr>
    </tbody></table>
    <p class="hint" id="ha_warn"></p>
  </div>
</div>

<div class="card">
  <h2>Funk</h2><div class="body" id="radio"></div>
</div>

<div class="card" id="karte_ereignisse" style="display:none">
  <h2>Zuletzt geschehen</h2>
  <div class="body flush"><table id="ereignisse"><tbody></tbody></table></div>
</div>

<p id="meldung" style="display:none"></p>

<div class="card" id="karte_integration" style="display:none">
  <h2>Integration in Home Assistant</h2>
  <div class="body">
    <p id="int_text"></p>
    <p class="hint" id="int_hint"></p>
    <p id="int_knopf" style="display:none">
      <button class="primary" onclick="haNeustart()">Home Assistant neu starten</button>
      <span class="hint" id="int_msg"></span></p>
  </div>
</div>

<div class="card" id="karte_post" style="display:none">
  <h2>Posteingang</h2>
  <div class="body">
    <p class="hint">Geräte, die gerade angelernt werden <b>wollen</b> — sie
      haben eben ihre Anlerntaste gesehen. QCCU kann sie nur mit dem Schlüssel
      vom Aufkleber aufnehmen; ohne ihn bleibt es beim Zusehen.</p>
    <div class="body flush"><table id="post"><tbody></tbody></table></div>
  </div>
</div>

<div class="card">
  <h2>Geräte</h2>
  <div class="body flush"><table id="devs"><thead><tr>
    <th>Name</th><th>Adresse</th><th>Typ</th><th>Zuletzt gehört</th>
    <th class="right">Pegel</th><th></th>
  </tr></thead><tbody></tbody></table></div>
</div>

<dialog id="dlgPair">
  <h3>Gerät anlernen</h3>
  <div class="dbody">
    <div class="field">
      <label for="key">Key auf dem Aufkleber (nicht SGTIN)</label>
      <input id="key" placeholder="XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX" autocomplete="off">
      <p class="hint">Der Aufkleber steht auf dem Gerät (26 Zeichen). Wer den
         Schlüssel als 32 Hexziffern hat, kann ihn ebenso eingeben.</p>
    </div>
    <div class="field">
      <label for="secs">Anlernfenster</label>
      <input id="secs" type="number" value="60" min="10" max="600">
      <p class="hint">So lange nimmt die Zentrale ein Gerät an. Danach schließt
         sie von selbst. Die Funkadresse vergibt sie dabei selbst.</p>
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
function uhr(t){
  const d=new Date(t*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)
        +':'+('0'+d.getSeconds()).slice(-2);
}

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
function oeffneFirmware(){ fwOffen=true; zeichneFirmware(); $('#dlgFw').showModal(); }
$('#dlgFw')?.addEventListener('close',()=>{fwOffen=false;});

// Die Funkadresse vergibt die Zentrale — sie ist keine Eingabe. Ein Feld dafür
// lädt nur dazu ein, eine bereits belegte zu erraten.
function pair(){
  const v=$('#key').value.trim(), plain=v.replace(/[\s-]/g,'');
  if(!plain){ $('#pairinfo').innerHTML='<span class="bad">Bitte Aufkleber oder Schlüssel eingeben.</span>'; return; }
  const istHex=/^[0-9a-fA-F]{32}$/.test(plain);
  post('api/pair',{sticker:istHex?null:v, local_key:istHex?plain:null,
                    seconds:+$('#secs').value});
}

async function haNeustart(){
  const m=$('#int_msg');
  if(!confirm('Home Assistant jetzt neu starten?\n\nDie Oberfläche ist dabei '
      +'kurz nicht erreichbar; QCCU läuft weiter.')) return;
  if(m){ m.textContent='läuft …'; m.className='hint'; }
  const j=await post('api/ha/neustart');
  if(m) m.textContent = (j&&j.ok) ? 'Neustart angestoßen.' : '';
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
  if(p.open) n.push(['act','','<b>Anlernfenster offen</b>Noch '+p.seconds_left
    +' s. '+esc(p.last||''),'Abbrechen',"post('api/pair/stop')"]);
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
  setz('ha_if', ab.interface||'HmIP-RF');
  setz('ha_if2', ab.interface||'HmIP-RF');
  setz('ha_json', ab.json_port ? String(ab.json_port) : 'aus');
  setz('ha_rpc', ab.rpc_port ? String(ab.rpc_port) : '—');
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
   +(d.unreach?' <span class="warn" title="meldet sich nicht">·</span>':'')+'</td>'
   +'<td><code>'+esc(d.address)+'</code>'
   +'<span class="mut" style="font-size:.85em"> · '+esc(d.rf||'—')+'</span></td>'
   +'<td class="mut">'+esc(d.label)+' · '+d.channels+' Kan.</td>'
   +'<td class="mut">'+alterText(d.vor_sek)+'</td>'
   +'<td class="right mut">'+(d.rssi==null?'—':(d.rssi+' dBm'))+'</td>'
   +'<td class="right"><button class="quiet" onclick="if(confirm(\'Gerät '
   +esc(d.name||d.address)+' entfernen?\\n\\nEs bekommt dabei einen '
   +'Funk-Ausschluss und lässt sich danach ohne Werksreset neu anlernen.\'))'
   +'post(\'api/device/delete\',{address:\''
   +esc(d.address)+'\'})">entfernen</button></td></tr>').join('')
   : '<tr><td colspan="6" class="empty">Noch kein Gerät angelernt.<br>'
    +'<span style="font-size:.9em">Über „Gerät anlernen" beginnen.</span></td></tr>';

  // Die mitgelieferte Integration — nur in der Erweiterung ueberhaupt ein
  // Thema, und nur dann eine Karte, wenn es etwas zu sagen gibt.
  const ig=s.integration||{}, ki=$('#karte_integration');
  if(ki){
    ki.style.display = ig.verfuegbar ? '' : 'none';
    if(ig.verfuegbar){
      const t=$('#int_text'), h=$('#int_hint'), k=$('#int_knopf');
      if(!ig.installiert){
        t.innerHTML='<span class="warn">Sie ist nicht eingerichtet.</span>';
        h.textContent='Mitgeliefert wäre '+esc(ig.mitgeliefert)
          +'. Ist das Mitliefern abgeschaltet, bleibt der Weg über HACS.';
        k.style.display='none';
      } else if(ig.installiert===ig.mitgeliefert){
        t.innerHTML='Fassung <b>'+esc(ig.installiert)+'</b> liegt bereit.';
        h.textContent='Sie stammt aus diesem Abbild. Erscheint sie in Home '
          +'Assistant nicht unter „Geräte & Dienste", fehlt noch ein Neustart.';
        k.style.display='';
      } else {
        t.innerHTML='Eingerichtet ist <b>'+esc(ig.installiert)+'</b>, '
          +'mitgeliefert wäre '+esc(ig.mitgeliefert)+'.';
        h.textContent='Die vorhandene ist neuer und bleibt unangetastet — so '
          +'gehört es sich, wenn HACS sie pflegt.';
        k.style.display='none';
      }
    }
  }

  // Posteingang: nur zeigen, wenn wirklich jemand anklopft.
  const pe=s.posteingang||[], kp=$('#karte_post');
  if(kp){
    kp.style.display = pe.length ? '' : 'none';
    if(pe.length) $('#post').tBodies[0].innerHTML = pe.map(e=>
      '<tr><td><code>'+esc(e.hmid||e.address||'—')+'</code></td>'
     +'<td>'+esc(e.label||('Typ '+(e.devtype||'?')))+'</td>'
     +'<td class="mut">'+esc(e.hinweis||'')+'</td>'
     +'<td class="right"><button class="quiet" onclick="oeffneAnlernen()">'
     +'anlernen</button></td></tr>').join('');
  }

  // Was zuletzt geschah — die kurze Fassung dessen, was sonst im Protokoll
  // zwischen tausenden Abrufen der Haussteuerung untergeht.
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

  let h='<dl class="kv"><dt>Eigene Adresse</dt><dd><code>'+esc(r.own_addr||'—')+'</code></dd>';
  const c=r.counters;
  if(c) h+='<dt>Empfangen</dt><dd>'+c.rx+' · entschlüsselt '+c.ok
        +' · Quittungen '+c.acks+' · gesendet '+c.tx
        +(c.txerr?' · <span class="bad">Fehler '+c.txerr+'</span>':'')+'</dd>';
  const ic=r.icmp||{}, ik=Object.keys(ic);
  if(ik.length) h+='<dt>Netz-Haushalt</dt><dd class="mut">'
    +ik.map(k=>esc(k.toLowerCase().replace(/_/g,' '))+' '+ic[k]).join(' · ')+'</dd>';
  h+='</dl>';
  const b=r.budget;
  if(b){ const pct=Math.round(100*b.credit/b.max);
    h+='<div style="margin-top:.9rem"><div class="bar" style="justify-content:space-between">'
      +'<span class="mut" style="font-size:.85rem">Sendezeit</span>'
      +'<span style="font-size:.85rem">'+b.credit+' / '+b.max
      +(b.lovf?' <span class="warn">· LOVF '+b.lovf+'</span>':'')
      +(b.on?'':' <span class="warn">· Bremse aus</span>')+'</span></div>'
      +'<div class="meter"><i style="width:'+pct+'%"></i></div></div>'; }
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
laden(); setInterval(laden,2000);
</script></body></html>
"""


FLASH_LOCK = threading.Lock()
FLASH_STATE = {"laeuft": False, "protokoll": []}


class WebHandler(BaseHTTPRequestHandler):
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
                         "label": d.label,
                         "name": benannt(addr, d.label) if benannt else d.label,
                         "channels": len(d.channel_list()),
                         "rssi": pegel if isinstance(pegel, (int, float)) else None,
                         "unreach": bool(getattr(d, "unreach", False)),
                         "vor_sek": int(jetzt - zuletzt) if zuletzt else None})
        ereignisse = getattr(lc, "ereignis_liste", None)
        posteingang = getattr(self.radio, "inbox_liste", None) if self.radio else None
        out = {"version": self.version, "devices": devs,
               "ereignisse": ereignisse() if ereignisse else [],
               "posteingang": posteingang() if posteingang else [],
               "pairing": self.radio.pair_state() if self.radio
                          else {"open": False, "seconds_left": 0,
                                "next_addr": "—", "last": "kein Funk angebunden"}}
        if self.radio:
            out["radio"] = self.radio.radio_state()
        out["firmware"] = self._firmware_state()
        out["integration"] = self._integration_state()
        out["anbindung"] = dict(self.anbindung)
        t = getattr(lc, "t", None)
        if t is not None and hasattr(t, "zustand"):
            out["tabellen"] = t.zustand()
        # Wovon die Oberflaeche abhaengt, wenn sie einen Rat gibt: in der
        # Erweiterung gibt es keine `docker run`-Zeile.
        try:
            from qccu_firmware import als_erweiterung
            out["erweiterung"] = als_erweiterung()
        except Exception:                            # noqa: BLE001
            out["erweiterung"] = False
        return out

    def _integration_state(self):
        """Was mit der mitgelieferten Integration ist.

        Sie wird beim Start abgelegt (siehe entrypoint.sh); hier wird nur
        NACHGESEHEN, was dabei herauskam — die Oberflaeche entscheidet nichts
        und aendert nichts. Ausserhalb einer Erweiterung gibt es das
        Verzeichnis von Home Assistant gar nicht; dann bleibt die Karte weg.
        """
        try:
            import qccu_integration as QI
        except Exception:                            # noqa: BLE001
            return {"verfuegbar": False}
        ha = os.environ.get("HA_CONFIG", "/homeassistant")
        quelle = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "integration", QI.DOMAIN)
        mit = QI.fassung_von(quelle)
        if not mit or not os.path.isdir(ha):
            return {"verfuegbar": False}
        ziel = os.path.join(ha, "custom_components", QI.DOMAIN)
        da = QI.fassung_von(ziel)
        return {"verfuegbar": True, "mitgeliefert": mit, "installiert": da,
                # „Neustart faellig" heisst: auf der Platte liegt etwas
                # anderes, als Home Assistant geladen hat. Das laesst sich von
                # hier nicht sehen — deshalb meldet es der Start, und die
                # Oberflaeche zeigt nur, was liegt.
                "neuer_stand": bool(da and da == mit)}

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

        if self.path == "/api/pair/stop":
            if self.radio:
                self.radio.stop_pairing()
            return self._json({"ok": True})

        if self.path == "/api/firmware/flash":
            return self._json(self._flash_start())

        if self.path == "/api/device/delete":
            addr = (body.get("address") or "").upper()
            self.qccu.deleteDevices(addr)
            return self._json({"ok": True})

        # Der Ausweg aus „Stick ohne Schluessel, aber Geraete eingetragen":
        # alles verwerfen und neu beginnen. Mit Sicherung — der Aufruf
        # verlangt `bestaetigt`, damit ihn kein Fehlgriff ausloest.
        if self.path == "/api/ha/neustart":
            # ⚠️ Home Assistant laedt Integrationen NUR beim Start. Nach dem
            # Ablegen ist ein Neustart faellig — und ihn hier anzubieten ist
            # freundlicher, als den Anwender ins Menue zu schicken. Ausgeloest
            # wird er ueber die Supervisor-Schnittstelle (`hassio_role:
            # homeassistant`), nicht von uns selbst.
            token = os.environ.get("SUPERVISOR_TOKEN")
            if not token:
                return self._json({"error": "Das geht nur in der Erweiterung "
                                            "von Home Assistant."}, 409)
            try:
                import urllib.request
                req = urllib.request.Request(
                    "http://supervisor/core/restart", data=b"",
                    headers={"Authorization": "Bearer " + token}, method="POST")
                with urllib.request.urlopen(req, timeout=30) as r:
                    r.read()
            except Exception as ex:                  # noqa: BLE001
                return self._json({"error": f"Neustart nicht ausgeloest: {ex}"}, 502)
            self.qccu.merke_ereignis("info", "Neustart von Home Assistant "
                                             "ausgeloest (Integration laden)")
            return self._json({"ok": True})

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
                if neu:
                    klasse.radio = neu
                    sag("Funk wieder angebunden.")
                else:
                    sag("Firmware eingespielt. Der Stick meldet sich noch "
                        "nicht — bitte kurz warten.")
            except Exception as ex:
                sag(f"Fehler: {ex}")
            finally:
                with FLASH_LOCK:
                    FLASH_STATE["laeuft"] = False

        threading.Thread(target=lauf, daemon=True).start()
        return {"ok": True}


def serve(qccu, radio, version, bind="0.0.0.0", port=8080, anbindung=None):
    """`anbindung` traegt die Angaben, die Home Assistant beim Einrichten
    verlangt (Host, JSON-Port, XML-RPC-Port, Schnittstellenname). Sie stehen
    nur hier zur Verfuegung — die Oberflaeche kennt die uebrigen Dienste
    sonst nicht, und geraten wird nichts."""
    WebHandler.qccu = qccu
    WebHandler.radio = radio
    WebHandler.version = version
    WebHandler.anbindung = anbindung or {}
    srv = ThreadingHTTPServer((bind, port), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
