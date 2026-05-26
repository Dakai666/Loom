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

After migration, keep only thin compatibility imports/classes where needed:

- `CodexResponsesProvider` imported from `codex_responses_provider.py`
- `XAIResponsesProvider` imported from `xai_responses_provider.py`

No new provider runtime logic should be added to this file.

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
