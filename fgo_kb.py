#!/usr/bin/env python3
"""Local, incremental knowledge base for fgo.wiki (Mooncell).

The sync path uses the public MediaWiki API and stores wikitext plus a compact
Chinese-friendly full-text index. No third-party Python packages are needed.
"""

from __future__ import annotations

import argparse
import datetime as datetime_module
import html
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover - urllib fallback keeps the script portable
    requests = None
    HTTPAdapter = None


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "fgo_wiki.sqlite3"
API_URL = os.environ.get("FGO_WIKI_API_URL", "https://fgo.wiki/api.php")
SOURCE_ROOT = "https://fgo.wiki"
LICENSE_NAME = "CC BY-NC-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
USER_AGENT = (
    "FgoWikiLocalResearch/1.0 "
    "(personal offline knowledge base; low-rate MediaWiki API client)"
)

CJK_RANGES = (
    r"\u3040-\u30ff\u31f0-\u31ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
)
CJK_RUN_RE = re.compile(f"[{CJK_RANGES}]+")
SEGMENT_RE = re.compile(f"[{CJK_RANGES}]+|[a-z0-9]+", re.IGNORECASE)
HEADING_RE = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
REDIRECT_RE = re.compile(
    r"^\s*#(?:REDIRECT|重定向)\s*:?\s*\[\[\s*:?\s*([^\]|#]+)", re.IGNORECASE
)

IMPORTANT_TEMPLATES = {
    "基础数值",
    "宝具",
    "持有技能",
    "职阶技能",
    "追加技能",
    "强化信息",
    "灵基再临素材",
    "技能强化素材",
    "追加技能强化素材",
    "材料消耗",
    "灵衣开放",
    "概念礼装",
    "指令纹章",
    "礼装",
    "礼装技能",
    "从者信息",
    "敌人",
    "关卡",
    "自由关卡",
    "主线关卡",
    "幕间物语",
    "强化任务",
    "召唤信息",
}

TITLE_STOPWORDS = {
    "技能",
    "宝具",
    "获取",
    "材料",
    "满级",
    "基础",
    "数值",
    "效果",
    "状态",
    "攻略",
    "一览",
    "推荐",
    "排行",
    "配队",
    "怎么",
    "多少",
    "什么",
    "英灵",
    "从者",
    "礼装",
    "活动",
    "关卡",
    "敌人",
    "atk",
    "hp",
    "np",
    "id",
    "lv",
}

NUMERAL_DIGITS = {
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}


class MediaWikiError(RuntimeError):
    pass


