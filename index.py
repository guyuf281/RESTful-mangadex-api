# MangaDex API - Flask 版
# 腕上漫画（bandcomic）自定义漫画源
# 适配部署：VPS（gunicorn/PM2）、Vercel、EdgeOne Pages

import io
import logging
import os
import re
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from cachetools import TTLCache, cached
from flask import Flask, Response, jsonify, request
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
app.debug = False
app.json.ensure_ascii = False

# Pillow 像素上限（对齐旧 JS 版 sharp 的 limitInputPixels）
Image.MAX_IMAGE_PIXELS = 268402689


class WSGIPathFixMiddleware:
    """修复部分 Serverless 运行时（如 EdgeOne）将 URL 解码为 Unicode 后
    直接放入 PATH_INFO/QUERY_STRING 的问题。

    PEP 3333 要求 PATH_INFO 为 latin-1 范围内的 str（表示原始字节），
    Werkzeug 会 encode('latin1') 还原字节。若运行时放入的是真正的
    Unicode 字符串（如 /search/明日方舟/1），需要重新按 UTF-8 编码
    再按 latin-1 解码，恢复 WSGI 标准格式。
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        for key in ("PATH_INFO", "QUERY_STRING", "SCRIPT_NAME"):
            value = environ.get(key)
            if not value:
                continue
            try:
                value.encode("latin1")
            except UnicodeEncodeError:
                environ[key] = value.encode("utf-8").decode("latin1")
        return self.wsgi_app(environ, start_response)


app.wsgi_app = WSGIPathFixMiddleware(app.wsgi_app)

# ==============================================================================
# 常量
# ==============================================================================
MANGADEX_API = "https://api.mangadex.org"
MANGADEX_UPLOADS = "https://uploads.mangadex.org"
API_UA = "bandcomic-mangadex-source/1.0 (github.com/sf-yuzifu/RESTful-mangadex-api)"
REQUEST_TIMEOUT = 20
IMAGE_TIMEOUT = 30
MAX_IMAGE_BYTES = 50 * 1024 * 1024  # 50MB
CONTENT_RATINGS = ["safe", "suggestive", "erotica", "pornographic"]
EN_LANGS = ["en"]
ZH_LANGS = ["zh", "zh-hk", "en"]  # 中文设备章节语言回退链
MAX_CHAPTERS = 2000
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# MangaDex 官方 77 个标签的固定中文映射（官方标签无中文本地化，故内置维护）
TAG_ZH = {
    # genre
    "action": "动作",
    "adventure": "冒险",
    "boys' love": "耽美",
    "comedy": "喜剧",
    "crime": "犯罪",
    "drama": "剧情",
    "fantasy": "奇幻",
    "girls' love": "百合",
    "historical": "历史",
    "horror": "恐怖",
    "isekai": "异世界",
    "magical girls": "魔法少女",
    "mecha": "机甲",
    "medical": "医疗",
    "mystery": "悬疑",
    "philosophical": "哲学",
    "psychological": "心理",
    "romance": "恋爱",
    "sci-fi": "科幻",
    "slice of life": "日常",
    "sports": "运动",
    "superhero": "超级英雄",
    "thriller": "惊悚",
    "tragedy": "悲剧",
    "wuxia": "武侠",
    # theme
    "aliens": "外星人",
    "animals": "动物",
    "cooking": "料理",
    "crossdressing": "变装",
    "delinquents": "不良少年",
    "demons": "恶魔",
    "genderswap": "性转换",
    "ghosts": "幽灵",
    "gyaru": "辣妹",
    "harem": "后宫",
    "incest": "乱伦",
    "loli": "萝莉",
    "mafia": "黑手党",
    "magic": "魔法",
    "mahjong": "麻将",
    "martial arts": "武术",
    "military": "军事",
    "monster girls": "魔物娘",
    "monsters": "怪物",
    "music": "音乐",
    "ninja": "忍者",
    "office workers": "上班族",
    "police": "警察",
    "post-apocalyptic": "后启示录",
    "reincarnation": "转生",
    "reverse harem": "逆后宫",
    "samurai": "武士",
    "school life": "校园",
    "shota": "正太",
    "supernatural": "超自然",
    "survival": "生存",
    "time travel": "时间旅行",
    "traditional games": "传统游戏",
    "vampires": "吸血鬼",
    "video games": "电子游戏",
    "villainess": "恶役千金",
    "virtual reality": "虚拟现实",
    "zombies": "丧尸",
    # format
    "4-koma": "四格漫画",
    "adaptation": "改编",
    "anthology": "选集",
    "award winning": "获奖作品",
    "doujinshi": "同人志",
    "fan colored": "粉丝上色",
    "full color": "全彩",
    "long strip": "条漫",
    "official colored": "官方全彩",
    "oneshot": "单篇",
    "self-published": "自出版",
    "web comic": "网络漫画",
    # content
    "gore": "血腥",
    "sexual violence": "性暴力",
}

# ==============================================================================
# 缓存（cachetools，lock 保证 gunicorn 多线程下安全）
# ==============================================================================
manga_cache = TTLCache(maxsize=500, ttl=3600)  # 漫画详情
manga_lock = threading.Lock()
stats_cache = TTLCache(maxsize=500, ttl=3600)  # 评分统计
stats_lock = threading.Lock()
feed_cache = TTLCache(maxsize=200, ttl=600)  # 去重后的章节列表
feed_lock = threading.Lock()
athome_cache = TTLCache(maxsize=200, ttl=300)  # MD@H 节点与文件列表
athome_lock = threading.Lock()
search_cache = TTLCache(maxsize=100, ttl=60)  # 搜索结果
search_lock = threading.Lock()
image_cache = TTLCache(maxsize=300, ttl=86400)  # 处理后的成品图片
image_lock = threading.Lock()

image_semaphore = threading.BoundedSemaphore(5)  # 图片处理并发上限


class UpstreamError(Exception):
    """上游（MangaDex / MD@H）请求失败，携带 HTTP 状态码"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ==============================================================================
