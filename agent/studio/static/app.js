// AutoSeguro Studio — vanilla JS (ES2020), sem framework/bundler/CDN. Módulo ES (`type="module"`
// no index.html), então nada aqui vaza para `window`: cada `const`/`function` de topo é escopo
// do módulo, não global solto.

import { escapeHtml, renderMarkdown } from "./markdown.js";

// -------------------------------------------------------------------------- core: api/sse/toast
const toastsEl = document.getElementById("toasts");

function el(id) {
  return document.getElementById(id);
}

function toast(message, kind = "info") {
  const div = document.createElement("div");
  div.className = "toast" + (kind === "error" ? " error" : kind === "success" ? " success" : "");
  div.textContent = message;
  toastsEl.appendChild(div);
  setTimeout(() => div.remove(), 4000);
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

/** Spinner + trava do botão durante a chamada. `aoTerminar` devolve o botão ao estado que a
 *  tela manda (ex.: "Salvar" volta desabilitado depois de salvar) — sem ele, o `disabled = false`
 *  do finally desfaria o que o render acabou de decidir. */
function withLoading(button, fn, aoTerminar) {
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
      if (aoTerminar) aoTerminar();
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

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function badgeEl(texto, classe = "") {
  const span = document.createElement("span");
  span.className = "badge" + (classe ? ` ${classe}` : "");
  span.textContent = texto;
  return span;
}

function badgeOrigem(origem) {
  const classe = origem === "override" ? "badge-origem-override" : origem.startsWith("env:") ? "badge-origem-env" : "badge-origem-default";
  return `<span class="badge ${classe}">${escapeHtml(origem)}</span>`;
}

// -------------------------------------------------------------------------- router (hash → aba)
const TABS = ["lab", "prompts", "tools", "config"];
const TAB_LABELS = { lab: "Lab", prompts: "Prompts", tools: "Tools", config: "Config" };

function abaAtual() {
  const h = (location.hash || "#lab").slice(1);
  return TABS.includes(h) ? h : "lab";
}

// Breadcrumb da barra superior: `<Aba> / <item>` (a marca à esquerda já diz "Studio").
// Cada aba guarda o seu último item (label do slot, id curto da sessão…); Tools/Config não têm.
const Breadcrumb = {
  itens: { lab: null, prompts: null, tools: null, config: null },

  set(tab, item) {
    this.itens[tab] = item || null;
    if (abaAtual() === tab) this.render();
  },

  render() {
    const tab = abaAtual();
    const partes = [TAB_LABELS[tab]];
    if (this.itens[tab]) partes.push(this.itens[tab]);
    const box = el("breadcrumb");
    box.innerHTML = "";
    partes.forEach((parte, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "sep";
        sep.textContent = "/";
        box.appendChild(sep);
      }
      const span = document.createElement("span");
      span.className = "crumb" + (i === partes.length - 1 ? " current" : "");
      span.textContent = parte; // textContent: nada de innerHTML com dado da API
      box.appendChild(span);
    });
  },
};

function renderTab() {
  const tab = abaAtual();
  for (const t of TABS) el(`tab-${t}`).hidden = t !== tab;
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  Breadcrumb.render();
  onTabShown(tab);
}

function onTabShown(tab) {
  if (tab === "prompts") Prompts.load().catch((e) => toast(e.message, "error"));
  if (tab === "tools") Tools.load().catch((e) => toast(e.message, "error"));
  if (tab === "config") Config.load().catch((e) => toast(e.message, "error"));
}

window.addEventListener("hashchange", renderTab);

// -------------------------------------------------------------------------- saúde (barra superior)
const Health = {
  async check() {
    const dot = el("health-dot");
    const text = el("health-text");
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

// -------------------------------------------------------------------------- dropdowns (popover)
// Um só aberto por vez; fecha em clique fora e no Esc. O menu é irmão do gatilho dentro de `.dd`
// (posicionamento por CSS), então não há cálculo de coordenada aqui.
let ddAberto = null;

function fecharDd() {
  if (!ddAberto) return;
  ddAberto.menu.hidden = true;
  if (ddAberto.trigger) ddAberto.trigger.setAttribute("aria-expanded", "false");
  ddAberto = null;
}

function abrirDd(menu, trigger, aoAbrir) {
  fecharDd();
  menu.hidden = false;
  if (trigger) trigger.setAttribute("aria-expanded", "true");
  ddAberto = { menu, trigger };
  if (aoAbrir) aoAbrir();
}

function alternarDd(menu, trigger, aoAbrir) {
  if (ddAberto && ddAberto.menu === menu) fecharDd();
  else abrirDd(menu, trigger, aoAbrir);
}

document.addEventListener("mousedown", (ev) => {
  if (!ddAberto) return;
  const dentro = ddAberto.menu.contains(ev.target) || (ddAberto.trigger && ddAberto.trigger.contains(ev.target));
  if (!dentro) fecharDd();
});

// -------------------------------------------------------------------------- aba Prompts
const VIEWS = ["editor", "split", "preview"];

const Prompts = {
  slots: {},
  key: null,
  version: null,
  view: "split",
  rascunhos: {}, // "key\nversão" -> {text, note} ainda não salvos (sobrevivem à troca de aba)

  // ---- dados
  async load() {
    const data = await api("/api/prompts");
    this.slots = data.slots;
    if (!this.slots[this.key]) this.key = Object.keys(this.slots)[0] || null;
    const slot = this.slot();
    if (slot && !slot.versions[this.version]) this.version = slot.active;
    this.render();
  },

  async recarregar(msg) {
    await this.load();
    if (msg) toast(msg, "success");
  },

  slot() {
    return this.slots[this.key] || null;
  },

  versao() {
    const slot = this.slot();
    return slot ? slot.versions[this.version] : null;
  },

  chave() {
    return `${this.key}\n${this.version}`;
  },

  rascunho() {
    return this.rascunhos[this.chave()] || null;
  },

  alterado() {
    return this.rascunho() !== null;
  },

  // ---- render
  render() {
    const slot = this.slot();
    const versao = this.versao();
    if (!slot || !versao) return;
    const ehDefault = this.version === "default";
    const ehAtiva = this.version === slot.active;
    const rascunho = this.rascunho();

    Breadcrumb.set("prompts", slot.label);
    el("dd-slot-label").textContent = slot.label;
    el("dd-version-label").textContent = this.version + (ehAtiva ? " • ativa" : "");

    // `default` costuma ser a ativa: mostra os dois selos em vez de esconder um deles
    const badges = el("pv-badges");
    badges.innerHTML = "";
    if (ehAtiva) badges.appendChild(badgeEl("Ativa", "badge-ativa"));
    if (ehDefault) badges.appendChild(badgeEl("Default · imutável"));
    else if (!ehAtiva) badges.appendChild(badgeEl("Rascunho"));

    const editor = el("prompts-editor");
    editor.value = rascunho ? rascunho.text : versao.text;
    editor.readOnly = ehDefault; // readOnly (e não disabled): ainda dá para ler, rolar e copiar
    const note = el("pv-note");
    note.value = rascunho ? rascunho.note : versao.note || "";
    note.readOnly = ehDefault;
    note.placeholder = ehDefault ? "comportamento entregue, imutável — crie uma versão" : "nota da versão";

    el("pv-save").disabled = ehDefault || !this.alterado();
    el("pv-activate").disabled = ehAtiva;
    el("pv-delete").disabled = ehDefault || ehAtiva;
    el("pv-diff").disabled = ehDefault;

    this.renderPlaceholders(slot, ehDefault);
    this.setView(this.view);
  },

  renderPlaceholders(slot, ehDefault) {
    const box = el("pv-placeholders");
    box.innerHTML = "";
    const lista = slot.placeholders || [];
    if (!lista.length) {
      const vazio = document.createElement("span");
      vazio.className = "muted";
      vazio.textContent = "sem placeholders";
      box.appendChild(vazio);
      return;
    }
    for (const p of lista) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = `{${p}}`;
      chip.title = `inserir {${p}} no cursor`;
      chip.disabled = ehDefault;
      chip.addEventListener("click", () => this.inserirPlaceholder(p));
      box.appendChild(chip);
    }
  },

  setView(view) {
    this.view = VIEWS.includes(view) ? view : "split";
    el("pp-body").dataset.view = this.view;
    document.querySelectorAll("#pp-views button").forEach((b) => b.classList.toggle("active", b.dataset.view === this.view));
    // o segmentado Texto|Preview marca o que está VISÍVEL: no dividido, os dois acesos
    document.querySelectorAll("#pp-seg button").forEach((b) => {
      b.classList.toggle("active", this.view === "split" || this.view === b.dataset.view);
    });
    if (this.view !== "editor") this.renderPreview();
  },

  renderPreview() {
    el("prompts-preview").innerHTML = renderMarkdown(el("prompts-editor").value);
  },

  renderSlotList() {
    const q = (el("prompts-search").value || "").trim().toLowerCase();
    const lista = el("dd-slot-list");
    lista.innerHTML = "";
    const grupos = {};
    for (const [key, slot] of Object.entries(this.slots)) {
      if (q && !key.toLowerCase().includes(q) && !slot.label.toLowerCase().includes(q)) continue;
      (grupos[slot.grupo] ||= []).push([key, slot]);
    }
    const nomes = Object.keys(grupos).sort();
    if (!nomes.length) {
      const vazio = document.createElement("div");
      vazio.className = "dd-group";
      vazio.textContent = "nenhum slot";
      lista.appendChild(vazio);
      return;
    }
    for (const grupo of nomes) {
      const titulo = document.createElement("div");
      titulo.className = "dd-group";
      titulo.textContent = grupo;
      lista.appendChild(titulo);
      for (const [key, slot] of grupos[grupo]) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "dd-item" + (key === this.key ? " selected" : "");
        const label = document.createElement("span");
        label.className = "dd-item-label";
        label.textContent = slot.label;
        const chave = document.createElement("span");
        chave.className = "dd-item-key";
        chave.textContent = key;
        item.append(label, chave);
        item.addEventListener("click", () => {
          fecharDd();
          this.selecionarSlot(key);
        });
        lista.appendChild(item);
      }
    }
  },

  renderVersionList() {
    const slot = this.slot();
    const lista = el("dd-version-list");
    lista.innerHTML = "";
    if (!slot) return;
    for (const [nome, versao] of Object.entries(slot.versions)) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "dd-item" + (nome === this.version ? " selected" : "");
      const label = document.createElement("span");
      label.className = "dd-item-label";
      label.textContent = nome + (nome === slot.active ? " • ativa" : "");
      const nota = document.createElement("span");
      nota.className = "dd-item-key";
      nota.textContent = truncate(versao.note || "", 28);
      item.append(label, nota);
      item.addEventListener("click", () => {
        fecharDd();
        this.selecionarVersao(nome);
      });
      lista.appendChild(item);
    }
    const sep = document.createElement("div");
    sep.className = "dd-sep";
    lista.appendChild(sep);
    const nova = document.createElement("button");
    nova.type = "button";
    nova.className = "dd-item";
    nova.textContent = "+ Nova versão…";
    nova.addEventListener("click", () => {
      el("pv-new-name").value = "";
      el("pv-new-note").value = "";
      el("pv-new-activate").checked = true;
      abrirDd(el("pv-new-menu"), el("dd-version-btn"), () => el("pv-new-name").focus());
    });
    lista.appendChild(nova);
  },

  // ---- edição
  aoEditar() {
    const versao = this.versao();
    if (!versao) return;
    const text = el("prompts-editor").value;
    const note = el("pv-note").value;
    if (text === versao.text && note === (versao.note || "")) delete this.rascunhos[this.chave()];
    else this.rascunhos[this.chave()] = { text, note };
    el("pv-save").disabled = this.version === "default" || !this.alterado();
    if (this.view !== "editor") this.renderPreview();
  },

  inserirPlaceholder(p) {
    const editor = el("prompts-editor");
    if (editor.readOnly) return;
    const ini = editor.selectionStart ?? editor.value.length;
    const fim = editor.selectionEnd ?? ini;
    const marca = `{${p}}`;
    editor.value = editor.value.slice(0, ini) + marca + editor.value.slice(fim);
    editor.focus();
    editor.selectionStart = editor.selectionEnd = ini + marca.length;
    this.aoEditar();
  },

  selecionarSlot(key) {
    if (!this.slots[key]) return;
    this.key = key;
    this.version = this.slots[key].active;
    this.render();
  },

  selecionarVersao(nome) {
    this.version = nome;
    this.render();
  },

  // ---- ações da API
  async salvar() {
    if (el("pv-save").disabled) return;
    const key = this.key;
    const nome = this.version;
    // `note` vai junto: o campo é editável na toolbar, então o rótulo da versão é o que está lá
    await api(`/api/prompts/${encodeURIComponent(key)}/versions/${encodeURIComponent(nome)}`, {
      method: "PUT",
      body: { text: el("prompts-editor").value, note: el("pv-note").value },
    });
    delete this.rascunhos[`${key}\n${nome}`];
    await this.recarregar("salvo");
  },

  async ativar() {
    await api(`/api/prompts/${encodeURIComponent(this.key)}/active`, { method: "PUT", body: { name: this.version } });
    await this.recarregar("versão ativada");
  },

  async apagar() {
    const nome = this.version;
    await api(`/api/prompts/${encodeURIComponent(this.key)}/versions/${encodeURIComponent(nome)}`, { method: "DELETE" });
    delete this.rascunhos[`${this.key}\n${nome}`];
    this.version = null; // o load cai na ativa
    await this.recarregar("versão apagada");
  },

  async criarVersao() {
    const nome = el("pv-new-name").value.trim();
    if (!nome) {
      toast("nome da versão é obrigatório", "error");
      return;
    }
    if (/\s/.test(nome)) {
      toast("nome da versão não pode ter espaços", "error");
      return;
    }
    await api(`/api/prompts/${encodeURIComponent(this.key)}/versions`, {
      method: "POST",
      body: {
        name: nome,
        text: el("prompts-editor").value,
        note: el("pv-new-note").value,
        activate: el("pv-new-activate").checked,
      },
    });
    delete this.rascunhos[this.chave()];
    this.version = nome;
    fecharDd();
    await this.recarregar("versão criada");
  },

  abrirDiff() {
    const slot = this.slot();
    if (!slot || this.version === "default") return;
    const a = (slot.versions.default.text || "").split("\n");
    const b = (el("prompts-editor").value || "").split("\n");
    let esquerda = "";
    let direita = "";
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const la = a[i] ?? "";
      const lb = b[i] ?? "";
      const mudou = la !== lb;
      esquerda += `<div class="diff-line${mudou ? " changed" : ""}">${escapeHtml(la) || "&nbsp;"}</div>`;
      direita += `<div class="diff-line${mudou ? " changed" : ""}">${escapeHtml(lb) || "&nbsp;"}</div>`;
    }
    el("diff-title").textContent = `default × ${this.version}`;
    el("diff-body").innerHTML = `<div class="diff-grid"><pre>${esquerda}</pre><pre>${direita}</pre></div>`;
    el("diff-drawer").hidden = false;
  },

  init() {
    el("dd-slot-btn").addEventListener("click", () => this.abrirBuscaSlot());
    el("prompts-search").addEventListener("input", () => this.renderSlotList());
    el("dd-version-btn").addEventListener("click", () =>
      alternarDd(el("dd-version-menu"), el("dd-version-btn"), () => this.renderVersionList())
    );
    el("pv-new-cancel").addEventListener("click", () => fecharDd());
    const criar = el("pv-new-create");
    criar.addEventListener("click", withLoading(criar, () => this.criarVersao()));

    el("prompts-editor").addEventListener("input", () => this.aoEditar());
    el("pv-note").addEventListener("input", () => this.aoEditar());
    document.querySelectorAll("#pp-views button, #pp-seg button").forEach((b) => {
      b.addEventListener("click", () => this.setView(b.dataset.view));
    });

    const salvar = el("pv-save");
    salvar.addEventListener("click", withLoading(salvar, () => this.salvar(), () => this.render()));
    const ativar = el("pv-activate");
    ativar.addEventListener("click", withLoading(ativar, () => this.ativar(), () => this.render()));
    const apagar = el("pv-delete");
    apagar.addEventListener("click", withLoading(apagar, () => this.apagar(), () => this.render()));

    el("pv-diff").addEventListener("click", () => this.abrirDiff());
    el("diff-close").addEventListener("click", () => this.fecharDiff());
    el("diff-close-bg").addEventListener("click", () => this.fecharDiff());
  },

  fecharDiff() {
    el("diff-drawer").hidden = true;
  },

  abrirBuscaSlot() {
    alternarDd(el("dd-slot-menu"), el("dd-slot-btn"), () => {
      this.renderSlotList();
      el("prompts-search").focus();
      el("prompts-search").select();
    });
  },
};

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

