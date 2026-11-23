# Evidence Browser

A browsable view of the merged evidence-packet store. Each packet holds one claim
about a Mondo term (a synonym, a subclass axiom or a mapping) with its evidence lines
from the sources Mondo integrates, scored by the SIEVE inclusion score S in [-1, 1].
Click a row for the statement's details.

!!! note "Regenerated on deploy"
    The data is exported from the committed store on every docs deploy. Evidence
    lines themselves are not shown here; run the web app (`just serve-web`) for the
    full packet view.

<div id="eb-app" data-src="data.json">
  <p id="eb-loading">Loading evidence packets…</p>
</div>

<style>
#eb-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin:1rem 0}
.eb-card{border:1px solid var(--md-default-fg-color--lightest);border-radius:.4rem;padding:.75rem 1rem}
.eb-card .eb-num{font-size:1.6rem;font-weight:700;color:var(--md-primary-fg-color);line-height:1.1}
.eb-card .eb-lbl{font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;color:var(--md-default-fg-color--light)}
.eb-bars{display:flex;flex-wrap:wrap;gap:.4rem;margin:.25rem 0 1rem}
.eb-chip{font-size:.78rem;border:1px solid var(--md-default-fg-color--lightest);border-radius:1rem;padding:.15rem .6rem}
.eb-controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:1rem 0 .5rem}
.eb-controls input,.eb-controls select{padding:.4rem .5rem;border:1px solid var(--md-default-fg-color--lightest);border-radius:.3rem;background:var(--md-default-bg-color);color:var(--md-default-fg-color);font-size:.85rem}
.eb-controls input[type=search]{flex:1;min-width:12rem}
#eb-count{font-size:.8rem;color:var(--md-default-fg-color--light)}
#eb-table{width:100%;border-collapse:collapse;font-size:.85rem}
#eb-table th,#eb-table td{text-align:left;padding:.45rem .5rem;border-bottom:1px solid var(--md-default-fg-color--lightest);vertical-align:top}
#eb-table th{font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;color:var(--md-default-fg-color--light)}
.eb-row{cursor:pointer}
.eb-row:hover{background:var(--md-code-bg-color)}
.eb-mono{font-family:var(--md-code-font-family,monospace);font-size:.8rem;color:var(--md-default-fg-color--light)}
.eb-src{display:inline-block;font-size:.7rem;border:1px solid var(--md-default-fg-color--lightest);border-radius:.25rem;padding:0 .35rem;margin-right:.2rem}
.eb-detail td{background:var(--md-code-bg-color)}
.eb-ev{font-size:.8rem;margin:.15rem 0}
.eb-band{font-size:.72rem;color:var(--md-default-fg-color--light)}
.eb-pager{display:flex;gap:.5rem;align-items:center;margin:.75rem 0}
.eb-pager button{padding:.3rem .7rem;border:1px solid var(--md-default-fg-color--lightest);border-radius:.3rem;background:var(--md-default-bg-color);color:var(--md-default-fg-color);cursor:pointer}
.eb-pager button:disabled{opacity:.4;cursor:default}
</style>

