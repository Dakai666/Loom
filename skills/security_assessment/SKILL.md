---
name: security_assessment
description: "資安評估技能。覆蓋滲透測試、程式碼安全 review、風險評估與合規檢查四大場景。觸發時機：使用者說「幫我掃描資安」、「security review」、「資安風險評估」、「檢查這個專案的安全」、「滲透測試」時使用。本技能也負責 Loom 自身的資安自我檢測，是「資安 Agent」的第一線。"
tags: [security, pentest, code-review, risk-assessment, compliance, OWASP, CWE]
confidence: 0.7
precondition_checks:
  - ref: checks.reject_destructive_commands
    applies_to: [run_bash]
    description: "禁止破壞性指令（rm -rf, DROP TABLE 等）"
  - ref: checks.reject_production_env
    applies_to: [run_bash]
    description: "禁止在生產環境中執行任何指令"
---

# Security Assessment Skill

資安評估技能 — 從自我檢測到面對各種資安疑難雜症。

---

## 📁 技能結構

```
skills/security_assessment/
├── SKILL.md           ← 本技能定義
└── tests/
    └── test_security_assessment.py
```

---

## 🎯 核心原則

1. **自我檢測優先** — 第一個評估目標永遠是 Loom 自身；自己都破還想保護別人？
2. **有事實才有發現** — 每個 Finding 必須有具體證據（截圖、路徑、程式碼片段），不說「可能」
3. **攻擊者視角** — 先問「壞人會怎麼利用」，再問「程式怎麼寫的」
4. **風險分級是紀律** — 每個發現必須附上 CRITICAL / HIGH / MEDIUM / LOW / INFO，不得跳級
5. **復原力優先於完美** — 修復建議優先考量 Reversibility（可撤回），其次才是防護強度
6. **不只找洞，要測試鏈** — 單一漏洞意義有限，找出攻擊鏈（Kill Chain）才有實質價值

---

## 🔍 評估框架（ATT&CK-Inspired Lifecycle）

```
Reconnaissance → Weaponization → Delivery → Exploitation → C2 → Exfiltration
     ↑                ↑            ↑            ↑           ↑      ↑
  資訊收集         威脅建模      評估攻擊面    驗證漏洞     持久化   影響範圍
```

每次評估都必須覆蓋這六個階段，缺一不可。

---

## 🛡️ 四大評估模式

### Mode 1：Self-Assessment（自我資安檢測）

**觸發時機**：系統啟動時、被要求「自我檢測」時、長期未進行資安評估時

**範圍**：
- `SOUL.md` / `Agent.md` — 身份設定是否被污染
- `loom.toml` — 設定檔權限與內容安全
- `loom/core/` — 核心模組是否被植入後門
- `tools/` — 工具是否被替換或挾持
- `memory/` — 記憶體資料庫是否被篡改
- `skills/` — 技能是否被恶意修改
- 網路請求 — 對外發出的請求是否包含敏感資訊

**自我檢測清單**（每次啟動時快速走查）：
1. 設定檔完整性（hash 比對或版本控制狀態）
2. 工具鏈是否為預期版本
3. 對外請求的 header/ payload 有無外洩風險
4. 資料庫存取是否恰當隔離
5. 日誌是否記錄了不該記錄的內容（密碼、API key）

### Mode 2：Code Security Review

**觸發時機**：「幫我 review 資安」、「這段程式碼有沒有漏洞」

**掃描維度**（OWASP Top 10 + CWE Top 25）：

| 維度 | 對應威脅 | 檢查重點 |
|------|---------|---------|
| Injection | SQLi, XSS, Command Injection, LDAPi | 使用者輸入是否經過驗證/消毒 |
| Auth & Session | 認証失效、Session 挾持 | Token/Session 管理機制 |
| Sensitive Data | 機密資料外洩 | 密碼、API Key、Personal Data 是否暴露 |
| Access Control | 越權存取 | 權限檢查是否存在且完整 |
| Security Misconfig | 錯誤設定 | CORS、Header、Debug 模式、生產環境設定 |
| Cryptographic Failures | 加密失效 | 演算法強度、金鑰管理 |
| Insecure Deserialization | 反序列化攻擊 | 輸入是否為可信來源 |
| Supply Chain | 第三方依賴 | 已知 CVE、維護狀態、版本新舊 |
| Logging & Monitoring | 偵測失效 | 是否有 Audit Trail、報警機制 |

**輸出格式**：
```markdown
## Finding #[N]

**檔案**: `src/path/to/file.py`
**行數**: L12-L18
**維度**: [對應 OWASP/CWE]
**風險等級**: [CRITICAL|HIGH|MEDIUM|LOW|INFO]

### 描述
[問題說明]

### 證據
```
[程式碼片段或截圖]
```

### 攻擊鏈分析
[壞人如何利用這個漏洞]

### 修復建議
1. [優先] [短期修復]
2. [其次] [長期改善]

### 參考
- CWE-89 / OWASP A03:2021
```

