# 節律開放時段

DK 2026-11-28 定案：沒有綁技能的 rhythm anchor 是正式的「開放時段」，不是 legacy，也不需要為它補一份技能。這是 Loom Agent 自己的生活，可以自由使用現有工具與技能。本頁取代 #708 S2／S2b 對無 skills 節律的 legacy 敘述，並採 PR #723 審查後裁決：**保留 allowed_tools 宣告、移除 scope_grants 天花板、忽略 legacy_reason**，不新增設定欄位或授權機制。

| 入口 | 回合開始 | 回合內 load_skill |
|---|---|---|
| 開放時段：anchor 無 skills | 保留 allowed_tools 宣告、忽略 scope_grants | workspace 技能的 allows 作正向授權，不當天花板；checks 必須成功掛載，外來技能沒有 grants |
| 綁定時段：anchor 有 skills | 技能聯集，明列 scope_grants 可整欄收窄 | 不追加 grants，不放寬原有收窄 |
| schedule／trigger 無 skills | 保留原 allowed_tools／scope_grants | 維持原行為 |
| furniture | 原 S1 嚴格子集 | 維持原行為 |

真正的白名單鎖來自常駐 `scope_grants`：`approval._routine_ceiling`／`classify_rules` 對同一 `(resource, action)` 的其他請求直接拒絕為 `scope_ceiling`，不送 classifier。開放時段忽略這一欄，拿掉這道牆。`allowed_tools` 是宣告，不是鎖：它不封殺未列名工具，非 EXEC 宣告讓工具走 `_auto_decide` 的 declared 快速放行，仍經 hard guard。這份既有宣告繼續以 unattended_only boundary 套用，不建立 ScopeGrant、不授權 EXEC。

不能連宣告一起拿掉：phase chime 是 `origin="chime"`，屬於 unattended；`_everyday_tool` 與未知工具進 Luna 的路徑都要求 attended。沒有宣告的 memorize／pursuit_write／spawn_agent／image_generate 會被 not_covered 拒絕，造成日常功能迴退。保留既有宣告讓這些工具繼續能用；未列名工具仍依 permission_mode、resolver、classifier 與 hard guard 判定，不保證每個未宣告工具都能在無人值守時執行。

在本機 local 開放 chime 中，成功 `load_skill` 的實際目錄必須正好是 workspace `skills/<name>`；不以 `-`／`_` 變體猜另一個技能的 allows。hook 帶 origin、目錄與 checks 掛載結果，attended 回合不安裝授權，即使旗標殘留也一樣。宣告了 precondition checks 卻未核准、解析失敗、缺 manager、任何 ref 的 applies_to 為空，或只部分掛載時，不安裝 allows，activation telemetry 仍記錄；無 checks 的技能照常。後續載入卸掉舊技能的 checks，也一併撤回那個技能的 grants。

具名 `unload_skill(name)` 表示不再使用該技能：即使它沒有 checks、removed 為 0，仍撤回當前 open source 下該技能的 grants 與 declaration。重複卸載冪等；不帶 name 只列清單，不撤權。共用撤權 helper 不動 anchor 宣告、其他技能或其他來源。session stop 的 unmount_all 後也清理當前所有 open-slot 技能授權（包含無 checks 的技能）；同技能重掛在同步 mount 中完成，若任何 check 未成功掛回，load hook 會撤權。

本地技能沿用 `read_skill_allows` → `allows_permission_fields`（內含 harden），將 exec shapes、write 前綴與 tools 套用為當回合的**正向授權**。來源為 `<open_slot_source>:skill:<name>` 的 grants 在這個開放回合不算 `_routine_ceiling`：載入 pet 後依然能 ls／git status，載入 news 的 write 後依然可由一般判定寫 outputs 或 journal。綁定時段、schedule、furniture、external 與 subagent 不享有這個例外，其 ceiling／max_calls 等收窄完全保留；所有 hard guards 不變。

相同技能當回合不重複授權，下一個開放回合可重新載入，即使本文已在 session 去重；去重也重新確認 checks 仍掛載。回合 finally 撤回全部來源宣告與 grants，清旗標。非法 allows、symlink、外來目錄不授權，hash 信任與 checks 核准機制照舊；exec shape 不是 OS 隔離。

`legacy_reason` 在開放時段完全忽略，不驗證型別、不要求填寫；同 process、同 anchor 提醒一次可刪除。與 `skills` 同時出現仍為錯誤。Schedule 的 legacy 訊息與 #721 退役 plugin／adapter config warning 共用 keyed log-once；INFO 與 WARNING 是不同 key，不互相吃掉，不同項目／退役鍵清單各印一次。工具以 lock 保護多執行緒去重，不改其他訊息。

## 未來選項

若要連既有宣告也不維護，必須另行放寬無人值守 classifier 路徑。這會讓她自己寫的節律 meaning 進入無人值守授權判定，屬於安全政策改變，留給 DK 拍板；本片不做。刪除 allowed_tools 會回到純開放的一般判定，未覆蓋的無人值守工具可能被 skip，因此不自動刪除此欄。

## 遷移提醒（merge 後由艾芮思執行）

由艾芮思檢查本機 live `autonomy/circadian/rhythm.toml`，刪掉無 `skills` anchor 的 `scope_grants`／`legacy_reason`，**allowed_tools 可以留，是宣告**。是否另刪宣告由艾芮思決定，並依前述純開放行為驗證工具呼叫。保留 meaning、time、name 與既有節律 metadata。綁定時段與 schedules 不做這項刪除；舊開放設定仍可載入，宣告繼續有效、舊 scope_grants 已忽略。

實作與測試只改 code、tracked example 和文件；沒有代寫 live 檔或執行生活工具。
