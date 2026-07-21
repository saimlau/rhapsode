// Exercise the search logic against a realistic library snapshot, headlessly.
const fs = require('fs');
const html = fs.readFileSync(require('path').join(__dirname, '..', 'library.html'), 'utf8');
const src = html.match(/<script>\n"use strict";([\s\S]*?)<\/script>/)[1];

const papers = {
  a: {title:"Polymer physics of the cytoskeleton", authors:"Gardel, Kasza", year:2008, status:"ready", duration:1200},
  b: {title:"A novel auxetic structure based bone screw design", authors:"Chen, Wang", year:2021, status:"error", error:"boom"},
  c: {title:"Stiffness-matching in AVOIDING stress shielding", authors:"Pei, Liu", year:2021, status:"ready", duration:900},
  d: {title:"DuncanWarwickJ2005PhD (1)", authors:"", year:null, status:"pending"},
};
const state = {papers, order:["a","b","c","d"], playlists:{}, settings:{}};

// minimal DOM + globals the extracted functions expect
const els = {};
const mk = () => ({ innerHTML:"", textContent:"", value:"", style:{}, classList:{toggle(){},remove(){}},
                    addEventListener(){}, appendChild(){}, checked:false, dataset:{} });
for (const id of ["queue","none","count","plTitle","empty","frame","autoadv","q","drop","side"]) els[id]=mk();
// faithful stand-in for the div esc() uses: setting textContent must produce
// escaped innerHTML, exactly as a browser does
const escapeHTML = t => String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;")
                                 .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const mkEl = () => { const e = {...mk(), classList:{add(){},contains:()=>false}, _t:""};
  Object.defineProperty(e, "textContent", { get(){return e._t;},
    set(v){ e._t = v ?? ""; e.innerHTML = escapeHTML(e._t); } });
  return e; };
global.document = { getElementById:id=>els[id]||mk(), createElement:()=>mkEl(),
                    addEventListener(){}, activeElement:{tagName:"BODY"}, body:{appendChild(){}} };
global.window = { addEventListener(){}, location:{search:""} };
global.fetch = () => new Promise(()=>{});
global.EventSource = function(){ this.addEventListener=()=>{}; };
global.localStorage = { getItem:()=>null, setItem(){}, removeItem(){}, clear(){} };

// load the real code, then reach into it
const ctx = {};
const wrapped = new Function('__seed','__ctx', `
  // capture hoisted declarations up-front so a later init throw can't hide them
  __ctx.visibleIds = () => visibleIds();
  __ctx.mark = t => mark(t);
  __ctx.citation = p => citation(p);
` + src.replace('let query = "";', 'let query = ""; __ctx.setQ = v => { query = v; }; state = __seed;') + `
`);
try { wrapped(state, ctx); } catch(e) { console.log("  (init threw, expected:", e.message + ")"); }
if (!ctx.setQ) { console.log("FATAL: could not capture setQ"); process.exit(1); }

let fail = 0;
const check = (name, cond, extra="") => { console.log((cond?"  ok   ":"  FAIL ")+name+(cond?"":" -> "+extra)); if(!cond) fail++; };

ctx.setQ("");
check("no query returns all 4", ctx.visibleIds().length===4, ctx.visibleIds().join(","));

ctx.setQ("polymer");
check("title match finds 1", JSON.stringify(ctx.visibleIds())==='["a"]', ctx.visibleIds().join(","));

ctx.setQ("pei");
check("author match finds c", JSON.stringify(ctx.visibleIds())==='["c"]', ctx.visibleIds().join(","));

ctx.setQ("2021");
check("year match finds b and c", JSON.stringify(ctx.visibleIds())==='["b","c"]', ctx.visibleIds().join(","));

ctx.setQ("AVOIDING".toLowerCase());
check("case-insensitive", JSON.stringify(ctx.visibleIds())==='["c"]', ctx.visibleIds().join(","));

ctx.setQ("zzzz");
check("no match returns empty", ctx.visibleIds().length===0);

ctx.setQ("duncan");
check("errored/pending papers are searchable too", JSON.stringify(ctx.visibleIds())==='["d"]', ctx.visibleIds().join(","));

// highlight correctness + XSS safety
ctx.setQ("poly");
const h = ctx.mark("Polymer physics");
check("highlight wraps the match", h==='<span class="hit">Poly</span>mer physics', h);
ctx.setQ("a");
const evil = ctx.mark('<img src=x onerror=alert(1)> and a');
check("escapes HTML while highlighting", !evil.includes("<img"), evil);
check("still marks inside escaped text", evil.includes('class="hit"'), evil);
ctx.setQ("");
check("empty query escapes too", ctx.mark("<b>x</b>")==="&lt;b&gt;x&lt;/b&gt;", ctx.mark("<b>x</b>"));

console.log(fail? `\n${fail} FAILURES` : "\nall search tests passed");
process.exit(fail?1:0);
