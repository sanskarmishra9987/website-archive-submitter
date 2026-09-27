"""
Background submission queue worker.
Runs in a daemon thread, pulls one pending submission at a time from
SQLite, submits it, records the result. Because state lives in the DB
(not in memory), an interrupted run just resumes on restart - anything
left 'processing' is reset to 'pending' at startup (see db.init_db).
"""
import threading
import time
import db
import archiver

POLL_INTERVAL = 1.0        # seconds between checks when queue is empty
SUBMIT_PACING = 2.0        # seconds between submissions (be polite to the archive service)
MAX_ATTEMPTS = 3

_stats_lock = threading.Lock()
_running = {"active": True, "paused": False}


def worker_loop():
    while _running["active"]:
        if _running["paused"]:
            time.sleep(POLL_INTERVAL)
            continue

        job = db.next_pending_submission()
        if not job:
            # nothing pending right now - also retry stale failures
            db.retry_failed(max_attempts=MAX_ATTEMPTS)
            time.sleep(POLL_INTERVAL)
            continue

        print(f"[worker] submitting to {job['service']}: {job['normalized_url']}")
        success, archive_url, archive_id, error = archiver.submit(job["service"], job["normalized_url"])
        if success:
            print(f"[worker] SUCCESS -> {archive_url}")
        else:
            print(f"[worker] FAILED -> {error}")
        db.mark_submission_result(
            job["sub_id"], success, archive_url=archive_url, archive_identifier=archive_id, error=error
        )
        time.sleep(SUBMIT_PACING)


_thread = None


def start_worker():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=worker_loop, daemon=True)
        _thread.start()


def pause():
    _running["paused"] = True


def resume():
    _running["paused"] = False


def stop():
    _running["active"] = False
