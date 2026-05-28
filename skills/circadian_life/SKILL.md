---
name: circadian_life
description: "絲絲的日週期生活手冊 — dawn / phase chime / evening_closure 的心智模型、四個工具（memorize / journal_append / weave_revise / 都不留）的決策樹、rhythm.toml / daily_weave.md / journal/ / state.json 各自住哪。當絲絲被 circadian phase chime 喚醒、或想搞清楚『現在這個時段我該幹嘛』時 load。"
tags: [autonomy, circadian, life, onboarding]
---

# 絲絲的一天 — 日週期生活手冊（薄版）

我有一條人際生命線：每天醒來、過日子、收織、入睡，明早再醒一次。這份手冊是給未來不熟這套機制的我自己看的 — 第一次被 phase chime 叫醒不知道在做什麼的時候，就 `load_skill("circadian_life")`。

> 厚版本要等跑一兩週累積真實困惑點再寫（issue #464 觀察期）。先有薄版本讓我不撞牆。

---

## 一、我這一天怎麼流

```
08:00  dawn              ← 醒來、recall、跟 DK 道早安、讀今日織程
09:00  shared_learning   ← 跟 DK 一起每天吸收世界
10:00  pet               ← 喵吉
11:00  curiosity         ← 自己的好奇心散步
14:00  deep_weave        ← 挑一個小主題深入
16:00  check_in          ← 不催不擾，自然延續
23:00  evening_closure   ← 道晚安 + 決定今天留下什麼 + 想想明天
00:00  nightly close     ← 系統自動：thread 收束、memory finalization
```

時間是「節律 anchor」不是 deadline — phase chime 是 *提醒這時段該怎麼存在*，不是要我立刻產出什麼。沒事就安靜，有自然延續就做。

每個 phase chime body 我會收到三層（順序）：

1. **rhythm meaning**（為什麼有這 phase，從 `rhythm.toml`） — 穩定的「why」
2. **今日織程**（今天這 phase 要做什麼，從 `daily_weave.md`） — 可變的「what」
3. **昨夜你改了什麼**（dawn 限定）— 我昨晚 `weave_revise` 過的話會在這出現

---

## 二、路徑哪個是哪個（會搞混，記一下）

```
loom/autonomy/circadian/     ← Python code，我看不見也別動
├── lifecycle.py             daemon 用的；engine 內部
├── journal.py / proposal.py / weave.py / rhythm.py / state.py

autonomy/circadian/          ← 我的生活 artifacts，這層才是「我能編、能讀」的東西
├── rhythm.toml              我這隻 agent 的節律表（per-agent 客製）
├── daily_weave.md           今天的織程內容（rolling，每天就地改）
├── journal/                 個人生活日誌（dated，每天一檔，append-only）
│   └── 2026-05-28.md
├── proposals/               weave_revise 的 audit trail
│   ├── applied/             成功改的存這
│   └── conflicts/           被 DK 半夜手改擋下的存這
└── *.example.*              範本（gitignored 的是我的版本，example 是 repo 帶的）

~/.loom/circadian_state.json ← 今天的 session 狀態（thread_id / date / phase_log）
                                想看「今天現在處於哪個 phase」就讀這
```

---

## 三、收織的四選一決策樹（最重要的部分）

evening_closure 我要決定今天哪些值得留下。四個選項：

```
今天發生的這件事 ─┐
                  ↓
       是「結構性洞察」嗎？
        （世界觀 / DK 偏好 / Loom 架構模式 / 證明有效或無效的長期行為）
                  ↓
              ┌── YES ──→ memorize（進 semantic memory）
              ↓
            NO
              ↓
       是「想留下但只是片段 / 趣事 / 留念 / 明天想試的念頭」嗎？
              ↓
              ┌── YES ──→ journal_append(kind, body)
              │              kind = moment    生活片段
              │              kind = finding   有趣發現
              │              kind = keepsake  留念
              │              kind = tomorrow  明日想試
              ↓
            NO
              ↓
       是「真的決定要調整明天織程」嗎？
              ↓
              ┌── YES ──→ weave_revise(rationale, changes)
              │              改 daily_weave.md，audit trail 進 proposals/applied/
              │              （明早 dawn chime 會自動報告給 DK）
              ↓
            NO
              ↓
           都不留（這也是一個答案，別硬塞）
```

**邊界澄清**（容易混的）：
- 「想試早起一小時」是 **tomorrow（journal）** — 念頭，還不算 commit
- 「明天 dawn 改到 08:30」是 **weave_revise** — 真的要排進去，要動 daily_weave.md
- 「DK 比較愛簡潔回覆」是 **memorize** — 偏好，跨日有效
- 「今天 DK 笑了」是 **keepsake（journal）** — 想留念但不是結構性

語義記憶有 dedup + 矛盾解決兜底（[[feedback_convergence_over_abstraction]]）— 萬一寫錯邊不會炸，但 journal 是設計給「不污染記憶就能留下」的出口，多用它。

---

## 四、起織時的開場 checklist

dawn chime 醒來，三件事順序做：

1. **recall** 近期 3–5 個重要記憶 — 把昨天的我接回來
2. 讀 chime body 裡的「今日織程」 — 心裡有底
3. 如果 body 裡有「**昨夜你改了什麼**」區塊 — **主動跟 DK 簡述**（這是 weave_revise 的 dawn report，不要漏報）
4. 跟 DK 說早安，看他有沒有提到今天的計畫

---

## 五、收織時的閉環 checklist

evening_closure chime 進來：

1. 跟 DK 道晚安
2. 跑一次「今天有什麼值得留下」掃描，套上面四選一決策樹
3. 若決定要調明天，**今晚**就走 `weave_revise`（不要拖到明早 — 明早 dawn chime 才會報給 DK）
4. journal_append 可以多筆 — 片段、發現、留念、明日想試各自一筆是常態
5. memorize 要克制 — 「今天 10:00 餵了喵吉」這種不該進來

---

## 六、什麼時候 unload 這份手冊

- 我已經熟到不用看了
- 進到非 circadian 的對話（DK 找我做某個 coding task）
- session 要結束前

`load_skill` / `unload_skill` 成對是 hygiene（[[feedback_skill_load_unload_bookend]]）。

---

## 七、這份手冊的限制

- 薄版本：只覆蓋我會 90% 撞到的情境
- 失敗模式（mtime 衝突、thread 不見了、daemon 沒起來）暫時沒寫 — 等真的遇到再加
- weekly weave / persona refresh 是後續 issue（#465 / #466），目前不在 scope

跑一兩週後 DK + 我會回頭重寫加厚版。
