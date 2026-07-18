# OMO 路由方案 B —— 直连模式（v8，无 9Router）

> **架构**：opencode 直连各 provider，OMO `fallback_models` 字段实现 failover。
>
> **v8 关键修正**：
> 1. **Free 模型在 Zen gateway 下**（不在 Go 下），走独立 `opencode/<model>-free` 路径
> 2. **直连不再失去 GPT-5 / Claude / Gemini** —— Zen 余额按量可访问任何顶级模型
> 3. **DIRECT 模式不再需要混合方案** —— 全功能直连即可
>
> **优势**：无 SPOF / 少一跳延迟 / 栈更简单 / 不需要维护 9Router 进程 / 仍能用所有顶级模型。
> **代价**：失去 timeout-based failover / 失去集中可观测性 / 失去提示词缓存跨 failover 友好性 / 配置散落多文件。

---

## 一、订阅资产盘点（直连 v8）

| 订阅 | 月费 | 直连可用？ | 备注 |
|---|---|---|---|
| 智谱 Coding Plan | ¥328 | ✅ | 原生 OpenAI 兼容 |
| **ChatGPT Plus** | ¥145 | ❌（除非另起 OAuth 代理） | **建议砍，Zen GPT-5.5 按量更便宜** |
| OCG Sub-A | $10 | ✅ | OpenAI 兼容 |
| OCG Sub-B | $10 | ✅ | OpenAI 兼容 |
| **OpenCode Zen 余额** | 按量 | ✅ | **5 个 free + 全目录顶级模型按量** |
| MiniMax Plus | ¥49 | ✅ | OpenAI 兼容 |
| DeepSeek Pro 按量 | 按量 | ✅ | OpenAI 兼容 |
| DeepSeek Flash 按量 | 按量 | ✅ | OpenAI 兼容 |
| 商汤 SenseNova free | free | ✅ | OpenAI 兼容 |

**v8 关键解锁**：直连模式通过 **Zen 余额**照样能用 GPT-5.5 / Claude Opus / Gemini Pro 等顶级模型，按量计费即可。直连模式 vs 代理模式的能力差距大幅缩小。

---

## 二、opencode provider 配置（直连 v8）

```json
{
  "provider": {
    "zhipu-coding": {
      "name": "Zhipu Coding Plan",
      "apiKey": "${ZHIPU_CODING_KEY}",
      "baseURL": "https://open.bigmodel.cn/api/coding/paas/v4"
    },
    "zen": {
      "name": "OpenCode Zen (free + pay-per-use)",
      "apiKey": "${ZEN_API_KEY}",
      "baseURL": "https://opencode.ai/zen/v1"
    },
    "ocg-a": {
      "name": "OpenCode Go Sub A",
      "apiKey": "${OCG_KEY_A}",
      "baseURL": "https://opencode.ai/go/v1"
    },
    "ocg-b": {
      "name": "OpenCode Go Sub B",
      "apiKey": "${OCG_KEY_B}",
      "baseURL": "https://opencode.ai/go/v1"
    },
    "minimax-plus": {
      "name": "MiniMax Plus",
      "apiKey": "${MINIMAX_PLUS_KEY}",
      "baseURL": "https://api.minimax.chat/v1"
    },
    "deepseek-pro": {
      "name": "DeepSeek Pro",
      "apiKey": "${DEEPSEEK_API_KEY}",
      "baseURL": "https://api.deepseek.com/v1"
    },
    "deepseek-flash": {
      "name": "DeepSeek Flash",
      "apiKey": "${DEEPSEEK_API_KEY}",
      "baseURL": "https://api.deepseek.com/v1"
    },
    "sensetime-free": {
      "name": "SenseTime Free",
      "apiKey": "${SENSETIME_KEY}",
      "baseURL": "https://api.sensenova.cn/v1/openai"
    }
  },
  "defaultProvider": "zhipu-coding",
  "defaultModel": "glm-4.7"
}
```

> **环境变量**（7 个 API key，多了 `ZEN_API_KEY`）：
> ```bash
> export ZHIPU_CODING_KEY="..."
> export ZEN_API_KEY="sk-..."          # 🆕 v8 新增
> export OCG_KEY_A="sk-..."            # Sub-A
> export OCG_KEY_B="sk-..."            # Sub-B
> export MINIMAX_PLUS_KEY="..."
> export DEEPSEEK_API_KEY="..."
> export SENSETIME_KEY="..."
> ```

---

## 三、OMO oh-my-openagent.json（直连 v8）

> 每个 chain 设计：智谱主 + Zen 免费兜底 + Go sub-a/b 镜像兜底 + Zen 余额顶级末档 + DeepSeek 按量超末档。

