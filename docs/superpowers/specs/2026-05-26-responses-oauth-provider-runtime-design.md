# Responses OAuth Provider Runtime Design

Date: 2026-05-26

## Goal

把 Loom 的 Codex OAuth 與 xAI OAuth runtime 從 `providers.py` 中切乾淨，讓兩條
Responses-style provider 路徑各自穩定、可觀測、可測試，最後只向上層輸出同一份
標準 `LLMResponse` contract。

這次修的是「OAuth provider runtime 邊界」而不是登入功能本身。登入/refresh helper
已經存在；問題出在長任務、大 context、推理期、SSE lifecycle、錯誤分類與停滯診斷。

## Scope

In scope:

- Codex OAuth chat runtime。
- xAI OAuth chat runtime。
- Responses SSE 解析、phase telemetry、TTFB/idle diagnostics。
- Provider-specific payload policy 與 error classification。
- Focused unit tests。
- 使用現有 OAuth 憑證跑實際 Codex/xAI LLM smoke test。

Out of scope:

- MiniMax/Anthropic runtime 重構。
- 通用 provider registry 大改。
- credential pool 系統。
- image/TTS/tool provider 的 OAuth resolver 統一。
- proxy server 或 OpenAI-compatible localhost endpoint。

MiniMax/Anthropic 目前穩定使用中，本 PR 不碰。後續可開 follow-up issue，把
Anthropic/MiniMax 也抽成同一個 provider contract，但必須等 Codex/xAI 先穩定。

## Current Context

`loom/core/cognition/` 目前有：

- `openai_auth.py`：Codex CLI OAuth credential loader，也服務 OpenAI 工具 path。
- `xai_auth.py`：xAI OAuth login / refresh / credential loader，OAuth-only。
- `providers.py`：同時包含 Anthropic/MiniMax、OpenAI-compatible、Codex Responses、
  xAI Responses 等 provider 實作，檔案已過大。

沒有 generic `oauth.py`。因此新 runtime 檔案不應命名為 `oauth.py`，也不應把
request payload、SSE handling、diagnostics 塞進 `openai_auth.py` 或 `xai_auth.py`。
Auth helper 只管 credential；runtime module 才管 provider request/stream behavior。

## Design Principles

每個 provider 自己整理自己的世界。

Codex 修補不應改到 xAI。xAI entitlement 或 SSE 差異不應污染 Codex。共享層只放
不帶 provider 意義的機械工具，例如 SSE frame parsing、phase tracking、normalized
event container。

上層 `LoomSession` 不應知道 provider 細節。它只收到：

- text delta
- final `LLMResponse`
- tool uses
- usage
- normalized stop reason
- structured diagnostics / failure type

第一階段先減少變數：

- 不在共用層猜 token/output 上限。
- Codex 與 xAI 預設都送 `reasoning.effort = "high"`，但由各自 provider policy 決定
  實際 wire shape。
- 不送會明顯拖慢 TTFT 的 reasoning summary request field，除非後續有明確測試證明
  需要。
- 保留現有 routing prefixes：`codex/*` 與 `xai/*`。

## Proposed Files

### `loom/core/cognition/codex_responses_provider.py`

Ownership:

- Codex OAuth bearer loading glue，呼叫 `openai_auth.load_codex_oauth_credential()`。
- Codex Responses payload builder。
- Codex-specific request headers/base URL/model normalization。
- Codex SSE policy。
- Codex-specific error classification。
- Codex diagnostics labels。

This module owns all Codex decisions. It must not import xAI runtime code.

### `loom/core/cognition/xai_responses_provider.py`

Ownership:

- xAI OAuth bearer loading glue，呼叫 `xai_auth.load_xai_oauth_credential()`。
- xAI Responses payload builder。
- xAI base URL validation glue。
- xAI SSE policy。
- xAI entitlement/quota/subscription error classification。
- xAI diagnostics labels。

This module owns all xAI decisions. It must not import Codex runtime code.

### `loom/core/cognition/responses_sse.py`

Ownership:

- Parse raw SSE lines into neutral frames.
- Track provider phase:
  - `request_built`
  - `request_sent`
  - `headers_received`
  - `first_event_wait`
  - `reasoning_stream`
  - `output_stream`
  - `tool_call_stream`
  - `finalizing`
  - `completed`
  - `failed`
- Track `last_event_type`, first-event timestamp, last-event timestamp, and
  streamed text/tool counts.
- Provide neutral helpers for TTFB/idle diagnostics.

This module must not know provider names, OAuth, model IDs, or reasoning policy.

### `loom/core/cognition/providers.py`

This file is **not** reduced to thin imports. Only the Codex/xAI runtime moves
out; `AnthropicProvider`, the `_OpenAICompatibleBase` family
(OpenAI/OpenRouter/Ollama/LMStudio), and `_to_anthropic_messages` stay here
unchanged.

After migration, providers.py keeps:

