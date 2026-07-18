# OMO 路由方案 A —— 9Router 代理模式（v8）

> **架构**：所有流量经 9Router → 9Router 按 chain 路由 + failover → 各 upstream provider。
>
> **v8 关键修正**：Free 模型在 OpenCode **Zen** gateway 下（不在 Go 下），走独立 API key，与 Go $60 池完全无关。两个 Go sub maxed 后真的什么都跑不了。Zen 余额可按量访问任何顶级模型（GPT-5.5 / Claude / Gemini / 全部开源）。

---

## 一、订阅资产盘点（v8 修正）

| 订阅 | 月费 | 用途 | v8 变化 |
|---|---|---|---|
| 智谱 Coding Plan Max 7 折 | ¥328 | GLM-4.7 / GLM-5.2 主路径 | 不变 |
| **ChatGPT Plus** | ¥145 | GPT-5 末档兜底 | ⚠️ 可考虑砍，Zen GPT-5.5 按量更便宜 |
| **OpenCode Go Sub-A** | $10 | $60 池已用完 | v7 误以为还能跑 free，实际 maxed 后失效 |
| **OpenCode Go Sub-B** | $10 | $60 池满血 | 不变 |
| **OpenCode Zen 余额** | 按量 | **5 个 free 模型 + 全目录零售访问** | 🆕 独立通道，重要补充 |
| MiniMax Plus | ¥49 | visual 主 + M3 兜底 | 不变 |
| DeepSeek V4 Pro 按量 | 按量 | Oracle/Prom 兜底 | 不变 |
| DeepSeek V4 Flash 按量 | 按量 | 终极兜底 | 不变 |
| 商汤 SenseNova free | free | 末档兜底 | 不变 |

---

## 二、9Router Upstreams 配置（v8）