// -------------------------------------------------------------------------- eventos do Lab (resumo de 1 linha)
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

// -------------------------------------------------------------------------- sessão do Lab (compartilhada)
// Uma sessão só para as duas telas que conversam com o agente: a aba Lab e o "Testar prompt"
// da aba Prompts. Uma única conexão SSE, com fan-out para os assinantes — assim as duas veem
// exatamente os mesmos eventos, na mesma ordem, sem abrir dois streams do mesmo `/events`.
const LabSession = {
  id: null,
  api: "",
  es: null,
  endpoints: {}, // rótulo -> url (de /api/effective)
  geminiModel: "",
  apiEscolhida: "", // "" = deixa o agente usar a base_url dele
  mensagens: [], // {lado: "lead"|"agent", texto, source?, turno?}
  ouvintes: new Set(),
  _criando: null,

  subscribe(fn) {
    this.ouvintes.add(fn);
  },

  _emitir(msg) {
    for (const fn of this.ouvintes) fn(msg);
  },

  async carregarEfetivo() {
    const data = await api("/api/effective");
    const qc = data.tools.quote_client;
    this.endpoints = (qc && qc.endpoints.value) || {};
    this.geminiModel = (data.settings.gemini_model && data.settings.gemini_model.value) || "";
    this._emitir({ tipo: "efetivo" });
  },

  /** A API de cotação é fixada quando a sessão é criada — trocá-la exige sessão nova. */
  async trocarApi(url) {
    const novo = (url || "").trim();
    if (novo === this.apiEscolhida) return;
    this.apiEscolhida = novo;
    this._emitir({ tipo: "api" });
    if (!this.id) return;
    try {
      await this.novaSessao();
      toast(`nova sessão · ${novo || "API padrão"}`, "success");
    } catch (err) {
      toast(err.message || String(err), "error");
    }
  },

  async garantirSessao() {
    if (this.id) return this.id;
    if (!this._criando) {
      this._criando = this._criar().finally(() => {
        this._criando = null;
      });
    }
    return this._criando;
  },

  async novaSessao() {
    this.encerrar();
    return this.garantirSessao();
  },

  async _criar() {
    const body = this.apiEscolhida ? { api: this.apiEscolhida } : {};
    let data;
    try {
      data = await api("/api/lab/sessions", { method: "POST", body });
    } catch (err) {
      this._emitir({ tipo: "indisponivel", mensagem: err.message });
      throw err;
    }
    this.id = data.id;
    this.api = data.api || "";
    this.mensagens = [];
    this._emitir({ tipo: "sessao" });
    this.es = sse(`/api/lab/sessions/${this.id}/events`, (ev) => this._onEvento(ev));
    return this.id;
  },

  _onEvento(ev) {
    if (ev.event === "inbound") {
      this.mensagens.push({ lado: "lead", texto: ev.data.text || `(mídia: ${ev.data.media_type})`, turno: ev.message_id });
    } else if (ev.event === "outbound") {
      this.mensagens.push({ lado: "agent", texto: ev.data.text, source: ev.data.source });
    }
    this._emitir({ tipo: "evento", ev });
  },

  encerrar() {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
    this.id = null;
    this.mensagens = [];
  },

  async enviar(body) {
    await this.garantirSessao();
    this._emitir({ tipo: "enviando", ativo: true });
    try {
      const data = await api(`/api/lab/sessions/${this.id}/messages`, { method: "POST", body });
      this._emitir({ tipo: "estado", state: data.state });
      return data;
    } finally {
      this._emitir({ tipo: "enviando", ativo: false });
    }
  },
};

