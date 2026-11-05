import base64

from conftest import CASES
from fastapi.testclient import TestClient

from autopilot.api import create_app


def test_dashboard_upload_review_and_approve(pilot):
    client = TestClient(create_app(pilot))
    assert client.get("/").status_code == 200

    with (CASES / "06_bakery_math_error.txt").open("rb") as f:
        r = client.post("/upload", files={"file": ("06_bakery_math_error.txt", f, "text/plain")},
                        follow_redirects=False)
    assert r.status_code == 303
    doc_url = r.headers["location"]
    page = client.get(doc_url)
    assert page.status_code == 200 and "total_mismatch" in page.text and "Approve and record" in page.text

    r = client.post(f"{doc_url}/approve", data={"reviewer": "carlos", "total": "3,180"}, follow_redirects=False)
    assert r.status_code == 303
    doc = client.get("/api" + doc_url).json()
    assert doc["status"] == "done" and doc["reviewed_by"] == "carlos" and doc["total"] == 3180
    assert client.post(f"{doc_url}/approve", data={}).status_code == 409


def test_webhook_for_no_code_tools(pilot):
    client = TestClient(create_app(pilot))
    content = base64.b64encode((CASES / "02_dairy_receipt.txt").read_bytes()).decode()
    r = client.post("/api/webhook/document", json={"filename": "02_dairy_receipt.txt", "content_base64": content})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert client.get("/api/metrics").json()["documents"] == 1


def test_rejects_bad_input(pilot):
    client = TestClient(create_app(pilot))
    r = client.post("/api/webhook/document", json={"filename": "evil.exe", "content_base64": "AAAA"})
    assert r.status_code == 400
    r = client.post("/api/webhook/document", json={"filename": "a.txt", "content_base64": "not base64!!"})
    assert r.status_code == 400
    assert client.get("/documents/nope").status_code == 404
