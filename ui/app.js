/* Inference Pipeline: live view of the demo gateway (gateway/serve.py).
 *
 * Everything shown comes from the gateway: its event stream (/events, the same events it
 * writes to results/.../events.jsonl) and its once-a-second `state` event (queue contents
 * in release order, engine slots, KV). Nothing is simulated here. A saved events.jsonl
 * replays through the same code.
 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const BASE = new URL('..', location.href);
  const params = new URLSearchParams(location.search);

  // ---------------------------------------------------------------- formatting
  const fmtS = (s) => {
    if (s == null || !isFinite(s)) return '–';
    if (s < 1) return `${Math.round(s * 1000)} ms`;
    if (s < 10) return `${s.toFixed(2)} s`;
    if (s < 60) return `${s.toFixed(1)} s`;
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  };
  const fmtN = (n) => (n == null ? '–' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
  const pct = (x) => (x == null ? '–' : `${Math.round(x * 100)}%`);
  const clock = (t) => { const d = new Date(t * 1000); return `${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`; };
  const modelTag = (m) => { const x = /(\d+(?:\.\d+)?)\s*b\b/i.exec(m || ''); return x ? `${x[1]}B` : (m || '?'); };
  const quantile = (xs, q) => {
    const v = xs.filter((x) => x != null && isFinite(x)).sort((a, b) => a - b);
    return v.length ? v[Math.min(v.length - 1, Math.floor(v.length * q))] : null;
  };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const setText = (e, t) => { t = String(t); if (e.textContent !== t) e.textContent = t; };

  const REASONS = {
    kv_pressure: 'KV cache pressure', no_signal: 'engine metrics stale', queue_depth: 'queue full', queue_full: 'queue full',
    new_session_shed: 'busy: new session refused', bucket: 'tenant over token budget', tenant_tokens: 'tenant over token budget',
    ttft_unmeetable: 'TTFT target unmeetable', ttfat_unmeetable: 'answer target unmeetable', deadline_unmeetable: 'deadline unmeetable',
    expired_in_queue: 'expired in queue', unknown_model: 'unknown model', upstream_error: 'engine unreachable', client_disconnect: 'client hung up',
    engine_error: 'engine returned an error', stream_dropped: 'stream dropped mid-answer', upstream_timeout: 'engine timed out',
    invalid_json: 'body is not JSON', model_not_allowed: 'model not allowed', bad_max_tokens: 'bad max_tokens', prompt_too_long: 'prompt too long',
    empty_prompt: 'empty prompt', vision_needs_image: 'vision call without image',
  };
  const LAYER_ORDER = ['edge', 'router', 'admission', 'queue', 'engine', 'client'];
  const LAYER_NAME = { edge: 'Edge (guardrails)', router: 'Router', admission: 'Admission', queue: 'Queue', engine: 'Engine', client: 'Client' };
  const reasonText = (r) => REASONS[r] || (r || 'rejected').replace(/_/g, ' ');
  const REASON_COLOR = (r) => (/unmeetable|bucket|tenant_tokens/.test(r || '') ? 'var(--c429)' : /expired/.test(r || '') ? 'var(--queue)' : 'var(--c503)');
  // The rule behind each refusal, with the thresholds in force (gateway/config.yaml admission:)
  function ruleText(reason) {
    const a = (S.policy && S.policy.admission) || {};
    const q = (S.policy && S.policy.queue) || {};
    const p = (x, d) => `${Math.round(100 * (x ?? d))}%`;
    const slack = a.estimate_slack ?? 1.5;
    return ({
      no_signal: `Engine metrics older than ${a.stale_s ?? 5} s, so the gateway can't see the load: it fails closed.`,
      kv_pressure: `Engine KV cache ≥ ${p(a.kv_shed, 0.9)}: refuse new work before vLLM has to preempt running requests.`,
      new_session_shed: `Engine busy (KV ≥ ${p(a.new_session_kv, 0.8)} or ≥ ${a.new_session_queue ?? 16} queued): no new sessions; sessions already running continue.`,
      queue_depth: `The model's queue is at its bound (${q.max_depth_per_lane ?? 64}): reject now rather than wait without limit.`,
      queue_full: `The model's queue is at its bound (${q.max_depth_per_lane ?? 64}): reject now rather than wait without limit.`,
      tenant_tokens: 'The tenant spent its token budget (token bucket, refilled per minute). Sessions already running get an overdraft.',
      ttft_unmeetable: `Estimated TTFT (queue wait + prompt ÷ prefill rate) > ${slack} × the class's TTFT target.`,
      ttfat_unmeetable: `Estimated first-answer time (TTFT + typical thinking) > ${slack} × the class's target.`,
      deadline_unmeetable: `Estimated total time > ${slack} × the class's deadline.`,
      expired_in_queue: "Waited in the queue longer than its class's queue budget.",
      upstream_error: 'The engine failed or was unreachable.',
      unknown_model: 'No pool serves the requested model.',
      client_disconnect: 'The client closed the connection; the gateway cancelled the call ("cancelled"). Not a failure of the stack.',
      engine_error: 'vLLM answered with an error status, passed through to the client.',
      stream_dropped: 'The engine connection broke mid-answer: the client gets an in-band error.',
      upstream_timeout: 'No token from the engine within the idle timeout.',
    })[reason] || '';
  }

  const PALETTE = ['#5b8def', '#3fae6a', '#c99a2e', '#9a7ae0', '#c9609b', '#3aa7a3', '#c7793b', '#8a9a4a', '#6f86c9', '#b9675d'];
  const colorOf = new Map();
  const tenantColor = (t) => { if (!colorOf.has(t)) colorOf.set(t, PALETTE[colorOf.size % PALETTE.length]); return colorOf.get(t); };

  // ---------------------------------------------------------------- state
  let S;
  function fresh() {
    return {
      calls: new Map(), // req_id -> call
      agents: new Map(), // agent key -> agent
      tenants: new Map(), // tenant -> {name, task, agents:Set, calls, last}
      decisions: [], // admission log, newest first
      responses: [], // returned calls, newest first
      flows: { recv: [], adm: [], disp: [], ret: [] },
      lanes: {}, replicas: [], stateT: null,
      mode: null, features: {}, variant: null, run: null,
      totals: { recv: 0, adm: 0, shed: 0, ret: 0, ok: 0, slo: 0, sloN: 0 },
      hitHist: {}, // replica -> [{t, q, h}]
      policy: null, // admission / queue thresholds and SLA classes (config events, /admin/config)
      rej: new Map(), // reason -> {code, n, last}
      incidents: [], // engine and gateway events, newest first
      eng: {}, // replica -> last seen {pre, kvHigh, kvNew, stale, preT}
      marks: [], // mode / config changes, drawn on the timeline
      ctx: {}, // model -> max_model_len (context window)
      samples: [], // one per state event: {t, kv: {replica: x}, depth: {pool: n}, running: {pool: n}}
    };
  }
  S = fresh();

  let replay = null; // {events, i, t, t0, t1, speed, playing, name}
  let offset = 0; // server clock - local clock (live)
  let silent = false; // no enter animations while rebuilding history
  const now = () => (replay ? replay.t : Date.now() / 1000 + offset);

  // An agent's calls, its compaction summaries and permission checks included (roles compaction and
  // classifier, sent with the agent's own id).
  const AGENT_ROLES = new Set(['lead', 'subagent', 'compaction', 'classifier']);
  function agentKey(ev) { return ev.agent && AGENT_ROLES.has(ev.role) ? ev.agent : `${ev.tenant}::client`; }

  function touchAgent(ev, call) {
    const key = agentKey(ev);
    let a = S.agents.get(key);
    if (!a) {
      const isAgent = key !== `${ev.tenant}::client`;
      a = { key, tenant: ev.tenant, name: isAgent ? ev.agent_name || ev.role || 'agent' : 'client calls',
            role: isAgent ? (ev.role === 'compaction' || ev.role === 'classifier' ? '' : ev.role || '') : 'client',
            parent: ev.parent || '', model: ev.model, calls: 0, last: null, session: ev.session };
      S.agents.set(key, a);
    }
    if (ev.agent_name && a.role !== 'client') a.name = ev.agent_name;
    if (!a.role && (ev.role === 'lead' || ev.role === 'subagent')) a.role = ev.role;
    a.model = call.pool || ev.model || a.model;
    a.last = call;
    let t = S.tenants.get(ev.tenant);
    if (!t) { t = { name: ev.tenant, task: ev.task || '', agents: new Set(), calls: 0, last: 0, first: ev.t }; S.tenants.set(ev.tenant, t); }
    if (ev.task) t.task = ev.task;
    t.agents.add(key);
    t.last = ev.t;
    return a;
  }

  function call(ev) {
    let c = S.calls.get(ev.req_id);
    if (!c) {
      const named = AGENT_ROLES.has(ev.role);
      c = { id: ev.req_id, tenant: ev.tenant, agent: agentKey(ev), name: named ? ev.agent_name || ev.role : ev.sla || 'client', role: ev.role,
            sla: ev.sla, model: ev.model, pool: ev.pool, tRecv: ev.t, tools: [] };
      S.calls.set(ev.req_id, c);
    }
    return c;
  }

  const push = (arr, x, n = 60) => { arr.unshift(x); if (arr.length > n) arr.length = n; };
  const incident = (t, sev, what, detail) => push(S.incidents, { t, sev, what, detail: detail || '' }, 150);

  // ---------------------------------------------------------------- events
  const seenEv = new Set();
  const seenOrder = [];
  function handle(ev) {
    if (!ev || !ev.kind) return;
    if (!replay && ev.kind !== 'state') {
      const id = `${ev.t}|${ev.kind}|${ev.req_id || ''}`;
      if (seenEv.has(id)) return;
      seenEv.add(id); seenOrder.push(id);
      if (seenOrder.length > 20000) seenEv.delete(seenOrder.shift());
    }
    if (!replay && !silent && ev.t) offset = offset * 0.8 + (ev.t - Date.now() / 1000) * 0.2;
    switch (ev.kind) {
      case 'received': {
        const c = call(ev);
        c.tRecv = ev.t; c.prompt = ev.prompt_tokens; c.targets = ev.targets || null; c.toolResults = ev.tool_results;
        c.maxTokens = ev.max_tokens; c.toolGap = ev.tool_gap_s; c.modeAt = S.mode;
        const a = touchAgent(ev, c);
        a.calls += 1;
        S.tenants.get(ev.tenant).calls += 1;
        S.totals.recv += 1; S.flows.recv.push(ev.t);
        break;
      }
      case 'admitted': {
        const c = call(ev);
        c.tAdm = ev.t; c.pool = ev.pool; c.est = ev.estimates || null; c.spilled = ev.spilled;
        S.totals.adm += 1; S.flows.adm.push(ev.t);
        if (ev.spilled) incident(ev.t, 'info', `Spilled ${c.name} (${c.tenant}) from ${modelTag(c.model)} to ${modelTag(ev.pool)}`, 'its own model\'s queue was backed up');
        push(S.decisions, { t: ev.t, ok: true, c }, 200);
        touchAgent(ev, c);
        break;
      }
      case 'queued': {
        const c = call(ev);
        c.tQueued = ev.t; c.pool = ev.pool; c.position = ev.position; c.estWait = ev.est_wait_s;
        break;
      }
      case 'dispatched': {
        const c = call(ev);
        c.tDisp = ev.t; c.pool = ev.pool; c.replica = ev.replica; c.queueWait = ev.queue_wait_s; c.routeReason = ev.reason;
        if (ev.via === 'overflow') { c.via = 'overflow'; c.localPool = ev.pool; c.pool = 'overflow'; c.target = ev.target; }
        S.flows.disp.push(ev.t);
        break;
      }
      case 'overflow': {
        const c = call(ev);
        c.overflow = { decision: ev.decision, target: ev.target, provider: ev.provider, fromReason: ev.from_reason, fromCode: ev.from_code, error: ev.error };
        if (ev.decision === 'send') {
          c.via = 'overflow'; c.overflowFrom = ev.from_reason;
          push(S.decisions, { t: ev.t, ok: false, overflow: true, c }, 200);
        } else if (ev.decision === 'failed') {
          incident(ev.t, 'bad', `Overflow to ${ev.provider || 'the provider'} failed`, ev.error || '');
        }
        break;
      }
      case 'first_token': {
        const c = call(ev);
        c.tFirst = ev.t; c.reasoning = !!ev.reasoning;
        if (!ev.reasoning) c.tAns = ev.t;
        break;
      }
      case 'first_answer': {
        const c = call(ev);
        c.tAns = ev.t;
        break;
      }
      case 'tool_call': {
        const c = call(ev);
        c.tools.push(ev.tool);
        break;
      }
      case 'shed': {
        const c = call(ev);
        c.tDone = ev.t; c.status = ev.code; c.reason = ev.reason; c.message = ev.message; c.est = ev.estimates || c.est;
        c.retryAfter = ev.retry_after_s; c.layer = ev.layer || c.layer;
        if (ev.reason !== 'client_disconnect') {
          S.totals.shed += 1;
          push(S.decisions, { t: ev.t, ok: false, c }, 200);
          if (ev.reason === 'upstream_error') incident(ev.t, 'bad', `Engine error on ${c.pool || c.model}`, ev.message);
        }
        touchAgent(ev, c);
        break;
      }
      case 'done': {
        const c = call(ev);
        Object.assign(c, {
          tDone: ev.t, status: ev.status, reason: ev.shed_reason || c.reason, ttft: ev.ttft_s, ttfat: ev.ttfat_s, e2e: ev.e2e_s,
          queueWait: ev.queue_wait_s, promptTokens: ev.prompt_tokens, cached: ev.cached_tokens, completion: ev.completion_tokens,
          itl: ev.itl_p50_s, itlMax: ev.itl_max_s, finish: ev.finish_reason,
          via: ev.via || c.via, overflowFrom: ev.overflow_from || c.overflowFrom,
          pool: (ev.via || c.via) === 'overflow' ? 'overflow' : (ev.pool || c.pool), replica: ev.replica || c.replica,
        });
        if (Array.isArray(ev.tool_calls) && ev.tool_calls.length) c.tools = ev.tool_calls.map((x) => (typeof x === 'string' ? x : x.name || 'tool'));
        if (c.status === 200) {
          S.totals.ok += 1;
          const m = meets(c);
          if (m != null) { S.totals.sloN += 1; S.totals.slo += m ? 1 : 0; }
        }
        if (c.status !== 499) S.totals.ret += 1;
        if (c.status !== 200) {
          c.layer = ev.layer || c.layer || (c.status === 499 ? 'client' : c.status === 400 ? 'edge' : c.status >= 500 ? 'engine' : 'admission');
          c.why = ev.why || c.why;
          const key = `${c.layer}|${c.status}|${c.reason || ''}`;
          const r = S.rej.get(key) || { key, layer: c.layer, code: c.status, reason: c.reason || '', n: 0, last: null };
          r.n += 1; r.last = c;
          S.rej.set(key, r);
        }
        S.flows.ret.push(ev.t);
        push(S.responses, c);
        touchAgent(ev, c);
        break;
      }
      case 'state': {
        S.lanes = ev.lanes || {}; S.replicas = ev.replicas || []; S.stateT = ev.t;
        if (ev.overflow) S.overflow = ev.overflow;
        if (!S.samples.length || ev.t > S.samples[S.samples.length - 1].t) {  // a replayed older snapshot would draw backwards
          const smp = { t: ev.t, kv: {}, depth: {}, running: {}, runRep: {} };
          for (const r of S.replicas) {
            smp.kv[r.id] = r.kv; smp.running[r.pool] = (smp.running[r.pool] || 0) + (r.inflight || 0); smp.runRep[r.id] = r.inflight || 0;
          }
          for (const [n, l] of Object.entries(S.lanes)) smp.depth[n] = l.depth || 0;
          S.samples.push(smp);
          if (S.samples.length > 3700) S.samples.shift();
        }
        if (S.replicas.length && S.replicas.every((r) => Array.isArray(r.calls))) {
          const live = new Set();
          for (const r of S.replicas) for (const c of r.calls) live.add(c.req_id);
          for (const c of S.calls.values()) {
            if (c.tDisp != null && c.tDone == null && !live.has(c.id) && ev.t - c.tDisp > 3) { c.tDone = ev.t; c.lost = true; }
          }
        }
        if (ev.mode_features) S.features = ev.mode_features;
        const a = (S.policy && S.policy.admission) || {};
        for (const r of S.replicas) {
          const e = S.eng[r.id];
          const kv = r.kv;
          const kvHigh = kv != null && kv >= (a.kv_shed ?? 0.9);
          const kvNew = kv != null && kv >= (a.new_session_kv ?? 0.8);
          const stale = r.healthy === false || (r.signal_age_s != null && r.signal_age_s > (a.stale_s ?? 5));
          if (e) {
            if (r.preemptions > e.pre) {
              incident(ev.t, 'bad', `${r.preemptions - e.pre} request${r.preemptions - e.pre > 1 ? 's' : ''} preempted on ${r.id}`,
                `KV cache ${pct(kv)}: vLLM freed their KV blocks and will recompute them, wasting GPU work`);
            }
            if (kvHigh && !e.kvHigh) incident(ev.t, 'bad', `${r.id} KV cache at ${pct(kv)}`, `≥ ${pct(a.kv_shed ?? 0.9)}: admission now refuses new calls to this engine`);
            else if (kvNew && !e.kvNew) incident(ev.t, 'warn', `${r.id} KV cache at ${pct(kv)}`, `≥ ${pct(a.new_session_kv ?? 0.8)}: new sessions refused, running ones continue`);
            else if (!kvNew && e.kvNew) incident(ev.t, 'ok', `${r.id} KV cache back to ${pct(kv)}`, 'admitting new sessions again');
            if (stale && !e.stale) incident(ev.t, 'bad', `${r.id} metrics stale`, `no fresh /metrics for ${fmtS(r.signal_age_s)}: calls to it are refused (fail closed)`);
            if (!stale && e.stale) incident(ev.t, 'ok', `${r.id} metrics fresh again`, '');
          }
          S.eng[r.id] = { pre: r.preemptions || 0, kvHigh, kvNew, stale,
                          preT: e && r.preemptions > e.pre ? ev.t : e ? e.preT : null, pre0: e ? e.pre0 : r.preemptions || 0 };
          const h = (S.hitHist[r.id] = S.hitHist[r.id] || []);
          h.push({ t: ev.t, q: r.prefix_queries || 0, hh: r.prefix_hits || 0 });
          while (h.length > 40) h.shift();
        }
        break;
      }
      case 'mode':
        if (S.mode && ev.mode !== S.mode) { incident(ev.t, 'info', `Gateway mode → ${ev.mode}`, ''); S.marks.push({ t: ev.t, text: `mode ${ev.mode}` }); }
        S.mode = ev.mode; S.features = ev.features || S.features;
        break;
      case 'config':
        if (ev.pools) for (const [n, pc] of Object.entries(ev.pools)) if (pc.max_model_len) { S.ctx[n] = pc.max_model_len; if (pc.model) S.ctx[pc.model] = pc.max_model_len; }
        if (S.variant && ev.variant && ev.variant !== S.variant) {
          incident(ev.t, 'info', `Config variant → ${ev.variant}`, (ev.changed || []).join(', '));
          S.marks.push({ t: ev.t, text: `${ev.variant}${ev.mode && ev.mode !== S.mode ? ` · ${ev.mode}` : ''}` });
        } else if (S.mode && ev.mode && ev.mode !== S.mode) S.marks.push({ t: ev.t, text: `mode ${ev.mode}` });
        S.mode = ev.mode || S.mode; S.variant = ev.variant; S.features = ev.features || S.features;
        if (ev.admission) S.policy = { admission: ev.admission, queue: ev.queue || {}, sla_classes: ev.sla_classes || {}, routing: ev.routing || {} };
        break;
      case 'run_start':
        S.run = `${ev.exp}/${ev.run}`;
        break;
      case 'run_end':
        break;
      case 'prefix_cache_reset':
        incident(ev.t, ev.ok === false ? 'bad' : 'info', `Prefix cache reset ${ev.ok === false ? 'FAILED on some engine' : 'on all engines'}`, ev.ok === false ? JSON.stringify(ev.result) : 'the next calls start cold');
        break;
      default:
        if (String(ev.kind).startsWith('harness_')) { /* harness events: shown via the calls they cause */ }
    }
    if (ev.mode && !S.mode) S.mode = ev.mode;
    if (ev.variant) S.variant = ev.variant;
    if (ev.run && !S.run) S.run = `${ev.exp || 'live'}/${ev.run}`;
    if (paused && ev.kind !== 'state') pausedEvents += 1;
    scheduleRender();
  }

  function meets(c) {
    const tg = c.targets;
    if (!tg || c.status !== 200) return null;
    if (tg.ttft_s && c.ttft != null && c.ttft > tg.ttft_s) return false;
    if (tg.ttfat_s && c.ttfat != null && c.ttfat > tg.ttfat_s) return false;
    if (tg.deadline_s && c.e2e != null && c.e2e > tg.deadline_s) return false;
    return true;
  }

  function phaseOf(c, t) {
    if (!c) return { cls: 'p-idle', text: 'Idle' };
    if (c.role === 'classifier' && c.tDone == null) return { cls: 'p-thinking', text: 'Checking permission' };
    if (c.role === 'compaction') {  // the agent summarizing its own history
      if (c.tDone == null) return { cls: 'p-thinking', text: 'Compacting' };
      if (!(c.status && c.status >= 400 && c.status !== 499)) return { cls: 'p-tool', text: 'Compacted' };
    }
    if (c.tDone != null) {
      if (c.status && c.status >= 400 && c.status !== 499) return { cls: 'p-rejected', text: `Rejected ${c.status}` };
      if (c.finish === 'tool_calls' || (c.tools.length && c.status === 200)) {
        return t - c.tDone < 600 ? { cls: 'p-tool', text: `Running ${c.tools[c.tools.length - 1] || 'tool'}` } : { cls: 'p-idle', text: 'Idle' };
      }
      return { cls: 'p-done', text: 'Done' };
    }
    if (c.tAns != null) return { cls: 'p-answering', text: 'Answering' };
    if (c.tFirst != null) return { cls: 'p-thinking', text: 'Thinking' };
    if (c.tDisp != null) return { cls: 'p-prefill', text: 'Prefill' };
    if (c.tQueued != null) return { cls: 'p-queued', text: 'Queued' };
    return { cls: 'p-wait', text: 'Admission' };
  }

  // ---------------------------------------------------------------- keyed lists
  function keyed(box, items, key, make, update) {
    const have = new Map();
    for (const e of Array.from(box.children)) {
      if (!e.dataset.key || e.classList.contains('leaving')) continue;
      if (have.has(e.dataset.key)) e.remove(); // a stray duplicate: never keep two rows for one key
      else have.set(e.dataset.key, e);
    }
    const seen = new Set();
    let prev = null;
    for (const it of items) {
      const k = key(it);
      seen.add(k);
      let e = have.get(k);
      if (!e) {
        e = make(it);
        e.dataset.key = k;
        if (!silent && !document.hidden && box.dataset.ready) e.classList.add('fresh');
      }
      update(e, it);
      const ref = prev ? prev.nextSibling : box.firstChild;
      if (e !== ref) box.insertBefore(e, ref);
      prev = e;
    }
    for (const [k, e] of have) {
      if (!seen.has(k)) e.remove();
    }
    for (const e of Array.from(box.children)) if (!e.dataset.key) e.remove();
    box.dataset.ready = '1';
  }
  function emptyNote(box, text) {
    if (box.children.length === 0 || (box.children.length === 1 && box.firstChild.classList.contains('empty'))) {
      if (!box.firstChild) box.append(el('div', 'empty', text)); else setText(box.firstChild, text);
    }
  }
  function cell(e, cls) { return e.querySelector(`.${cls}`); }
  // A list you've scrolled into holds still while you read it; scroll back to the top to resume.
  const reading = (box) => box.scrollTop > 6;

  // ---------------------------------------------------------------- window aggregates
  function windowCalls(t) {
    const t0 = t - tlWin;
    const out = [];
    for (const c of S.calls.values()) if ((c.tDone ?? t) >= t0 && c.tRecv <= t && c.tRecv >= clearedAt && inFocus(c.tenant)) out.push(c);
    return out;
  }
  const isRejected = (c) => c.tDone != null && c.status && c.status !== 200 && c.status !== 499;

  // ---------------------------------------------------------------- renderers
  function renderHeader(t) {
    const live = !replay;
    setText($('runLabel'), `${live ? 'live' : 'replay'} · ${S.run || 'run'}${S.variant && S.variant !== 'default' ? ` · ${S.variant}` : ''}`);
    for (const s of $('modeSeg').children) s.classList.toggle('on', s.dataset.mode === S.mode);
    const f = S.features || {};
    const routing = (S.policy || {}).routing || {};
    const on = [['admission', 'admission'], ['queue', 'gateway queue'],
      ['prefix', routing.policy ? `placement ${routing.policy}` : 'prefix routing'], ['spill', 'spill'], ['priority', 'priority']]
      .filter(([k]) => f[k] && !(k === 'spill' && routing.spill === false)).map(([, n]) => n);
    setText($('featureLine'), on.length ? on.join(' · ') : 'pass-through: no gateway controls');
    const lanes = Object.values(S.lanes);
    const t0 = t - tlWin;
    const wc = windowCalls(t);
    const recv = wc.filter((c) => c.tRecv >= t0).length;
    const adm = wc.filter((c) => c.tAdm != null && c.tAdm >= t0).length;
    const rej = wc.filter((c) => isRejected(c) && c.tDone >= t0).length;
    const ret = wc.filter((c) => c.tDone != null && c.tDone >= t0 && c.status !== 499).length;
    const met = wc.filter((c) => c.tDone >= t0 && c.status === 200).map(meets).filter((m) => m != null);
    setText($('kpiRecv'), fmtN(recv));
    setText($('kpiAdm'), fmtN(adm));
    setText($('kpiShed'), fmtN(rej));
    // Waiting happens in the gateway queue, or (queue off) inside vLLM, where the gateway can't reorder it:
    // count both, and say where. vLLM's own running/waiting counts aren't per tenant, so a tenant focus
    // shows the gateway's view only.
    const gwWait = lanes.reduce((n, l) => n + (l.items || []).filter((it) => inFocus(it.tenant)).length, 0);
    const vWait = focus ? 0 : S.replicas.reduce((n, r) => n + (r.waiting || 0), 0);
    setText($('kpiWait'), vWait ? `${gwWait + vWait} (${vWait} in vLLM)` : String(gwWait));
    $('kpiWait').classList.toggle('bad', vWait > 0);
    const dispatched = S.replicas.reduce((n, r) => n + (r.calls || []).filter((c) => inFocus(c.tenant)).length, 0);
    setText($('kpiRun'), focus ? dispatched : S.replicas.reduce((n, r) => n + (r.running || 0), 0));
    setText($('kpiRet'), fmtN(ret));
    setText($('kpiSlo'), met.length ? pct(met.filter(Boolean).length / met.length) : '–');
    for (const dt of document.querySelectorAll('.kpis dt')) {
      if (!dt.dataset.base) dt.dataset.base = dt.textContent;
      const now = ['Queued', 'Running'].includes(dt.dataset.base);
      setText(dt, now ? `${dt.dataset.base} now` : `${dt.dataset.base} ${winLabel()}`);
    }
    for (const sp of $('tlWindow').children) sp.classList.toggle('on', Number(sp.dataset.win) === tlWin);
    const fb = $('focusBar');
    fb.hidden = !focus;
    if (focus) $('focusText').innerHTML = `Following tenant <b>${focus}</b>: every panel shows only its calls.`;
    const pb = $('btnPause');
    pb.classList.toggle('on', paused);
    setText(pb, paused ? `Resume${pausedEvents ? ` (${pausedEvents} new)` : ''}` : 'Pause');
    const st = $('liveState');
    st.className = `status ${live ? (connected ? 'live' : '') : 'replay'}`;
    setText(st.querySelector('b'), live ? (connected ? 'Live' : 'Offline') : 'Replay');

    const rate = (arr) => { const lo = t - 10; while (arr.length && arr[0] < t - 60) arr.shift(); return arr.filter((x) => x >= lo && x <= t).length / 10; };
    [['flow1', S.flows.recv], ['flow2', S.flows.adm], ['flow3', S.flows.disp], ['flow4', S.flows.ret]].forEach(([id, arr]) => {
      const r = rate(arr);
      setText($(id), `${r < 10 ? r.toFixed(1) : Math.round(r)}/s`);
      $(id).parentElement.title = `${r.toFixed(1)} calls per second ${{ flow1: 'arriving at admission', flow2: 'admitted into the queue', flow3: 'sent to an engine', flow4: 'returned to clients' }[id]} (last 10 s)`;
      $(id).parentElement.classList.toggle('busy', r > 0);
    });
  }

  function renderClients(t) {
    const box = $('tenantList');
    const tenants = Array.from(S.tenants.values()).filter((x) => t - x.last < 900 && x.last >= clearedAt).sort((a, b) => b.first - a.first).slice(0, 12);
    setText($('tenantStat'), `${tenants.length} tenant${tenants.length === 1 ? '' : 's'}`);
    keyed(box, tenants, (x) => x.name, (x) => {
      const d = el('div', 'tenant');
      d.style.setProperty('--stripe', tenantColor(x.name));
      const h = el('div', 'tenant-head');
      h.append(el('span', 'sw'), el('span', 'tn', x.name), el('span', 'tk'), el('span', 'tc'));
      h.title = 'Click to follow this tenant in every panel';
      h.addEventListener('click', () => { focus = focus === x.name ? null : x.name; clearLists(); render(); });
      d.append(h, el('div', 'agents'), el('div', 'more'));
      return d;
    }, (d, x) => {
      setText(cell(d, 'tk'), x.task);
      d.classList.toggle('focused', focus === x.name);
      d.style.opacity = focus && focus !== x.name ? '0.4' : '';
      const agents = Array.from(x.agents).map((k) => S.agents.get(k)).filter(Boolean);
      setText(cell(d, 'tc'), `${agents.length} agent${agents.length === 1 ? '' : 's'} · ${x.calls} calls`);
      // lead first, then its subagents by activity
      const order = agents.sort((a, b) => (a.role === 'lead' ? -1 : b.role === 'lead' ? 1 : a.key < b.key ? -1 : 1));
      const shown = order.slice(0, 7);
      keyed(cell(d, 'agents'), shown, (a) => a.key, (a) => {
        const r = el('div', `row agent${a.role === 'subagent' ? ' sub' : ''}`);
        r.style.setProperty('--stripe', tenantColor(x.name));
        r.append(el('span', 'name'), el('span', 'tag'), el('span', 'phase'));
        return r;
      }, (r, a) => {
        const suffix = a.role === 'subagent' && a.key.length > 6 ? ` ${a.key.slice(-4)}` : '';
        setText(cell(r, 'name'), `${a.name}${suffix}`);
        setText(cell(r, 'tag'), modelTag(a.model));
        if (a.last) r.dataset.req = a.last.id;
        let p = phaseOf(a.last, t);
        if (a.role === 'client') {
          let live = 0;
          for (const c of S.calls.values()) if (c.agent === a.key && c.tDone == null) live += 1;
          p = live ? { cls: 'p-answering', text: `${live} in flight` } : { cls: 'p-idle', text: `${a.calls} calls` };
        }
        const ph = cell(r, 'phase');
        ph.className = `phase ${p.cls}`;
        setText(ph, p.text);
      });
      setText(cell(d, 'more'), order.length > shown.length ? `+${order.length - shown.length} more agents` : '');
    });
    emptyNote(box, 'No clients yet. Start tinyharness or Locust.');
  }

  function renderAdmission(t) {
    const t0 = t - tlWin;
    const wc = windowCalls(t);
    const byReason = new Map();
    for (const c of wc) if (isRejected(c) && c.tDone >= t0) byReason.set(c.reason, (byReason.get(c.reason) || 0) + 1);
    const total = Array.from(byReason.values()).reduce((a, b) => a + b, 0);
    const okN = wc.filter((c) => c.tAdm != null && c.tAdm >= t0).length;
    setText($('admStat'), `${winLabel()}: ${okN} in · ${total} out`);
    const rb = $('admReasons');
    rb.innerHTML = '';
    const bar = el('div', 'bar');
    const seg = (n, c) => { if (!n) return; const i = el('i'); i.style.width = `${(100 * n) / (okN + total || 1)}%`; i.style.background = c; bar.append(i); };
    seg(okN, 'var(--ok)');
    for (const [r, n] of byReason) seg(n, REASON_COLOR(r));
    const lg = el('div', 'reason-legend');
    const add = (txt, c) => { const s = el('span', null, txt); s.style.setProperty('--c', c); lg.append(s); };
    add(`${S.features.admission ? 'admitted' : 'passed (no admission checks)'} ${okN}`, 'var(--ok)');
    const ovfN = wc.filter((c) => c.via === 'overflow' && c.tRecv >= t0).length;
    if (ovfN) add(`sent to overflow ${ovfN}`, 'var(--accent)');
    if (!total) add('nothing rejected', 'var(--faint)');
    for (const [r, n] of byReason) add(`${reasonText(r)} ${n}`, REASON_COLOR(r));
    rb.append(bar, lg, admissionChecks(t, wc, byReason));

    if (reading($('admLog'))) { setText($('admStat'), 'paused: scroll to top to resume'); return; }
    const items = S.decisions.filter((d) => d.t >= clearedAt && (d.system || inFocus(d.c.tenant)) && (admFilter !== 'rejected' || (!d.ok && !d.system))).slice(0, 40);
    keyed($('admLog'), items, (d) => `${d.system ? 'sys' : d.c.id}:${d.ok ? 'a' : 's'}:${d.t}`, (d) => {
      const r = el('div', 'row decision');
      if (d.system) { r.append(el('span', 't'), el('span', 'name'), el('span', 'verdict')); return r; }
      r.style.setProperty('--stripe', tenantColor(d.c.tenant));
      r.append(el('span', 't'), el('span', 'name'), el('span', 'verdict'), el('span', 'why'));
      return r;
    }, (r, d) => {
      setText(cell(r, 't'), clock(d.t));
      if (d.system) { setText(cell(r, 'name'), d.system); return; }
      const c = d.c;
      r.dataset.req = c.id;
      setText(cell(r, 'name'), `${c.name} · ${c.tenant}`);
      const v = cell(r, 'verdict');
      if (d.overflow) {
        v.className = 'verdict ovf';
        setText(v, `${c.overflow ? c.overflow.fromCode : 503} ${reasonText(c.overflowFrom)} → overflow`);
        setText(cell(r, 'why'), `refused locally for lack of capacity; sent to ${(c.overflow && c.overflow.provider) || 'the overflow provider'} (${(c.overflow && c.overflow.target) || ''})`);
      } else if (d.ok) {
        v.className = 'verdict ok';
        setText(v, `${c.est ? 'Admit' : 'Pass'} → ${c.replica || modelTag(c.pool)}${c.spilled ? ' (spill)' : ''}`);  // the instance once dispatched
        const tg = c.targets || {};
        const e = c.est;
        setText(cell(r, 'why'), e
          ? `predicted TTFT ${fmtS(e.ttft_s)} (target ${fmtS(tg.ttft_s)}) · first answer ${fmtS(e.ttfat_s)} (target ${fmtS(tg.ttfat_s)}) · class ${c.sla || '–'}`
          : `class ${c.sla || '–'}${tg.ttft_s != null ? ` · targets TTFT ${fmtS(tg.ttft_s)}, answer ${fmtS(tg.ttfat_s)}` : ''} · not checked (mode ${c.modeAt || S.mode})`);
      } else {
        v.className = `verdict c${c.status}`;
        setText(v, `${c.status} ${reasonText(c.reason)}${c.layer && c.layer !== 'admission' ? ` (${c.layer})` : ''}`);
        setText(cell(r, 'why'), c.message || (c.est ? `predicted TTFT ${fmtS(c.est.ttft_s)}, target ${fmtS((c.targets || {}).ttft_s)}` : ''));
        r.title = ruleText(c.reason);
      }
    });
    emptyNote($('admLog'), admFilter === 'rejected' ? 'Nothing rejected.' : 'Waiting for the first call.');
  }

  // The admission checks, in the order admission runs them: what each looks at right now, how close that is
  // to its refusal threshold, and how many calls it refused in the window. With admission off (mode baseline /
  // route / queue) the rows stay live but dimmed: they show what admission would have done.
  const CHECKS = [
    ['stale', 'Engine signal', 'no_signal', 503], ['kv', 'KV pressure', 'kv_pressure', 503],
    ['queue_bound', 'Queue bound', 'queue_depth', 503], ['new_session', 'New sessions', 'new_session_shed', 503],
    ['bucket', 'Token budget', 'tenant_tokens', 429], ['ttft', 'TTFT estimate', 'ttft_unmeetable', 429],
    ['ttfat', 'First-answer estimate', 'ttfat_unmeetable', 429], ['deadline', 'Deadline estimate', 'deadline_unmeetable', 429],
  ];
  function admissionChecks(t, wc, byReason) {
    const A = (S.policy || {}).admission || {};
    const enabled = new Set(A.checks || []);
    const on = !!S.features.admission;
    const reps = S.replicas || [];
    const maxKv = Math.max(0, ...reps.map((r) => r.kv || 0));
    const maxAge = Math.max(0, ...reps.map((r) => (r.signal_age_s != null && r.signal_age_s >= 0 ? r.signal_age_s : 0)));
    const lanes = Object.values(S.lanes || {});
    const depth = Math.max(0, ...lanes.map((l) => l.depth || 0)), bound = Math.max(1, ...lanes.map((l) => l.bound || 0));
    const slack = A.estimate_slack ?? 1.5;
    // the worst recent prediction against its target (admitted calls carry admission's estimates)
    const worst = (k, tk) => Math.max(0, ...wc.filter((c) => c.est && c.targets && c.targets[tk]).map((c) => (c.est[k] || 0) / c.targets[tk]));
    // per check: [how far to its refusal threshold (1 = refuses), the value against the threshold, the full rule]
    const est = (k, tk) => (on ? worst(k, tk) : null);
    const live = {
      stale: [maxAge / (A.stale_s || 5), `${fmtS(maxAge)} of ${fmtS(A.stale_s ?? 5)}`, `engine metrics older than ${fmtS(A.stale_s ?? 5)} → 503 no_signal`],
      kv: [maxKv / (A.kv_shed || 0.9), `${pct(maxKv)} of ${pct(A.kv_shed ?? 0.9)}`, `the fullest replica's KV cache at ${pct(A.kv_shed ?? 0.9)} → 503 kv_pressure`],
      queue_bound: [depth / bound, `${depth} of ${bound}`, `the gateway queue full (${bound}) → 503 queue_depth`],
      new_session: [Math.max(maxKv / (A.new_session_kv || 0.8), depth / (A.new_session_queue || 16)), `${pct(maxKv)} of ${pct(A.new_session_kv ?? 0.8)}`,
        `a NEW session while KV ≥ ${pct(A.new_session_kv ?? 0.8)} or ${A.new_session_queue ?? 16} queued → 503 new_session_shed`],
      bucket: [null, 'per tenant', 'a tenant over its tokens-per-minute budget → 429 tenant_tokens'],
      ttft: [est('ttft_s', 'ttft_s') != null ? est('ttft_s', 'ttft_s') / slack : null, on ? `${fmtX(est('ttft_s', 'ttft_s'))} of ${slack}×` : '–',
        `predicted TTFT more than ${slack}× its class target → 429 ttft_unmeetable (shown: the worst prediction ÷ target in the window)`],
      ttfat: [est('ttfat_s', 'ttfat_s') != null ? est('ttfat_s', 'ttfat_s') / slack : null, on ? `${fmtX(est('ttfat_s', 'ttfat_s'))} of ${slack}×` : '–',
        `predicted first answer more than ${slack}× its target → 429 ttfat_unmeetable`],
      deadline: [est('total_s', 'deadline_s') != null ? est('total_s', 'deadline_s') / slack : null, on ? `${fmtX(est('total_s', 'deadline_s'))} of ${slack}×` : '–',
        `predicted whole call more than ${slack}× its deadline → 429 deadline_unmeetable`],
    };
    const box = el('div', `checks${on ? '' : ' off'}`);
    box.append(el('div', 'checks-head', on ? 'How close each check is to refusing a call (bar full = refuses)'
      : `Admission is off (mode ${S.mode}): nothing is refused. How close each check would be:`));
    for (const [k, label, reason, code] of CHECKS) {
      const active = on && enabled.has(k);
      const [frac, value, rule] = live[k];
      const n = byReason.get(reason) || 0;
      const row = el('div', `check${active ? '' : ' inactive'}${frac != null && frac >= 1 ? ' trip' : frac != null && frac >= 0.7 ? ' near' : ''}`);
      const b = el('div', 'check-bar'); const i = el('i'); i.style.width = `${Math.min(100, 100 * (frac || 0))}%`; b.append(i);
      if (frac == null) b.classList.add('none');
      const state = n ? `${n} × ${code}` : frac != null && frac >= 1 ? (active ? 'refusing' : 'would refuse') : active ? 'ok' : 'off';
      row.append(el('span', 'check-name', label), b, el('span', 'check-text', value), el('span', `check-n${n || (frac != null && frac >= 1) ? ' hit' : ''}`, state));
      row.title = `${label}: ${rule}${active ? '' : ' (this check is off now)'}`;
      box.append(row);
    }
    return box;
  }
  const fmtX = (x) => (x ? `${x.toFixed(2)}×` : '–');

  function renderQueue(t) {
    const box = $('laneList');
    const names = Object.keys(S.lanes).sort();
    const depth = names.reduce((n, k) => n + (S.lanes[k].depth || 0), 0);
    const vllmWaiting = S.replicas.reduce((a, r) => a + (r.waiting || 0), 0);
    setText($('queueStat'), !names.length ? '' : S.features.queue ? `${depth} waiting` : `${vllmWaiting} waiting inside vLLM`);
    const age = S.stateT ? Math.max(0, t - S.stateT) : 0;
    keyed(box, names, (n) => n, (n) => {
      const d = el('div', 'lane');
      const h = el('div', 'lane-head');
      h.append(el('span', 'ln', n), el('span', 'lp'));
      const cap = el('div', 'cap');
      const bar = el('div', 'bar'); bar.append(el('i'));
      const ct = el('div', 'cap-text'); ct.append(el('span', 'cl'), el('span', 'cr'));
      cap.append(bar, ct);
      d.append(h, el('div', 'policy-note'), cap, el('div', 'tickets'), el('div', 'off'), el('div', 'released'));
      return d;
    }, (d, n) => {
      const l = S.lanes[n];
      const queueOn = !!S.features.queue;
      setText(cell(d, 'lp'), queueOn ? `policy ${l.policy || '–'} · a new call would wait ~${fmtS(l.est_wait_s)}` : 'gateway queue off');
      cell(d, 'cap').style.display = queueOn ? '' : 'none';
      d.querySelector('.bar i').style.width = `${Math.min(100, (100 * (l.depth || 0)) / Math.max(1, l.bound || 1))}%`;
      setText(cell(d, 'cl'), `${l.depth || 0} waiting (limit ${l.bound || '–'})`);
      setText(cell(d, 'cr'), `avg. length ${l.little_l != null ? l.little_l.toFixed(2) : '–'} (Little's law)`);
      cell(d, 'cr').title = "Little's law: arrival rate × average wait = average number waiting. Compare it with the live count on the left.";
      const POLICY_TEXT = {
        edf_fair: (l) => `Tenants take turns (deficit round-robin: ${fmtN(l.quantum_tokens || 8192)} tokens of credit per turn, a call costs prompt + up to 2k output). Inside a tenant: earliest effective deadline = deadline − ${l.aging_gain ?? 0.5} × time waited.`,
        edf: (l) => `Earliest effective deadline first, across all tenants: deadline − ${l.aging_gain ?? 0.5} × time waited.`,
        fifo: () => 'First come, first served.',
        priority: () => 'Highest SLA-class priority first, then oldest. The lowest class can wait until it expires.',
        sjf: () => 'Smallest call (prompt + expected output) first. Long calls wait.',
      };
      const note = cell(d, 'policy-note');
      const credit = Object.entries(l.credit || {}).filter(([tn]) => inFocus(tn)).map(([tn, c]) => `${tn} ${fmtN(c)}`).join(' · ');
      setText(note, queueOn && l.policy ? `${(POLICY_TEXT[l.policy] || (() => ''))(l)}${l.policy === 'edf_fair' && credit ? ` Credit now: ${credit}.` : ''}` : '');
      const off = cell(d, 'off');
      if (!queueOn) {
        // no gateway queue: the waiting happens inside each vLLM instance, so show it there, live
        off.className = 'off lane-off';
        const reps = S.replicas.filter((r) => r.pool === n);
        if (!off.querySelector('.vq-note')) {
          off.innerHTML = '';
          off.append(el('div', 'vq-note', 'Gateway queue off: each call goes straight to a vLLM instance, which queues it itself (first come, first served). The gateway can\'t reorder or refuse it.'), el('div', 'vq-reps'));
        }
        keyed(off.querySelector('.vq-reps'), reps, (r) => r.id, () => {
          const row = el('div', 'vq');
          const bar = el('div', 'vq-bar'); bar.append(el('i', 'run'), el('i', 'wait'));
          row.append(el('span', 'vq-id'), bar, el('span', 'vq-num'));
          return row;
        }, (row, r) => {
          const slots = Math.max(1, r.slots || 1), run = r.running ?? r.inflight ?? 0, wait = r.waiting || 0;
          setText(cell(row, 'vq-id'), r.id);
          row.querySelector('i.run').style.width = `${Math.min(100, (100 * run) / slots)}%`;
          row.querySelector('i.wait').style.width = `${Math.min(100, (100 * wait) / slots)}%`;
          setText(cell(row, 'vq-num'), `vLLM: ${run}/${slots} running${wait ? ` · ${wait} waiting` : ''}`);
          row.classList.toggle('late', wait > 0);
          row.title = `${r.id}: vLLM is running ${run} of ${slots} sequences and holding ${wait} in its own queue`;
        });
      } else { off.className = 'off'; off.innerHTML = ''; }
      const items = (l.items || []).map((it, i) => ({ ...it, pos: i + 1, call: S.calls.get(it.req_id) }));
      const tickets = cell(d, 'tickets');
      keyed(tickets, items, (it) => it.req_id, (it) => {
        const r = el('div', 'row ticket');
        r.style.setProperty('--stripe', tenantColor(it.tenant));
        const b = el('div', 'budget'); b.append(el('i'));
        r.append(el('span', 'pos'), el('span', 'name'), el('span', 'num'), el('span', 'tsub'), b);
        return r;
      }, (r, it) => {
        r.dataset.req = it.req_id;
        r.classList.toggle('dim', !inFocus(it.tenant));
        setText(cell(r, 'pos'), it.pos === 1 ? 'next' : `#${it.pos}`);
        cell(r, 'pos').style.color = it.pos === 1 ? 'var(--text)' : '';
        setText(cell(r, 'name'), `${it.agent_name || (it.call && it.call.name) || it.role || 'call'} · ${it.tenant}`);
        const waited = (it.waited_s || 0) + age;
        const budget = (it.call && it.call.targets && it.call.targets.queue_budget_s) || null;
        setText(cell(r, 'num'), `${it.sla || ''} · waiting ${fmtS(waited)}`);
        const frac = budget ? Math.min(1, waited / budget) : 0;
        r.querySelector('.budget i').style.width = `${frac * 100}%`;
        r.classList.toggle('late', frac > 0.8);
        r.title = budget ? `waited ${fmtS(waited)} of a ${fmtS(budget)} queue budget` : '';
        setText(cell(r, 'tsub'), it.deadline_in_s != null
          ? `deadline in ${fmtS(Math.max(0, it.deadline_in_s - age))}${it.aged_s ? `, moved up ${fmtS(it.aged_s + age * (l.aging_gain ?? 0.5))} by waiting` : ''} · costs ${fmtN(it.cost_tokens)} tokens · budget ${fmtS(it.budget_s)}`
          : '');
      });
      if (queueOn && !items.length) {
        // why it's empty: a call waits here only when every slot of the pool is busy
        const reps = S.replicas.filter((r) => r.pool === n);
        const busy = reps.reduce((a, r) => a + (r.inflight || 0), 0), all = reps.reduce((a, r) => a + (r.slots || 0), 0);
        const txt = `Empty: a call waits here only when all ${all} slots of ${n} are busy (now ${busy}/${all}), so every call so far went straight to a free slot.`;
        let note = tickets.querySelector('.note');
        if (!note && tickets.children.length === 0) { note = el('div', 'note'); tickets.append(note); }
        if (note) setText(note, txt);
      }
      // the last calls this lane let through, and how long each waited
      const out = Array.from(S.calls.values()).filter((c) => c.pool === n && c.tDisp != null && t - c.tDisp < 120 && c.tRecv >= clearedAt && inFocus(c.tenant))
        .sort((a, b) => b.tDisp - a.tDisp).slice(0, queueOn ? 5 : 8);
      const rel = cell(d, 'released');
      rel.innerHTML = '';
      if (out.length) {
        rel.append(el('div', 'rel-head', queueOn ? 'Recently released' : 'Recently sent: to which instance, and why'));
        for (const c of out) {
          const line = el('div', 'rel');
          line.style.setProperty('--stripe', tenantColor(c.tenant));
          const w = c.queueWait != null ? c.queueWait : c.tDisp - (c.tAdm ?? c.tRecv);
          const where = c.replica ? `→ ${c.replica}${c.routeReason ? ` · ${c.routeReason}` : ''}` : '';
          const waited = w > 0.05 ? `waited ${fmtS(w)}` : queueOn ? 'no wait' : '';
          line.append(el('span', 'name', `${c.name} · ${c.tenant}`), el('span', 'num', [where, waited, `${fmtS(t - c.tDisp)} ago`].filter(Boolean).join(' · ')));
          line.dataset.req = c.id;
          rel.append(line);
        }
      }
    });
    emptyNote(box, 'No lanes yet.');
  }

  function recentHit(id) {
    const h = S.hitHist[id] || [];
    if (h.length < 2) return null;
    const a = h[0], b = h[h.length - 1];
    const dq = b.q - a.q, dh = b.hh - a.hh;
    // Only recent lookups: vLLM's counters run from the engine's start (a prefix-cache reset or the
    // UI's Clear doesn't zero them), so with no traffic lately there is no current rate to show.
    return dq > 0 && dh >= 0 ? dh / dq : null;
  }

  function renderEngines(t) {
    const box = $('engineList');
    const pools = new Map();
    for (const r of S.replicas) { if (!pools.has(r.pool)) pools.set(r.pool, []); pools.get(r.pool).push(r); }
    const names = Array.from(pools.keys()).sort();
    const run = S.replicas.reduce((n, r) => n + (r.inflight || 0), 0), slots = S.replicas.reduce((n, r) => n + (r.slots || 0), 0);
    setText($('engineStat'), slots ? `${run} / ${slots} slots busy` : '');
    const ov = S.overflow;
    if (ov && ov.enabled) names.push('__overflow');
    keyed(box, names, (n) => n, (n) => {
      const d = el('div', 'engine');
      const h = el('div', 'engine-head');
      h.append(el('span', 'en', n === '__overflow' ? 'overflow' : n), el('span', 'eu'));
      d.append(h, el('div', n === '__overflow' ? 'ovf' : 'reps'));
      return d;
    }, (d, n) => {
      if (n === '__overflow') {
        const o = S.overflow || {};
        const live = Array.from(S.calls.values()).filter((c) => c.via === 'overflow' && c.tDone == null);
        setText(cell(d, 'en'), `overflow · ${o.provider || 'provider'}`);
        setText(cell(d, 'eu'), o.dry_run ? 'dry run: counts only' : !o.credentials ? 'no API key: counts only' : `${live.length} in flight (cap ${o.max_inflight})`);
        const box2 = cell(d, 'ovf');
        const targets = Object.entries(o.targets || {}).map(([p, m]) => `${p} → ${m}`).join(' · ') || 'no target models';
        const cnt = o.counts || {};
        const kept = Object.entries(cnt).filter(([k]) => k !== 'send' && k !== 'off').map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ');
        box2.innerHTML = '';
        const g = el('dl', 'gauges');
        const add = (k, v, tip) => { const x = el('div', 'gauge'); x.title = tip; x.append(el('dt', null, k), el('dd', null, v)); g.append(x); };
        add('Sent', `${o.sent ?? 0}${o.max_calls ? ` / ${o.max_calls}` : ''}`, 'Capacity refusals answered by the provider, against the call budget (max_calls)');
        add('In flight', `${o.inflight ?? 0}`, 'Overflow calls streaming now');
        add('Kept local', `${Object.entries(cnt).filter(([k]) => k !== 'send' && k !== 'off').reduce((a, [, v]) => a + v, 0)}`, `Refusals the gate did not send: ${kept || 'none'}`);
        box2.append(el('div', 'policy-note', `Only 503 / 529 capacity refusals leave; 429, 400 and 500 stay. Targets: ${targets}.`), g);
        for (const c of live) {
          const r = el('div', 'row slot');
          r.style.setProperty('--stripe', tenantColor(c.tenant));
          r.dataset.req = c.id;
          const p = phaseOf(c, t);
          r.append(el('span', 'ix', '·'), el('span', 'name', `${c.name} · ${c.tenant}`), el('span', `ph phase ${p.cls}`, p.text), el('span', 'num', fmtS(t - (c.tDisp || t))));
          box2.append(r);
        }
        return;
      }
      const reps = pools.get(n);
      const inf = reps.reduce((a, r) => a + (r.inflight || 0), 0), sl = reps.reduce((a, r) => a + (r.slots || 0), 0);
      setText(cell(d, 'eu'), `${inf}/${sl} running${reps.length > 1 ? ` · ${reps.length} replicas` : ''}`);
      const repBox = cell(d, 'reps');
      repBox.classList.toggle('multi', reps.length > 1);  // replicas side by side: each vLLM instance in its own column
      repBox.style.gridTemplateColumns = reps.length > 1 ? `repeat(${Math.min(reps.length, 3)}, minmax(0, 1fr))` : '';
      keyed(repBox, reps, (r) => r.id, (r) => {
        const w = el('div', 'replica');
        const g = el('dl', 'gauges');
        for (const k of ['kv', 'hit', 'gap', 'vllm', 'pre', 'place']) {
          const gd = el('div', `gauge g-${k}`);
          gd.append(el('dt', null, { kv: 'KV cache used', hit: 'Prefix-cache hits (40 s)', gap: 'Token gap (60 s)', vllm: 'vLLM running / waiting', pre: 'Preempted (total)', place: 'Sent here (60 s)' }[k]), el('dd'));
          gd.title = {
            kv: 'Share of this engine\'s KV cache (conversation memory) in use. The red mark is where admission starts refusing calls.',
            hit: 'Share of prompt tokens served from the prefix cache over the last ~40 s, so not recomputed.',
            gap: 'Typical time between streamed tokens for calls that finished on this engine in the last 60 s (p50, and p95). It rises when the GPU is shared with a busy neighbour.',
            vllm: 'Requests vLLM itself is running / holding in its own queue. Waiting should stay 0 when the gateway queue is on.',
            pre: 'Requests vLLM preempted since it started: their KV was freed and will be recomputed. ↑ = new in the last minute.',
            place: 'Calls the gateway sent to this replica in the last 60 s (finished or not; the slots below show only those running now), by the router\'s reason: prefix = this replica already held the prompt\'s prefix (KV-aware placement), p2c = the less loaded of two, round_robin / single = no choice made on load or cache. Then the share of those calls\' prompt tokens vLLM served from its prefix cache.',
          }[k];
          if (k === 'kv' || k === 'hit') { const b = el('div', 'bar'); b.append(el('i')); gd.append(b); }
          g.append(gd);
        }
        const strip = el('div', 'strip');
        strip.append(el('div', 'cells'), el('span', 'strip-text'));
        w.append(el('div', 'replica-label'), g, strip, el('div', 'slots'));
        return w;
      }, (w, r) => {
        setText(cell(w, 'replica-label'), reps.length > 1 ? r.id : '');
        // calls on this replica: the gateway's own list (its state, every ~1 s) is the truth; add only calls sent
        // after that snapshot. (Trusting every call the page thinks is running left ghosts in the slots when a
        // 'done' event was missed, e.g. across an event-stream reconnect, and the list looked frozen.)
        const ids = new Set((r.calls || []).map((c) => c.req_id));
        for (const c of S.calls.values()) {
          if (c.replica === r.id && c.tDisp != null && c.tDone == null && (!S.stateT || c.tDisp > S.stateT)) ids.add(c.id);
        }
        const occ = Array.from(ids).map((id) => {
          const c = S.calls.get(id);
          const sc = (r.calls || []).find((x) => x.req_id === id) || {};
          return { id, c, sc };
        }).filter((o) => !(o.c && o.c.tDone != null))
          .sort((a, b) => ((a.c && a.c.tDisp) || 0) - ((b.c && b.c.tDisp) || 0));
        const nSlots = Math.max(r.slots || 0, 0);
        const all = occ.map((o, i) => {
          let p = o.c ? phaseOf(o.c, t) : { cls: `p-${o.sc.phase || 'prefill'}`, text: (o.sc.phase || 'prefill').replace(/^./, (x) => x.toUpperCase()) };
          if (i >= nSlots && p.cls === 'p-prefill') p = { cls: 'p-vllm', text: 'In vLLM queue' };
          return { ...o, ix: i + 1, over: i >= nSlots, p };
        });
        // one row per slot; what waits beyond them is a count (the strip says how many), so the card doesn't grow
        // one row per slot, always: a call fills its slot's row while it runs, a free slot shows as such, so the card
        // keeps its shape; what waits beyond the slots is a count (the strip says how many)
        const rows = Array.from({ length: Math.max(nSlots, 1) }, (_, i) => (all[i] ? { ...all[i], key: `slot-${i}` } : { key: `slot-${i}`, free: true, ix: i + 1 }));
        // the slot strip: one cell per slot, coloured by what it is doing
        const cells = cell(w, 'cells');
        while (cells.children.length < nSlots) cells.append(el('i'));
        while (cells.children.length > nSlots) cells.lastChild.remove();
        Array.from(cells.children).forEach((ci, i) => { ci.className = rows[i] && !rows[i].free ? rows[i].p.cls : ''; });
        const over = Math.max(0, occ.length - nSlots);
        setText(cell(w, 'strip-text'), `${Math.min(occ.length, nSlots)}/${nSlots} slots${over ? ` · ${over} waiting inside vLLM` : ''}`);
        keyed(cell(w, 'slots'), rows, (o) => o.key, (o) => {
          const s = el('div', 'row slot');
          s.append(el('span', 'ix'), el('span', 'name'), el('span', 'ph phase'), el('span', 'num'));
          return s;
        }, (s, o) => {
          setText(cell(s, 'ix'), o.over ? '·' : o.ix);
          s.classList.toggle('free', !!o.free);
          if (o.free) {
            delete s.dataset.req;
            s.classList.remove('dim'); s.style.removeProperty('--stripe');
            setText(cell(s, 'name'), 'free'); cell(s, 'ph').className = 'ph phase'; setText(cell(s, 'ph'), ''); setText(cell(s, 'num'), '');
            s.title = 'an empty slot: vLLM can start another sequence here';
            return;
          }
          const c = o.c, sc = o.sc;
          s.dataset.req = o.id;
          s.classList.toggle('dim', !inFocus((c && c.tenant) || sc.tenant));
          s.style.setProperty('--stripe', tenantColor((c && c.tenant) || sc.tenant));
          setText(cell(s, 'name'), `${(c && c.name) || sc.agent_name || sc.role || 'call'} · ${(c && c.tenant) || sc.tenant}`);
          const p = o.p;
          const ph = cell(s, 'ph');
          ph.className = `ph phase ${p.cls}`;
          setText(ph, p.text);
          const since = c && c.tDisp != null ? t - c.tDisp : sc.running_s;
          setText(cell(s, 'num'), fmtS(since));
          s.title = `${p.text} · ${fmtS(since)} since it was sent to the engine`;
        });
        const kv = r.kv;
        const g = (k) => w.querySelector(`.g-${k}`);
        setText(g('kv').querySelector('dd'), pct(kv));
        const ki = g('kv').querySelector('.bar i');
        ki.style.width = `${Math.round((kv || 0) * 100)}%`;
        ki.classList.toggle('hot', (kv || 0) > 0.85);
        const hit = recentHit(r.id);
        setText(g('hit').querySelector('dd'), pct(hit));
        g('hit').querySelector('.bar i').style.width = `${Math.round((hit || 0) * 100)}%`;
        setText(g('vllm').querySelector('dd'), `${r.running ?? '–'} / ${r.waiting ?? '–'}`);
        const recentDone = Array.from(S.calls.values()).filter((c) => c.replica === r.id && c.tDone != null && t - c.tDone < 60 && c.tRecv >= clearedAt && c.itl != null);
        const gp50 = quantile(recentDone.map((c) => c.itl), 0.5), gp95 = quantile(recentDone.map((c) => c.itl), 0.95);
        setText(g('gap').querySelector('dd'), gp50 != null ? `${fmtS(gp50)} / ${fmtS(gp95)}` : '–');
        const e = S.eng[r.id] || {};
        setText(g('pre').querySelector('dd'), r.preemptions == null ? '–' : `${r.preemptions}${e.preT && t - e.preT < 60 ? ' ↑' : ''}`);
        g('pre').classList.toggle('alert', !!(e.preT && t - e.preT < 60));
        const placed = Array.from(S.calls.values()).filter((c) => c.replica === r.id && c.tDisp != null && t - c.tDisp < 60 && c.tRecv >= clearedAt);
        const why = {};
        for (const c of placed) why[c.routeReason || '?'] = (why[c.routeReason || '?'] || 0) + 1;
        const fin = placed.filter((c) => c.promptTokens);
        const cachedShare = fin.length ? fin.reduce((a, c) => a + (c.cached || 0), 0) / fin.reduce((a, c) => a + c.promptTokens, 0) : null;
        const still = placed.filter((c) => c.tDone == null).length;
        // a short summary, then one chip per routing reason and one for the cache share; '–' when nothing came
        const pdd = g('place').querySelector('dd');
        const chips = Object.entries(why).sort((x, y) => y[1] - x[1]).map(([k, v]) => [`${k} ${v}`, '']);
        if (cachedShare != null) chips.push([`${pct(cachedShare)} cached`, 'cache']);
        const sig = placed.length ? `${placed.length}|${still}|${chips.map((c) => c[0]).join('|')}` : '-';
        if (pdd.dataset.sig !== sig) {
          pdd.dataset.sig = sig;
          pdd.className = placed.length ? 'place' : '';
          pdd.textContent = '';
          if (!placed.length) pdd.textContent = '–';
          else {
            pdd.append(el('span', 'place-main', `${placed.length} call${placed.length > 1 ? 's' : ''}${still ? ` · ${still} running` : ''}`));
            for (const [txt, cls] of chips) pdd.append(el('span', `chip${cls ? ` ${cls}` : ''}`, txt));
          }
        }
        g('kv').classList.toggle('alert', (kv || 0) >= (((S.policy || {}).admission || {}).kv_shed ?? 0.9));
        const kb = g('kv').querySelector('.bar');
        if (!kb.querySelector('b')) kb.append(el('b'));
        kb.querySelector('b').style.left = `${100 * (((S.policy || {}).admission || {}).kv_shed ?? 0.9)}%`;
        kb.title = 'marker: KV level where admission starts refusing calls';
      });
    });
    emptyNote(box, 'Waiting for the gateway state.');
  }

  function renderResponses(t) {
    const recent = S.responses.filter((c) => t - c.tDone < 60 && c.tDone >= clearedAt);
    setText($('respStat'), reading($('respList')) ? 'paused: scroll to top to resume' : `last 60 s: ${recent.length}`);
    if (reading($('respList'))) return;
    const list = S.responses.filter((c) => c.tDone >= clearedAt && inFocus(c.tenant) && (respFilter === 'all' || c.status !== 200)).slice(0, 40);
    keyed($('respList'), list, (c) => c.id, (c) => {
      const r = el('div', 'row resp');
      r.style.setProperty('--stripe', tenantColor(c.tenant));
      r.append(el('span', 'code'), el('span', 'name'), el('span', 'slo'), el('span', 'line2'));
      return r;
    }, (r, c) => {
      r.dataset.req = c.id;
      const code = cell(r, 'code');
      code.className = `code ${c.status === 200 ? 'ok' : `c${c.status}`}`;
      setText(code, c.status || '–');
      setText(cell(r, 'name'), `${c.name} · ${c.tenant}`);
      const slo = cell(r, 'slo');
      const m = meets(c);
      slo.className = `slo ${m === true ? 'met' : m === false ? 'missed' : ''}`;
      setText(slo, m === true ? 'in SLO' : m === false ? 'late' : '');
      if (c.status === 200) {
        const parts = [`TTFT ${fmtS(c.ttft)}`];
        if (c.via === 'overflow') parts.unshift(`via ${(S.overflow && S.overflow.provider) || 'overflow'} (local: ${reasonText(c.overflowFrom)})`);
        if (c.ttfat != null && c.ttft != null && c.ttfat - c.ttft > 0.05) parts.push(`first answer ${fmtS(c.ttfat)}`);
        parts.push(`total ${fmtS(c.e2e)}`);
        if (c.completion != null) parts.push(`${fmtN(c.completion)} output tokens`);
        if (c.tools && c.tools.length) parts.push(`→ ${c.tools[c.tools.length - 1]}`);
        setText(cell(r, 'line2'), parts.join(' · '));
      } else if (c.status === 499) {
        setText(cell(r, 'line2'), 'client disconnected');
      } else {
        const after = c.tDone != null && c.tRecv != null ? fmtS(c.tDone - c.tRecv) : '';
        setText(cell(r, 'line2'), `${LAYER_NAME[c.layer] || c.layer || ''}: ${reasonText(c.reason)} · after ${after}${c.retryAfter ? ` · retry in ${fmtS(c.retryAfter)}` : ''}`);
      }
    });
    emptyNote($('respList'), respFilter === 'err' ? 'No errors in view.' : 'Nothing returned yet.');
  }

  // ---------------------------------------------------------------- incidents
  let admFilter = 'all';
  function renderIncidents(t) {
    const t0 = Math.max(t - tlWin, clearedAt);
    const agg = new Map();
    for (const c of windowCalls(t)) {
      if (c.tDone == null || c.tDone < t0 || !c.status || c.status === 200) continue;
      const layer = c.layer || (c.status === 499 ? 'client' : c.status === 400 ? 'edge' : c.status >= 500 ? 'engine' : 'admission');
      const key = `${layer}|${c.status}|${c.reason || ''}`;
      const r = agg.get(key) || { key, layer, code: c.status, reason: c.reason || '', n: 0, last: null };
      r.n += 1;
      if (!r.last || c.tDone > r.last.tDone) r.last = c;
      agg.set(key, r);
    }
    const rej = Array.from(agg.values()).sort((a, b) => (LAYER_ORDER.indexOf(a.layer) - LAYER_ORDER.indexOf(b.layer)) || (a.code - b.code) || (b.n - a.n));
    const total = rej.filter((r) => r.code !== 499).reduce((n, r) => n + r.n, 0);
    const pre = Object.values(S.eng).reduce((n, e) => n + Math.max(0, e.pre - e.pre0), 0);
    setText($('incStat'), `${winLabel()}: ${total} rejected · ${pre} preempted since load`);
    keyed($('rejTable'), rej, (r) => r.key, () => {
      const d = el('div', 'row rej');
      d.append(el('span', 'rn'), el('span', 'rc'), el('span', 'rule'), el('span', 'ex'));
      return d;
    }, (d, r) => {
      d.style.setProperty('--stripe', r.code === 499 ? 'var(--faint)' : r.code === 429 ? 'var(--c429)' : r.code === 400 ? 'var(--warn)' : 'var(--c503)');
      setText(cell(d, 'rn'), `${LAYER_NAME[r.layer] || r.layer} · ${r.code} ${reasonText(r.reason)}`);
      setText(cell(d, 'rc'), `× ${r.n}`);
      setText(cell(d, 'rule'), ruleText(r.reason) || (r.last && r.last.why) || '');
      const c = r.last;
      setText(cell(d, 'ex'), c ? `latest ${clock(c.tDone)} · ${c.name} · ${c.tenant}${c.message ? ` · ${c.message}` : ''}` : '');
      if (c) d.dataset.req = c.id;
    });
    emptyNote($('rejTable'), 'Every call got a 200 so far.');
    // where in the stack the no's came from
    const byLayer = new Map();
    for (const r of rej) byLayer.set(r.layer, (byLayer.get(r.layer) || 0) + r.n);
    setText($('rejLayers'), LAYER_ORDER.filter((l) => byLayer.get(l)).map((l) => `${LAYER_NAME[l]} ${byLayer.get(l)}`).join('  →  ') || '');
    if (!reading($('engEvents').parentElement)) {
      keyed($('engEvents'), S.incidents.filter((x) => x.t >= t0).slice(0, 50), (x) => `${x.t}|${x.what}`, () => {
        const d = el('div', 'row inc');
        d.append(el('span', 't'), el('span', 'what'), el('span', 'detail'));
        return d;
      }, (d, x) => {
        d.className = `row inc sev-${x.sev}`;
        setText(cell(d, 't'), clock(x.t));
        setText(cell(d, 'what'), x.what);
        setText(cell(d, 'detail'), x.detail);
      });
      emptyNote($('engEvents'), 'No preemptions, KV alarms, stale engines or spills so far.');
    }
  }

  // ---------------------------------------------------------------- timeline
  let tlWin = 300;
  let tlZoom = 120; // timeline span in seconds, or 0 for the header's window
  let focus = null; // a tenant to follow across every panel
  let paused = false, pausedEvents = 0;
  // Clear: panels and counts show only what happened after this moment (the events stay, for the
  // drawer and the timeline). Live only, remembered across reloads; a replay starts uncleared.
  let clearedAt = -Infinity;
  try { clearedAt = Number(localStorage.getItem('clearedAt')) || -Infinity; } catch { /* storage blocked */ }
  let respFilter = 'all';
  let selected = null; // req_id shown in the drawer
  const winLabel = () => (tlWin >= 3600 ? `${tlWin / 3600}h` : `${tlWin / 60}m`);
  const inFocus = (tenant) => !focus || tenant === focus;
  let tlHits = [];
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  function renderTimeline(t) {
    const wrap = $('tlWrap'), cv = $('tlCanvas');
    const W = wrap.clientWidth;
    const span = tlZoom ? Math.min(tlZoom, tlWin) : tlWin;
    const winT0 = t - span, LABEL = 190, RIGHT = 56, AX = 22, ROW = 15, HEAD = 20;
    // rows: tenants (recent first), their agents; nothing from before a Clear
    const tenants = Array.from(S.tenants.values()).filter((x) => x.last > winT0 - 60 && x.last >= clearedAt && inFocus(x.name)).sort((a, b) => b.first - a.first);
    const rows = [];
    for (const tn of tenants) {
      const ag = Array.from(tn.agents).map((k) => S.agents.get(k)).filter(Boolean)
        .sort((a, b) => (a.role === 'lead' ? -1 : b.role === 'lead' ? 1 : a.key < b.key ? -1 : 1));
      rows.push({ head: tn });
      const live = ag.filter((a) => { for (const c of S.calls.values()) if (c.agent === a.key && (c.tDone ?? t) >= winT0 && c.tRecv >= clearedAt) return true; return false; });
      if (!live.length) { rows.pop(); continue; }
      for (const a of live.slice(0, 14)) rows.push({ agent: a, tenant: tn.name });
    }
    // calls by agent, and sub-lanes: calls of one row that overlap in time (many Locust users behind one tenant, an
    // agent's parallel calls) each get their own thin lane instead of being painted on top of each other
    const byAgent = new Map();
    for (const c of S.calls.values()) {
      const end = c.tDone ?? t;
      if (end < winT0 || c.tRecv < clearedAt) continue;
      if (!byAgent.has(c.agent)) byAgent.set(c.agent, []);
      byAgent.get(c.agent).push(c);
    }
    const laneOf = new Map(), MAXROW = 150;
    for (const r of rows) {
      if (!r.agent) continue;
      const calls = (byAgent.get(r.agent.key) || []).sort((p, q) => p.tRecv - q.tRecv);
      const ends = [];
      for (const c of calls) {
        const end = c.tDone ?? t;
        let i = ends.findIndex((e) => e <= c.tRecv);
        if (i < 0) { i = ends.length; ends.push(end); } else ends[i] = end;
        laneOf.set(c.id, i);
      }
      r.lanes = Math.max(1, ends.length);
      r.h = r.lanes <= 1 ? ROW : Math.min(MAXROW, 6 + r.lanes * 6);
    }
    const H = Math.max(wrap.clientHeight - 2, AX + rows.reduce((h, r) => h + (r.head ? HEAD : r.h || ROW), 0) + 8);
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); cv.style.height = `${H}px`;
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const col = { text: cssVar('--text'), muted: cssVar('--muted'), faint: cssVar('--faint'), border: cssVar('--border'),
                  queue: cssVar('--queue'), prefill: cssVar('--prefill'), reason: cssVar('--reason'), answer: cssVar('--answer'),
                  tool: cssVar('--tool'), bad: cssVar('--bad') };
    const font = getComputedStyle(document.body).fontFamily;
    const t0 = winT0;
    const x = (tt) => LABEL + ((tt - t0) / span) * (W - LABEL - RIGHT);

    // axis: about eight ticks at a round step
    const step = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800].find((st) => span / st <= 9) || 3600;
    g.font = `10.5px ${font}`; g.textAlign = 'center'; g.textBaseline = 'middle';
    for (let s = 0; s <= span; s += step) {
      const xx = x(t - s);
      g.fillStyle = col.border; g.fillRect(Math.round(xx), AX, 1, H - AX);
      g.fillStyle = col.faint; g.fillText(s === 0 ? 'now' : s < 60 ? `-${s}s` : `-${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`, xx, AX / 2);
    }
    // mode / config changes: switches a few ms apart (a script resetting, then applying a variant) share one line,
    // labelled with the last of them; labels that would still collide go on the next of three rows
    const shown = [];
    for (const mk of S.marks) {
      if (mk.t < t0 || mk.t < clearedAt) continue;
      const xx = Math.round(x(mk.t)), last = shown[shown.length - 1];
      if (last && xx - last.x < 4) { last.text = mk.text; last.n += 1; continue; }
      shown.push({ x: xx, text: mk.text, n: 1 });
    }
    g.textAlign = 'left'; g.font = `600 10.5px ${font}`;
    const rowEnd = [-Infinity, -Infinity, -Infinity];
    for (const mk of shown) {
      g.fillStyle = col.text; g.globalAlpha = 0.5;
      for (let yy = AX; yy < H; yy += 6) g.fillRect(mk.x, yy, 1, 3);
      g.globalAlpha = 1;
      const label = mk.n > 1 ? `${mk.text} (+${mk.n - 1})` : mk.text;
      const row = rowEnd.findIndex((end) => mk.x + 4 > end);
      if (row < 0) continue;  // no room: the line alone
      g.fillText(label, mk.x + 4, AX + 8 + row * 12);
      rowEnd[row] = mk.x + 4 + g.measureText(label).width + 6;
    }
    g.font = `10.5px ${font}`;
    if (!rows.length) {
      g.fillStyle = col.faint; g.textAlign = 'left'; g.fillText('Sessions appear here as calls arrive.', 12, AX + 18);
      tlHits = []; return;
    }
    tlHits = [];
    let y = AX + 4;
    for (const r of rows) {
      if (r.head) {
        g.textAlign = 'left'; g.font = `600 11px ${font}`; g.fillStyle = col.text;
        g.fillStyle = tenantColor(r.head.name); g.fillRect(8, y + 6, 7, 7);
        g.fillStyle = col.text; g.fillText(r.head.name, 21, y + 10);
        const tw = g.measureText(r.head.name).width;
        g.font = `10.5px ${font}`; g.fillStyle = col.muted;
        if (r.head.task) g.fillText(r.head.task, 27 + tw, y + 10);
        y += HEAD;
        continue;
      }
      const a = r.agent;
      g.textAlign = 'left'; g.font = `11px ${font}`; g.fillStyle = col.muted;
      const label = `${a.role === 'subagent' ? '  └ ' : '  '}${a.name}${a.role === 'subagent' ? ` ${a.key.slice(-4)}` : ''}`;
      g.fillText(label.length > 28 ? `${label.slice(0, 27)}…` : label, 12, y + ROW / 2);
      g.fillStyle = col.faint; g.textAlign = 'right'; g.fillText(modelTag(a.model), LABEL - 10, y + ROW / 2);
      const calls = (byAgent.get(a.key) || []).sort((p, q) => p.tRecv - q.tRecv);
      const rowH = r.h || ROW, multi = (r.lanes || 1) > 1, laneH = multi ? (rowH - 6) / r.lanes : ROW;
      if (multi) { g.fillStyle = col.faint; g.textAlign = 'left'; g.font = `10px ${font}`; g.fillText(`${r.lanes} at once (peak)`, 24, y + ROW / 2 + 13); g.font = `11px ${font}`; }
      let prevEnd = null, prevTool = false;
      for (const c of calls) {
        const lane = laneOf.get(c.id) || 0;
        const bh = multi ? Math.max(2, laneH - 1) : 9, by = multi ? y + 3 + lane * laneH : y + (ROW - bh) / 2;
        // local tool time: the gap after a call that ended in a tool call, until the agent's next call
        if (!multi && prevTool && prevEnd != null && c.tRecv > prevEnd && c.tRecv - prevEnd < 900) {
          g.fillStyle = col.tool; g.fillRect(x(Math.max(prevEnd, t0)), y + ROW / 2 - 0.5, Math.max(0, x(c.tRecv) - x(Math.max(prevEnd, t0))), 1);
        }
        const end = c.tDone ?? t;
        if (c.status && c.status >= 400 && c.status !== 499) {
          g.fillStyle = col.bad; g.fillRect(x(c.tRecv) - 1, multi ? by : y + 1, 2, multi ? bh : ROW - 2);
          tlHits.push({ x0: x(c.tRecv) - 3, x1: x(c.tRecv) + 3, y0: multi ? by : y, y1: multi ? by + bh : y + ROW, c });
          prevEnd = end; prevTool = false; continue;
        }
        const segs = [
          [c.tRecv, c.tDisp ?? (c.tDone ? c.tRecv : end), col.queue],
          [c.tDisp, c.tFirst ?? (c.tDisp != null ? end : null), col.prefill],
          [c.tFirst, c.tAns ?? (c.tFirst != null ? end : null), col.reason],
          [c.tAns, c.tAns != null ? end : null, col.answer],
        ];
        // phases side by side; one of 20 ms or more gets at least 3 px, so a short prefill isn't painted over by the
        // answer that follows it (a few ms of passing through the gateway stays invisible: it isn't waiting)
        let cursor = -Infinity;
        for (const [s0, s1, cl] of segs) {
          if (s0 == null || s1 == null || s1 <= t0 || s1 <= s0) continue;
          const x0 = Math.max(x(Math.max(s0, t0)), cursor);
          const x1 = Math.max(x0 + (s1 - s0 >= 0.02 ? 3 : 1), x(s1));
          g.fillStyle = cl; g.fillRect(x0, by, x1 - x0, bh);
          cursor = x1;
        }
        tlHits.push({ x0: x(Math.max(c.tRecv, t0)), x1: Math.max(x(end), x(Math.max(c.tRecv, t0)) + 3), y0: multi ? by : y, y1: multi ? by + Math.max(bh, 3) : y + ROW, c });
        // duration, after the bar when there's room before this agent's next call (one-lane rows only)
        const nextStart = calls[calls.indexOf(c) + 1] ? x(calls[calls.indexOf(c) + 1].tRecv) : W - 4;
        if (!multi && nextStart - x(end) > 40 && end > t0) {
          g.fillStyle = col.faint; g.font = `10px ${font}`; g.textAlign = 'left';
          g.fillText(fmtS(end - c.tRecv), x(end) + 4, y + ROW / 2);
          g.font = `11px ${font}`;
        }
        prevEnd = end; prevTool = c.finish === 'tool_calls' || (c.tools && c.tools.length > 0);
      }
      y += rowH;
    }
  }

  function tipFor(c) {
    const rows = [];
    const add = (k, v) => { if (v != null && v !== '–') rows.push(`<tr><td>${k}</td><td>${v}</td></tr>`); };
    add('status', c.status ? `${c.status}${c.reason ? ` ${reasonText(c.reason)}` : ''}` : 'in flight');
    add('model', c.pool || c.model);
    add('SLA class', c.sla);
    add('queue wait', c.queueWait != null ? fmtS(c.queueWait) : null);
    add('TTFT', c.ttft != null ? fmtS(c.ttft) : null);
    add('first answer', c.ttfat != null ? fmtS(c.ttfat) : null);
    add('total', c.e2e != null ? fmtS(c.e2e) : null);
    add('prompt / cached', c.promptTokens != null ? `${fmtN(c.promptTokens)} / ${fmtN(c.cached || 0)}` : c.prompt != null ? `~${fmtN(c.prompt)}` : null);
    add('output tokens', c.completion != null ? fmtN(c.completion) : null);
    add('ITL p50', c.itl != null ? fmtS(c.itl) : null);
    if (c.tools && c.tools.length) add('tool call', c.tools.join(', '));
    const esc = (s) => String(s).replace(/[&<>]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m]));
    return `<b>${esc(c.name)}</b> <span class="muted">${esc(c.tenant)}</span><table>${rows.join('')}</table>`;
  }

  // ---------------------------------------------------------------- diagnosis
  // Findings: measured facts only. No advice and no tuned thresholds: each one counts or
  // compares what the gateway and the engines reported, against limits from the config.
  function phaseTimes(c, t) {
    const end = c.tDone ?? t;
    const d = (a, b) => (a != null && b != null && b > a ? b - a : 0);
    return { queue: d(c.tRecv, c.tDisp ?? end), prefill: d(c.tDisp, c.tFirst ?? end), thinking: d(c.tFirst, c.tAns ?? end), answering: d(c.tAns, end) };
  }
  const dominant = (pt) => Object.entries(pt).sort((a, b) => b[1] - a[1])[0];
  function missedWhat(c) {
    const tg = c.targets || {};
    if (tg.ttft_s && c.ttft != null && c.ttft > tg.ttft_s) return ['TTFT', c.ttft, tg.ttft_s];
    if (tg.ttfat_s && c.ttfat != null && c.ttfat > tg.ttfat_s) return ['first answer', c.ttfat, tg.ttfat_s];
    if (tg.deadline_s && c.e2e != null && c.e2e > tg.deadline_s) return ['total time', c.e2e, tg.deadline_s];
    return null;
  }


  // "What happened": one paragraph per failed or late call, built only from its own numbers.
  function storyOf(c, t) {
    if (c.tDone == null) return '';
    const tg = c.targets || {};
    const e = c.est || {};
    const a = (S.policy && S.policy.admission) || {};
    const slack = e.slack || a.estimate_slack || 1.5;
    const ra = c.retryAfter ? ` The client was told to retry in ${fmtS(c.retryAfter)}.` : '';
    const pool = c.pool || c.model || 'the model';
    const queued = c.tQueued ?? c.tAdm;
    const waited = queued != null ? (c.tDisp ?? c.tDone) - queued : null;
    const admittedLine = c.est
      ? `Admission predicted TTFT ${fmtS(e.ttft_s)} against its limit of ${fmtS(slack * (tg.ttft_s || 0))} (${slack} × the ${fmtS(tg.ttft_s)} target), so it let the call in${slack * (tg.ttft_s || 0) - e.ttft_s < 0.25 * (tg.ttft_s || 1) ? ` with only ${fmtS(slack * tg.ttft_s - e.ttft_s)} to spare` : ''}.`
      : (c.tAdm != null ? `Admission checks were off (mode ${c.modeAt || '?'}), so it went straight on.` : '');
    if (c.via === 'overflow' && c.status === 200) {
      const prov = (S.overflow && S.overflow.provider) || 'the overflow provider';
      return `The local ${c.localPool || 'engine'} had no capacity for it (${reasonText(c.overflowFrom)}), so the overflow gate sent it to ${prov}'s ${c.target || 'model'} instead of refusing it. `
        + `It answered: TTFT ${fmtS(c.ttft)}, total ${fmtS(c.e2e)}${c.completion != null ? `, ${fmtN(c.completion)} output tokens` : ''}. The local engines never saw this call.`;
    }
    const ov = c.overflow;
    const ovLine = ov && ov.decision && ov.decision !== 'send' && ov.decision !== 'off' ? ` The overflow gate kept it local: ${{
      stays_local_code: `a ${c.status} never leaves (only 503 / 529 do)`, stays_local_reason: 'this reason is not on the overflow list',
      no_target: `no overflow model is set for ${c.pool || 'this pool'}`, budget_spent: 'the overflow call budget (max_calls) was spent',
      inflight_cap: 'the overflow in-flight cap was reached', dry_run: `dry run: it would have gone to ${ov.target || 'the provider'}`,
      no_credentials: 'no OVERFLOW_API_KEY on the gateway', failed: `the provider failed (${ov.error || 'error'})`,
    }[ov.decision] || ov.decision}.` : '';
    if (ovLine && c.status !== 200 && c.status !== 499) return `${storyOf({ ...c, overflow: null }, t)}${ovLine}`;
    if (c.status === 200) {
      const miss = missedWhat(c);
      if (!miss) return '';
      const pt = phaseTimes(c, t);
      const parts = Object.entries(pt).filter(([, v]) => v > 0.005).map(([k, v]) => `${k} ${fmtS(v)}`).join(', ');
      const qLine = c.tQueued != null ? ` It waited in the ${pool} queue from position #${c.position ?? '?'} for ${fmtS(waited)} (estimated ${fmtS(c.estWait)}).` : '';
      return `${admittedLine}${qLine} Its ${miss[0]} was ${fmtS(miss[1])}, over the ${fmtS(miss[2])} target for the ${c.sla} class. Time by phase: ${parts}.`;
    }
    if (c.status === 499) return `The client closed the connection after ${fmtS(c.tDone - c.tRecv)}, while the call was at "${phaseOf({ ...c, tDone: null }, c.tDone).text}". The gateway cancelled it so the engine stops working on it.`;
    switch (c.reason) {
      case 'expired_in_queue': {
        const b = tg.queue_budget_s;
        return `${admittedLine} It joined the ${pool} queue at position #${c.position ?? '?'} with an estimated wait of ${fmtS(c.estWait)}. `
          + `It actually waited ${fmtS(waited)}${c.estWait ? ` (${(waited / Math.max(c.estWait, 0.05)).toFixed(1)}× the estimate)` : ''}, `
          + `${b ? `past the ${c.sla} class's queue budget of ${fmtS(b)}` : 'past its queue budget'}, so the queue dropped it (503) rather than serve it late. It never reached the GPU.${ra}`;
      }
      case 'ttft_unmeetable': case 'ttfat_unmeetable': case 'deadline_unmeetable': {
        const [what, est, target] = c.reason === 'ttft_unmeetable' ? ['TTFT', e.ttft_s, tg.ttft_s] : c.reason === 'ttfat_unmeetable' ? ['first-answer time', e.ttfat_s, tg.ttfat_s] : ['total time', e.total_s, tg.deadline_s];
        return `Admission predicted ${what} ${fmtS(est)} for this call, over its limit of ${fmtS(slack * (target || 0))} (${slack} × the ${fmtS(target)} target for the ${c.sla} class). `
          + `It refused the call at once (429) instead of letting it miss later. Nothing was queued or sent to the GPU.${ra}`;
      }
      case 'tenant_tokens':
        return `Tenant ${c.tenant} had used up its token budget: this call's reservation didn't fit in what was left of the bucket, so admission refused it (429) before any other check. Other tenants were unaffected.${ra}`;
      case 'kv_pressure':
        return `The ${pool} engine's KV cache was at or above admission.kv_shed (${pct(a.kv_shed ?? 0.9)}): ${c.message || ''}. Admission refused new work (503) so vLLM wouldn't have to preempt running requests.${ra}`;
      case 'new_session_shed':
        return `This was the first call of a session while the engine was busy (${(c.message || '').replace(/: not starting new sessions now$/, '')}). Above KV ${pct(a.new_session_kv ?? 0.8)} or ${a.new_session_queue ?? 16} queued, new sessions are refused (503) and sessions already running continue.${ra}`;
      case 'no_signal':
        return `The gateway had no fresh metrics from the ${pool} engine (older than ${a.stale_s ?? 5} s), so it couldn't see the load and refused the call (503): fail closed.${ra}`;
      case 'queue_depth': case 'queue_full':
        return `The ${pool} queue was full (${c.message || 'at its bound'}), so the call was refused (503) instead of waiting without limit.${ra}`;
      default:
        if (c.layer === 'edge') return `The guardrails refused the request before admission (${c.status}): ${c.message || reasonText(c.reason)}. Nothing reached the queue or the GPU; the client has to fix the request.`;
        if (c.layer === 'engine') return `${c.tDisp != null ? `It was sent to ${c.replica || pool} after waiting ${fmtS(waited)}. ` : ''}The engine failed it (${c.status} ${reasonText(c.reason)})${c.message ? `: ${c.message}` : ''}.`;
        return c.message || '';
    }
  }

  const DIAG_WHERE = { edge: 'panelAdmission', router: 'panelAdmission', admission: 'panelAdmission', queue: 'panelQueue', engine: 'panelEngines', client: 'panelResponses' };
  function diagnose(t) {
    const out = [];
    const t0 = t - tlWin;
    const wc = windowCalls(t);
    const recv = wc.filter((c) => c.tRecv >= t0).length || 1;
    const a = (S.policy && S.policy.admission) || {};
    // non-200 responses, by layer / code / reason
    const groups = new Map();
    for (const c of wc) {
      if (c.tDone == null || c.tDone < t0 || !c.status || c.status === 200) continue;
      const k = `${c.layer || ''}|${c.status}|${c.reason || ''}`;
      const g = groups.get(k) || { n: 0, c, layer: c.layer, code: c.status, reason: c.reason };
      g.n += 1; if (c.tDone >= g.c.tDone) g.c = c;
      groups.set(k, g);
    }
    for (const g of groups.values()) {
      const layer = g.layer || (g.code === 499 ? 'client' : g.code === 400 ? 'edge' : g.code >= 500 ? 'engine' : 'admission');
      out.push({ id: `rej:${layer}|${g.code}|${g.reason}`, rank: 10, sev: g.code >= 500 && layer === 'engine' ? 'bad' : 'warn', layer,
        title: `${g.n} × ${g.code} ${reasonText(g.reason)}`,
        evidence: `${pct(g.n / recv)} of calls · rule: ${ruleText(g.reason) || g.c.why || '–'} · latest: ${g.c.name} · ${g.c.tenant}${g.c.message ? ` · ${g.c.message}` : ''}`,
        example: g.c.id });
    }
    // answers that missed their SLO, and where their time went
    const ok = wc.filter((c) => c.status === 200 && c.tDone >= t0);
    const late = ok.filter((c) => meets(c) === false);
    if (late.length) {
      const tally = { queue: 0, prefill: 0, thinking: 0, answering: 0 };
      const what = new Map();
      for (const c of late) { tally[dominant(phaseTimes(c, t))[0]] += 1; const m = missedWhat(c); if (m) what.set(m[0], (what.get(m[0]) || 0) + 1); }
      const worst = late.slice().sort((x, y) => (y.e2e || 0) - (x.e2e || 0))[0];
      const missed = Array.from(what.entries()).sort((x, y) => y[1] - x[1]).map(([k, v]) => `${k} ${v}`).join(', ');
      const where = Object.entries(tally).filter(([, n]) => n).sort((x, y) => y[1] - x[1]).map(([k, n]) => `${k} ${n}`).join(', ');
      out.push({ id: 'late', rank: 1, sev: 'warn', layer: dominant(tally)[0] === 'queue' ? 'queue' : 'engine',
        title: `${late.length} of ${ok.length} answers missed their SLO`,
        evidence: `missed: ${missed || '–'} · longest phase: ${where}`, example: worst && worst.id });
    }
    // admitted, predicted to make it, then didn't
    const wrong = ok.filter((c) => c.est && c.est.ttft_s != null && c.ttft != null && c.targets && c.targets.ttft_s
      && c.est.ttft_s <= (c.est.slack || 1.5) * c.targets.ttft_s && c.ttft > c.targets.ttft_s);
    if (wrong.length) {
      const ratio = quantile(wrong.map((c) => c.ttft / Math.max(c.est.ttft_s, 0.05)), 0.5);
      out.push({ id: 'under', rank: 2, sev: 'warn', layer: 'admission',
        title: `${wrong.length} admitted call${wrong.length > 1 ? 's' : ''} missed TTFT that admission predicted in time`,
        evidence: `actual TTFT a median ${ratio ? ratio.toFixed(1) : '?'}× the prediction · ${wrong.filter((c) => dominant(phaseTimes(c, t))[0] === 'queue').length} of ${wrong.length} spent most of it in the queue`,
        example: wrong[0].id });
    }
    // engines
    for (const r of S.replicas) {
      if ((r.waiting || 0) > 0) {
        out.push({ id: `vwait:${r.id}`, rank: 3, sev: S.features.queue ? 'warn' : 'bad', layer: 'engine',
          title: `${r.waiting} call${r.waiting > 1 ? 's' : ''} waiting inside vLLM on ${r.id}`,
          evidence: `vLLM running ${r.running}, gateway slots ${r.slots}, gateway queue ${S.features.queue ? 'on' : `off (mode ${S.mode})`}` });
      }
      if (r.kv != null && r.kv >= (a.kv_shed ?? 0.9)) {
        out.push({ id: `kv:${r.id}`, rank: 4, sev: 'bad', layer: 'engine', title: `${r.id} KV cache at ${pct(r.kv)}`,
          evidence: `admission.kv_shed is ${pct(a.kv_shed ?? 0.9)}; admission ${S.features.admission ? 'refuses new calls to it' : 'is off in this mode'}` });
      }
      const pre = S.incidents.filter((x) => x.t >= t0 && x.what.includes('preempted') && x.what.includes(r.id));
      if (pre.length) {
        out.push({ id: `pre:${r.id}`, rank: 5, sev: 'bad', layer: 'engine', title: `Preemptions on ${r.id}`,
          evidence: `${pre.length} preemption event${pre.length > 1 ? 's' : ''} in the window · KV now ${pct(r.kv)}` });
      }
      if ((S.eng[r.id] || {}).stale) {
        out.push({ id: `stale:${r.id}`, rank: 0, sev: 'bad', layer: 'engine', title: `${r.id} metrics are stale`,
          evidence: `no fresh /metrics for ${fmtS(r.signal_age_s)} (admission.stale_s ${a.stale_s ?? 5} s): calls to it are refused` });
      }
    }
    // output cut off at its max_tokens cap
    // (not a check: a harness's /model ping or a probe asks for a token or two on purpose)
    const cut = ok.filter((c) => c.finish === 'length' && !(c.maxTokens && c.maxTokens <= 16));
    if (cut.length) out.push({ id: 'cut', rank: 6, sev: 'warn', layer: 'engine', title: `${cut.length} answer${cut.length > 1 ? 's' : ''} cut off at max_tokens`,
      evidence: `finish_reason "length": the model hit its output cap (max_tokens_by_role) before finishing${cut.some((c) => c.reasoning) ? '; thinking counts toward the cap' : ''}`, example: cut[0].id });
    // prompts close to the context window
    const near = wc.filter((c) => c.tRecv >= t0 && S.ctx[c.pool || c.model] && (c.promptTokens ?? c.prompt ?? 0) >= 0.8 * S.ctx[c.pool || c.model]);
    if (near.length) {
      const big = near.slice().sort((a, b) => (b.promptTokens ?? b.prompt) - (a.promptTokens ?? a.prompt))[0];
      out.push({ id: 'ctx', rank: 6, sev: 'warn', layer: 'engine', title: `${near.length} call${near.length > 1 ? 's' : ''} using ≥ 80% of the context window`,
        evidence: `largest: ${fmtN(big.promptTokens ?? big.prompt)} of ${fmtN(S.ctx[big.pool || big.model])} tokens (${big.name} · ${big.tenant}); past the window vLLM refuses the call`, example: big.id });
    }
    // agents retrying after a refusal
    const refusedAt = new Map();
    let retries = 0, retryEx = null;
    for (const c of wc.slice().sort((a, b) => a.tRecv - b.tRecv)) {
      if (c.role !== 'lead' && c.role !== 'subagent') continue;
      const last = refusedAt.get(c.agent);
      if (last != null && c.tRecv - last < 60) { retries += 1; retryEx = c; }
      if (isRejected(c)) refusedAt.set(c.agent, c.tDone);
    }
    if (retries) out.push({ id: 'retry', rank: 7, sev: 'info', layer: 'client', title: `${retries} agent call${retries > 1 ? 's' : ''} retried after a refusal`,
      evidence: 'the same agent called again within 60 s of a 429 / 503 (tinyharness retries up to request_retries times)', example: retryEx && retryEx.id });
    // expired while newer calls of the same tenant were served (aging didn't save them)
    const starved = wc.filter((c) => c.reason === 'expired_in_queue' && c.tDone >= t0 && c.tQueued != null).filter((c) => {
      for (const d of S.calls.values()) {
        if (d.tenant === c.tenant && d.pool === c.pool && d.tRecv > c.tRecv && d.tDisp != null && d.tDisp > c.tQueued && d.tDisp < c.tDone) return true;
      }
      return false;
    });
    if (starved.length) out.push({ id: 'starve', rank: 6, sev: 'warn', layer: 'queue', title: `${starved.length} call${starved.length > 1 ? 's' : ''} expired while newer calls of the same tenant were served`,
      evidence: `inside one tenant the earliest deadline goes first; aging (${(S.policy && S.policy.queue && S.policy.queue.aging_gain) ?? 0.5} × wait) didn't close the gap before the queue budget ran out`, example: starved[0].id });
    // overflow
    const sentO = wc.filter((c) => c.via === 'overflow' && c.tRecv >= t0);
    if (sentO.length) {
      const by = new Map(); for (const c of sentO) by.set(c.overflowFrom, (by.get(c.overflowFrom) || 0) + 1);
      const okO = sentO.filter((c) => c.status === 200);
      out.push({ id: 'ovf-sent', rank: 8, sev: 'info', layer: 'router', title: `${sentO.length} call${sentO.length > 1 ? 's' : ''} overflowed to ${(S.overflow && S.overflow.provider) || 'the provider'}`,
        evidence: `${okO.length} answered (TTFT p50 ${fmtS(quantile(okO.map((c) => c.ttft), 0.5))}); from: ${Array.from(by.entries()).map(([k, v]) => `${reasonText(k)} ${v}`).join(', ')}`, example: sentO[0].id });
    }
    const keptO = wc.filter((c) => c.overflow && ['budget_spent', 'inflight_cap', 'no_credentials', 'failed', 'dry_run'].includes(c.overflow.decision) && c.tDone >= t0);
    if (keptO.length) {
      const by = new Map(); for (const c of keptO) by.set(c.overflow.decision, (by.get(c.overflow.decision) || 0) + 1);
      out.push({ id: 'ovf-kept', rank: 8, sev: 'warn', layer: 'router', title: `${keptO.length} capacity refusal${keptO.length > 1 ? 's' : ''} could have overflowed but stayed local`,
        evidence: Array.from(by.entries()).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · '), example: keptO[0].id });
    }
    // spills
    const spills = wc.filter((c) => c.spilled && c.tAdm >= t0);
    if (spills.length) out.push({ id: 'spill', rank: 8, sev: 'info', layer: 'router', title: `${spills.length} call${spills.length > 1 ? 's' : ''} spilled to another model`, evidence: 'the 9B lane was backed up at admission', example: spills[spills.length - 1].id });
    return out;
  }

  // Findings keep their place: recomputed every 3 s, ordered by severity then kind (never by a
  // count that changes each second), and kept 20 s after they clear, marked as cleared.
  const held = new Map(); // id -> {issue, seen, sevRank}
  let lastDiag = 0;
  const SEV = { bad: 0, warn: 1, info: 2 };
  let diagOpen = true;
  try { diagOpen = localStorage.getItem('pipeline-findings') !== 'closed'; } catch (e) { /* storage blocked */ }
  function renderDiagnosis(t) {
    $('diagPanel').classList.toggle('closed', !diagOpen);
    setText($('diagToggle'), diagOpen ? 'Hide' : 'Show');
    if (t - lastDiag >= 3 || lastDiag > t) {
      lastDiag = t;
      const fresh = diagnose(t);
      const ids = new Set();
      for (const x of fresh) {
        ids.add(x.id);
        const h = held.get(x.id);
        held.set(x.id, { issue: x, seen: t, sev: h ? Math.min(h.sev, SEV[x.sev]) : SEV[x.sev] });
      }
      for (const [id, h] of held) if (!ids.has(id) && t - h.seen > 20) held.delete(id);
    }
    const issues = Array.from(held.values()).sort((a, b) => (a.sev - b.sev) || (a.issue.rank - b.issue.rank) || (a.issue.id < b.issue.id ? -1 : 1))
      .map((h) => ({ ...h.issue, cleared: t - h.seen > 4 ? t - h.seen : 0 }));
    const active = issues.filter((x) => !x.cleared).length;
    setText($('diagStat'), issues.length ? `${active} active${issues.length > active ? `, ${issues.length - active} cleared` : ''} · last ${winLabel()}${focus ? ` · ${focus}` : ''} · measured, not inferred · updates every 3 s` : `last ${winLabel()} · updates every 3 s`);
    const list = issues.length ? issues : [{ id: 'ok', sev: 'ok', layer: '', title: `Nothing to report in the last ${winLabel()}`, evidence: 'every call got a 200 within its SLO; nothing waited inside vLLM; no KV alarms, preemptions or stale engines' }];
    keyed($('diagList'), list.slice(0, 10), (x) => x.id || 'ok', () => {
      const d = el('div', 'row issue');
      const h = el('div', 'ih'); h.append(el('span', 'il'), el('span', 'it'));
      const acts = el('div', 'ia');
      const ex = el('button', 'btn', 'Open an example'); ex.type = 'button'; ex.dataset.act = 'example';
      const go = el('button', 'btn', 'Show where'); go.type = 'button'; go.dataset.act = 'where';
      acts.append(ex, go);
      d.append(h, el('div', 'ie'), acts);
      return d;
    }, (d, x) => {
      d.className = `row issue sev-${x.sev}${x.cleared ? ' cleared' : ''}`;
      setText(cell(d, 'il'), x.layer || 'all');
      cell(d, 'il').style.display = x.layer ? '' : 'none';
      setText(cell(d, 'it'), x.title);
      setText(cell(d, 'ie'), x.cleared ? `Cleared ${fmtS(x.cleared)} ago. ${x.evidence}` : x.evidence);
      d.title = `${x.title}\n${x.evidence}`;
      const ex = d.querySelector('[data-act="example"]'), go = d.querySelector('[data-act="where"]');
      ex.hidden = !x.example; ex.dataset.req = x.example || '';
      if (!x.example) delete ex.dataset.req;
      go.hidden = !x.layer; go.dataset.where = DIAG_WHERE[x.layer] || '';
      d.querySelector('.ia').hidden = !x.example && !x.layer;
    });
  }

  // ---------------------------------------------------------------- summary view
  let view = 'pipeline';
  try { view = localStorage.getItem('pipeline-view') === 'summary' ? 'summary' : 'pipeline'; } catch (e) { /* storage blocked */ }
  function applyView() {
    $('summaryView').hidden = view !== 'summary';
    document.querySelector('main').hidden = view === 'summary';
    for (const sp of $('viewSeg').children) sp.classList.toggle('on', sp.dataset.view === view);
  }

  function lineChart(cv, t0, t1, series, yFmt, yMaxFixed) {
    cv._args = [t0, t1, series, yFmt, yMaxFixed];
    const W = cv.clientWidth, H = cv.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const font = getComputedStyle(document.body).fontFamily;
    const L = 52, R = 10, T = 22, B = 22;
    let ymax = yMaxFixed || 0;
    if (!yMaxFixed) for (const sr of series) for (const [, v] of sr.points) if (v != null && v > ymax) ymax = v;
    // round axis: the next 1 / 2 / 2.5 / 5 × 10^n above the data
    const nice = (v) => { const e = 10 ** Math.floor(Math.log10(v)); return [1, 2, 2.5, 5, 10].map((m) => m * e).find((m) => m >= v); };
    ymax = yMaxFixed || (ymax > 0 ? nice(ymax * 1.05) : 1);
    const x = (tt) => L + ((tt - t0) / Math.max(1e-6, t1 - t0)) * (W - L - R);
    const y = (v) => T + (1 - v / ymax) * (H - T - B);
    g.font = `10.5px ${font}`; g.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const v = (ymax * i) / 4, yy = Math.round(y(v));
      g.fillStyle = cssVar('--border'); g.fillRect(L, yy, W - L - R, 1);
      g.fillStyle = cssVar('--faint'); g.textAlign = 'right'; g.fillText(yFmt(v), L - 6, yy);
    }
    g.textAlign = 'center';
    for (let i = 0; i <= 4; i++) {
      const tt = t0 + ((t1 - t0) * i) / 4, ago = Math.round(t1 - tt);
      g.fillStyle = cssVar('--faint');
      g.fillText(ago === 0 ? 'now' : ago < 60 ? `-${ago}s` : `-${Math.floor(ago / 60)}:${String(ago % 60).padStart(2, '0')}`, x(tt), H - B / 2);
    }
    let lx = L;
    g.textAlign = 'left';
    for (const sr of series) {
      g.strokeStyle = sr.color; g.lineWidth = sr.width || 1.6; g.beginPath();
      let pen = false, prevT = null;
      for (const [tt, v] of sr.points) {
        if (sr.maxGap && prevT != null && tt - prevT > sr.maxGap) pen = false;  // no line across a gap in the data
        prevT = tt;
        if (v == null) { if (!sr.connect) pen = false; continue; }
        if (!pen) { g.moveTo(x(tt), y(v)); pen = true; } else g.lineTo(x(tt), y(v));
      }
      g.stroke();
      if (sr.dots) {
        g.fillStyle = sr.color;
        for (const [tt, v] of sr.points) if (v != null) { g.beginPath(); g.arc(x(tt), y(v), 2.2, 0, Math.PI * 2); g.fill(); }
      }
      g.fillStyle = sr.color; g.fillRect(lx, T / 2 - 1, 10, 3);
      g.fillStyle = cssVar('--muted'); g.fillText(sr.name, lx + 14, T / 2);
      lx += 22 + g.measureText(sr.name).width;
    }
    // hover: crosshair at the nearest data time, values in a tooltip
    if (cv._hx != null && cv._hx >= L && cv._hx <= W - R) {
      const tt = t0 + ((cv._hx - L) / (W - L - R)) * (t1 - t0);
      let best = null;
      for (const sr of series) for (const [pt, v] of sr.points) if (v != null && (best == null || Math.abs(pt - tt) < Math.abs(best - tt))) best = pt;
      if (best != null) {
        const xx = Math.round(x(best));
        g.fillStyle = cssVar('--muted'); g.globalAlpha = 0.6; g.fillRect(xx, T, 1, H - T - B); g.globalAlpha = 1;
        const rows = [];
        for (const sr of series) {
          let near = null;
          for (const p of sr.points) if (p[1] != null && (near == null || Math.abs(p[0] - best) < Math.abs(near[0] - best))) near = p;
          if (near && Math.abs(near[0] - best) <= Math.max(1, (t1 - t0) / 40)) {
            g.fillStyle = sr.color; g.beginPath(); g.arc(x(near[0]), y(near[1]), 3.5, 0, Math.PI * 2); g.fill();
            rows.push(`<tr><td><i class="sw" style="background:${sr.color}"></i>${sr.name}</td><td>${yFmt(near[1])}</td></tr>`);
          } else rows.push(`<tr><td><i class="sw" style="background:${sr.color}"></i>${sr.name}</td><td>–</td></tr>`);
        }
        const ago = Math.max(0, t1 - best);
        const tip = chartTip();
        tip.innerHTML = `<b>${ago < 1 ? 'now' : `${fmtS(ago)} ago`}</b> <span class="muted">${clock(best)}</span><table>${rows.join('')}</table>`;
        tip.style.display = 'block';
        const rect = cv.getBoundingClientRect();
        const left = rect.left + xx + 14 + tip.offsetWidth > window.innerWidth - 8 ? rect.left + xx - tip.offsetWidth - 14 : rect.left + xx + 14;
        tip.style.left = `${left + window.scrollX}px`;
        tip.style.top = `${rect.top + window.scrollY + T}px`;
      }
    }
    if (!series.some((sr) => sr.points.some(([, v]) => v != null))) {
      g.fillStyle = cssVar('--faint'); g.textAlign = 'center'; g.fillText('No data in this window yet.', (L + W - R) / 2, H / 2);
    }
  }

  let tipEl = null;
  function chartTip() {
    if (!tipEl) { tipEl = el('div', 'tip chart-tip'); document.body.append(tipEl); }
    return tipEl;
  }
  for (const id of ['chThroughput', 'chLatency', 'chKv', 'chQueue']) {
    const cv = $(id);
    cv.addEventListener('mousemove', (e) => {
      cv._hx = e.clientX - cv.getBoundingClientRect().left;
      if (cv._args) lineChart(cv, ...cv._args);
    });
    cv.addEventListener('mouseleave', () => {
      cv._hx = null;
      chartTip().style.display = 'none';
      if (cv._args) lineChart(cv, ...cv._args);
    });
  }

  function renderSummary(t) {
    const t0 = t - tlWin;
    const wc = windowCalls(t);
    const done = wc.filter((c) => c.tDone != null && c.tDone >= t0);
    const ok = done.filter((c) => c.status === 200);
    const metL = ok.map(meets).filter((m) => m != null);
    const good = ok.filter((c) => meets(c) !== false);
    const refused = done.filter(isRejected);
    const recv = wc.filter((c) => c.tRecv >= t0).length;
    const span = Math.max(1, Math.min(tlWin, t - (S.samples[0] ? S.samples[0].t : t)));
    const kpi = (label, value, sub, cls, tip) => `<div class="kpi ${cls || ''}" title="${tip}"><dt>${label}</dt><dd>${value}</dd><div class="sub">${sub}</div></div>`;
    const html = [
      kpi('Calls', fmtN(recv), `${(recv / span).toFixed(2)} / s offered`, '', 'Calls that reached the gateway in the window'),
      kpi('Goodput', `${(good.length / span).toFixed(2)}/s`, `${fmtN(good.length)} answers within SLO`, 'good', 'Completed 200s that met their SLA targets, per second'),
      kpi('Within SLO', metL.length ? pct(metL.filter(Boolean).length / metL.length) : '–', `of ${fmtN(metL.length)} answers`, '', 'Share of 200 responses that met TTFT, first-answer and total targets'),
      kpi('Refused', recv ? pct(refused.length / recv) : '–', `${fmtN(refused.length)} non-200s`, refused.length ? 'bad' : '', 'Non-200 responses from any layer (client hang-ups excluded)'),
      kpi('TTFT p50 / p95', `${fmtS(quantile(ok.map((c) => c.ttft), 0.5))}`, `p95 ${fmtS(quantile(ok.map((c) => c.ttft), 0.95))}`, '', 'Time to the first token of any kind'),
      kpi('First answer p95', fmtS(quantile(ok.map((c) => c.ttfat), 0.95)), `p50 ${fmtS(quantile(ok.map((c) => c.ttfat), 0.5))}`, '', 'Time to the first token of the answer, after any thinking'),
    ].join('');
    if ($('sumKpis').dataset.html !== html) { $('sumKpis').innerHTML = html; $('sumKpis').dataset.html = html; }

    // time series in buckets
    const bs = Math.max(5, Math.round(tlWin / 30));
    setText($('chLatencyNote'), `p95 of calls finished in each ${fmtS(bs)}; a dot = a slot with calls, the line joins them`);
    setText($('chThroughputNote'), `calls / s per ${fmtS(bs)} · offered, completed, within SLO`);
    const nb = Math.ceil(tlWin / bs);
    const bucket = (tt) => Math.floor((tt - t0) / bs);
    const mid = (i) => t0 + (i + 0.5) * bs;
    const zeros = () => Array.from({ length: nb }, () => 0);
    const off = zeros(), comp = zeros(), gp = zeros();
    const ttft = Array.from({ length: nb }, () => []), ttfat = Array.from({ length: nb }, () => []);
    for (const c of wc) {
      if (c.tRecv >= t0) { const i = bucket(c.tRecv); if (i >= 0 && i < nb) off[i] += 1; }
      if (c.tDone != null && c.tDone >= t0 && c.status !== 499) {
        const i = bucket(c.tDone);
        if (i < 0 || i >= nb) continue;
        comp[i] += 1;
        if (c.status === 200) { if (meets(c) !== false) gp[i] += 1; if (c.ttft != null) ttft[i].push(c.ttft); if (c.ttfat != null) ttfat[i].push(c.ttfat); }
      }
    }
    const last = bucket(t);
    const pts = (arr, f) => arr.map((v, i) => [mid(i), i > last ? null : f(v)]);
    lineChart($('chThroughput'), t0, t, [
      { name: 'offered', color: cssVar('--faint'), points: pts(off, (v) => v / bs) },
      { name: 'completed', color: cssVar('--accent'), points: pts(comp, (v) => v / bs) },
      { name: 'within SLO', color: cssVar('--ok'), points: pts(gp, (v) => v / bs), width: 2.2 },
    ], (v) => `${v.toFixed(1)}/s`);
    lineChart($('chLatency'), t0, t, [
      { name: 'TTFT p95', color: cssVar('--prefill'), points: ttft.map((a, i) => [mid(i), a.length ? quantile(a, 0.95) : null]), connect: true, dots: true },
      { name: 'first answer p95', color: cssVar('--reason'), points: ttfat.map((a, i) => [mid(i), a.length ? quantile(a, 0.95) : null]), connect: true, dots: true },
    ], (v) => fmtS(v));
    const smp = S.samples.filter((x) => x.t >= t0 && x.t >= clearedAt);
    const reps = Array.from(new Set(smp.flatMap((x) => Object.keys(x.kv)))).sort();
    lineChart($('chKv'), t0, t, reps.map((id, i) => ({ name: id, color: PALETTE[i % PALETTE.length], points: smp.map((x) => [x.t, x.kv[id] != null ? x.kv[id] * 100 : null]), maxGap: 5 })),
      (v) => `${Math.round(v)}%`, 100);
    const pools = Array.from(new Set(smp.flatMap((x) => Object.keys(x.depth)))).sort();
    const runReps = Array.from(new Set(smp.flatMap((x) => Object.keys(x.runRep || {})))).sort();
    lineChart($('chQueue'), t0, t, [
      ...pools.map((n, i) => ({ name: `${modelTag(n)} queued`, color: PALETTE[(i * 2 + 2) % PALETTE.length], points: smp.map((x) => [x.t, x.depth[n] ?? null]), maxGap: 5 })),
      ...runReps.map((id, i) => ({ name: `${id} running`, color: PALETTE[(i * 2 + 3) % PALETTE.length], points: smp.map((x) => [x.t, (x.runRep || {})[id] ?? null]), maxGap: 5 })),
    ], (v) => v.toFixed(0));

    // per model
    const byPool = new Map();
    for (const c of ok) { const k = c.pool || c.model; if (!byPool.has(k)) byPool.set(k, []); byPool.get(k).push(c); }
    const mhtml = Array.from(new Set([...byPool.keys(), ...S.replicas.map((r) => r.pool)])).sort().map((pool) => {
      const cs = byPool.get(pool) || [];
      const prompt = cs.reduce((a, c) => a + (c.promptTokens || 0), 0), cached = cs.reduce((a, c) => a + (c.cached || 0), 0), out = cs.reduce((a, c) => a + (c.completion || 0), 0);
      const reps2 = S.replicas.filter((r) => r.pool === pool);
      const kv = Math.max(...reps2.map((r) => r.kv || 0), 0);
      const busy = reps2.reduce((a, r) => a + (r.inflight || 0), 0), slots = reps2.reduce((a, r) => a + (r.slots || 0), 0);
      const pre = reps2.reduce((a, r) => a + Math.max(0, ((S.eng[r.id] || {}).pre || 0) - ((S.eng[r.id] || {}).pre0 || 0)), 0);
      const d = (k, v, tip) => `<div title="${tip}"><dt>${k}</dt><dd>${v}</dd></div>`;
      return `<div class="mcard"><h3>${pool}</h3><dl>`
        + d('answers', fmtN(cs.length), 'Completed 200s in the window')
        + d('prompt : output', out ? `${(prompt / out).toFixed(1)} : 1` : '–', 'Prompt tokens per output token. High = prefill-heavy, low = decode-heavy')
        + d('uncached : output', out ? `${((prompt - cached) / out).toFixed(1)} : 1` : '–', 'Only the prompt tokens the engine actually computed (not from the prefix cache), per output token')
        + d('from prefix cache', prompt ? pct(cached / prompt) : '–', 'Share of prompt tokens served from the prefix cache')
        + d('TTFT p50 / p95', `${fmtS(quantile(cs.map((c) => c.ttft), 0.5))} / ${fmtS(quantile(cs.map((c) => c.ttft), 0.95))}`, 'Time to first token')
        + d('first answer p95', fmtS(quantile(cs.map((c) => c.ttfat), 0.95)), 'Time to the first answer token, after thinking')
        + d('token gap p50', fmtS(quantile(cs.map((c) => c.itl), 0.5)), 'Typical time between streamed tokens')
        + d('KV · slots now', `${pct(kv)} · ${busy}/${slots}`, 'KV cache used, and engine slots in use, right now')
        + d('preempted', pre, 'Requests vLLM preempted since this page started watching')
        + d('within SLO', (() => { const m = cs.map(meets).filter((x) => x != null); return m.length ? pct(m.filter(Boolean).length / m.length) : '–'; })(), 'Share of this model\'s answers within their targets')
        + '</dl></div>';
    }).join('');
    if ($('sumModels').dataset.html !== mhtml) { $('sumModels').innerHTML = mhtml; $('sumModels').dataset.html = mhtml; }

    // tenants
    const byT = new Map();
    for (const c of done) { if (!byT.has(c.tenant)) byT.set(c.tenant, []); byT.get(c.tenant).push(c); }
    let th = '<thead><tr><th>Tenant</th><th title="SLA task label">Task</th><th>Calls</th><th title="Share of 200s within their targets">Within SLO</th><th title="Time to first token, 95% faster">TTFT p95</th><th title="Time in the gateway queue, 95% waited less">Queue wait p95</th><th title="Prompt / output tokens">Tokens in / out</th><th title="Non-200s, client hang-ups excluded">Refused</th></tr></thead><tbody>';
    for (const [tn, cs] of Array.from(byT.entries()).sort((a, b) => b[1].length - a[1].length)) {
      const okc = cs.filter((c) => c.status === 200), m = okc.map(meets).filter((x) => x != null);
      th += `<tr><td><i class="sw" style="background:${tenantColor(tn)}"></i>${tn}</td><td>${(S.tenants.get(tn) || {}).task || '–'}</td><td>${cs.length}</td>`
        + `<td>${m.length ? pct(m.filter(Boolean).length / m.length) : '–'}</td><td>${fmtS(quantile(okc.map((c) => c.ttft), 0.95))}</td>`
        + `<td>${fmtS(quantile(okc.map((c) => c.queueWait), 0.95))}</td>`
        + `<td>${fmtN(okc.reduce((a, c) => a + (c.promptTokens || 0), 0))} / ${fmtN(okc.reduce((a, c) => a + (c.completion || 0), 0))}</td>`
        + `<td>${cs.filter(isRejected).length}</td></tr>`;
    }
    if (!byT.size) th += '<tr><td colspan="8" class="muted">No completed calls in this window.</td></tr>';
    th += '</tbody>';
    if ($('sumTenants').dataset.html !== th) { $('sumTenants').innerHTML = th; $('sumTenants').dataset.html = th; }

    // refusals by layer
    const byL = new Map();
    for (const c of refused) {
      const l = c.layer || 'engine';
      const e = byL.get(l) || { n: 0, reasons: new Map() };
      e.n += 1; e.reasons.set(`${c.status} ${reasonText(c.reason)}`, (e.reasons.get(`${c.status} ${reasonText(c.reason)}`) || 0) + 1);
      byL.set(l, e);
    }
    const maxL = Math.max(1, ...Array.from(byL.values()).map((e) => e.n));
    const rh = LAYER_ORDER.filter((l) => byL.has(l)).map((l) => {
      const e = byL.get(l);
      const col = l === 'edge' ? 'var(--warn)' : l === 'admission' ? 'var(--c429)' : 'var(--c503)';
      const top = Array.from(e.reasons.entries()).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join(' · ');
      return `<div class="layerbar" style="--c:${col}"><span>${LAYER_NAME[l]}</span><div class="bar"><i style="width:${(100 * e.n) / maxL}%"></i></div><span class="n">${e.n}</span><span class="why">${top}</span></div>`;
    }).join('') || '<div class="empty">Every call got a 200 in this window.</div>';
    if ($('sumRefusals').dataset.html !== rh) { $('sumRefusals').innerHTML = rh; $('sumRefusals').dataset.html = rh; }
  }

  // ---------------------------------------------------------------- call details
  function openCall(id) {
    if (!S.calls.has(id)) return;
    selected = id;
    $('drawer').hidden = false;
    renderDrawer(now(), true);
  }
  let drawerHtml = '';
  function renderDrawer(t, force) {
    if (!selected || $('drawer').hidden) return;
    const c = S.calls.get(selected);
    if (!c) return;
    const esc = (x) => String(x ?? '').replace(/[&<>"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
    const off = (x) => (x == null ? '' : `+${fmtS(x - c.tRecv)}`);
    const tg = c.targets || {};
    const live = c.tDone == null;
    let verdict;
    if (live) verdict = { c: 'var(--accent)', b: `In flight: ${phaseOf(c, t).text}`, s: `${fmtS(t - c.tRecv)} so far` };
    else if (c.status === 200) {
      const m = meets(c);
      verdict = { c: m === false ? 'var(--warn)' : 'var(--ok)', b: `200 returned${m === true ? ', within its SLO' : m === false ? ', but late for its SLO' : ''}`,
                  s: m === false ? `targets: TTFT ${fmtS(tg.ttft_s)}, first answer ${fmtS(tg.ttfat_s)}, total ${fmtS(tg.deadline_s)}` : '' };
    } else if (c.status === 499) verdict = { c: 'var(--faint)', b: 'Cancelled: the client hung up', s: 'not a failure of the stack' };
    else {
      verdict = { c: c.status === 429 ? 'var(--c429)' : 'var(--c503)',
                  b: `${c.status} refused at ${LAYER_NAME[c.layer] || c.layer || '?'}: ${reasonText(c.reason)}`,
                  s: `Rule: ${ruleText(c.reason) || c.why || '–'}` };
    }
    const end = c.tDone ?? t;
    const seg = (a, b, col) => (a != null && b != null && b > a ? `<i style="width:${(100 * (b - a)) / Math.max(1e-3, end - c.tRecv)}%;background:${col}"></i>` : '');
    const bar = `<div class="phasebar">${seg(c.tRecv, c.tDisp ?? (c.tDone ?? t), 'var(--queue)')}${seg(c.tDisp, c.tFirst ?? end, 'var(--prefill)')}`
      + `${seg(c.tFirst, c.tAns ?? end, 'var(--reason)')}${seg(c.tAns, end, 'var(--answer)')}</div>`;
    const e = c.est || {};
    const step = (col, name, at, sub, missing) => `<li class="${missing ? 'missing' : ''}"><i style="--c:${col}"></i><span>${name}</span><span class="at">${at}</span>${sub ? `<span class="sub">${sub}</span>` : ''}</li>`;
    const rejectedEarly = c.status && c.status !== 200 && c.tAdm == null;
    const edge = c.layer === 'edge';
    const steps = edge ? [
      step('var(--faint)', 'Received by the gateway', clock(c.tRecv), 'no SLA class or targets: the request was refused before it was classified'),
      step('var(--bad)', 'Refused by the guardrails', off(c.tDone), esc(c.message || reasonText(c.reason))),
      step('var(--faint)', 'Admission, queue and engine', '', 'not reached', true),
    ].join('') : [
      step('var(--faint)', 'Received by the gateway', clock(c.tRecv), `${c.sla || '–'} class · ~${fmtN(c.prompt)} prompt tokens · targets TTFT ${fmtS(tg.ttft_s)} · answer ${fmtS(tg.ttfat_s)} · total ${fmtS(tg.deadline_s)} · queue budget ${fmtS(tg.queue_budget_s)}`),
      c.via === 'overflow' && c.tAdm == null
        ? step('var(--accent)', `Refused locally: ${(c.overflow && c.overflow.fromCode) || 503} ${reasonText(c.overflowFrom)} → overflow`, off(c.tDisp), 'capacity refusals may leave; the gate had budget and a target model')
        : c.tAdm != null
        ? step('var(--ok)', c.est ? 'Admitted' : 'Passed (admission off)', off(c.tAdm),
          c.est ? `predicted TTFT ${fmtS(e.ttft_s)} (limit ${fmtS((e.slack || 1.5) * (tg.ttft_s || 0))}) · answer ${fmtS(e.ttfat_s)} · total ${fmtS(e.total_s)}`
            + (c.ttft != null ? ` · actual TTFT ${fmtS(c.ttft)}${c.ttft > 2 * Math.max(e.ttft_s, 0.2) ? ` (${(c.ttft / Math.max(e.ttft_s, 0.05)).toFixed(1)}× the prediction)` : ''}` : '')
            : `mode ${c.modeAt || '?'}`)
        : step('var(--bad)', rejectedEarly ? `Refused (${c.layer || '?'})` : 'Admission', rejectedEarly ? off(c.tDone) : '', rejectedEarly ? esc(c.message || '') : '', !rejectedEarly),
      step('var(--queue)', c.tQueued != null ? `Queued at #${c.position ?? '?'}` : 'Gateway queue', c.tQueued != null ? off(c.tQueued) : '',
        c.tQueued != null
          ? `estimated wait ${fmtS(c.estWait)}${c.reason === 'expired_in_queue'
            ? ` · waited ${fmtS(c.tDone - c.tQueued)}, past the ${fmtS(tg.queue_budget_s)} queue budget → dropped`
            : c.tDisp != null ? ` · actual ${fmtS(c.tDisp - c.tQueued)}` : ` · waiting ${fmtS(t - c.tQueued)} of a ${fmtS(tg.queue_budget_s)} budget`}`
          : c.tDisp != null ? 'a slot was free: no wait' : '', c.tQueued == null && c.tDisp == null),
      step('var(--prefill)', c.via === 'overflow' ? `Sent to ${(S.overflow && S.overflow.provider) || 'overflow'} (${c.target || ''})` : c.tDisp == null && c.tDone != null ? 'Never sent to the engine' : 'Sent to the engine', off(c.tDisp), c.tDisp != null ? `${c.replica || ''} · waited ${fmtS(c.queueWait ?? (c.tDisp - (c.tAdm ?? c.tRecv)))} · placement: ${c.routeReason || '–'}` : '', c.tDisp == null),
      step('var(--reason)', c.reasoning ? 'First token (thinking)' : 'First token', off(c.tFirst), c.tFirst != null ? `TTFT ${fmtS(c.ttft ?? (c.tFirst - c.tRecv))}` : '', c.tFirst == null),
      step('var(--answer)', 'First answer token', off(c.tAns), c.tAns != null ? `TTFAT ${fmtS(c.ttfat ?? (c.tAns - c.tRecv))}${c.reasoning && c.tFirst != null ? ` · thought for ${fmtS(c.tAns - c.tFirst)}` : ''}` : '', c.tAns == null),
      step(c.status === 200 ? 'var(--ok)' : c.status ? 'var(--bad)' : 'var(--faint)', c.tDone != null ? `Finished ${c.status || ''}` : 'Finished', off(c.tDone),
        c.tDone != null && c.status === 200 ? `total ${fmtS(c.e2e ?? (c.tDone - c.tRecv))}${c.finish ? ` · finish: ${c.finish}` : ''}` : '', c.tDone == null),
    ].join('');
    const kv = (rows) => `<table class="kv">${rows.filter(([, v]) => v != null && v !== '' && v !== '–').map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</table>`;
    const hist = Array.from(S.calls.values()).filter((x) => x.agent === c.agent).sort((a, b) => b.tRecv - a.tRecv).slice(0, 12);
    const story = storyOf(c, t);
    const why = story ? `<b>What happened:</b> ${esc(story)}` : '';
    const showBar = !edge && end - c.tRecv > 0.01;
    const html = `<div class="verdict" style="--c:${verdict.c}"><b>${esc(verdict.b)}</b><span>${esc(verdict.s)}</span></div>${showBar ? bar : ''}`
      + (why ? `<div class="why">${why.replace(/<(?!\/?b>)/g, '&lt;')}</div>` : '')
      + `<h3>Lifecycle</h3><ul class="steps">${steps}</ul>`
      + (edge ? '' : `<h3>Tokens and speed</h3>${kv([
        ['prompt tokens', c.promptTokens != null
          ? `${fmtN(c.promptTokens)}${S.ctx[c.pool] ? ` of a ${fmtN(S.ctx[c.pool])} context (${pct(c.promptTokens / S.ctx[c.pool])})` : ''}${c.prompt ? ` · gateway counted ~${fmtN(c.prompt)} (${c.prompt >= c.promptTokens ? '+' : ''}${Math.round(100 * (c.prompt - c.promptTokens) / Math.max(1, c.promptTokens))}%)` : ''}`
          : c.prompt ? `~${fmtN(c.prompt)} (gateway's count; ${c.tDone == null ? 'the engine reports its own when the call ends' : c.tFirst != null || c.replica ? 'the engine stopped before reporting its own' : 'the engine never saw it'})` : null],
        ['served from prefix cache', c.cached != null && c.promptTokens ? `${fmtN(c.cached)} (${pct(c.cached / c.promptTokens)})` : null],
        ['output tokens', c.completion != null ? `${fmtN(c.completion)}${c.finish === 'length' ? ' (cut off: hit max_tokens)' : ''}` : null],
        ['max_tokens (output cap)', c.maxTokens ? fmtN(c.maxTokens) : null],
        ['typical gap between tokens', c.itl != null ? fmtS(c.itl) : null],
        ['longest pause between tokens', c.itlMax != null ? `${fmtS(c.itlMax)}${c.itl && c.itlMax > 5 * c.itl ? ` (${Math.round(c.itlMax / c.itl)}× the typical gap: the stream stalled once)` : ''}` : null],
        ['tool calls', c.tools && c.tools.length ? esc(c.tools.join(', ')) : null],
        ['local tool time before it', c.toolGap != null ? fmtS(c.toolGap) : null],
      ])}`)
      + `<h3>Identity</h3>${kv([['tenant', esc(c.tenant)],
        ['agent', c.role === 'lead' || c.role === 'subagent' ? esc(c.name) : 'none: a plain API client (no tinyharness agent headers)'],
        ['role', c.role === 'lead' || c.role === 'subagent' ? esc(c.role) : null], ['model asked / served', `${esc(c.model)} / ${esc(c.pool || '–')}${c.spilled ? ' (spilled)' : ''}`],
        ['request id', `<span class="copy" data-copy="${esc(c.id)}" title="copy">${esc(c.id)}</span>`]])}`
      + `<p class="mark-note">Full body and output: python -m analysis.sessions → bodies(results, "${esc(c.id)}")</p>`
      + `<h3>This agent's calls</h3><div class="hist">${hist.map((x) => `<div class="row${x.id === c.id ? ' sel' : ''}" data-req="${esc(x.id)}" style="--stripe:${tenantColor(x.tenant)}"><span class="num">${x.status || '…'}</span><span class="name">${clock(x.tRecv)} · ${phaseOf(x, t).text}</span><span class="num">${x.tDone != null ? fmtS(x.tDone - x.tRecv) : ''}</span></div>`).join('')}</div>`;
    setText($('drawerTitle'), `${c.name} · ${c.tenant}`);
    if (force || html !== drawerHtml) { $('drawerBody').innerHTML = html; drawerHtml = html; }
  }

  // ---------------------------------------------------------------- latency table
  function renderLatency(t) {
    const t0 = t - tlWin;
    const done = Array.from(S.calls.values()).filter((c) => c.tDone != null && c.tDone >= t0 && c.tRecv >= clearedAt && inFocus(c.tenant));
    const groups = new Map();
    const add = (k, c) => { if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); };
    for (const c of done) {
      add(`pool:${c.pool || c.model || '?'}`, c); add(`tenant:${c.tenant}`, c);
      if (c.replica) add(`replica:${c.replica}`, c);  // the vLLM instance that served it (refusals never reach one)
    }
    const head = ['', 'Calls', 'TTFT p50', 'TTFT p95', 'First answer p50', 'First answer p95', 'Token gap p50', 'Total p95', 'Queue wait p95', 'In SLO', 'Refused'];
    const tips = ['', 'Calls that finished in the window (any status)', 'Time to the first token of any kind: half of calls were faster', 'Time to first token: 95% of calls were faster',
      'Time to the first token of the answer, after any thinking: half were faster', 'Time to the first answer token: 95% were faster',
      'Typical gap between two streamed tokens (inter-token latency)', 'Whole call, start to end: 95% were faster', 'Time held in the gateway queue: 95% waited less',
      'Share of 200s that met their class targets (TTFT, first answer, total)', 'Non-200 responses, excluding client hang-ups'];
    const row = (label, cs, swatch) => {
      const ok = cs.filter((c) => c.status === 200);
      const met = ok.map(meets).filter((m) => m != null);
      const rej = cs.filter((c) => c.status && c.status >= 400 && c.status !== 499).length;
      const q = (f, p) => fmtS(quantile(ok.map(f), p));
      const sw = swatch ? `<i class="sw" style="background:${swatch}"></i>` : '';
      return `<tr><td>${sw}${label}</td><td>${cs.length}</td><td>${q((c) => c.ttft, 0.5)}</td><td>${q((c) => c.ttft, 0.95)}</td>`
        + `<td>${q((c) => c.ttfat, 0.5)}</td><td>${q((c) => c.ttfat, 0.95)}</td><td>${q((c) => c.itl, 0.5)}</td>`
        + `<td>${q((c) => c.e2e, 0.95)}</td><td>${q((c) => c.queueWait, 0.95)}</td>`
        + `<td>${met.length ? pct(met.filter(Boolean).length / met.length) : '–'}</td><td>${rej}</td></tr>`;
    };
    let html = `<thead><tr>${head.map((h, i) => `<th title="${tips[i]}">${h}</th>`).join('')}</tr></thead><tbody>`;
    const pools = Array.from(groups.keys()).filter((k) => k.startsWith('pool:')).sort();
    const tenants = Array.from(groups.keys()).filter((k) => k.startsWith('tenant:')).sort();
    const replicas = Array.from(groups.keys()).filter((k) => k.startsWith('replica:')).sort();
    if (!done.length) html += `<tr><td colspan="${head.length}" class="muted">No completed calls in this window.</td></tr>`;
    else {
      html += `<tr class="group"><td colspan="${head.length}">By model</td></tr>`;
      for (const k of pools) html += row(k.slice(5), groups.get(k));
      if (replicas.length) {
        html += `<tr class="group"><td colspan="${head.length}">By replica (vLLM instance)</td></tr>`;
        for (const k of replicas) html += row(k.slice(8), groups.get(k));
      }
      html += `<tr class="group"><td colspan="${head.length}">By tenant</td></tr>`;
      for (const k of tenants.slice(0, 10)) html += row(k.slice(7), groups.get(k), tenantColor(k.slice(7)));
    }
    html += '</tbody>';
    const tb = $('latTable');
    tb.className = 'lat';
    if (tb.dataset.html !== html) { tb.innerHTML = html; tb.dataset.html = html; }
  }

  // ---------------------------------------------------------------- render loop
  let dirty = false;
  function scheduleRender() { dirty = true; }

  function clearLists() {
    for (const id of ['tenantList', 'admLog', 'laneList', 'engineList', 'respList', 'rejTable', 'engEvents']) $(id).innerHTML = '';
  }
  function render() {
    const t = now();
    if (paused) { renderHeader(t); return; }
    applyView();
    renderHeader(t);
    if (view === 'summary') { renderSummary(t); renderDrawer(t); return; }
    renderDiagnosis(t);
    renderDrawer(t);
    renderClients(t);
    renderAdmission(t);
    renderQueue(t);
    renderEngines(t);
    renderResponses(t);
    renderIncidents(t);
    renderTimeline(t);
    renderLatency(t);
  }
  // One redraw a second on a fixed clock: a monitoring view should only move when its data does.
  setInterval(() => { if (!silent) { dirty = false; render(); } }, 1000);
  window.addEventListener('resize', () => render());

  // prune old calls so a long session stays fast
  setInterval(() => {
    const t = now();
    for (const [id, c] of S.calls) if (c.tDone != null && t - c.tDone > 3700) S.calls.delete(id);
  }, 30000);

  // ---------------------------------------------------------------- live
  let connected = false;
  let es = null;
  async function startLive() {
    stopReplay();
    S = fresh();
    try { clearedAt = Number(localStorage.getItem('clearedAt')) || -Infinity; } catch { clearedAt = -Infinity; }
    seenEv.clear(); seenOrder.length = 0;
    try {
      const st = await fetch(new URL('admin/state', BASE)).then((r) => r.json());
      try {
        const models = (await fetch(new URL('v1/models', BASE)).then((r) => r.json())).data || [];
        for (const m of models) if (m.max_model_len) S.ctx[m.id] = m.max_model_len;
      } catch (e) { /* older gateway */ }
      S.mode = st.mode; S.features = st.features || {};
      if (st.run) S.run = `${st.run.exp}/${st.run.run}`;
      try {
        const cfg = (await fetch(new URL('admin/config', BASE)).then((r) => r.json())).config || {};
        S.policy = { admission: cfg.admission || {}, queue: cfg.queue || {}, sla_classes: cfg.sla_classes || {},
                     routing: { policy: (cfg.routing || {}).policy, spill: !!(((cfg.routing || {}).spill || {}).enabled ?? true) } };
      } catch (e) { /* older gateway */ }
      const recent = await fetch(new URL('events/recent?n=4000', BASE)).then((r) => r.json());
      silent = true;
      for (const ev of recent) handle(ev);
      handle({ kind: 'state', t: Date.now() / 1000 + offset, ...st });
    } catch (e) { /* gateway not reachable yet: the stream below retries */ }
    silent = false;
    render();
    if (es) es.close();
    es = new EventSource(new URL('events?backlog=500', BASE));
    es.onopen = () => { connected = true; render(); };
    es.onerror = () => { connected = false; };
    es.onmessage = (m) => { if (replay) return; try { handle(JSON.parse(m.data)); } catch (e) { /* skip */ } };
  }

  // ---------------------------------------------------------------- replay
  // One or more events.jsonl files (e.g. before and after a gateway restart), merged in time order.
  function loadReplay(texts, name) {
    const events = [];
    const seen = new Set();
    for (const text of Array.isArray(texts) ? texts : [texts]) {
      for (const line of text.split('\n')) {
        const s = line.trim();
        if (!s) continue;
        try {
          const ev = JSON.parse(s);
          if (!ev || !ev.kind || !ev.t) continue;
          const id = `${ev.t}|${ev.kind}|${ev.req_id || ''}`;
          if (seen.has(id)) continue;
          seen.add(id);
          events.push(ev);
        } catch (e) { /* skip */ }
      }
    }
    if (!events.length) return;
    events.sort((a, b) => a.t - b.t);
    buildChapters(events);
    if (es) { es.close(); es = null; connected = false; }
    replay = { events, i: 0, t0: events[0].t, t1: events[events.length - 1].t, t: events[0].t, speed: 5, playing: true, name };
    S = fresh();
    clearedAt = -Infinity;
    $('replayBar').hidden = false;
    setText($('replayName'), name || '');
    setSpeed(5);
    play(true);
  }
  // Chapters: each experiment by its task label (TH_TASK), each Exp-3 variant, each mode switch.
  function buildChapters(events) {
    const tasks = new Map(), variants = [], marks = [];
    let lastVariant = null, lastMode = null;
    for (const ev of events) {
      if (ev.task && (ev.kind === 'received' || ev.kind === 'done')) {
        const c = tasks.get(ev.task) || { start: ev.t, end: ev.t };
        c.end = Math.max(c.end, ev.t); tasks.set(ev.task, c);
      }
      if (ev.variant && ev.variant !== lastVariant && ev.kind !== 'state') {
        if (variants.length) variants[variants.length - 1].end = ev.t;
        variants.push({ name: ev.variant, start: ev.t, end: ev.t }); lastVariant = ev.variant;
      } else if (variants.length) variants[variants.length - 1].end = ev.t;
      if (ev.mode && ev.mode !== lastMode && ev.kind !== 'state') { if (lastMode) marks.push({ name: `mode → ${ev.mode}`, start: ev.t }); lastMode = ev.mode; }
    }
    const opts = [];
    for (const [name, c] of tasks) opts.push({ label: `Task ${name}`, start: c.start, end: c.end });
    for (const v of variants) if (v.name !== 'default' || variants.length > 1) opts.push({ label: `Variant ${v.name}`, start: v.start, end: v.end });
    for (const m of marks) opts.push({ label: m.name, start: m.start, end: null });
    opts.sort((a, b) => a.start - b.start);
    const sel = $('chapters');
    sel.innerHTML = '<option value="">Jump to…</option>' + opts.map((o, i) => `<option value="${i}">${clock(o.start)} · ${o.label}${o.end ? ` · ${fmtS(o.end - o.start)}` : ''}</option>`).join('');
    sel._opts = opts;
    sel.hidden = !opts.length;
  }
  function seek(target) {
    S = fresh();
    replay.i = 0; replay.t = target;
    silent = true;
    while (replay.i < replay.events.length && replay.events[replay.i].t <= target) handle(replay.events[replay.i++]);
    silent = false;
    for (const id of ['tenantList', 'admLog', 'laneList', 'engineList', 'respList']) $(id).innerHTML = '';
    render();
  }
  function play(on) { replay.playing = on; setText($('btnPlay'), on ? 'Pause' : 'Play'); }
  function setSpeed(v) {
    replay.speed = v;
    for (const s of document.querySelectorAll('[data-speed]')) s.classList.toggle('on', Number(s.dataset.speed) === v);
  }
  let lastTick = performance.now();
  setInterval(() => {
    const nowMs = performance.now(), dt = (nowMs - lastTick) / 1000;
    lastTick = nowMs;
    if (!replay || !replay.playing) return;
    replay.t = Math.min(replay.t1, replay.t + dt * replay.speed);
    while (replay.i < replay.events.length && replay.events[replay.i].t <= replay.t) handle(replay.events[replay.i++]);
    if (replay.t >= replay.t1) play(false);
    const span = Math.max(1e-6, replay.t1 - replay.t0);
    $('scrub').value = String(Math.round((1000 * (replay.t - replay.t0)) / span));
    setText($('replayTime'), `${fmtS(replay.t - replay.t0)} / ${fmtS(span)}`);
  }, 100);
  function stopReplay() {
    replay = null;
    $('replayBar').hidden = true;
  }
  // Frame-exact control for recording clips (docs/presentation/tools/record_clip.py): pause the wall-clock player,
  // seek to a data time, then step by data-seconds and render, one screenshot per step.
  window.replayCtl = {
    ready: () => !!replay,
    range: () => (replay ? [replay.t0, replay.t1] : null),
    seek: (t) => { play(false); seek(t); return replay.t; },
    step: (dt) => {
      replay.t = Math.min(replay.t1, replay.t + dt);
      while (replay.i < replay.events.length && replay.events[replay.i].t <= replay.t) handle(replay.events[replay.i++]);
      const span = Math.max(1e-6, replay.t1 - replay.t0);
      $('scrub').value = String(Math.round((1000 * (replay.t - replay.t0)) / span));
      setText($('replayTime'), `${fmtS(replay.t - replay.t0)} / ${fmtS(span)}`);
      render();
      return replay.t;
    },
  };

  $('btnPlay').addEventListener('click', () => { if (replay) { if (replay.t >= replay.t1) seek(replay.t0); play(!replay.playing); } });
  $('btnLive').addEventListener('click', () => startLive());
  $('scrub').addEventListener('input', (e) => { if (!replay) return; seek(replay.t0 + (Number(e.target.value) / 1000) * (replay.t1 - replay.t0)); });
  for (const s of document.querySelectorAll('[data-speed]')) s.addEventListener('click', () => replay && setSpeed(Number(s.dataset.speed)));
  for (const s of document.querySelectorAll('[data-win]')) {
    s.addEventListener('click', () => {
      tlWin = Number(s.dataset.win);
      for (const o of document.querySelectorAll('[data-win]')) o.classList.toggle('on', o === s);
      render();
    });
  }
  for (const sp of document.querySelectorAll('[data-f]')) {
    sp.addEventListener('click', () => {
      admFilter = sp.dataset.f;
      for (const o of document.querySelectorAll('[data-f]')) o.classList.toggle('on', o === sp);
      $('admLog').innerHTML = '';
      render();
    });
  }
  $('btnReplay').addEventListener('click', () => $('file').click());
  const readAll = async (files) => Promise.all(Array.from(files).map((f) => f.text()));
  $('file').addEventListener('change', async (e) => {
    const fs = e.target.files;
    if (fs && fs.length) loadReplay(await readAll(fs), Array.from(fs).map((f) => f.name).join(' + '));
    e.target.value = '';
  });
  $('chapters').addEventListener('change', (e) => {
    const o = (e.target._opts || [])[Number(e.target.value)];
    if (o && replay) { seek(Math.max(replay.t0, o.start - 5)); play(true); }
    e.target.value = '';
  });
  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth += 1; $('drop').hidden = false; });
  window.addEventListener('dragleave', () => { dragDepth -= 1; if (dragDepth <= 0) { dragDepth = 0; $('drop').hidden = true; } });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', async (e) => {
    e.preventDefault(); dragDepth = 0; $('drop').hidden = true;
    const fs = e.dataTransfer.files;
    if (fs && fs.length) loadReplay(await readAll(fs), Array.from(fs).map((f) => f.name).join(' + '));
  });

  // timeline hover
  $('tlCanvas').addEventListener('mousemove', (e) => {
    const r = e.target.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = tlHits.find((h) => mx >= h.x0 && mx <= h.x1 && my >= h.y0 && my <= h.y1);
    const tip = $('tlTip');
    if (!hit) { tip.style.display = 'none'; return; }
    tip.innerHTML = tipFor(hit.c);
    tip.style.display = 'block';
    const wrap = $('tlWrap');
    const left = Math.min(mx + 14, wrap.clientWidth - tip.offsetWidth - 8);
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${my + 14}px`;
  });
  $('tlCanvas').addEventListener('mouseleave', () => { $('tlTip').style.display = 'none'; });
  $('tlCanvas').addEventListener('click', (e) => {
    const r = e.target.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = tlHits.find((h) => mx >= h.x0 && mx <= h.x1 && my >= h.y0 && my <= h.y1);
    if (hit) openCall(hit.c.id);
  });
  // any row that carries a request id opens its details
  document.addEventListener('click', (e) => {
    const cp = e.target.closest('[data-copy]');
    if (cp) { try { navigator.clipboard.writeText(cp.dataset.copy); setText(cp, 'copied'); } catch (err) { /* clipboard blocked */ } return; }
    const row = e.target.closest('[data-req]');
    if (row && !e.target.closest('.tenant-head')) openCall(row.dataset.req);
  });
  document.addEventListener('click', (e) => {
    const w = e.target.closest('[data-where]');
    if (!w || !w.dataset.where) return;
    const p = $(w.dataset.where);
    p.scrollIntoView({ behavior: 'smooth', block: 'center' });
    p.classList.remove('flash'); void p.offsetWidth; p.classList.add('flash');
  });
  for (const sp of document.querySelectorAll('[data-fit]')) {
    sp.addEventListener('click', () => {
      tlZoom = Number(sp.dataset.fit);
      for (const o of document.querySelectorAll('[data-fit]')) o.classList.toggle('on', o === sp);
      render();
    });
  }
  $('drawerClose').addEventListener('click', () => { $('drawer').hidden = true; selected = null; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { $('drawer').hidden = true; selected = null; $('modeConfirm').hidden = true; } });
  $('btnClear').addEventListener('click', () => {
    clearedAt = now();
    if (!replay) try { localStorage.setItem('clearedAt', String(clearedAt)); } catch { /* storage blocked */ }
    clearLists(); render();
  });
  $('btnPause').addEventListener('click', () => { paused = !paused; pausedEvents = 0; if (!paused) clearLists(); render(); });
  $('focusClear').addEventListener('click', () => { focus = null; clearLists(); render(); });
  for (const sp of document.querySelectorAll('[data-r]')) {
    sp.addEventListener('click', () => {
      respFilter = sp.dataset.r;
      for (const o of document.querySelectorAll('[data-r]')) o.classList.toggle('on', o === sp);
      $('respList').innerHTML = '';
      render();
    });
  }
  // mode switch, with a confirmation that says what will change
  const MODE_TEXT = {
    baseline: 'Pass-through. Route by model only: no admission, no gateway queue, no spill or prefix routing. Every call goes straight to vLLM, which queues it internally. Expect no refusals (except edge 400s), "waiting inside vLLM" in the engines under load, and long TTFT tails.',
    route: 'Adds placement across the replicas (prefix-aware, then the less loaded; spill to another model if configured). Still no admission and no gateway queue: overload still lands inside vLLM.',
    queue: 'Adds the gateway queue (edf_fair: deadline order inside a tenant, fair across tenants) and vLLM priorities. No admission: nothing is refused, but excess waits in the gateway queue in policy order and vLLM\'s own waiting stays near 0. Under overload, queue waits grow.',
    full: 'Adds admission: tenant token budgets, KV / stale-metrics / new-session shedding, and TTFT / answer / deadline estimates. Expect some 429 / 503 under load, bounded queue waits and a higher share within SLO.',
  };
  let pendingMode = null;
  for (const sp of $('modeSeg').children) {
    sp.addEventListener('click', () => {
      if (replay || sp.dataset.mode === S.mode) return;
      pendingMode = sp.dataset.mode;
      $('modeConfirmText').innerHTML = `<b>Switch the gateway to ${pendingMode}?</b><br>${MODE_TEXT[pendingMode]}<br><span class="mark-note">Applies to all live traffic at once (same as experiments/mode.sh).</span>`;
      $('modeConfirm').hidden = false;
    });
  }
  $('modeNo').addEventListener('click', () => { $('modeConfirm').hidden = true; pendingMode = null; });
  $('modeYes').addEventListener('click', async () => {
    $('modeConfirm').hidden = true;
    if (!pendingMode) return;
    try {
      await fetch(new URL('admin/mode', BASE), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mode: pendingMode }) });
    } catch (err) { /* the mode event (or its absence) shows the result */ }
    pendingMode = null;
  });

  // theme
  function applyTheme(th) {
    if (th) document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme;
  }
  try { applyTheme(localStorage.getItem('pipeline-theme')); } catch (e) { /* storage blocked */ }
  $('btnTheme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    applyTheme(next);
    try { localStorage.setItem('pipeline-theme', next); } catch (e) { /* storage blocked */ }
    render();
  });

  for (const sp of $('viewSeg').children) {
    sp.addEventListener('click', () => {
      view = sp.dataset.view;
      try { localStorage.setItem('pipeline-view', view); } catch (e) { /* storage blocked */ }
      render();
    });
  }
  $('diagToggle').addEventListener('click', () => {
    diagOpen = !diagOpen;
    try { localStorage.setItem('pipeline-findings', diagOpen ? 'open' : 'closed'); } catch (e) { /* storage blocked */ }
    render();
  });

  // segmented controls are spans: make them focusable and operable from the keyboard
  for (const sp of document.querySelectorAll('.seg span')) {
    sp.tabIndex = 0;
    sp.setAttribute('role', 'button');
    sp.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sp.click(); } });
  }

  // ---------------------------------------------------------------- start
  const replayUrl = params.get('replay');
  if (replayUrl) {
    // ?replay=a.jsonl,b.jsonl (comma-separated) loads and merges several files
    const urls = replayUrl.split(',').filter(Boolean);
    Promise.all(urls.map((u) => fetch(u).then((r) => { if (!r.ok) throw new Error(u); return r.text(); })))
      .then((txts) => loadReplay(txts, urls.map((u) => u.split('/').pop()).join(' + '))).catch(() => startLive());
  } else {
    startLive();
  }
})();
