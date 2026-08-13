/* Agent 回答的 Markdown 渲染。
 *
 * 在这之前，回答是 escapeHtml 之后塞进 <p style="white-space: pre-wrap">，
 * 于是模型输出的表格、列表、代码块、加粗全都以原始符号显示：
 *     | 项目 | 状态 |
 *     |---|---|
 *     - 第一条
 *     **重点**
 * 而 Agent 的回答恰恰最爱用表格和列表——「事实 / 判断 / 下一步」这种结构
 * 用纯文本读起来最费劲。
 *
 * 为什么自己写而不是引一个 markdown 库：
 * 这段文本不完全可信。它来自 LLM，而 LLM 的输入里有抓回来的网页正文、
 * 知识库笔记、收件箱内容——这些都可能带着构造好的 HTML。通用库默认允许
 * 内联 HTML，要安全就得再叠一个 sanitizer，两个依赖都得跟着升级。
 * 这里反过来做：先把整段文本转义成纯文本，之后只把我自己认识的那几种
 * 结构还原成标签。任何没被识别的东西都留在转义态，构造不出标签。
 */
(function initWorkbenchMarkdown() {
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  // 只允许 http/https 链接。javascript: 和 data: 是最常见的两种注入载体，
  // 而 Agent 回答里出现它们没有任何正当理由。
  function safeLink(url) {
    const text = String(url || "").trim();
    return /^https?:\/\//i.test(text) ? text : "";
  }

  function inline(text) {
    // 进来的已经是转义过的文本，下面只做「把标记符号换成标签」。
    let out = text;
    out = out.replace(/`([^`\n]+)`/g, (_m, code) => `<code>${code}</code>`);
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (match, label, href) => {
      const url = safeLink(href.replaceAll("&amp;", "&"));
      return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : match;
    });
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return out;
  }

  function tableRowCells(line) {
    // 去掉首尾的竖线再切，避免出现空的首尾列。
    return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());
  }

  const isTableDivider = (line) => /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes("-");

  function renderMarkdown(source) {
    const lines = escapeHtml(source).split("\n");
    const html = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];

      // 围栏代码块：整块原样输出，里面的 Markdown 符号不参与解析。
      const fence = line.match(/^\s*```(\S*)\s*$/);
      if (fence) {
        const body = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        html.push(`<div class="md-code-block"><button type="button" class="md-copy" data-md-copy>复制</button><pre class="md-code"${fence[1] ? ` data-lang="${escapeHtml(fence[1])}"` : ""}><code>${body.join("\n")}</code></pre></div>`);
        continue;
      }

      // 表格：至少要有表头 + 分隔行，否则一行普通的竖线文本会被误当成表格。
      if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        const head = tableRowCells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(tableRowCells(lines[index]));
          index += 1;
        }
        html.push(
          `<div class="md-table-wrap"><table class="md-table"><thead><tr>${
            head.map((cell) => `<th>${inline(cell)}</th>`).join("")
          }</tr></thead><tbody>${
            rows.map((row) => `<tr>${
              // 补齐/截断到表头列数：模型偶尔会多写或少写一个竖线，
              // 不对齐的话整张表会错位。
              head.map((_h, column) => `<td>${inline(row[column] ?? "")}</td>`).join("")
            }</tr>`).join("")
          }</tbody></table></div>`,
        );
        continue;
      }

      const heading = line.match(/^\s*(#{1,4})\s+(.*)$/);
      if (heading) {
        const level = Math.min(6, heading[1].length + 2);   // # 在气泡里当 h3 起步，避免抢标题层级
        html.push(`<h${level} class="md-h">${inline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
        html.push('<hr class="md-hr" />');
        index += 1;
        continue;
      }

      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (bullet || ordered) {
        const tag = bullet ? "ul" : "ol";
        const items = [];
        while (index < lines.length) {
          const current = lines[index];
          const match = bullet ? current.match(/^\s*[-*+]\s+(.*)$/) : current.match(/^\s*\d+[.)]\s+(.*)$/);
          if (!match) break;
          items.push(`<li>${inline(match[1])}</li>`);
          index += 1;
        }
        html.push(`<${tag} class="md-list">${items.join("")}</${tag}>`);
        continue;
      }

      const quote = line.match(/^\s*&gt;\s?(.*)$/);
      if (quote) {
        const body = [];
        while (index < lines.length) {
          const match = lines[index].match(/^\s*&gt;\s?(.*)$/);
          if (!match) break;
          body.push(match[1]);
          index += 1;
        }
        html.push(`<blockquote class="md-quote">${inline(body.join("<br />"))}</blockquote>`);
        continue;
      }

      if (!line.trim()) {
        index += 1;
        continue;
      }

      // 普通段落：连续的非空行合成一段，行内换行保留为 <br>。
      const paragraph = [];
      while (index < lines.length && lines[index].trim()
             && !/^\s*```/.test(lines[index])
             && !/^\s*(#{1,4})\s+/.test(lines[index])
             && !/^\s*[-*+]\s+/.test(lines[index])
             && !/^\s*\d+[.)]\s+/.test(lines[index])
             && !/^\s*&gt;\s?/.test(lines[index])
             && !(lines[index].includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1]))) {
        paragraph.push(lines[index]);
        index += 1;
      }
      if (paragraph.length) html.push(`<p class="md-p">${inline(paragraph.join("<br />"))}</p>`);
      else index += 1;
    }
    return html.join("");
  }

  // 只做行内标记（**粗体**、`代码`、[链接]()），不产生块级标签。
  // 用在「结构化结果」的条目里：那里每条本来就是一行，套一层 <p> 反而会把
  // <li> 撑开成两行。
  function renderInline(source) {
    return inline(escapeHtml(source));
  }

  // 代码块复制：navigator.clipboard 需要安全上下文（https/localhost）；
  // 非安全上下文 fallback 到临时 textarea + execCommand('copy')。
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall through */ }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(textarea);
    return ok;
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-md-copy]");
    if (!button) return;
    const block = button.closest(".md-code-block");
    const code = block?.querySelector("pre code");
    if (!code) return;
    const original = button.textContent;
    button.disabled = true;
    copyText(code.textContent).then((ok) => {
      button.textContent = ok ? "已复制" : "复制失败";
      window.setTimeout(() => {
        button.textContent = original;
        button.disabled = false;
      }, 1600);
    }).catch(() => {
      button.textContent = "复制失败";
      button.disabled = false;
    });
  });

  window.WorkbenchMarkdown = { render: renderMarkdown, renderInline, escapeHtml };
})();