```json
{
  "agents": {
    "sisyphus": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "zen/deepseek-v4-flash-free",
        "ocg-b/qwen3.7-plus",
        "ocg-a/qwen3.7-plus",
        "zen/gpt-5.4-mini",
        "deepseek-pro/deepseek-v4-pro"
      ]
    },

    "prometheus": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "ocg-a/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "ocg-b/kimi-k2.7-code",
        "zen/gpt-5.4-mini"
      ]
    },

    "oracle": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "ocg-a/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "ocg-b/kimi-k2.7-code",
        "zen/gpt-5.5"
      ]
    },

    "hephaestus": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "ocg-b/qwen3.7-plus",
        "ocg-a/qwen3.7-plus",
        "ocg-b/kimi-k2.7-code",
        "deepseek-pro/deepseek-v4-pro"
      ]
    },

    "atlas": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "ocg-b/qwen3.7-plus",
        "ocg-a/qwen3.7-plus",
        "ocg-b/kimi-k2.7-code",
        "deepseek-pro/deepseek-v4-pro"
      ]
    },

    "librarian": {
      "model": "zen/deepseek-v4-flash-free",
      "fallback_models": [
        "zen/nemotron-3-ultra-free",
        "zen/mimo-v2.5-free",
        "zen/big-pickle",
        "ocg-b/mimo-v2.5",
        "sensetime-free/SenseNova-Nano"
      ]
    },

    "explore": {
      "model": "ocg-b/minimax-m3",
      "fallback_models": [
        "ocg-a/minimax-m3",
        "zen/deepseek-v4-flash-free",
        "ocg-b/qwen3.7-plus",
        "minimax-plus/abab7-m3"
      ]
    },

    "momus": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "ocg-b/kimi-k2.7-code"
      ]
    },

    "metis": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "ocg-b/kimi-k2.7-code"
      ]
    },

    "code-reviewer": {
      "model": "ocg-b/kimi-k2.7-code",
      "fallback_models": [
        "ocg-a/kimi-k2.7-code",
        "ocg-b/qwen3.7-plus",
        "zhipu-coding/glm-4.7",
        "deepseek-pro/deepseek-v4-pro"
      ]
    }
  },

  "categories": {
    "visual-engineering": {
      "model": "minimax-plus/abab7-m3",
      "fallback_models": [
        "ocg-b/minimax-m3",
        "ocg-a/minimax-m3",
        "zen/gpt-5.4-mini"
      ]
    },
    "ultrabrain": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "zen/gpt-5.5",
        "zen/claude-sonnet-5"
      ]
    },
    "deep": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "ocg-b/qwen3.7-plus",
        "ocg-b/kimi-k2.7-code",
        "deepseek-pro/deepseek-v4-pro"
      ]
    },
    "artistry": {
      "model": "zhipu-coding/glm-5.2",
      "fallback_models": [
        "ocg-b/qwen3.7-max",
        "deepseek-pro/deepseek-v4-pro",
        "zen/claude-sonnet-5"
      ]
    },
    "quick": {
      "model": "zen/mimo-v2.5-free",
      "fallback_models": [
        "zen/deepseek-v4-flash-free",
        "zen/nemotron-3-ultra-free",
        "deepseek-flash/deepseek-v4-flash"
      ]
    },
    "unspecified-high": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "ocg-b/qwen3.7-plus",
        "deepseek-pro/deepseek-v4-pro"
      ]
    },
    "unspecified-low": {
      "model": "zen/mimo-v2.5-free",
      "fallback_models": [
        "zen/deepseek-v4-flash-free",
        "deepseek-flash/deepseek-v4-flash"
      ]
    },
    "writing": {
      "model": "zhipu-coding/glm-4.7",
      "fallback_models": [
        "ocg-b/qwen3.7-plus",
        "deepseek-pro/deepseek-v4-pro"
      ]
    }
  }
}
```

---

## 四、各 chain 设计依据（直连 v8）

