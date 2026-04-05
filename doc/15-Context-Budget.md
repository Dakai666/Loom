# Context Budget

Context Budget 是 Loom 的 Token 配額管理系統。它確保每個 session 不會超出 context window 限制，並在接近上限時自動觸發壓縮。

---

## 核心問題

Context Window 是 LLM 的「工作記憶」：

```
┌─────────────────────────────────────────────────────┐
│                 Context Window                      │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐  │
│  │ System      │ │  History    │ │  Current    │  │
│  │ Prompt      │ │  Messages   │ │  Request    │  │
│  │             │ │             │ │             │  │
│  │ ~4K tokens  │ │ ~100K tokens│ │ ~4K tokens  │  │
│  └─────────────┘ └─────────────┘ └─────────────┘  │
│                                                     │
│  總限額: 128K tokens (GPT-4o)                      │
└─────────────────────────────────────────────────────┘
```

當對話歷史越來越長，`History Messages` 會不斷膨脹直到接近上限。Context Budget 的任務是：
1. **追蹤** — 精確計算已使用的 tokens
2. **預警** — 接近上限前發出警告
3. **壓縮** — 自動執行摘要，釋放空間

---

## 配額結構

```python
# loom/core/cognition/budget.py
@dataclass
class ContextBudget:
    """Session 的 token 配額"""
    
    # 配額設定
    max_tokens: int = 100_000          # 預設上限（低於 model 實際值，留 buffer）
    warning_threshold: float = 0.8     # 80% 時預警
    critical_threshold: float = 0.95   # 95% 時強制壓縮
    
    # 追蹤狀態
    used_tokens: int = 0
    prompt_tokens: int = 0             # system prompt 消耗
    history_tokens: int = 0            # 對話歷史消耗
    response_tokens: int = 0           # 回應消耗
    
    # 壓縮歷史
    compression_count: int = 0         # 壓縮次數
    last_compression_at: datetime | None = None
```

---

## Token 計算

### 精確計算

Loom 使用 `tiktoken` 進行精確的 token 計算（而非粗略的 4 字符 = 1 token）：

```python
# loom/core/cognition/tokenizer.py
import tiktoken

class TokenCounter:
    """精確的 token 計算"""
    
    def __init__(self, model: str = "gpt-4o"):
        self.encoding = tiktoken.encoding_for_model(model)
    
    def count(self, text: str) -> int:
        """計算單段文字的 token 數"""
        return len(self.encoding.encode(text))
    
    def count_messages(self, messages: list[dict]) -> int:
        """計算 messages 列表的總 token 數"""
        total = 0
        
        for msg in messages:
            # 每條訊息有固定的 overhead
            total += 3  # role + content + overhead
            
            # 加上實際內容
            total += self.count(msg.get("content", ""))
            
            # 如果有 tool_calls，加上其 token 消耗
            if "tool_calls" in msg:
                total += self.count(str(msg["tool_calls"]))
        
        return total
```

### 預估計算

對於某些場景（如 streaming），無法等待精確計算完成。這時使用粗略估算：

```python
# 粗略估算：每 4 個字元 ≈ 1 token（英文）
# 中文約 1.5 字符 ≈ 1 token
def estimate_tokens(text: str) -> int:
    chinese_ratio = 0.7  # 假設 70% 是中文
    return int(len(text) * chinese_ratio / 1.5 + len(text) * (1 - chinese_ratio) / 4)
```

---

## 配額追蹤

### 消費記錄

```python
# loom/core/cognition/budget.py
class ContextBudgetManager:
    def __init__(self, budget: ContextBudget, counter: TokenCounter):
        self.budget = budget
        self.counter = counter
    
    def consume(self, text: str, category: str = "response"):
        """記錄 token 消耗"""
        tokens = self.counter.count(text)
        
        self.budget.used_tokens += tokens
        
        if category == "prompt":
            self.budget.prompt_tokens += tokens
        elif category == "history":
            self.budget.history_tokens += tokens
        elif category == "response":
            self.budget.response_tokens += tokens
        
        # 檢查是否需要預警
        self._check_thresholds()
    
    def _check_thresholds(self):
        ratio = self.budget.used_tokens / self.budget.max_tokens
        
        if ratio >= self.budget.critical_threshold:
            self._trigger_compression()
        elif ratio >= self.budget.warning_threshold:
            self._emit_warning(ratio)
```

### 預算查詢

```python
# Agent 可查詢剩餘配額來决定是否要簡潔回答
def get_remaining_ratio(self) -> float:
    return 1 - (self.budget.used_tokens / self.budget.max_tokens)

def can_fit(self, estimated_tokens: int) -> bool:
    return (self.budget.used_tokens + estimated_tokens) < self.budget.max_tokens
```

---

## 自動壓縮

### 觸發時機

