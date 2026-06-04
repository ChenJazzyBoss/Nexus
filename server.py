"""Nexus Visualizer - Canvas-based multi-agent visualization."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from nexus.core import ToolRegistry, tool_result, load_config
from nexus.orchestrator.engine import Orchestrator
from nexus.orchestrator.events import EventBus, EventType, Event
from nexus.visualizer.hub import VisualizerHub

app = FastAPI(title="Nexus Multi-Agent Platform")

event_bus = EventBus()
visualizer = VisualizerHub(event_bus)
config = load_config("config.yaml")
registry = ToolRegistry()

PAPER_DB = {
    "1706.03762": {"title": "Attention Is All You Need", "abstract": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks. We propose a new simple network architecture, the Transformer, based solely on attention mechanisms."},
    "1810.04805": {"title": "BERT: Pre-training of Deep Bidirectional Transformers", "abstract": "We introduce BERT, a new language representation model designed to pre-train deep bidirectional representations from unlabeled text."},
}

def handle_read_paper(args):
    paper = PAPER_DB.get(args.get("arxiv_id", ""))
    if paper:
        return tool_result({"found": True, "title": paper["title"], "abstract": paper["abstract"]})
    return tool_result({"found": False, "error": "Paper not found"})

def handle_search_papers(args):
    query = args.get("query", "").lower()
    results = []
    if "attention" in query or "transformer" in query:
        results.append({"arxiv_id": "1706.03762", "title": "Attention Is All You Need", "citations": 120000})
    if "bert" in query or "pre-training" in query:
        results.append({"arxiv_id": "1810.04805", "title": "BERT", "citations": 80000})
    if not results:
        results.append({"arxiv_id": "2301.00001", "title": "Generic ML Paper", "citations": 50})
    return tool_result({"results": results[: args.get("max_results", 3)]})

def handle_calculate(args):
    try:
        return tool_result({"result": eval(args.get("expression", "0"), {"__builtins__": {}}, {})})
    except Exception as e:
        return tool_result({"error": str(e)})

registry.register(name="read_paper_abstract", toolset="research", schema={"description": "Read paper abstract by arXiv ID", "parameters": {"type": "object", "properties": {"arxiv_id": {"type": "string"}}, "required": ["arxiv_id"]}}, handler=handle_read_paper)
registry.register(name="search_papers", toolset="research", schema={"description": "Search papers by keyword", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}}, handler=handle_search_papers)
registry.register(name="calculate", toolset="utility", schema={"description": "Evaluate math expression", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}, handler=handle_calculate)

orchestrator = Orchestrator(nexus_config=config, tool_registry=registry, event_bus=event_bus)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE

@app.get("/api/events")
async def stream_events():
    async def generate():
        queue = asyncio.Queue()
        def listener(event: Event):
            queue.put_nowait(event)
        unsub = event_bus.on_all(listener)
        try:
            while True:
                event = await queue.get()
                yield {"event": event.event_type.value, "data": json.dumps(event.to_dict())}
        except asyncio.CancelledError:
            unsub()
    return EventSourceResponse(generate())

@app.get("/api/status")
async def get_status():
    return {"agents": visualizer.get_agent_statuses(), "tasks": [], "config": {"model": config.default_model, "provider": config.default_provider, "max_iterations": config.max_iterations, "max_concurrent_agents": config.max_concurrent_agents}}

@app.post("/api/task")
async def submit_task(body: dict):
    task = body.get("task", "")
    if not task:
        return {"error": "Missing 'task' field"}
    result = await orchestrator.execute_task(task)
    return {"task_id": result.task_id, "status": result.status.value, "result": result.result}


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nexus</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@300;400;500&family=Noto+Sans+SC:wght@300;400;500;600;700&display=swap');
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#f8f9fc;--surface:#fff;--surface2:#f3f4f8;
  --border:#e5e7ef;--border2:#d1d5e0;
  --shadow:0 2px 8px rgba(0,0,0,.04);--shadow-md:0 4px 20px rgba(0,0,0,.06);--shadow-lg:0 8px 40px rgba(0,0,0,.08);
  --blue:#4f6ef7;--blue-bg:#eef1fe;--cyan:#0ea5e9;--cyan-bg:#e0f7ff;
  --violet:#8b5cf6;--violet-bg:#f0ebff;--emerald:#10b981;--emerald-bg:#e6faf4;
  --rose:#f43f5e;--rose-bg:#fff0f3;--amber:#f59e0b;--amber-bg:#fff8e6;
  --text:#1a1d2e;--text2:#5a5e78;--text3:#8b8fa8;--text4:#b8bcc8;
  --sans:'Plus Jakarta Sans','Noto Sans SC',sans-serif;--mono:'JetBrains Mono',monospace;
  --radius:14px;--radius-sm:10px;
}
html,body{height:100%;overflow:hidden}
body{font-family:var(--sans);background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}

.app{display:grid;grid-template-rows:56px 1fr;grid-template-columns:1fr 320px;height:100vh}

/* HEADER */
.hdr{grid-column:1/-1;display:flex;align-items:center;padding:0 28px;gap:16px;background:var(--surface);border-bottom:1px solid var(--border)}
.logo{font-size:20px;font-weight:800;letter-spacing:-.3px;background:linear-gradient(135deg,var(--blue),var(--violet));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-sub{font-size:10px;color:var(--text3);font-family:var(--mono);letter-spacing:1px}
.sep{width:1px;height:20px;background:var(--border)}
.status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text3)}
.status .dot{width:7px;height:7px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-bg);animation:blink 2s ease infinite}
@keyframes blink{0%,100%{box-shadow:0 0 0 3px var(--emerald-bg)}50%{box-shadow:0 0 0 6px var(--emerald-bg)}}
.hdr-right{margin-left:auto;display:flex;gap:18px;font-size:11px;color:var(--text3)}
.hdr-right b{color:var(--text);font-weight:600}

/* CANVAS AREA */
.canvas-wrap{position:relative;overflow:hidden;background:var(--bg)}
canvas{display:block;width:100%;height:100%}

/* BOTTOM BAR */
.bottom-bar{position:absolute;bottom:0;left:0;right:0;padding:16px 24px;
  background:linear-gradient(transparent,rgba(248,249,252,.95) 30%);pointer-events:none;z-index:10}
.bottom-inner{display:flex;gap:10px;pointer-events:auto;max-width:720px;margin:0 auto}
.inp{flex:1;height:48px;background:var(--surface);border:2px solid var(--border);border-radius:var(--radius-sm);
  padding:0 16px;font-size:13px;font-family:var(--sans);color:var(--text);outline:none;transition:all .2s;box-shadow:var(--shadow)}
.inp:focus{border-color:var(--blue);box-shadow:0 0 0 4px rgba(79,110,247,.08),var(--shadow-md)}
.inp::placeholder{color:var(--text4)}
.btn{height:48px;padding:0 24px;border:none;border-radius:var(--radius-sm);
  background:linear-gradient(135deg,var(--blue),var(--violet));color:#fff;
  font-family:var(--sans);font-weight:700;font-size:13px;cursor:pointer;transition:all .2s;box-shadow:var(--shadow)}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(79,110,247,.25)}
.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}

.tags{display:flex;justify-content:center;gap:8px;margin-top:10px;pointer-events:auto;flex-wrap:wrap}
.tag{padding:6px 14px;background:var(--surface);border:1px solid var(--border);border-radius:20px;
  font-size:11px;color:var(--text2);cursor:pointer;transition:all .15s;box-shadow:var(--shadow)}
.tag:hover{background:var(--blue-bg);border-color:var(--blue);color:var(--blue)}

/* SIDEBAR */
.side{background:var(--surface);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.side-hd{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
.side-hd .ico{width:28px;height:28px;display:flex;align-items:center;justify-content:center;border-radius:8px;background:var(--blue-bg);color:var(--blue);font-size:13px}
.side-hd .title{font-size:13px;font-weight:700}
.side-hd .cnt{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--text3);background:var(--surface2);padding:2px 8px;border-radius:10px}
.side-body{flex:1;overflow-y:auto;padding:12px}

.agent-card{padding:12px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);
  margin-bottom:8px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden}
.agent-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.agent-card.coordinator::before{background:linear-gradient(90deg,var(--blue),var(--violet))}
.agent-card.worker::before{background:var(--cyan)}
.agent-card.done::before{background:var(--emerald)}
.agent-card.failed::before{background:var(--rose)}
.agent-card:hover{border-color:var(--blue);transform:translateY(-2px);box-shadow:var(--shadow-md)}
.agent-card .row{display:flex;align-items:center;gap:8px}
.agent-card .avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.agent-card.coordinator .avatar{background:var(--blue-bg);color:var(--blue)}
.agent-card.worker .avatar{background:var(--cyan-bg);color:var(--cyan)}
.agent-card.done .avatar{background:var(--emerald-bg);color:var(--emerald)}
.agent-card.failed .avatar{background:var(--rose-bg);color:var(--rose)}
.agent-card .name{font-size:13px;font-weight:600;flex:1}
.agent-card .role{font-size:10px;font-family:var(--mono);color:var(--text3);padding:2px 8px;background:var(--surface);border-radius:6px}
.agent-card .desc{font-size:11px;color:var(--text3);margin-top:6px;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.agent-detail{display:none;padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px;animation:fadeIn .25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.agent-detail .dt-row{display:flex;justify-content:space-between;padding:4px 0;font-size:11px;border-bottom:1px solid var(--border)}
.agent-detail .dt-row:last-child{border:none}
.agent-detail .dt-row .k{color:var(--text3)}
.agent-detail .dt-row .v{font-family:var(--mono);color:var(--text)}

.empty{color:var(--text4);font-size:12px;text-align:center;padding:40px 16px}

/* RESULT OVERLAY */
.result-overlay{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:28px 32px;max-width:640px;width:90%;box-shadow:var(--shadow-lg);z-index:20;animation:popIn .3s ease}
@keyframes popIn{from{opacity:0;transform:translate(-50%,-50%) scale(.9)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}
.result-overlay .title{font-size:15px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.result-overlay .body{font-size:13px;line-height:1.8;color:var(--text2);max-height:400px;overflow-y:auto}
.result-overlay .body h1,.result-overlay .body h2,.result-overlay .body h3{color:var(--text);margin:16px 0 8px;font-weight:700}
.result-overlay .body h1{font-size:18px}.result-overlay .body h2{font-size:15px}.result-overlay .body h3{font-size:13px}
.result-overlay .body p{margin:8px 0}
.result-overlay .body ul,.result-overlay .body ol{margin:8px 0;padding-left:20px}
.result-overlay .body li{margin:4px 0}
.result-overlay .body code{background:var(--surface2);padding:2px 6px;border-radius:4px;font-family:var(--mono);font-size:12px;color:var(--blue)}
.result-overlay .body pre{background:var(--surface2);padding:14px;border-radius:8px;margin:10px 0;overflow-x:auto}
.result-overlay .body pre code{background:none;padding:0;color:var(--text)}
.result-overlay .body strong{color:var(--text);font-weight:700}
.result-overlay .body em{font-style:italic}
.result-overlay .body blockquote{border-left:3px solid var(--blue);padding-left:14px;margin:10px 0;color:var(--text3)}
.result-overlay .body table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}
.result-overlay .body th,.result-overlay .body td{padding:8px 12px;border:1px solid var(--border);text-align:left}
.result-overlay .body th{background:var(--surface2);font-weight:600}
.result-overlay .close{position:absolute;top:12px;right:14px;width:28px;height:28px;display:flex;align-items:center;
  justify-content:center;border-radius:50%;background:var(--surface2);border:1px solid var(--border);cursor:pointer;
  font-size:14px;color:var(--text3);transition:all .15s}
.result-overlay .close:hover{background:var(--rose-bg);color:var(--rose);border-color:var(--rose)}
</style>
</head>
<body>

<div class="app">
  <div class="hdr">
    <div class="logo">Nexus</div>
    <div class="logo-sub">MULTI-AGENT</div>
    <div class="sep"></div>
    <div class="status"><span class="dot"></span> 系统在线</div>
    <div class="hdr-right">
      <span>模型 <b id="model">---</b></span>
      <span>并发 <b id="maxConc">---</b></span>
    </div>
  </div>

  <div class="canvas-wrap">
    <canvas id="canvas"></canvas>

    <div class="result-overlay" id="resultOverlay">
      <div class="close" onclick="closeResult()">×</div>
      <div class="title">&#10003; 任务完成</div>
      <div class="body" id="resultBody"></div>
    </div>

    <div class="bottom-bar">
      <div class="bottom-inner">
        <input id="taskInput" class="inp" placeholder="描述你的任务..." />
        <button id="submitBtn" class="btn" onclick="submitTask()">部署执行</button>
      </div>
      <div class="tags">
        <span class="tag" onclick="fillTask('分析 Transformer 论文的核心贡献')">Transformer 论文分析</span>
        <span class="tag" onclick="fillTask('对比 BERT 和 GPT 的架构差异')">BERT vs GPT 对比</span>
        <span class="tag" onclick="fillTask('计算 2 的 10 次方')">数学计算</span>
        <span class="tag" onclick="fillTask('搜索关于 attention 机制的论文')">论文检索</span>
      </div>
    </div>
  </div>

  <div class="side">
    <div class="side-hd">
      <div class="ico">&#9672;</div>
      <div class="title">智能体</div>
      <div class="cnt" id="agentCnt">0</div>
    </div>
    <div class="side-body" id="agentList">
      <div class="empty">等待任务...</div>
    </div>
  </div>
</div>

<script>
/* ═══════════════════════════════════════
   CANVAS AGENT VISUALIZATION ENGINE
   ═══════════════════════════════════════ */

const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
let W, H, dpr;

function resize() {
  dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  W = rect.width; H = rect.height;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
resize();
window.addEventListener('resize', resize);

/* ── Colors ── */
const COLORS = {
  blue: '#4f6ef7', cyan: '#0ea5e9', violet: '#8b5cf6',
  emerald: '#10b981', rose: '#f43f5e', amber: '#f59e0b',
  bg: '#f8f9fc', grid: '#ecedf2', text: '#1a1d2e', text2: '#8b8fa8'
};

const WORKER_COLORS = ['#4f6ef7','#0ea5e9','#8b5cf6','#f59e0b','#10b981','#f43f5e','#ec4899','#06b6d4'];

/* ── Easing ── */
function easeInOut(t) { return t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t+2,3)/2; }
function easeOut(t) { return 1 - Math.pow(1-t,3); }

/* ── Agent class ── */
class Agent {
  constructor(id, role, label, x, y) {
    this.id = id;
    this.role = role; // 'coordinator' | 'worker'
    this.label = label;
    this.x = x; this.y = y;
    this.targetX = x; this.targetY = y;
    this.fromX = x; this.fromY = y;
    this.radius = role === 'coordinator' ? 36 : 26;
    this.color = role === 'coordinator' ? COLORS.blue : WORKER_COLORS[agents.length % WORKER_COLORS.length];
    this.state = 'idle'; // idle | moving | working | returning | done | failed
    this.progress = 0;
    this.moveProgress = 0;
    this.moveDuration = 1200;
    this.born = performance.now();
    this.pulsePhase = Math.random() * Math.PI * 2;
    this.floatPhase = Math.random() * Math.PI * 2;
    this.workstation = null;
    this.desc = '';
    this.opacity = 0;
    this.showInSidebar = true;
  }

  moveTo(tx, ty, duration) {
    this.fromX = this.x; this.fromY = this.y;
    this.targetX = tx; this.targetY = ty;
    this.moveProgress = 0;
    this.moveDuration = duration || 1200;
    this.state = 'moving';
  }

  update(dt) {
    const age = performance.now() - this.born;
    this.opacity = Math.min(1, age / 400);

    if (this.state === 'moving') {
      this.moveProgress += dt / this.moveDuration;
      if (this.moveProgress >= 1) {
        this.moveProgress = 1;
        this.x = this.targetX;
        this.y = this.targetY;
        if (this.state === 'moving') this.state = 'working';
      } else {
        const t = easeInOut(this.moveProgress);
        // Bezier curve with slight arc
        const mx = (this.fromX + this.targetX) / 2;
        const my = (this.fromY + this.targetY) / 2 - 60;
        const u = 1 - t;
        this.x = u*u*this.fromX + 2*u*t*mx + t*t*this.targetX;
        this.y = u*u*this.fromY + 2*u*t*my + t*t*this.targetY;
      }
    }

    if (this.state === 'returning') {
      this.moveProgress += dt / this.moveDuration;
      if (this.moveProgress >= 1) {
        this.moveProgress = 1;
        this.x = this.targetX;
        this.y = this.targetY;
        this.state = 'done';
      } else {
        const t = easeInOut(this.moveProgress);
        const mx = (this.fromX + this.targetX) / 2;
        const my = (this.fromY + this.targetY) / 2 - 40;
        const u = 1 - t;
        this.x = u*u*this.fromX + 2*u*t*mx + t*t*this.targetX;
        this.y = u*u*this.fromY + 2*u*t*my + t*t*this.targetY;
      }
    }

    this.pulsePhase += dt * 0.003;
    this.floatPhase += dt * 0.002;
  }

  draw(ctx) {
    ctx.save();
    ctx.globalAlpha = this.opacity;

    const floatY = (this.state === 'working') ? Math.sin(this.floatPhase) * 3 : 0;
    const dx = this.x, dy = this.y + floatY;

    // Glow
    if (this.state === 'working' || this.state === 'moving') {
      const glowR = this.radius + 12 + Math.sin(this.pulsePhase) * 4;
      const grad = ctx.createRadialGradient(dx, dy, this.radius, dx, dy, glowR + 10);
      grad.addColorStop(0, this.color + '20');
      grad.addColorStop(1, this.color + '00');
      ctx.fillStyle = grad;
      ctx.beginPath(); ctx.arc(dx, dy, glowR + 10, 0, Math.PI * 2); ctx.fill();
    }

    // Shadow
    ctx.shadowColor = this.color + '30';
    ctx.shadowBlur = 16;
    ctx.shadowOffsetY = 4;

    // Body
    ctx.fillStyle = '#fff';
    ctx.beginPath(); ctx.arc(dx, dy, this.radius, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

    // Border
    ctx.strokeStyle = this.state === 'done' ? COLORS.emerald :
                      this.state === 'failed' ? COLORS.rose :
                      this.state === 'working' ? this.color : this.color + '80';
    ctx.lineWidth = this.state === 'working' ? 3 : 2;
    ctx.stroke();

    // Icon
    const iconSize = this.role === 'coordinator' ? 20 : 15;
    ctx.font = iconSize + 'px sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillStyle = this.color;
    const icon = this.state === 'done' ? '✓' : this.state === 'failed' ? '✗' :
                 this.state === 'working' ? '⚙' : this.role === 'coordinator' ? '👑' : '●';
    ctx.fillText(icon, dx, dy);

    // Label
    ctx.font = '600 11px "Plus Jakarta Sans", sans-serif';
    ctx.fillStyle = COLORS.text;
    ctx.textAlign = 'center';
    ctx.fillText(this.label, dx, dy + this.radius + 16);

    // State badge
    if (this.state === 'working') {
      ctx.font = '500 9px "JetBrains Mono", monospace';
      ctx.fillStyle = this.color;
      ctx.fillText('执行中', dx, dy + this.radius + 28);
    } else if (this.state === 'done') {
      ctx.font = '500 9px "JetBrains Mono", monospace';
      ctx.fillStyle = COLORS.emerald;
      ctx.fillText('已完成', dx, dy + this.radius + 28);
    } else if (this.state === 'failed') {
      ctx.font = '500 9px "JetBrains Mono", monospace';
      ctx.fillStyle = COLORS.rose;
      ctx.fillText('失败', dx, dy + this.radius + 28);
    }

    // Progress ring (working state)
    if (this.state === 'working') {
      this.progress = Math.min(1, this.progress + 0.002);
      ctx.beginPath();
      ctx.arc(dx, dy, this.radius + 4, -Math.PI/2, -Math.PI/2 + Math.PI*2*this.progress);
      ctx.strokeStyle = this.color;
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    ctx.restore();
  }
}

/* ── Workstation class ── */
class Workstation {
  constructor(x, y, label, color) {
    this.x = x; this.y = y;
    this.label = label;
    this.color = color;
    this.occupied = false;
    this.opacity = 0;
    this.born = performance.now();
  }

  update() {
    const age = performance.now() - this.born;
    this.opacity = Math.min(1, age / 500);
  }

  draw(ctx) {
    ctx.save();
    ctx.globalAlpha = this.opacity;

    const r = 42;
    // Dashed circle
    ctx.beginPath();
    ctx.arc(this.x, this.y, r, 0, Math.PI * 2);
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = this.occupied ? this.color + '60' : '#d1d5e060';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.setLineDash([]);

    // Fill if occupied
    if (this.occupied) {
      ctx.fillStyle = this.color + '08';
      ctx.fill();
    }

    // Label
    ctx.font = '500 10px "Plus Jakarta Sans", sans-serif';
    ctx.textAlign = 'center';
    ctx.fillStyle = COLORS.text2;
    ctx.fillText(this.label, this.x, this.y + r + 16);

    ctx.restore();
  }
}

/* ── Particle trail ── */
class Trail {
  constructor(x, y, color) {
    this.x = x; this.y = y; this.color = color;
    this.life = 1; this.r = 3;
  }
  update(dt) { this.life -= dt * 0.003; }
  draw(ctx) {
    if (this.life <= 0) return;
    ctx.save();
    ctx.globalAlpha = this.life * 0.4;
    ctx.fillStyle = this.color;
    ctx.beginPath(); ctx.arc(this.x, this.y, this.r * this.life, 0, Math.PI*2); ctx.fill();
    ctx.restore();
  }
}

/* ── State ── */
let agents = [];
let workstations = [];
let trails = [];
let coordinator = null;
let events = [];
let taskId = null;
let taskDone = false;

/* ── Grid background ── */
function drawGrid() {
  const step = 40;
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 0.5;
  for (let x = 0; x < W; x += step) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }
  for (let y = 0; y < H; y += step) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
}

/* ── Brand watermark ── */
function drawBrand() {
  ctx.save();
  ctx.globalAlpha = 0.06;
  ctx.font = '800 80px "Plus Jakarta Sans", sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = COLORS.text;
  ctx.fillText('NEXUS', W/2, H/2);
  ctx.font = '400 14px "JetBrains Mono", monospace';
  ctx.fillText('MULTI-AGENT SYSTEM', W/2, H/2 + 55);
  ctx.restore();
}

/* ── Main loop ── */
let lastTime = performance.now();
function loop(now) {
  const dt = Math.min(now - lastTime, 50);
  lastTime = now;

  ctx.clearRect(0, 0, W, H);
  drawGrid();

  if (agents.length === 0) drawBrand();

  // Update & draw workstations
  workstations.forEach(ws => { ws.update(); ws.draw(ctx); });

  // Update & draw trails
  trails = trails.filter(t => { t.update(dt); t.draw(ctx); return t.life > 0; });

  // Update & draw agents
  agents.forEach(a => {
    a.update(dt);
    // Add trail for moving agents
    if (a.state === 'moving' || a.state === 'returning') {
      trails.push(new Trail(a.x, a.y, a.color));
    }
    a.draw(ctx);
  });

  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

/* ═══════════════════════════════════════
   EVENT HANDLING
   ═══════════════════════════════════════ */

const evtSource = new EventSource('/api/events');
const evtTypes = ['task_created','task_started','task_completed','task_failed',
                  'subtask_created','subtask_started','subtask_completed','subtask_failed'];
evtTypes.forEach(t => {
  evtSource.addEventListener(t, e => {
    handleEvent(JSON.parse(e.data));
  });
});

function handleEvent(d) {
  events.push(d);
  const type = d.event;
  const data = d.data || {};

  // Ignore events after task is done
  if (taskDone && type !== 'task_created') return;

  if (type === 'task_created') {
    // Reset state
    taskDone = false;
    agents = [];
    workstations = [];
    trails = [];
    coordinator = null;
    // Create coordinator
    const cx = W / 2, cy = H / 2;
    coordinator = new Agent('coordinator', 'coordinator', '协调者', cx, cy);
    coordinator.state = 'idle';
    agents = [coordinator];
    taskId = data.task_id;
    updateSidebar();
  }

  if (type === 'subtask_created') {
    // Create worker and workstation
    const count = agents.filter(a => a.role === 'worker').length;
    const total = Math.max(count + 1, 3);
    const angle = (count / total) * Math.PI * 2 - Math.PI / 2;
    const dist = 160;
    const cx = coordinator ? coordinator.x : W/2;
    const cy = coordinator ? coordinator.y : H/2;
    const wsX = cx + Math.cos(angle) * dist;
    const wsY = cy + Math.sin(angle) * dist;

    const wsColor = WORKER_COLORS[count % WORKER_COLORS.length];
    const ws = new Workstation(wsX, wsY, '工位 ' + (count + 1), wsColor);
    workstations.push(ws);

    const agentId = 'worker_' + Date.now() + '_' + count;
    const worker = new Agent(agentId, 'worker', 'Agent ' + (count + 1), cx, cy);
    worker.color = wsColor;
    worker.workstation = ws;
    worker.desc = data.goal || data.task || '子任务 ' + (count + 1);
    agents.push(worker);

    // Animate: move to workstation
    setTimeout(() => {
      ws.occupied = true;
      worker.moveTo(wsX, wsY, 1200);
    }, 300 + count * 400);

    updateSidebar();
  }

  if (type === 'subtask_started') {
    // Find the worker and mark as working
    const workers = agents.filter(a => a.role === 'worker' && a.state === 'idle');
    if (workers.length > 0) {
      workers[0].state = 'working';
      workers[0].progress = 0;
    }
  }

  if (type === 'subtask_completed') {
    // Worker returns to coordinator
    const workers = agents.filter(a => a.role === 'worker' && (a.state === 'working' || a.state === 'moving'));
    if (workers.length > 0 && coordinator) {
      const w = workers[0];
      if (w.workstation) w.workstation.occupied = false;
      setTimeout(() => {
        w.moveTo(coordinator.x, coordinator.y, 1000);
        setTimeout(() => { w.state = 'done'; updateSidebar(); }, 1100);
      }, 200);
    }
  }

  if (type === 'subtask_failed') {
    const workers = agents.filter(a => a.role === 'worker' && (a.state === 'working' || a.state === 'moving'));
    if (workers.length > 0 && coordinator) {
      const w = workers[0];
      if (w.workstation) w.workstation.occupied = false;
      w.state = 'failed';
      setTimeout(() => {
        w.moveTo(coordinator.x, coordinator.y, 1000);
      }, 200);
      updateSidebar();
    }
  }

  if (type === 'task_completed') {
    taskDone = true;
    // Mark all workers as done
    agents.forEach(a => {
      if (a.role === 'worker' && a.state !== 'done' && a.state !== 'failed') {
        a.state = 'done';
      }
    });
    if (coordinator) {
      coordinator.state = 'idle';
      coordinator.desc = '任务完成';
    }
    // Show result from SSE event if available
    if (data.result) {
      showResult(data.result);
    }
    updateSidebar();
  }

  if (type === 'task_failed') {
    taskDone = true;
    if (coordinator) {
      coordinator.state = 'failed';
      coordinator.desc = '任务失败';
    }
    updateSidebar();
  }
}

/* ═══════════════════════════════════════
   SIDEBAR
   ═══════════════════════════════════════ */

function updateSidebar() {
  const el = document.getElementById('agentList');
  document.getElementById('agentCnt').textContent = agents.length;

  if (agents.length === 0) {
    el.innerHTML = '<div class="empty">等待任务...</div>';
    return;
  }

  el.innerHTML = agents.map((a, i) => {
    const cls = a.role === 'coordinator' ? 'coordinator' :
                a.state === 'done' ? 'done' : a.state === 'failed' ? 'failed' : 'worker';
    const icon = a.role === 'coordinator' ? '👑' : a.state === 'done' ? '✓' : a.state === 'failed' ? '✗' : '●';
    const roleLabel = a.role === 'coordinator' ? '协调者' : 'Worker';
    const stateLabel = a.state === 'idle' ? '空闲' : a.state === 'moving' ? '移动中' :
                       a.state === 'working' ? '执行中' : a.state === 'returning' ? '返回中' :
                       a.state === 'done' ? '已完成' : '失败';
    return '<div class="agent-card ' + cls + '" onclick="focusAgent(' + i + ')">' +
      '<div class="row"><div class="avatar">' + icon + '</div>' +
      '<div class="name">' + a.label + '</div>' +
      '<div class="role">' + roleLabel + '</div></div>' +
      '<div class="desc">' + stateLabel + (a.desc ? ' · ' + a.desc : '') + '</div></div>';
  }).join('');
}

function focusAgent(idx) {
  const a = agents[idx];
  if (!a) return;
  // Smooth pan to agent (simple version: just highlight)
  agents.forEach((ag, i) => {
    ag._highlight = (i === idx);
  });
}

/* ═══════════════════════════════════════
   TASK SUBMISSION
   ═══════════════════════════════════════ */

function fillTask(s) {
  document.getElementById('taskInput').value = s;
  document.getElementById('taskInput').focus();
}

async function submitTask() {
  const input = document.getElementById('taskInput');
  const btn = document.getElementById('submitBtn');
  const task = input.value.trim();
  if (!task) return;

  btn.disabled = true;
  btn.textContent = '部署中...';

  // Reset canvas
  agents = [];
  workstations = [];
  trails = [];
  coordinator = null;
  taskDone = false;

  try {
    const res = await fetch('/api/task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task})
    });
    const data = await res.json();
    if (data.result) showResult(data.result);
  } catch(e) {
    showResult('错误: ' + e.message);
  }

  btn.disabled = false;
  btn.textContent = '部署执行';
}

function renderMarkdown(text) {
  // Simple markdown renderer
  let html = text
    // Code blocks
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
    // Inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Headers
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Blockquote
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    // Unordered list
    .replace(/^[*-] (.+)$/gm, '<li>$1</li>')
    // Ordered list
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    // Horizontal rule
    .replace(/^---$/gm, '<hr>')
    // Line breaks (double newline = paragraph)
    .replace(/\n\n/g, '</p><p>')
    // Single newline
    .replace(/\n/g, '<br>');

  // Wrap consecutive <li> in <ul>
  html = html.replace(/(<li>.*?<\/li>)+/g, '<ul>$&</ul>');
  // Clean up
  html = '<p>' + html + '</p>';
  html = html.replace(/<p><\/p>/g, '').replace(/<p>(<h[123]>)/g, '$1').replace(/(<\/h[123]>)<\/p>/g, '$1');
  html = html.replace(/<p>(<pre>)/g, '$1').replace(/(<\/pre>)<\/p>/g, '$1');
  html = html.replace(/<p>(<ul>)/g, '$1').replace(/(<\/ul>)<\/p>/g, '$1');
  html = html.replace(/<p>(<hr>)/g, '$1').replace(/(<hr>)<\/p>/g, '$1');
  html = html.replace(/<p>(<blockquote>)/g, '$1').replace(/(<\/blockquote>)<\/p>/g, '$1');
  return html;
}

function showResult(text) {
  const overlay = document.getElementById('resultOverlay');
  document.getElementById('resultBody').innerHTML = renderMarkdown(text);
  overlay.style.display = 'block';
}

function closeResult() {
  document.getElementById('resultOverlay').style.display = 'none';
}

/* Init */
refreshStatus();
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('model').textContent = d.config.model;
    document.getElementById('maxConc').textContent = d.config.max_concurrent_agents;
  } catch(e) {}
}

document.getElementById('taskInput').addEventListener('keypress', e => {
  if (e.key === 'Enter') submitTask();
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    print("Starting Nexus Visualizer on http://localhost:8080")
    uvicorn.run(app, host="0.0.0.0", port=8080)
