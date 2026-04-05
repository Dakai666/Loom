# Discord 多媒體增強計畫

> 建立日期：2026-04-05
> 用途：記錄 Discord 平台在多媒體（檔案/圖片）支援上的缺口，以及未來開發方向

---

## 1. 現況缺口

目前 `LoomDiscordBot`（`loom/platform/discord/bot.py`）只處理純文字訊息。以下功能完全不存在：

| 缺口項目 | 嚴重程度 | 說明 |
|---------|---------|------|
| 接收用戶上傳的圖片/檔案 | 🔴 高 | `on_message` 只讀 `message.content`，完全忽略 `message.attachments` |
| 主動發送圖片/檔案給用戶 | 🔴 高 | `channel.send()` 只有純文字，沒有任何 `discord.File` 程式碼 |
| 圖片轉 text（OCR/視覺分析） | 🟡 中 | 即使收到圖片，也無法讓 LLM 看見內容 |
| 檔案大小 / 格式限制 | 🟡 中 | 沒有任何 Validation Logic |
| Discord Thread 中下載並保存附件 | 🟡 中 | 缺少 workspace 寫入流程 |

---

## 2. 需求分析

### 2.1 接收上傳（用戶 → Loom）

```
用戶在 Discord 傳送圖片/檔案
  → Bot 攔截 on_message
  → 取出 message.attachments（discord.Attachment 列表）
  → 依序：
      1. 下載到 workspace 臨時目錄（e.g. ~/.loom/discord_attachments/）
      2. 如果是圖片且有視覺模型支援 → 轉為 base64 或路徑傳給 LLM
      3. 如果是文字檔（.txt/.md/.py/.json...）→ 讀取內容
      4. 生成描述：如 "📎 上傳了 2 個檔案：screenshot.png（2.3MB）, report.pdf"
  → 將「檔案描述 + 內容」拼入 user message 一起送入 LoomSession
```

### 2.2 主動發送（Loom → 用戶）

```
Loom 回覆或工具執行結果中含圖片/檔案
  → 攔截：當 response 或 tool output 包含圖片路徑 / base64
  → 使用 discord.File() 發送
  → 注意：Discord 同一訊息最多 8 個附件、每個最大 25MB
  → 超出時需分段或壓縮
```

### 2.3 圖片視覺分析（需要 LLM 視覺能力）

目前 Loom 使用的模型（MiniMax-M2、Claude）若支援視覺輸入，需包裝成 tool：

```
analyze_image(image_path: str, query: str = "描述這張圖片的內容")
  → 讀取圖片為 base64
  → 送入視覺模型
  → 回傳分析結果
```

---

## 3. 技術實作要點

### 3.1 Attachment 下載

```python
# pseudocode — 尚未實現
for attachment in message.attachments:
    url = attachment.url
    filename = attachment.filename
    # 下載到 workspace：~/.loom/discord_attachments/{session_id}/
    local_path = workspace / "discord_attachments" / session_id / filename
    await attachment.save(local_path)
```

### 3.2 圖片 → LLM 上下文

有兩種策略：

**策略 A：當成額外使用者訊息**
```python
content = content  # 用戶文字
files_block = "\n\n[上傳檔案]\n" + "\n".join(descriptions)
combined = f"{content}\n{files_block}"
```

**策略 B：實作專屬工具讓 LLM 主動查詢**
```python
read_attachment(path: str)  # SAFE tool，讀取已下載的附件內容
analyze_image(path: str, question: str)  # GUARDED tool，視覺分析
```

### 3.3 發送檔案（Loom → Discord）

```python
# pseudocode — 尚未實現
if output_contains_image(result):
    image_path = extract_image_path(result)
    await message.channel.send(
        file=discord.File(image_path)
    )
```

### 3.4 安全考量

| 項目 | 說明 |
|------|------|
| 副檔名白名單 | 限定允許的類型（圖片：png/jpg/gif/webp，文字：txt/md/py/json，文件：pdf）|
| 檔案大小上限 | Discord 25MB，但 workspace 應設更嚴格的上限（如 10MB）|
| 路徑隔離 | 附件必須存於 `~/.loom/discord_attachments/{session_id}/`，不可直接進 workspace |
| 自動清理 | Session 結束後刪除該 session 的附件目錄 |
| prompt injection | 用戶上傳的檔案內容需過 `sanitize_untrusted_text()` |

---

## 4. 實作順序建議

### Phase 1：基礎接收（最高優先）

- [ ] `message.attachments` 攔截
- [ ] 圖片/文字檔下載至 `~/.loom/discord_attachments/`
- [ ] 拼接為「上傳描述文字」附加到 user message
- [ ] 副檔名 / 大小限制
- [ ] Session 結束時清理附件

### Phase 2：主動發送

- [ ] 攔截 `stream_turn` 中的 tool result / final response
- [ ] 識別含圖片路徑的輸出
- [ ] `channel.send(file=discord.File(path))`
- [ ] 多檔分段邏輯（8個附件上限）

### Phase 3：視覺分析工具

- [ ] `analyze_image` tool 定義（若模型支援 vision）
- [ ] `read_attachment` tool（讀取已下載的文字檔）
- [ ] tool result 自動轉為 Discord 訊息回傳

### Phase 4：增強 UX

- [ ] 上傳進度指示（`📥 下載中…` → `✅ 已收到 N 個檔案`）
- [ ] 圖片預覽缩略（Discord 會自動生成预览）
- [ ] 附件列表 `/attachments` 命令
- [ ] 錯誤處理（下載失敗、格式不支援）

---

## 5. 相關檔案

| 檔案 | 角色 |
|------|------|
| `loom/platform/discord/bot.py` | 主要修改目標 |
| `loom/platform/cli/tools.py` | 參考 tool 定義模式 |
| `loom/core/harness/permissions.py` | TrustLevel 調整（attachments → GUARDED）|
| `loom/core/harness/registry.py` | 新工具註冊 |
| `doc/39-新增工具.md` | 新增 tool 的標準流程 |

---

## 6. 附：Discord Attachment API 參考

```python
# discord.Attachment 重要屬性
attachment.url          # CDN URL（可用於下載）
attachment.filename     # 原始檔名
attachment.size         # 位元組大小
attachment.content_type # MIME type

# 下載（asyncio）
await attachment.save(fp, use_cached=False)

# 發送
await channel.send(file=discord.File(fp))
await channel.send(content="文字說明", file=discord.File(fp))
```
