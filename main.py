import threading
import time
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
import crawler
import worker

app = FastAPI(title="Website Archive Submitter")


class DomainIn(BaseModel):
    domain: str
    max_pages: int = 200


@app.on_event("startup")
def startup():
    db.init_db()
    worker.start_worker()


def _run_discovery(domain_id: int, domain: str, max_pages: int, is_rescan: bool):
    db.set_domain_status(domain_id, "discovering")
    new_count = 0
    total_count = 0
    try:
        for item in crawler.discover_all(domain, max_pages=max_pages):
            total_count += 1
            is_new = db.insert_url_if_new(
                domain_id, item["original_url"], item["normalized_url"],
                item["source"], item["http_status"],
            )
            if is_new:
                new_count += 1
                with db.get_conn() as conn:
                    row = conn.execute(
                        "SELECT id FROM urls WHERE domain_id=? AND normalized_url=?",
                        (domain_id, item["normalized_url"]),
                    ).fetchone()
                if row:
                    db.queue_submission(row["id"], service="wayback")
    finally:
        db.set_domain_status(domain_id, "queued" if new_count else "done", touch_scan=True)
    return {"total_discovered": total_count, "newly_found": new_count}


@app.post("/api/domains")
def add_domain(payload: DomainIn):
    domain = payload.domain.strip()
    if not domain:
        raise HTTPException(400, "domain is required")
    domain_id = db.get_or_create_domain(domain)
    t = threading.Thread(target=_run_discovery, args=(domain_id, domain, payload.max_pages, False), daemon=True)
    t.start()
    return {"domain_id": domain_id, "status": "discovery_started"}


@app.post("/api/domains/{domain_id}/rescan")
def rescan_domain(domain_id: int, max_pages: int = 200):
    with db.get_conn() as conn:
        row = conn.execute("SELECT domain FROM domains WHERE id=?", (domain_id,)).fetchone()
    if not row:
        raise HTTPException(404, "domain not found")
    t = threading.Thread(target=_run_discovery, args=(domain_id, row["domain"], max_pages, True), daemon=True)
    t.start()
    return {"status": "rescan_started"}


@app.get("/api/domains")
def domains():
    return db.list_domains()


@app.get("/api/domains/{domain_id}/urls")
def domain_urls(domain_id: int):
    return db.list_urls_for_domain(domain_id)


@app.get("/api/stats")
def stats():
    return db.get_stats()


@app.get("/api/search")
def search(q: str = "", service: str = None, status: str = None):
    return db.search_repository(q, service, status)


@app.get("/api/urls/{url_id}/history")
def url_history(url_id: int):
    return db.submission_history(url_id)


@app.post("/api/urls/{url_id}/rearchive")
def rearchive(url_id: int, service: str = "wayback"):
    db.force_requeue(url_id, service)
    return {"status": "requeued"}


@app.post("/api/worker/pause")
def worker_pause():
    worker.pause()
    return {"status": "paused"}


@app.post("/api/worker/resume")
def worker_resume():
    worker.resume()
    return {"status": "resumed"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def dashboard():
    return FileResponse("static/index.html")
