#!/usr/bin/env python3
"""Webfrontend der QCCU."""
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
    <source srcset="/logo-dark.png" media="(prefers-color-scheme: dark)">
    <img src="logo.png" alt="busware" class="logo" onerror="this.parentNode.remove()">
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

<div class="card">
  <h2>Geräte</h2>
  <div class="body flush"><table id="devs"><thead><tr>
    <th>Adresse</th><th>Typ</th><th>Funk</th><th class="right">Kanäle</th><th></th>
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

async function post(u,b){
  await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                 body:JSON.stringify(b||{})});
  return laden();
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

async function laden(){
  let s; try{ s=await (await fetch('api/state')).json(); }catch(e){ return; }
  ZUSTAND=s;
  $('#ver').textContent=s.version||'';
  // Die Angaben, die Home Assistant beim Einrichten verlangt. Sie kommen aus
  // dem laufenden Dienst, nicht aus der Anleitung — sonst stimmen sie nicht
  // mehr, sobald jemand einen Port aendert.
  // Fehlen die Gerätebeschreibungen, ist das die wichtigste Nachricht der
  // Seite — sie steht deshalb ganz oben und nur dann.
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
    '<tr><td><code>'+esc(d.address)+'</code></td><td>'+esc(d.label)+'</td>'
   +'<td><code class="mut">'+esc(d.rf||'—')+'</code></td>'
   +'<td class="right">'+d.channels+'</td>'
   +'<td class="right"><button class="quiet" onclick="if(confirm(\'Gerät '
   +esc(d.address)+' entfernen?\'))post(\'api/device/delete\',{address:\''
   +esc(d.address)+'\'})">entfernen</button></td></tr>').join('')
   : '<tr><td colspan="5" class="empty">Noch kein Gerät angelernt.<br>'
    +'<span style="font-size:.9em">Über „Gerät anlernen" beginnen.</span></td></tr>';

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
  $('#pairinfo').textContent = p.open ? ('Fenster offen, noch '+p.seconds_left+' s.')
                                      : (p.last||'');
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
    LOGO = os.path.join(_HIER, "busware_logo.png")
    LOGO_DARK = os.path.join(_HIER, "busware_logo_dark.png")
    ICON = os.path.join(_HIER, "busware_icon.png")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path in ("/logo.png", "/logo-dark.png", "/favicon.png"):
            datei = {"/logo-dark.png": self.LOGO_DARK,
                     "/favicon.png": self.ICON}.get(self.path, self.LOGO)
            try:
                with open(datei, "rb") as f:
                    daten = f.read()
            except Exception:
                return self._json({"error": "kein Logo"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(daten)))
            self.send_header("Cache-Control", "max-age=86400")
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
        for addr, d in items:
            devs.append({"address": addr, "rf": rf.get(addr),
                         "label": d.label, "channels": len(d.channel_list())})
        out = {"version": self.version, "devices": devs,
               "pairing": self.radio.pair_state() if self.radio
                          else {"open": False, "seconds_left": 0,
                                "next_addr": "—", "last": "kein Funk angebunden"}}
        if self.radio:
            out["radio"] = self.radio.radio_state()
        out["firmware"] = self._firmware_state()
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
                # Wer hier eingespielt hat, meint DIESEN Stick — auch wenn die
                # Anlage bisher an einem anderen hing. Ohne das Vergessen wuerde
                # der frisch eingespielte nicht uebernommen: die Suche liesse
                # nur die gemerkte Seriennummer zu. Auf Platte bleibt die alte
                # stehen, bis der neue wirklich antwortet.
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
