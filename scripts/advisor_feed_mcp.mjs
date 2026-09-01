import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

const UI_URI = "ui://advisor/feed-v1.html";
const SERVER_VERSION = "0.1.0";

function emit(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function codexHome() {
  return path.resolve(process.env.CODEX_HOME || path.join(os.homedir(), ".codex"));
}

function pluginRoot() {
  return path.resolve(process.env.PLUGIN_ROOT || process.cwd());
}

function dataRoot() {
  if (process.env.PLUGIN_DATA) return path.resolve(process.env.PLUGIN_DATA);
  const home = codexHome();
  const cache = path.join(home, "plugins", "cache");
  const relative = path.relative(cache, pluginRoot());
  const parts = relative.split(path.sep).filter(Boolean);
  if (!relative.startsWith("..") && parts.length >= 3) {
    const [marketplace, pluginName] = parts;
    return path.join(home, "plugins", "data", `${pluginName}-${marketplace}`);
  }
  return path.join(home, "plugin-data", "advisor");
}

function sessionKey(sessionId) {
  return crypto.createHash("sha256").update(String(sessionId), "utf8").digest("hex").slice(0, 24);
}

function latestSessionDirectory(root) {
  const sessions = path.join(root, "sessions");
  let candidates = [];
  try {
    candidates = fs.readdirSync(sessions, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(sessions, entry.name));
  } catch {
    return null;
  }
  const activity = (directory) => {
    let newest = 0;
    for (const name of ["feed.json", "usage.json", "queue.json", "worker.json"]) {
      try {
        newest = Math.max(newest, fs.statSync(path.join(directory, name)).mtimeMs);
      } catch { /* absent files are expected */ }
    }
    return newest;
  };
  candidates.sort((left, right) => activity(right) - activity(left));
  return candidates[0] || null;
}

function resolveSession(args = {}) {
  const root = dataRoot();
  const sessions = path.join(root, "sessions");
  if (typeof args.session_key === "string" && /^[a-f0-9]{24}$/i.test(args.session_key)) {
    return path.join(sessions, args.session_key.toLowerCase());
  }
  if (typeof args.session_id === "string" && args.session_id.trim()) {
    return path.join(sessions, sessionKey(args.session_id.trim()));
  }
  return latestSessionDirectory(root);
}

function totalTokens(section) {
  return Number(section?.totals?.total_tokens || 0);
}

function snapshot(args = {}) {
  const directory = resolveSession(args);
  const limit = Math.max(1, Math.min(200, Number(args.limit || 100)));
  if (!directory || !fs.existsSync(directory)) {
    return {
      session_key: directory ? path.basename(directory) : null,
      session_id: args.session_id || null,
      updated_at: new Date().toISOString(),
      activity: { status: "not-started" },
      usage: { main_total: 0, advisor_total: 0, advisor_share: 0, advisor_main_ratio: 0 },
      feed: [],
    };
  }
  const usage = readJson(path.join(directory, "usage.json"), {});
  const worker = readJson(path.join(directory, "worker.json"), {});
  const allFeed = readJson(path.join(directory, "feed.json"), []);
  const mainTotal = totalTokens(usage.main);
  const advisorTotal = totalTokens(usage.advisor);
  const combined = mainTotal + advisorTotal;
  const feed = (Array.isArray(allFeed) ? allFeed : []).slice(-limit).reverse().map((item) => ({
    id: String(item.id || ""),
    kind: String(item.kind || "advice"),
    note: String(item.note || ""),
    severity: String(item.severity || "nit"),
    origin: String(item.origin || "runtime"),
    transcript: item.transcript ? path.basename(String(item.transcript)) : null,
    created_at: Number(item.created_at || 0),
  }));
  return {
    session_key: path.basename(directory),
    session_id: args.session_id || null,
    updated_at: new Date().toISOString(),
    activity: {
      status: String(worker.status || "unknown"),
      review_attempt: worker.review_attempt || null,
      heartbeat: worker.heartbeat || null,
      invocations: Number(usage.advisor?.invocations || 0),
      successful_reviews: Number(usage.advisor?.successful_reviews || 0),
      silent_reviews: Number(usage.advisor?.silent_reviews || 0),
      failed_reviews: Number(usage.advisor?.failed_reviews || 0),
      model: usage.advisor?.model || null,
      reasoning_effort: usage.advisor?.reasoning_effort || null,
    },
    usage: {
      main_total: mainTotal,
      advisor_total: advisorTotal,
      advisor_share: combined ? 100 * advisorTotal / combined : 0,
      advisor_main_ratio: mainTotal ? 100 * advisorTotal / mainTotal : 0,
    },
    feed,
  };
}

function toolResult(args) {
  const data = snapshot(args);
  const summary = data.feed.length
    ? `Advisor feed: ${data.feed.length} recent entries; worker ${data.activity.status}.`
    : `Advisor feed has no visible advice yet; worker ${data.activity.status}.`;
  return {
    content: [{ type: "text", text: summary }],
    structuredContent: data,
  };
}

function tools() {
  const commonSchema = {
    type: "object",
    properties: {
      session_id: { type: "string", description: "Current Codex task/session id. Omit to use the latest active Advisor task." },
      session_key: { type: "string", description: "Opaque Advisor session key returned by an earlier feed call." },
      limit: { type: "integer", minimum: 1, maximum: 200, default: 100 },
    },
    additionalProperties: false,
  };
  return [
    {
      name: "show_advisor_feed",
      title: "Show Advisor feed",
      description: "Open the read-only live Advisor feed and main-versus-Advisor usage dashboard.",
      inputSchema: commonSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      _meta: { ui: { resourceUri: UI_URI } },
    },
    {
      name: "read_advisor_feed",
      title: "Refresh Advisor feed",
      description: "Read current Advisor feed data without changing agent context or task state.",
      inputSchema: commonSchema,
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
      _meta: { ui: { visibility: ["app"] } },
    },
  ];
}

const UI_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark;font:13px/1.45 ui-sans-serif,system-ui,sans-serif}body{margin:0;background:transparent;color:CanvasText}.wrap{padding:14px}.top{display:flex;align-items:center;justify-content:space-between;gap:12px}.title{font-size:16px;font-weight:700}.status{font-size:12px;opacity:.72}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:12px 0}.card,.entry{border:1px solid color-mix(in srgb,CanvasText 15%,transparent);border-radius:10px;padding:10px;background:color-mix(in srgb,Canvas 94%,CanvasText 6%)}.value{font-size:17px;font-weight:700}.label,.meta{font-size:11px;opacity:.68}.feed{display:grid;gap:8px}.entry{border-left:4px solid #6b7280}.entry.concern{border-left-color:#f59e0b}.entry.blocker{border-left-color:#ef4444}.entry.warning{border-left-color:#8b5cf6}.row{display:flex;justify-content:space-between;gap:8px}.badge{font-weight:700;text-transform:uppercase;font-size:10px}.note{margin-top:5px;white-space:pre-wrap}.empty{padding:20px;text-align:center;opacity:.66}button{font:inherit;border:1px solid color-mix(in srgb,CanvasText 20%,transparent);border-radius:8px;padding:5px 9px;background:Canvas;cursor:pointer}@media(max-width:520px){.cards{grid-template-columns:1fr}.row{display:block}}
</style></head><body><div class="wrap"><div class="top"><div><div class="title">Advisor feed</div><div class="status" id="status">Connecting…</div></div><button id="refresh">Refresh</button></div><div class="cards" id="cards"></div><div class="feed" id="feed"></div></div>
<script>
let seq=0,sessionId=null,sessionKey=null;const pending=new Map();
function call(method,params){return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});parent.postMessage({jsonrpc:'2.0',id,method,params},'*')})}
function toolArguments(params){return params?.arguments||params?.input||params?.toolInput||params||null}
function bindInput(params){const args=toolArguments(params);if(!args)return;if(typeof args.session_id==='string'&&args.session_id)sessionId=args.session_id;if(typeof args.session_key==='string'&&args.session_key)sessionKey=args.session_key;if(sessionId||sessionKey)void refresh()}
function accepts(data){if(!data)return false;const incomingId=typeof data.session_id==='string'&&data.session_id?data.session_id:null;const incomingKey=typeof data.session_key==='string'&&data.session_key?data.session_key:null;if(sessionId&&incomingId!==sessionId)return false;if(sessionKey&&incomingKey!==sessionKey)return false;if(!sessionId&&incomingId)sessionId=incomingId;if(!sessionKey&&incomingKey)sessionKey=incomingKey;return true}
addEventListener('message',event=>{if(event.source!==window.parent)return;const m=event.data;if(!m||m.jsonrpc!=='2.0')return;if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);return}if(m.method==='ui/notifications/tool-input'){bindInput(m.params);return}if(m.method==='ui/notifications/tool-result')render(m.params?.structuredContent||m.params?.result?.structuredContent||m.params?.result)});
const number=n=>new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(Number(n||0));
const pct=n=>Number(n||0).toFixed(1)+'%';
function render(data){if(!accepts(data))return;document.getElementById('status').textContent=[data.activity?.status,data.activity?.model,data.activity?.reasoning_effort,'updated '+new Date(data.updated_at).toLocaleTimeString()].filter(Boolean).join(' · ');document.getElementById('cards').innerHTML=[['Main tokens',number(data.usage?.main_total)],['Advisor tokens',number(data.usage?.advisor_total)],['Advisor / Main',pct(data.usage?.advisor_main_ratio)]].map(x=>'<div class="card"><div class="value">'+x[1]+'</div><div class="label">'+x[0]+'</div></div>').join('');const feed=data.feed||[];document.getElementById('feed').innerHTML=feed.length?feed.map(e=>'<div class="entry '+escapeHtml(e.severity)+'"><div class="row"><span class="badge">'+escapeHtml(e.origin)+' · '+escapeHtml(e.severity)+'</span><span class="meta">'+new Date(e.created_at*1000).toLocaleTimeString()+'</span></div><div class="note">'+escapeHtml(e.note)+'</div></div>').join(''):'<div class="empty">No Advisor advice has been emitted for this task yet.</div>'}
function escapeHtml(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){if(!sessionId&&!sessionKey){document.getElementById('status').textContent='Waiting for task binding…';return}try{const result=await call('tools/call',{name:'read_advisor_feed',arguments:{session_id:sessionId,session_key:sessionKey,limit:100}});render(result?.structuredContent||result)}catch(e){document.getElementById('status').textContent='Refresh failed'}}
document.getElementById('refresh').addEventListener('click',refresh);call('ui/initialize',{protocolVersion:'2025-06-18',appInfo:{name:'Advisor feed',version:'1.0.0'},capabilities:{}}).catch(()=>{});setInterval(()=>{if(sessionKey)refresh()},3000);
</script></body></html>`;

function handle(request) {
  const method = request.method;
  if (method === "initialize") {
    return {
      protocolVersion: "2025-06-18",
      capabilities: { tools: {}, resources: {} },
      serverInfo: { name: "advisor-feed", version: SERVER_VERSION },
    };
  }
  if (method === "tools/list") return { tools: tools() };
  if (method === "tools/call") return toolResult(request.params?.arguments || {});
  if (method === "resources/list") {
    return { resources: [{ uri: UI_URI, name: "Advisor feed", mimeType: "text/html;profile=mcp-app" }] };
  }
  if (method === "resources/read") {
    if (request.params?.uri !== UI_URI) throw new Error("Unknown Advisor UI resource");
    return { contents: [{ uri: UI_URI, mimeType: "text/html;profile=mcp-app", text: UI_HTML, _meta: { ui: { prefersBorder: true } } }] };
  }
  if (method === "ping") return {};
  return undefined;
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let request;
  try {
    request = JSON.parse(line);
    if (["notifications/initialized", "notifications/cancelled"].includes(request.method)) return;
    const result = handle(request);
    if (request.id == null) return;
    if (result === undefined) emit({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "Method not found" } });
    else emit({ jsonrpc: "2.0", id: request.id, result });
  } catch (error) {
    if (request?.id != null) emit({ jsonrpc: "2.0", id: request.id, error: { code: -32603, message: String(error?.message || error) } });
  }
});
