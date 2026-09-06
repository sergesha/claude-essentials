#!/usr/bin/env python3
"""Render a Tech Radar HTML dashboard from a collect-news JSON data document.

Architecture: the data is embedded as inline JSON into a static Alpine.js +
Tailwind template. All layout, filtering, topic switching and highlighting are
declarative (Alpine) — this script does NOT build cards in Python. To change the
dashboard, edit the TEMPLATE/CARD strings below.

OS-agnostic, Python standard-library only (no pip installs). Reads the canonical
JSON (default reports/radar-latest.json) and writes reports/dashboard-<stamp>.html
plus reports/dashboard-latest.html.

Usage:
  python3 render.py [path/to/radar-<stamp>.json] [--stdout]
"""
import sys, json, re
from pathlib import Path

# Tailwind Play CDN generates classes from the live DOM; dynamic `bg-${color}-100`
# strings are kept generatable by listing them in a hidden safelist (SAFELIST).
_COLORS = ["blue", "indigo", "purple", "teal", "pink", "slate", "green", "cyan",
           "red", "emerald", "gray", "sky", "zinc", "amber", "orange", "yellow"]
SAFELIST = " ".join(f"bg-{c}-100 text-{c}-800" for c in _COLORS) + \
    " border-purple-400 border-red-500 border-amber-400 border-orange-500 ring-1 ring-red-300 bg-yellow-100"

# One card definition, reused by every x-for list. `i` is the Alpine loop item.
CARD = """
<div class="bg-white rounded-lg shadow p-4 border-l-4"
     :class="(i.kind==='research'?'border-purple-400':(i.relevance==='high'?'border-red-500':'border-amber-400'))+(i.stance==='competitor'?' ring-1 ring-red-300':'')">
  <a :href="i.url" target="_blank" class="text-lg font-semibold text-blue-700 hover:underline" x-html="hl(i.title)"></a>
  <p class="text-gray-600 mt-2" x-html="hl(i.summary)"></p>
  <div class="flex flex-wrap items-center gap-2 mt-3 text-sm text-gray-500">
    <span class="cursor-pointer hover:underline" @click="addFilter('source', i.source)" x-text="i.source"></span>
    <span>·</span><span x-text="i.date"></span>
    <template x-if="i.stance">
      <span class="cursor-pointer px-2 py-0.5 rounded text-xs font-medium" :class="'bg-'+sColor(i.stance)+'-100 text-'+sColor(i.stance)+'-800'" @click="addFilter('stance', i.stance)"><span x-show="i.stance==='competitor'">⚔️ </span><span x-text="i.stance"></span></span>
    </template>
    <template x-if="i.kind">
      <span class="cursor-pointer px-2 py-0.5 rounded text-xs font-medium" :class="'bg-'+kColor(i.kind)+'-100 text-'+kColor(i.kind)+'-800'" @click="addFilter('kind', i.kind)" x-text="i.kind"></span>
    </template>
    <template x-if="i.field">
      <span class="cursor-pointer px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700" @click="addFilter('field', i.field)" x-text="i.field"></span>
    </template>
    <template x-if="i.relevance">
      <span class="cursor-pointer px-2 py-0.5 rounded text-xs font-medium" :class="i.relevance==='high'?'bg-red-100 text-red-800':'bg-amber-100 text-amber-800'" @click="addFilter('relevance', i.relevance)" x-text="i.relevance+' · '+i.match+'%'"></span>
    </template>
    <template x-if="!i.relevance && i.match!=null">
      <span class="px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800" x-text="i.match+'%'"></span>
    </template>
    <template x-if="i.temperature">
      <span :title="'temperature '+i.temperature" x-text="FLAME[i.temperature]"></span>
    </template>
    <template x-if="i.language && i.language!=='en'">
      <span class="cursor-pointer px-2 py-0.5 rounded text-xs font-medium bg-gray-200 text-gray-700" @click="addFilter('language', i.language)" x-text="i.language.toUpperCase()"></span>
    </template>
  </div>
</div>
"""