# 平台适配与语言感知
# ==============================================================================
def get_api_url() -> str:
    """获取对外可访问的 API 基础地址。

    EdgeOne Pages 会将 Host 改写为内部域名，原始域名经 Eo-Pages-Host 透传。
    优先级：PUBLIC_URL 环境变量 > Eo-Pages-Host > X-Forwarded-* > Forwarded > Host。
    """
    public = os.environ.get("PUBLIC_URL")
    if public:
        return public.rstrip("/")

    eo_host = request.headers.get("Eo-Pages-Host", "").strip()
    if eo_host:
        return f"https://{eo_host}"

    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()

    if not host or not proto:
        forwarded = request.headers.get("Forwarded", "")
        if not host:
            m = re.search(r'host="?([^;,"]+)"?', forwarded, re.I)
            if m:
                host = m.group(1)
        if not proto:
            m = re.search(r'proto="?([^;,"]+)"?', forwarded, re.I)
            if m:
                proto = m.group(1)

    if not host:
        host = request.host
    if not proto:
        proto = request.scheme
    return f"{proto}://{host}"


def decode_search_value(value: str) -> str:
    """判断并解码搜索值，兼容多重 URL 编码"""
    pattern = r"%[0-9A-Fa-f]{2}"
    if not re.search(pattern, value):
        return value
    try:
        from urllib.parse import unquote

        decoded = unquote(value)
        while re.search(pattern, decoded):
            temp = unquote(decoded)
            if temp == decoded:
                break
            decoded = temp
        return decoded
    except Exception:
        return value


def parse_user_agent(user_agent: str) -> dict:
    """解析腕上漫画 UA：
    packageName(version)/product/brand/osType/osVersion/osVersionCode/language/region
    """
    device = {"language": "", "region": ""}
    try:
        parts = (user_agent or "").split("/")
        if len(parts) > 6:
            device["language"] = parts[6]
        if len(parts) > 7:
            device["region"] = parts[7]
    except Exception:
        pass
    return device


