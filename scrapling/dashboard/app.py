from __future__ import annotations

import asyncio
import csv
import gzip
import io
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse
from html import unescape

import orjson
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from scrapling.fetchers import Fetcher
from tld import get_fld


STATIC_DIR = Path(__file__).parent / "static"
DATA_DIR = Path.cwd() / ".scrapling-dashboard"
JOBS_DIR = DATA_DIR / "jobs"
_jobs: dict[str, dict[str, Any]] = {}
_tasks: dict[str, asyncio.Task] = {}
_lock = threading.RLock()


def _now() -> float:
    return round(time.time(), 3)


def _public_job(job: dict[str, Any], include_pages: bool = False) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if key != "pages"}
    result["page_count"] = len(job.get("pages", []))
    if include_pages:
        result["pages"] = [
            {key: value for key, value in page.items() if key != "html"}
            for page in job.get("pages", [])
        ]
    return result


def _save(job: dict[str, Any]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    target = JOBS_DIR / f"{job['id']}.json"
    temp = target.with_suffix(".tmp")
    temp.write_bytes(orjson.dumps(job, option=orjson.OPT_INDENT_2))
    temp.replace(target)


def _load_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = orjson.loads(path.read_bytes())
            if job.get("status") in {"queued", "running"}:
                job["status"] = "interrupted"
                job["message"] = "Job bị gián đoạn khi dashboard dừng."
            _jobs[job["id"]] = job
        except Exception:
            continue


def _first_text(page: Any, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            value = page.css(selector).get()
            if value and value.strip():
                return value.strip()
        except Exception:
            continue
    return ""


def _detect_language(response: Any, url: str, content: str) -> str:
    declared = _first_text(
        response,
        ["html::attr(lang)", "meta[http-equiv='content-language']::attr(content)", "meta[property='og:locale']::attr(content)"],
    ).lower().replace("_", "-")
    if declared.startswith("vi"):
        return "vi"
    if declared.startswith("en"):
        return "en"
    path = urlparse(url).path.lower()
    if re.search(r"/(?:vi|vi-vn)(?:/|$)", path):
        return "vi"
    if re.search(r"/(?:en|en-us|en-gb)(?:/|$)", path):
        return "en"
    sample = content[:20_000].lower()
    vietnamese_marks = sum(sample.count(char) for char in "ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ")
    if vietnamese_marks >= 3:
        return "vi"
    return "en" if re.search(r"\b(?:the|and|with|from|this|that|for|you|your)\b", sample) else "unknown"


def _extract(response: Any, url: str, keep_html: bool) -> tuple[dict[str, Any], list[str]]:
    title = _first_text(response, ["meta[property='og:title']::attr(content)", "title::text", "h1::text"])
    description = _first_text(
        response,
        ["meta[name='description']::attr(content)", "meta[property='og:description']::attr(content)"],
    )
    published_at = _first_text(
        response,
        [
            "meta[property='article:published_time']::attr(content)",
            "time::attr(datetime)",
            "meta[name='date']::attr(content)",
        ],
    )
    author = _first_text(
        response,
        ["meta[name='author']::attr(content)", "meta[property='article:author']::attr(content)", "[rel='author']::text"],
    )
    content_node = None
    for selector in ["article", "main", "[role='main']", ".post-content", ".article-content", "body"]:
        matches = response.css(selector)
        if matches:
            content_node = max(matches, key=lambda node: len(node.get_all_text()))
            break
    content = content_node.get_all_text(separator="\n", strip=True) if content_node else response.get_all_text(separator="\n", strip=True)
    headings = [text.strip() for text in response.css("h1::text, h2::text, h3::text").getall() if text.strip()][:50]
    images = []
    for src in response.css("img::attr(src)").getall():
        if src:
            absolute = response.urljoin(src)
            if absolute not in images:
                images.append(absolute)
    links = []
    for href in response.css("a::attr(href)").getall():
        if not href:
            continue
        absolute, _ = urldefrag(response.urljoin(href))
        parsed = urlparse(absolute)
        if parsed.scheme in {"http", "https"} and absolute not in links:
            links.append(absolute)
    html = response.html_content if keep_html else ""
    item = {
        "url": url,
        "status": response.status,
        "title": title,
        "description": description,
        "author": author,
        "published_at": published_at,
        "content": content[:500_000],
        "headings": headings,
        "images": images[:100],
        "links": links[:500],
        "html": html[:2_000_000],
        "scraped_at": _now(),
        "word_count": len(content.split()),
    }
    item["language"] = _detect_language(response, url, content)
    return item, links


def _in_scope(url: str, origin: Any, scope: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if scope == "path":
        if parsed.netloc.lower() != origin.netloc.lower():
            return False
        root_path = origin.path.rstrip("/") or "/"
        target_path = parsed.path.rstrip("/") or "/"
        return target_path == root_path or target_path.startswith(root_path + "/")
    if scope == "hostname":
        return parsed.netloc.lower() == origin.netloc.lower()
    root = get_fld(origin.geturl(), fix_protocol=True, fail_silently=True) or origin.hostname
    target = get_fld(url, fix_protocol=True, fail_silently=True) or parsed.hostname
    return bool(root and target and root.lower() == target.lower())


def _url_matches_language(url: str, language: str) -> bool:
    if language == "all":
        return True
    segments = [segment.lower() for segment in urlparse(url).path.split("/") if segment][:4]
    language_codes = {
        "ar", "bn", "cs", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "hu", "id", "it",
        "ja", "ko", "ms", "nl", "no", "pl", "pt", "ro", "ru", "sk", "sv", "ta", "te", "th", "tr",
        "uk", "ur", "vi", "zh",
    }
    for segment in segments:
        if segment in language_codes:
            return segment == language
        locale = re.fullmatch(r"[a-z]{2}[-_]([a-z]{2})", segment)
        if locale:
            return locale.group(1) == language
    return True


def _sitemap_locations(content: bytes) -> list[str]:
    try:
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
    except (OSError, EOFError):
        return []
    text = content.decode("utf-8", errors="ignore")
    return [unescape(value.strip()) for value in re.findall(r"<loc[^>]*>(.*?)</loc>", text, re.I | re.S)]


async def _discover_sitemaps(job: dict[str, Any], origin: Any) -> list[str]:
    base = f"{origin.scheme}://{origin.netloc}"
    path_segments = [segment for segment in origin.path.split("/") if segment]
    scoped_candidates = []
    if job["scope"] == "path" and path_segments:
        for length in range(len(path_segments), 0, -1):
            prefix = "/".join(path_segments[:length])
            scoped_candidates.append(f"{base}/{prefix}/sitemap.xml")
    candidates = deque(scoped_candidates + [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"])
    discovered: list[str] = []
    seen_maps: set[str] = set()
    try:
        robots = await asyncio.to_thread(Fetcher.get, f"{base}/robots.txt", timeout=min(job["timeout"], 20))
        robots_text = robots.body.decode("utf-8", errors="ignore")
        robot_maps = [value.strip() for value in re.findall(r"^\s*Sitemap:\s*(\S+)", robots_text, re.I | re.M)]
        if job["scope"] == "path" and path_segments:
            root_segment = f"/{path_segments[0]}/"
            robot_maps = [value for value in robot_maps if root_segment in urlparse(value).path]
        candidates.extend(robot_maps)
    except Exception:
        pass

    while candidates and len(seen_maps) < 20 and len(discovered) < 20_000:
        sitemap_url = candidates.popleft()
        if sitemap_url in seen_maps:
            continue
        seen_maps.add(sitemap_url)
        try:
            response = await asyncio.to_thread(Fetcher.get, sitemap_url, timeout=min(job["timeout"], 30))
            if response.status >= 400:
                continue
            for location in _sitemap_locations(response.body):
                if re.search(r"(?:sitemap|sitemap_index)[^/]*\.(?:xml|xml\.gz)(?:$|\?)", location, re.I):
                    if job["scope"] != "path" or _in_scope(location, origin, "path"):
                        candidates.append(location)
                elif (
                    _in_scope(location, origin, job["scope"])
                    and _url_matches_language(location, job["language"])
                    and location not in discovered
                ):
                    discovered.append(location)
        except Exception:
            continue
    return discovered


async def _crawl(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        job.update(status="running", started_at=_now(), message="Đang kết nối tới website…")
        _save(job)

    start_url = job["url"]
    origin = urlparse(start_url)
    queue = deque([start_url])
    seen: set[str] = set()
    max_pages = job["max_pages"]

    try:
        if job["use_sitemap"]:
            with _lock:
                job["message"] = "Đang tìm URL trong robots.txt và sitemap…"
            sitemap_urls = await _discover_sitemaps(job, origin)
            for sitemap_url in sitemap_urls:
                if sitemap_url != start_url:
                    queue.append(sitemap_url)
            with _lock:
                job["sitemap_urls"] = len(sitemap_urls)
                job["discovered"] = len(queue)
                _save(job)
        while queue and len(seen) < max_pages:
            with _lock:
                if job.get("cancel_requested"):
                    job.update(status="cancelled", message="Đã dừng theo yêu cầu.", finished_at=_now())
                    _save(job)
                    return
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            with _lock:
                job["current_url"] = url
                job["message"] = f"Đang lấy trang {len(seen)}/{max_pages}"
                job["discovered"] = len(seen) + len(queue)

            try:
                response = await asyncio.to_thread(
                    Fetcher.get,
                    url,
                    timeout=job["timeout"],
                    follow_redirects=True,
                    stealthy_headers=True,
                )
                item, links = await asyncio.to_thread(_extract, response, url, job["keep_html"])
                item["engine"] = "http"
                needs_browser = item["word_count"] < 20 or (job["crawl_links"] and not links)
                if job["engine"] == "browser" or (job["engine"] == "auto" and needs_browser):
                    with _lock:
                        job["message"] = f"Trang cần JavaScript — đang render bằng Chromium ({len(seen)}/{max_pages})"
                        _save(job)
                    try:
                        from scrapling.fetchers import DynamicFetcher

                        browser_response = await asyncio.to_thread(
                            DynamicFetcher.fetch,
                            url,
                            headless=True,
                            timeout=job["timeout"] * 1000,
                            wait=1000,
                            disable_resources=False,
                            block_ads=True,
                        )
                        browser_item, browser_links = await asyncio.to_thread(
                            _extract, browser_response, url, job["keep_html"]
                        )
                        if browser_item["word_count"] >= item["word_count"]:
                            response, item, links = browser_response, browser_item, browser_links
                            item["engine"] = "browser"
                    except Exception as browser_error:
                        item["warning"] = f"Không render được JavaScript: {str(browser_error)[:300]}"
                links = [
                    link
                    for link in links
                    if _in_scope(link, origin, job["scope"]) and _url_matches_language(link, job["language"])
                ]
                item["links"] = links[:500]
                with _lock:
                    job["processed"] = len(seen)
                    job["bytes"] += len(response.body)
                    if job["language"] == "all" or item["language"] == job["language"]:
                        job["pages"].append(item)
                        job["successful"] += 1
                    else:
                        job["skipped_language"] += 1

                if job["crawl_links"]:
                    for link in links:
                        parsed = urlparse(link)
                        ignored = re.search(r"\.(?:pdf|zip|jpe?g|png|gif|svg|webp|mp4|mp3|css|js)(?:$|\?)", link, re.I)
                        if (
                            _in_scope(link, origin, job["scope"])
                            and _url_matches_language(link, job["language"])
                            and not ignored
                            and link not in seen
                            and link not in queue
                        ):
                            queue.append(link)
            except Exception as exc:
                with _lock:
                    job["processed"] = len(seen)
                    job["failed"] += 1
                    job["errors"].append({"url": url, "error": str(exc)[:500]})
            with _lock:
                _save(job)

        with _lock:
            job.update(
                status="completed",
                message=(
                    f"Hoàn tất {job['successful']} trang"
                    + (f", bỏ qua {job['skipped_language']} trang khác ngôn ngữ." if job["skipped_language"] else ".")
                ),
                current_url="",
                finished_at=_now(),
            )
            _save(job)
    except asyncio.CancelledError:
        with _lock:
            job.update(status="cancelled", message="Đã dừng.", finished_at=_now())
            _save(job)
        raise
    except Exception as exc:
        with _lock:
            job.update(status="failed", message=str(exc), finished_at=_now())
            _save(job)


async def homepage(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def list_jobs(_: Request) -> JSONResponse:
    with _lock:
        jobs = sorted((_public_job(job) for job in _jobs.values()), key=lambda item: item["created_at"], reverse=True)
    return JSONResponse(jobs)


async def create_job(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Dữ liệu JSON không hợp lệ."}, status_code=400)
    url = str(data.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return JSONResponse({"error": "URL phải bắt đầu bằng http:// hoặc https://"}, status_code=400)
    try:
        max_pages = min(5000, max(1, int(data.get("max_pages", 100))))
        timeout = min(120, max(5, int(data.get("timeout", 30))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Giới hạn trang hoặc timeout không hợp lệ."}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "status": "queued",
        "message": "Đang chờ…",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "current_url": "",
        "max_pages": max_pages,
        "timeout": timeout,
        "engine": data.get("engine") if data.get("engine") in {"auto", "http", "browser"} else "auto",
        "scope": data.get("scope") if data.get("scope") in {"path", "hostname", "domain"} else "path",
        "language": data.get("language") if data.get("language") in {"all", "en", "vi"} else "all",
        "use_sitemap": bool(data.get("use_sitemap", True)),
        "sitemap_urls": 0,
        "crawl_links": bool(data.get("crawl_links", True)),
        "keep_html": bool(data.get("keep_html", True)),
        "processed": 0,
        "successful": 0,
        "failed": 0,
        "skipped_language": 0,
        "discovered": 1,
        "bytes": 0,
        "errors": [],
        "pages": [],
        "cancel_requested": False,
    }
    with _lock:
        _jobs[job_id] = job
        _save(job)
    _tasks[job_id] = asyncio.create_task(_crawl(job_id))
    return JSONResponse(_public_job(job), status_code=201)


async def get_job(request: Request) -> JSONResponse:
    with _lock:
        job = _jobs.get(request.path_params["job_id"])
        if not job:
            return JSONResponse({"error": "Không tìm thấy job."}, status_code=404)
        return JSONResponse(_public_job(job, include_pages=True))


async def cancel_job(request: Request) -> JSONResponse:
    with _lock:
        job = _jobs.get(request.path_params["job_id"])
        if not job:
            return JSONResponse({"error": "Không tìm thấy job."}, status_code=404)
        if job["status"] in {"queued", "running"}:
            job["cancel_requested"] = True
            job["message"] = "Đang dừng sau request hiện tại…"
            _save(job)
        return JSONResponse(_public_job(job))


async def page_html(request: Request) -> Response:
    with _lock:
        job = _jobs.get(request.path_params["job_id"])
        index = int(request.path_params["index"])
        if not job or index < 0 or index >= len(job["pages"]):
            return Response("Không tìm thấy trang", status_code=404)
        page = job["pages"][index]
        html = page.get("html", "")
        base = f'<base href="{page["url"]}">'
        html = re.sub(r"(<head[^>]*>)", rf"\1{base}", html, count=1, flags=re.I)
        if base not in html:
            html = base + html
    return Response(html, media_type="text/html", headers={"Content-Security-Policy": "sandbox"})


async def export_job(request: Request) -> Response:
    fmt = request.path_params["fmt"]
    with _lock:
        job = _jobs.get(request.path_params["job_id"])
        if not job:
            return JSONResponse({"error": "Không tìm thấy job."}, status_code=404)
        pages = [{key: value for key, value in page.items() if key != "html"} for page in job["pages"]]
    if fmt == "json":
        body = orjson.dumps(pages, option=orjson.OPT_INDENT_2)
        media_type = "application/json"
    elif fmt == "csv":
        output = io.StringIO()
        fields = ["url", "status", "title", "description", "author", "published_at", "word_count", "content"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(pages)
        body = output.getvalue().encode("utf-8-sig")
        media_type = "text/csv"
    else:
        return JSONResponse({"error": "Định dạng không hỗ trợ."}, status_code=400)
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="scrapling-{job["id"]}.{fmt}"'},
    )


def create_app() -> Starlette:
    _load_jobs()
    return Starlette(
        debug=False,
        routes=[
            Route("/", homepage),
            Route("/api/jobs", list_jobs, methods=["GET"]),
            Route("/api/jobs", create_job, methods=["POST"]),
            Route("/api/jobs/{job_id}", get_job, methods=["GET"]),
            Route("/api/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
            Route("/api/jobs/{job_id}/pages/{index:int}/html", page_html, methods=["GET"]),
            Route("/api/jobs/{job_id}/export/{fmt}", export_job, methods=["GET"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
        ],
    )
