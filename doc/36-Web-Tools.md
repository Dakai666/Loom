# Web Tools

Web Tools 是 Loom 的網路工具集，允許 agent 訪問網頁和搜尋資訊。

---

## 內建 Web Tools

| 工具 | 功能 | Trust Level |
|------|------|-------------|
| `fetch_url` | 讀取網頁內容 | SAFE |
| `web_search` | 搜尋網路 | SAFE |

---

## fetch_url

### 功能

讀取指定 URL 的網頁內容，並返回清理後的文字。

### 定義

```python
# loom/core/tools/web/fetch_url.py
class FetchUrlTool(Tool):
    name = "fetch_url"
    description = "Fetch a URL and return the page title and cleaned body text. Use this to read web pages, documentation, or articles. Output is truncated to 2000 chars."
    trust_level = "SAFE"
    
    async def execute(self, args: dict, context: dict) -> ToolResult:
        url = args["url"]
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Loom/1.0"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    content = await response.text()
                    
                    # 清理 HTML
                    cleaned = self._clean_html(content)
                    
                    # 截斷
                    if len(cleaned) > 2000:
                        cleaned = cleaned[:2000] + "..."
                    
                    return ToolResult(
                        success=True,
                        output=cleaned,
                        metadata={
                            "url": url,
                            "status": response.status,
                            "title": self._extract_title(content),
                        }
                    )
        
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
            )
    
    def _clean_html(self, html: str) -> str:
        """移除 HTML 標籤和腳本"""
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, "html.parser")
        
        # 移除腳本和樣式
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        
        # 取得文字
        text = soup.get_text(separator="\n", strip=True)
        
        # 合併空行
        lines = [line for line in text.split("\n") if line.strip()]
        
        return "\n".join(lines)
    
    def _extract_title(self, html: str) -> str:
        """提取標題"""
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        
        return title.get_text(strip=True) if title else ""
```

### 使用方式

```python
# Agent 呼叫
result = await harness.execute(
    tool="fetch_url",
    args={"url": "https://example.com/docs"},
)
```

### CLI 測試

```bash
loom tool test fetch_url url="https://example.com"
```

### 限制

- 輸出限制 2000 字元
- 請求逾時 10 秒
- 不執行 JavaScript

---

## web_search

### 功能

使用 Brave Search 搜尋網路，返回標題、URL 和描述。

### 定義

```python
# loom/core/tools/web/web_search.py
class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web via Brave Search and return top results with titles, URLs, and descriptions. Use this to find current information, documentation, or answers."
    trust_level = "SAFE"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    async def execute(self, args: dict, context: dict) -> ToolResult:
        query = args["query"]
        count = args.get("count", 5)
        
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://api.search.brave.com/res/v1/web/search"
                headers = {
                    "X-Subscription-Token": self.api_key,
                    "Accept": "application/json",
                }
                params = {
                    "q": query,
                    "count": min(count, 10),  # 最多 10 個
                }
                
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    data = await response.json()
                    
                    results = self._parse_results(data)
                    
                    return ToolResult(
                        success=True,
                        output=results,
                        metadata={
                            "query": query,
                            "result_count": len(results),
                        }
                    )
        
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
            )
    
    def _parse_results(self, data: dict) -> str:
        """解析搜尋結果"""
        results = data.get("web", {}).get("results", [])
        
        if not results:
            return "No results found."
        
        lines = []
        for i, result in enumerate(results, 1):
            title = result.get("title", "")
            url = result.get("url", "")
            desc = result.get("description", "")
            
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            lines.append(f"   {desc}")
            lines.append("")
        
        return "\n".join(lines)
```

### 使用方式

```python
# Agent 呼叫
result = await harness.execute(
    tool="web_search",
    args={
        "query": "Python async await tutorial",
        "count": 5,
    },
)
```

### CLI 測試

```bash
loom tool test web_search query="Loom framework" count=5
```

### 限制

- 最多返回 10 個結果
- 請求逾時 15 秒
- 需要 Brave Search API key

---

## 環境變數設定

```bash
# Brave Search API key
export BRAVE_SEARCH_API_KEY="your_api_key"

# 或在 .env 檔案中
BRAVE_SEARCH_API_KEY=your_api_key
```

---

## 工具配置

### loom.toml 配置

```toml
[tools.web]

# fetch_url 設定
[tools.web.fetch_url]
timeout = 10
max_chars = 2000
user_agent = "Loom/1.0"

# web_search 設定
[tools.web.web_search]
provider = "brave"  # 可擴展其他 provider
timeout = 15
max_results = 10
```

---

## 安全考量

### URL 驗證

```python
from urllib.parse import urlparse

def validate_url(url: str) -> bool:
    """驗證 URL 是否安全"""
    parsed = urlparse(url)
    
    # 只允許 http/https
    if parsed.scheme not in ("http", "https"):
        return False
    
    # 拒絕內網 IP
    if parsed.netloc in ("localhost", "127.0.0.1", "0.0.0.0"):
        return False
    
    return True
```

### 內容過濾

```python
class ContentFilter:
    """內容過濾"""
    
    BLOCKED_DOMAINS = [
        "malicious.com",
        "phishing.net",
    ]
    
    def is_allowed(self, url: str) -> bool:
        """檢查 URL 是否允許"""
        parsed = urlparse(url)
        
        for domain in self.BLOCKED_DOMAINS:
            if domain in parsed.netloc:
                return False
        
        return True
```

---

## 擴展 Web Tools

### 新增 Provider

```python
# 新增 Google Search provider
class GoogleSearchTool(WebSearchTool):
    name = "google_search"
    
    def __init__(self, api_key: str, cx: str):
        super().__init__(api_key)
        self.cx = cx
    
    async def execute(self, args: dict, context: dict) -> ToolResult:
        # Google Custom Search API 實作
        ...
```

### 註冊工具

```toml
[tools.web]
provider = "google"

[tools.web.google]
api_key = "${GOOGLE_API_KEY}"
cx = "${GOOGLE_CX}"
```

---

## 總結

Web Tools 提供網路訪問能力：

| 工具 | 功能 | 限制 |
|------|------|------|
| fetch_url | 讀取網頁 | 2000 字元、10 秒逾時 |
| web_search | 搜尋網路 | 10 結果、15 秒逾時 |