def is_chinese_locale() -> bool:
    device = parse_user_agent(request.headers.get("User-Agent", ""))
    language = (device.get("language") or "").lower()
    region = (device.get("region") or "").upper()
    accept_language = request.headers.get("Accept-Language", "").lower()
    return (
        language.startswith("zh")
        or region in ("CN", "TW", "HK", "MO")
        or "zh" in accept_language
    )


def lang_key_for_request() -> str:
    return "zh" if is_chinese_locale() else "en"


def languages_for_key(lang_key: str) -> list:
    return ZH_LANGS if lang_key == "zh" else EN_LANGS


def pick_title(attributes: dict, lang_key: str) -> str:
    """按设备语言选择标题：中文设备 zh → zh-hk → zh-tw → en → ja-ro"""
    title = attributes.get("title") or {}
    alt_titles = attributes.get("altTitles") or []
    order = (
        ["zh", "zh-hk", "zh-tw", "en", "ja-ro"]
        if lang_key == "zh"
        else ["en", "ja-ro"]
    )
    for lang in order:
        if title.get(lang):
            return title[lang]
        for alt in alt_titles:
            if alt.get(lang):
                return alt[lang]
    if title:
        return next(iter(title.values()))
    for alt in alt_titles:
        if alt:
            return next(iter(alt.values()))
    return "untitled"


def translate_tags(attributes: dict, lang_key: str) -> list:
    tags = attributes.get("tags") or []
    names = []
    for tag in tags:
        name = ((tag.get("attributes") or {}).get("name") or {}).get("en")
        if not name:
            continue
        if lang_key == "zh":
            names.append(TAG_ZH.get(name.lower(), name))
        else:
            names.append(name)
    return names


# ==============================================================================
# MangaDex API 客户端
# ==============================================================================
def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": API_UA})
    return session


_session = _build_session()


def md_get(path: str, params=None) -> dict:
    """请求 MangaDex API，非 200 抛 UpstreamError"""
    url = f"{MANGADEX_API}{path}"
    try:
        resp = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise UpstreamError(502, f"上游网络错误: {e}")
    if resp.status_code != 200:
        raise UpstreamError(resp.status_code, f"上游返回 {resp.status_code}: {path}")
    return resp.json()


def _content_rating_params() -> list:
    return [("contentRating[]", r) for r in CONTENT_RATINGS]


def search_manga(text: str, limit: int, offset: int) -> dict:
    params = [
        ("title", text),
        ("limit", str(limit)),
        ("offset", str(offset)),
        ("hasAvailableChapters", "true"),
        ("order[relevance]", "desc"),
        ("includes[]", "cover_art"),
    ] + _content_rating_params()
    return md_get("/manga", params)


@cached(cache=manga_cache, lock=manga_lock)
def get_manga(manga_id: str) -> dict:
    return md_get(f"/manga/{manga_id}", [("includes[]", "cover_art")]).get("data") or {}


@cached(cache=stats_cache, lock=stats_lock)
def get_statistics(manga_id: str) -> dict:
    data = md_get(f"/statistics/manga/{manga_id}")
    return (data.get("statistics") or {}).get(manga_id) or {}


def probe_feed_has_chapter(manga_id: str, languages: list) -> bool:
    """探测漫画是否有指定语言的可直接阅读章节（排除纯外部链接章节）"""
    params = (
        [("translatedLanguage[]", lang) for lang in languages]
        + [("limit", "1"), ("includeExternalUrl", "0")]
        + _content_rating_params()
    )
    try:
        data = md_get(f"/manga/{manga_id}/feed", params)
        return len(data.get("data") or []) > 0
    except UpstreamError:
        return False


def _chapter_sort_key(chapter: dict):
    num = (chapter.get("attributes") or {}).get("chapter")
    try:
        n = float(num)
    except (TypeError, ValueError):
        n = float("inf")
    return (n, (chapter.get("attributes") or {}).get("publishAt") or "")


