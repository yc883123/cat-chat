// ============================================================
// 26-model-skin.js —— 模型家族/档位识别与皮肤映射（唯一真源）
//
// 为什么单独一个模块：映射规则一旦散进事件处理代码，改一个档位词就要翻三处。
// 这里只做纯函数（模型名 → 皮肤键），不碰 DOM、不碰状态，便于单测直接跑。
//
// 匹配口径（与 plan-model-skin-auto-switch.md 一致）：
//   1. 转小写，在字母与数字交界处插空格 → `gemini31flash` 变 `gemini 31 flash`。
//   2. 按非字母数字切分，**丢弃纯数字 token**（版本号/参数量/日期都在此列）。
//      所以 `gpt-6.1-sol` 的 `6.1` 不参与判断，版本升级 5.6→6.1→7.0 不用改规则。
//   3. 家族词必须作为**独立 token** 命中，不做子串包含——否则 `Sakimichan` 会撞 claude。
//   4. 档位判定顺序：轻量词 → 高阶词 → 该族标准档 → 该族高阶档。
//      先查轻量是为了 `MiniMax-M2-Flash` 不被系列名 `m` 抢先判成高阶。
//   5. 家族词与档位词都没命中，或家族不在九个之内 → 返回空串（调用方保持当前皮肤）。
// ============================================================

// 家族词 → 家族键。词表只放「能被独立 token 命中」的写法。
const FAMILY_WORDS = {
  deepseek: ['deepseek'],
  gemini: ['gemini'],
  claude: ['claude'],
  gpt: ['gpt', 'chatgpt'],
  grok: ['grok'],
  glm: ['glm', 'chatglm', 'zhipu', 'codegeex'],
  kimi: ['kimi', 'moonshot'],
  minimax: ['minimax', 'hailuo', 'abab'],
  qwen: ['qwen', 'tongyi'],
};

// 轻量档词（先判）。
const LOW_TIER_WORDS = {
  deepseek: ['flash', 'mini', 'lite'],
  gemini: ['flash', 'lite'],
  claude: ['haiku', 'sonnet'],
  gpt: ['luna', 'terra', 'mini', 'nano'],
  grok: ['mini', 'fast', 'lite'],
  glm: ['flash', 'air'],
  kimi: ['flash', 'air', 'mini', 'turbo'],
  minimax: ['flash'],
  qwen: ['flash', 'lite'],
};

// 高阶档词（后判）。注意：minimax 的 m / m2 是系列名不是档位词，绝不能放进这里，
// 否则 `MiniMax-M2` 会被当成高阶（那正是它的标准档）。
const HIGH_TIER_WORDS = {
  deepseek: ['pro', 'reasoner', 'r1'],
  gemini: ['pro', 'ultra'],
  claude: ['opus', 'fable', 'mythos'],
  gpt: ['sol', 'astra'],
  grok: ['heavy'],
  glm: ['pro'],
  kimi: ['pro'],
  minimax: [],
  qwen: ['max'],
};

// 家族 × 档位 → 皮肤键。档位只有 standard / low / high 三种，
// 缺哪一格就是「该家族没有这个档位的图」，由 resolveModelSkin 做回落。
const FAMILY_SKINS = {
  deepseek: { standard: '01_deepseek', low: '02_deepseek_mini' },
  gemini: { high: '03_gemini_pro', low: '04_gemini_flash' },
  claude: { high: '05_claude_opus_fable_mythos', low: '06_claude_haiku_sonnet' },
  gpt: { high: '07_gpt_sol_astra', low: '08_gpt_luna_terra' },
  grok: { high: '09_grok_heavy', low: '10_grok_mini_fast' },
  glm: { standard: '11_glm', low: '12_glm_flash' },
  kimi: { high: '13_kimi_pro', standard: '14_kimi' },
  minimax: { standard: '15_minimax_m', low: '16_minimax_m_flash' },
  qwen: { standard: '17_qwen', low: '18_qwen_flash' },
};

// 非对话模型词：带家族名但是画图 / 语音 / 向量 / 视频模型。
// 不排除的话，用户切到画图模型时头像会突然变成无关角色。
const NON_CHAT_WORDS = [
  'image', 'video', 'tts', 'audio', 'speech', 'embed', 'rerank',
  'asr', 'whisper', 'sora', 'imagine', 'ocr', 'music', 'voice',
];

function tokenize(modelId) {
  const lowered = String(modelId || '').toLowerCase();
  if (!lowered) return [];
  const spaced = lowered
    .replace(/([a-z])([0-9])/g, '$1 $2')
    .replace(/([0-9])([a-z])/g, '$1 $2');
  return spaced.split(/[^a-z0-9]+/).filter((token) => token && !/^[0-9.]+$/.test(token));
}

function familyOf(tokens) {
  for (const [family, words] of Object.entries(FAMILY_WORDS)) {
    if (tokens.some((token) => words.includes(token))) return family;
  }
  return '';
}

function hasAnyWord(tokens, words) {
  return words.some((word) => tokens.includes(word));
}

/** 模型名是否属于「画出图片/语音」这类不换肤的模型。 */
export function isNonChatModel(modelId) {
  const lowered = String(modelId || '').toLowerCase();
  return NON_CHAT_WORDS.some((word) => lowered.includes(word));
}

/**
 * 模型 ID → 皮肤键。识别不出来时返回 ''（调用方应保持当前皮肤，不做任何猜测）。
 *
 * 档位回落规则（18 张图只有 9 族 × 2 档的必然结果）：
 * - 带高阶词但该族无高阶图 → 回落该族标准档。
 * - 带轻量词但该族无轻量图 → 回落该族标准档。
 * - 无档位词 → 有标准档用标准档；没有标准档就用高阶档（产品拍板：默认偏高阶）。
 */
export function resolveModelSkin(modelId) {
  const raw = String(modelId || '').trim();
  if (!raw) return '';
  // 非对话模型先排除：它们也带家族词，但换肤毫无意义。
  if (isNonChatModel(raw)) return '';
  const tokens = tokenize(raw);
  const family = familyOf(tokens);
  if (!family) return '';
  const map = FAMILY_SKINS[family] || {};

  if (hasAnyWord(tokens, LOW_TIER_WORDS[family] || [])) {
    return map.low || map.standard || map.high || '';
  }
  if (hasAnyWord(tokens, HIGH_TIER_WORDS[family] || [])) {
    return map.high || map.standard || map.low || '';
  }
  // 无档位词：标准档优先，没有标准档的家族按拍板结果偏高阶。
  return map.standard || map.high || map.low || '';
}
