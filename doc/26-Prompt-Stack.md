# Prompt Stack

Prompt Stack 是 Loom 的「提示詞堆疊」系統。它將多個來源的提示詞組合成最終的 system prompt，供 LLM 使用。

---

## 三層結構

```
┌─────────────────────────────────────────────────────────────┐
│                    Prompt Stack                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Layer 1: SOUL.md                                    │   │
│   │ "你是 Loom，一個 harness-first, memory-native agent" │   │
│   │ 定義核心身份、價值觀、思維方式                        │   │
│   └─────────────────────────────────────────────────────┘   │
│                           │                                 │
│                           ▼                                 │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Layer 2: Agent Prompt                               │   │
│   │ "你的任務是幫助用戶完成任務"                         │   │
│   │ 定義當前任務、工具、記憶召回結果                      │   │
│   └─────────────────────────────────────────────────────┘   │
│                           │                                 │
│                           ▼                                 │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Layer 3: Personality                               │   │
│   │ "你是一個簡潔、精確的Architect人格"                 │   │
│   │ 定義回答風格、語氣、用詞偏好                         │   │
│   └─────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 組裝流程

```python
# loom/core/prompt/stack.py
class PromptStack:
    """Prompt 堆疊"""
    
    def __init__(
        self,
        soul_md: str,
        agent_prompt_generator: AgentPromptGenerator,
        personality_loader: PersonalityLoader,
    ):
        self.soul_md = soul_md
        self.agent_prompt_generator = agent_prompt_generator
        self.personality_loader = personality_loader
    
    async def build(
        self,
        session: Session,
        memory_index: MemoryIndex,
        context: dict,
    ) -> str:
        """
        組裝最終的 system prompt
        """
        
        # 1. 載入 SOUL.md
        soul = self.soul_md
        
        # 2. 生成 Agent Prompt
        agent = await self.agent_prompt_generator.generate(
            session=session,
            memory_index=memory_index,
            context=context,
        )
        
        # 3. 載入 Personality
        personality = await self.personality_loader.load(
            session.personality_id
        )
        
        # 4. 組合
        return self._compose(soul, agent, personality)
    
    def _compose(self, soul: str, agent: str, personality: str) -> str:
        """組合三層"""
        
        return f"""
{soul}

---

## 當前任務

{agent}

---

## 回答風格

{personality}
"""
```

---

## Layer 1: SOUL.md

### 作用

SOUL.md 定義 Loom 的「靈魂」——核心身份、價值觀、思維方式。它是相對靜態的，通常不會每個 session 都改變。

### 內容結構

```markdown
# SOUL.md — Loom Agent Identity

> 這是 Loom 的身份定義文件。

## Identity
你是 Loom，一個 harness-first, memory-native agent...

## How You Think
- 行動前評估影響範圍
- 信任需要贏得
- 記憶是累積的判斷

## How You Work With Users
- 後果前確認，不是思考前確認
- 自主是特權，不是預設

## What You Value
- 精確勝過冗長
- 可逆性優先
- 連續性勝過獨立
```

詳見 [27-SOUL-設計.md](27-SOUL-設計.md)。

---

## Layer 2: Agent Prompt

### 作用

Agent Prompt 是動態生成的，包含當前任務的具體上下文。

### 內容結構

```python
# loom/core/prompt/agent.py
class AgentPromptGenerator:
    async def generate(
        self,
        session: Session,
        memory_index: MemoryIndex,
        context: dict,
    ) -> str:
        """生成 Agent Prompt"""
        
        parts = []
        
        # 1. 當前任務描述
        if context.get("task"):
            parts.append(f"## 任務\n{context['task']}")
        
        # 2. Memory Index 摘要
        parts.append(f"""
## 你的記憶狀態

- {memory_index.semantic_count} 個 Facts
- {memory_index.episodic_count} 次 Session 記憶
- {memory_index.skill_count} 個 Skills
- 主要領域：{', '.join(memory_index.top_topics[:5])}
""")
        
        # 3. 可用工具
        if context.get("tools"):
            tools_desc = self._format_tools(context["tools"])
            parts.append(f"## 可用工具\n{tools_desc}")
        
        # 4. 最近對話歷史（壓縮後）
        if context.get("recent_history"):
            parts.append(f"## 最近的對話\n{context['recent_history']}")
        
        return "\n\n".join(parts)
