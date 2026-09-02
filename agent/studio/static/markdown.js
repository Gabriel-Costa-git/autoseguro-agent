// markdown.js — renderizador Markdown mínimo do Studio (preview da aba Prompts).
// Sem lib, sem CDN. Regra de segurança: o texto é SEMPRE escapado antes de virar HTML;
// as marcas do Markdown são aplicadas depois, sobre o texto já escapado, e os trechos que
// não podem ser reprocessados (código, links) saem de cena como token opaco.
// Cobre: headings, negrito, itálico, listas, código inline/bloco, links, régua, quebras de
// linha e os placeholders `{nome}` do slot, que viram chip destacado.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
// mesma regra do backend (`_PLACEHOLDER_RE` em agent/runtime_config.py)
const PLACEHOLDER_RE = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
const MARCA = "\u0000"; // delimitador de token: não aparece em texto digitado
const TOKEN_RE = /\u0000(\d+)\u0000/g;

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function guardar(tokens, html) {
  tokens.push(html);
  return `${MARCA}${tokens.length - 1}${MARCA}`;
}

function restaurar(s, tokens) {
  // repete até estabilizar: um link pode conter o token de um código inline
  let antes;
  do {
    antes = s;
    s = s.replace(TOKEN_RE, (_, i) => tokens[Number(i)]);
  } while (s !== antes);
  return s;
}

function urlSegura(url) {
  // só esquemas inertes; `javascript:` e afins ficam como texto
  return /^(https?:\/\/|mailto:|#|\/)/i.test(url) ? url : null;
}

function inline(texto, tokens) {
  let s = escapeHtml(texto);
  s = s.replace(/\{\{|\}\}/g, (m) => guardar(tokens, m[0])); // chave literal escapada
  s = s.replace(/`([^`]+)`/g, (_, code) => guardar(tokens, `<code>${code}</code>`));
  s = s.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, txt, url) => {
    const href = urlSegura(url);
    return href === null ? m : guardar(tokens, `<a href="${href}" target="_blank" rel="noopener noreferrer">${txt}</a>`);
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  s = s.replace(PLACEHOLDER_RE, '<span class="md-ph">{$1}</span>');
  return restaurar(s, tokens);
}

export function renderMarkdown(texto) {
  const tokens = [];
  const linhas = String(texto ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let paragrafo = [];
  let lista = null; // { tag, itens }

  const fecharParagrafo = () => {
    if (!paragrafo.length) return;
    // quebra simples dentro do parágrafo vira <br> (prompt tem muita linha solta)
    out.push(`<p>${paragrafo.map((l) => inline(l, tokens)).join("<br>")}</p>`);
    paragrafo = [];
  };
  const fecharLista = () => {
    if (!lista) return;
    out.push(`<${lista.tag}>${lista.itens.map((t) => `<li>${inline(t, tokens)}</li>`).join("")}</${lista.tag}>`);
    lista = null;
  };
  const fecharTudo = () => {
    fecharParagrafo();
    fecharLista();
  };

  for (let i = 0; i < linhas.length; i++) {
    const linha = linhas[i];

    if (/^\s*```/.test(linha)) {
      fecharTudo();
      const corpo = [];
      i++;
      while (i < linhas.length && !/^\s*```/.test(linhas[i])) corpo.push(linhas[i++]);
      out.push(`<pre><code>${escapeHtml(corpo.join("\n"))}</code></pre>`);
      continue;
    }
    if (!linha.trim()) {
      fecharTudo();
      continue;
    }
    if (/^\s*(-{3,}|_{3,}|\*{3,})\s*$/.test(linha)) {
      fecharTudo();
      out.push("<hr>");
      continue;
    }
    const heading = linha.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      fecharTudo();
      const n = heading[1].length;
      out.push(`<h${n}>${inline(heading[2], tokens)}</h${n}>`);
      continue;
    }
    const item = linha.match(/^\s*[-*+]\s+(.*)$/) || linha.match(/^\s*\d+[.)]\s+(.*)$/);
    if (item) {
      fecharParagrafo();
      const tag = /^\s*[-*+]\s/.test(linha) ? "ul" : "ol";
      if (lista && lista.tag !== tag) fecharLista();
      lista = lista || { tag, itens: [] };
      lista.itens.push(item[1]);
      continue;
    }
    fecharLista();
    paragrafo.push(linha);
  }
  fecharTudo();
  return out.join("\n");
}