APP = """
function radar(){
  return {
    d: window.__RADAR__,
    topic: 'all',
    f: {},
    FLAME: {1:'❄️',2:'\U0001f321',3:'\U0001f525',4:'\U0001f525\U0001f525',5:'\U0001f525\U0001f525\U0001f525'},
    KIND: {announcement:'blue',standard:'indigo',research:'purple',implementation:'teal',proposal:'pink',analysis:'slate',guide:'green',tool:'cyan'},
    STANCE: {competitor:'red',adoptable:'emerald',narrow:'gray',complementary:'sky',context:'zinc'},
    kColor(k){ return this.KIND[k]||'gray'; },
    sColor(s){ return this.STANCE[s]||'gray'; },
    get topics(){ return this.d.topics||[]; },
    get tabs(){ return [{slug:'all',name:'All'}].concat(this.topics); },
    name(slug){ var t=this.topics.find(function(t){return t.slug===slug;}); return t?t.name:slug; },
    desc(){ if(this.topic==='all') return 'All topics.'; var t=this.topics.find((t)=>t.slug===this.topic); return t?(t.description||''):''; },
    get hot(){ var out=[], hn=this.d.hot_news||{}; for(var s in hn){ hn[s].forEach(function(x){ x.topic=s; out.push(x); }); } return out; },
    esc(s){ var d=document.createElement('div'); d.textContent=(s==null?'':s); return d.innerHTML; },
    hl(s){ return this.esc(s).replace(/==(.+?)==/g, '<mark class="bg-yellow-100 rounded px-0.5">$1</mark>'); },
    match(i){
      if(this.topic!=='all' && i.topic!==this.topic) return false;
      for(var p in this.f){ if(this.f[p].indexOf(i[p]==null?'':i[p])<0) return false; }
      return true;
    },
    hits(){ return (this.d.radar_hits||[]).filter((i)=>this.match(i)); },
    hotFor(slug){ return this.hot.filter((i)=>i.topic===slug && this.match(i)); },
    researchItems(){ return (this.d.research||[]).filter((i)=>this.match(i)); },
    summaries(){ var rs=this.d.radar_summary||{}; if(this.topic!=='all') return (this.topic in rs)?[[this.topic,rs[this.topic]]]:[]; return Object.entries(rs); },
    flashGroups(){
      var items=[].concat(this.d.radar_hits||[], this.hot).filter(function(i){return i.flash;});
      var g={}; items.forEach(function(i){ (g[i.topic]=g[i.topic]||[]).push(i); });
      var self=this;
      return Object.keys(g).map(function(s){
        var arr=g[s], top=arr.reduce(function(a,b){return (b.match||0)>(a.match||0)?b:a;});
        var mt=Math.max.apply(null, arr.map(function(x){return x.temperature||0;}));
        return {slug:s, name:self.name(s), count:arr.length, flame:self.FLAME[mt]||'', top:top};
      });
    },
    addFilter(p,v){ if(!this.f[p]) this.f[p]=[]; var s=this.f[p], k=s.indexOf(v); if(k<0) s.push(v); else s.splice(k,1); if(!s.length) delete this.f[p]; },
    clearF(){ this.f={}; },
    get chips(){ var out=[]; for(var p in this.f) this.f[p].forEach(function(v){ out.push({p:p,v:v}); }); return out; }
  };
}
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tech Radar Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<script>window.__RADAR__ = __DATA__;</script>
<script>__APP__</script>
</head>
<body class="bg-gray-50 text-gray-900">
<div class="hidden">__SAFELIST__</div>
<div x-data="radar()">
<header class="bg-white border-b border-gray-200"><div class="max-w-7xl mx-auto px-6 py-8">
  <div class="flex items-center gap-3"><span class="text-3xl">\U0001f4e1</span><h1 class="text-3xl font-bold tracking-tight">Tech Radar Dashboard</h1></div>
  <p class="text-gray-500 mt-2"><span x-text="d.generated_at"></span> · <span x-text="(d.stats||{}).topics"></span> topics</p>
  <div class="flex flex-wrap gap-4 mt-6">
    <div class="bg-gray-50 border rounded-lg px-5 py-3"><div class="text-2xl font-bold" x-text="(d.stats||{}).scanned"></div><div class="text-sm text-gray-500">Scanned</div></div>
    <div class="bg-gray-50 border rounded-lg px-5 py-3"><div class="text-2xl font-bold" x-text="(d.stats||{}).stored"></div><div class="text-sm text-gray-500">Stored</div></div>
    <div class="bg-red-50 border border-red-200 rounded-lg px-5 py-3"><div class="text-2xl font-bold text-red-700" x-text="(d.stats||{}).radar_hits"></div><div class="text-sm text-red-600">Radar Hits</div></div>
    <div class="bg-purple-50 border border-purple-200 rounded-lg px-5 py-3"><div class="text-2xl font-bold text-purple-700" x-text="(d.stats||{}).research"></div><div class="text-sm text-purple-600">Research</div></div>
    <div class="bg-orange-50 border border-orange-200 rounded-lg px-5 py-3"><div class="text-2xl font-bold text-orange-700"><span x-text="(d.stats||{}).flash"></span> \U0001f525</div><div class="text-sm text-orange-600">Flash</div></div>
    <div class="bg-gray-50 border rounded-lg px-5 py-3"><div class="text-2xl font-bold"><span x-text="(d.stats||{}).competitors||0"></span> ⚔️</div><div class="text-sm text-gray-500">Competitive</div></div>
  </div></div></header>
<main class="max-w-7xl mx-auto px-6 py-10 space-y-10">

  <section id="flash" x-show="flashGroups().length">
    <div class="flex items-center gap-2 mb-5"><span class="text-xl">\U0001f525</span><h2 class="text-2xl font-bold">Flash — surging now</h2><span class="text-sm text-gray-400 ml-2">global · not filtered</span></div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <template x-for="g in flashGroups()" :key="g.slug">
        <div class="bg-orange-50 rounded-lg shadow p-5 border-l-4 border-orange-500">
          <div class="flex items-center justify-between"><h3 class="text-lg font-bold" x-text="g.name"></h3><span class="text-lg" x-text="g.flame"></span></div>
          <p class="text-gray-700 mt-2"><span x-text="g.count"></span> surging items. Top: <span x-html="hl(g.top.title)"></span></p>
          <a :href="g.top.url" target="_blank" class="inline-block mt-2 text-sm font-medium text-blue-700 hover:underline">Open top hit (<span x-text="g.top.match"></span>%) →</a>
        </div>
      </template>
    </div>
  </section>

  <nav class="flex flex-wrap gap-2 items-center border-t border-b border-gray-200 py-3">
    <template x-for="t in tabs" :key="t.slug">
      <button @click="topic=t.slug" class="px-3 py-1.5 rounded-full text-sm font-medium" :class="topic===t.slug?'bg-blue-600 text-white':'bg-gray-100 hover:bg-gray-200'" x-text="t.name"></button>
    </template>
  </nav>
  <p class="text-gray-600 -mt-6" x-text="desc()"></p>
  <div class="flex flex-wrap gap-2 items-center -mt-4" x-show="chips.length">
    <span class="text-sm text-gray-500">Filters:</span>
    <template x-for="c in chips" :key="c.p+c.v">
      <button @click="addFilter(c.p,c.v)" class="px-2 py-0.5 rounded-full text-xs bg-blue-100 text-blue-800 hover:bg-blue-200"><span x-text="c.p+': '+c.v+' ✕'"></span></button>
    </template>
    <button @click="clearF()" class="px-2 py-0.5 rounded-full text-xs bg-gray-200 hover:bg-gray-300">clear all</button>
  </div>
  <p class="text-xs text-gray-400 -mt-4">Tip: click any badge — kind, stance, source, language, field — to filter. Combine several; Flash is never filtered.</p>

  <section id="radar-hits" x-show="hits().length">
    <div class="flex items-center gap-2 mb-5"><span class="text-xl">\U0001f534</span><h2 class="text-2xl font-bold">Radar Hits</h2><span class="text-sm text-gray-400 ml-2">match ≥ 70%</span></div>
    <template x-for="s in summaries()" :key="s[0]">
      <div class="bg-white rounded-lg shadow p-5 border-l-4 border-red-500 mb-4">
        <h3 class="text-lg font-bold mb-2">\U0001f4e1 <span x-text="name(s[0])"></span> — Summary</h3>
        <p class="text-gray-700" x-html="hl(s[1])"></p>
      </div>
    </template>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <template x-for="i in hits()" :key="i.url">__CARD__</template>
    </div>
  </section>

  <template x-for="t in topics" :key="t.slug">
    <section x-show="hotFor(t.slug).length">
      <div class="flex items-center gap-2 mb-1"><span class="text-xl">\U0001f7e1</span><h2 class="text-2xl font-bold" x-text="t.name"></h2></div>
      <p class="text-sm text-gray-400 mb-5">Hot News · match 50–69%</p>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <template x-for="i in hotFor(t.slug)" :key="i.url">__CARD__</template>
      </div>
    </section>
  </template>

  <section id="research" x-show="researchItems().length">
    <div class="flex items-center gap-2 mb-5"><span class="text-xl">\U0001f52c</span><h2 class="text-2xl font-bold">Theory &amp; Research</h2><span class="text-sm text-gray-400 ml-2">adjacent work · match ≥ 40%</span></div>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <template x-for="i in researchItems()" :key="i.url">__CARD__</template>
    </div>
  </section>

</main>
<footer class="border-t border-gray-200 bg-white"><div class="max-w-7xl mx-auto px-6 py-6 text-sm text-gray-500 space-y-1">
  <p>Topics: <span x-text="topics.map(function(t){return t.name;}).join(' · ')"></span></p>
  <p>Generated <span x-text="d.generated_at"></span> · <span x-text="(d.stats||{}).scanned"></span> scanned · <span x-text="(d.stats||{}).stored"></span> stored</p>
</div></footer>
</div></body></html>
"""


def stamp_from(data, src):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", data.get("generated_at") or "")
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}-{m[4]}{m[5]}"
    return src.stem.replace("radar-", "") or "latest"


def build_html(data):
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__CARD__", CARD)
    html = html.replace("__DATA__", payload).replace("__APP__", APP).replace("__SAFELIST__", SAFELIST)
    return html


def main():
    argv = sys.argv[1:]
    to_stdout = "--stdout" in argv
    pos = [a for a in argv if not a.startswith("--")]
    src = Path(pos[0]) if pos else Path("reports/radar-latest.json")
    if not src.exists():
        sys.exit(f"No data document at {src}. Run /tech-radar:collect-news first.")
    data = json.loads(src.read_text())
    html = build_html(data)
    if to_stdout:
        sys.stdout.write(html)
        return
    reports = src.parent
    out = reports / f"dashboard-{stamp_from(data, src)}.html"
    out.write_text(html)
    (reports / "dashboard-latest.html").write_text(html)
    print(f"Dashboard generated: {out} (latest: {reports / 'dashboard-latest.html'})")


if __name__ == "__main__":
    main()
