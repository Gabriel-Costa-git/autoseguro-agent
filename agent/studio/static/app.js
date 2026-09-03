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
    const erro = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    erro.status = res.status; // 404 numa rota nova = backend ainda não a implementou
    throw erro;
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

/** Texto de uma bolha de chat: escapado primeiro, depois a ênfase do WhatsApp (`*texto*` → negrito).
 *  Só asteriscos simples que abrem e fecham na MESMA linha; `**`, asterisco solto e lista `* item` ficam como
 *  estão. As quebras de linha não viram `<br>`: quem preserva é o `white-space: pre-wrap` da `.bubble`. */
function textoDaBolha(texto) {
  return escapeHtml(texto ?? "").replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, "$1<strong>$2</strong>");
}

function badgeEl(texto, classe = "") {
  const span = document.createElement("span");
  span.className = "badge" + (classe ? ` ${classe}` : "");
  span.textContent = texto;
  return span;
}

// ícones inline (Lucide, 16px stroke 1.5) para o HTML gerado em string pelas fichas
const ICONS = {
  reset: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" />',
  remove: '<path d="M18 6 6 18" /><path d="m6 6 12 12" />',
  plus: '<path d="M12 5v14" /><path d="M5 12h14" />',
};

function icon(nome) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[nome]}</svg>`;
}

function badgeOrigem(origem) {
  const classe = origem === "override" ? "badge-origem-override" : origem.startsWith("env:") ? "badge-origem-env" : "badge-origem-default";
  return `<span class="badge ${classe}">${escapeHtml(origem)}</span>`;
}

// -------------------------------------------------------------------------- router (hash → aba/sub-aba)
// `#atendimentos`, `#atendimentos/<cid>`, `#lab/conversa|prompts|tools`, `#config`.
const TABS = ["atendimentos", "lab", "config"];
const SUBS_LAB = ["conversa", "prompts", "tools"];
const TAB_LABELS = { atendimentos: "Atendimentos", lab: "Lab", config: "Config" };
const SUB_LABELS = { conversa: "Conversa", prompts: "Prompts", tools: "Tools" };
const HASH_LEGADO = { prompts: "#lab/prompts", tools: "#lab/tools" }; // links da v1