```yaml
upstreams:
  # ===== 智谱 Coding Plan =====
  zhipu-coding:
    base_url: "https://open.bigmodel.cn/api/coding/paas/v4"
    api_key: "${ZHIPU_CODING_KEY}"
    models: [glm-4.7, glm-5.2]
    failover_on: [429]
    max_retries: 2
    retry_backoff: exponential

  # ===== OpenCode Zen（v8 新增，独立 gateway） =====
  # 5 个 free 模型零成本 + 余额可按量访问任何顶级模型
  zen:
    base_url: "https://opencode.ai/zen/v1"
    api_key: "${ZEN_API_KEY}"
    models:
      # ===== Free 档（零成本，与 Go 池无关） =====
      - opencode/deepseek-v4-flash-free
      - opencode/mimo-v2.5-free
      - opencode/nemotron-3-ultra-free
      - opencode/north-mini-code-free
      - opencode/big-pickle
      # ===== 余额按量档（仅在 chain 末位兜底时触发） =====
      - opencode/gpt-5.5          # $5/$30 per M
      - opencode/gpt-5.4-mini     # $0.75/$4.5 per M，性价比之选
      - opencode/claude-sonnet-5  # $2/$10 per M
      - opencode/gemini-3.1-pro   # $2/$12 per M
    failover_on: [402, 429]
    failover_on_timeout: 60
    max_retries: 1

  # ===== OpenCode Go Sub-A（已到上限，配置保留待重置） =====
  ocg-a:
    base_url: "https://opencode.ai/go/v1"
    api_key: "${OCG_KEY_A}"
    models:
      - opencode-go/qwen3.7-plus
      - opencode-go/qwen3.7-max
      - opencode-go/qwen3.6-plus
      - opencode-go/minimax-m3
      - opencode-go/minimax-m2.7
      - opencode-go/mimo-v2.5
      - opencode-go/mimo-v2.5-pro
      - opencode-go/deepseek-v4-pro
      - opencode-go/deepseek-v4-flash
      - opencode-go/kimi-k2.7-code
      - opencode-go/kimi-k2.6
      - opencode-go/glm-5.2
      - opencode-go/glm-5.1
    failover_on: [402, 429]
    failover_on_timeout: 60
    max_retries: 1

  # ===== OpenCode Go Sub-B（满血 $60 池） =====
  ocg-b:
    base_url: "https://opencode.ai/go/v1"
    api_key: "${OCG_KEY_B}"
    models:
      # 同 ocg-a，两个 sub 镜像，靠 failover 自动切换
      - opencode-go/qwen3.7-plus
      - opencode-go/qwen3.7-max
      - opencode-go/qwen3.6-plus
      - opencode-go/minimax-m3
      - opencode-go/minimax-m2.7
      - opencode-go/mimo-v2.5
      - opencode-go/mimo-v2.5-pro
      - opencode-go/deepseek-v4-pro
      - opencode-go/deepseek-v4-flash
      - opencode-go/kimi-k2.7-code
      - opencode-go/kimi-k2.6
      - opencode-go/glm-5.2
      - opencode-go/glm-5.1
    failover_on: [402, 429]
    failover_on_timeout: 60
    max_retries: 1

  # ===== ChatGPT Plus（9Router 内 OAuth 代理） =====
  # 可选 —— 若决定砍 Plus 改用 Zen GPT-5.5 按量，删除此 upstream
  plus-gpt5:
    base_url: "https://api.openai.com/v1"
    auth: oauth_session
    models: [gpt-5]
    failover_on: [429]
    max_retries: 1

  # ===== MiniMax Plus（独立池） =====
  minimax-plus:
    base_url: "https://api.minimax.chat/v1"
    api_key: "${MINIMAX_PLUS_KEY}"
    models: [abab6.5-chat, abab7-m3]
    failover_on: [429]
    failover_on_timeout: 90
    max_retries: 1

  # ===== DeepSeek 按量 =====
  deepseek-pro:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    models: [deepseek-v4-pro]
    max_retries: 2
    retry_backoff: exponential

  deepseek-flash:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    models: [deepseek-v4-flash]

  # ===== 商汤 free =====
  sensetime-free:
    base_url: "https://api.sensenova.cn/v1/openai"
    api_key: "${SENSETIME_KEY}"
    models: [SenseNova-Nano]
    failover_on: [429]
    max_retries: 2
    retry_backoff: exponential
```

---

## 三、9Router Chain 配置（v8，10 条）

> 设计原则（v8 修正）：
> - **HOT 路径压 zen 免费档**（与 Go 池完全无关，零成本）
> - **Go pool 模型走 ocg-a/b**（两个 sub 镜像，靠 failover 切换）
> - **智谱 prompts 主用于 Sisyphus / Hephaestus / Oracle / Prometheus**
> - **顶级闭源模型走 Zen 余额按量**（chain 末位兜底，控制触发频率）
> - **超时也触发 failover**