- `CodexResponsesProvider` re-exported (import) from `codex_responses_provider.py`
- `XAIResponsesProvider` re-exported (import) from `xai_responses_provider.py`

The re-export is load-bearing: existing tests import both classes from
`loom.core.cognition.providers` (`tests/test_codex_responses_provider.py`,
`tests/test_xai_responses_provider.py`). Breaking the import path breaks them.

No new provider runtime logic should be added to this file.

#### Helper keep/move list

Module-level helpers in providers.py must be triaged before moving (extraction
hazard — cf. PR #407 missing a module-level `_log`):

| Helper | Used by | Action |
|--------|---------|--------|
| `_retry_async` | **Anthropic only** (not Responses) | **Stay** in providers.py |
| `_to_anthropic_messages` | Anthropic only | Stay |
| `_stream_interrupt_detail` | Codex + xAI | Move to shared (responses_sse or provider base) |
| `_http_status_detail` | Codex + xAI | Move to shared |
| `_responses_sse_error_detail` | Codex + xAI, provider-neutral | Move to `responses_sse.py` |
| `_ReasoningStreamWrapper` | Codex + xAI, provider-neutral | Move to `responses_sse.py` |
| `_normalize_reasoning_effort` + `_RESPONSES_REASONING_*` | Codex + xAI, **reasoning policy** | See Open Question C — must NOT land in `responses_sse.py` |

## Runtime Behavior

### Codex

Codex provider should:

- Load only Codex OAuth credentials; no `OPENAI_API_KEY` fallback.
- Build Codex Responses payload from Loom messages.
- Send `reasoning.effort = "high"` by default.
- Avoid `max_output_tokens` if the Codex backend rejects it.
- Surface `response.failed`, `error`, and non-`max_output_tokens`
  `response.incomplete` as explicit runtime errors.
- Treat no first SSE event within the TTFB threshold as a provider stall.
- Report phase and last event details in diagnostics.

### xAI

xAI provider should:

- Load only xAI OAuth credentials; no `XAI_API_KEY` fallback for `xai/*`.
- Build xAI Responses payload from Loom messages.
- Send `reasoning.effort = "high"` by default unless xAI model policy says the
  selected model should omit the effort field.
- Keep xAI entitlement/quota/subscription failures distinct from generic auth
  or retryable transport failures.
- Surface xAI error bodies without editorializing beyond a short failure type.
- Treat no first SSE event within the TTFB threshold as a provider stall.
- Report phase and last event details in diagnostics.

## Failure Types

Both providers should normalize failures into a small set:

- `credential_missing`
- `credential_expired`
- `http_error`
- `stream_error_event`
- `stream_incomplete`
- `ttfb_timeout`
- `idle_timeout`
- `malformed_sse`
- `entitlement_or_quota`
- `provider_protocol_error`
- `unknown`

The final user-visible error should include:

- provider
- model
- phase
- last event type, if any
- elapsed seconds
- concise provider message or body snippet

## Testing

Focused tests:

- Codex payload builder sends expected default reasoning and omits unsupported
  token cap.
- xAI payload builder sends expected default reasoning and preserves OAuth-only
  behavior.
- Codex and xAI SSE parser surfaces `response.failed`.
- Codex and xAI SSE parser surfaces non-`max_output_tokens` incomplete reasons.
- TTFB watchdog fires when no SSE event arrives.
- TTFB watchdog does not fire after any event arrives, including reasoning-only
  or tool-call-only events.
- xAI entitlement/quota-shaped body maps to `entitlement_or_quota`, not blind
  retry/refresh.
- `providers.py` compatibility imports still satisfy existing tests.

Live smoke tests:

- Use existing local Codex OAuth login to run a small `codex/gpt-5.5` prompt.
- Use existing local xAI OAuth login to run a small `xai/grok-4.3` prompt.
- Run one larger-context prompt on each path to verify phase diagnostics and
  no silent stall under normal conditions.

If live provider calls fail because of account entitlement, quota, or upstream
service state, the result is acceptable only if Loom surfaces a clear
structured failure instead of hanging silently.

## Open Questions / Decisions to Resolve

切檔本身不會讓 codex/gpt 停滯消失；消失靠的是 watchdog 與失敗分類。這份 design
對「真正修掉 bug 的那一塊」目前規格最薄。以下是 implementation plan 定稿前必須先
拍板的項目，按影響排序。

### Must resolve (decides whether the stall is actually fixed)

**A. TTFB/idle watchdog 機制、threshold、與「reasoning 期靜默」的衝突。**
- 現況是 `async for line in resp.aiter_lines()` 的阻塞迭代。加 watchdog 需要併發
  計時器（`asyncio.wait_for(anext(...))` 或獨立 watchdog task）；本 design 未定義
  機制，也未定義它與 `abort_signal` 及既有 600s timeout 的協調。
- 語義衝突：本 design 強制省略 `summary`。一旦不送 summary，gpt-5.5 在
  `effort=high` 的 reasoning 期很可能完全沒有 SSE event（即一直在打的數分鐘靜默）。
  若 TTFB watchdog 以「first SSE event」為基準，會誤殺它本要保護的 workload。
  Testing 一節「does not fire after any event arrives, including reasoning-only」
  預設 reasoning 期有事件，與「省略 summary」自相矛盾。
- **先回答的事實問題**：Codex/xAI backend 在 reasoning 期是否送
  `response.created` / `response.in_progress` / keep-alive 心跳？沒有的話「TTFB」
  概念站不住，watchdog 判準須改（例如「header 已到但 N 秒無 byte」vs「已收到
  created 後 idle」）。此題不解，threshold 是猜的、smoke test 不可重現。

**B. 「失敗類型 / 結構化診斷」如何交付給 session，而 session 不在檔案計劃內。**
目前 contract 是 provider `raise RuntimeError(str)` + stream 吐
`(chunk, LLMResponse)`。要把 `failure_type / phase / last_event / elapsed` 傳上去，
要嘛新 exception class、要嘛 `LLMResponse` 加欄位 —— 兩者都會動到
`session.stream_turn` 的 except handler，但 PR 步驟 3–7 只動 provider 檔。契約邊界
改了卻沒追到消費端。需明確：失敗是「更豐富的 RuntimeError 字串」還是「結構化
物件」？後者要列出 session 端改動點。

**C. `_normalize_reasoning_effort` + effort tuple 沒有家。**
它是 reasoning policy，而本 design 明文禁止 `responses_sse.py` 知道 reasoning
policy。但目前只規劃兩個獨立 provider 檔 + 一個 neutral SSE 檔，沒有 Responses
provider 共享 base。結果這段要嘛重複進兩個 provider 檔（違反 DRY），要嘛需要第四個
「shared-but-policy」模組。需指定歸屬。

> （D「providers.py 不會變薄、helper 搬留」已併入上方 Proposed Files →
> `providers.py` 一節，不在此重複。）

### Decisions to nail down (doc 目前沉默或矛盾)

**E. `malformed_sse` 是行為變更。** 現況 `json.JSONDecodeError` 是 silently
`continue`。列為 failure type 等於要改成 surface。skip-vs-fail 是真決策，須挑明。

**F. xAI 仍送 `max_output_tokens`，Codex 不送（`SUPPORTS_MAX_OUTPUT_TOKENS=False`）。**
gpt-5.5 stall 的根因正是 `max_output_tokens` 隱性 cap 推理，而 xAI payload 現在
還在送。此 asymmetry 須顯式決定去留，不能默默留著。

**G. `stream_incomplete` 不能吃掉 `reason==max_output_tokens`。**
那條是 load-bearing recovery：session 的 Issue #271 `_consecutive_max_tokens`
reasoning-continuation 靠它把高推理 turn 接下去。本 design 已說保留
non-`max_output_tokens` 才當錯誤，但須明指這個 cross-module 契約連到 session #271，
否則實作者可能把這條線斷掉。

### Smaller notes

**H. Codex/xAI credential 不對稱。** `load_xai_oauth_credential` 會自動 refresh；
`load_codex_oauth_credential` 不 refresh（過期回 None，靠 `codex` CLI 外部刷新）。
`credential_expired` 對兩者的意義與補救不同，不應對稱處理。

**I. Phase enum 跨越邊界。** `request_built / request_sent / headers_received`
發生在 httpx 設定階段（provider 側），不在 SSE line loop 內。phase tracker 須由
provider 先更新、再交給 parser；其所有權其實是跨層的，非純 `responses_sse.py`。

**J. `tool_call_stream` phase 偵測**需要監看目前被刻意丟棄的
`function_call_arguments.delta`（canonical args 走 `output_item.done`）。要嘛接受
此 phase 永遠不亮，要嘛改 parser。

**K.（相鄰、非本 PR）** `validate_xai_base_url` 不保證 `/responses` 後綴：user 若
override `base_url=https://api.x.ai/v1` 會 POST 到缺 `/responses` 的 endpoint。
建議掛 follow-up issue，不阻擋本 PR。

## PR Workflow

Implementation must happen on a new branch and PR before runtime edits.

Expected sequence:

1. Open branch.
2. Create draft PR early.
3. Move Codex runtime into `codex_responses_provider.py`.
4. Move xAI runtime into `xai_responses_provider.py`.
5. Add `responses_sse.py`.
6. Keep `providers.py` compatibility.
7. Run focused tests.
8. Run real OAuth smoke tests.
9. Open follow-up issue for Anthropic/MiniMax provider contract extraction.

## Acceptance Criteria

- Codex and xAI provider code live in separate provider-specific files.
- Shared code is neutral and does not branch on provider name.
- Existing Codex/xAI routing still works.
- MiniMax/Anthropic behavior is unchanged.
- Silent stream stalls become visible diagnostics or structured errors.
- Tests cover TTFB/idle/error-event behavior.
- At least one real OAuth Codex and xAI smoke test is attempted and reported.
