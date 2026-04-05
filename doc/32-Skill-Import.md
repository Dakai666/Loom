# Skill Import

Skill Import 允許從外部匯入技能（Skills）到 Loom 的記憶系統中。

---

## 為什麼需要 Skill Import？

Loom 的記憶系統會隨著使用而累積經驗，但這個過程較慢。Skill Import 允許：
- 從外部學習新技能
- 加速初始知識建立
- 從專家經驗中受益

---

## Import Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│                   Skill Import Pipeline                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   1. Discovery     2. Validation   3. Deduplication         │
│   ┌───────────┐   ┌───────────┐   ┌───────────┐            │
│   │  發現技能  │──▶│  驗證格式  │──▶│  檢查重複 │            │
│   └───────────┘   └───────────┘   └─────┬─────┘            │
│                                          │                  │
│                                          ▼                  │
│   6. Memory      5. Confidence    4. Transformation          │
│   ┌───────────┐   ┌───────────┐   ┌───────────┐            │
│   │  寫入記憶  │◀──│  設定初始 │◀──│  轉換格式 │            │
│   │           │   │  confidence│   │           │            │
│   └───────────┘   └───────────┘   └───────────┘            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Pipeline 各階段

### 1. Discovery

```python
# loom/core/memory/skill_import/discovery.py
class SkillDiscovery:
    """技能發現"""
    
    async def discover(
        self,
        source: SkillSource,
    ) -> list[RawSkill]:
        """
        從來源發現技能
        
        Sources:
        - GitHub repository
        - Local files
        - URL
        - Text description
        """
        
        if isinstance(source, GitHubSource):
            return await self._discover_github(source)
        elif isinstance(source, FileSource):
            return await self._discover_files(source)
        elif isinstance(source, URLSource):
            return await self._discover_url(source)
        elif isinstance(source, TextSource):
            return await self._discover_text(source)
        
        raise ValueError(f"Unknown source type: {type(source)}")
    
    async def _discover_github(self, source: GitHubSource) -> list[RawSkill]:
        """從 GitHub 發現技能"""
        
        skills = []
        
        # 列目錄
        contents = await self.github_api.list_contents(
            repo=source.repo,
            path=source.path,
        )
        
        for item in contents:
            if item.type == "file" and item.name.endswith(".md"):
                content = await self.github_api.get_file(item.path)
                skill = self._parse_markdown(content)
                skills.append(skill)
        
        return skills
```

### 2. Validation

```python
# loom/core/memory/skill_import/validator.py
@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

class SkillValidator:
    """技能驗證"""
    
    REQUIRED_FIELDS = ["name", "description", "instructions"]
    
    def validate(self, skill: RawSkill) -> ValidationResult:
        """驗證技能格式"""
        
        errors = []
        warnings = []
        
        # 檢查必填欄位
        for field in self.REQUIRED_FIELDS:
            if not getattr(skill, field, None):
                errors.append(f"Missing required field: {field}")
        
        # 檢查名稱格式
        if skill.name and not self._is_valid_name(skill.name):
            errors.append(f"Invalid skill name: {skill.name}")
        
        # 檢查指令長度
        if skill.instructions and len(skill.instructions) < 50:
            warnings.append("Instructions are very short")
        
        # 檢查信任等級
        if skill.trust_level not in ["SAFE", "GUARDED", "CRITICAL"]:
            warnings.append(f"Unknown trust level: {skill.trust_level}")
        
        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )
    
    def _is_valid_name(self, name: str) -> bool:
        """驗證技能名稱"""
        import re
        return bool(re.match(r'^[a-z][a-z0-9_-]*$', name))
```

### 3. Deduplication

```python
# loom/core/memory/skill_import/dedup.py
class SkillDeduplicator:
    """技能去重"""
    
    async def check_duplicate(
        self,
        skill: RawSkill,
        existing_skills: list[SkillGenome],
    ) -> DuplicateResult:
        """
        檢查是否重複
        
        Returns:
            DuplicateResult: 是否重複及相似度
        """
        
        for existing in existing_skills:
            # 名稱完全相同
            if skill.name == existing.key:
                return DuplicateResult(
                    is_duplicate=True,
                    reason="exact_name_match",
                    existing_skill=existing,
                )
            
            # 描述高度相似（用 embedding 相似度）
            similarity = await self._calculate_similarity(
                skill.description,
                existing.description,
            )
            
            if similarity > 0.9:
                return DuplicateResult(
                    is_duplicate=True,
                    reason=f"high_similarity_{similarity:.2f}",
                    existing_skill=existing,
                )
        
        return DuplicateResult(is_duplicate=False)
    
    async def resolve_duplicate(
        self,
        new_skill: RawSkill,
        existing: SkillGenome,
        strategy: DuplicateStrategy,
    ) -> SkillGenome:
        """解決重複"""
        
        if strategy == DuplicateStrategy.KEEP_EXISTING:
            return existing
        
        elif strategy == DuplicateStrategy.REPLACE:
            return self._convert_to_genome(new_skill)
        
        elif strategy == DuplicateStrategy.MERGE:
            return await self._merge_skills(new_skill, existing)
```

### 4. Transformation

