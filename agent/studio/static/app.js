// AutoSeguro Studio — vanilla JS (ES2020), sem framework/bundler/CDN. Módulo ES (`type="module"`
// no index.html), então nada aqui vaza para `window`: cada `const`/`function` de topo é escopo
// do módulo, não global solto.

// -------------------------------------------------------------------------- core: api/sse/toast
const toastsEl = document.getElementById("toasts");

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : kind === "success" ? " success" : "");
  el.textContent = message;
  toastsEl.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function api(path, opts = {}) {
  const hasBody = opts.body !== undefined;
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: hasBody ? { "Content-Type": "application/json" } : undefined,
    body: hasBody ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* corpo vazio (ex.: 204) */
  }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `${res.status} ${res.statusText}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function sse(url, onEvent) {
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    try {
      onEvent(JSON.parse(ev.data));
    } catch {
      /* heartbeat (": ping") não chega em onmessage; ignora payload não-JSON por segurança */
    }
  };
  return es;
}

function withLoading(button, fn) {
  return async (...args) => {
    button.classList.add("loading");
    button.disabled = true;
    try {
      return await fn(...args);
    } catch (err) {
      toast(err.message || String(err), "error");
      return undefined;
    } finally {
      button.classList.remove("loading");
      button.disabled = false;
    }
  };
}

function guarded(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (err) {
      toast(err.message || String(err), "error");
      return undefined;
    }
  };
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function badgeOrigem(origem) {
  const classe = origem === "override" ? "badge-origem-override" : origem.startsWith("env:") ? "badge-origem-env" : "badge-origem-default";
  return `<span class="badge ${classe}">${escapeHtml(origem)}</span>`;
}

// -------------------------------------------------------------------------- router (hash → aba)
const TABS = ["lab", "prompts", "tools", "config"];

function abaAtual() {
  const h = (location.hash || "#lab").slice(1);
  return TABS.includes(h) ? h : "lab";
}

function renderTab() {
  const tab = abaAtual();
  for (const t of TABS) document.getElementById(`tab-${t}`).hidden = t !== tab;
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  onTabShown(tab);
}

function onTabShown(tab) {
  if (tab === "prompts") Prompts.load().catch((e) => toast(e.message, "error"));
  if (tab === "tools") Tools.load().catch((e) => toast(e.message, "error"));
  if (tab === "config") Config.load().catch((e) => toast(e.message, "error"));
}

window.addEventListener("hashchange", renderTab);

// -------------------------------------------------------------------------- saúde (rodapé)
const Health = {
  async check() {
    const dot = document.getElementById("health-dot");
    const text = document.getElementById("health-text");
    try {
      const data = await api("/api/health");
      dot.classList.remove("down");
      dot.classList.add("ok");
      text.textContent = data.status === "ok" ? "online" : data.status;
    } catch {
      dot.classList.remove("ok");
      dot.classList.add("down");
      text.textContent = "offline";
    }
  },
  start() {
    this.check();
    setInterval(() => this.check(), 15000);
  },
};

// -------------------------------------------------------------------------- aba Prompts
const Prompts = {
  slots: {},
  selectedKey: null,
  selectedVersion: null,

  async load() {
    const data = await api("/api/prompts");
    this.slots = data.slots;
    this.renderList();
    if (this.selectedKey && this.slots[this.selectedKey]) this.renderDetail();
  },

  async reload(msg) {
    await this.load();
    if (msg) toast(msg, "success");
  },

  renderList() {
    const container = document.getElementById("prompts-list");
    const q = (document.getElementById("prompts-search").value || "").toLowerCase();
    container.innerHTML = "";
    const grupos = {};
    for (const [key, slot] of Object.entries(this.slots)) {
      if (q && !(key.toLowerCase().includes(q) || slot.label.toLowerCase().includes(q))) continue;
      (grupos[slot.grupo] ||= []).push([key, slot]);
    }
    for (const grupo of Object.keys(grupos).sort()) {
      const h = document.createElement("div");
      h.className = "slot-meta";
      h.style.padding = "6px 10px 2px";
      h.textContent = grupo;
      container.appendChild(h);
      for (const [key, slot] of grupos[grupo]) {
        const item = document.createElement("div");
        item.className = "slot-item" + (key === this.selectedKey ? " selected" : "");
        item.innerHTML = `<div class="slot-label">${escapeHtml(slot.label)}</div>
          <div class="slot-meta">ativa: ${escapeHtml(slot.active)} · ${Object.keys(slot.versions).length} versões</div>`;
        item.addEventListener("click", () => this.select(key));
        container.appendChild(item);
      }
    }
  },

  select(key) {
    this.selectedKey = key;
    this.selectedVersion = this.slots[key].active;
    this.renderList();
    this.renderDetail();
  },

  renderDetail() {
    const el = document.getElementById("prompts-detail");
    const key = this.selectedKey;
    const slot = this.slots[key];
    if (!slot) {
      el.innerHTML = '<p class="muted">Selecione um slot à esquerda.</p>';
      return;
    }
    const versionName = versionExiste(slot, this.selectedVersion) ? this.selectedVersion : slot.active;
    this.selectedVersion = versionName;
    const version = slot.versions[versionName];
    const isDefault = versionName === "default";
    const isActive = versionName === slot.active;

    el.innerHTML = `
      <h3>${escapeHtml(slot.label)}</h3>
      <div class="muted">key: <code>${escapeHtml(key)}</code></div>
      <div style="margin:8px 0">${
        (slot.placeholders || []).map((p) => `<span class="badge">{${escapeHtml(p)}}</span>`).join(" ") ||
        '<span class="muted">sem placeholders</span>'
      }</div>
      <ul class="version-list" id="prompts-version-list"></ul>
      <div class="new-version-form" id="prompts-new-version" hidden>
        <label class="field">Nome<input id="pv-name" type="text" /></label>
        <label class="field">Nota<input id="pv-note" type="text" /></label>
        <label class="switch"><input id="pv-activate" type="checkbox" checked />ativar</label>
        <button id="pv-confirm" class="primary" type="button">Criar</button>
        <button id="pv-cancel" type="button">Cancelar</button>
      </div>
      <div class="editor-row">
        <textarea id="prompts-editor" class="mono" ${isDefault ? "disabled" : ""}>${escapeHtml(version.text)}</textarea>
      </div>
      ${isDefault ? '<p class="notice">comportamento entregue, imutável — crie uma versão</p>' : ""}
      <div class="card-actions">
        <button id="pv-save" type="button" ${isDefault ? "disabled" : ""}>Salvar</button>
        <button id="pv-new" type="button">Nova versão</button>
        <button id="pv-delete" class="danger" type="button" ${isDefault || isActive ? "disabled" : ""}>Apagar</button>
        <button id="pv-diff" type="button" ${isDefault ? "disabled" : ""}>Diff vs default</button>
      </div>
      <div id="pv-diff-view"></div>
    `;

    this.renderVersionList();

    const btnSave = document.getElementById("pv-save");
    btnSave.addEventListener(
      "click",
      withLoading(btnSave, async () => {
        const text = document.getElementById("prompts-editor").value;
        await api(`/api/prompts/${encodeURIComponent(key)}/versions/${encodeURIComponent(versionName)}`, {
          method: "PUT",
          body: { text },
        });
        await this.reload("aplicado");
      })
    );

    document.getElementById("pv-new").addEventListener("click", () => {
      document.getElementById("prompts-new-version").hidden = false;
    });
    document.getElementById("pv-cancel").addEventListener("click", () => {
      document.getElementById("prompts-new-version").hidden = true;
    });
    const btnConfirm = document.getElementById("pv-confirm");
    btnConfirm.addEventListener(
      "click",
      withLoading(btnConfirm, async () => {
        const name = document.getElementById("pv-name").value.trim();
        const note = document.getElementById("pv-note").value;
        const activate = document.getElementById("pv-activate").checked;
        const text = document.getElementById("prompts-editor").value;
        if (!name) {
          toast("nome da versão é obrigatório", "error");
          return;
        }
        await api(`/api/prompts/${encodeURIComponent(key)}/versions`, { method: "POST", body: { name, text, note, activate } });
        this.selectedVersion = name;
        await this.reload("aplicado");
      })
    );

    const btnDelete = document.getElementById("pv-delete");
    btnDelete.addEventListener(
      "click",
      withLoading(btnDelete, async () => {
        await api(`/api/prompts/${encodeURIComponent(key)}/versions/${encodeURIComponent(versionName)}`, { method: "DELETE" });
        this.selectedVersion = null;
        await this.reload("aplicado");
      })
    );

    document.getElementById("pv-diff").addEventListener("click", () => this.renderDiff(slot));
  },

  renderVersionList() {
    const key = this.selectedKey;
    const slot = this.slots[key];
    const ul = document.getElementById("prompts-version-list");
    ul.innerHTML = "";
    for (const [name, v] of Object.entries(slot.versions)) {
      const li = document.createElement("li");
      li.className = "version-row";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "active-version";
      radio.checked = name === slot.active;
      radio.title = "Ativar esta versão";
      radio.addEventListener(
        "change",
        guarded(async () => {
          await api(`/api/prompts/${encodeURIComponent(key)}/active`, { method: "PUT", body: { name } });
          await this.reload("aplicado");
        })
      );
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "version-select" + (name === this.selectedVersion ? " selected" : "");
      btn.innerHTML = `${escapeHtml(name)} <span class="version-note">${escapeHtml(v.note || "")}</span> <time>${escapeHtml(v.created_at || "")}</time>`;
      btn.addEventListener("click", () => {
        this.selectedVersion = name;
        this.renderDetail();
      });
      li.appendChild(radio);
      li.appendChild(btn);
      ul.appendChild(li);
    }
  },

  renderDiff(slot) {
    const el = document.getElementById("pv-diff-view");
    const a = (slot.versions.default.text || "").split("\n");
    const b = (document.getElementById("prompts-editor").value || "").split("\n");
    const max = Math.max(a.length, b.length);
    let left = "";
    let right = "";
    for (let i = 0; i < max; i++) {
      const la = a[i] ?? "";
      const lb = b[i] ?? "";
      const changed = la !== lb;
      left += `<div class="diff-line${changed ? " changed" : ""}">${escapeHtml(la) || "&nbsp;"}</div>`;
      right += `<div class="diff-line${changed ? " changed" : ""}">${escapeHtml(lb) || "&nbsp;"}</div>`;
    }
    el.innerHTML = `<div class="diff-grid"><pre>${left}</pre><pre>${right}</pre></div>`;
  },
};

function versionExiste(slot, name) {
  return !!name && Object.prototype.hasOwnProperty.call(slot.versions, name);
}

// -------------------------------------------------------------------------- aba Tools
const Tools = {
  effective: null,

  async load() {
    const data = await api("/api/effective");
    this.effective = data.tools;
    this.render();
  },

  render() {
    const container = document.getElementById("tools-cards");
    container.innerHTML = "";
    container.appendChild(this.renderQuoteClientCard());
    container.appendChild(
      this.renderCard("viacep", "ViaCEP", [
        { key: "enabled", label: "Habilitado", type: "switch" },
        { key: "url", label: "URL", type: "text" },
        { key: "timeout_s", label: "Timeout (s)", type: "number", step: "0.1" },
      ])
    );
    container.appendChild(
      this.renderCard("policy", "Policy", [
        { key: "max_turnos_sem_progresso", label: "Máx. turnos sem progresso", type: "number" },
        { key: "max_cep_tentativas", label: "Máx. tentativas de CEP", type: "number" },
        { key: "objecoes_ate_handoff", label: "Objeções até handoff", type: "number" },
      ])
    );
    container.appendChild(
      this.renderCard("rules", "Rules", [{ key: "pre_validacao_local", label: "Pré-validação local", type: "switch" }])
    );
  },

  fieldRowHtml(grupo, def, campo) {
    const id = `tool-${grupo}-${def.key}`;
    const inputHtml =
      def.type === "switch"
        ? `<label class="switch"><input type="checkbox" id="${id}" ${campo.value ? "checked" : ""} /> ${escapeHtml(def.label)}</label>`
        : `<label class="field">${escapeHtml(def.label)}
            <input type="${def.type}" id="${id}" value="${escapeHtml(campo.value ?? "")}" ${def.step ? `step="${def.step}"` : ""} /></label>`;
    return `<div class="card-row">
      ${inputHtml}
      ${badgeOrigem(campo.origem)}
      ${campo.origem === "override" ? `<button type="button" class="small reset-btn" data-path="${grupo}/${def.key}">voltar ao padrão</button>` : ""}
    </div>`;
  },

  lerCampo(id, def) {
    const el = document.getElementById(id);
    if (def.type === "switch") return el.checked;
    if (def.type === "number") return Number(el.value);
    return el.value;
  },

  renderCard(grupo, titulo, campos) {
    const div = document.createElement("div");
    div.className = "card";
    const corpo = campos.map((def) => this.fieldRowHtml(grupo, def, this.effective[grupo][def.key])).join("");
    div.innerHTML = `<h3>${escapeHtml(titulo)}</h3>${corpo}
      <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>`;
    this.wireResetButtons(div);
    const btn = div.querySelector(".save-btn");
    btn.addEventListener(
      "click",
      withLoading(btn, async () => {
        const patch = {};
        for (const def of campos) {
          const novo = this.lerCampo(`tool-${grupo}-${def.key}`, def);
          const atual = this.effective[grupo][def.key].value;
          if (novo !== atual) patch[def.key] = novo;
        }
        if (Object.keys(patch).length === 0) {
          toast("nada para salvar");
          return;
        }
        await api("/api/tools", { method: "PUT", body: { [grupo]: patch } });
        await this.load();
        toast("aplicado", "success");
      })
    );
    return div;
  },

  renderEndpointRows(container, endpoints) {
    container.innerHTML = "";
    for (const [label, url] of Object.entries(endpoints)) {
      this.addEndpointRow(container, label, url);
    }
  },

  addEndpointRow(container, label = "", url = "") {
    const row = document.createElement("div");
    row.className = "endpoint-row";
    row.innerHTML = `<input type="text" class="ep-label" placeholder="rótulo" value="${escapeHtml(label)}" />
      <input type="text" class="ep-url" placeholder="http://..." value="${escapeHtml(url)}" />
      <button type="button" class="small ep-remove">remover</button>`;
    row.querySelector(".ep-remove").addEventListener("click", () => row.remove());
    container.appendChild(row);
  },

  renderQuoteClientCard() {
    const grupo = "quote_client";
    const g = this.effective[grupo];
    const div = document.createElement("div");
    div.className = "card";
    const endpoints = g.endpoints.value || {};
    const options = Object.entries(endpoints)
      .map(([label, url]) => `<option value="${escapeHtml(url)}">${escapeHtml(label)} — ${escapeHtml(url)}</option>`)
      .join("");
    div.innerHTML = `
      <h3>quote_client</h3>
      <div class="card-row">
        <label class="field">base_url — endpoint conhecido
          <select id="tool-qc-base_url-select"><option value="">(usar campo livre abaixo)</option>${options}</select>
        </label>
      </div>
      <div class="card-row">
        <label class="field">base_url
          <input type="text" id="tool-quote_client-base_url" value="${escapeHtml(g.base_url.value ?? "")}" /></label>
        ${badgeOrigem(g.base_url.origem)}
        ${g.base_url.origem === "override" ? '<button type="button" class="small reset-btn" data-path="quote_client/base_url">voltar ao padrão</button>' : ""}
      </div>
      <div class="card-row" style="align-items:flex-start;flex-direction:column">
        <span class="muted">endpoints</span>
        <div id="tool-endpoints-rows"></div>
        <button type="button" id="tool-endpoints-add" class="small">+ endpoint</button>
        ${badgeOrigem(g.endpoints.origem)}
        ${g.endpoints.origem === "override" ? '<button type="button" class="small reset-btn" data-path="quote_client/endpoints">voltar ao padrão</button>' : ""}
      </div>
      ${this.fieldRowHtml(grupo, { key: "timeout_s", label: "timeout_s", type: "number", step: "0.1" }, g.timeout_s)}
      ${this.fieldRowHtml(grupo, { key: "max_attempts", label: "max_attempts", type: "number" }, g.max_attempts)}
      ${this.fieldRowHtml(grupo, { key: "budget_s", label: "budget_s", type: "number", step: "0.5" }, g.budget_s)}
      ${this.fieldRowHtml(grupo, { key: "backoff_base_s", label: "backoff_base_s", type: "number", step: "0.1" }, g.backoff_base_s)}
      <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>
    `;
    const endpointsContainer = div.querySelector("#tool-endpoints-rows");
    this.renderEndpointRows(endpointsContainer, endpoints);
    div.querySelector("#tool-endpoints-add").addEventListener("click", () => this.addEndpointRow(endpointsContainer));
    div.querySelector("#tool-qc-base_url-select").addEventListener("change", (e) => {
      if (e.target.value) div.querySelector("#tool-quote_client-base_url").value = e.target.value;
    });
    this.wireResetButtons(div);

    const btn = div.querySelector(".save-btn");
    btn.addEventListener(
      "click",
      withLoading(btn, async () => {
        const patch = {};
        const baseUrl = div.querySelector("#tool-quote_client-base_url").value;
        if (baseUrl !== g.base_url.value) patch.base_url = baseUrl;

        const novosEndpoints = {};
        endpointsContainer.querySelectorAll(".endpoint-row").forEach((row) => {
          const label = row.querySelector(".ep-label").value.trim();
          const url = row.querySelector(".ep-url").value.trim();
          if (label && url) novosEndpoints[label] = url;
        });
        if (JSON.stringify(novosEndpoints) !== JSON.stringify(endpoints)) patch.endpoints = novosEndpoints;

        for (const key of ["timeout_s", "max_attempts", "budget_s", "backoff_base_s"]) {
          const novo = Number(document.getElementById(`tool-quote_client-${key}`).value);
          if (novo !== g[key].value) patch[key] = novo;
        }
        if (Object.keys(patch).length === 0) {
          toast("nada para salvar");
          return;
        }
        await api("/api/tools", { method: "PUT", body: { quote_client: patch } });
        await this.load();
        toast("aplicado", "success");
      })
    );
    return div;
  },

  wireResetButtons(div) {
    div.querySelectorAll(".reset-btn").forEach((btn) => {
      btn.addEventListener(
        "click",
        withLoading(btn, async () => {
          await api(`/api/tools/${btn.dataset.path}`, { method: "DELETE" });
          await this.load();
          toast("aplicado", "success");
        })
      );
    });
  },
};

// -------------------------------------------------------------------------- aba Config
const Config = {
  effective: null,
  campos: [
    { key: "gemini_model", label: "Modelo Gemini", type: "text" },
    {
      key: "responder_history_runs",
      label: "Janela de contexto do Responder",
      help: "mensagens do histórico enviadas em cada chamada; o Extractor é sem histórico por desenho",
      type: "number",
    },
    { key: "extractor_temperature", label: "Temperatura do Extractor", type: "number", step: "0.1" },
    { key: "responder_temperature", label: "Temperatura do Responder", type: "number", step: "0.1" },
    { key: "llm_max_tentativas", label: "Máx. tentativas do LLM", type: "number" },
    { key: "llm_budget_s", label: "Orçamento do LLM (s)", type: "number", step: "0.5" },
    { key: "script_delay_s", label: "Delay do roteiro do CLI (s)", type: "number", step: "0.1" },
    { key: "agent_db_path", label: "Caminho do banco do agente", type: "text" },
  ],

  async load() {
    const data = await api("/api/effective");
    this.effective = data.settings;
    this.render();
  },

  render() {
    const el = document.getElementById("config-card");
    el.innerHTML =
      "<h3>settings</h3>" +
      this.campos
        .map((def) => {
          const campo = this.effective[def.key];
          const id = `cfg-${def.key}`;
          return `<div class="card-row">
        <label class="field">${escapeHtml(def.label)}${def.help ? `<span class="muted"> — ${escapeHtml(def.help)}</span>` : ""}
          <input type="${def.type}" id="${id}" value="${escapeHtml(campo.value ?? "")}" ${def.step ? `step="${def.step}"` : ""} /></label>
        ${badgeOrigem(campo.origem)}
        ${campo.origem === "override" ? `<button type="button" class="small reset-btn" data-key="${def.key}">voltar ao padrão</button>` : ""}
      </div>`;
        })
        .join("") +
      '<div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>';

    el.querySelectorAll(".reset-btn").forEach((btn) => {
      btn.addEventListener(
        "click",
        withLoading(btn, async () => {
          await api(`/api/config/${btn.dataset.key}`, { method: "DELETE" });
          await this.load();
          toast("aplicado", "success");
        })
      );
    });

    const btn = el.querySelector(".save-btn");
    btn.addEventListener(
      "click",
      withLoading(btn, async () => {
        const patch = {};
        for (const def of this.campos) {
          const raw = document.getElementById(`cfg-${def.key}`).value;
          const novo = def.type === "number" ? Number(raw) : raw;
          if (novo !== this.effective[def.key].value) patch[def.key] = novo;
        }
        if (Object.keys(patch).length === 0) {
          toast("nada para salvar");
          return;
        }
        await api("/api/config", { method: "PUT", body: patch });
        await this.load();
        toast("aplicado", "success");
      })
    );
  },
};

// -------------------------------------------------------------------------- aba Lab
function summarizeEvent(ev) {
  const d = ev.data || {};
  switch (ev.event) {
    case "inbound":
      return `inbound: "${truncate(d.text || `(mídia: ${d.media_type})`, 60)}"`;
    case "outbound":
      return `outbound (${d.source}): "${truncate(d.text, 60)}"`;
    case "extraction": {
      const partes = [];
      if (d.intent) partes.push(`intent=${d.intent}`);
      if (d.idade != null) partes.push(`idade=${d.idade}`);
      if (d.veiculo_texto) partes.push(`veiculo=${d.veiculo_texto}`);
      if (d.veiculo_ano != null) partes.push(`ano=${d.veiculo_ano}`);
      if (d.cep) partes.push(`cep=${d.cep}`);
      if (d.plano_id) partes.push(`plano=${d.plano_id}`);
      if (d.indisponivel) partes.push("indisponivel");
      return `extraction: ${partes.join(" ") || "(sem campos)"}`;
    }
    case "decision":
      return `decision: ${d.stage} → ${(d.actions || []).join(", ")}`;
    case "quote_attempt":
      return `quote_attempt ${d.attempt}: ${d.status}${d.http_status ? " " + d.http_status : ""} · ${d.latency_ms} ms`;
    case "quote_result":
      return `quote_result: ${d.outcome}${d.motivo_recusa ? " — " + d.motivo_recusa : ""}${d.erro ? " — " + d.erro : ""} · ${d.total_ms} ms`;
    case "cep_lookup":
      return `cep_lookup: existe=${d.existe} ${d.cidade || ""}${d.uf ? "/" + d.uf : ""}`;
    case "llm_call":
      return `llm_call (${d.papel}): ${d.latency_ms} ms`;
    case "llm_trace":
      return `llm_trace (${d.papel} #${d.tentativa}): ${d.status} · ${d.latency_ms} ms`;
    case "handoff":
      return `handoff: ${d.reason}`;
    case "refusal":
      return `refusal: ${truncate(d.motivo, 60)}`;
    case "error":
      return `error: ${d.erro}${d.detalhe ? " — " + d.detalhe : ""}`;
    default:
      return `${ev.event}: ${truncate(JSON.stringify(d), 60)}`;
  }
}