```yaml
chains:
  sisyphus:
    - { upstream: zhipu-coding, model: glm-4.7 }
    - { upstream: zen, model: opencode/deepseek-v4-flash-free }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-plus }
    - { upstream: ocg-a, model: opencode-go/qwen3.7-plus }
    - { upstream: zen, model: opencode/gpt-5.4-mini }

  prometheus:
    - { upstream: zhipu-coding, model: glm-5.2 }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-max }
    - { upstream: ocg-a, model: opencode-go/qwen3.7-max }
    - { upstream: deepseek-pro, model: deepseek-v4-pro }
    - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }

  oracle:
    - { upstream: zhipu-coding, model: glm-5.2 }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-max }
    - { upstream: ocg-a, model: opencode-go/qwen3.7-max }
    - { upstream: deepseek-pro, model: deepseek-v4-pro }
    - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }

  hephaestus:
    - { upstream: zhipu-coding, model: glm-4.7 }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-plus }
    - { upstream: ocg-a, model: opencode-go/qwen3.7-plus }
    - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }
    - { upstream: deepseek-pro, model: deepseek-v4-pro }

  librarian:
    - { upstream: zen, model: opencode/deepseek-v4-flash-free }
    - { upstream: zen, model: opencode/nemotron-3-ultra-free }
    - { upstream: zen, model: opencode/mimo-v2.5-free }
    - { upstream: ocg-b, model: opencode-go/mimo-v2.5 }
    - { upstream: sensetime-free, model: SenseNova-Nano }

  explore:
    - { upstream: ocg-b, model: opencode-go/minimax-m3 }
    - { upstream: ocg-a, model: opencode-go/minimax-m3 }
    - { upstream: zen, model: opencode/deepseek-v4-flash-free }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-plus }
    - { upstream: minimax-plus, model: abab7-m3 }

  reviewer:
    - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }
    - { upstream: ocg-a, model: opencode-go/kimi-k2.7-code }
    - { upstream: ocg-b, model: opencode-go/qwen3.7-plus }
    - { upstream: zhipu-coding, model: glm-4.7 }
    - { upstream: deepseek-pro, model: deepseek-v4-pro }

  quick:
    - { upstream: zen, model: opencode/mimo-v2.5-free }
    - { upstream: zen, model: opencode/deepseek-v4-flash-free }
    - { upstream: zen, model: opencode/nemotron-3-ultra-free }
    - { upstream: deepseek-flash, model: deepseek-v4-flash }

  visual:
    - { upstream: minimax-plus, model: abab7-m3 }
    - { upstream: ocg-b, model: opencode-go/minimax-m3 }
    - { upstream: ocg-a, model: opencode-go/minimax-m3 }
    - { upstream: plus-gpt5, model: gpt-5 }
    # 若砍 Plus，改：- { upstream: zen, model: opencode/gpt-5.4-mini }

  momus:
    reuse: oracle
  metis:
    reuse: oracle
```

---

## 四、opencode provider 配置

```json
{
  "provider": {
    "ninerouter": {
      "name": "9Router Unified",
      "apiKey": "sk-your-9router-key",
      "baseURL": "http://localhost:8080/v1"
    },
    "deepseek-emergency": {
      "name": "DeepSeek Emergency Direct",
      "apiKey": "${DEEPSEEK_API_KEY}",
      "baseURL": "https://api.deepseek.com/v1"
    }
  },
  "defaultProvider": "ninerouter",
  "defaultModel": "sisyphus"
}
```

---

## 五、OMO oh-my-openagent.json（normal 模式）

```json
{
  "agents": {
    "sisyphus":      { "model": "ninerouter/sisyphus" },
    "prometheus":    { "model": "ninerouter/prometheus" },
    "oracle":        { "model": "ninerouter/oracle" },
    "hephaestus":    { "model": "ninerouter/hephaestus" },
    "atlas":         { "model": "ninerouter/hephaestus" },
    "librarian":     { "model": "ninerouter/librarian" },
    "explore":       { "model": "ninerouter/explore" },
    "momus":         { "model": "ninerouter/oracle" },
    "metis":         { "model": "ninerouter/oracle" },
    "code-reviewer": { "model": "ninerouter/reviewer" }
  },
  "categories": {
    "visual-engineering": { "model": "ninerouter/visual" },
    "ultrabrain":         { "model": "ninerouter/oracle" },
    "deep":               { "model": "ninerouter/hephaestus" },
    "artistry":           { "model": "ninerouter/oracle" },
    "quick":              { "model": "ninerouter/quick" },
    "unspecified-high":   { "model": "ninerouter/hephaestus" },
    "unspecified-low":    { "model": "ninerouter/quick" },
    "writing":            { "model": "ninerouter/hephaestus" }
  }
}
```

---

## 六、OMO oh-my-openagent.emergency.json（9Router 挂时）

