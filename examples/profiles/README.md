# Loom Profile Templates

三個情境化的 `loom.toml` 範本，反映三種典型使用姿態的差異。每份檔案都是可獨立 `cp` 即用的完整設定。

| 範本檔 | 場景 | 預設姿態 |
|---|---|---|
| [`loom.toml.example.secure`](loom.toml.example.secure) | 企業 / 高敏感資料 / 純 CLI | OS-level sandbox (srt) + 網路 allowlist + autonomy 關 |
| [`loom.toml.example.assistant`](loom.toml.example.assistant) | 7×24 Discord 助手 / 等使用者開口 | Discord bot 主場 + autonomy 關 + 預設安全等級 |
| [`loom.toml.example.autonomous`](loom.toml.example.autonomous) | 自主智能體 / 獨立設備 / 容器 | autonomy 全開 + scope_grants 預先批准 + ⚠ 高風險 |

## 用法

```bash
# 從專案 root：
cp examples/profiles/loom.toml.example.secure     loom.toml   # 企業安全場景
cp examples/profiles/loom.toml.example.assistant  loom.toml   # 7×24 Discord 助手
cp examples/profiles/loom.toml.example.autonomous loom.toml   # 自主智能體（高風險）
```

複製後直接編輯 `loom.toml`（例如填入 timezone、調整 default_model）。完整的 per-key 教學註解請看根目錄的 [`loom.toml.example`](../../loom.toml.example) — 範本檔為求簡潔已精簡通用註解，只保留 profile-specific 的取捨理由。

## 維護姿態

- **主力**：根目錄 `loom.toml.example`（dev baseline）+ 各人本地 `loom.toml`
- **範本**：這三份 — 只在新增/移除 toml key、或情境定義改變時才同步更新
- **保留 drift 預算**：三份範本不保證與 baseline 行對行同步，僅保證情境合理性

主要推薦：**assistant** 是大多數使用者的合理起點；**secure** 與 **autonomous** 是兩極場景，按需挑選。
