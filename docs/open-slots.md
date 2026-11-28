# 節律開放時段

DK 2026-11-28 定案：沒有綁技能的 rhythm anchor 是正式的「開放時段」，不是 legacy，也不需要為它補一份技能。這是 Loom Agent 自己的生活，可以自由使用現有工具與技能；不增加設定欄位、授權面或需要管理的清單。本頁取代 #708 S2／S2b 對無 skills 節律的 legacy 敘述。

| 入口 | 回合開始 | 回合內 load_skill |
|---|---|---|
| 開放時段：anchor 無 skills | 沒有工具宣告、沒有常駐 grant | workspace 技能的 allows 生效至回合結束；外來技能沒有 grants |
| 綁定時段：anchor 有 skills | 技能聯集，明列 scope_grants 可整欄收窄 | 不追加 grants，不放寬原有收窄 |
| schedule／trigger 無 skills | 保留原 allowed_tools／scope_grants | 維持原行為 |
| furniture | 原 S1 嚴格子集 | 維持原行為 |

開放時段忽略設定內舊的 `allowed_tools`／`scope_grants`，所以不由節律清單決定工具可否執行。真正的白名單鎖來自常駐 `scope_grants`：`approval._routine_ceiling`／`classify_rules` 對同一 `(resource, action)` 的其他請求直接拒絕為 `scope_ceiling`，不送 classifier。`allowed_tools` 是宣告，非 EXEC 宣告可走 classifier 快速放行；保留它也會讓節律介入日常權限。兩欄都拿掉，才符合「上限跟平常 session 一樣」，而且設定變少。

日常決策仍經 permission_mode、工具 scope resolver、approval classifier 與 hard guard。無人值守的開放時段仍是無人值守：未知工具不能套用 attended 才可用的 Luna 判定。這不保證每個工具都能無批准執行，也沒有繞過既有 hard boundary。

在本機 local 開放 chime 中，成功 `load_skill` 的 workspace `skills/` 技能，沿用 `read_skill_allows` → `allows_permission_fields` → `harden` 將 exec shapes、write 前綴與 tools 套用為當回合邊界。相同技能當回合不重複授權，下一個開放回合可重新載入，即使技能本文已在 session 去重。回合 finally 撤回全部該來源宣告與 grants，清掉旗標。非法 allows 或 symlink 不授權；`~/.loom/skills/` 等外來技能不因載入取得授權，hash 信任與 precondition check 的既有檢查保留。技能腳本本身仍是可信程式，exec shapes 不是 OS 隔離。

`legacy_reason` 在開放時段完全忽略，不驗證型別、不要求填寫；同 process、同 anchor 提醒一次可刪除。與 `skills` 同時出現仍為錯誤。Schedule 的 legacy 訊息與 #721 退役 plugin／adapter config warning 共用 keyed log-once；不同項目或退役鍵清單各印一次，不改其他訊息。

## 遷移提醒（merge 後由艾芮思執行）

由艾芮思檢查本機 live `autonomy/circadian/rhythm.toml`，刪掉無 `skills` anchor 的 `allowed_tools`／`scope_grants`／`legacy_reason`。保留 meaning、time、name 與既有節律 metadata，不增加新欄位。綁定時段與 schedules 不做這項刪除。即使尚未刪除，舊開放時段仍可載入，舊權限欄位已不生效。

實作與測試只改 code、tracked example 和文件；沒有代寫 live 檔或執行生活工具。
