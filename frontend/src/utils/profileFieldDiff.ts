/**
 * 档案字段「旧值 → 新值」的可读差异摘要（纯前端展示，不改计划数据）。
 *
 * 背景：档案字段是 AI 依据事实整段重渲染的，很多变化不是「增删一条」而是整段改写。
 * 因此这里先按字符级 LCS 算相似度，再把变更片段扩展到最近的分句边界（、，。；换行）：
 * - 高相似度（列表式增删）：折叠态只显示「− 删除片段 / ＋ 新增片段」；
 * - 低相似度（整段重写）：折叠态只显示字数变化，避免满屏红绿碎片，展开看旧/新全文。
 */

export type FieldChangeMode = 'delta' | 'rewrite';

export interface FieldChangeSummary {
  mode: FieldChangeMode;
  /** 旧值里被删除的分句（已按分隔符扩到完整片段） */
  removed: string[];
  /** 新值里新增的分句 */
  added: string[];
  /** 2 * LCS 长度 / (旧长度 + 新长度)，0–1 */
  similarity: number;
  oldLength: number;
  newLength: number;
}

const DELIMITERS = new Set([
  '、', '，', ',', '；', ';', '。', '\n', '！', '!', '？', '?',
]);

/** 超过该字符数不做字符级 diff（避免长字段的 O(n*m) 与碎片化展示） */
const MAX_DIFF_CHARS = 2000;
/** 低于该相似度视为「整段重写」，不做逐片段展示 */
const DELTA_SIMILARITY_THRESHOLD = 0.6;
/** 变化片段过多时同样降级为「整段重写」，避免摘要比全文还长 */
const MAX_DELTA_CHUNKS = 10;

interface TextRun {
  start: number;
  text: string;
}

interface CharDiff {
  equalChars: number;
  deletes: TextRun[];
  inserts: TextRun[];
}

function pushRun(runs: TextRun[], start: number, char: string): void {
  const last = runs[runs.length - 1];
  if (last && last.start + last.text.length === start) {
    last.text += char;
  } else {
    runs.push({ start, text: char });
  }
}

/** 字符级 LCS diff：返回相等字符数与删除/新增片段（带各自字符串内偏移）。 */
function diffChars(oldText: string, newText: string): CharDiff {
  const n = oldText.length;
  const m = newText.length;
  const width = m + 1;
  const dp = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    const row = i * width;
    const nextRow = (i + 1) * width;
    for (let j = m - 1; j >= 0; j -= 1) {
      if (oldText[i] === newText[j]) {
        dp[row + j] = dp[nextRow + j + 1] + 1;
      } else {
        dp[row + j] = Math.max(dp[nextRow + j], dp[row + j + 1]);
      }
    }
  }

  const deletes: TextRun[] = [];
  const inserts: TextRun[] = [];
  let equalChars = 0;
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (oldText[i] === newText[j]) {
      equalChars += 1;
      i += 1;
      j += 1;
    } else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) {
      pushRun(deletes, i, oldText[i]);
      i += 1;
    } else {
      pushRun(inserts, j, newText[j]);
      j += 1;
    }
  }
  while (i < n) {
    pushRun(deletes, i, oldText[i]);
    i += 1;
  }
  while (j < m) {
    pushRun(inserts, j, newText[j]);
    j += 1;
  }
  return { equalChars, deletes, inserts };
}

/** 把变更片段扩展到最近的分句边界，得到用户能读懂的整句（而不是半个词）。 */
function expandToSegment(text: string, start: number, end: number): { start: number; end: number } {
  let from = start;
  if (from < text.length && DELIMITERS.has(text[from])) from += 1;
  while (from > 0 && !DELIMITERS.has(text[from - 1])) from -= 1;
  let to = end;
  while (to < text.length && !DELIMITERS.has(text[to])) to += 1;
  return { start: from, end: to };
}

function trimChunk(text: string): string {
  return text
    .replace(/^[、，,；;。！？!?\s]+/u, '')
    .replace(/[、，,；;。！？!?\s]+$/u, '')
    .trim();
}

function collectChunks(text: string, runs: TextRun[]): string[] {
  const ranges = runs
    .map(run => expandToSegment(text, run.start, run.start + run.text.length))
    .sort((left, right) => left.start - right.start);

  const merged: Array<{ start: number; end: number }> = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range.start <= last.end) {
      last.end = Math.max(last.end, range.end);
    } else {
      merged.push({ ...range });
    }
  }

  const chunks = merged
    .map(range => trimChunk(text.slice(range.start, range.end)))
    .filter(chunk => chunk.length > 0);
  // 多个不连续变更可能扩展到同一分句，去重后保持原顺序
  return Array.from(new Set(chunks));
}

export function summarizeFieldChange(
  oldValue?: string | null,
  newValue?: string | null,
): FieldChangeSummary {
  const oldText = (oldValue ?? '').trim();
  const newText = (newValue ?? '').trim();
  const base = {
    oldLength: oldText.length,
    newLength: newText.length,
  };

  if (!oldText || !newText || oldText === newText || oldText.length + newText.length > MAX_DIFF_CHARS) {
    return {
      ...base,
      mode: 'rewrite',
      removed: [],
      added: [],
      similarity: 0,
    };
  }

  const diff = diffChars(oldText, newText);
  const similarity = (2 * diff.equalChars) / (oldText.length + newText.length);
  const removed = collectChunks(oldText, diff.deletes);
  const added = collectChunks(newText, diff.inserts);
  const changedChunks = removed.length + added.length;
  const mode: FieldChangeMode = similarity >= DELTA_SIMILARITY_THRESHOLD
    && changedChunks > 0
    && changedChunks <= MAX_DELTA_CHUNKS
    ? 'delta'
    : 'rewrite';

  return {
    ...base,
    mode,
    // 只有 delta 模式才暴露片段；rewrite 模式由调用方展示「整段重写」，不展示红绿碎片
    removed: mode === 'delta' ? removed : [],
    added: mode === 'delta' ? added : [],
    similarity,
  };
}
