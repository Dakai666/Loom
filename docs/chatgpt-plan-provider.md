# Official ChatGPT plan provider

Loom's `chatgpt/<slug>` provider uses the documented Sign in with ChatGPT
flow for local/open-source clients. Its credentials belong to Loom and are
separate from `codex/` and API-key `openai/`. No automatic fallback crosses
these billing paths. The existing `codex/` provider remains an explicit backup.

## Connect and discover

```sh
loom auth chatgpt login --label personal
loom auth chatgpt models
loom auth chatgpt verify --model <slug-from-models> --output /tmp/loom-chatgpt-check.json
loom chat --model chatgpt/<slug-from-models>
```

Choose **Use your ChatGPT plan** when OpenAI asks for consent. Identity-only
sign-in is stored, but cannot perform inference. No OpenAI Platform API key
or Nous subscription is required by this integration. Account/workspace
eligibility, model access, app limits, and existing plan usage still apply.

`models` fetches the selected account's live catalog. It distinguishes server
advertised modalities from unknown metadata and actual verification. Catalog
membership is not a successful inference test. It does not establish access
to every OpenAI API model or to other model vendors.

`verify` consumes ChatGPT plan usage. It sends four small synthetic requests
per model: a text marker, a generated two-color image, a harmless function call,
and a tool-result replay. It never reads project files or executes real shell
tools. Repeat `--model` for several choices, or use `--all-models` deliberately.
It stops at the first provider error and retains the error in the report.
An unexpected answer is **inconclusive**, not proof that a capability is absent.

As of the October 27, 2026 documented preview:

| Capability | Route contract | Live verification |
| --- | --- | --- |
| Text input/output | Supported for eligible models | Text marker |
| Image understanding | Depends on the selected model | Generated image with randomized colors |
| Image generation | Unsupported on this route | Not requested |
| Audio input/transcription | Unsupported on this route | Not requested |
| Audio output/TTS | Unsupported on this route | Not requested |
| Local function tools | Supported with namespace packaging | Call + result replay |

Loom's separate TTS/STT tools may have their own providers and billing; they
are not made available by ChatGPT plan authorization. Hosted Responses tools
have additional restrictions; see the upstream limitations below.

## Accounts, renewal, and sign-out

```sh
loom auth chatgpt accounts
loom auth chatgpt login --new-account --label work
loom auth chatgpt use personal
loom auth chatgpt login --account personal
loom auth chatgpt logout --account personal
```

The store defaults to `~/.loom/chatgpt-auth.json` (`LOOM_HOME` overrides its
directory). It stores one stable host ID and separate client IDs, verified
identities, tokens and model caches per account. Files use owner-only
permissions and atomic replacement. A cross-process lock covers refresh token
redemption and persistence so concurrent Loom processes do not redeem the
same rotating token. Never copy this file into source control or share it.

Login verifies the signed ID token, issuer, audience, expiry, and nonce. It
validates the OAuth state and PKCE exchange before activating a connection.
Reauthorization retains the issued client ID; different accounts get separate
registrations. A failed new login does not replace the active account.

Refresh is automatic near access-token expiry. Transient network/server errors
preserve credentials; terminal refresh failures require another login. Sign-out
attempts remote revocation and clears local tokens, retaining the host/client
mapping. If remote revocation cannot be confirmed, the command says so.

Manage per-app usage limits and disconnect Loom in ChatGPT Settings. The
connection shares existing plan allowance; it does not add a separate quota.

## Configuration

```toml
[cognition]
default_model = "chatgpt/<slug-from-models>"

[providers.chatgpt]
enabled = true
account = "personal"  # optional ID or label; otherwise the active saved account
timeout = 600
# reasoning_effort = "high"  # only for models that support it
```

The provider uses a server-reported `context_window` from the account's cached
catalog when available; otherwise it starts with a conservative 32,768-token
budget. A known model's explicit context override can be set through Loom's
existing `[cognition.context_window_overrides]` configuration.

The public endpoint is fixed to `https://api.openai.com/v1/responses`. Payloads
use `store: false`, `stream: true`, complete local history and namespaced tools.
Unsupported preview parameters such as `max_output_tokens` are not sent. A
stream must reach `response.completed`; partial output and quota errors are
never reported as a successful response.

## Validation status

Automated tests cover signed OIDC validation, PKCE/state, separate account
registrations, concurrent refreshes, credential preservation/revocation, public
endpoint routing, streaming failures, image conversion, and tool-result replay.
On October 27, 2026, the user completed real ChatGPT authorization for Loom.
The account returned the following catalog. All eight models completed real
text and function-call/result-replay requests, and all accepted image inputs.
Six passed the synthetic color/location check. Two returned incorrect visual
answers; these are inconclusive for understanding, not an unsupported-input
error. This is an account-specific snapshot, not a promise of access for every
account or a model-quality benchmark.

| Model | Context from catalog | Text | Tool loop | Image understanding |
| --- | ---: | --- | --- | --- |
| gpt-6.1-sol | 500,000 | Verified | Verified | Verified |
| gpt-6-astra | 272,000 | Verified | Verified | Verified |
| gpt-6.1-terra | 500,000 | Verified | Verified | Verified |
| gpt-6-sol | 272,000 | Verified | Verified | Inconclusive: swapped left/right colors |
| gpt-6-luna | 272,000 | Verified | Verified | Inconclusive: incorrect second color |
| gpt-5.6-sol | 272,000 | Verified | Verified | Verified |
| gpt-5.6-terra | 272,000 | Verified | Verified | Verified |
| gpt-5.6-luna | 272,000 | Verified | Verified | Verified |

The first tiny-image/strict-format probe was inconclusive for six models. A
larger synthetic image and color-word scoring resolved four of those cases;
the remaining two failures are retained above. No repeated-until-success
claim is made. The audio limitations remain route-level restrictions and were
not probed against another endpoint or billing path.

The focused suite completed **315 passing tests**, covering the new provider
and existing Codex/xAI providers, CLI, routing, vision, tier selection, context
budgets, and session model updates. Token rotation was verified with a real
cross-process test using a mocked OAuth server; no one-hour expiry wait was
used for the live account.

A separate live integration check also passed through `build_router` and
Loom's existing local-file vision builder: `chatgpt/gpt-6.1-sol` read a
synthetic blue/green PNG correctly and resolved its 500,000-token context
window from the account catalog. No session memories or project files were
included in these requests.

The sanitized [live verification report](validation/chatgpt-plan-2026-10-27.json)
contains the synthetic observations and check times, without account identity
or credentials. Refresh the catalog and rerun selected checks when changing
accounts or when upstream capabilities change.

## Upstream contract

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
