# Prompt Stack（更新版）

> 依據 `loom/core/cognition/prompt_stack.py` 更新。

---

## ⚠️ 與舊版文件的差異

舊版描述的是**不存在的架構**（`AgentPromptGenerator`、`PersonalityLoader`、async `build()`）。

實際實作非常簡單：純同步讀檔、組合字串、runtime personality 切換。

---

## 實際實作

```python
class PromptStack:
    def load(self) -> str:
        """同步讀取三層檔案並組合"""
        self._layers = []
        # Layer 1 — SOUL
        if self._soul_path.exists():
            self._layers.append(PromptLayer("soul", self._soul_path.read_text(), self._soul_path))
        # Layer 2 — Agent
        if self._agent_path.exists():
            self._layers.append(PromptLayer("agent", self._agent_path.read_text(), self._agent_path))
        # Layer 3 — Personality
        if self._personality_path.exists():
            self._layers.append(PromptLayer("personality", self._personality_path.read_text(), self._personality_path))
        return self.composed_prompt

    @property
    def composed_prompt(self) -> str:
        return "\n\n---\n\n".join(layer.content for layer in self._layers)

    def switch_personality(self, name: str) -> bool:
        """Runtime 切換 personality"""
        # 從 personalities_dir/ 讀取 {name}.md，替換 personality 層

    def clear_personality(self) -> None:
        """移除 personality 層"""

    def available_personalities(self) -> list[str]:
        """列舉 personalities_dir/ 下的所有 .md 檔案"""
        return sorted(p.stem for p in self._personalities_dir.glob("*.md"))

    @classmethod
    def from_config(cls, config: dict, base_dir=None) -> "PromptStack":
        """從 loom.toml 的 [identity] 區段建立"""
```

---

## from_config() — loom.toml 格式

```python
identity = config.get("identity", {})
# soul = "SOUL.md"                  (default)
# agent = "Agent.md"                (optional)
# personality = "personalities/foo.md" (optional)
# personalities_dir = "personalities"  (default)
```

---

## switch_personality() 的實際行為

```python
stack.switch_personality("sisi_moon")  # → 讀取 personalities/sisi_moon.md
# 若該 personality 有 mood_frontmatter，
# 可觸發不同的行為模式（如 Moon Mood tarot）
```

Moon Mood tarot 的 `personalities/sisi.md` 內容結構：
- 包含 `mood_frontmatter`（YAML）標記 Mood Tarot 變體
- `switch_personality("sisi_moon")` → 加載 Moon Mood tarot

---

## Interaction Language Layer（v0.3.8.0，PR #423+#430）

除了 SOUL / Agent / Personality 三層之外，v0.3.8.0 引入一個**獨立 contract layer**：

```python
INTERACTION_LANGUAGE_INSTRUCTIONS = (
    "When you dispatch a multi-tool batch, provide a one-line intent "
    "before the batch starting with '▸ '. After the batch completes, "
    "start your outcome judgement line with one glyph: '✓' fulfilled, "
    "'◐' partial, '⚠' unfulfilled, '↪' pivoted, or '🛑' aborted. "
    "Single-tool calls do not need an intent header."
)
```

定義在 `loom/core/envelope_outcome.py`、由 PromptStack 在 build 時併入 system prompt（位置在 personality 之後）。

這個層的作用是教 agent 用 marker 標記 envelope intent / outcome，給 CLI + Discord 的 envelope renderer 可解析的訊號：

- `▸ <intent>` → 抽進 `envelope.intent`，渲染為 envelope header
- `✓ ◐ ⚠ ↪ 🛑` 開頭的 line → 抽進 `envelope.outcome` + `outcome_summary`，渲染為 outcome row（marker scan 掃多行，不限第一行）

producer-side wiring 細節見 PR #431 + `docs/superpowers/specs/2026-05-20-ui-ux-interaction-language-design.md`。

---

## 與舊版文件的關係

舊版 `doc/26` 描述的 SOUL/Agent/Personality 層級概念**仍然正確**，但實作方式是同步讀取靜態檔案。動態生成（`AgentPromptGenerator`）並未實作。Agent context 的動態部分目前由 Session 在每次 turn 注入具體資訊，而非在 Prompt Stack 層處理。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*