# #648 P4 sub-agent handoff retired (2026-11-19, #650)

Master `5af76c0` copies of the files touched when the sub-agent handoff was
reduced to one exit rule. Rollback is a revert of the P4 PR; these copies are
reference only.

Removed:
- `result_write` tool, the result slot and its precedence over the final reply,
  the HAND-OFF CONTRACT prompt section, the `result_write` redirect in the
  unknown-tool hint (`loom/core/agent/subagent.py`).
- The wrap-up reminder at 2 turns left and `wrapup_warned`.
- `_classify_failure` (codes `tool_loop` / `max_turns_partial` /
  `max_turns_no_progress`), the failure counters, `_write_failure_scratchpad`
  and the `subagent_failure:<id>` ref, the unused `scratchpad` parameter of
  `run_subagent`.
- 7 failure fields of `SubAgentResult` (`last_tool_name`, `last_tool_error`,
  `partial_output`, `failure_code`, `recovery_suggestion`, `error_context`,
  `result_slot`).
- `_verify_spawn_agent` and its `post_validator`, the 6 `subagent_*` metadata
  keys, the multi-part failure message (`loom/platform/cli/tools.py`).
- Tests: `TestSubAgentFailureContext`, `TestSubAgentFailureCodes`,
  `TestResultWriteSlot`, `TestWrapupReminder`, the result_write redirect case of
  `TestUnknownToolHint` (`tests/test_subagent.py`); `TestSpawnAgentVerifier`
  (`tests/test_tool_verifiers.py`).

Kept: child `session_log` persistence, tool scoping, `max_turns`, the ledger
`model_event`, memorize provenance, `max_tokens=4096`.