// -------------------------------------------------------------------------- aba Lab
const Lab = {
  turnos: new Map(), // message_id do inbound -> { inbound, events: [] }
  turnoAtual: null,
  turnoSelecionado: null,

  init() {
    const btnNova = el("lab-new-session");
    btnNova.addEventListener(
      "click",
      withLoading(btnNova, () => {
        LabSession.apiEscolhida = el("lab-api-custom").value.trim();
        return LabSession.novaSessao();
      })
    );
    el("lab-api-select").addEventListener("change", (e) => {
      if (e.target.value) el("lab-api-custom").value = e.target.value;
    });
    el("lab-send").addEventListener("click", () => this.enviar());
    el("lab-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.enviar();
    });
    el("lab-audio").addEventListener("click", () => this.enviarAudio());
    LabSession.subscribe((msg) => this.onSessao(msg));
  },

  onSessao(msg) {
    if (msg.tipo === "efetivo") this.preencherSelect();
    else if (msg.tipo === "api") el("lab-api-custom").value = LabSession.apiEscolhida;
    else if (msg.tipo === "sessao") this.aoAbrirSessao();
    else if (msg.tipo === "evento") this.onEvento(msg.ev);
    else if (msg.tipo === "estado") el("lab-state-json").textContent = JSON.stringify(msg.state, null, 2);
    else if (msg.tipo === "enviando") el("lab-typing").hidden = !msg.ativo;
    else if (msg.tipo === "indisponivel") {
      const aviso = el("lab-unavailable");
      aviso.hidden = false;
      aviso.textContent = `Lab indisponível: ${msg.mensagem}`;
    }
  },

  preencherSelect() {
    el("lab-api-select").innerHTML =
      '<option value="">(usar URL livre)</option>' +
      Object.entries(LabSession.endpoints)
        .map(([label, url]) => `<option value="${escapeHtml(url)}">${escapeHtml(label)} — ${escapeHtml(url)}</option>`)
        .join("");
  },

  aoAbrirSessao() {
    this.turnos.clear();
    this.turnoAtual = null;
    this.turnoSelecionado = null;
    el("lab-unavailable").hidden = true;
    el("lab-session-id").textContent = `sessão ${LabSession.id} · ${LabSession.api || "(padrão)"}`;
    Breadcrumb.set("lab", String(LabSession.id).slice(0, 8));
    el("lab-messages").innerHTML = "";
    el("lab-events-list").innerHTML = "";
    el("lab-context-body").innerHTML = '<p class="muted">Selecione um turno (bolha do lead) para ver o contexto.</p>';
    el("lab-state-json").textContent = "(sem turnos ainda)";
    el("lab-input").disabled = false;
    el("lab-send").disabled = false;
    el("lab-audio").disabled = false;
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
    const bolha = document.createElement("div");
    bolha.className = "bubble bubble-lead";
    bolha.dataset.turno = ev.message_id;
    bolha.textContent = ev.data.text || `(mídia: ${ev.data.media_type})`;
    bolha.addEventListener("click", () => this.selecionarTurno(ev.message_id));
    const box = el("lab-messages");
    box.appendChild(bolha);
    box.scrollTop = box.scrollHeight;
  },

  renderBubbleAgent(ev) {
    const bolha = document.createElement("div");
    bolha.className = "bubble bubble-agent";
    bolha.dataset.turno = this.turnoAtual || "";
    bolha.innerHTML = `${escapeHtml(ev.data.text)}<span class="src">${escapeHtml(ev.data.source || "")}</span>`;
    if (this.turnoAtual) bolha.addEventListener("click", () => this.selecionarTurno(this.turnoAtual));
    const box = el("lab-messages");
    box.appendChild(bolha);
    box.scrollTop = box.scrollHeight;
  },

  selecionarTurno(id) {
    this.turnoSelecionado = id;
    document.querySelectorAll("#lab-messages .bubble").forEach((b) => b.classList.toggle("selected", b.dataset.turno === id));
    this.renderContexto();
  },

  renderEventRow(ev) {
    const list = el("lab-events-list");
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
    const box = el("lab-context-body");
    const turno = this.turnos.get(this.turnoSelecionado);
    if (!turno) {
      box.innerHTML = '<p class="muted">Selecione um turno (bolha do lead) para ver o contexto.</p>';
      return;
    }
    const traces = turno.events.filter((e) => e.event === "llm_trace");
    if (traces.length === 0) {
      box.innerHTML = '<p class="muted">Sem chamadas de LLM neste turno (ainda).</p>';
      return;
    }
    box.innerHTML = traces
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
    const input = el("lab-input");
    const texto = input.value.trim();
    if (!texto) return;
    input.value = "";
    await guarded(() => LabSession.enviar({ text: texto }))();
  },

  async enviarAudio() {
    await guarded(() => LabSession.enviar({ media_type: "audio" }))();
  },
};