```

### 範例輸出

```markdown
## 任務
用戶詢問如何新增一個工具到 Loom。

## 你的記憶狀態

- 12 個 Facts（topics: loom, harness, tool, middleware）
- 14 次 Session 的壓縮記憶
- 8 個 Skills
- 主要領域：loom, tool, harness, memory, middleware

## 可用工具

1. read_file(path) - 讀取文件
2. write_file(path, content) - 寫入文件
3. run_bash(command) - 執行 shell 命令

## 最近的對話

- 用戶問過 loom.toml 的設定方式
- 用戶偏好簡潔的回答
```

---

## Layer 3: Personality

### 作用

Personality 定義回答的「風格」——語氣、用詞、格式偏好。

### 內容結構

```markdown
## Personality: Architect

你是一個Architect人格的 AI 助手。

### 回答風格
- 簡潔、條理分明
- 優先使用列表和表格
- 直接給出結論，再解釋原因

### 語氣
- 自信、專業
- 避免過多的開場白
- 直接稱呼「你」

### 格式偏好
- Markdown 標題層級清晰
- 程式碼區塊有語言標註
- 表格用於對比
```

詳見 [28-Personalities.md](28-Personalities.md)。

---

## 組合順序的考量

### 為什麼 SOUL 在最前面？

1. **身份先決** — LLM 需要先知道「自己是誰」才能正確執行任務
2. **價值觀傳遞** — SOUL 中的價值觀會影響後續所有輸出
3. **思維框架** — 「如何思考」比「做什麼」更基礎

### 為什麼 Personality 在最後？

1. **覆蓋優先** — 後面的內容會覆蓋前面的風格
2. **任務確定後再調整** — Personality 是「怎麼說」，不是「說什麼」
3. **最終潤飾** — Personality 確保回答符合用戶偏好

---

## Token 控制

### Prompt Stack 也受 Context Budget 限制

```python
async def build(
    self,
    session: Session,
    memory_index: MemoryIndex,
    context: dict,
) -> str:
    # 計算各層的 token 數
    soul_tokens = self.counter.count(self.soul_md)
    agent_tokens = await self._estimate_agent_tokens(...)
    personality_tokens = await self._estimate_personality_tokens(...)
    
    total = soul_tokens + agent_tokens + personality_tokens
    
    # 如果超出預算，壓縮 Agent Prompt
    if total > BUDGET.max_prompt_tokens:
        available = BUDGET.max_prompt_tokens - soul_tokens - personality_tokens
        agent = await self.agent_prompt_generator.generate(
            session=session,
            memory_index=memory_index,
            context=context,
            max_tokens=available,
        )
    else:
        agent = await self.agent_prompt_generator.generate(...)
    
    return self._compose(self.soul_md, agent, personality)
```

---

## loom.toml 配置

```toml
[prompt_stack]

# SOUL.md 路徑
soul_path = "loom/core/soul/SOUL.md"

# 預設 Personality
default_personality = "architect"

# Prompt 各層的 token 限制
[pompt_stack.budget]
max_soul_tokens = 2000
max_agent_tokens = 4000
max_personality_tokens = 1000
max_total_tokens = 7000
```

---

## 總結

Prompt Stack 實現了三層關注點分離：

| 層 | 內容 | 頻率 |
|----|------|------|
| SOUL | 身份、價值觀、思維方式 | 幾乎不變 |
| Agent | 任務、工具、記憶上下文 | 每 session 變化 |
| Personality | 回答風格、語氣、格式 | 可切換 |

這種設計確保：
- **一致性** — SOUL 不會被遺忘
- **靈活性** — Agent Prompt 可動態調整
- **可定製** — Personality 可隨時切換