@cached(cache=feed_cache, lock=feed_lock)
def get_feed(manga_id: str, lang_key: str) -> list:
    """分页拉取全量章节，按章节号跨语言去重（优先保留靠前语言），升序返回"""
    languages = languages_for_key(lang_key)
    base_params = (
        [("translatedLanguage[]", lang) for lang in languages]
        + [
            ("order[chapter]", "asc"),
            ("limit", "100"),
            ("includeExternalUrl", "0"),
            ("includeFuturePublishAt", "0"),
            ("includeEmptyPages", "0"),
        ]
        + _content_rating_params()
    )

    chapters = []
    offset = 0
    while True:
        try:
            data = md_get(f"/manga/{manga_id}/feed", base_params + [("offset", str(offset))])
        except UpstreamError:
            if not chapters:
                raise
            break  # 中途失败用已拉到的部分，对齐旧 JS 行为
        batch = data.get("data") or []
        chapters.extend(batch)
        if len(batch) < 100 or len(chapters) >= MAX_CHAPTERS:
            break
        offset += 100

    chapters = chapters[:MAX_CHAPTERS]

    # 跨语言去重：同一章节号（无编号章节按标题）只保留语言优先级最高的一个
    lang_pref = {lang: i for i, lang in enumerate(languages)}
    groups = {}
    for chapter in chapters:
        attrs = chapter.get("attributes") or {}
        num = attrs.get("chapter")
        if num is not None:
            try:
                key = ("num", float(num))
            except ValueError:
                key = ("num", num)
        else:
            key = ("title", (attrs.get("title") or chapter.get("id") or "").strip().lower())
        pref = lang_pref.get(attrs.get("translatedLanguage"), 99)
        current = groups.get(key)
        if current is None or pref < current[0]:
            groups[key] = (pref, chapter)

    deduped = [chapter for _, chapter in groups.values()]
    deduped.sort(key=_chapter_sort_key)
    return deduped


@cached(cache=athome_cache, lock=athome_lock)
def get_chapter_files(chapter_id: str) -> dict:
    """从 MD@H 获取章节图片节点与文件列表（链接带时效 token，短 TTL 缓存）"""
    data = md_get(f"/at-home/server/{chapter_id}")
    chapter = data.get("chapter") or {}
    return {
        "base_url": (data.get("baseUrl") or "").rstrip("/"),
        "hash": chapter.get("hash") or "",
        "files": chapter.get("data") or [],
    }


def invalidate_chapter_files(chapter_id: str):
    with athome_lock:
        athome_cache.pop((chapter_id,), None)


def cover_filename(manga: dict):
    for rel in manga.get("relationships") or []:
        if rel.get("type") == "cover_art":
            name = (rel.get("attributes") or {}).get("fileName")
            if name:
                return name
    return None


def page_image_url(files_info: dict, page_num: int) -> str:
    return (
        f"{files_info['base_url']}/data/{files_info['hash']}"
        f"/{files_info['files'][page_num - 1]}"
    )


# ==============================================================================
# 图片处理（Pillow，对齐 CUSTOM_SOURCE.md 第 8/9 节）
# ==============================================================================
def is_truthy(value) -> bool:
    return value in ("1", "true", "True", "yes", "on")


def normalize_rgb_image(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode in ("RGBA", "LA"):
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def optimize_png_image(image: Image.Image, quality: int) -> Image.Image:
    quality = max(1, min(100, quality))
    image = normalize_rgb_image(image)
    if quality >= 95:
        return image
    colors = max(16, min(256, int(16 + quality * 2.4)))
    return image.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)


def convert_to_lvgl8(image: Image.Image) -> bytes:
    """将 PIL Image 转换为 LVGL 预解码二进制（CF_INDEXED_8_BIT）"""
    image = normalize_rgb_image(image)
    image = image.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    w, h = image.size

    raw_palette = image.getpalette() or []
    palette = []
    for i in range(256):
        idx = i * 3
        if idx + 2 < len(raw_palette):
            palette.append((raw_palette[idx], raw_palette[idx + 1], raw_palette[idx + 2]))
        else:
            palette.append((0, 0, 0))

    header_word = 10 | (w << 10) | (h << 21)
    output = io.BytesIO()
    output.write(struct.pack("<I", header_word))
    for r, g, b in palette:
        output.write(bytes([b, g, r, 0xFF]))
    output.write(image.tobytes())
    return output.getvalue()