// -------------------------------------------------------------------------- "Testar prompt" (aba Prompts)
// Mesmo motor e MESMA sessão do Lab: só o chat aparece aqui — eventos e contexto continuam
// sendo assunto da aba Lab (link "ver eventos no Lab").
const TestPanel = {
  init() {
    el("test-toggle").addEventListener("click", () => this.alternar());
    el("test-send").addEventListener("click", () => this.enviar());
    el("test-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.enviar();
    });
    el("test-clear").addEventListener("click", () => {
      const input = el("test-input");
      input.value = "";
      input.focus();
    });
    el("test-gemini").addEventListener("click", () => {
      location.hash = "#config";
    });
    el("test-api").addEventListener("change", () => {
      const valor = el("test-api").value;
      const livre = valor === "__livre";
      el("test-api-custom").hidden = !livre;
      if (livre) el("test-api-custom").focus();
      else guarded(() => LabSession.trocarApi(valor))();
    });
    el("test-api-custom").addEventListener("change", () => guarded(() => LabSession.trocarApi(el("test-api-custom").value))());
    LabSession.subscribe((msg) => this.onSessao(msg));
  },

  async alternar() {
    const corpo = el("test-body");
    const abrindo = corpo.hidden;
    corpo.hidden = !abrindo;
    const botao = el("test-toggle");
    botao.setAttribute("aria-expanded", String(abrindo));
    botao.classList.toggle("aberto", abrindo);
    if (!abrindo) return;
    this.renderMensagens();
    if (!LabSession.id) await guarded(() => LabSession.garantirSessao())();
    el("test-input").focus();
  },

  onSessao(msg) {
    if (msg.tipo === "efetivo") this.preencherControles();
    else if (msg.tipo === "api") this.refletirApi();
    else if (msg.tipo === "sessao") {
      el("test-unavailable").hidden = true;
      el("test-ver-eventos").hidden = false;
      this.renderMensagens();
    } else if (msg.tipo === "evento") {
      if (msg.ev.event === "inbound" || msg.ev.event === "outbound") this.renderMensagens();
    } else if (msg.tipo === "enviando") el("test-typing").hidden = !msg.ativo;
    else if (msg.tipo === "indisponivel") {
      const aviso = el("test-unavailable");
      aviso.hidden = false;
      aviso.textContent = `Lab indisponível: ${msg.mensagem}`;
    }
  },

  preencherControles() {
    const opcoes = ['<option value="">API padrão do agente</option>'];
    for (const [rotulo, url] of Object.entries(LabSession.endpoints)) {
      opcoes.push(`<option value="${escapeHtml(url)}">${escapeHtml(rotulo)}</option>`);
    }
    opcoes.push('<option value="__livre">URL livre…</option>');
    el("test-api").innerHTML = opcoes.join("");
    this.refletirApi();
    el("test-gemini").textContent = `Gemini · ${LabSession.geminiModel || "—"}`;
  },

  refletirApi() {
    const select = el("test-api");
    const url = LabSession.apiEscolhida || "";
    const conhecida = Array.from(select.options).some((o) => o.value === url);
    select.value = conhecida ? url : "__livre";
    el("test-api-custom").hidden = conhecida;
    if (!conhecida) el("test-api-custom").value = url;
  },

  renderMensagens() {
    const box = el("test-messages");
    const msgs = LabSession.mensagens;
    box.innerHTML = "";
    for (const m of msgs) {
      const bolha = document.createElement("div");
      bolha.className = "bubble " + (m.lado === "lead" ? "bubble-lead" : "bubble-agent");
      bolha.textContent = m.texto;
      if (m.lado === "agent" && m.source) {
        const src = document.createElement("span");
        src.className = "src";
        src.textContent = m.source;
        bolha.appendChild(src);
      }
      box.appendChild(bolha);
    }
    box.hidden = msgs.length === 0;
    el("test-empty").hidden = msgs.length > 0;
    box.scrollTop = box.scrollHeight;
  },

  async enviar() {
    const input = el("test-input");
    const texto = input.value.trim();
    if (!texto) return;
    input.value = "";
    await guarded(() => LabSession.enviar({ text: texto }))();
  },
};

// -------------------------------------------------------------------------- atalhos de teclado
function atalhos() {
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      fecharDd();
      Prompts.fecharDiff();
      return;
    }
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === "s" || ev.key === "S")) {
      if (abaAtual() !== "prompts") return;
      ev.preventDefault();
      const salvar = el("pv-save");
      if (!salvar.disabled) salvar.click();
      return;
    }
    const alvo = ev.target;
    const digitando = alvo && (alvo.tagName === "INPUT" || alvo.tagName === "TEXTAREA" || alvo.isContentEditable);
    if (ev.key === "/" && !digitando && abaAtual() === "prompts") {
      ev.preventDefault();
      Prompts.abrirBuscaSlot();
    }
  });
}

// -------------------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", () => {
  Health.start();
  Prompts.init();
  Lab.init();
  TestPanel.init();
  atalhos();
  LabSession.carregarEfetivo().catch((err) => toast(err.message, "error"));
  renderTab();
});