function abaAtual() {
  const bruto = (location.hash || "").replace(/^#/, "");
  const partes = bruto.split("/");
  const tab = partes[0];
  if (!TABS.includes(tab)) return { tab: "atendimentos", sub: "", item: "" };
  if (tab === "lab") {
    const sub = SUBS_LAB.includes(partes[1]) ? partes[1] : "conversa";
    return { tab, sub, item: partes.slice(2).join("/") }; // slot de Prompts / nome da tool
  }
  return { tab, sub: partes.slice(1).join("/"), item: "" };
}

/** Manda o hash para a forma canônica; devolve true quando redirecionou (o hashchange refaz o render). */
function normalizarHash() {
  const bruto = (location.hash || "").replace(/^#/, "");
  const raiz = bruto.split("/")[0];
  if (HASH_LEGADO[raiz]) {
    location.replace(HASH_LEGADO[raiz]);
    return true;
  }
  if (!TABS.includes(raiz)) {
    location.replace("#atendimentos"); // landing
    return true;
  }
  if (raiz === "lab" && !SUBS_LAB.includes(bruto.split("/")[1] || "")) {
    location.replace("#lab/conversa");
    return true;
  }
  return false;
}

// Breadcrumb da barra superior: `Atendimentos / <cid>`, `Lab / Conversa / <id>`,
// `Lab / Prompts / <slot>`, `Lab / Tools`, `Config`. A marca à esquerda já diz "Studio".
const Breadcrumb = {
  itens: { conversa: null, prompts: null },

  set(chave, item) {
    this.itens[chave] = item || null;
    this.render();
  },

  render() {
    const { tab, sub } = abaAtual();
    const partes = [TAB_LABELS[tab]];
    if (tab === "lab") {
      partes.push(SUB_LABELS[sub]);
      if (this.itens[sub]) partes.push(this.itens[sub]);
    } else if (tab === "atendimentos" && sub) {
      partes.push(sub);
    }
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

let abaAnterior = null;

function renderTab() {
  if (normalizarHash()) return;
  const { tab, sub, item } = abaAtual();
  if (abaAnterior && abaAnterior !== tab) onTabHidden(abaAnterior);
  abaAnterior = tab;
  for (const t of TABS) el(`tab-${t}`).hidden = t !== tab;
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  for (const s of SUBS_LAB) el(`lab-sub-${s}`).hidden = !(tab === "lab" && s === sub);
  document.querySelectorAll("#lab-subtabs button").forEach((b) => {
    b.classList.toggle("active", tab === "lab" && b.dataset.sub === sub);
  });
  el("lab-session-head").hidden = !(tab === "lab" && sub === "conversa");
  Breadcrumb.render();
  onTabShown(tab, sub, item);
}

function onTabShown(tab, sub, item) {
  if (tab === "atendimentos") Atendimentos.entrar(sub);
  if (tab === "lab" && sub === "prompts") Prompts.load(item).catch((e) => toast(e.message, "error"));
  if (tab === "lab" && sub === "tools") Tools.load(item).catch((e) => toast(e.message, "error"));
  if (tab === "config") Config.load().catch((e) => toast(e.message, "error"));
}

function onTabHidden(tab) {
  if (tab === "atendimentos") Atendimentos.sair(); // nenhum polling roda fora da aba
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

// -------------------------------------------------------------------------- catálogo de modelos
// `GET /api/models` é rota nova: enquanto o backend não a tiver, 404 vira "indisponível" e a
// lista fica só com o modelo em uso — sem erro na tela e sem travar o seletor.
const ModelCatalog = {
  modelos: [],
  atualizadoEm: null,
  disponivel: true,
  ouvintes: new Set(),
  _carregado: false,

  subscribe(fn) {
    this.ouvintes.add(fn);
  },

  _emitir() {
    for (const fn of this.ouvintes) fn();
  },

  /** Lista para os selects, sempre contendo o modelo em uso (mesmo fora do catálogo). */
  lista(atual) {
    const emUso = atual || LabSession.geminiModel;
    const itens = this.modelos.slice();
    if (emUso && !itens.some((m) => m.id === emUso)) itens.unshift({ id: emUso, nome: emUso });
    return itens;
  },

  aplicar(data) {
    this.modelos = Array.isArray(data.modelos) ? data.modelos : [];
    this.atualizadoEm = data.atualizado_em || null;
    this.disponivel = true;
    this._emitir();
  },

  async carregar() {
    if (this._carregado) return;
    this._carregado = true;
    try {
      this.aplicar(await api("/api/models"));
    } catch (err) {
      if (err.status === 404) this.disponivel = false;
      else toast(err.message || String(err), "error");
      this._emitir();
    }
  },

  async refresh() {
    this.aplicar(await api("/api/models/refresh", { method: "POST" }));
    toast(`${this.modelos.length} modelos · atualizado ${formatarData(this.atualizadoEm)}`, "success");
  },
};

function formatarData(iso) {
  if (!iso) return "agora";
  const data = new Date(iso);
  if (Number.isNaN(data.getTime())) return iso;
  return data.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function opcoesModelo(atual) {
  return ModelCatalog.lista(atual)
    .map((m) => `<option value="${escapeHtml(m.id)}"${m.id === atual ? " selected" : ""}>${escapeHtml(m.nome || m.id)}</option>`)
    .join("");
}

/** Troca o modelo ativo (override em settings) e faz Lab e Config refletirem. */
async function aplicarModelo(id) {
  if (!id || id === LabSession.geminiModel) return;
  await api("/api/config", { method: "PUT", body: { gemini_model: id } });
  await LabSession.carregarEfetivo();
  toast(`modelo aplicado: ${id}`, "success");
  if (abaAtual().tab === "config") await Config.load();
}

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
  async load(chaveInicial) {
    const data = await api("/api/prompts");
    this.slots = data.slots;
    if (chaveInicial && this.slots[chaveInicial] && chaveInicial !== this.key) {
      this.key = chaveInicial; // veio de #lab/prompts/<slot> (link "editar" da aba Tools)
      this.version = this.slots[chaveInicial].active;
    }
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
    history.replaceState(null, "", `#lab/prompts/${key}`); // link direto, sem recarregar a aba
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

// -------------------------------------------------------------------------- fichas (Tools/Config)
// Uma célula por campo: rótulo + badge de origem + "voltar ao padrão" (só quando há override)
// em cima, controle embaixo. As fichas são grades de 2 colunas; campo largo ocupa a linha toda.
function campoHtml(id, def, campo, path) {
  const limites = `${def.step ? ` step="${def.step}"` : ""}${def.min != null ? ` min="${def.min}"` : ""}${def.max != null ? ` max="${def.max}"` : ""}`;
  const controle =
    def.type === "switch"
      ? `<label class="switch"><input type="checkbox" id="${id}" ${campo.value ? "checked" : ""} /><span>${campo.value ? "ligado" : "desligado"}</span></label>`
      : def.type === "select"
        ? `<select id="${id}">${opcoesDoCampo(def, campo.value)}</select>`
        : `<input type="${def.type}" id="${id}" value="${escapeHtml(campo.value ?? "")}"${limites} />`;
  return `<div class="field-cell${def.wide ? " wide" : ""}">
      <div class="field-head">
        <span class="field-label">${escapeHtml(def.label)}</span>
        <span class="field-meta">${badgeOrigem(campo.origem)}${campo.origem === "override" ? botaoReset(path) : ""}</span>
      </div>
      ${controle}
      ${def.help ? `<span class="field-help">${escapeHtml(def.help)}</span>` : ""}
    </div>`;
}

/** Opções de um campo `select`; garante que o valor efetivo esteja na lista para não se perder ao salvar. */
function opcoesDoCampo(def, atual) {
  const opcoes = (def.opcoes || []).slice();
  if (atual != null && atual !== "" && !opcoes.includes(atual)) opcoes.unshift(String(atual));
  return opcoes.map((o) => `<option value="${escapeHtml(o)}"${o === atual ? " selected" : ""}>${escapeHtml(o)}</option>`).join("");
}

function botaoReset(path) {
  return `<button type="button" class="icon reset-btn" data-path="${escapeHtml(path)}" title="voltar ao padrão" aria-label="voltar ao padrão">${icon("reset")}</button>`;
}

/** Toggle visual: o texto ao lado acompanha o estado. */
function ligarSwitches(root) {
  root.querySelectorAll(".switch input").forEach((input) => {
    const texto = input.nextElementSibling;
    if (!texto) return;
    input.addEventListener("change", () => {
      texto.textContent = input.checked ? "ligado" : "desligado";
    });
  });
}

function lerCampo(id, def) {
  const campo = document.getElementById(id);
  if (def.type === "switch") return campo.checked;
  if (def.type === "number") return Number(campo.value);
  return campo.value;
}

// -------------------------------------------------------------------------- aba Tools (Integrações)
// Lista à esquerda (builtin + tools criadas no painel) e detalhe à direita. As tools novas moram
// em `/api/custom-tools`; enquanto essa rota não existir, a aba funciona só com as builtin.
// `handoff` só aparece quando `/api/effective` traz o grupo (o backend está criando nesta frente)
const TOOLS_BUILTIN = ["quote_client", "viacep", "handoff", "canal"];

const CAMPOS_HANDOFF = [
  { key: "auto_assumir", label: "Assumir a conversa automaticamente no handoff", type: "switch", wide: true },
  { key: "consultor_number", label: "WhatsApp do consultor (só dígitos com DDI)", type: "text" },
  { key: "studio_url", label: "URL do Studio (base do link no aviso)", type: "text" },
  { key: "webhook_url", label: "Webhook (POST JSON a cada handoff)", type: "text", wide: true },
  { key: "auto_devolver_apos_min", label: "Devolver ao agente após (min sem mensagem humana)", type: "number" },
];

// freios do canal WhatsApp — existem por causa do loop de 02/09 (23 respostas em 80 s)
const CAMPOS_CANAL = [
  { key: "max_respostas_por_minuto", label: "Máx. de respostas por minuto (por conversa)", type: "number" },
  { key: "debounce_s", label: "Debounce (s) — junta mensagens picadas; 0 desliga", type: "number", step: "0.5" },
];

const CAMPOS_VIACEP = [
  { key: "enabled", label: "Habilitado", type: "switch" },
  { key: "timeout_s", label: "Timeout (s)", type: "number", step: "0.1" },
  { key: "url", label: "URL", type: "text", wide: true },
];
const CAMPOS_POLICY = [
  { key: "max_turnos_sem_progresso", label: "Máx. turnos sem progresso", type: "number" },
  { key: "max_cep_tentativas", label: "Máx. tentativas de CEP", type: "number" },
  { key: "objecoes_ate_handoff", label: "Objeções até handoff", type: "number" },
  // chegam com o backend desta frente; até lá, `fichaTools` não os desenha
  { key: "plano_padrao", label: "Plano padrão quando o lead não escolhe", type: "select", opcoes: ["essencial", "completo", "premium"] },
  { key: "max_veiculos", label: "Máx. de carros por cotação", type: "number", min: 1, max: 5 },
];
const CAMPOS_RULES = [
  { key: "pre_validacao_local", label: "Pré-validação local", type: "switch", help: "valida idade, ano e CEP antes de chamar a API" },
];

// textos de Prompts que cada integração produz ou usa (chaves conferidas em config/prompts.json)
const SLOTS_DA_TOOL = {
  viacep: {
    chaves: ["presenter.confirm_cep", "diretiva.cep", "fallback.cep", "presenter.present.aviso_cep_ausente"],
    prefixos: [],
  },
  quote_client: {
    chaves: ["policy.txt_instabilidade", "policy.txt_aguarde", "policy.diretiva_pos_cotacao", "presenter.handoff.cotacao_indisponivel"],
    prefixos: ["presenter.present.", "presenter.cobertura.", "presenter.refuse"],
  },
  // `presenter.handoff.aviso_consultor` (o texto que vai para o consultor) entra pelo prefixo assim que existir
  handoff: { chaves: ["policy.txt_terminal_handoff"], prefixos: ["presenter.handoff."] },
};

const TIPOS_PARAM = ["string", "number", "integer", "boolean"];
const METODOS_HTTP = ["GET", "POST", "PUT", "PATCH", "DELETE"];

/** Ficha de um grupo de `tools.*` (viacep, policy, rules): grade de campos + Salvar. */
function fichaTools(grupo, titulo, campos, efetivo, recarregar) {
  const div = document.createElement("div");
  div.className = "card";
  // campo que o backend ainda não expõe em /api/effective simplesmente não aparece (sem erro)
  const presentes = campos.filter((def) => efetivo[grupo] && efetivo[grupo][def.key] !== undefined);
  const celulas = presentes.map((def) => campoHtml(`tool-${grupo}-${def.key}`, def, efetivo[grupo][def.key], `${grupo}/${def.key}`)).join("");
  div.innerHTML = `<h3>${escapeHtml(titulo)}</h3>
    <div class="card-grid">${celulas}</div>
    <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>`;
  ligarSwitches(div);
  div.querySelectorAll(".reset-btn").forEach((btn) => {
    btn.addEventListener(
      "click",
      withLoading(btn, async () => {
        await api(`/api/tools/${btn.dataset.path}`, { method: "DELETE" });
        await recarregar();
        toast("aplicado", "success");
      })
    );
  });
  const salvar = div.querySelector(".save-btn");
  salvar.addEventListener(
    "click",
    withLoading(salvar, async () => {
      const patch = {};
      for (const def of presentes) {
        const novo = lerCampo(`tool-${grupo}-${def.key}`, def);
        if (novo !== efetivo[grupo][def.key].value) patch[def.key] = novo;
      }
      if (Object.keys(patch).length === 0) {
        toast("nada para salvar");
        return;
      }
      await api("/api/tools", { method: "PUT", body: { [grupo]: patch } });
      await recarregar();
      toast("aplicado", "success");
    })
  );
  return div;
}

function previaSlot(texto) {
  return truncate(String(texto || "").replace(/\s+/g, " ").trim(), 220);
}

const Tools = {
  effective: null,
  custom: {}, // tools salvas no backend
  rascunho: null, // tool criada no painel e ainda não salva
  suporte: true, // `/api/custom-tools` existe?
  envVars: [],
  sel: null,

  // ---- dados
  async load(item) {
    const data = await api("/api/effective");
    this.effective = data.tools;
    await this.carregarCustom();
    await this.garantirSlots();
    if (item && this.existe(item)) this.sel = item;
    if (!this.existe(this.sel)) this.sel = "quote_client";
    this.renderLista();
    this.renderDetalhe();
  },

  /** Builtin que o backend realmente expõe (o grupo `handoff` pode não existir ainda). */
  builtins() {
    return TOOLS_BUILTIN.filter((id) => this.effective && this.effective[id]);
  },

  existe(id) {
    return Boolean(id && (this.builtins().includes(id) || this.custom[id] || (this.rascunho && this.rascunho.nome === id)));
  },

  tool(id) {
    if (this.custom[id]) return this.custom[id];
    if (this.rascunho && this.rascunho.nome === id) return this.rascunho;
    return null;
  },

  async carregarCustom() {
    try {
      const data = await api("/api/custom-tools");
      this.custom = data.tools || {};
      this.suporte = true;
      if (!this.envVars.length) await this.carregarEnv();
    } catch (err) {
      if (err.status !== 404) toast(err.message || String(err), "error");
      this.suporte = false;
      this.custom = {};
    }
    el("tl-nova").hidden = !this.suporte;
    el("tl-sem-suporte").hidden = this.suporte;
  },

  async carregarEnv() {
    try {
      const data = await api("/api/custom-tools/env");
      this.envVars = data.vars || [];
    } catch {
      this.envVars = []; // sugestão de ${env:X} é conveniência, não requisito
    }
  },

  /** O bloco "Instruções e textos" precisa dos slots; a aba Prompts pode não ter carregado ainda. */
  async garantirSlots() {
    if (Object.keys(Prompts.slots).length) return;
    try {
      const data = await api("/api/prompts");
      Prompts.slots = data.slots;
    } catch {
      /* sem prévia dos textos; o resto da aba continua */
    }
  },

  // ---- lista
  renderLista() {
    const box = el("tl-itens");
    box.innerHTML = "";
    for (const id of this.builtins()) box.appendChild(this.itemLista(id, [badgeEl("builtin")], null));
    for (const [nome, tool] of Object.entries(this.custom)) {
      box.appendChild(this.itemLista(nome, [badgeEl(tool.tipo, `tl-tipo-${tool.tipo}`)], Boolean(tool.enabled)));
    }
    if (this.rascunho) {
      box.appendChild(this.itemLista(this.rascunho.nome, [badgeEl(this.rascunho.tipo, `tl-tipo-${this.rascunho.tipo}`), badgeEl("não salva")], false));
    }
  },

  itemLista(id, badges, enabled) {
    const div = document.createElement("div");
    div.className = "tl-item" + (id === this.sel ? " selected" : "");
    const ponto = document.createElement("span");
    ponto.className = "tl-ponto" + (enabled ? " on" : "") + (enabled === null ? " builtin" : "");
    ponto.title = enabled === null ? "integração nativa" : enabled ? "habilitada" : "desabilitada";
    const nome = document.createElement("span");
    nome.className = "tl-item-nome";
    nome.textContent = id;
    div.append(ponto, nome, ...badges);
    div.addEventListener("click", () => this.selecionar(id));
    return div;
  },

  selecionar(id) {
    if (!this.existe(id)) return;
    this.sel = id;
    history.replaceState(null, "", `#lab/tools/${id}`); // link direto sem recarregar a aba
    this.renderLista();
    this.renderDetalhe();
  },

  // ---- detalhe
  renderDetalhe() {
    const box = el("tl-detalhe");
    box.innerHTML = "";
    if (this.sel === "quote_client") {
      const nota = document.createElement("p");
      nota.className = "notice";
      nota.textContent = "guard_price não é configurável por regra.";
      box.append(nota, this.cardQuoteClient(), this.blocoSlots("quote_client"));
      return;
    }
    if (this.sel === "viacep") {
      box.append(fichaTools("viacep", "viacep", CAMPOS_VIACEP, this.effective, () => this.load(this.sel)), this.blocoSlots("viacep"));
      return;
    }
    if (this.sel === "handoff") {
      box.append(this.cardHandoff(), this.blocoSlots("handoff"));
      return;
    }
    if (this.sel === "canal") {
      box.append(fichaTools("canal", "canal", CAMPOS_CANAL, this.effective, () => this.load(this.sel)));
      return;
    }
    const tool = this.tool(this.sel);
    if (tool) box.appendChild(this.formularioTool(tool));
  },

  blocoSlots(toolId) {
    const cfg = SLOTS_DA_TOOL[toolId] || { chaves: [], prefixos: [] };
    const chaves = Object.keys(Prompts.slots)
      .filter((k) => cfg.chaves.includes(k) || cfg.prefixos.some((p) => k.startsWith(p)))
      .sort();
    const card = document.createElement("div");
    card.className = "card";
    const titulo = document.createElement("h3");
    titulo.textContent = "Instruções e textos";
    const ajuda = document.createElement("p");
    ajuda.className = "field-help";
    ajuda.textContent = chaves.length
      ? "O que esta integração escreve para o lead. Editar abre o slot na aba Prompts."
      : "Nenhum texto de Prompts ligado a esta integração.";
    card.append(titulo, ajuda);
    for (const chave of chaves) {
      const slot = Prompts.slots[chave];
      const versao = slot.versions[slot.active] || { text: "" };
      const linha = document.createElement("div");
      linha.className = "tl-slot";
      const topo = document.createElement("div");
      topo.className = "tl-slot-topo";
      const label = document.createElement("span");
      label.className = "tl-slot-label";
      label.textContent = slot.label;
      const key = document.createElement("span");
      key.className = "dd-item-key";
      key.textContent = chave;
      const link = document.createElement("a");
      link.className = "link tl-slot-link";
      link.href = `#lab/prompts/${chave}`;
      link.textContent = "editar";
      topo.append(label, key, badgeEl(slot.active, slot.active === "default" ? "" : "badge-ativa"), link);
      const previa = document.createElement("p");
      previa.className = "tl-slot-previa";
      previa.textContent = previaSlot(versao.text);
      linha.append(topo, previa);
      card.appendChild(linha);
    }
    return card;
  },

  cardQuoteClient() {
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
      <div class="card-grid">
        <div class="field-cell wide">
          <div class="field-head">
            <span class="field-label">base_url</span>
            <span class="field-meta">${badgeOrigem(g.base_url.origem)}${g.base_url.origem === "override" ? botaoReset("quote_client/base_url") : ""}</span>
          </div>
          <div class="field-dupla">
            <select id="tool-qc-base_url-select"><option value="">endpoint conhecido…</option>${options}</select>
            <input type="text" id="tool-quote_client-base_url" value="${escapeHtml(g.base_url.value ?? "")}" aria-label="base_url" />
          </div>
        </div>
        <div class="field-cell wide">
          <div class="field-head">
            <span class="field-label">endpoints</span>
            <span class="field-meta">${badgeOrigem(g.endpoints.origem)}${g.endpoints.origem === "override" ? botaoReset("quote_client/endpoints") : ""}</span>
          </div>
          <div id="tool-endpoints-rows"></div>
          <button type="button" id="tool-endpoints-add" class="small">${icon("plus")} endpoint</button>
        </div>
        ${campoHtml("tool-quote_client-timeout_s", { key: "timeout_s", label: "timeout_s", type: "number", step: "0.1" }, g.timeout_s, "quote_client/timeout_s")}
        ${campoHtml("tool-quote_client-max_attempts", { key: "max_attempts", label: "max_attempts", type: "number" }, g.max_attempts, "quote_client/max_attempts")}
        ${campoHtml("tool-quote_client-budget_s", { key: "budget_s", label: "budget_s", type: "number", step: "0.5" }, g.budget_s, "quote_client/budget_s")}
        ${campoHtml("tool-quote_client-backoff_base_s", { key: "backoff_base_s", label: "backoff_base_s", type: "number", step: "0.1" }, g.backoff_base_s, "quote_client/backoff_base_s")}
      </div>
      <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>
    `;
    const linhas = div.querySelector("#tool-endpoints-rows");
    for (const [label, url] of Object.entries(endpoints)) this.linhaEndpoint(linhas, label, url);
    div.querySelector("#tool-endpoints-add").addEventListener("click", () => this.linhaEndpoint(linhas));
    div.querySelector("#tool-qc-base_url-select").addEventListener("change", (e) => {
      if (e.target.value) div.querySelector("#tool-quote_client-base_url").value = e.target.value;
    });
    div.querySelectorAll(".reset-btn").forEach((btn) => {
      btn.addEventListener(
        "click",
        withLoading(btn, async () => {
          await api(`/api/tools/${btn.dataset.path}`, { method: "DELETE" });
          await this.load(this.sel);
          toast("aplicado", "success");
        })
      );
    });

    const salvar = div.querySelector(".save-btn");
    salvar.addEventListener(
      "click",
      withLoading(salvar, async () => {
        const patch = {};
        const baseUrl = div.querySelector("#tool-quote_client-base_url").value;
        if (baseUrl !== g.base_url.value) patch.base_url = baseUrl;
        const novosEndpoints = {};
        linhas.querySelectorAll(".endpoint-row").forEach((row) => {
          const label = row.querySelector(".ep-label").value.trim();
          const url = row.querySelector(".ep-url").value.trim();
          if (label && url) novosEndpoints[label] = url;
        });
        if (JSON.stringify(novosEndpoints) !== JSON.stringify(endpoints)) patch.endpoints = novosEndpoints;
        for (const key of ["timeout_s", "max_attempts", "budget_s", "backoff_base_s"]) {
          const novo = Number(el(`tool-quote_client-${key}`).value);
          if (novo !== g[key].value) patch[key] = novo;
        }
        if (Object.keys(patch).length === 0) {
          toast("nada para salvar");
          return;
        }
        await api("/api/tools", { method: "PUT", body: { quote_client: patch } });
        await this.load(this.sel);
        toast("aplicado", "success");
      })
    );
    return div;
  },

  cardHandoff() {
    const g = this.effective.handoff;
    const div = document.createElement("div");
    div.className = "card";
    const headers = g.webhook_headers ? g.webhook_headers.value || {} : {};
    const temEnv = this.envVars.length > 0;
    div.innerHTML = `
      <h3>handoff</h3>
      <p class="field-help">o que acontece quando a conversa passa para um humano: assumir no painel, avisar o consultor e chamar o CRM.</p>
      <div class="card-grid">
        ${CAMPOS_HANDOFF.filter((def) => g[def.key] !== undefined)
          .map((def) => campoHtml(`tool-handoff-${def.key}`, def, g[def.key], `handoff/${def.key}`))
          .join("")}
        ${
          g.webhook_headers === undefined
            ? ""
            : `<div class="field-cell wide">
                 <div class="field-head">
                   <span class="field-label">Headers do webhook</span>
                   <span class="field-meta">${badgeOrigem(g.webhook_headers.origem)}${g.webhook_headers.origem === "override" ? botaoReset("handoff/webhook_headers") : ""}</span>
                 </div>
                 <div class="tl-pares" id="tl-handoff-headers"></div>
                 <button type="button" class="small" id="tl-handoff-header-add">${icon("plus")} header</button>
                 ${
                   temEnv
                     ? `<div class="field-dupla" style="margin-top:8px">
                          <select id="tl-env">
                            <option value="">inserir \${env:…} no campo em foco</option>
                            ${this.envVars.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("")}
                          </select>
                        </div>
                        <span class="field-help">o valor fica no ambiente; só o nome é gravado em config/</span>`
                     : ""
                 }
               </div>`
        }
      </div>
      <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>
    `;
    ligarSwitches(div);
    const linhas = div.querySelector("#tl-handoff-headers");
    if (linhas) {
      for (const [k, v] of Object.entries(headers)) this.linhaPar(linhas, k, v);
      div.querySelector("#tl-handoff-header-add").addEventListener("click", () => this.linhaPar(linhas));
      this.ligarEnv(div);
    }
    div.querySelectorAll(".reset-btn").forEach((btn) => {
      btn.addEventListener(
        "click",
        withLoading(btn, async () => {
          await api(`/api/tools/${btn.dataset.path}`, { method: "DELETE" });
          await this.load(this.sel);
          toast("aplicado", "success");
        })
      );
    });

    const salvar = div.querySelector(".save-btn");
    salvar.addEventListener(
      "click",
      withLoading(salvar, async () => {
        const patch = {};
        for (const def of CAMPOS_HANDOFF) {
          if (g[def.key] === undefined) continue;
          const novo = lerCampo(`tool-handoff-${def.key}`, def);
          if (novo !== g[def.key].value) patch[def.key] = novo;
        }
        if (linhas) {
          const novos = {};
          linhas.querySelectorAll(".tl-par-row").forEach((row) => {
            const chave = row.querySelector(".k").value.trim();
            if (chave) novos[chave] = row.querySelector(".v").value;
          });
          if (JSON.stringify(novos) !== JSON.stringify(headers)) patch.webhook_headers = novos;
        }
        if (Object.keys(patch).length === 0) {
          toast("nada para salvar");
          return;
        }
        await api("/api/tools", { method: "PUT", body: { handoff: patch } });
        await this.load(this.sel);
        toast("aplicado", "success");
      })
    );
    return div;
  },

  linhaEndpoint(container, label = "", url = "") {
    const row = document.createElement("div");
    row.className = "endpoint-row";
    row.innerHTML = `<input type="text" class="ep-label" placeholder="rótulo" value="${escapeHtml(label)}" />
      <input type="text" class="ep-url" placeholder="http://..." value="${escapeHtml(url)}" />
      <button type="button" class="icon ep-remove" title="remover endpoint" aria-label="remover endpoint">${icon("remove")}</button>`;
    row.querySelector(".ep-remove").addEventListener("click", () => row.remove());
    container.appendChild(row);
  },

  // ---- formulário de uma tool nova
  formularioTool(tool) {
    const novo = !this.custom[tool.nome];
    const card = document.createElement("div");
    card.className = "card tl-form";
    const http = tool.http || { metodo: "GET", url: "", headers: {}, query: {}, body: null, resposta: "json" };
    const sql = tool.sql || { conexao: "", query: "", max_linhas: 20 };
    card.innerHTML = `
      <div class="tl-form-head">
        <h3>${escapeHtml(tool.nome)}</h3>
        <span class="badge tl-tipo-${escapeHtml(tool.tipo)}">${escapeHtml(tool.tipo)}</span>
        ${novo ? '<span class="badge">não salva</span>' : ""}
        <label class="switch"><input type="checkbox" id="tl-enabled" ${tool.enabled ? "checked" : ""} /><span>${tool.enabled ? "ligada" : "desligada"}</span></label>
        <div class="tl-form-acoes">
          <button type="button" class="danger" id="tl-apagar">${novo ? "Descartar" : "Apagar"}</button>
          <button type="button" class="primary" id="tl-salvar">Salvar</button>
        </div>
      </div>
      <div class="card-grid">
        <div class="field-cell wide">
          <div class="field-head"><span class="field-label">Descrição</span></div>
          <textarea id="tl-descricao" rows="2" placeholder="quando o modelo deve chamar esta tool">${escapeHtml(tool.descricao || "")}</textarea>
          <span class="field-help">é o que o modelo lê para decidir se usa a tool</span>
        </div>
        <div class="field-cell wide">
          <div class="field-head"><span class="field-label">Instruções (opcional)</span></div>
          <textarea id="tl-instrucoes" rows="2" placeholder="regras que entram no prompt quando esta tool está ativa">${escapeHtml(tool.instrucoes || "")}</textarea>
        </div>
        <div class="field-cell wide">
          <div class="field-head"><span class="field-label">Parâmetros</span></div>
          <div class="tl-params" id="tl-params"></div>
          <button type="button" class="small" id="tl-param-add">${icon("plus")} parâmetro</button>
        </div>
        ${
          tool.tipo === "http"
            ? `<div class="field-cell">
                 <div class="field-head"><span class="field-label">Método</span></div>
                 <select id="tl-metodo">${METODOS_HTTP.map((m) => `<option${m === http.metodo ? " selected" : ""}>${m}</option>`).join("")}</select>
               </div>
               <div class="field-cell">
                 <div class="field-head"><span class="field-label">Resposta</span></div>
                 <select id="tl-resposta">
                   <option value="json"${http.resposta === "json" ? " selected" : ""}>json</option>
                   <option value="texto"${http.resposta === "texto" ? " selected" : ""}>texto</option>
                 </select>
               </div>
               <div class="field-cell wide">
                 <div class="field-head"><span class="field-label">URL</span></div>
                 <input type="text" id="tl-url" value="${escapeHtml(http.url || "")}" placeholder="https://api.exemplo.com/recurso/{param}" />
                 <span class="field-help">use {nome_do_parametro} para interpolar</span>
               </div>
               <div class="field-cell wide">
                 <div class="field-head"><span class="field-label">Headers</span></div>
                 <div class="tl-pares" id="tl-headers"></div>
                 <button type="button" class="small" id="tl-header-add">${icon("plus")} header</button>
               </div>
               <div class="field-cell wide">
                 <div class="field-head"><span class="field-label">Query</span></div>
                 <div class="tl-pares" id="tl-query"></div>
                 <button type="button" class="small" id="tl-query-add">${icon("plus")} parâmetro de query</button>
               </div>
               <div class="field-cell wide">
                 <div class="field-head"><span class="field-label">Body (JSON, opcional)</span></div>
                 <textarea id="tl-body" class="mono" rows="3" placeholder='{"cpf": "{cpf}"}'>${escapeHtml(http.body === null || http.body === undefined ? "" : typeof http.body === "string" ? http.body : JSON.stringify(http.body, null, 2))}</textarea>
               </div>`
            : `<div class="field-cell wide">
                 <div class="field-head"><span class="field-label">Conexão</span></div>
                 <input type="text" id="tl-conexao" value="${escapeHtml(sql.conexao || "")}" placeholder="data/apolices.db  ou  \${env:APOLICES_DB}" />
               </div>
               <div class="field-cell wide">
                 <div class="field-head"><span class="field-label">Query (somente leitura)</span></div>
                 <textarea id="tl-query-sql" class="mono" rows="4" placeholder="SELECT numero, status FROM apolices WHERE cpf = :cpf LIMIT 20">${escapeHtml(sql.query || "")}</textarea>
                 <span class="field-help">um SELECT/WITH, sem ponto e vírgula; parâmetros como :nome</span>
               </div>
               <div class="field-cell">
                 <div class="field-head"><span class="field-label">Máx. linhas</span></div>
                 <input type="number" id="tl-max-linhas" value="${escapeHtml(sql.max_linhas ?? 20)}" />
               </div>`
        }
        <div class="field-cell">
          <div class="field-head"><span class="field-label">Timeout (s)</span></div>
          <input type="number" id="tl-timeout" step="0.5" value="${escapeHtml(tool.timeout_s ?? 5)}" />
        </div>
        <div class="field-cell">
          <div class="field-head"><span class="field-label">Máx. caracteres do resultado</span></div>
          <input type="number" id="tl-max-chars" value="${escapeHtml(tool.max_chars ?? 2000)}" />
        </div>
        <div class="field-cell wide">
          <div class="field-head"><span class="field-label">Variáveis de ambiente</span></div>
          <div class="field-dupla">
            <select id="tl-env">
              <option value="">inserir \${env:…} no campo em foco</option>
              ${this.envVars.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("")}
            </select>
          </div>
          <span class="field-help">${this.envVars.length ? "o valor fica no ambiente; só o nome é gravado em config/" : "nenhuma variável disponível (o backend informa só os nomes)"}</span>
        </div>
      </div>

      <div class="tl-testar">
        <h4>Testar</h4>
        <p class="field-help">roda a tool salva, sem passar pelo modelo e sem gravar evento na conversa.</p>
        <div class="tl-testar-args" id="tl-testar-args"></div>
        <div class="card-actions">
          <button type="button" class="primary" id="tl-testar-btn"${novo ? " disabled" : ""}>Testar</button>
        </div>
        <pre class="tl-testar-saida" id="tl-testar-saida" hidden></pre>
      </div>
    `;

    ligarSwitches(card);
    const params = card.querySelector("#tl-params");
    for (const [nome, def] of Object.entries(tool.parametros || {})) this.linhaParam(params, nome, def);
    card.querySelector("#tl-param-add").addEventListener("click", () => this.linhaParam(params));
    if (tool.tipo === "http") {
      const headers = card.querySelector("#tl-headers");
      const query = card.querySelector("#tl-query");
      for (const [k, v] of Object.entries(http.headers || {})) this.linhaPar(headers, k, v);
      for (const [k, v] of Object.entries(http.query || {})) this.linhaPar(query, k, v);
      card.querySelector("#tl-header-add").addEventListener("click", () => this.linhaPar(headers));
      card.querySelector("#tl-query-add").addEventListener("click", () => this.linhaPar(query));
    }
    this.ligarEnv(card);
    this.renderArgsTeste(card, tool);

    const salvar = card.querySelector("#tl-salvar");
    salvar.addEventListener("click", withLoading(salvar, () => this.salvarTool(tool)));
    const apagar = card.querySelector("#tl-apagar");
    apagar.addEventListener("click", withLoading(apagar, () => this.apagarTool(tool)));
    const testar = card.querySelector("#tl-testar-btn");
    testar.addEventListener("click", withLoading(testar, () => this.testarTool(tool)));
    return card;
  },

  linhaParam(container, nome = "", def = {}) {
    const row = document.createElement("div");
    row.className = "tl-param-row";
    row.innerHTML = `
      <input type="text" class="p-nome" placeholder="nome" value="${escapeHtml(nome)}" />
      <select class="p-tipo">${TIPOS_PARAM.map((t) => `<option${t === (def.tipo || "string") ? " selected" : ""}>${t}</option>`).join("")}</select>
      <input type="text" class="p-desc" placeholder="descrição para o modelo" value="${escapeHtml(def.descricao || "")}" />
      <label class="switch"><input type="checkbox" class="p-obr" ${def.obrigatorio ? "checked" : ""} /><span>${def.obrigatorio ? "obrigatório" : "opcional"}</span></label>
      <button type="button" class="icon p-rm" title="remover parâmetro" aria-label="remover parâmetro">${icon("remove")}</button>`;
    ligarSwitches(row);
    row.querySelector(".p-obr").addEventListener("change", (e) => {
      e.target.nextElementSibling.textContent = e.target.checked ? "obrigatório" : "opcional";
    });
    row.querySelector(".p-rm").addEventListener("click", () => row.remove());
    container.appendChild(row);
  },

  linhaPar(container, chave = "", valor = "") {
    const row = document.createElement("div");
    row.className = "tl-par-row";
    row.innerHTML = `<input type="text" class="k" placeholder="chave" value="${escapeHtml(chave)}" />
      <input type="text" class="v" placeholder="valor" value="${escapeHtml(valor)}" />
      <button type="button" class="icon rm" title="remover" aria-label="remover">${icon("remove")}</button>`;
    row.querySelector(".rm").addEventListener("click", () => row.remove());
    container.appendChild(row);
  },

  /** O select de ${env:X} escreve no último campo de texto que teve foco no formulário. */
  ligarEnv(card) {
    let ultimo = null;
    card.addEventListener("focusin", (ev) => {
      if (ev.target.matches('input[type="text"], textarea')) ultimo = ev.target;
    });
    const select = card.querySelector("#tl-env");
    if (!select) return; // sem variáveis de ambiente conhecidas, o seletor nem é desenhado
    select.addEventListener("change", () => {
      const nome = select.value;
      select.value = "";
      if (!nome) return;
      if (!ultimo) {
        toast("clique antes no campo onde a variável entra", "error");
        return;
      }
      const marca = `\${env:${nome}}`;
      const ini = ultimo.selectionStart ?? ultimo.value.length;
      const fim = ultimo.selectionEnd ?? ini;
      ultimo.value = ultimo.value.slice(0, ini) + marca + ultimo.value.slice(fim);
      ultimo.focus();
      ultimo.selectionStart = ultimo.selectionEnd = ini + marca.length;
    });
  },

  renderArgsTeste(card, tool) {
    const box = card.querySelector("#tl-testar-args");
    box.innerHTML = "";
    const params = Object.entries(tool.parametros || {});
    if (!params.length) {
      box.innerHTML = '<p class="muted">Sem parâmetros.</p>';
      return;
    }
    for (const [nome, def] of params) {
      const linha = document.createElement("label");
      linha.className = "tl-arg";
      const rotulo = document.createElement("span");
      rotulo.className = "field-label";
      rotulo.textContent = nome + (def.obrigatorio ? " *" : "");
      const campo = document.createElement("input");
      campo.className = "tl-arg-campo";
      campo.dataset.nome = nome;
      campo.dataset.tipo = def.tipo || "string";
      campo.type = def.tipo === "boolean" ? "checkbox" : def.tipo === "number" || def.tipo === "integer" ? "number" : "text";
      campo.placeholder = def.descricao || "";
      linha.append(rotulo, campo);
      box.appendChild(linha);
    }
  },

  // ---- ações
  coletar(tool) {
    const parametros = {};
    for (const row of document.querySelectorAll("#tl-params .tl-param-row")) {
      const nome = row.querySelector(".p-nome").value.trim();
      if (!nome) continue;
      parametros[nome] = {
        tipo: row.querySelector(".p-tipo").value,
        descricao: row.querySelector(".p-desc").value,
        obrigatorio: row.querySelector(".p-obr").checked,
      };
    }
    const pares = (seletor) => {
      const saida = {};
      for (const row of document.querySelectorAll(`${seletor} .tl-par-row`)) {
        const chave = row.querySelector(".k").value.trim();
        if (chave) saida[chave] = row.querySelector(".v").value;
      }
      return saida;
    };
    const novo = {
      ...tool,
      nome: tool.nome,
      tipo: tool.tipo,
      enabled: el("tl-enabled").checked,
      descricao: el("tl-descricao").value,
      instrucoes: el("tl-instrucoes").value,
      parametros,
      timeout_s: Number(el("tl-timeout").value),
      max_chars: Number(el("tl-max-chars").value),
    };
    if (tool.tipo === "http") {
      const body = el("tl-body").value.trim();
      novo.http = {
        metodo: el("tl-metodo").value,
        url: el("tl-url").value.trim(),
        headers: pares("#tl-headers"),
        query: pares("#tl-query"),
        body: body || null,
        resposta: el("tl-resposta").value,
      };
      novo.sql = null;
    } else {
      novo.sql = {
        conexao: el("tl-conexao").value.trim(),
        query: el("tl-query-sql").value.trim(),
        max_linhas: Number(el("tl-max-linhas").value),
      };
      novo.http = null;
    }
    return novo;
  },

  async salvarTool(tool) {
    const corpo = this.coletar(tool);
    const salva = await api(`/api/custom-tools/${encodeURIComponent(tool.nome)}`, { method: "PUT", body: corpo });
    this.custom[tool.nome] = salva && salva.nome ? salva : corpo;
    if (this.rascunho && this.rascunho.nome === tool.nome) this.rascunho = null;
    toast("tool salva", "success");
    await this.load(tool.nome);
  },

  async apagarTool(tool) {
    if (this.rascunho && this.rascunho.nome === tool.nome) {
      this.rascunho = null;
      this.sel = "quote_client";
      this.renderLista();
      this.renderDetalhe();
      return;
    }
    await api(`/api/custom-tools/${encodeURIComponent(tool.nome)}`, { method: "DELETE" });
    delete this.custom[tool.nome];
    this.sel = "quote_client";
    history.replaceState(null, "", "#lab/tools/quote_client"); // o hash não pode apontar para o que não existe mais
    toast("tool apagada", "success");
    await this.load(this.sel);
  },

  async testarTool(tool) {
    const args = {};
    for (const campo of document.querySelectorAll("#tl-testar-args .tl-arg-campo")) {
      const tipo = campo.dataset.tipo;
      if (tipo === "boolean") args[campo.dataset.nome] = campo.checked;
      else if (campo.value !== "") args[campo.dataset.nome] = tipo === "number" || tipo === "integer" ? Number(campo.value) : campo.value;
    }
    const saida = el("tl-testar-saida");
    const data = await api(`/api/custom-tools/${encodeURIComponent(tool.nome)}/testar`, { method: "POST", body: { args } });
    saida.hidden = false;
    saida.textContent = `${data.ok ? "ok" : "erro"} · ${data.latency_ms} ms\n\n${data.erro ? `erro: ${data.erro}\n\n` : ""}${data.resultado || ""}`;
  },

  // ---- nova tool (popover)
  init() {
    el("tl-nova-btn").addEventListener("click", () =>
      alternarDd(el("tl-nova-menu"), el("tl-nova-btn"), () => {
        el("tl-nova-nome").value = "";
        el("tl-nova-nome").focus();
      })
    );
    el("tl-nova-cancel").addEventListener("click", () => fecharDd());
    el("tl-nova-criar").addEventListener("click", () => this.criarTool());
  },

  criarTool() {
    const nome = el("tl-nova-nome").value.trim();
    if (!/^[a-z][a-z0-9_]{2,40}$/.test(nome)) {
      toast("nome inválido: minúsculas, números e _ , começando por letra", "error");
      return;
    }
    if (this.existe(nome)) {
      toast(`já existe uma tool chamada ${nome}`, "error");
      return;
    }
    const tipo = el("tl-nova-tipo").value;
    // nasce como rascunho local e desligada: nada vai para config/ antes do primeiro Salvar
    this.rascunho = {
      nome,
      tipo,
      enabled: false,
      descricao: "",
      instrucoes: "",
      parametros: {},
      timeout_s: 5,
      max_chars: 2000,
      http: tipo === "http" ? { metodo: "GET", url: "", headers: {}, query: {}, body: null, resposta: "json" } : null,
      sql: tipo === "sql" ? { conexao: "", query: "", max_linhas: 20 } : null,
    };
    fecharDd();
    this.selecionar(nome);
  },
};

// -------------------------------------------------------------------------- aba Config
const Config = {
  effective: null,
  tools: null,
  campos: [
    { key: "gemini_model", label: "Modelo Gemini", type: "select", wide: true },
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
    { key: "agent_db_path", label: "Caminho do banco do agente", type: "text", wide: true },
  ],

  async load() {
    const data = await api("/api/effective");
    this.effective = data.settings;
    this.tools = data.tools; // policy e rules moram aqui desde a v3
    this.render();
  },

  /** Modelo do Gemini: select do catálogo + "Atualizar modelos" (consulta a API do Google). */
  celulaModelo(def, campo) {
    const aviso = ModelCatalog.disponivel
      ? ""
      : '<span class="field-help">catálogo indisponível nesta versão do backend — mostrando só o modelo em uso</span>';
    return `<div class="field-cell wide">
      <div class="field-head">
        <span class="field-label">${escapeHtml(def.label)}</span>
        <span class="field-meta">${badgeOrigem(campo.origem)}${campo.origem === "override" ? botaoReset(def.key) : ""}</span>
      </div>
      <div class="field-dupla">
        <select id="cfg-${def.key}">${opcoesModelo(campo.value)}</select>
        <button type="button" class="cfg-modelos-refresh" ${ModelCatalog.disponivel ? "" : 'disabled title="rota /api/models ainda não existe neste backend"'}>${icon("reset")} Atualizar modelos</button>
      </div>
      ${aviso}
    </div>`;
  },

  ligarRefreshModelos(card) {
    const btn = card.querySelector(".cfg-modelos-refresh");
    if (!btn) return;
    btn.addEventListener("click", withLoading(btn, () => ModelCatalog.refresh()));
  },

  render() {
    const card = el("config-card");
    const celulas = this.campos
      .map((def) => (def.type === "select" ? this.celulaModelo(def, this.effective[def.key]) : campoHtml(`cfg-${def.key}`, def, this.effective[def.key], def.key)))
      .join("");
    card.innerHTML = `<h3>settings</h3>
      <div class="card-grid">${celulas}</div>
      <div class="card-actions"><button type="button" class="primary save-btn">Salvar</button></div>`;
    ligarSwitches(card);
    this.ligarRefreshModelos(card);

    card.querySelectorAll(".reset-btn").forEach((btn) => {
      btn.addEventListener(
        "click",
        withLoading(btn, async () => {
          await api(`/api/config/${btn.dataset.path}`, { method: "DELETE" });
          await this.load();
          toast("aplicado", "success");
        })
      );
    });

    const btn = card.querySelector(".save-btn");
    btn.addEventListener(
      "click",
      withLoading(btn, async () => {
        const patch = {};
        for (const def of this.campos) {
          const novo = lerCampo(`cfg-${def.key}`, def);
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

    this.renderFichasTools();
  },

  /** `policy` e `rules` saíram de Tools; a API continua sendo `PUT /api/tools`. */
  renderFichasTools() {
    const box = el("config-tools-cards");
    box.innerHTML = "";
    if (!this.tools) return;
    box.appendChild(fichaTools("policy", "policy", CAMPOS_POLICY, this.tools, () => this.load()));
    box.appendChild(fichaTools("rules", "rules", CAMPOS_RULES, this.tools, () => this.load()));
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
    case "handoff_notice":
      return `handoff_notice ${d.canal}: ${d.status}${d.destino ? ` · ${d.destino}` : ""}`;
    case "tool_call":
      return `tool_call ${d.tool}: ${d.status}${d.latency_ms != null ? ` · ${d.latency_ms} ms` : ""}`;
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
// UMA sessão por aba do navegador, usada pelas duas telas que conversam com o agente (aba Lab e
// "Testar prompt"). O id fica no `sessionStorage`: um F5 retoma a MESMA sessão em vez de abrir
// outra e deixar a anterior órfã no servidor. Uma única conexão SSE, com fan-out para os
// assinantes — as duas telas veem os mesmos eventos, na mesma ordem.
const SESSAO_KEY = "studio.lab.sessao";

const LabSession = {
  id: null,
  api: "", // URL de cotação EFETIVA da sessão (a que o backend devolveu)
  es: null,
  endpoints: {}, // rótulo -> url (de /api/effective)
  geminiModel: "",
  apiEscolhida: "", // "" = deixa o agente usar a base_url dele
  mensagens: [], // {lado: "lead"|"agent", texto, source?, turno?}
  turno: null, // message_id do inbound corrente (etiqueta as mensagens do turno)
  ouvintes: new Set(),
  _criando: null,
  _retomando: null,

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

  // ---- ciclo de vida
  /** Boot: tenta retomar a sessão desta aba antes de qualquer coisa criar uma nova. */
  iniciar() {
    this._retomando = this._retomar()
      .catch(() => null)
      .finally(() => {
        this._retomando = null;
      });
    return this._retomando;
  },

  async _retomar() {
    const salvo = lerSessaoSalva();
    if (!salvo) return null;
    let estado;
    try {
      estado = await api(`/api/lab/sessions/${encodeURIComponent(salvo.id)}/state`);
    } catch {
      esquecerSessaoSalva(); // servidor reiniciou ou sessão encerrada: começa do zero
      return null;
    }
    this.id = salvo.id;
    this.api = salvo.api || "";
    this.mensagens = [];
    this.turno = null;
    this._emitir({ tipo: "sessao" });
    this._emitir({ tipo: "estado", state: estado });
    this._abrirSse(); // o SSE reenvia o histórico: bolhas e eventos voltam sozinhos
    return this.id;
  },

  async garantirSessao() {
    if (this._retomando) await this._retomando;
    if (this.id) return this.id;
    return this.novaSessao();
  },

  async novaSessao() {
    if (this._criando) return this._criando; // duplo clique / duas telas pedindo junto
    this._criando = this._criar().finally(() => {
      this._criando = null;
    });
    return this._criando;
  },

  async _criar() {
    const anterior = this.id;
    this._fecharSse();
    this.id = null;
    const body = this.apiEscolhida ? { api: this.apiEscolhida } : {};
    let data;
    try {
      data = await api("/api/lab/sessions", { method: "POST", body });
    } catch (err) {
      this._emitir({ tipo: "indisponivel", mensagem: err.message });
      throw err;
    }
    if (anterior) api(`/api/lab/sessions/${encodeURIComponent(anterior)}`, { method: "DELETE" }).catch(() => {});
    this.id = data.id;
    this.api = data.api || "";
    this.mensagens = [];
    this.turno = null;
    guardarSessao(this.id, this.api);
    this._emitir({ tipo: "sessao" });
    this._abrirSse();
    return this.id;
  },

  /** A API de cotação é fixada na criação da sessão — trocá-la de verdade exige sessão nova. */
  async trocarApi(url) {
    const novo = (url || "").trim();
    if (novo === (this.id ? this.api : this.apiEscolhida)) {
      this.apiEscolhida = novo; // já é a URL em uso: nada a recriar
      return;
    }
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

  _abrirSse() {
    this.es = sse(`/api/lab/sessions/${this.id}/events`, (ev) => this._onEvento(ev));
  },

  _fecharSse() {
    if (this.es) {
      this.es.close();
      this.es = null;
    }
  },

  _onEvento(ev) {
    if (ev.event === "inbound") {
      this.turno = ev.message_id;
      this.mensagens.push({ lado: "lead", texto: ev.data.text || `(mídia: ${ev.data.media_type})`, turno: this.turno });
    } else if (ev.event === "outbound") {
      this.mensagens.push({ lado: "agent", texto: ev.data.text, source: ev.data.source, turno: this.turno });
    }
    this._emitir({ tipo: "evento", ev });
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

// `sessionStorage` pode lançar (modo privado, cookies bloqueados): nunca deixa o boot cair
function lerSessaoSalva() {
  try {
    const salvo = JSON.parse(sessionStorage.getItem(SESSAO_KEY) || "null");
    return salvo && salvo.id ? salvo : null;
  } catch {
    return null;
  }
}

function guardarSessao(id, apiUrl) {
  try {
    sessionStorage.setItem(SESSAO_KEY, JSON.stringify({ id, api: apiUrl }));
  } catch {
    /* sem storage: a sessão só não sobrevive ao reload */
  }
}

function esquecerSessaoSalva() {
  try {
    sessionStorage.removeItem(SESSAO_KEY);
  } catch {
    /* idem */
  }
}

// -------------------------------------------------------------------------- componente de chat
// Mesmo markup (`<template id="tpl-chat">`) e mesmo comportamento no Lab e no "Testar prompt":
// bolhas + barra de API/modelo + composer. As diferenças vêm por `opts`.
function criarChat(container, opts) {
  container.appendChild(el("tpl-chat").content.cloneNode(true));
  const q = (sel) => container.querySelector(sel);
  const scroll = q(".chat-scroll");
  const vazio = q(".empty-state");
  const aviso = q(".chat-unavailable");
  const input = q(".chat-input");
  const typing = q(".chat-typing");
  const select = q(".chat-api");
  const custom = q(".chat-api-custom");

  q(".empty-1").textContent = opts.vazio1;
  q(".empty-2").textContent = opts.vazio2;
  input.placeholder = opts.placeholder;

  const chat = {
    turnoSelecionado: null,

    render() {
      scroll.innerHTML = "";
      for (const m of LabSession.mensagens) {
        const bolha = document.createElement("div");
        bolha.className = "bubble " + (m.lado === "lead" ? "bubble-lead" : "bubble-agent");
        bolha.innerHTML = textoDaBolha(m.texto); // conteúdo já escapado por textoDaBolha
        if (m.turno) bolha.dataset.turno = m.turno;
        if (m.lado === "agent" && m.source) {
          const src = document.createElement("span");
          src.className = "src";
          src.textContent = m.source;
          bolha.appendChild(src);
        }
        if (opts.aoSelecionarTurno && m.turno) {
          bolha.classList.add("clicavel");
          bolha.classList.toggle("selected", m.turno === chat.turnoSelecionado);
          bolha.addEventListener("click", () => opts.aoSelecionarTurno(m.turno));
        }
        scroll.appendChild(bolha);
      }
      const tem = LabSession.mensagens.length > 0;
      scroll.hidden = !tem;
      vazio.hidden = tem;
      scroll.scrollTop = scroll.scrollHeight;
    },

    marcarTurno(id) {
      chat.turnoSelecionado = id;
      scroll.querySelectorAll(".bubble").forEach((b) => b.classList.toggle("selected", b.dataset.turno === id));
    },

    focar() {
      input.focus();
    },

    /** URL de cotação a mostrar: a EFETIVA da sessão; sem sessão, a escolhida. */
    urlAtual() {
      return (LabSession.id ? LabSession.api : LabSession.apiEscolhida) || "";
    },

    preencherControles() {
      const opcoes = ['<option value="">API padrão do agente</option>'];
      for (const [rotulo, url] of Object.entries(LabSession.endpoints)) {
        opcoes.push(`<option value="${escapeHtml(url)}">${escapeHtml(rotulo)}</option>`);
      }
      opcoes.push('<option value="__livre">URL livre…</option>');
      select.innerHTML = opcoes.join("");
      chat.refletirApi();
      chat.preencherModelos();
    },

    preencherModelos() {
      const modelo = q(".chat-model");
      modelo.innerHTML = opcoesModelo(LabSession.geminiModel);
      modelo.value = LabSession.geminiModel || "";
    },

    refletirApi() {
      const url = chat.urlAtual();
      const conhecida = Array.from(select.options).some((o) => o.value === url);
      select.value = conhecida ? url : "__livre";
      custom.hidden = conhecida;
      if (!conhecida) custom.value = url;
    },

    onSessao(msg) {
      if (msg.tipo === "efetivo") chat.preencherControles();
      else if (msg.tipo === "api") chat.refletirApi();
      else if (msg.tipo === "sessao") {
        aviso.hidden = true;
        chat.turnoSelecionado = null;
        chat.refletirApi();
        chat.render();
      } else if (msg.tipo === "evento") {
        if (msg.ev.event === "inbound" || msg.ev.event === "outbound") chat.render();
      } else if (msg.tipo === "enviando") typing.hidden = !msg.ativo;
      else if (msg.tipo === "indisponivel") {
        aviso.hidden = false;
        aviso.textContent = `Lab indisponível: ${msg.mensagem}`;
      }
    },

    async enviarTexto() {
      const texto = input.value.trim();
      if (!texto) return;
      input.value = "";
      await guarded(() => LabSession.enviar({ text: texto }))();
    },
  };

  q(".chat-send").addEventListener("click", () => chat.enviarTexto());
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") chat.enviarTexto();
  });
  q(".chat-clear").addEventListener("click", () => {
    input.value = "";
    input.focus();
  });
  q(".chat-model").addEventListener("change", async () => {
    const modelo = q(".chat-model");
    try {
      await aplicarModelo(modelo.value);
    } catch (err) {
      toast(err.message || String(err), "error");
      chat.preencherModelos(); // falhou: volta a mostrar o modelo que está valendo
    }
  });
  select.addEventListener("change", () => {
    const livre = select.value === "__livre";
    custom.hidden = !livre;
    if (livre) custom.focus();
    else guarded(() => LabSession.trocarApi(select.value))();
  });
  custom.addEventListener("change", () => guarded(() => LabSession.trocarApi(custom.value))());
  if (opts.microfone) {
    const mic = q(".chat-mic");
    mic.hidden = false;
    mic.addEventListener("click", () => guarded(() => LabSession.enviar({ media_type: "audio" }))());
  }

  LabSession.subscribe((msg) => chat.onSessao(msg));
  ModelCatalog.subscribe(() => chat.preencherModelos());
  return chat;
}

// -------------------------------------------------------------------------- aba Atendimentos
// Todas as conversas do agente (WhatsApp, Lab e CLI) lidas dos JSONL pelo backend. A lista
// atualiza a cada 5s; a conversa aberta busca só os eventos novos (`since`) a cada 3s. Nenhum
// timer roda fora da aba.
const AT_CAMPOS = [
  ["idade", "idade"],
  ["veiculo_texto", "veículo"],
  ["veiculo_ano", "ano"],
  ["cep", "CEP"],
  ["plano_id", "plano"],
  ["data_inicio", "início"],
];
const AT_INTERVALO_LISTA = 5000;
const AT_INTERVALO_DETALHE = 3000;

/** "agora", "há 3 min", "há 2 h", "há 4 d" — o suficiente para varrer a lista. */
function tempoRelativo(iso) {
  if (!iso) return "";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "";
  const seg = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (seg < 60) return "agora";
  if (seg < 3600) return `há ${Math.floor(seg / 60)} min`;
  if (seg < 86400) return `há ${Math.floor(seg / 3600)} h`;
  return `há ${Math.floor(seg / 86400)} d`;
}

const Atendimentos = {
  itens: [],
  assinaturaLista: "",
  cid: null,
  resumo: null,
  eventos: [],
  total: 0,
  filtros: { status: "", origem: "", q: "" },
  disponivel: true,
  ativo: false,
  timerLista: null,
  timerDetalhe: null,

  init() {
    document.querySelectorAll("#at-status button").forEach((b) => {
      b.addEventListener("click", () => {
        this.filtros.status = b.dataset.status;
        document.querySelectorAll("#at-status button").forEach((o) => o.classList.toggle("active", o === b));
        this.recarregarLista();
      });
    });
    el("at-origem").addEventListener("change", () => {
      this.filtros.origem = el("at-origem").value;
      this.recarregarLista();
    });
    el("at-busca").addEventListener("input", () => {
      this.filtros.q = el("at-busca").value.trim();
      this.recarregarLista();
    });
    document.querySelectorAll("#at-side-tabs button").forEach((b) => {
      b.addEventListener("click", () => this.mostrarPainel(b.dataset.pane));
    });
    const takeover = el("at-takeover");
    takeover.addEventListener("click", withLoading(takeover, () => this.alternarTakeover()));
    el("at-send").addEventListener("click", () => this.enviar());
    el("at-input").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") this.enviar();
    });
    el("at-clear").addEventListener("click", () => {
      el("at-input").value = "";
      el("at-input").focus();
    });
  },

  // ---- ciclo de vida (ligado ao router)
  entrar(cid) {
    this.ativo = true;
    if (!this.timerLista) this.timerLista = setInterval(() => this.carregarLista(), AT_INTERVALO_LISTA);
    this.carregarLista();
    this.abrir(cid || null);
  },

  sair() {
    this.ativo = false;
    clearInterval(this.timerLista);
    this.timerLista = null;
    this.pararDetalhe();
  },

  pararDetalhe() {
    clearInterval(this.timerDetalhe);
    this.timerDetalhe = null;
  },

  indisponivel(mensagem) {
    this.disponivel = false;
    const aviso = el("at-indisponivel");
    aviso.hidden = false;
    aviso.textContent = `Atendimentos indisponível: ${mensagem}`;
    el("at-itens").hidden = true;
    el("at-vazio").hidden = true;
    this.sair();
  },

  // ---- lista
  recarregarLista() {
    this.assinaturaLista = ""; // força o redesenho mesmo com os mesmos itens
    this.carregarLista();
  },

  async carregarLista() {
    if (!this.disponivel) return;
    const busca = new URLSearchParams();
    for (const [chave, valor] of Object.entries(this.filtros)) if (valor) busca.set(chave, valor);
    let data;
    try {
      data = await api(`/api/atendimentos?${busca.toString()}`);
    } catch (err) {
      if (err.status === 404) this.indisponivel("rota não existe neste backend");
      else toast(err.message || String(err), "error");
      return;
    }
    this.itens = data.itens || [];
    const assinatura = JSON.stringify(this.itens) + this.cid;
    if (assinatura === this.assinaturaLista) return; // nada mudou: não repinta (preserva o scroll)
    this.assinaturaLista = assinatura;
    this.renderLista();
  },

  renderLista() {
    const box = el("at-itens");
    const scroll = box.scrollTop;
    box.innerHTML = "";
    for (const item of this.itens) box.appendChild(this.linha(item));
    box.scrollTop = scroll;
    box.hidden = this.itens.length === 0;
    const vazio = el("at-vazio");
    vazio.hidden = this.itens.length > 0;
    // "nada aqui" e "nada com esses filtros" são coisas diferentes para quem opera
    const filtrando = Boolean(this.filtros.status || this.filtros.origem || this.filtros.q);
    vazio.querySelector(".empty-1").textContent = filtrando ? "Nenhuma conversa com esses filtros." : "Nenhuma conversa ainda.";
    vazio.querySelector(".empty-2").textContent = filtrando
      ? "Ajuste o status, a origem ou a busca."
      : "As conversas do WhatsApp, do Lab e do CLI aparecem aqui assim que o agente responde a primeira mensagem.";
    this.preencherOrigens();
  },

  /** O select de origem sai dos próprios itens (o backend não expõe a lista). */
  preencherOrigens() {
    const select = el("at-origem");
    const origens = Array.from(new Set(this.itens.map((i) => i.origem).filter(Boolean))).sort();
    const atual = this.filtros.origem;
    const conhecidas = Array.from(select.options).map((o) => o.value);
    if (JSON.stringify(["", ...origens]) === JSON.stringify(conhecidas)) return;
    select.innerHTML =
      '<option value="">Todas as origens</option>' +
      origens.map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
    select.value = origens.includes(atual) ? atual : "";
  },

  linha(item) {
    const div = document.createElement("div");
    div.className = "at-item" + (item.conversation_id === this.cid ? " selected" : "");

    const topo = document.createElement("div");
    topo.className = "at-item-topo";
    const nome = document.createElement("span");
    nome.className = "at-item-nome";
    nome.textContent = item.nome || item.conversation_id;
    const quando = document.createElement("span");
    quando.className = "at-item-quando";
    quando.textContent = tempoRelativo(item.ultimo_ts);
    topo.append(nome, quando);

    const meta = document.createElement("div");
    meta.className = "at-item-meta";
    meta.appendChild(badgeEl(item.origem || "?", "at-origem"));
    meta.appendChild(badgeEl(item.status, `at-st-${item.status}`));
    if (item.stage) {
      const etapa = document.createElement("span");
      etapa.className = "at-item-etapa";
      etapa.textContent = item.stage;
      meta.appendChild(etapa);
    }

    const msg = document.createElement("div");
    msg.className = "at-item-msg";
    msg.textContent = truncate((item.ultima_msg || "").replace(/\s+/g, " "), 80);

    div.append(topo, meta, msg);
    div.addEventListener("click", () => {
      location.hash = `#atendimentos/${item.conversation_id}`;
    });
    return div;
  },

  // ---- detalhe
  abrir(cid) {
    if (cid === this.cid) return;
    this.pararDetalhe();
    this.cid = cid;
    this.eventos = [];
    this.total = 0;
    this.resumo = null;
    el("at-conversa").hidden = !cid;
    el("at-sem-selecao").hidden = !!cid;
    this.assinaturaLista = ""; // a seleção mudou: repinta a lista
    this.renderLista();
    if (!cid) return;
    el("at-cid").textContent = cid;
    el("at-mensagens").innerHTML = "";
    el("at-eventos").innerHTML = "";
    this.mostrarPainel("eventos");
    this.carregarDetalhe();
    this.timerDetalhe = setInterval(() => this.carregarDetalhe(), AT_INTERVALO_DETALHE);
  },

  async carregarDetalhe() {
    if (!this.cid) return;
    const cid = this.cid;
    let data;
    try {
      data = await api(`/api/atendimentos/${encodeURIComponent(cid)}?since=${this.eventos.length}`);
    } catch (err) {
      this.pararDetalhe();
      if (err.status === 404) toast(`conversa ${cid} não encontrada`, "error");
      else toast(err.message || String(err), "error");
      return;
    }
    if (cid !== this.cid) return; // o operador trocou de conversa enquanto a resposta vinha
    if (data.total < this.eventos.length) this.eventos = []; // arquivo rotacionado: recomeça
    const novos = data.eventos || [];
    this.eventos = this.eventos.concat(novos);
    this.total = data.total;
    this.resumo = data.resumo;
    this.renderCabecalho();
    if (novos.length) {
      this.renderMensagens();
      for (const ev of novos) el("at-eventos").appendChild(this.linhaEvento(ev));
      el("at-eventos").scrollTop = el("at-eventos").scrollHeight;
    }
    this.renderEstado();
  },

  renderCabecalho() {
    const resumo = this.resumo || {};
    const badges = el("at-badges");
    badges.innerHTML = "";
    badges.appendChild(badgeEl(resumo.origem || "?", "at-origem"));
    badges.appendChild(badgeEl(resumo.status || "?", `at-st-${resumo.status}`));
    if (resumo.stage) badges.appendChild(badgeEl(resumo.stage));
    if (resumo.handoff_reason) badges.appendChild(badgeEl(`handoff: ${resumo.handoff_reason}`, "at-st-encerrado"));

    const humano = resumo.status === "humano";
    const takeover = el("at-takeover");
    takeover.textContent = humano ? "Devolver ao agente" : "Assumir";
    takeover.classList.toggle("green", humano);

    // enviar só vale em conversa do WhatsApp assumida (contrato do backend)
    const podeEnviar = humano && String(this.cid).startsWith("wa-");
    el("at-composer").hidden = !podeEnviar;
    const nota = el("at-nota");
    if (humano && !podeEnviar) {
      nota.hidden = false;
      nota.textContent = "Conversa assumida. Só dá para responder por aqui em conversas do WhatsApp (wa-*).";
    } else {
      nota.hidden = true;
    }
  },

  renderMensagens() {
    const box = el("at-mensagens");
    const noFim = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = "";
    for (const ev of this.eventos) {
      if (ev.event !== "inbound" && ev.event !== "outbound") continue;
      const dados = ev.data || {};
      const lead = ev.event === "inbound";
      const source = dados.source || "";
      const bolha = document.createElement("div");
      bolha.className = "bubble " + (lead ? "bubble-lead" : "bubble-agent") + (source === "humano" ? " bubble-humano" : "");
      bolha.innerHTML = textoDaBolha(dados.text || (dados.media_type ? `(mídia: ${dados.media_type})` : ""));
      const etiqueta = lead ? dados.sender_name || "" : source;
      if (etiqueta) {
        const src = document.createElement("span");
        src.className = "src";
        src.textContent = etiqueta;
        bolha.appendChild(src);
      }
      box.appendChild(bolha);
    }
    if (noFim) box.scrollTop = box.scrollHeight;
  },

  linhaEvento(ev) {
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
    return row;
  },

  /** Estado do lead reconstruído da transcrição: extrações acumuladas + última decisão. */
  renderEstado() {
    const campos = {};
    let intent = null;
    let stage = null;
    let handoff = null;
    for (const ev of this.eventos) {
      const dados = ev.data || {};
      if (ev.event === "extraction") {
        for (const [chave] of AT_CAMPOS) if (dados[chave] !== null && dados[chave] !== undefined) campos[chave] = dados[chave];
        if (dados.intent) intent = dados.intent;
      } else if (ev.event === "decision" && dados.stage) stage = dados.stage;
      else if (ev.event === "handoff") handoff = dados.reason || dados.motivo || "sim";
    }
    const resumo = this.resumo || {};
    const linhas = [
      ["origem", resumo.origem],
      ["etapa", stage || resumo.stage],
      ["turnos", resumo.turnos],
      ["última intenção", intent],
      ...AT_CAMPOS.map(([chave, rotulo]) => [rotulo, campos[chave]]),
      ["handoff", handoff || resumo.handoff_reason],
    ].filter(([, valor]) => valor !== null && valor !== undefined && valor !== "");

    const box = el("at-estado");
    box.innerHTML = "";
    if (!linhas.length) {
      box.innerHTML = '<p class="muted">Sem dados coletados nesta conversa.</p>';
      return;
    }
    for (const [rotulo, valor] of linhas) {
      const linha = document.createElement("div");
      linha.className = "at-estado-linha";
      const c = document.createElement("span");
      c.className = "at-estado-chave";
      c.textContent = rotulo;
      const v = document.createElement("span");
      v.className = "at-estado-valor";
      v.textContent = String(valor);
      linha.append(c, v);
      box.appendChild(linha);
    }
  },

  mostrarPainel(nome) {
    for (const p of ["eventos", "estado"]) el(`at-pane-${p}`).hidden = p !== nome;
    document.querySelectorAll("#at-side-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.pane === nome));
  },

  // ---- ações
  async alternarTakeover() {
    if (!this.cid) return;
    const humano = this.resumo && this.resumo.status === "humano";
    const rota = humano ? "devolver" : "assumir";
    this.resumo = await api(`/api/atendimentos/${encodeURIComponent(this.cid)}/${rota}`, { method: "POST" });
    this.renderCabecalho();
    toast(humano ? "conversa devolvida ao agente" : "conversa assumida", "success");
    this.recarregarLista();
  },

  async enviar() {
    const input = el("at-input");
    const texto = input.value.trim();
    if (!texto || !this.cid) return;
    input.value = "";
    try {
      await api(`/api/atendimentos/${encodeURIComponent(this.cid)}/mensagens`, { method: "POST", body: { text: texto } });
      await this.carregarDetalhe();
      this.recarregarLista();
    } catch (err) {
      input.value = texto; // não perde o que o operador escreveu
      toast(err.message || String(err), "error");
    }
  },
};

// -------------------------------------------------------------------------- aba Lab
const PAINEIS = ["eventos", "contexto", "estado"];

const Lab = {
  chat: null,
  turnos: new Map(), // message_id do inbound -> { inbound, events: [] }
  turnoAtual: null,
  turnoSelecionado: null,

  init() {
    this.chat = criarChat(el("lab-chat"), {
      placeholder: "Converse com o agente…",
      vazio1: "Converse com o agente enviando uma mensagem.",
      vazio2: "Cada turno abre à direita: eventos ao vivo, contexto do LLM e estado do lead.",
      microfone: true,
      aoSelecionarTurno: (id) => this.selecionarTurno(id),
    });

    const btnNova = el("lab-new-session");
    btnNova.addEventListener("click", withLoading(btnNova, () => LabSession.novaSessao()));
    el("lab-copy-id").addEventListener("click", () => this.copiarId());
    document.querySelectorAll("#lab-subtabs button").forEach((b) => {
      b.addEventListener("click", () => {
        location.hash = `#lab/${b.dataset.sub}`;
      });
    });
    document.querySelectorAll("#lab-side-tabs button").forEach((b) => {
      b.addEventListener("click", () => this.mostrarPainel(b.dataset.pane));
    });
    LabSession.subscribe((msg) => this.onSessao(msg));
  },

  mostrarPainel(nome) {
    for (const p of PAINEIS) el(`lab-pane-${p}`).hidden = p !== nome;
    document.querySelectorAll("#lab-side-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.pane === nome));
  },

  async copiarId() {
    if (!LabSession.id) return;
    try {
      await navigator.clipboard.writeText(LabSession.id);
      toast("id da sessão copiado", "success");
    } catch {
      toast("não consegui copiar o id", "error");
    }
  },

  onSessao(msg) {
    if (msg.tipo === "sessao") this.aoAbrirSessao();
    else if (msg.tipo === "evento") this.onEvento(msg.ev);
    else if (msg.tipo === "estado") el("lab-state-json").textContent = JSON.stringify(msg.state, null, 2);
  },

  aoAbrirSessao() {
    this.turnos.clear();
    this.turnoAtual = null;
    this.turnoSelecionado = null;
    el("lab-session-id").textContent = LabSession.id;
    el("lab-copy-id").disabled = false;
    Breadcrumb.set("conversa", String(LabSession.id).slice(0, 8));
    el("lab-events-list").innerHTML = "";
    el("lab-context-body").innerHTML = '<p class="muted">Selecione um turno (bolha do lead) para ver o contexto.</p>';
    el("lab-state-json").textContent = "(sem turnos ainda)";
  },

  onEvento(ev) {
    if (ev.event === "inbound") {
      this.turnoAtual = ev.message_id;
      this.turnos.set(this.turnoAtual, { inbound: ev, events: [] });
    } else if (this.turnoAtual && this.turnos.has(this.turnoAtual)) {
      this.turnos.get(this.turnoAtual).events.push(ev);
    }
    this.renderEventRow(ev);
    if (this.turnoSelecionado && this.turnoSelecionado === this.turnoAtual) this.renderContexto();
  },

  selecionarTurno(id) {
    this.turnoSelecionado = id;
    this.chat.marcarTurno(id);
    this.mostrarPainel("contexto");
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
};

// -------------------------------------------------------------------------- "Testar prompt" (aba Prompts)
// Mesmo componente de chat do Lab e MESMA sessão: aqui só o chat: eventos e contexto continuam
// sendo assunto da aba Lab (link "ver eventos no Lab").
const TestPanel = {
  chat: null,

  init() {
    this.chat = criarChat(el("test-body"), {
      placeholder: "Envie uma mensagem para testar o prompt…",
      vazio1: "Teste o agente enviando uma mensagem.",
      vazio2: "O agente usa a versão ATIVA de cada slot — ative a versão para testá-la.",
    });
    el("test-toggle").addEventListener("click", () => this.alternar());
    LabSession.subscribe((msg) => {
      if (msg.tipo === "sessao") el("test-ver-eventos").hidden = false;
    });
  },

  async alternar() {
    const corpo = el("test-body");
    const abrindo = corpo.hidden;
    corpo.hidden = !abrindo;
    const botao = el("test-toggle");
    botao.setAttribute("aria-expanded", String(abrindo));
    botao.classList.toggle("aberto", abrindo);
    if (!abrindo) return;
    this.chat.render();
    if (!LabSession.id) await guarded(() => LabSession.garantirSessao())();
    this.chat.focar();
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
    const emPrompts = abaAtual().tab === "lab" && abaAtual().sub === "prompts";
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === "s" || ev.key === "S")) {
      if (!emPrompts) return;
      ev.preventDefault();
      const salvar = el("pv-save");
      if (!salvar.disabled) salvar.click();
      return;
    }
    const alvo = ev.target;
    const digitando = alvo && (alvo.tagName === "INPUT" || alvo.tagName === "TEXTAREA" || alvo.isContentEditable);
    if (ev.key === "/" && !digitando && emPrompts) {
      ev.preventDefault();
      Prompts.abrirBuscaSlot();
    }
  });
}

// -------------------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", () => {
  Health.start();
  Atendimentos.init();
  Tools.init();
  Prompts.init();
  Lab.init();
  TestPanel.init();
  atalhos();
  ModelCatalog.subscribe(() => {
    if (Config.effective) Config.render(); // refresh do catálogo repinta o select da ficha
  });
  LabSession.carregarEfetivo()
    .then(() => ModelCatalog.carregar())
    .catch((err) => toast(err.message, "error"));
  LabSession.iniciar(); // retoma a sessão desta aba, se ainda existir no servidor
  renderTab();
});