def process_image(data: bytes, max_width: int, quality: int, fmt: str):
    img = Image.open(io.BytesIO(data))
    if img.width > max_width:
        new_height = int((max_width / img.width) * img.height)
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if fmt == "lvgl":
        return convert_to_lvgl8(img), "application/octet-stream"

    output = io.BytesIO()
    if fmt == "png":
        img = optimize_png_image(img, quality)
        img.save(output, "PNG", optimize=True, compress_level=9)
        return output.getvalue(), "image/png"

    img = normalize_rgb_image(img)
    img.save(output, "JPEG", quality=quality, optimize=True, progressive=True)
    return output.getvalue(), "image/jpeg"


def _fetch_image_bytes(url: str) -> bytes:
    try:
        resp = _session.get(url, timeout=IMAGE_TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise UpstreamError(502, f"图片下载失败: {e}")
    if resp.status_code != 200:
        raise UpstreamError(resp.status_code, f"图片下载失败: {resp.status_code}")
    content_length = int(resp.headers.get("Content-Length") or 0)
    if content_length > MAX_IMAGE_BYTES:
        raise UpstreamError(413, "图片过大，最大支持 50MB")
    data = resp.content
    if len(data) > MAX_IMAGE_BYTES:
        raise UpstreamError(413, "图片过大，最大支持 50MB")
    return data


@cached(cache=image_cache, lock=image_lock)
def get_processed_image(url: str, max_width: int, quality: int, fmt: str):
    """下载并处理图片（成品按 URL+参数 缓存 24h）"""
    data = _fetch_image_bytes(url)
    return process_image(data, max_width, quality, fmt)


def serve_image(url: str, default_width: int):
    """公共图片响应逻辑：解析参数、限流、缓存、返回图片响应"""
    width = request.args.get("width") or request.args.get("w")
    max_width = int(width) if width and str(width).isdigit() else default_width
    quality = request.args.get("quality") or request.args.get("q")
    quality = int(quality) if quality and str(quality).isdigit() else 50
    quality = max(1, min(100, quality))

    if is_truthy(request.args.get("ifLVGL", "0")):
        fmt = "lvgl"
    elif is_truthy(request.args.get("ifPNG", "0")):
        fmt = "png"
    else:
        fmt = "jpeg"

    if not image_semaphore.acquire(timeout=20):
        return jsonify({"code": 429, "message": "请求过多，请稍后再试"}), 429
    try:
        body, mimetype = get_processed_image(url, max_width, quality, fmt)
    finally:
        image_semaphore.release()

    return Response(
        body,
        mimetype=mimetype,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Type": mimetype,
        },
    )


# ==============================================================================
# 路由
# ==============================================================================
@app.get("/")
def home():
    return "MangaDex API 服务运行中！"


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/config")
@app.get("/config/")
def config():
    api_url = get_api_url()
    return jsonify(
        {
            "MangaDex": {
                "name": "MangaDex",
                "apiUrl": api_url,
                "detailPath": "/comic/<id>",
                "photoPath": "/photo/<id>/ch/<chapter>",
                "searchPath": "/search/<text>/<page>",
                "type": "mangadex",
            }
        }
    )


