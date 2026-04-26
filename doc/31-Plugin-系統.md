# Plugin 系統（增量更新）

> 對 [doc/31-Plugin-系統.md](doc/31-Plugin-系統.md) 的增量更新，補充首次確認機制細節。

---

## Plugin 首次確認：RelationalMemory 記錄格式

首次執行新 plugin 時，批准記錄寫入 `RelationalMemory`：

```
RelationalTriple(
  subject = "user",
  predicate = "approved_plugin",
  object = "<plugin_name>",
  source = "manual_confirm"
)
```

未來 session 啟動時，PluginRegistry 檢查 `query_relations(user, approved_plugin, plugin_name)`，若存在則跳過確認，直接安裝。

---

## loom_tools.py 工作區掃描

Loom 自動掃描以下位置的 `loom_tools.py`：
1. 當前專案根目錄
2. `~/.loom/plugins/` 下各 plugin 目錄

每個 `@loom.tool` 裝飾的函數自動註冊。Plugin 的 `name + version` 由 class 屬性決定；`loom_tools.py` 中獨立的 `@loom.tool` 函數視為匿名 plugin。

---

*增量更新 | 2026-04-26 03:21 Asia/Taipei*
