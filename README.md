# WebPageCrawler

Web PDF Crawler 是一个基于 Playwright 的异步网页爬虫工具，能够自动抓取网站内容并保存为 PDF 文件。该工具支持处理多级链接、自动处理 Cookie 弹窗、遵循 robots.txt 规则，并提供灵活的配置选项以适应不同网站的爬取需求。

## 快速开始

### 前提条件
- Python 3.8+
- 安装依赖包：
  ```bash
  pip install playwright pyyaml aiofiles
  playwright install chromium
  ```

### 配置文件
在 `config` 目录下创建 YAML 配置文件（参考示例）：
```yaml
start_url: "https://example.com"          # 起始爬取URL
output_dir: "output/example"             # PDF输出目录
history_path: "history/example.json"     # 历史记录文件路径
concurrency: 3                           # 并发数
max_depth: 2                             # 最大爬取深度
timeout: 10000                           # 超时时间(毫秒)
delay: 1.0                               # 域名访问延迟(秒)
prefixes: ["https://example.com/article"] # 允许的URL前缀
refresh_mode: "pagination"               # 刷新模式: none/pull/pagination
obey_robot: true                         # 是否遵循robots.txt
no_new_limit: 5                          # 无新内容时的重试次数
deal_cookie: true                        # 是否自动处理Cookie弹窗
verbose: false                           # 详细日志模式
```

### 运行程序
```bash
python crawler.py
```

## 项目结构

```
WebPageCrawler/
├── config/                  # 配置文件目录
│   ├── config3.yaml         # 示例配置文件
│   └── ...
├── output/                  # PDF输出目录
│   └── ...
├── history/                 # 历史记录文件目录
│   └── ...
├── crawler.py               # 主程序文件
└── README.md                # 说明文档
```

核心模块说明：
- **Crawler 类**：爬虫核心类，包含初始化、页面处理、链接提取等核心逻辑
- **配置处理**：通过 YAML 配置文件管理爬虫参数
- **PDF 生成**：使用 Playwright 的 PDF 生成功能保存网页内容
- **历史记录**：通过 JSON 文件记录已爬取的 URL，避免重复爬取
- **工具函数**：包含 URL 规范化、文件名清洗、Cookie 处理等辅助功能

## 功能特点

### 1. 智能爬取能力
- **多级深度爬取**：支持配置最大爬取深度，自动递归抓取网页链接
- **链接过滤**：可通过前缀限制只爬取特定类型的链接
- **并发控制**：可配置并发数，平衡爬取效率与服务器负载

### 2. 网站适配功能
- **Cookie 弹窗处理**：自动识别并关闭常见的 Cookie 同意弹窗，支持多语言文本匹配
- **内容刷新机制**：支持两种刷新模式：
  - `pagination`：自动点击"下一页"等分页导航元素
  - `pull`：自动点击"加载更多"等按钮加载内容
- **robots.txt 支持**：可配置是否遵循网站的 robots 协议

### 3. 可靠性保障
- **历史记录管理**：通过 SHA1 哈希记录已爬取 URL，避免重复工作
- **超时控制**：为页面加载和 PDF 生成设置超时时间
- **域名访问延迟**：可配置同一域名的访问间隔，避免触发反爬机制
- **失败处理**：单个页面处理失败不影响整体爬取流程

### 4. 输出管理
- **PDF 生成**：使用无头 Chromium 渲染网页并保存为 A4 格式 PDF
- **文件名规范化**：自动清洗网页标题作为 PDF 文件名，确保跨平台兼容性
- **原子写入**：使用临时文件+替换机制确保历史记录文件的完整性

该工具适用于需要批量保存网页内容为 PDF 的场景，如资料收集、内容归档等，具有良好的可配置性和扩展性。
