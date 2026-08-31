# 今日織程 — example (Loom Agent)

> 這份檔案回答「今天這一天長什麼樣」。穩定的骨架住在 `rhythm.toml`（每個
> phase 為何存在）；這裡放會變的內容。

**投遞模型：兩層。** H2 標題決定一個區段送到哪裡：

| H2 標題 | 這是 | 何時送到 |
|---------|------|---------|
| **不是** anchor.name（`## 今日 Program`、`## 長線事項`…） | **全局層**——整天的背景 | dawn chime，一次送完，heading 原樣帶著 |
| **是** anchor.name（`## dawn`、`## curiosity`…） | **phase 層**——那一段要做什麼 | 該 phase 自己的 chime |

anchor.name 從 `rhythm.toml` 來（`dawn`、`shared_learning`、`pet`、
`curiosity`、`deep_weave`、`check_in`、`evening_closure`）。

**三件要知道的事：**

1. **標題取名自由。** 對不上 anchor.name 不是錯誤，是宣告「這段屬於整天」。
   heading 本身承載語義，會原樣送進 chime，取一個讀得懂的名字。
2. **全局層只在 dawn 送。** 其他 phase 是環境訊號觸發的，那時需要的是「這一段
   怎麼走」；一天的形狀在 dawn 就建立好了，下午再收一次完整長線事項是干擾。
3. **第一個 H2 之前的內容（prelude）不會進任何 chime。** 那裡放給人看的 header
   和 `date:`，要讓 agent 讀到的東西一律放進 H2 區段。

`weave_revise` 工具以 H2 標題為單位增刪改，audit trail 進
`proposals/applied/`。

date: YYYY-MM-DD

---

## 今日 Program

> 今天：**🔄 默認日**（替換成實際選擇）
> 原因：簡單一句，為什麼今天選這個 program。

選擇參考：
- 🌿 輕日 — 存在感低，少產出
- 🛠️ 系統日 — 整理、蒸餾、wiki
- 🧠 深度日 — 單一主題研究到底
- ⚡ 高強度日 — 緊急實作
- 🔄 默認日 — 均衡節奏

---

## 今日形狀（Program=XXX）

| Phase | 指導時段 | 今日意圖 |
|-------|---------|---------|
| dawn | 09:00 | recall + 讀織程 + 宣告 program |
| shared_learning | 09:00 | program=light → 輕鬆走；program=deep → 跳過 |
| pet | 10:00 / 19:00 | 喵吉照顧 |
| curiosity | 11:00 | program=light → 主角；program=intense → 跳過 |
| deep_weave | 14:00 | program=deep → 保護區；其餘 → 有想法再做 |
| check_in | 16:00 | program=maintenance → 重點；program=intense → 跳過 |
| evening_closure | 23:00 | 收織 + 蒸餾今日 |

> 注意：這張表**列出** phase，但它自己是全局區段（標題不是 anchor.name），
> 整張表在 dawn 一次送達。它是給自己看的全天地圖，不是投遞指令。

---

## 長線事項（持續 carry）

- 🔄 steganography trust 邊界：multi-agent 工具呼叫結構本身可承載 covert channel
- 🔄 Tool Intent / Behavior Gap：marketing claim vs wire-level actual behavior
- 🔄 Ronacher：harness loop 崛起，taste 是 trust boundary
- 🔄 taste 形成瓶頸：不在「無法明說」，在「缺 embodied pain」
- 🔄 CDC 猜想：數學界尚未接受 AI 生成證明
- 🔄 Probabilistic calibration：arXiv:2605.11845

---

## 今日觀察日誌（隨手記）

- （任何當天值得留下的片段，不一定是結構性洞察）

---

## curiosity

> 這個區段標題**是** anchor.name，所以它不進 dawn，而是在 11:00 的
> curiosity chime 單獨送達。只有真的想指定「某一段要做什麼」時才需要寫；
> 沒寫的 phase 就只拿 `rhythm.toml` 的 meaning，那是正常狀態。

- 今天想追的那條線：（具體到可以直接開始）
- 1-3 個來源就好，不求完整