class MediaWikiClient:
    def __init__(self, delay: float = 0.5, timeout: float = 180.0) -> None:
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self._last_request = 0.0
        self._session = None
        if requests is not None and HTTPAdapter is not None:
            self._session = requests.Session()
            adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)

    def request(self, params: dict[str, Any]) -> dict[str, Any]:
        request_params = {
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
            **params,
        }
        url = f"{API_URL}?{urllib.parse.urlencode(request_params, doseq=True)}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }

        for attempt in range(6):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)

            retry_after = 0.0
            if self._session is not None:
                try:
                    self._last_request = time.monotonic()
                    response = self._session.get(
                        API_URL,
                        params=request_params,
                        headers={
                            **headers,
                            "Accept-Encoding": "gzip, deflate",
                        },
                        timeout=(15, self.timeout),
                    )
                except requests.RequestException as exc:
                    if attempt >= 5:
                        raise MediaWikiError(f"API request failed: {exc}") from exc
                    time.sleep(min(2**attempt, 20))
                    continue
                retry_after = _retry_after_seconds(
                    response.headers.get("Retry-After")
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt >= 5:
                        raise MediaWikiError(
                            f"API HTTP {response.status_code}: "
                            f"{response.text[:500]}"
                        )
                    time.sleep(max(retry_after, min(2**attempt, 20)))
                    continue
                if not response.ok:
                    raise MediaWikiError(
                        f"API HTTP {response.status_code}: {response.text[:500]}"
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    if attempt >= 5:
                        raise MediaWikiError("API returned invalid JSON") from exc
                    time.sleep(min(2**attempt, 20))
                    continue
            else:
                try:
                    self._last_request = time.monotonic()
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=self.timeout) as response:
                        payload = response.read().decode("utf-8")
                    data = json.loads(payload)
                except urllib.error.HTTPError as exc:
                    retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
                    if exc.code not in {429, 500, 502, 503, 504} or attempt >= 5:
                        detail = exc.read().decode("utf-8", errors="replace")[:500]
                        raise MediaWikiError(
                            f"API HTTP {exc.code}: {detail or exc.reason}"
                        ) from exc
                    time.sleep(max(retry_after, min(2**attempt, 20)))
                    continue
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    json.JSONDecodeError,
                    OSError,
                    http.client.HTTPException,
                ) as exc:
                    if attempt >= 5:
                        raise MediaWikiError(f"API request failed: {exc}") from exc
                    time.sleep(min(2**attempt, 20))
                    continue

            error = data.get("error")
            if error:
                code = str(error.get("code", ""))
                if code == "maxlag" and attempt < 5:
                    time.sleep(max(retry_after, 2 + attempt))
                    continue
                raise MediaWikiError(
                    f"MediaWiki API error {code}: {error.get('info', error)}"
                )
            return data

        raise MediaWikiError("API request exhausted retries")

    def query(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request({"action": "query", **params})


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def utc_now() -> str:
    return datetime_module.datetime.now(datetime_module.UTC).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def connect_database(path: Path) -> sqlite3.Connection:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pages (
            page_id INTEGER PRIMARY KEY,
            namespace INTEGER NOT NULL,
            title TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            revision_id INTEGER NOT NULL,
            revision_timestamp TEXT,
            content_model TEXT,
            sha1 TEXT,
            size_bytes INTEGER NOT NULL,
            wikitext TEXT NOT NULL,
            plain_text TEXT NOT NULL,
            is_redirect INTEGER NOT NULL DEFAULT 0,
            redirect_target TEXT,
            categories_json TEXT NOT NULL DEFAULT '[]',
            thumbnail_url TEXT,
            page_image TEXT,
            touched TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pages_title_idx ON pages(title);
        CREATE INDEX IF NOT EXISTS pages_namespace_idx ON pages(namespace);
        CREATE INDEX IF NOT EXISTS pages_revision_timestamp_idx
            ON pages(revision_timestamp);

        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            section_no INTEGER NOT NULL,
            heading TEXT NOT NULL,
            level INTEGER NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS sections_page_idx ON sections(page_id);
        CREATE INDEX IF NOT EXISTS sections_heading_idx ON sections(heading);

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER NOT NULL,
            chunk_no INTEGER NOT NULL,
            heading TEXT NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS chunks_page_idx ON chunks(page_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            title_tokens,
            body_tokens,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS template_fields (
            page_id INTEGER NOT NULL,
            template_name TEXT NOT NULL,
            instance_no INTEGER NOT NULL,
            field_no INTEGER NOT NULL,
            field_key TEXT NOT NULL,
            field_value TEXT NOT NULL,
            PRIMARY KEY(page_id, template_name, instance_no, field_no),
            FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS template_fields_page_idx
            ON template_fields(page_id);
        CREATE INDEX IF NOT EXISTS template_fields_key_idx
            ON template_fields(field_key);
        """
    )
    return connection


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def fetch_siteinfo(client: MediaWikiClient) -> dict[str, Any]:
    data = client.query(
        {
            "meta": "siteinfo",
            "siprop": "general|statistics|rightsinfo",
        }
    )
    return data["query"]


def iter_all_pages(
    client: MediaWikiClient, namespace: int
) -> Iterator[dict[str, Any]]:
    continuation: dict[str, Any] = {}
    while True:
        data = client.query(
            {
                "list": "allpages",
                "apnamespace": namespace,
                "aplimit": "max",
                "apfilterredir": "all",
                **continuation,
            }
        )
        yield from data.get("query", {}).get("allpages", [])
        continuation = data.get("continue", {})
        if not continuation:
            break


def iter_recent_changes(
    client: MediaWikiClient, namespaces: Sequence[int], since: str | None
) -> Iterator[dict[str, Any]]:
    continuation: dict[str, Any] = {}
    while True:
        params: dict[str, Any] = {
            "list": "recentchanges",
            "rcnamespace": "|".join(str(namespace) for namespace in namespaces),
            "rctype": "edit|new",
            "rcprop": "title|timestamp|ids|sizes",
            "rclimit": "max",
            "rcdir": "newer",
            **continuation,
        }
        if since:
            params["rcstart"] = since
        data = client.query(params)
        yield from data.get("query", {}).get("recentchanges", [])
        continuation = data.get("continue", {})
        if not continuation:
            break


def fetch_pages(client: MediaWikiClient, titles: Sequence[str]) -> list[dict[str, Any]]:
    data = client.query(
        {
            "titles": "|".join(titles),
            "prop": "revisions|info|categories|pageimages",
            "rvprop": "ids|timestamp|content|contentmodel|size|sha1",
            "rvslots": "main",
            "clprop": "sortkey|timestamp",
            "cllimit": "max",
            "piprop": "thumbnail|name",
            "pithumbsize": "320",
        }
    )
    pages = data.get("query", {}).get("pages", [])
    return [page for page in pages if "missing" not in page and page.get("revisions")]


def page_url(title: str) -> str:
    return f"{SOURCE_ROOT}/w/{urllib.parse.quote(title.replace(' ', '_'), safe='')}"


def split_top_level(value: str, delimiter: str = "|") -> list[str]:
    parts: list[str] = []
    start = 0
    template_depth = 0
    link_depth = 0
    table_depth = 0
    index = 0
    while index < len(value):
        if value.startswith("{{", index):
            template_depth += 1
            index += 2
            continue
        if value.startswith("}}", index) and template_depth:
            template_depth -= 1
            index += 2
            continue
        if value.startswith("[[", index):
            link_depth += 1
            index += 2
            continue
        if value.startswith("]]", index) and link_depth:
            link_depth -= 1
            index += 2
            continue
        if value.startswith("{|", index):
            table_depth += 1
            index += 2
            continue
        if value.startswith("|}", index) and table_depth:
            table_depth -= 1
            index += 2
            continue
        if (
            value[index] == delimiter
            and template_depth == 0
            and link_depth == 0
            and table_depth == 0
        ):
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts


def split_top_level_once(value: str, delimiter: str = "=") -> tuple[str, str] | None:
    parts = split_top_level(value, delimiter)
    if len(parts) < 2:
        return None
    return parts[0], delimiter.join(parts[1:])


def _template_end(text: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(text) - 1:
        if text.startswith("{{", index):
            depth += 1
            index += 2
            continue
        if text.startswith("}}", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
            continue
        index += 1
    return -1


def iter_important_templates(
    text: str, depth: int = 0
) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    if depth > 12:
        return
    index = 0
    while True:
        start = text.find("{{", index)
        if start < 0:
            return
        end = _template_end(text, start)
        if end < 0:
            return
        inner = text[start + 2 : end - 2]
        parts = split_top_level(inner)
        raw_name = parts[0].strip() if parts else ""
        template_name = re.sub(r"<[^>]+>", "", raw_name).strip()
        if template_name in IMPORTANT_TEMPLATES:
            fields: list[tuple[str, str]] = []
            positional_index = 0
            for raw_part in parts[1:]:
                pair = split_top_level_once(raw_part, "=")
                if pair:
                    key, value = pair
                    key = key.strip()
                else:
                    key = str(positional_index)
                    value = raw_part
                    positional_index += 1
                value = value.strip()
                if key and value:
                    fields.append((key, value))
            yield template_name, fields
        yield from iter_important_templates(inner, depth + 1)
        index = end


def _template_to_text(match: re.Match[str]) -> str:
    parts = split_top_level(match.group(1))
    output: list[str] = []
    if parts and parts[0].strip():
        output.append(parts[0].strip())
    for part in parts[1:]:
        stripped = part.strip()
        if not stripped:
            continue
        pair = split_top_level_once(stripped, "=")
        if pair:
            key, value = pair
            output.append(f"{key.strip()}: {value.strip()}")
        else:
            output.append(stripped)
    return "\n".join(output)


def _replace_file_link(match: re.Match[str]) -> str:
    body = match.group(1)
    parts = split_top_level(body)
    captions = [
        part.strip()
        for part in parts[1:]
        if part.strip()
        and not re.match(
            r"^(?:\d+px|thumb|thumbnail|frameless|right|left|center|"
            r"border|alt|link|upright|缩略图|有框|无框|左|右|居中)",
            part.strip(),
            re.IGNORECASE,
        )
    ]
    return captions[-1] if captions else ""


def wikitext_to_plain(wikitext: str) -> str:
    text = wikitext
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(
        r"<(ref|references)\b[^>]*>.*?</\1\s*>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<gallery\b[^>]*>.*?</gallery\s*>", "", text, flags=re.DOTALL | re.I)
    text = re.sub(
        r"\[\[\s*(?:File|Image|文件)\s*:(.*?)\]\]",
        _replace_file_link,
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"\[\[\s*(?:Category|分类)\s*:[^\]]*\]\]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<hr\s*/?>", "\n", text, flags=re.IGNORECASE)

    for _ in range(12):
        updated = re.sub(r"\{\{([^{}]*)\}\}", _template_to_text, text, flags=re.DOTALL)
        if updated == text:
            break
        text = updated

    text = re.sub(
        r"\[\[([^\]|]+)\|([^\]]+)\]\]",
        lambda match: match.group(2),
        text,
    )
    text = re.sub(r"\[\[([^\]]+)\]\]", lambda match: match.group(1), text)
    text = re.sub(
        r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]",
        lambda match: match.group(1),
        text,
    )
    text = re.sub(r"\[(?:https?:)?//[^\]]+\]", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^={1,6}\s*(.*?)\s*={1,6}\s*$", r"\1", text, flags=re.M)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("{|", "\n").replace("|}", "\n")
    text = re.sub(r"^\s*\|[-+]\s*.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[!|]\s*", "", text, flags=re.MULTILINE)
    text = text.replace("{{", "\n").replace("}}", "\n")
    text = re.sub(r"\[\[|\]\]", "", text)
    text = text.replace("|", " ")
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(
    wikitext: str, page_title: str
) -> list[tuple[int, str, str]]:
    matches = list(HEADING_RE.finditer(wikitext))
    if not matches:
        return [(1, page_title, wikitext)]

    sections: list[tuple[int, str, str]] = []
    preamble = wikitext[: matches[0].start()].strip()
    if preamble:
        sections.append((1, page_title, preamble))

    for number, match in enumerate(matches):
        start = match.end()
        end = matches[number + 1].start() if number + 1 < len(matches) else len(wikitext)
        heading = match.group(2).strip() or page_title
        level = len(match.group(1))
        body = wikitext[start:end].strip()
        sections.append((level, heading, body))
    return sections


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 160) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        target_end = min(len(text), start + max_chars)
        end = target_end
        if target_end < len(text):
            newline = text.rfind("\n", start + max_chars // 2, target_end)
            if newline > start:
                end = newline
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def normalize_for_search(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.sub(
        r"第?([一二三四五六七八九十])技能",
        lambda match: NUMERAL_DIGITS[match.group(1)] + "技能",
        normalized,
    )


def tokenize_for_fts(text: str) -> list[str]:
    normalized = normalize_for_search(text)
    tokens: list[str] = []
    for segment in SEGMENT_RE.findall(normalized):
        if CJK_RUN_RE.fullmatch(segment):
            if len(segment) == 1:
                tokens.append(segment)
            else:
                tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
        else:
            tokens.append(segment)
    return tokens


def query_token_groups(query: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for segment in SEGMENT_RE.findall(normalize_for_search(query)):
        if CJK_RUN_RE.fullmatch(segment):
            if len(segment) == 1:
                groups.append([segment])
            else:
                groups.append(
                    [segment[index : index + 2] for index in range(len(segment) - 1)]
                )
        else:
            groups.append([segment])
    return groups


def _fts_quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def build_fts_query(groups: Sequence[Sequence[str]], operator: str) -> str:
    tokens = []
    seen: set[str] = set()
    for group in groups:
        for token in group:
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    joiner = f" {operator.upper()} "
    return joiner.join(_fts_quote(token) for token in tokens)


def generate_page_index(
    connection: sqlite3.Connection,
    page_id: int,
    title: str,
    wikitext: str,
) -> tuple[str, int, int]:
    old_chunk_ids = [
        row["id"]
        for row in connection.execute("SELECT id FROM chunks WHERE page_id = ?", (page_id,))
    ]
    for chunk_id in old_chunk_ids:
        connection.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    connection.execute("DELETE FROM chunks WHERE page_id = ?", (page_id,))
    connection.execute("DELETE FROM sections WHERE page_id = ?", (page_id,))
    connection.execute("DELETE FROM template_fields WHERE page_id = ?", (page_id,))

    section_rows = split_sections(wikitext, title)
    plain_sections: list[str] = []
    section_count = 0
    chunk_count = 0
    chunk_number = 0
    for section_no, (level, heading, body) in enumerate(section_rows):
        plain = wikitext_to_plain(body)
        if not plain:
            continue
        section_count += 1
        plain_sections.append(f"## {heading}\n{plain}")
        connection.execute(
            """
            INSERT INTO sections(page_id, section_no, heading, level, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            (page_id, section_no, heading, level, plain),
        )
        for chunk in chunk_text(plain):
            chunk_number += 1
            title_tokens = " ".join(tokenize_for_fts(f"{title} {heading}"))
            body_tokens = " ".join(tokenize_for_fts(chunk))
            cursor = connection.execute(
                """
                INSERT INTO chunks(page_id, chunk_no, heading, text)
                VALUES(?, ?, ?, ?)
                """,
                (page_id, chunk_number, heading, chunk),
            )
            chunk_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO chunks_fts(rowid, title_tokens, body_tokens)
                VALUES(?, ?, ?)
                """,
                (chunk_id, title_tokens, body_tokens),
            )
            chunk_count += 1

    instance_numbers: dict[str, int] = {}
    for template_name, fields in iter_important_templates(wikitext):
        instance_no = instance_numbers.get(template_name, 0) + 1
        instance_numbers[template_name] = instance_no
        for field_no, (key, value) in enumerate(fields):
            connection.execute(
                """
                INSERT OR REPLACE INTO template_fields(
                    page_id, template_name, instance_no, field_no,
                    field_key, field_value
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (page_id, template_name, instance_no, field_no, key, value),
            )

    plain_text = "\n\n".join(plain_sections).strip()
    return plain_text, section_count, chunk_count


def upsert_page(connection: sqlite3.Connection, page: dict[str, Any]) -> None:
    page_id = int(page["pageid"])
    title = str(page["title"])
    revision = page["revisions"][0]
    slot = revision.get("slots", {}).get("main", {})
    wikitext = str(slot.get("content", ""))
    redirect_match = REDIRECT_RE.match(wikitext)
    redirect_target = redirect_match.group(1).strip() if redirect_match else None
    categories = sorted(
        {
            str(category.get("title", "")).removeprefix("Category:")
            .removeprefix("分类:")
            .strip()
            for category in page.get("categories", [])
            if category.get("title")
        }
    )
    thumbnail = page.get("thumbnail", {}).get("source")
    page_image = page.get("pageimage")

    title_owner = connection.execute(
        "SELECT page_id FROM pages WHERE title = ?", (title,)
    ).fetchone()
    if title_owner and int(title_owner["page_id"]) != page_id:
        _delete_page(connection, int(title_owner["page_id"]))

    connection.execute(
        """
        INSERT INTO pages(
            page_id, namespace, title, url, revision_id, revision_timestamp,
            content_model, sha1, size_bytes, wikitext, plain_text, is_redirect,
            redirect_target, categories_json, thumbnail_url, page_image,
            touched, fetched_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            namespace = excluded.namespace,
            title = excluded.title,
            url = excluded.url,
            revision_id = excluded.revision_id,
            revision_timestamp = excluded.revision_timestamp,
            content_model = excluded.content_model,
            sha1 = excluded.sha1,
            size_bytes = excluded.size_bytes,
            wikitext = excluded.wikitext,
            plain_text = excluded.plain_text,
            is_redirect = excluded.is_redirect,
            redirect_target = excluded.redirect_target,
            categories_json = excluded.categories_json,
            thumbnail_url = excluded.thumbnail_url,
            page_image = excluded.page_image,
            touched = excluded.touched,
            fetched_at = excluded.fetched_at
        """,
        (
            page_id,
            int(page.get("ns", 0)),
            title,
            page_url(title),
            int(revision.get("revid", 0)),
            revision.get("timestamp"),
            slot.get("contentmodel") or revision.get("contentmodel"),
            revision.get("sha1"),
            int(revision.get("size", len(wikitext.encode("utf-8")))),
            wikitext,
            "",
            1 if page.get("redirect") is not None or redirect_target else 0,
            redirect_target,
            json.dumps(categories, ensure_ascii=False),
            thumbnail,
            page_image,
            page.get("touched"),
            utc_now(),
        ),
    )
    plain_text, _, _ = generate_page_index(connection, page_id, title, wikitext)
    connection.execute(
        "UPDATE pages SET plain_text = ? WHERE page_id = ?",
        (plain_text, page_id),
    )


def sync_namespace(
    client: MediaWikiClient,
    connection: sqlite3.Connection,
    namespace: int,
    full: bool,
    prune: bool,
    resume: bool = False,
) -> tuple[int, int]:
    siteinfo = fetch_siteinfo(client)
    server_time = siteinfo.get("general", {}).get("time") or utc_now()
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES('siteinfo', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (json.dumps(siteinfo, ensure_ascii=False),),
    )
    set_meta(connection, "source_api", API_URL)
    set_meta(connection, "license", LICENSE_NAME)
    set_meta(connection, "license_url", LICENSE_URL)

    page_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM pages WHERE namespace = ?", (namespace,)
        ).fetchone()[0]
    )
    is_full_sync = full or page_count == 0
    seen_titles: set[str] | None = set() if is_full_sync else None
    if is_full_sync:
        log(f"Listing namespace {namespace}...")
        titles = []
        for page in iter_all_pages(client, namespace):
            title = str(page["title"])
            titles.append(title)
            if seen_titles is not None:
                seen_titles.add(title)
        changed_titles = titles
        if resume:
            existing_titles = {
                str(row["title"])
                for row in connection.execute(
                    "SELECT title FROM pages WHERE namespace = ?", (namespace,)
                )
            }
            before_resume_filter = len(changed_titles)
            changed_titles = [
                title for title in changed_titles if title not in existing_titles
            ]
            log(
                f"Resume: keeping {before_resume_filter - len(changed_titles):,} "
                "already indexed pages"
            )
        log(f"Namespace {namespace}: {len(changed_titles):,} pages to fetch")
    else:
        since = get_meta(connection, f"last_sync_utc_ns_{namespace}")
        if not since:
            log("No incremental checkpoint; switching to a full sync")
            return sync_namespace(client, connection, namespace, True, prune, resume)
        changed_titles_set: set[str] = set()
        for change in iter_recent_changes(client, [namespace], since):
            if str(change.get("ns")) == str(namespace) and change.get("title"):
                changed_titles_set.add(str(change["title"]))
        changed_titles = sorted(changed_titles_set)
        log(
            f"Namespace {namespace}: {len(changed_titles):,} changed pages "
            f"since {since}"
        )

    updated = 0
    fetched_titles: set[str] = set()
    batch_size = 50
    for start in range(0, len(changed_titles), batch_size):
        batch = changed_titles[start : start + batch_size]
        pages = fetch_pages(client, batch)
        with connection:
            for page in pages:
                upsert_page(connection, page)
                updated += 1
                fetched_titles.add(str(page["title"]))
        if start == 0 or (start + batch_size) >= len(changed_titles) or (
            start // batch_size
        ) % 10 == 0:
            log(
                f"  indexed {min(start + batch_size, len(changed_titles)):,}/"
                f"{len(changed_titles):,}"
            )

    if is_full_sync and prune:
        keep_titles = seen_titles or fetched_titles
        stale_rows = [
            row["page_id"]
            for row in connection.execute(
                "SELECT page_id, title FROM pages WHERE namespace = ?", (namespace,)
            )
            if row["title"] not in keep_titles
        ]
        with connection:
            for page_id in stale_rows:
                _delete_page(connection, int(page_id))
        if stale_rows:
            log(f"Pruned {len(stale_rows):,} pages no longer present")

    checkpoint = server_time if is_full_sync else utc_now()
    with connection:
        set_meta(connection, f"last_sync_utc_ns_{namespace}", checkpoint)
        if is_full_sync:
            set_meta(connection, f"last_full_sync_utc_ns_{namespace}", checkpoint)
        set_meta(connection, "last_sync_utc", checkpoint)
        set_meta(
            connection,
            "site_statistics",
            json.dumps(siteinfo.get("statistics", {}), ensure_ascii=False),
        )
    return updated, len(changed_titles)


def _delete_page(connection: sqlite3.Connection, page_id: int) -> None:
    for row in connection.execute(
        "SELECT id FROM chunks WHERE page_id = ?", (page_id,)
    ):
        connection.execute("DELETE FROM chunks_fts WHERE rowid = ?", (int(row["id"]),))
    connection.execute("DELETE FROM chunks WHERE page_id = ?", (page_id,))
    connection.execute("DELETE FROM sections WHERE page_id = ?", (page_id,))
    connection.execute("DELETE FROM template_fields WHERE page_id = ?", (page_id,))
    connection.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))


