// ============================================================
// 02-markdown.js —— 拆分自 public/app.js 第 398-711 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, escapeHtml, normalizeLanguage, restoreSafeHtml } from "./01-core.js";
import { choicePreviewMarkup, explicitChoiceGroups, isChoiceFence } from "./17-choice-groups.js";
export function highlightCode(rawCode, language) {
  const code = String(rawCode ?? '');
  const lang = normalizeLanguage(language);
  const esc = escapeHtml;
  const wrap = (text, cls) => `<span class="tok-${cls}">${esc(text)}</span>`;

  // HTML：先整体把标签与注释挑出来，其余按纯文本。
  if (lang === 'html') {
    const parts = [];
    const re = /<!--[\s\S]*?-->|<\/?[A-Za-z][^>]*>|&/g;
    let last = 0;
    let m;
    while ((m = re.exec(code)) !== null) {
      parts.push(esc(code.slice(last, m.index)));
      const chunk = m[0];
      if (chunk.startsWith('<!--')) {
        parts.push(wrap(chunk, 'comment'));
      } else if (chunk === '&') {
        parts.push(wrap(chunk, 'number'));
      } else {
        const m2 = chunk.match(/^(<\/?)([A-Za-z][\w-]*)([\s\S]*)$/);
        if (!m2) {
          parts.push(esc(chunk));
        } else {
          const open = `<span class="tok-tag">${esc(m2[1] + m2[2])}</span>`;
          const rest = m2[3];
          // 在「原始」属性区上一次性交替匹配属性名与字符串值，逐 token 转义后包 span，
          // 避免对已生成的 span 二次替换。
          let restHtml = '';
          let rlast = 0;
          const are = /[A-Za-z_:][\w:.-]*(?==)|"[^"]*"|'[^']*'/g;
          let am;
          while ((am = are.exec(rest)) !== null) {
            if (am.index > rlast) restHtml += esc(rest.slice(rlast, am.index));
            const tok = am[0];
            if (tok[0] === '"' || tok[0] === "'") restHtml += wrap(tok, 'string');
            else restHtml += `<span class="tok-attr">${esc(tok)}</span>`;
            rlast = am.index + tok.length;
          }
          if (rlast < rest.length) restHtml += esc(rest.slice(rlast));
          parts.push(open + restHtml);
        }
      }
      last = m.index + chunk.length;
    }
    parts.push(esc(code.slice(last)));
    return parts.join('');
  }

  const rules = [];
  if (lang === 'py' || lang === 'bash' || lang === 'yaml') {
    if (lang === 'bash') rules.push([/(^|\n)(\s*#[^\n]*)/g, 'comment']);
    else rules.push([/(#[^\n]*)/g, 'comment']);
  } else {
    rules.push([/(\/\/[^\n]*|\/\*[\s\S]*?\*\/|#[^\n]*)/g, 'comment']);
  }

  if (lang === 'py') {
    rules.push([/([rfbu]{0,2}"""[\s\S]*?"""|[rfbu]{0,2}'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g, 'string']);
    rules.push([/(\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?j?\b)/g, 'number']);
    rules.push([/(\b(?:def|class|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|import|from|as|with|try|except|finally|raise|lambda|pass|break|continue|global|nonlocal|yield|assert|del|async|await|match|case|self|cls)\b)/g, 'keyword']);
    rules.push([/(\b[A-Za-z_]\w*(?=\s*\())/g, 'func']);
  } else if (lang === 'json') {
    rules.push([/("[^"\n]*")(\s*:)/g, 'key']);
    rules.push([/("(?:\\.|[^"\\])*")/g, 'string']);
    rules.push([/(\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)/g, 'number']);
    rules.push([/(\b(?:true|false|null)\b)/g, 'keyword']);
  } else if (lang === 'bash') {
    rules.push([/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g, 'string']);
    rules.push([/(\b\d+(?:\.\d+)?\b)/g, 'number']);
    rules.push([/(\b(?:if|then|else|elif|fi|for|while|do|done|in|case|esac|function|return|exit|local|export|echo|cd|source|set|unset|shift)\b)/g, 'keyword']);
  } else if (lang === 'css') {
    rules.push([/("[^"]*"|'[^']*')/g, 'string']);
    rules.push([/(#[0-9a-fA-F]{3,8}\b|\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr|pt)?\b)/g, 'number']);
    rules.push([/(@[\w-]+)/g, 'keyword']);
    rules.push([/([A-Za-z-]+)(?=\s*:)/g, 'attr']);
    rules.push([/(\.[A-Za-z_-][\w-]*|#[A-Za-z_-][\w-]*)/g, 'func']);
  } else if (lang === 'yaml') {
    rules.push([/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/g, 'string']);
    rules.push([/(\b\d+(?:\.\d+)?\b|\btrue\b|\bfalse\b|\bnull\b)/g, 'number']);
    rules.push([(/(^[ \t]*[-:]?\s*)([A-Za-z_][\w-]*)(?=\s*:)/gm), 'key']);
  } else {
    // js / ts 及退化的类 C 语言
    rules.push([new RegExp('`(?:\\\\.|[^`\\\\])*`|"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'', 'g'), 'string']);
    rules.push([/(\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?n?\b)/g, 'number']);
    rules.push([/(\b(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|typeof|instanceof|in|of|class|extends|super|import|export|from|default|try|catch|finally|throw|async|await|yield|delete|void|this|static|get|set|interface|type|enum|implements|public|private|protected|readonly|namespace|declare|as|is|satisfies)\b)/g, 'keyword']);
    rules.push([/(\b(?:true|false|null|undefined|NaN|Infinity)\b)/g, 'number']);
    rules.push([/(\b[A-Za-z_$][\w$]*(?=\s*\())/g, 'func']);
  }

  // 用命名捕获组把每条规则合并进一个组合正则，匹配时直接得知 token 类别，
  // 避免对单个 token 重复 test() 受 lastIndex 影响导致的归属错误。
  let re;
  try {
    re = new RegExp(rules.map((r, i) => `(?<g${i}>${r[0].source})`).join('|'), 'g');
  } catch (_) {
    return esc(code);
  }
  const out = [];
  let last = 0;
  let mm;
  while ((mm = re.exec(code)) !== null) {
    if (mm.index > last) out.push(esc(code.slice(last, mm.index)));
    const token = mm[0];
    let cls = null;
    for (let i = 0; i < rules.length; i++) {
      if (mm.groups[`g${i}`] !== undefined) { cls = rules[i][1]; break; }
    }
    out.push(cls ? wrap(token, cls) : esc(token));
    last = mm.index + token.length;
  }
  if (last < code.length) out.push(esc(code.slice(last)));
  return out.join('');
}

// ---- Markdown 内联处理：逐段 / 逐单元格执行，避免加粗/代码/链接跨行、跨段落或跨表格行泄漏 ----
export function markdownInline(s) {
  return String(s || '')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

// 防御式表格单元格切分：按 | 切分一行，但：
//  1) 不切分被内联标签（<code>/<a>/<strong>…）包裹的 |（例如 <code>a|b</code> 里的 |）；
//  2) 支持 \| 转义（\| 作为一个字面 | 留在单元格里）。
export const TABLE_SHIELD_TAGS = new Set(['code','a','strong','b','em','i','span','del','s','u','sub','sup','pre','mark','kbd']);
export function splitMarkdownTableRow(row) {
  const text = String(row || '').trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells = [];
  let current = '';
  let inTag = false;
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '<') {
      const m = text.slice(i).match(/^<\/?\s*([A-Za-z][\w-]*)/);
      if (m) {
        const tag = m[1].toLowerCase();
        if (TABLE_SHIELD_TAGS.has(tag)) {
          if (text[i + 1] === '/') stack.pop();
          else stack.push(tag);
        }
      }
      inTag = true;
      current += ch;
      continue;
    }
    if (ch === '>') { inTag = false; current += ch; continue; }
    if (inTag) { current += ch; continue; }
    if (ch === '|' && text[i - 1] === '\\') {
      current = current.replace(/\\$/, '') + '|';
      continue;
    }
    if (ch === '|' && stack.length === 0) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  cells.push(current.trim());
  return cells;
}

// 只有“以 | 开头、以 | 结尾且中间至少一个字符”的行才可能是一个表格行，
// 避免把普通含 | 的文本（例如 “a | b，不是表格”）误判。
export function isMarkdownTableRow(line) {
  const t = String(line || '').trim();
  if (!t.startsWith('|') || !t.endsWith('|')) return false;
  return t.length > 2;
}

// 分隔行：所有单元格都是 - 和可选 :（如 | :-- | --: |）。
export function isMarkdownTableSeparator(line) {
  const t = String(line || '').trim();
  if (!t.startsWith('|') || !t.endsWith('|')) return false;
  const cells = splitMarkdownTableRow(t);
  return cells.length >= 1 && cells.every((c) => /^:?-{3,}:?$/.test(c.trim()));
}

export function collectMarkdownTable(lines, start) {
  if (start + 1 >= lines.length) return null;
  if (!isMarkdownTableRow(lines[start])) return null;
  if (!isMarkdownTableSeparator(lines[start + 1])) return null;
  const rows = [lines[start], lines[start + 1]];
  let j = start + 2;
  while (j < lines.length) {
    const t = lines[j].trim();
    if (!t) break;
    if (!t.startsWith('|')) break;
    rows.push(lines[j]);
    j++;
  }
  return { rows, end: j };
}

export function renderMarkdownTable(rows) {
  // 每行先做内联（把 ** / `code` / [text](url) 换成 <strong>/<code>/<a>），再按 | 切分，
  // 这样 “|” 在内联代码/链接/加粗里不会把单元格切坏；同时把加粗限制在“单行”内，避免跨行泄漏。
  const header = splitMarkdownTableRow(markdownInline(rows[0]));
  const body = rows.slice(2).map((r) => splitMarkdownTableRow(markdownInline(r)));
  const colCount = Math.max(1, header.length, ...body.map((c) => c.length));
  const th = header.map((c) => `<th>${c}</th>`).join('');
  const tbody = body.map((cells) => {
    let tds = '';
    for (let k = 0; k < colCount; k++) tds += `<td>${k < cells.length ? cells[k] : ''}</td>`;
    return `<tr>${tds}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tbody}</tbody></table></div>`;
}

export function markdown(text, allowRichText = true) {
  const codeBlocks = [];
  const addCodeBlock = (language, rawCode) => {
    const index = codeBlocks.length;
    // 显式 ```naiba-choices 块是模型给出的结构化选择（见 naiba/core/choices.py）：有效块渲染成
    // 可读题目与选项，不让用户看到原始 JSON；无效块保持下面的普通代码块（不隐藏、不生成控件）。
    if (isChoiceFence(language)) {
      const preview = choicePreviewMarkup(explicitChoiceGroups(rawCode));
      if (preview) {
        codeBlocks.push(preview);
        return index;
      }
    }
    const lang = escapeHtml(String(language || '').trim());
    const highlighted = highlightCode(rawCode, String(language || '').trim());
    codeBlocks.push(
      `<div class="code-block"><div class="code-block-bar">` +
      `<span class="code-lang">${lang}</span>` +
      `<button type="button" class="code-copy" data-copy-code>复制</button>` +
      `</div><pre><code data-language="${lang}">${highlighted}</code></pre></div>`
    );
    return index;
  };
  // 先在「未转义原文」上提取围栏代码块，把原始代码交给 highlightCode 着色，
  // 其余文本统一 escape；@CODE_x@ 占位符不含 HTML 特殊字符，escape 不受影响。
  let safe = String(text ?? '')
    .replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, language, code) => `\n@@CODE_${addCodeBlock(language, code)}@@\n`)
    .replace(/```([^\n]*)\n([\s\S]*)$/, (_, language, code) => `\n@@CODE_${addCodeBlock(language, code)}@@`);
  safe = escapeHtml(safe);
  // 富文本恒开（原「富文本」开关已移除）：仅白名单标签按安全规则还原，其余保持纯文本。
  if (allowRichText) safe = restoreSafeHtml(safe);
  const blocks = [];
  let paragraph = [];
  let listType = '';
  let listItems = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    // 段落结束时统一做内联，保证加粗/代码/链接只在单个段落内匹配，
    // 避免一个未闭合 ** 跨过段落到后续内容才闭合导致“吞掉”整段。
    blocks.push(`<p>${markdownInline(paragraph.join('<br>'))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(`<${listType}>${listItems.map((item) => `<li>${markdownInline(item)}</li>`).join('')}</${listType}>`);
    listType = '';
    listItems = [];
  };
  const startListItem = (type, item) => {
    flushParagraph();
    if (listType && listType !== type) flushList();
    listType = type;
    listItems.push(item);
  };

  const lines = safe.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }
    const codeMatch = line.match(/^@@CODE_(\d+)@@$/);
    if (codeMatch) {
      flushParagraph();
      flushList();
      blocks.push(codeBlocks[Number(codeMatch[1])]);
      continue;
    }
    // 表格：只有“表头行 + 紧跟的分隔行”才识别为表格，避免普通含 | 文本误判。
    const table = collectMarkdownTable(lines, i);
    if (table) {
      flushParagraph();
      flushList();
      blocks.push(renderMarkdownTable(table.rows));
      i = table.end - 1;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      blocks.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushParagraph();
      flushList();
      blocks.push('<hr>');
      continue;
    }
    const unordered = line.match(/^[-*+]\s+(.+)$/);
    if (unordered) {
      startListItem('ul', unordered[1]);
      continue;
    }
    const ordered = line.match(/^\d{1,3}[.)、]\s+(.+)$/);
    if (ordered) {
      startListItem('ol', ordered[1]);
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  return blocks.join('');
}