| chain | 主 | 设计依据 |
|---|---|---|
| **sisyphus** | `zhipu/glm-4.7` | HOT + 中注意力 + 零边际 = GLM-4.7 唯一解；末档 Zen GPT-5.4 Mini 性价比顶级 |
| **prometheus / oracle** | `zhipu/glm-5.2` | COLD + 高推理；Oracle 末档配 GPT-5.5 / Sonnet 5 应对极端场景 |
| **hephaestus** | `zhipu/glm-4.7` | prompts 制甜点 + Qwen Plus 256K 长程兜底 |
| **librarian** | `zen/deepseek-v4-flash-free` | bounded + 极便宜，全档走 Zen 免费档 |
| **explore** | `ocg-b/minimax-m3` | 浅检索 + 多模态可能 |
| **reviewer** | `ocg-b/kimi-k2.7-code` | OCG 池里唯一编码专精 |
| **quick** | `zen/mimo-v2.5-free` | trivial + 三 Zen 免费档串联 |
| **visual** | `minimax-plus/abab7-m3` | MiniMax M3 主 + Zen GPT-5.4 Mini 兜底（替代 Plus） |
| **momus / metis** | `zhipu/glm-5.2` | 复用 oracle 路径 |
| **ultrabrain** | `zhipu/glm-5.2` | 末档可触达 Zen GPT-5.5 / Claude Sonnet 5（极端复杂场景） |

---

## 五、直连模式的限制（v8 修正后）

### 1. **没有 timeout-based failover**

OMO 的 `fallback_models` 通常只在 HTTP 错误码（429/500/超时整体失败）时切换。

**缓解**：在 opencode provider 层设置 timeout（如 `timeout: 30000`），让超时变成失败触发 fallback。具体字段名查 opencode 文档。

### 2. **失去集中可观测性**

直连模式下：
- 智谱 prompts 消耗：登录智谱控制台
- OCG 池消耗：登录 OCG 控制台（两个 sub 分别看）
- **Zen 余额消耗：登录 Zen 控制台**（设置月度限额防意外烧光）
- DeepSeek 按量：登录 DeepSeek 控制台
- 各项分别手工对账

**重要**：**Zen 控制台设置月度限额**（如 $20），防止某条 chain 失控连续触发顶级模型按量。

### 3. **提示词缓存失效风险**

OMO 切 fallback 时切到不同 provider，缓存前缀失效。**Zen 内同模型家族切换可保持缓存**（如 zen/deepseek-v4-flash-free → zen/gpt-5.4-mini 都是 zen gateway，缓存可能延续；具体看 Zen 实现）。

### 4. **sub-A / sub-B 自动轮换**

直连模式下需要在 `fallback_models` 里同时列两个 sub：

```json
"fallback_models": [
  "ocg-b/qwen3.7-plus",
  "ocg-a/qwen3.7-plus",   // 🆕 sub-b 402 时自动切 sub-a
  ...
]
```

v8 OMO 配置已采用此模式。

---

## 六、ChatGPT Plus 砍掉的具体收益（v8 重点）

按量算账（用户画像：GPT-5 主要做末档兜底，月触发 200-500 次）：

| 方案 | 月费 | 含 GPT-5？ | 其他顶级模型？ |
|---|---|---|---|
| 保留 Plus | ¥145 flat | ✅ GPT-5 | ❌ 只有 GPT-5 |
| 砍 Plus，Zen GPT-5.4 Mini 按量 | **¥3-8** | ✅ GPT-5.4 Mini（略弱）| ✅ 还能用 GPT-5.5 / Claude / Gemini |
| 砍 Plus，Zen GPT-5.5 按量 | **¥15-40** | ✅ GPT-5.5（更强）| ✅ 全部顶级 |

**砍 Plus 是 v8 直连模式的标准推荐**：
- 月省 ¥100+
- 解锁 Claude Sonnet 5 / Opus 4.8 / Gemini 3.1 Pro / GPT-5.5 全部顶级模型
- 不被 Plus 3h 窗口限制
- 唯一代价是失去 Deep Research / Sora（如果不用就无所谓）

---

## 七、直连 vs 代理对比（v8 修正后）

| 维度 | 代理（9Router） | 直连（v8） |
|---|---|---|
| **SPOF** | 9Router 是 SPOF（需 systemd 自愈）| 无 SPOF |
| **延迟** | 多一跳（通常 +10-50ms）| 直连最低 |
| **顶级模型访问** | ✅ 通过 9Router 路由到 Zen | ✅ 直接走 Zen 余额 |
| **timeout failover** | ✅ 可配 `failover_on_timeout` | ⚠️ 取决于 OMO 实现 |
| **提示词缓存** | ✅ 同家族跨 failover 保持 | ⚠️ Zen 内部可能保持，跨 gateway 失效 |
| **集中可观测性** | ✅ 统一日志 | ❌ 各家控制台分别看 |
| **sub-A/B 自动轮换** | ✅ failover 自动 | ⚠️ 冗余 fallback 显式列出 |
| **配置位置** | 9Router 一处 | OMO + opencode + env 多处 |
| **进程维护** | 9Router 进程 + healthcheck | 无 |
| **栈复杂度** | 多一层 | 简洁 |
| **Plus 必要性** | 9Router OAuth 代理可保留 | Zen 按量替代，可砍 |