def title_candidates(
    connection: sqlite3.Connection, query: str, limit: int = 12
) -> list[sqlite3.Row]:
    normalized = urllib.parse.unquote(query).strip().replace("_", " ")
    if not normalized:
        return []
    exact = connection.execute(
        """
        SELECT * FROM pages
        WHERE title = ? COLLATE NOCASE
        ORDER BY length(title)
        """,
        (normalized,),
    ).fetchall()
    if exact:
        return exact

    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return connection.execute(
        """
        SELECT * FROM pages
        WHERE title LIKE ? ESCAPE '\\' COLLATE NOCASE
        ORDER BY
            CASE WHEN title LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 0 ELSE 1 END,
            length(title),
            title
        LIMIT ?
        """,
        (f"%{escaped}%", f"{escaped}%", limit),
    ).fetchall()


def resolve_page(
    connection: sqlite3.Connection, title: str, follow_redirect: bool = True
) -> tuple[sqlite3.Row, list[str]]:
    candidates = title_candidates(connection, title, limit=12)
    if not candidates:
        raise ValueError(f"No page title matched: {title}")
    exact = [
        row
        for row in candidates
        if row["title"].casefold() == title.replace("_", " ").casefold()
    ]
    page = exact[0] if exact else candidates[0]
    redirect_chain: list[str] = []
    seen: set[int] = set()
    while follow_redirect and page["redirect_target"] and page["page_id"] not in seen:
        seen.add(int(page["page_id"]))
        redirect_chain.append(str(page["title"]))
        target = str(page["redirect_target"])
        target_rows = title_candidates(connection, target, limit=2)
        if not target_rows:
            break
        page = target_rows[0]
    return page, redirect_chain


