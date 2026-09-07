# MangaDex API 代理服务

一个基于 Flask 的 MangaDex RESTful API 代理服务。

> Python/Flask 重写版（原为 Express.js），修复了旧版进程级崩溃问题，并支持中文本地化。

## ✨ 功能特性

- **漫画搜索**：关键词搜索 + 分页，封面经 `includes[]=cover_art` 内联获取（无 N+1 查询）
- **漫画详情**：标题 / 评分 / 标签 / 章节统计
- **中文本地化**：根据设备 UA 语言自动回退
  - 标题：中文设备按 `zh → zh-hk → zh-tw → en → ja-ro` 选择
  - 章节：按 `zh → zh-hk → en` 去重优选（无中文汉化时回退英文，内容不缺席）
  - 标签：内置 MangaDex 官方 77 标签中文映射
- **章节图片**：惰性编号 URL + 请求时实时解析 MD@H 节点（token 过期自动刷新重试）
- **图片处理**：宽度缩放 / JPEG 质量 / PNG 量化 / LVGL 预解码，适配低性能设备
- **健壮性**：所有错误按请求隔离返回 JSON（`{"code", "message"}`），无进程级退出
- **多级缓存**：详情 1h / 章节 10min / MD@H 5min / 搜索 60s / 成品图片 24h

## 🚀 快速开始

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python index.py          # 默认 0.0.0.0:3000，PORT 环境变量可改
```

生产环境推荐 gunicorn：

```bash
gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:3001 --timeout 120 index:app
```

## 📖 API 文档

### 源配置

`GET /config`

```json
{
  "MangaDex": {
    "name": "MangaDex",
    "apiUrl": "http://your-domain.com",
    "detailPath": "/comic/<id>",
    "photoPath": "/photo/<id>/ch/<chapter>",
    "searchPath": "/search/<text>/<page>",
    "type": "mangadex"
  }
}
```

### 搜索漫画

`GET /search/<text>/<page>`

```json
{
  "page": 1,
  "has_more": true,
  "results": [
    {
      "comic_id": "manga-uuid",
      "title": "漫画标题",
      "cover_url": "http://your-domain.com/comic/manga-uuid/cover",
      "pages": 0
    }
  ]
}
```

### 漫画详情

`GET /comic/<id>`（id 为 MangaDex UUID）

```json
{
  "item_id": "manga-uuid",
  "name": "漫画标题",
  "page_count": 14736,
  "rate": 9.33,
  "cover": "http://your-domain.com/comic/manga-uuid/cover",
  "tags": ["动作", "冒险"],
  "total_chapters": 763
}
```

### 章节图片列表

`GET /photo/<id>/ch/<chapter>`（chapter 为去重后的章节序号，从 1 开始）

```json
{
  "title": "章节标题",
  "images": [
    { "url": "http://your-domain.com/photo/manga-uuid/ch/1/1.jpg" },
    { "url": "http://your-domain.com/photo/manga-uuid/ch/1/2.jpg" }
  ]
}
```

### 图片接口（封面 / 正文页）

- 封面：`GET /comic/<id>/cover`
- 正文页：`GET /photo/<id>/ch/<chapter>/<page>.jpg`

查询参数：

| 参数 | 说明 |
| --- | --- |
| `width` / `w` | 目标宽度，等比缩放 |
| `quality` / `q` | 质量 1-100（JPEG 质量 / PNG 量化） |
| `ifPNG` | `1/true/yes/on` 时返回 PNG |
| `ifLVGL` | `1/true/yes/on` 时返回 LVGL 预解码二进制（优先于 ifPNG） |

## ☁️ 部署

### VPS（PM2 + gunicorn）

```bash
pip install -r requirements.txt   # 建议 venv
npm install -g pm2
pm2 start ecosystem.config.js     # 调用 gunicorn，端口 3001
pm2 save
```

> `ecosystem.config.js` 默认调用 PATH 中的 `gunicorn`；如使用 venv，请先激活再 `pm2 start`，或将 `script` 改为绝对路径（如 `./venv/bin/gunicorn`）。

### Vercel

仓库已内置 `vercel.json`（`@vercel/python` → `index.py`），导入仓库即可部署。

### EdgeOne Pages

仓库已内置 `edgeone.json`（`maxDuration: 120`）与 `cloud-functions/` 双入口（薄 shim，自动加载根目录 `index.py`）。

> EdgeOne 会将 `Host` 改写为内部域名，原始域名经 `Eo-Pages-Host` 请求头透传，服务已自动识别。若有异常，可配置环境变量 `PUBLIC_URL=https://your-domain` 强制指定。

### 环境变量

| 变量 | 说明 |
| --- | --- |
| `PORT` | 本地开发端口（默认 3000；gunicorn 由 `-b` 参数决定） |
| `PUBLIC_URL` | 强制指定对外 API 基础地址（如 `https://api.example.com`） |

## ⚠️ 注意事项

- 本服务仅为 MangaDex API 的代理，不存储任何漫画内容
- 请遵守 MangaDex 使用条款；客户端已按 5 req/s 限流设计（搜索探测并发 ≤4、自动重试 429）
- MangaDex 上部分漫画（如《海贼王》《葬送的芙莉莲》）官方章节为外部链接（MangaPlus 等），本源只返回可直接阅读的章节，因此这些漫画可能显示极少章节或不出现
- 本地开发请使用 OpenSSL 1.1.1+ 的现代 Python（macOS 系统自带 3.9/LibreSSL 无法与 MangaDex 完成 TLS 握手）

## 📄 许可证

MIT - 查看 [LICENSE](LICENSE) 了解详情。
