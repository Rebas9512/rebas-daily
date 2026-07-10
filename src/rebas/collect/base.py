"""采集层公共设施：URL 规范化、内容哈希、关键词预筛、HTTP 抓取。"""

from __future__ import annotations

import gzip
import hashlib
import html as html_mod
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from rebas.config import Profile

# 纯浏览器 UA：带自定义后缀会触发部分站点（Substack 等）的 WAF 403
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 去掉这些跟踪参数后再做唯一约束
_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|cmpid|smid)$", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ARXIV_ABS_RE = re.compile(r"^(/abs/[\w.\-/]+?)v\d+$")


def canonicalize_url(url: str) -> str:
    """URL 规范化：小写 scheme/host、去跟踪参数（按 key 排序）、去 fragment、去末尾斜杠；
    arXiv abs 链接剥版本号（HN 帖常链 v2，与 arXiv 源的无版本链接应视为同条）。"""
    parts = urlsplit(url.strip())
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k)
    ))
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.endswith("arxiv.org"):
        m = _ARXIV_ABS_RE.match(path)
        if m:
            path = m.group(1)
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def content_hash(title: str) -> str:
    """标题规范化哈希，捕捉同文异链的转载。"""
    norm = re.sub(r"\W+", "", title).lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def strip_html(text: str, limit: int | None = None) -> str:
    text = html_mod.unescape(_TAG_RE.sub(" ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def first_image(html: str) -> str | None:
    m = _IMG_RE.search(html or "")
    return m.group(1) if m else None


# 图库提取的噪声过滤：跟踪像素/头像/表情/矢量图标（内容摄影不会是 svg）
_IMG_JUNK_RE = re.compile(
    r"^data:|\.svg(?:\?|$)|gravatar\.com|doubleclick\.|feedburner"
    r"|/emoji|/smilies?/|/avatars?/|pixel\.(?:gif|png)|spacer\.|blank\.gif",
    re.I)
# WordPress 缩略图尺寸后缀（photo-768x512.jpg）：归一后去重，同图多尺寸只留首个
_WP_SIZE_RE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}$)")


def all_images(html: str, base_url: str = "", cap: int = 6) -> list[str]:
    """正文 HTML 里的配图列表（艺术/设计多图排版用，2026-07-07）：按出现顺序取
    <img src>，剥噪声、相对路径按 base_url 补全（无基准丢弃）、同图多尺寸归一去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for url in _IMG_RE.findall(html or ""):
        url = html_mod.unescape(url.strip())
        if not url or _IMG_JUNK_RE.search(url):
            continue
        if not url.startswith(("http://", "https://")):
            if not base_url:
                continue
            url = urljoin(base_url, url)
            if not url.startswith(("http://", "https://")):
                continue
        key = _WP_SIZE_RE.sub("", url.split("?", 1)[0])
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= cap:
            break
    return out


class KeywordMatcher:
    """画像关键词预筛。词边界匹配 + 允许后缀（agent→agents、RL→RLHF），
    避免短关键词的子串误命中（RL 不会命中 world/URL）。"""

    def __init__(self, profile: Profile):
        self._include = self._compile(profile.all_keywords())
        self._block = self._compile(profile.blocklist)

    @staticmethod
    def _compile(keywords: tuple[str, ...]) -> re.Pattern | None:
        if not keywords:
            return None
        parts = [re.escape(kw).replace(r"\ ", r"\s+") for kw in keywords]
        return re.compile(r"\b(?:" + "|".join(parts) + r")\w*", re.I)

    def matches(self, text: str) -> bool:
        if self._block and self._block.search(text):
            return False
        return bool(self._include and self._include.search(text))


@dataclass
class FetchResult:
    status: int                     # HTTP 状态码；304 = 未变化
    data: bytes = b""
    etag: str | None = None
    last_modified: str | None = None
    final_url: str = ""             # 跟随重定向后的最终 URL（≠请求 URL 时提示更新 endpoint）


@dataclass
class HttpResponse:
    status_code: int
    content: bytes
    headers: dict = field(default_factory=dict)
    url: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "ignore")


class HttpClient:
    """urllib 轻封装。

    为什么不用 httpx：Cloudflare 会按 TLS 指纹拦 httpx（Substack 实测 403，
    换完整浏览器头也无效），而 urllib 的指纹在本项目全部 40+ 信息源上实测畅通
    （2026-07-03）。urllib 自动跟随重定向。
    """

    _DEFAULT = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    def __init__(self, timeout: int = 25):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def _decompress(data: bytes, encoding: str) -> bytes:
        if encoding == "gzip" or data[:2] == b"\x1f\x8b":
            return gzip.decompress(data)
        if encoding == "deflate":
            return zlib.decompress(data, -zlib.MAX_WBITS)
        return data

    def _request(self, url: str, *, data: bytes | None = None,
                 headers: dict | None = None) -> HttpResponse:
        req = urllib.request.Request(url, data=data, headers={**self._DEFAULT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = self._decompress(resp.read(), resp.headers.get("Content-Encoding", ""))
            return HttpResponse(
                status_code=resp.status,
                content=raw,
                headers=dict(resp.headers),
                url=resp.geturl(),
            )

    def get(self, url: str, headers: dict | None = None) -> HttpResponse:
        return self._request(url, headers=headers)

    def post(self, url: str, content: str | bytes, headers: dict | None = None) -> HttpResponse:
        body = content.encode() if isinstance(content, str) else content
        return self._request(url, data=body, headers=headers)


def make_client() -> HttpClient:
    return HttpClient()


def fetch_url(client: HttpClient, url: str, *, etag: str | None = None,
              last_modified: str | None = None, retries: int = 2) -> FetchResult:
    """带 conditional GET 与重试的抓取。"""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, headers=headers)
            return FetchResult(
                status=resp.status_code,
                data=resp.content.lstrip(),   # 部分源 feed 开头有空白
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                final_url=resp.url or "",
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(status=304, etag=etag, last_modified=last_modified)
            raise                             # 其余 4xx/5xx 不重试，交上层记录
        except OSError as exc:                # 网络层错误重试（URLError 是 OSError 子类）
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def parse_date(value) -> str | None:
    """把 feedparser struct_time 或 ISO 字符串统一为 UTC ISO8601。

    出刊窗口用字符串比较（SQL），带本地偏移（-05:00 等）的时间戳必须先归一到 UTC，
    否则窗口边界误差可达数小时。解析不了的字符串原样保留（好过丢失）。"""
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.replace(tzinfo=None).isoformat(timespec="seconds") + "+00:00"
        except ValueError:
            return value
    return None