@app.get("/search/<text>")
@app.get("/search/<text>/")
@app.get("/search/<text>/<int:page>")
def search(text, page: int = 1):
    try:
        keyword = decode_search_value(text)
        if not keyword.strip():
            return jsonify({"code": 400, "message": "Missing parameter"}), 400
        page = max(1, page)
        limit = 10
        offset = (page - 1) * limit
        lang_key = lang_key_for_request()

        cache_key = (keyword, page, lang_key)
        with search_lock:
            cached_data = search_cache.get(cache_key)
        if cached_data is not None:
            return jsonify(cached_data)

        data = search_manga(keyword, limit, offset)
        manga_list = data.get("data") or []
        total = data.get("total") or 0
        has_more = offset + limit < total

        # 并发探测每本漫画是否有当前语言可直接阅读的章节
        languages = languages_for_key(lang_key)
        with ThreadPoolExecutor(max_workers=4) as executor:
            readable = list(
                executor.map(lambda m: probe_feed_has_chapter(m.get("id", ""), languages), manga_list)
            )

        api_url = get_api_url()
        results = []
        for manga, ok in zip(manga_list, readable):
            if not ok:
                continue
            manga_id = manga.get("id")
            attrs = manga.get("attributes") or {}
            results.append(
                {
                    "comic_id": manga_id,
                    "title": pick_title(attrs, lang_key),
                    "cover_url": f"{api_url}/comic/{manga_id}/cover",
                    "pages": 0,
                }
            )

        response = {"page": page, "has_more": has_more, "results": results}
        with search_lock:
            search_cache[cache_key] = response
        return jsonify(response)

    except UpstreamError as e:
        logging.warning(f"搜索上游失败: {e.message}")
        return jsonify({"code": 502, "message": "Upstream failed"}), 502
    except Exception as e:
        logging.exception("路由 /search 出错")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/comic/<manga_id>")
def comic_detail(manga_id: str):
    try:
        if not UUID_RE.match(manga_id or ""):
            return jsonify({"code": 400, "message": "无效的漫画 ID 格式（应为 UUID）"}), 400

        lang_key = lang_key_for_request()
        try:
            manga = get_manga(manga_id)
        except UpstreamError as e:
            if e.status == 404:
                return jsonify({"code": 404, "message": "Comic not found"}), 404
            raise
        attrs = manga.get("attributes") or {}

        try:
            stats = get_statistics(manga_id)
            rate = round((stats.get("rating") or {}).get("bayesian") or 0, 2)
        except UpstreamError:
            rate = None

        feed = get_feed(manga_id, lang_key)
        page_count = sum((c.get("attributes") or {}).get("pages") or 0 for c in feed)

        api_url = get_api_url()
        response = {
            "item_id": manga_id,
            "name": pick_title(attrs, lang_key),
            "page_count": page_count,
            "cover": f"{api_url}/comic/{manga_id}/cover",
            "tags": translate_tags(attrs, lang_key),
            "total_chapters": len(feed) or 1,
        }
        if rate:
            response["rate"] = rate
        return jsonify(response)

    except UpstreamError as e:
        logging.warning(f"详情上游失败: {e.message}")
        return jsonify({"code": 502, "message": "Upstream failed"}), 502
    except Exception as e:
        logging.exception("路由 /comic 出错")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/comic/<manga_id>/cover")
def comic_cover(manga_id: str):
    """封面图片：详情缓存取 fileName 后回源 uploads.mangadex.org"""
    try:
        if not UUID_RE.match(manga_id or ""):
            return jsonify({"code": 400, "message": "无效的漫画 ID 格式（应为 UUID）"}), 400
        try:
            manga = get_manga(manga_id)
        except UpstreamError as e:
            if e.status == 404:
                return jsonify({"code": 404, "message": "Comic not found"}), 404
            raise
        filename = cover_filename(manga)
        if not filename:
            return jsonify({"code": 404, "message": "No cover found"}), 404
        url = f"{MANGADEX_UPLOADS}/covers/{manga_id}/{filename}.256.jpg"
        return serve_image(url, default_width=256)

    except UpstreamError as e:
        if e.status == 413:
            return jsonify({"code": 413, "message": e.message}), 413
        logging.warning(f"封面上游失败: {e.message}")
        return jsonify({"code": 502, "message": "Upstream failed"}), 502
    except Exception as e:
        logging.exception("路由 /comic/<id>/cover 出错")
        return jsonify({"code": 500, "message": str(e)}), 500


def _resolve_chapter(manga_id: str, chapter: int, lang_key: str):
    """章节序号 → 章节对象，越界返回 None"""
    feed = get_feed(manga_id, lang_key)
    if chapter < 1 or chapter > len(feed):
        return None
    return feed[chapter - 1]


