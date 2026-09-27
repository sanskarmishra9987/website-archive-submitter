"""
Archive service integrations.

Wayback Machine: uses the public, documented "Save Page Now" endpoint
(https://web.archive.org/save/<url>). No API key required for basic use.
This is Internet Archive's own published mechanism for triggering an
on-demand capture - no CAPTCHA bypass, no auth bypass.

Archive.today: intentionally NOT implemented. It has no stable published
API, and its own web form is protected by a CAPTCHA/JS challenge -
automating around that would violate the assignment's own "do not bypass
CAPTCHAs" rule. Documented here as an explicit scope decision, not an
oversight - a real integration would need their (currently unofficial,
frequently-changing) endpoints and is listed as a bonus follow-up.
"""
import httpx
import time

WAYBACK_SAVE_URL = "https://web.archive.org/save/{url}"
HEADERS = {"User-Agent": "WebsiteArchiveSubmitter/1.0 (+educational project)"}


def submit_to_wayback(url: str, timeout: int = 30):
    """
    Triggers a fresh capture via Wayback's Save Page Now.
    Returns (success: bool, archive_url: str|None, archive_id: str|None, error: str|None)
    """
    target = WAYBACK_SAVE_URL.format(url=url)
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(target, headers=HEADERS)
            # Successful capture: IA redirects to /web/<timestamp>/<url>
            final_url = str(resp.url)
            if "/web/" in final_url and resp.status_code == 200:
                # archive identifier = the timestamp segment
                try:
                    archive_id = final_url.split("/web/")[1].split("/")[0]
                except IndexError:
                    archive_id = None
                return True, final_url, archive_id, None
            elif resp.status_code == 429:
                return False, None, None, "rate_limited (429) - will retry with backoff"
            else:
                return False, None, None, f"unexpected_response status={resp.status_code}"
    except httpx.TimeoutException:
        return False, None, None, "timeout"
    except Exception as e:
        return False, None, None, f"error: {e}"


def submit(service: str, url: str):
    if service == "wayback":
        return submit_to_wayback(url)
    if service == "archive_today":
        return False, None, None, "archive_today integration not implemented (no safe public API - see archiver.py docstring)"
    return False, None, None, f"unknown service: {service}"