const Lab = {
  sessionId: null,
  es: null,
  turnos: new Map(), // message_id do inbound -> { inbound, events: [] }
  turnoAtual: null,
  turnoSelecionado: null,

  init() {
    this.initSelector();
    const btnNova = document.getElementById("lab-new-session");
    btnNova.addEventListener("click", withLoading(btnNova, () => this.novaSessao()));
    document.getElementById("lab-send").addEventListener("click", () => this.enviar());
    document.getElementById("lab-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.enviar();
    });
    document.getElementById("lab-audio").addEventListener("click", () => this.enviarAudio());
  },

  async initSelector() {
    try {
      const data = await api("/api/effective");
      const endpoints = (data.tools.quote_client && data.tools.quote_client.endpoints.value) || {};
      const select = document.getElementById("lab-api-select");
      select.innerHTML =
        '<option value="">(usar URL livre)</option>' +
        Object.entries(endpoints)
          .map(([label, url]) => `<option value="${escapeHtml(url)}">${escapeHtml(label)} — ${escapeHtml(url)}</option>`)
          .join("");
      select.addEventListener("change", () => {
        if (select.value) document.getElementById("lab-api-custom").value = select.value;
      });
    } catch (err) {
      toast(err.message, "error");
    }
  },

  async novaSessao() {
    this.fecharSse();
    const custom = document.getElementById("lab-api-custom").value.trim();
    const body = custom ? { api: custom } : {};
    let data;
    try {
      data = await api("/api/lab/sessions", { method: "POST", body });
    } catch (err) {
      const aviso = document.getElementById("lab-unavailable");
      aviso.hidden = false;
      aviso.textContent = `Lab indisponível: ${err.message}`;
      return;
    }
    document.getElementById("lab-unavailable").hidden = true;
    this.sessionId = data.id;
    this.turnos.clear();
    this.turnoAtual = null;
    this.turnoSelecionado = null;
    document.getElementById("lab-session-id").textContent = `sessão ${data.id} · ${data.api || "(padrão)"}`;
    document.getElementById("lab-messages").innerHTML = "";
    document.getElementById("lab-events-list").innerHTML = "";
    document.getElementById("lab-context-body").innerHTML = '<p class="muted">Selecione um turno (bolha do lead) para ver o contexto.</p>';
    document.getElementById("lab-state-json").textContent = "(sem turnos ainda)";
    document.getElementById("lab-input").disabled = false;
    document.getElementById("lab-send").disabled = false;
    document.getElementById("lab-audio").disabled = false;
    this.abrirSse();
  },

  abrirSse() {
    this.es = sse(`/api/lab/sessions/${this.sessionId}/events`, (ev) => this.onEvento(ev));
  },

  fecharSse() {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
  },

  onEvento(ev) {
    if (ev.event === "inbound") {
      this.turnoAtual = ev.message_id;
      this.turnos.set(this.turnoAtual, { inbound: ev, events: [] });
      this.renderBubbleLead(ev);
    } else {
      if (this.turnoAtual && this.turnos.has(this.turnoAtual)) this.turnos.get(this.turnoAtual).events.push(ev);
      if (ev.event === "outbound") this.renderBubbleAgent(ev);
    }
    this.renderEventRow(ev);
    if (this.turnoSelecionado && this.turnoSelecionado === this.turnoAtual) this.renderContexto();
  },

  renderBubbleLead(ev) {
    const el = document.createElement("div");
    el.className = "bubble bubble-lead";
    el.dataset.turno = ev.message_id;
    el.textContent = ev.data.text || `(mídia: ${ev.data.media_type})`;
    el.addEventListener("click", () => this.selecionarTurno(ev.message_id));
    const box = document.getElementById("lab-messages");
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  },

  renderBubbleAgent(ev) {
    const el = document.createElement("div");
    el.className = "bubble bubble-agent";
    el.dataset.turno = this.turnoAtual || "";
    el.innerHTML = `${escapeHtml(ev.data.text)}<span class="src">${escapeHtml(ev.data.source || "")}</span>`;
    if (this.turnoAtual) el.addEventListener("click", () => this.selecionarTurno(this.turnoAtual));
    const box = document.getElementById("lab-messages");
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  },

  selecionarTurno(id) {
    this.turnoSelecionado = id;
    document.querySelectorAll("#lab-messages .bubble").forEach((b) => b.classList.toggle("selected", b.dataset.turno === id));
    this.renderContexto();
  },

  renderEventRow(ev) {
    const list = document.getElementById("lab-events-list");
    const row = document.createElement("div");
    row.className = "event-row";
    const hora = (ev.ts || "").slice(11, 19);
    row.innerHTML = `<div class="event-line">
        <time>${escapeHtml(hora)}</time>
        <span class="badge badge-ev badge-ev-${escapeHtml(ev.event)}">${escapeHtml(ev.event)}</span>
        <span>${escapeHtml(summarizeEvent(ev))}</span>
      </div>`;
    row.addEventListener("click", () => {
      const existente = row.querySelector("pre");
      if (existente) {
        existente.remove();
        return;
      }
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(ev, null, 2);
      row.appendChild(pre);
    });
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
  },

  renderContexto() {
    const el = document.getElementById("lab-context-body");
    const turno = this.turnos.get(this.turnoSelecionado);
    if (!turno) {
      el.innerHTML = '<p class="muted">Selecione um turno (bolha do lead) para ver o contexto.</p>';
      return;
    }
    const traces = turno.events.filter((e) => e.event === "llm_trace");
    if (traces.length === 0) {
      el.innerHTML = '<p class="muted">Sem chamadas de LLM neste turno (ainda).</p>';
      return;
    }
    el.innerHTML = traces
      .map((t) => {
        const d = t.data;
        const historico =
          (d.historico || [])
            .map((m) => `<div><strong>${escapeHtml(m.role)}${m.from_history ? " (histórico)" : ""}:</strong> ${escapeHtml(m.content)}</div>`)
            .join("") || '<span class="muted">vazio</span>';
        const saida = typeof d.saida === "string" ? d.saida : JSON.stringify(d.saida, null, 2);
        return `<div class="trace-block">
          <h4>${escapeHtml(d.papel)} · tentativa ${escapeHtml(d.tentativa)} · ${escapeHtml(d.status)} · ${escapeHtml(d.latency_ms)} ms · ${escapeHtml(d.modelo || "")}</h4>
          <div><strong>instructions</strong><pre>${escapeHtml(d.instructions || "")}</pre></div>
          <div><strong>histórico enviado</strong><div>${historico}</div></div>
          <div><strong>entrada</strong><pre>${escapeHtml(d.entrada || "")}</pre></div>
          <div><strong>saída</strong><pre>${escapeHtml(saida)}</pre></div>
          ${d.erro ? `<div><strong>erro</strong><pre>${escapeHtml(d.erro)}</pre></div>` : ""}
        </div>`;
      })
      .join("");
  },

  async enviar() {
    const input = document.getElementById("lab-input");
    const texto = input.value.trim();
    if (!texto || !this.sessionId) return;
    input.value = "";
    await this.postMensagem({ text: texto });
  },

  async enviarAudio() {
    if (!this.sessionId) return;
    await this.postMensagem({ media_type: "audio" });
  },

  async postMensagem(body) {
    const typing = document.getElementById("lab-typing");
    typing.hidden = false;
    try {
      const data = await api(`/api/lab/sessions/${this.sessionId}/messages`, { method: "POST", body });
      document.getElementById("lab-state-json").textContent = JSON.stringify(data.state, null, 2);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      typing.hidden = true;
    }
  },
};

// -------------------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", () => {
  Health.start();
  Lab.init();
  document.getElementById("prompts-search").addEventListener("input", () => Prompts.renderList());
  renderTab();
});