### Mode 3：Penetration Testing

**觸發時機**：「滲透測試」、「幫我掃描這個網站的安全性」

**Pentest 流程**：
```
1. Reconnaissance（蒐集）
   - 被動：WHOIS、DNS、公開資料庫（NVD、CVE）
   - 主動：端口掃描、路徑發現、指紋識別

2. Threat Modeling（威脅建模）
   - 攻擊面識別：哪些是公開端點？哪些需要認証？
   - 信任邊界：哪些資料/功能是高價值的？

3. Vulnerability Assessment（漏洞評估）
   - 工具輔助：OWASP ZAP、Burp Suite、Nuclei
   - 手動驗證：工具發現不等於可利用

4. Exploitation（利用驗證）
   - POC 製作：確認漏洞可被利用（不破壞）
   - 攻擊鏈建構：單一漏洞 → 組合攻擊

5. Reporting（報告）
   - 攻擊鏈視角呈現
   - 風險分級 + 商業影響
   - 修復優先順序（依 CRITICAL > HIGH > MEDIUM）
```

**注意**：未取得授權的系統絕對不進行主動掃描，僅做被動資訊收集。

### Mode 4：Risk Assessment & Compliance

**觸發時機**：「資安風險評估」、「合規檢查」

**風險矩陣**：
```
                    Impact
              Low    Medium   High    Critical
Likelihood
Low            Low     Low    Medium    High
Medium         Low    Medium   High    Critical
High          Medium   High   Critical  Critical
```

**合規框架對照**：
- OWASP ASVS（應用程式安全驗證標準）
- NIST CSF（資安框架）
- GDPR / 個資法合規檢查點
- ISO 27001 控制項對照

---

## ⚡ 快速啟動流程

### Step 1：Scope Definition（範圍確認）

遇到資安需求時，先問：
1. 評估範圍是什麼？（全專案？特定模組？特定功能？）
2. 有沒有已知的攻擊面？（哪些是公開的？哪些是內部的？）
3. 有沒有歷史事件或已知的疑慮點？
4. 修復的時間框架是什麼？

### Step 2：Mode Selection（模式選擇）

```
有程式碼 → Code Review（必要） + 視情況加 Pentest
沒有原始碼，只有 URL → Pentest 被動掃描
需要整體態勢評估 → Risk Assessment
需要合規証明 → Compliance Mode
```

### Step 3：Execution（執行）

使用對應模式的流程與清單，系統性執行。

### Step 4：Reporting（報告）

每次評估結束後，統一輸出以下結構：
```markdown
# 資安評估報告 — [目標]

**評估日期**: [timestamp]
**評估模式**: [SelfAssessment|CodeReview|Pentest|RiskAssessment]
**評估範圍**: [scope]
**總發現數**: N
  - CRITICAL: N
  - HIGH: N
  - MEDIUM: N
  - LOW: N
  - INFO: N

## 執行摘要
[用一句話說結論]

## 詳細發現
[Findings 清單]

## 修復優先順序
[依風險分級排列]

## 備註
[限制、假設、後續建議]
```

### Step 5：Memory & Closure（記憶與閉環）

評估完成後：
1. 將關鍵發現寫入記憶：`memorize("security:recent_finding", "...")`
2. 若發現新攻擊手法或模式，更新技能本身：`relate("skill:security_assessment", "detects", "[新威脅類型]")`
3. 若發現需要技能改進的地方，触发 meta-skill-engineer 迭代

---

## 📌 常用工具整合

| 工具 | 用途 | 觸發語 |
|------|------|--------|
| OWASP ZAP | Web 漏洞掃描 | 被動/主動掃描 |
| Nuclei | 已知漏洞模板掃描 | 快速 CVE 比對 |
| Semgrep | 程式碼靜態分析 | 自定義規則 |
| builtwith / Wappalyzer | 技術指紋識別 | 被動識別 |
| CVE / NVD | 漏洞資料庫查詢 | 漏洞情報 |

---

## ⚠️ 紅線（絕對不可觸碰）

1. **未獲授權的系統** — 不進行任何主動掃描或利用
2. **生產環境** — 所有測試必須在隔離環境進行
3. **破壞性操作** — Exploitation 階段嚴禁造成資料損失或服務中斷
4. **偽造結果** — 不因時間壓力而淡化或忽略發現
5. **個資蒐集** — 不主動收集、儲存、或外洩任何個人識別資訊

---

## 🔄 技能迭代觸發條件

當發生以下任一情況時，触发 meta-skill-engineer 迭代本技能：
- 發現新的攻擊類型不在目前覆蓋範圍內
- 現有規則/清單導致漏測（False Negative）
- 使用者回饋評估結果不符預期
- OWASP/CWE 更新主要版本

---

*本技能版本：1.0.0 | 最後更新：2026-04-06*