def _excerpt(text: str, query: str, max_chars: int = 600) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    needles = re.findall(f"[{CJK_RANGES}]+|[A-Za-z0-9]+", normalize_for_search(query))
    compact_folded = normalize_for_search(compact)
    position = -1
    width = 0
    for needle in sorted(needles, key=len, reverse=True):
        found = compact_folded.find(needle)
        if found >= 0:
            position = found
            width = len(needle)
            break
    if position < 0:
        return compact[:max_chars] + ("..." if len(compact) > max_chars else "")
    start = max(0, position - 130)
    end = min(len(compact), max(position + width + 420, start + max_chars))
    prefix = "..." if start else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def search_database(
    connection: sqlite3.Connection, query: str, limit: int = 10
) -> dict[str, Any]:
    groups = query_token_groups(query)
    if not groups:
        raise ValueError("Search query is empty after normalization")

    all_query = build_fts_query(groups, "AND")
    any_query = build_fts_query(groups, "OR")
    sql = """
        SELECT
            c.id AS chunk_id,
            c.heading,
            c.text,
            p.page_id,
            p.title,
            p.url,
            p.redirect_target,
            p.revision_timestamp,
            bm25(chunks_fts, 8.0, 1.0) AS score
        FROM chunks_fts
        JOIN chunks AS c ON c.id = chunks_fts.rowid
        JOIN pages AS p ON p.page_id = c.page_id
        WHERE chunks_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """
    rows = connection.execute(sql, (all_query, limit * 8)).fetchall()
    match_mode = "all"
    if len(rows) < limit:
        fallback = connection.execute(sql, (any_query, limit * 12)).fetchall()
        seen_ids = {int(row["chunk_id"]) for row in rows}
        rows.extend(row for row in fallback if int(row["chunk_id"]) not in seen_ids)
        match_mode = "all+any" if rows else "any"
    rows.sort(key=lambda row: float(row["score"]))

    title_queries: list[str] = [query.strip()]
    query_segments = [
        segment
        for segment in SEGMENT_RE.findall(normalize_for_search(query))
        if len(segment) >= 2 and segment.casefold() not in TITLE_STOPWORDS
    ]
    title_queries.extend(
        sorted(dict.fromkeys(query_segments), key=len, reverse=True)
    )
    title_rows: list[sqlite3.Row] = []
    seen_title_pages: set[int] = set()
    for title_query in title_queries:
        normalized_title_query = title_query.replace("_", " ").casefold()
        for row in title_candidates(connection, title_query, limit=limit):
            if str(row["title"]).replace("_", " ").casefold() != normalized_title_query:
                continue
            page_id = int(row["page_id"])
            if page_id not in seen_title_pages:
                seen_title_pages.add(page_id)
                title_rows.append(row)
                target = row["redirect_target"]
                if target:
                    for target_row in title_candidates(connection, str(target), limit=2):
                        if str(target_row["title"]).casefold() != str(target).casefold():
                            continue
                        target_id = int(target_row["page_id"])
                        if target_id not in seen_title_pages:
                            seen_title_pages.add(target_id)
                            title_rows.append(target_row)

    results: list[dict[str, Any]] = []
    per_page: dict[int, int] = {}
    seen_pages: set[int] = set()

    for row in title_rows:
        page_id = int(row["page_id"])
        if len(results) >= limit:
            break
        results.append(
            {
                "title": row["title"],
                "url": row["url"],
                "redirect_target": row["redirect_target"],
                "heading": "",
                "excerpt": _excerpt(row["plain_text"], query),
                "score": -1000.0,
                "revision_timestamp": row["revision_timestamp"],
                "match": "title",
            }
        )
        seen_pages.add(page_id)
        per_page[page_id] = 1

    for row in rows:
        if len(results) >= limit:
            break
        page_id = int(row["page_id"])
        if per_page.get(page_id, 0) >= 2:
            continue
        results.append(
            {
                "title": row["title"],
                "url": row["url"],
                "redirect_target": row["redirect_target"],
                "heading": row["heading"],
                "excerpt": _excerpt(str(row["text"]), query),
                "score": round(float(row["score"]), 4),
                "revision_timestamp": row["revision_timestamp"],
                "match": match_mode,
            }
        )
        per_page[page_id] = per_page.get(page_id, 0) + 1
        seen_pages.add(page_id)

    return {
        "query": query,
        "results": results,
        "result_count": len(results),
        "match_mode": match_mode,
        "source": API_URL,
    }


