# 完成前驗證（Verification Gate）

**觸發後自動載入本檔案。**

此情境適用於任何「完成了嗎」「通過了嗎」「確認一下」「可以 commit/PR 嗎」，
也適用於所有 Code_Weaver 情境的最後一步。

---

## 鐵律

```text
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

本輪沒有跑過能證明該聲明的命令，就不能宣稱完成、修好、通過、乾淨、可 merge。

---

## 成功定義

- **產出**：聲明對應的驗證命令 + fresh output 摘要 + 實際狀態
- **品質指標**：每個正向結論都有剛剛取得的證據；失敗時明確說失敗
- **驗收方式**：使用者能直接看到「哪個命令證明了哪件事」

---

## Gate Function

在任何完成宣稱前，照順序執行：

1. **Identify**：我要證明什麼？
2. **Select**：哪個命令或檢查能證明它？
3. **Run**：新鮮執行完整命令，不引用舊結果。
4. **Read**：讀完整 output、exit code、failure count。
5. **Compare**：output 是否真的支持聲明？
6. **State**：只說證據支持的實際狀態。

---

## 常見聲明與所需證據

| 聲明 | 需要的證據 | 不足以證明 |
|------|-----------|------------|
| 測試通過 | 測試命令 exit 0 / failure 0 | 「應該會過」、之前跑過 |
| import 沒炸 | `pytest --collect-only` 或等價 import check | lint 通過 |
| lint/format 乾淨 | linter/formatter check output | 測試通過 |
| bug 修好 | 原始 reproduction 現在通過 | 只看 code diff |
| regression test 有效 | red/green 證據 | test 寫了且 pass |
| 變更未超 scope | `npx gitnexus detect-changes` | 人工掃 diff |
| GitHub 寫入成功 | `gh ... view` 看到剛寫入內容 | CLI command exit 0 |
| release 成功 | tag/release view 可查 | 本地 tag 存在 |

---

## Code_Weaver 預設驗證順序

### 實作 / 修復

```bash
pytest --collect-only
pytest <relevant tests>
npx gitnexus detect-changes
git diff --check
```

必要時加上全套測試、lint、format、build、UI render 或 smoke test。

### PR / diff review

```bash
gh pr view <number> --repo <owner>/<repo> --json number,title,state,files
gh pr diff <number> --repo <owner>/<repo>
npx gitnexus detect-changes
```

若 PR 新增 tool/executor/dataclass/middleware 行為，做最小 runtime invariant check。

### GitHub 寫入

```bash
gh pr comment <number> --repo <owner>/<repo> --body-file outputs/doc/review_body.md
gh pr view <number> --repo <owner>/<repo> --json comments
```

依實際操作替換 `view` 欄位；重點是成功後要查到剛寫入的東西。

### Release

```bash
git tag --list <version>
gh release view <version> --repo <owner>/<repo>
```

---

## 交付格式

```markdown
## Verification

**Claim checked:** [要證明的聲明]
**Command:** `[實際命令]`
**Result:** [pass/fail + 關鍵 output]
**Status:** [現在能誠實宣稱什麼；還不能宣稱什麼]
```

若有多個命令，列成多個 bullet。不要只寫「驗證通過」。

---

## 失敗時怎麼說

```markdown
驗證沒有通過。

**Command:** `pytest tests/test_x.py`
**Result:** 3 failed, 42 passed
**Meaning:** 目前不能宣稱修復完成；失敗集中在 [area]。
**Next step:** 回到 systematic_debugging / feature_implementation 處理。
```

---

*Code_Weaver 完成前驗證情境 · v3.0 — 2026-05-19*