```json
{
  "agents": {
    "sisyphus":      { "model": "deepseek-emergency/deepseek-v4-pro" },
    "prometheus":    { "model": "deepseek-emergency/deepseek-v4-pro" },
    "oracle":        { "model": "deepseek-emergency/deepseek-v4-pro" },
    "hephaestus":    { "model": "deepseek-emergency/deepseek-v4-pro" },
    "atlas":         { "model": "deepseek-emergency/deepseek-v4-pro" },
    "librarian":     { "model": "deepseek-emergency/deepseek-v4-flash" },
    "explore":       { "model": "deepseek-emergency/deepseek-v4-flash" },
    "momus":         { "model": "deepseek-emergency/deepseek-v4-pro" },
    "metis":         { "model": "deepseek-emergency/deepseek-v4-pro" },
    "code-reviewer": { "model": "deepseek-emergency/deepseek-v4-pro" }
  },
  "categories": {
    "visual-engineering": {
      "model": "deepseek-emergency/deepseek-v4-flash",
      "_note": "DS 无多模态，emergency 模式下含图片输入应直接 reject"
    },
    "ultrabrain":       { "model": "deepseek-emergency/deepseek-v4-pro" },
    "deep":             { "model": "deepseek-emergency/deepseek-v4-pro" },
    "artistry":         { "model": "deepseek-emergency/deepseek-v4-pro" },
    "quick":            { "model": "deepseek-emergency/deepseek-v4-flash" },
    "unspecified-high": { "model": "deepseek-emergency/deepseek-v4-pro" },
    "unspecified-low":  { "model": "deepseek-emergency/deepseek-v4-flash" },
    "writing":          { "model": "deepseek-emergency/deepseek-v4-pro" }
  }
}
```

---

## 七、9Router 自愈（systemd + healthcheck）

### `/etc/systemd/system/ninerouter.service`

```ini
[Unit]
Description=9Router Unified LLM Gateway
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ninerouter --config /etc/ninerouter/config.yaml
Restart=always
RestartSec=5
WatchdogSec=30
NotifyAccess=main
Environment=NINEROUTER_HEALTH_PORT=8080

[Install]
WantedBy=multi-user.target
```

### `/usr/local/bin/omo-healthcheck.sh`

```bash
#!/usr/bin/env bash
set -u
HEALTH_URL="http://localhost:8080/health"
OMO_CFG="$HOME/.config/opencode/oh-my-openagent.json"
NORMAL_SRC="$HOME/.config/opencode/oh-my-openagent.normal.json"
EMERGENCY_SRC="$HOME/.config/opencode/oh-my-openagent.emergency.json"
FAILS=0
DEGRADED=0

while true; do
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    FAILS=0
    if [ "$DEGRADED" = "1" ]; then
      cp "$NORMAL_SRC" "$OMO_CFG"
      pkill -HUP opencode 2>/dev/null || true
      logger -t omo-healthcheck "9Router recovered, restored normal config"
      DEGRADED=0
    fi
  else
    FAILS=$((FAILS+1))
    if [ "$DEGRADED" = "0" ] && [ "$FAILS" -ge 3 ]; then
      cp "$EMERGENCY_SRC" "$OMO_CFG"
      pkill -HUP opencode 2>/dev/null || true
      logger -t omo-healthcheck "9Router down 3x, switched to emergency"
      DEGRADED=1
    fi
  fi
  sleep 10
done
```

---

## 八、订阅周期轮换（v8 简化）

Go sub 的 5h / 周 / 月限制重置后，sub-A 重新可用。**v8 推荐做法**：两个 sub 完全镜像配置（见上文），靠 `failover_on: [402]` 自动切。**不需要手动轮换**。

> **Free 模型与 sub 状态完全无关** —— 即使两个 Go sub 都 maxed，Zen 的 5 个 free 模型照常工作，HOT 路径（Librarian / Quick）始终有保障。

---

## 九、ChatGPT Plus 去留决策