def page_to_dict(row: sqlite3.Row, include_raw: bool = False) -> dict[str, Any]:
    result = {
        "page_id": row["page_id"],
        "namespace": row["namespace"],
        "title": row["title"],
        "url": row["url"],
        "revision_id": row["revision_id"],
        "revision_timestamp": row["revision_timestamp"],
        "fetched_at": row["fetched_at"],
        "content_model": row["content_model"],
        "size_bytes": row["size_bytes"],
        "is_redirect": bool(row["is_redirect"]),
        "redirect_target": row["redirect_target"],
        "categories": json.loads(row["categories_json"]),
        "thumbnail_url": row["thumbnail_url"],
        "page_image": row["page_image"],
    }
    if include_raw:
        result["wikitext"] = row["wikitext"]
        result["plain_text"] = row["plain_text"]
    return result


def show_page(
    connection: sqlite3.Connection,
    title: str,
    section: str | None,
    raw: bool,
    max_chars: int,
) -> dict[str, Any]:
    page, redirect_chain = resolve_page(connection, title)
    result = page_to_dict(page, include_raw=False)
    result["requested_title"] = title
    result["redirect_chain"] = redirect_chain

    if raw:
        if section:
            section_rows = split_sections(str(page["wikitext"]), str(page["title"]))
            matched = [
                (level, heading, body)
                for level, heading, body in section_rows
                if section.casefold() in heading.casefold()
            ]
            if not matched:
                raise ValueError(f"Section not found in {page['title']}: {section}")
            text = "\n\n".join(
                f"{'=' * level} {heading} {'=' * level}\n{body}"
                for level, heading, body in matched
            )
        else:
            text = str(page["wikitext"])
    else:
        rows = connection.execute(
            "SELECT heading, level, text FROM sections WHERE page_id = ? ORDER BY section_no",
            (int(page["page_id"]),),
        ).fetchall()
        if section:
            rows = [row for row in rows if section.casefold() in str(row["heading"]).casefold()]
            if not rows:
                raise ValueError(f"Section not found in {page['title']}: {section}")
        text = "\n\n".join(
            f"## {row['heading']}\n{row['text']}" for row in rows
        )

    truncated = len(text) > max_chars
    result["text"] = text[:max_chars]
    result["truncated"] = truncated
    if truncated:
        result["text"] += "\n\n[TRUNCATED]"
    return result


