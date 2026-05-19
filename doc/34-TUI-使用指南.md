# TUI 使用指南

> ## ⚠️ RETIRED — 2026-05-19
>
> 整套 Textual TUI（`loom chat --tui` / `LoomChatApp` / `loom.platform.cli.tui.*` / `textual` 依賴）**已退役**。背景：Loom 主要互動在 Discord 端 + CLI 直跑，TUI 維護成本（widget bugs、scroll quirks、PIL/Textual 依賴、testing 環境）不對等於使用率。
>
> **替代方案**：
>
> - 互動式 chat → `loom chat`（CLI，prompt_toolkit-based `LoomApp` 仍提供持久 footer / inline confirm / pause overlay 等）
> - 富互動 / 多人 / 長對話 → `loom discord start` + Discord thread
>
> **原稿**：完整 TUI source、components、tests、本指南原版保留在 local `_archive/tui-retired-2026-05-19/`（gitignored）。
>
> **追蹤 issue**：#403