**保留 Plus 的理由**：
- 重度使用 Deep Research / Sora 等 Plus 专属功能
- GPT-5 调用频率高（>500 次/月），Plus flat rate 更划算
- 不想管理 Zen 余额消耗

**砍 Plus 改 Zen 按量的理由**：
- 月省 ¥145
- GPT-5 仅做末档兜底，月触发 200-500 次，按量 ¥15-40 远低于 Plus
- 同时获得 GPT-5.5 / Claude Opus / Gemini Pro 等更多顶级模型访问权
- 不被 Plus 3h 窗口 80-100 msg 限制

**砍 Plus 后的 chain 修改**：

```yaml
# Sisyphus 末档
- { upstream: zen, model: opencode/gpt-5.4-mini }   # $0.75/$4.5 per M，性价比顶级
# 或更贵但更强
- { upstream: zen, model: opencode/gpt-5.5 }         # $5/$30 per M

# Visual 末档
- { upstream: zen, model: opencode/gpt-5.4-mini }
```

并在 9Router 删除 `plus-gpt5` upstream。

---

## 十、9 月底 GLM-5.2 红利到期迁移

只动 9Router chain：

```yaml
prometheus:
  - { upstream: deepseek-pro, model: deepseek-v4-pro }
  - { upstream: ocg-b, model: opencode-go/qwen3.7-max }
  - { upstream: ocg-a, model: opencode-go/qwen3.7-max }
  - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }
  - { upstream: zhipu-coding, model: glm-5.2 }

oracle:
  - { upstream: deepseek-pro, model: deepseek-v4-pro }
  - { upstream: ocg-b, model: opencode-go/qwen3.7-max }
  - { upstream: ocg-a, model: opencode-go/qwen3.7-max }
  - { upstream: ocg-b, model: opencode-go/kimi-k2.7-code }
  - { upstream: zhipu-coding, model: glm-5.2 }
```

---

## 十一、上线 P0 验证清单

| # | 任务 | 验收 |
|---|---|---|
| 1 | 登 Zen 控制台查 5 个 free 模型的 RPM 限制 | 拿到具体数字 |
| 2 | 测 Zen 控制台 "Use balance" 是否开启 + 月度限额设置 | 防止余额意外烧光 |
| 3 | 9Router 配 zen + ocg-a + ocg-b 三个 upstream | `curl localhost:8080/v1/models` 返回所有 chain alias |
| 4 | 测 `failover_on_timeout: 60` | 故意构造超时 |
| 5 | systemd ninerouter.service + omo-healthcheck 部署 | kill ninerouter → 30s 内切 emergency |
| 6 | 测 Zen 余额 GPT-5.4 Mini 单次调用 | 确认按量计费通路畅通 |
| 7 | 决定 Plus 去留 | 看 Deep Research / Sora 实际使用频率 |

---

## 十二、预算复核（v8）

**保留 Plus**：

| 项目 | 月费 |
|---|---|
| 智谱 Coding Plan | ¥328 |
| ChatGPT Plus | ¥145 |
| OCG × 2 | $20 ≈ ¥140 |
| MiniMax Plus | ¥49 |
| DeepSeek V4 Pro 按量 | ¥60-120 |
| DeepSeek V4 Flash 按量 | ¥5-10 |
| Zen 余额（顶级模型末档兜底）| ¥10-30 |
| **合计** | **¥737-822** |

**砍 Plus**：

| 项目 | 月费 |
|---|---|
| 智谱 Coding Plan | ¥328 |
| OCG × 2 | ¥140 |
| MiniMax Plus | ¥49 |
| DeepSeek V4 Pro 按量 | ¥60-120 |
| DeepSeek V4 Flash 按量 | ¥5-10 |
| **Zen 余额（GPT-5.4 Mini 末档 + 偶发顶级）** | **¥30-80** |
| **合计** | **¥612-727** |

砍 Plus 省 ¥100-125/月，且解锁更多顶级模型访问。除非重度依赖 Deep Research / Sora，否则推荐砍。
