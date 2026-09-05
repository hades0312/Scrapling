from unittest.mock import MagicMock
from urllib.parse import urlparse

from starlette.testclient import TestClient

from scrapling.dashboard import app as dashboard


def _client(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(dashboard, "JOBS_DIR", jobs_dir)
    dashboard._jobs.clear()
    dashboard._tasks.clear()
    return TestClient(dashboard.create_app())


def test_homepage_and_empty_history(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Scrapling Studio" in response.text
        assert client.get("/api/jobs").json() == []


def test_create_job_validates_url(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/jobs", json={"url": "file:///etc/passwd"})
        assert response.status_code == 400
        assert "http://" in response.json()["error"]


def test_domain_scope_includes_subdomains():
    origin = urlparse("https://docs.example.co.uk/start")
    assert dashboard._in_scope("https://news.example.co.uk/article", origin, "domain")
    assert not dashboard._in_scope("https://news.example.net/article", origin, "domain")
    assert not dashboard._in_scope("https://news.example.co.uk/article", origin, "hostname")


def test_path_scope_stays_inside_pasted_url_tree():
    origin = urlparse("https://www.ibm.com/docs/en/mam/7.6.1")
    assert dashboard._in_scope("https://www.ibm.com/docs/en/mam/7.6.1", origin, "path")
    assert dashboard._in_scope("https://www.ibm.com/docs/en/mam/7.6.1/topic/install", origin, "path")
    assert dashboard._in_scope("https://www.ibm.com/docs/en/mam/7.6.1?topic=install", origin, "path")
    assert not dashboard._in_scope("https://www.ibm.com/in-en/products", origin, "path")
    assert not dashboard._in_scope("https://support.ibm.com/docs/en/mam/7.6.1", origin, "path")


def test_sitemap_locations_support_gzip():
    import gzip

    content = b"<urlset><url><loc>https://example.test/a&amp;b</loc></url></urlset>"
    assert dashboard._sitemap_locations(gzip.compress(content)) == ["https://example.test/a&b"]


def test_language_detection_prefers_declared_html_language():
    response = MagicMock()
    selector_result = MagicMock()
    selector_result.get.side_effect = ["vi-VN"]
    response.css.return_value = selector_result
    assert dashboard._detect_language(response, "https://example.test/post", "Hello world") == "vi"


def test_url_language_filter_rejects_other_locales():
    assert dashboard._url_matches_language("https://example.test/us-en/articles/one", "en")
    assert dashboard._url_matches_language("https://example.test/docs/vi/guide", "vi")
    assert not dashboard._url_matches_language("https://example.test/qa-ar/articles/one", "en")
    assert not dashboard._url_matches_language("https://example.test/in-hi/articles/one", "en")
    assert dashboard._url_matches_language("https://example.test/in-en/articles/one", "en")
    assert not dashboard._url_matches_language("https://example.test/en/articles/one", "vi")
    assert dashboard._url_matches_language("https://example.test/articles/without-locale", "en")


def test_create_job_and_export(tmp_path, monkeypatch):
    response = MagicMock()
    response.status = 200
    response.body = b"<html><title>Demo</title><body><h1>Hello</h1></body></html>"
    response.html_content = response.body.decode()
    response.urljoin.side_effect = lambda value: value
    response.get_all_text.return_value = "Hello dashboard"
    selector_result = MagicMock()
    selector_result.__bool__.return_value = False
    selector_result.get.return_value = None
    selector_result.getall.return_value = []
    response.css.return_value = selector_result
    monkeypatch.setattr(dashboard.Fetcher, "get", lambda *args, **kwargs: response)

    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/jobs",
            json={"url": "https://example.test", "max_pages": 1, "crawl_links": False, "engine": "http", "use_sitemap": False, "language": "all"},
        )
        assert created.status_code == 201
        job_id = created.json()["id"]

        # TestClient executes the background task on its event loop.
        for _ in range(20):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "completed":
                break
        assert job["status"] == "completed"
        assert job["page_count"] == 1
        assert client.get(f"/api/jobs/{job_id}/export/json").status_code == 200
        csv_response = client.get(f"/api/jobs/{job_id}/export/csv")
        assert csv_response.status_code == 200
        assert "Hello dashboard" in csv_response.text