**v8 后直连模式的能力大幅接近代理模式**。两者主要差距收敛到：
- 集中可观测性（代理胜）
- timeout 主动 failover（代理胜）
- 栈简洁度（直连胜）
- 无 SPOF（直连胜）

---

## 八、上线 P0 验证清单（直连 v8）

| # | 任务 | 验收 |
|---|---|---|
| 1 | 测 OMO `fallback_models` 字段触发条件 | 是只看 HTTP 错误码，还是也看 timeout？ |
| 2 | opencode provider 是否支持 `timeout` 字段 | 决定超时能否被触发为失败 |
| 3 | 设置环境变量（7 个 API key） | `echo $ZEN_API_KEY` 等都能输出 |
| 4 | Zen 控制台设置月度限额（如 $20-30）| 防止 chain 失控烧光余额 |
| 5 | 每个 agent 单发一条测试 prompt | 主路径 200 通过 |
| 6 | 手动让主路径失败（key 临时改错）| fallback 链按顺序触发 |
| 7 | 登 Zen 控制台查 5 个 free 模型 RPM | 决定 Quick/Librarian 主档选择 |
| 8 | 测 Zen 余额 GPT-5.4 Mini 单次调用 | 确认按量计费通路畅通 |
| 9 | 决定 Plus 去留 | 看 Deep Research / Sora 实际使用频率 |

---

## 九、预算复核（直连 v8）

**保留 Plus**：

| 项目 | 月费 |
|---|---|
| 智谱 Coding Plan | ¥328 |
| ChatGPT Plus | ¥145 |
| OCG × 2 | ¥140 |
| MiniMax Plus | ¥49 |
| DeepSeek V4 Pro 按量 | ¥60-120 |
| DeepSeek V4 Flash 按量 | ¥5-10 |
| Zen 余额（顶级模型末档）| ¥10-30 |
| **合计** | **¥737-822** |

**砍 Plus（推荐）**：

| 项目 | 月费 |
|---|---|
| 智谱 Coding Plan | ¥328 |
| OCG × 2 | ¥140 |
| MiniMax Plus | ¥49 |
| DeepSeek V4 Pro 按量 | ¥60-120 |
| DeepSeek V4 Flash 按量 | ¥5-10 |
| **Zen 余额（GPT-5.4 Mini 主末档 + 偶发顶级）** | **¥30-80** |
| **合计** | **¥612-727** |

直连 + 砍 Plus 是最省方案，且能力无明显损失。

---

## 十、9 月底 GLM-5.2 红利到期迁移

直连模式下需修改 OMO `oh-my-openagent.json`（不能像代理模式那样只动一处）：

```json
"prometheus": {
  "model": "deepseek-pro/deepseek-v4-pro",
  "fallback_models": [
    "ocg-b/qwen3.7-max",
    "ocg-a/qwen3.7-max",
    "ocg-b/kimi-k2.7-code",
    "zen/gpt-5.4-mini",
    "zhipu-coding/glm-5.2"
  ]
},
"oracle": {
  "model": "deepseek-pro/deepseek-v4-pro",
  "fallback_models": [
    "ocg-b/qwen3.7-max",
    "ocg-a/qwen3.7-max",
    "ocg-b/kimi-k2.7-code",
    "zen/gpt-5.5",
    "zhipu-coding/glm-5.2"
  ]
}
```

需修改 prometheus / oracle / momus / metis 四个角色 + ultrabrain / artistry 两个 category。这是直连模式比代理模式繁琐的地方。

---

## 十一、总结：v8 后的两个方案选哪个

| 你的偏好 | 推荐方案 |
|---|---|
| 重度用户，要集中观测，chain 经常调，可维护进程 | **代理（PROXY）** |
| 中度用户，追求栈简洁，能接受手工对账 | **直连（DIRECT）** |
| 想最小化 SPOF | **直连** |
| 经常需要实验性调整 chain 顺序 | **代理** |
| **想砍 ChatGPT Plus 省钱** | **两个都行，直连略简单** |
| 不在乎 Plus 钱，要 Deep Research / Sora | **代理（OAuth 处理更顺）** |

**对你（重度多 Agent + 两个 OCG sub + Zen 余额可用）的 v8 推荐**：
- **想省心**：**代理方案 A**，集中管理 + 自动 failover + 9 月底切换无痛
- **想省钱 + 栈简洁**：**直连方案 B + 砍 Plus**，月省 ¥100+，能力无明显损失

两个方案都是 v8 修正后的完整版，可直接落地。
