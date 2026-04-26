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

## 與舊版文件的關係

舊版 `doc/26` 描述的 SOUL/Agent/Personality 層級概念**仍然正確**，但實作方式是同步讀取靜態檔案。動態生成（`AgentPromptGenerator`）並未實作。Agent context 的動態部分目前由 Session 在每次 turn 注入具體資訊，而非在 Prompt Stack 層處理。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*