// ============================================================
// 02b-markdown-stream.js —— 流式 Markdown 的块边界扫描（纯函数，零依赖、零 DOM）
// ============================================================
//
// 流式回复的块级增量渲染（04-messages.js::scheduleStreamingMarkdown）靠这里把累计全文
// 切成块。它必须是纯字符串逻辑：不带任何 import / DOM 引用，node 才能直接加载做行为单测。
//
// 块边界两条规则（与 02-markdown.js::markdown() 的块语义对齐）：
//   1. 空行（不在代码围栏内）——markdown() 在空行处 flush 段落/列表，表格、段落都以空行收尾；
//   2. 代码围栏的闭合行——markdown() 把围栏代码提取成独立块（@@CODE_n@@ 占位符自成一个块），
//      所以闭合行一到，这个代码块就是完整块，不必等后面的空行。
//
// 已知取舍：行中出现的 ```（不在行首）本扫描器不认、markdown() 的全局正则会认——这种
// 病态输入下流式分块与终态全量渲染可能有出入，终态渲染仍是唯一权威，结束即自校正。
//
// state 是调用方持有并复用的普通对象（就地推进，勿复制）：
//   pos       已扫描到的偏移。不变式：永远停在行首（或 0）——没扫完的半行留到下次。
//   fenceOpen 扫到的位置是否处于未闭合围栏内。
//   segStart  当前未完结块的起始偏移（= 稳定区长度；尾块 = raw.slice(segStart)）。
// 返回本次新完结的稳定块区间 [[start, end)，...]（按出现顺序、互不重叠；end 是闭区间
// 右端 +1，即 slice 语义）。

export function scanMarkdownBlocks(raw, state) {
  const boundaries = [];
  const len = raw.length;
  let i = state.pos;
  let lineStart = i;
  let fenceOpen = state.fenceOpen;
  let segStart = state.segStart;
  while (i < len) {
    const nl = raw.indexOf('\n', i);
    if (nl === -1) break;
    const trimmed = raw.slice(lineStart, nl).trim();
    if (trimmed.startsWith('```')) {
      if (fenceOpen) {
        // 闭合行：代码块到此完整，闭合行本身算块的一部分。
        fenceOpen = false;
        boundaries.push([segStart, nl + 1]);
        segStart = nl + 1;
      } else {
        fenceOpen = true;
      }
    } else if (!fenceOpen && trimmed === '') {
      // 空行边界；连续空行不产生空块（slice 区间必须非空）。
      if (lineStart > segStart && raw.slice(segStart, lineStart).trim() !== '') {
        boundaries.push([segStart, lineStart]);
      }
      segStart = nl + 1;
    }
    i = nl + 1;
    lineStart = i;
  }
  state.pos = i;
  state.fenceOpen = fenceOpen;
  state.segStart = segStart;
  return boundaries;
}
