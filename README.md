# Website Archive Submitter & Automated Backup Repository

A Python tool that discovers a domain's public URLs and automatically
submits them to the Internet Archive's Wayback Machine, keeping a
searchable, crash-safe record of everything it finds and archives.

## 1. Setup (2 minutes)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://localhost:8000** — that's the dashboard.

No API keys, no external DB server — it's plain SQLite
(`archive_repository.db`, created automatically on first run).

## 2. How it works

```
 Add domain
     │
     ▼
 crawler.py  ──►  robots.txt → sitemap.xml (recursive) → bounded HTML crawl
     │              (internal links + <link rel=canonical>)
     ▼
 db.py       ──►  normalize + dedupe → insert only NEW urls (incremental backup)
     │
     ▼
 submissions queue (SQLite, status: pending/processing/success/failed)
     │
     ▼
 worker.py   ──►  background thread, 1 submission every 2s, retries failures
     │              up to 3x, survives restarts (resets 'processing'→'pending'
     │              on boot — see db.init_db)
     ▼
 archiver.py ──►  Wayback Machine "Save Page Now" (web.archive.org/save/<url>)
```

Discovery and submission both run in **background threads**, so the
dashboard stays responsive and the queue keeps draining even while
you're browsing.

## 3. Architecture notes (for the writeup)

- **Database**: SQLite, 3 tables — `domains`, `urls`, `submissions`.
  One `urls` row per unique normalized URL per domain (`UNIQUE(domain_id,
  normalized_url)` enforces the incremental-backup rule for free).
  `submissions` is 1-to-many off `urls`, so re-archiving a URL just adds
  a new row and preserves full history.
- **Resumability**: nothing is held only in memory. If the server
  crashes mid-queue, restart it — `init_db()` resets any submission
  stuck in `processing` back to `pending`, and the worker picks up
  exactly where it left off.
- **Incremental rescans**: hitting "Rescan" re-runs discovery; because
  of the unique constraint, only genuinely new URLs get inserted and
  queued — old ones are left untouched (matches the assignment's
  "5,000 → +350 new" example).
- **Archive.today**: deliberately not implemented. It has no stable
  published API and its submission form is behind a CAPTCHA/JS
  challenge — automating that would violate the assignment's own rule
  against bypassing CAPTCHAs. `archiver.py` documents this and stubs
  the service so the schema/queue already support adding it later.
- **Multi-domain**: the schema and API are domain-scoped from the
  start (`domain_id` foreign key everywhere) — adding a second domain
  just works, each with its own queue and stats, and there's also a
  combined view via `/api/stats` and `/api/search`.

## 4. Demo script (matches the assignment's "Mandatory Demonstration")

1. Paste a domain (e.g. `example.com`) into the input, hit **Start
   Discovery**. Watch the domain's status go `discovering → queued →
   done` in real time.
2. Click the domain row — see the discovered URL inventory with
   source (sitemap/html_link/canonical) and live submission status.
3. Watch the queue drain — badges flip `pending → processing →
   success` as the worker submits to Wayback, with the real archive
   link appearing.
4. Click **Pause Queue** mid-submission, then **Resume Queue** — or
   just kill `uvicorn` (Ctrl+C) and restart it — to demonstrate crash
   recovery: nothing is lost, the queue continues from where it
   stopped.
5. Click **Rescan** on the same domain — only newly-found URLs get
   added to the queue; already-archived ones are untouched (open the
   URL table to show timestamps proving it).
6. Add a second domain to show multi-domain support — independent
   per-domain stats, one combined dashboard.
7. Use the **Search Repository** box to look up a URL or domain and
   confirm its archive status/link.

## 5. Known scope cuts (documented, not accidental)

These are explicitly bonus-tier in the assignment brief and are not
built tonight — noted here so it's clear they were a deliberate
time/scope call, not missed requirements:

- Archive.today integration (see above)
- JavaScript-rendered page capture via Playwright/Selenium
- Distributed/multi-worker processing, priority queues
- CSV/JSON export, scheduled recurring scans
- 50+ domain stress testing

## 6. File map

```
main.py        FastAPI app + routes
db.py          SQLite schema + all queries
crawler.py     URL discovery (robots/sitemap/HTML crawl + normalization)
archiver.py    Wayback Machine submission
worker.py      Background queue processor
static/index.html   Dashboard (vanilla JS, polls the API every 2.5s)
```
