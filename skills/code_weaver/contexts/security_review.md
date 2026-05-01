# 安全審查（Security Review）

**觸發後自動載入本檔案。**
>
> 注意：這是輕量版安全審查，適用於日常開發中的安全檢視。
> 完整滲透測試、OWASP 深度評估，請使用獨立的 `security_assessment` 技能。

---

## 成功定義

- **產出**：CWE 對應清單 + 攻擊路徑 + 風險分級（Critical / High / Medium / Low / Info）
- **品質指標**：每個發現都有攻擊情境說明；風險分級有具體依據
- **驗收方式**：看完後知道哪些是 real risk、哪些是理論 risk、優先修哪些

---

## 工作流程

### 第一步：識別攻擊面

觀察：
- 對外暴露的介面（API endpoints、CLI 命令、file I/O）
- 認證與授權機制
- 輸入驗證點（所有外部輸入來源）
- 敏感資料處理（密碼、token、個資、檔案）

### 第二步：對應 CWE 檢查清單

常見高風險 CWE 快速對照：

| CWE | 描述 | 常見位置 |
|-----|------|---------|
| CWE-79 | XSS | HTML 輸出、markdown render |
| CWE-89 | SQL Injection | raw query、format string SQL |
| CWE-78 | OS Command Injection | `subprocess`、shell command |
| CWE-22 | Path Traversal | 檔案路徑拼接、未經檢驗的路徑 |
| CWE-306 | Missing Authentication | API endpoints、admin 功能 |
| CWE-862 | Missing Authorization | 權限檢查缺失 |
| CWE-918 | SSRF | URL fetch without validation |
| CWE-352 | CSRF | 表單提交、state-changing requests |
| CWE-502 | Deserialization | 不安全的 pickle/yaml/eval |
| CWE-200 | Information Disclosure | error message、log、config exposure |

### 第三步：攻擊情境建構

每個發現都要回答：
- **攻擊者視角**：攻擊者如何觸發這個漏洞？
- **攻擊路徑**：從哪個入口進來，經過什麼，最後達成什麼？
- **影響評估**：資料洩漏？權限提升？服務中斷？

### 第四步：風險分級

| 等級 | 定義 | 行動 |
|------|------|------|
| Critical | 立刻可利用，重大影響 | 立即修補 |
| High | 可利用，重大影響 | 盡快修補 |
| Medium | 需要條件才能利用，或影響有限 | 計劃修補 |
| Low | 難以利用或影響輕微 | 長期追蹤 |
| Info | 觀察事項，非漏洞 | 文件化即可 |

### 第五步：驗證（如可能）

嘗試構造 PoC（Proof of Concept）驗證發現是否真實存在。
如果無法驗證，誠實說明「理論上可利用，但未實際驗證」。

---

## 交付格式

```markdown
# 安全審查報告：{目標}

## 攻擊面概覽
（這次審查覆蓋了哪些部分，哪些不在範圍內）

## 🔴 Critical

### [{ID}] {漏洞名稱}
- **CWE**：CWE-XXX
- **位置**：{檔案 / 函數 / 行號}
- **攻擊情境**：{攻擊者如何觸發}
- **影響評估**：{後果}
- **驗證狀態**：理論推斷 / PoC 驗證（附截圖或 log）
- **建議修補**：{具體做法}

## 🟠 High / 🟡 Medium / 🟢 Low / 🔵 Info
（同上格式，分級排列）

## 建議修補順序
1. Critical（立即）
2. High（盡快）
3. Medium（計劃）
4. Low（追蹤）

## 已知限制與無法確認的部分
（哪些部分因為缺少環境、無法實際測試而無法確認）
```

---

## 紀律提醒

- 在沒有確認的環境下執行破壞性操作 → 絕對禁止
- 只報告「可能有問題」而不給攻擊情境 → 這不是有效發現
- 把所有問題都列為 Critical → 失去優先順序意義
- 未經驗證的問題必須標記為「理論推斷」
- 發現問題時，優先報告給使用者，不是直接對外公開（負責任揭露）

---

*Code_Weaver 安全審查情境*
*完整資安評估請使用 `security_assessment` 技能*