def fields_for_page(connection: sqlite3.Connection, title: str) -> dict[str, Any]:
    page, redirect_chain = resolve_page(connection, title)
    rows = connection.execute(
        """
        SELECT template_name, instance_no, field_no, field_key, field_value
        FROM template_fields
        WHERE page_id = ?
        ORDER BY template_name, instance_no, field_no
        """,
        (int(page["page_id"]),),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        template = str(row["template_name"])
        instances = grouped.setdefault(template, [])
        instance_no = int(row["instance_no"])
        while len(instances) < instance_no:
            instances.append({})
        instances[instance_no - 1][str(row["field_key"])] = row["field_value"]
    return {
        "title": page["title"],
        "url": page["url"],
        "revision_id": page["revision_id"],
        "redirect_chain": redirect_chain,
        "templates": grouped,
    }


def stats(connection: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    counts = {}
    for table in ("pages", "sections", "chunks", "template_fields"):
        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    counts["redirect_pages"] = int(
        connection.execute("SELECT COUNT(*) FROM pages WHERE is_redirect = 1").fetchone()[0]
    )
    meta = {
        str(row["key"]): row["value"]
        for row in connection.execute("SELECT key, value FROM meta")
    }
    return {
        "database": str(db_path.resolve()),
        "database_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "counts": counts,
        "last_sync_utc": meta.get("last_sync_utc"),
        "license": meta.get("license", LICENSE_NAME),
        "license_url": meta.get("license_url", LICENSE_URL),
        "site_statistics": json.loads(meta["site_statistics"])
        if meta.get("site_statistics")
        else None,
    }


def reindex_database(connection: sqlite3.Connection) -> int:
    page_ids = [
        int(row["page_id"])
        for row in connection.execute("SELECT page_id FROM pages ORDER BY page_id")
    ]
    updated = 0
    for start in range(0, len(page_ids), 200):
        batch = page_ids[start : start + 200]
        with connection:
            for page_id in batch:
                row = connection.execute(
                    "SELECT title, wikitext FROM pages WHERE page_id = ?", (page_id,)
                ).fetchone()
                if not row:
                    continue
                plain_text, _, _ = generate_page_index(
                    connection,
                    page_id,
                    str(row["title"]),
                    str(row["wikitext"]),
                )
                connection.execute(
                    "UPDATE pages SET plain_text = ? WHERE page_id = ?",
                    (plain_text, page_id),
                )
                updated += 1
        if start == 0 or min(start + 200, len(page_ids)) == len(page_ids) or (
            start // 200
        ) % 10 == 0:
            log(f"  reindexed {min(start + 200, len(page_ids)):,}/{len(page_ids):,}")
    connection.execute("PRAGMA optimize")
    return updated


def print_search_markdown(result: dict[str, Any]) -> None:
    print(f"# 搜索结果：{result['query']}")
    print()
    if not result["results"]:
        print("没有找到匹配内容。")
        return
    for index, item in enumerate(result["results"], start=1):
        heading = f" · {item['heading']}" if item["heading"] else ""
        redirect = (
            f"（重定向至 {item['redirect_target']}）"
            if item.get("redirect_target")
            else ""
        )
        print(f"{index}. [{item['title']}]({item['url']}){redirect}{heading}")
        print()
        print(item["excerpt"])
        print()
        print(f"  修订时间：{item['revision_timestamp'] or '未知'}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local searchable knowledge base for fgo.wiki"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Sync pages from fgo.wiki")
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help="Rescan and refetch every page in the selected namespaces",
    )
    sync_parser.add_argument(
        "--prune",
        action="store_true",
        help="After a full sync, remove pages no longer present",
    )
    sync_parser.add_argument(
        "--resume",
        action="store_true",
        help="With --full, skip pages already present in the database",
    )
    sync_parser.add_argument(
        "--namespace",
        type=int,
        nargs="+",
        default=[0],
        help="MediaWiki namespace IDs (default: main articles only)",
    )
    sync_parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Minimum seconds between API requests (default: 0.5)",
    )

    search_parser = subparsers.add_parser("search", help="Full-text search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )

    show_parser = subparsers.add_parser("show", help="Read one page")
    show_parser.add_argument("title")
    show_parser.add_argument("--section", help="Only output sections containing this text")
    show_parser.add_argument("--raw", action="store_true", help="Output original wikitext")
    show_parser.add_argument("--max-chars", type=int, default=20000)
    show_parser.add_argument("--format", choices=("json", "text"), default="text")

    fields_parser = subparsers.add_parser(
        "fields", help="Show structured fields extracted from common templates"
    )
    fields_parser.add_argument("title")

    subparsers.add_parser(
        "reindex", help="Rebuild the local search index from stored wikitext"
    )
    subparsers.add_parser("stats", help="Show database statistics")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = args.db.resolve()

    if args.command == "sync":
        client = MediaWikiClient(delay=args.delay)
        connection = connect_database(db_path)
        try:
            total_updated = 0
            total_seen = 0
            for namespace in args.namespace:
                updated, seen = sync_namespace(
                    client,
                    connection,
                    namespace,
                    args.full,
                    args.prune,
                    args.resume,
                )
                total_updated += updated
                total_seen += seen
            connection.execute("PRAGMA optimize")
            log(
                f"Sync complete: {total_updated:,} pages indexed, "
                f"{total_seen:,} pages checked"
            )
        finally:
            connection.close()
        return 0

    if not db_path.exists():
        raise SystemExit(
            f"Database not found: {db_path}\nRun: python fgo_kb.py --db \"{db_path}\" sync --full"
        )

    connection = connect_database(db_path)
    try:
        if args.command == "search":
            result = search_database(connection, args.query, limit=max(1, args.limit))
            if args.format == "markdown":
                print_search_markdown(result)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "show":
            result = show_page(
                connection,
                args.title,
                args.section,
                args.raw,
                max(1, args.max_chars),
            )
            if args.format == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                if result["redirect_chain"]:
                    print(
                        "Redirect: "
                        + " -> ".join(result["redirect_chain"] + [result["title"]])
                    )
                    print()
                print(result["text"])
                print()
                print(f"Source: {result['url']}")
                print(
                    f"Revision: {result['revision_id']} "
                    f"({result['revision_timestamp']})"
                )
            return 0
        if args.command == "fields":
            print(
                json.dumps(
                    fields_for_page(connection, args.title),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "reindex":
            count = reindex_database(connection)
            log(f"Reindex complete: {count:,} pages")
            return 0
        if args.command == "stats":
            print(json.dumps(stats(connection, db_path), ensure_ascii=False, indent=2))
            return 0
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