```python
# 當 budget 使用超過 95% 時觸發自動壓縮
class AutoCompressor:
    def __init__(self, memory: MemoryStore, budget: ContextBudget):
        self.memory = memory
        self.budget = budget
    
    async def compress_if_needed(self):
        ratio = self.budget.used_ratio
        
        if ratio < 0.95:
            return  # 不需要壓縮
        
        await self.compress_history()
    
    async def compress_history(self):
        # 1. 讀取當前對話歷史
        history = await self._get_recent_history()
        
        # 2. 生成摘要（呼叫 LLM）
        summary = await self._generate_summary(history)
        
        # 3. 寫入 Episodic Memory
        await self.memory.write_episode(
            content=summary,
            metadata={"type": "compressed_history", "tokens_saved": self.budget.history_tokens}
        )
        
        # 4. 清空歷史（保留 system prompt 和 summary）
        await self._clear_history()
        
        # 5. 更新 budget
        self.budget.history_tokens = self.counter.count(summary)
        self.budget.compression_count += 1
        self.budget.last_compression_at = datetime.now()
```

### 摘要生成

```python
async def _generate_summary(self, history: list[dict]) -> str:
    """用 LLM 生成對話摘要"""
    prompt = f"""請簡潔總結以下對話的要點，保留關鍵資訊和決定：

{self._format_history(history)}

要求：
- 不超過 500 tokens
- 保留所有的事實、決定、和待辦事項
- 用簡潔的條列式表達"""

    response = await self.llm.complete(prompt)
    return response
```

---

## 優先級管理

### 保留策略

當 context 即將滿時，Loom 按照以下優先級保留內容：

```
Priority 1: System Prompt（SOUL + Agent + Personality）
    ↓
Priority 2: Tool Result（最近一次工具調用的結果）
    ↓
Priority 3: Memory Context（Semantic/Skill 的召回結果）
    ↓
Priority 4: Conversation History（對話歷史，會被壓縮）
    ↓
Priority 5: 舊的 Tool Result
    ↓
Priority 6: 更舊的對話歷史
```

### 動態裁剪

```python
async def trim_to_fit(self, target_tokens: int):
    """裁剪 history 直到符合 target tokens"""
    
    while self.budget.history_tokens > target_tokens:
        # 1. 先嘗試壓縮
        if self.budget.compression_count < 3:  # 最多壓縮 3 次
            await self.compress_history()
            continue
        
        # 2. 壓縮已達上限，直接刪除最舊的訊息
        oldest = await self._get_oldest_message()
        if oldest:
            await self._delete_message(oldest["id"])
            self.budget.history_tokens -= self.counter.count_message(oldest)
        else:
            break  # 已經沒有可刪的了
```

---

## loom.toml 配置

```toml
[cognition.budget]

# 配額上限（低於 model 實際 context window）
max_tokens = 100000

# 預警閾值
warning_threshold = 0.8    # 80% 時發出警告
critical_threshold = 0.95  # 95% 時自動壓縮

# 自動壓縮設定
auto_compress = true
max_compressions_per_session = 3

# 每條訊息的 buffer（預留給 tool_calls 等）
per_message_buffer = 50

# 強制保留的訊息（system prompt 等）
reserved_tokens = 4000
```

---

## 監控與報告

### Budget 狀態

```python
def get_status(self) -> dict:
    """取得當前 budget 狀態"""
    return {
        "used": self.budget.used_tokens,
        "max": self.budget.max_tokens,
        "ratio": self.budget.used_ratio,
        "remaining": self.budget.remaining_tokens,
        "history_tokens": self.budget.history_tokens,
        "compression_count": self.budget.compression_count,
        "last_compression": self.budget.last_compression_at,
        "status": self._get_status_label()
    }

def _get_status_label(self) -> str:
    if self.budget.used_ratio < 0.8:
        return "healthy"
    elif self.budget.used_ratio < 0.95:
        return "warning"
    else:
        return "critical"
```

### Reflection API 整合

Context Budget 的統計會寫入 Reflection Report：

```python
# 供 Reflection API 使用
def get_report(self) -> dict:
    return {
        "total_tokens_used": self.budget.used_tokens,
        "compression_count": self.budget.compression_count,
        "average_usage_per_turn": self.budget.used_tokens / self.budget.turn_count,
        "recommendations": self._get_recommendations()
    }
```

---

## 總結

Context Budget 確保 Loom 不會「忘記說話」：

| 功能 | 說明 |
|------|------|
| 精確計算 | 使用 tiktoken 計算實際 token 數 |
| 多級閾值 | 80% 預警、95% 自動壓縮 |
| 優先級保留 | System > Tool Result > Memory > History |
| 自動摘要 | 壓縮對話歷史，寫入 Episodic Memory |
| 可配置 | loom.toml 調整閾值和上限 |