<script>
(function(){
  var app=document.getElementById('eb-app');
  var PAGE=50, page=0, all=[], filtered=[];
  function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  fetch(app.dataset.src).then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
    .then(render).catch(function(e){
      app.innerHTML='<p>Could not load <code>data.json</code> ('+esc(e.message)+'). '+
        'Generate it with <code>just gen-web-data</code> (or <code>mondo-ai export-web-data</code>) before building the docs.</p>';
    });

  function card(n,l){return '<div class="eb-card"><div class="eb-num">'+n.toLocaleString()+'</div><div class="eb-lbl">'+l+'</div></div>';}
  function bars(obj){return Object.keys(obj||{}).sort(function(a,b){return obj[b]-obj[a];})
      .map(function(k){return '<span class="eb-chip">'+esc(k)+': <strong>'+obj[k].toLocaleString()+'</strong></span>';}).join('');}
  function opts(label,obj){return '<option value="">'+label+'</option>'+Object.keys(obj||{}).sort()
      .map(function(x){return '<option>'+esc(x)+'</option>';}).join('');}
  function bar(label,obj){return '<div class="eb-bars"><strong style="font-size:.8rem;align-self:center">'+label+':</strong> '+bars(obj)+'</div>';}

  // data.json is {stats, fields, packets}: one array per packet, zipped with data.fields
  function rows(data){
    var f=data.fields||[];
    return (data.packets||[]).map(function(r){var o={};for(var i=0;i<f.length;i++)o[f[i]]=r[i];return o;});
  }

  function render(data){
    all=rows(data); var s=data.stats||{};
    var sources=Object.keys(s.by_source||{});
    app.innerHTML=
      '<div id="eb-cards">'+card(s.total_packets||all.length,'Packets')+
        card(s.total_evidence_lines||0,'Evidence lines')+
        card(s.unique_subjects||0,'Mondo subjects')+
        card(sources.length,'Sources')+'</div>'+
      bar('By claim type',s.by_claim_type)+
      bar('By review state',s.by_review_state)+
      bar('Inclusion score',s.score_distribution)+
      bar('Evidence lines by source',s.by_source)+
      '<div class="eb-controls">'+
        '<input type="search" id="eb-q" placeholder="Search statement, subject, object, ID…">'+
        '<select id="eb-type">'+opts('All claim types',s.by_claim_type)+'</select>'+
        '<select id="eb-state">'+opts('All review states',s.by_review_state)+'</select>'+
        '<select id="eb-source">'+opts('All sources',s.by_source)+'</select>'+
        '<input type="number" id="eb-min" step="0.1" min="-1" max="1" placeholder="min S" style="width:6rem">'+
        '<span id="eb-count"></span></div>'+
      '<div style="overflow-x:auto"><table id="eb-table"><thead><tr>'+
        '<th>Statement</th><th>Claim type</th><th>S</th><th>Lines</th><th>Sources</th><th>Review state</th></tr></thead>'+
        '<tbody id="eb-body"></tbody></table></div>'+
      '<div class="eb-pager"><button id="eb-prev">&laquo; Prev</button>'+
        '<span id="eb-page"></span><button id="eb-next">Next &raquo;</button></div>';
    ['eb-q','eb-min'].forEach(function(id){document.getElementById(id).addEventListener('input',apply);});
    ['eb-type','eb-state','eb-source'].forEach(function(id){document.getElementById(id).addEventListener('change',apply);});
    document.getElementById('eb-prev').addEventListener('click',function(){if(page>0){page--;draw();}});
    document.getElementById('eb-next').addEventListener('click',function(){if((page+1)*PAGE<filtered.length){page++;draw();}});
    apply();
  }

  function apply(){
    var q=(document.getElementById('eb-q').value||'').toLowerCase();
    var type=document.getElementById('eb-type').value;
    var state=document.getElementById('eb-state').value;
    var src=document.getElementById('eb-source').value;
    var minv=document.getElementById('eb-min').value, min=minv===''?null:Number(minv);
    filtered=all.filter(function(p){
      if(type && p['claim_type']!==type) return false;
      if(state && p['review_state']!==state) return false;
      if(src && (p['sources']||[]).indexOf(src)<0) return false;
      if(min!==null && (p['score']==null || p['score']<min)) return false;
      if(q){
        var hay=((p['statement_text']||'')+' '+(p['subject']||'')+' '+(p['object']||'')+' '+p['id']).toLowerCase();
        if(hay.indexOf(q)<0) return false;
      }
      return true;
    });
    page=0; draw();
  }

  function draw(){
    var body=document.getElementById('eb-body'); body.innerHTML='';
    var start=page*PAGE, slice=filtered.slice(start,start+PAGE);
    slice.forEach(function(p){
      var tr=document.createElement('tr'); tr.className='eb-row';
      tr.innerHTML='<td>'+esc(p['statement_text']||(p['subject']+' '+p['predicate']+' '+p['object']))+'</td>'+
        '<td>'+esc(p['claim_type'])+'</td>'+
        '<td>'+(p['score']==null?'':Number(p['score']).toFixed(3))+(p['strength']?' <span class="eb-band">'+esc(p['strength'])+'</span>':'')+'</td>'+
        '<td>'+esc(p['line_count'])+'</td>'+
        '<td>'+(p['sources']||[]).map(function(x){return '<span class="eb-src">'+esc(x)+'</span>';}).join('')+'</td>'+
        '<td>'+esc(p['review_state'])+'</td>';
      var det=document.createElement('tr'); det.className='eb-detail'; det.style.display='none';
      det.innerHTML='<td colspan="6">'+
        '<div class="eb-ev">Subject <span class="eb-mono">'+esc(p['subject'])+'</span></div>'+
        '<div class="eb-ev">Predicate '+esc(p['predicate'])+'</div>'+
        '<div class="eb-ev">Object <span class="eb-mono">'+esc(p['object'])+'</span></div>'+
        '<div class="eb-mono" style="margin-top:.3rem">'+esc(p['id'])+'</div></td>';
      tr.addEventListener('click',function(){det.style.display=det.style.display==='none'?'':'none';});
      body.appendChild(tr); body.appendChild(det);
    });
    document.getElementById('eb-count').textContent=filtered.length.toLocaleString()+' packet(s)';
    var pages=Math.max(1,Math.ceil(filtered.length/PAGE));
    document.getElementById('eb-page').textContent='Page '+(page+1)+' of '+pages;
    document.getElementById('eb-prev').disabled=page===0;
    document.getElementById('eb-next').disabled=(page+1)*PAGE>=filtered.length;
  }
})();
</script>
