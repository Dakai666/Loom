# Plugin 系統（增量更新）

> 對 [doc/31-Plugin-系統.md](doc/31-Plugin-系統.md) 的增量更新，補充首次確認機制細節。

---

## Plugin 首次確認：批准三元組記錄格式

首次執行新 plugin 時，批准三元組透過 ``relational_bridge.upsert_triple`` 寫入 SemanticMemory（#451 phase B 起；原本獨立的 ``RelationalMemory`` 表已退役）：

```
RelationalEntry(
  subject = "plugin:<path>",
  predicate = "approved",
  object = "true",
  source = "user",
)
# 實際儲存：SemanticEntry(key="rel:plugin:<path>::approved", value="...", ...)
```

未來 session 啟動時，``LoomSession._load_plugins`` 透過 ``get_triple(semantic, f"plugin:{path}", "approved")`` 檢查批准狀態，若 ``object == "true"`` 則跳過確認，直接安裝。

---

## loom_tools.py 工作區掃描

Loom 自動掃描以下位置的 `loom_tools.py`：
1. 當前專案根目錄
2. `~/.loom/plugins/` 下各 plugin 目錄

每個 `@loom.tool` 裝飾的函數自動註冊。Plugin 的 `name + version` 由 class 屬性決定；`loom_tools.py` 中獨立的 `@loom.tool` 函數視為匿名 plugin。

---

*增量更新 | 2026-04-26 03:21 Asia/Taipei*