```python
# loom/core/memory/skill_import/transformer.py
class SkillTransformer:
    """技能格式轉換"""
    
    def transform(self, raw_skill: RawSkill) -> SkillGenome:
        """將 RawSkill 轉換為 SkillGenome"""
        
        return SkillGenome(
            key=raw_skill.name,
            value=self._build_prompt(raw_skill),
            metadata={
                "source": raw_skill.source,
                "source_url": raw_skill.url,
                "author": raw_skill.author,
                "tags": raw_skill.tags,
                "examples": raw_skill.examples,
            },
            confidence=0.0,  # 初始為 0，通過使用逐漸增加
            call_count=0,
            success_count=0,
            last_used=None,
            created_at=datetime.now(),
        )
    
    def _build_prompt(self, skill: RawSkill) -> str:
        """構建技能指令"""
        
        parts = [
            f"# {skill.name}\n",
            f"{skill.description}\n",
            f"## Instructions\n{skill.instructions}\n",
        ]
        
        if skill.examples:
            parts.append("## Examples\n")
            for ex in skill.examples:
                parts.append(f"### Example: {ex.get('title', 'Untitled')}\n")
                parts.append(f"{ex.get('content', '')}\n")
        
        if skill.troubleshooting:
            parts.append(f"## Troubleshooting\n{skill.troubleshooting}\n")
        
        return "\n".join(parts)
```

### 5. Confidence Gate

```python
# loom/core/memory/skill_import/confidence_gate.py
class ConfidenceGate:
    """Confidence 門控"""
    
    def __init__(
        self,
        initial_confidence: float = 0.3,
        max_initial_confidence: float = 0.5,
    ):
        self.initial_confidence = initial_confidence
        self.max_initial_confidence = max_initial_confidence
    
    def evaluate(
        self,
        skill: RawSkill,
        validation: ValidationResult,
        duplicate: DuplicateResult,
    ) -> ConfidenceDecision:
        """
        評估是否允許匯入及初始 confidence
        """
        
        # 驗證失敗，不允許匯入
        if not validation.valid:
            return ConfidenceDecision(
                allowed=False,
                initial_confidence=0.0,
                reason=f"Validation failed: {validation.errors}",
            )
        
        # 重複，根據策略決定
        if duplicate.is_duplicate:
            return ConfidenceDecision(
                allowed=True,
                initial_confidence=self.initial_confidence * 0.5,  # 重複降低 confidence
                reason=f"Duplicate of {duplicate.existing_skill.key}",
            )
        
        # 根據驗證警告調整
        confidence = self.initial_confidence
        if len(validation.warnings) > 3:
            confidence *= 0.8
        
        # 不超過最大初始值
        confidence = min(confidence, self.max_initial_confidence)
        
        return ConfidenceDecision(
            allowed=True,
            initial_confidence=confidence,
            reason="passed_gate",
        )
```

### 6. Memory Write

```python
# loom/core/memory/skill_import/writer.py
class SkillWriter:
    """技能寫入記憶"""
    
    async def write(
        self,
        genome: SkillGenome,
        memory_store: MemoryStore,
    ) -> bool:
        """寫入技能到記憶"""
        
        # 寫入 Skill Genome
        await memory_store.create_skill_genome(
            key=genome.key,
            value=genome.value,
            metadata=genome.metadata,
            confidence=genome.confidence,
        )
        
        # 更新統計
        await memory_store.increment_stat("skills_imported")
        
        logger.info(f"Imported skill: {genome.key}")
        
        return True
```

---

## CLI 命令

```bash
# 從 GitHub 匯入
loom skill import --source github --repo owner/repo --path skills/

# 從本地檔案匯入
loom skill import --source file --path ./my-skills/

# 從 URL 匯入
loom skill import --source url --url https://example.com/skill.md

# 從文字匯入
loom skill import --source text --text "如何寫Python..."

# 驗證模式（不實際匯入）
loom skill import --dry-run ...

# 指定處理策略
loom skill import --on-duplicate merge  # 或 replace / skip
```

---

## Skill 格式

### Markdown 格式

```markdown
---
name: python_code_review
description: Python 程式碼審查技能
author: expert@example.com
tags: [python, code-review, quality]
trust_level: GUARDED
---

# Python Code Review

## Instructions

當你需要審查 Python 程式碼時：

1. 首先檢查基本的程式碼風格
2. 檢查可能的錯誤和漏洞
3. 評估效能問題
4. 檢查測試覆蓋

## Examples

### Example: 發現效能問題

當看到重複的 list comprehension 時...

### Example: 發現安全漏洞

當看到 SQL 拼接時...

## Troubleshooting

Q: 如何處理複雜的商業邏輯？
A: 先理解業務需求，再評估實作。
```

---

## loom.toml 配置

```toml
[skill_import]

# 預設來源
default_source = "github"

# 初始 confidence
initial_confidence = 0.3
max_initial_confidence = 0.5

# 重複處理策略
on_duplicate = "skip"  # skip / merge / replace

# 驗證設定
[skill_import.validation]
require_author = false
min_instructions_length = 50
allow_unknown_trust_level = true

# GitHub 設定
[skill_import.github]
token = "${GITHUB_TOKEN}"
default_branch = "main"
```

---

## 總結

Skill Import Pipeline 確保匯入的技能：

| 階段 | 功能 |
|------|------|
| Discovery | 從各種來源發現技能 |
| Validation | 驗證格式正確性 |
| Deduplication | 避免重複技能 |
| Transformation | 轉換為內部格式 |
| Confidence Gate | 控制初始 confidence |
| Memory Write | 寫入記憶系統 |
