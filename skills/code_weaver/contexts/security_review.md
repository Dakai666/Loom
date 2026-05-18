# 安全審查轉介（Security Review）

**觸發後自動載入本檔案。**

Code_Weaver 只負責日常開發中的輕量安全檢視與程式碼導航。
完整 OWASP/CWE 深度評估、滲透測試、威脅建模，請改用 `loom_security_assessment`。

---

## 使用邊界

留在 Code_Weaver：
- PR 中某段 code 是否有明顯安全 regression。
- 幫 `loom_security_assessment` 找入口、資料流、相關符號。
- 對小範圍改動做 CWE 快速對照。

轉 `loom_security_assessment`：
- 使用者要求完整安全評估。
- 需要 OWASP/CWE 系統性覆蓋。
- 需要攻擊路徑、風險矩陣、PoC、負責任揭露建議。
- 範圍跨多模組或外部系統。

---

## Code_Weaver 輕量流程

### 1. 識別攻擊面

用 GitNexus 找外部輸入與敏感操作：

```bash
npx gitnexus query "external input endpoint fetch user input auth permission file subprocess"
```

人工補查：
- 認證與授權：`TrustLevel` / `ScopeGrant` / permission checks
- 敏感資料：token、credentials、個資、log
- 高風險 sink：shell、filesystem、network fetch、eval/deserialization
- Loom 特有：`run_bash` / `fetch_url` 的 trust level 傳遞

### 2. 快速 CWE 對照

| CWE | 描述 | 常見位置 |
|-----|------|---------|
| CWE-78 | OS Command Injection | shell/subprocess |
| CWE-22 | Path Traversal | path join / file open |
| CWE-89 | SQL Injection | raw query / format SQL |
| CWE-862 | Missing Authorization | permission guard |
| CWE-918 | SSRF | fetch URL |
| CWE-200 | Information Disclosure | logs/errors/config |
| CWE-502 | Deserialization | pickle/yaml/eval |
| CWE-79 | XSS | HTML/markdown rendering |

### 3. 發現格式

```markdown
### [Severity] Title

**CWE:** CWE-XXX
**Location:** `path:line`
**Attack path:** [攻擊者如何從入口到 sink]
**Impact:** [資料洩漏/權限提升/任意命令/DoS]
**Evidence:** [code/diff/GitNexus/test]
**Verification status:** Confirmed / Theoretical / Not tested
**Recommended fix:** [具體修補方向]
```

Severity：
- **Critical**：可立即利用且重大影響。
- **High**：可利用，影響重大，但需要條件。
- **Medium**：需要較多前置條件或影響有限。
- **Low**：難利用或主要是 hardening。
- **Info**：觀察事項。

---

## 交付格式

```markdown
## Security Scope

[本次只看哪個 PR/檔案/流程；哪些不在範圍]

## Findings

[依 Critical -> Info 排序；沒有就明確寫未發現 Critical/High]

## Limits

[未實際驗證、缺少環境、需轉 loom_security_assessment 的部分]

## Handoff

[若需要深度評估，列出給 loom_security_assessment 的入口和證據]
```

---

## 紀律提醒

- 未驗證的發現要標記 Theoretical，不要寫成已確認漏洞。
- 不做破壞性 PoC，除非使用者明確授權且環境安全。
- 不把所有問題都升級為 Critical。
- 深度安全任務要轉專門技能，不讓 Code_Weaver 假裝完整覆蓋。

---

*Code_Weaver 安全審查轉介情境 · v3.0 — 2026-05-19*