@app.get("/photo/<manga_id>")
@app.get("/photo/<manga_id>/")
@app.get("/photo/<manga_id>/ch/<int:chapter>")
def photo_list(manga_id: str, chapter: int = 1):
    try:
        if not UUID_RE.match(manga_id or ""):
            return jsonify({"code": 400, "message": "无效的漫画 ID 格式（应为 UUID）"}), 400
        lang_key = lang_key_for_request()
        chapter_obj = _resolve_chapter(manga_id, chapter, lang_key)
        if chapter_obj is None:
            return jsonify({"code": 404, "message": "Chapter not found"}), 404

        files_info = get_chapter_files(chapter_obj["id"])
        if not files_info["files"]:
            return jsonify({"code": 404, "message": "Chapter not found"}), 404

        attrs = chapter_obj.get("attributes") or {}
        title = attrs.get("title") or f"Chapter {attrs.get('chapter') or chapter}"

        api_url = get_api_url()
        images = [
            {"url": f"{api_url}/photo/{manga_id}/ch/{chapter}/{page}.jpg"}
            for page in range(1, len(files_info["files"]) + 1)
        ]
        return jsonify({"title": title, "images": images})

    except UpstreamError as e:
        if e.status == 404:
            return jsonify({"code": 404, "message": "Comic not found"}), 404
        logging.warning(f"图片列表上游失败: {e.message}")
        return jsonify({"code": 502, "message": "Upstream failed"}), 502
    except Exception as e:
        logging.exception("路由 /photo 出错")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.get("/photo/<manga_id>/ch/<int:chapter>/<path:page>")
def photo_page(manga_id: str, chapter: int, page: str):
    """章节单页图片，格式 /photo/<id>/ch/<chapter>/<page>.jpg

    真实图片地址在请求时实时解析（MD@H 链接带时效 token），
    上游 4xx 时作废 at-home 缓存并重试一次。
    """
    try:
        if not UUID_RE.match(manga_id or ""):
            return jsonify({"code": 400, "message": "无效的漫画 ID 格式（应为 UUID）"}), 400
        m = re.match(r"^(\d+)", page or "")
        if not m:
            return jsonify({"code": 400, "message": "无效的页码"}), 400
        page_num = int(m.group(1))

        lang_key = lang_key_for_request()
        chapter_obj = _resolve_chapter(manga_id, chapter, lang_key)
        if chapter_obj is None:
            return jsonify({"code": 404, "message": "Chapter not found"}), 404
        chapter_id = chapter_obj["id"]

        files_info = get_chapter_files(chapter_id)
        if page_num < 1 or page_num > len(files_info["files"]):
            return jsonify({"code": 404, "message": "Page not found"}), 404

        url = page_image_url(files_info, page_num)
        try:
            return serve_image(url, default_width=600)
        except UpstreamError as e:
            if e.status not in (403, 404, 410):
                raise
            # MD@H token 过期：作废缓存，重取节点后重试一次
            logging.info(f"MD@H 返回 {e.status}，刷新 at-home 节点重试: {chapter_id}")
            invalidate_chapter_files(chapter_id)
            files_info = get_chapter_files(chapter_id)
            if page_num > len(files_info["files"]):
                return jsonify({"code": 404, "message": "Page not found"}), 404
            url = page_image_url(files_info, page_num)
            return serve_image(url, default_width=600)

    except UpstreamError as e:
        if e.status == 413:
            return jsonify({"code": 413, "message": e.message}), 413
        if e.status == 404:
            return jsonify({"code": 404, "message": "Comic not found"}), 404
        logging.warning(f"单页图片上游失败: {e.message}")
        return jsonify({"code": 502, "message": "Upstream failed"}), 502
    except Exception as e:
        logging.exception("路由 /photo/<id>/ch/<chapter>/<page> 出错")
        return jsonify({"code": 500, "message": str(e)}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({"code": 404, "message": "路由未找到"}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"code": 500, "message": "服务器内部错误"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
