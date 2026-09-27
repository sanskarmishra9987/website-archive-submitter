"""
URL discovery engine.
Combines: robots.txt sitemap hints, sitemap.xml / sitemap indexes, and a
bounded internal HTML crawl (links + canonical tags). Stays within the
target domain.
"""
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag
import xml.etree.ElementTree as ET

HEADERS = {"User-Agent": "WebsiteArchiveSubmitter/1.0 (+educational project; respects robots.txt)"}
TIMEOUT = 10


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop fragment, drop trailing slash (except root)."""
    url, _frag = urldefrag(url)
    parts = urlparse(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    # drop default ports
    netloc = netloc.replace(":80", "").replace(":443", "")
    return urlunparse((scheme, netloc, path, "", parts.query, ""))


def same_domain(url: str, root_domain: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    root = root_domain.lower().replace("www.", "")
    return host == root or host == "www." + root or host.endswith("." + root)


def _get(client, url):
    try:
        r = client.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        return r
    except Exception:
        return None


def discover_from_robots(client, base_url):
    """Return list of sitemap URLs declared in robots.txt."""
    sitemaps = []
    r = _get(client, urljoin(base_url, "/robots.txt"))
    if r and r.status_code == 200:
        for line in r.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
    return sitemaps


def discover_from_sitemap(client, sitemap_url, root_domain, depth=0, max_depth=3, seen=None):
    """Recursively parse sitemap / sitemap-index XML. Returns list of (url, source)."""
    if seen is None:
        seen = set()
    if sitemap_url in seen or depth > max_depth:
        return []
    seen.add(sitemap_url)

    r = _get(client, sitemap_url)
    if not r or r.status_code != 200:
        return []
    found = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []

    tag = root.tag.lower()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    if tag.endswith("sitemapindex"):
        for sm in root.findall("sm:sitemap/sm:loc", ns) or root.findall(".//{*}loc"):
            loc = sm.text.strip() if sm.text else None
            if loc:
                found += discover_from_sitemap(client, loc, root_domain, depth + 1, max_depth, seen)
    elif tag.endswith("urlset"):
        for u in root.findall("sm:url/sm:loc", ns) or root.findall(".//{*}loc"):
            loc = u.text.strip() if u.text else None
            if loc and same_domain(loc, root_domain):
                found.append((loc, "sitemap"))
    return found


def discover_from_html_crawl(client, start_url, root_domain, max_pages=200):
    """Bounded breadth-first crawl following internal <a href> and <link rel=canonical>."""
    found = []
    visited = set()
    queue = [start_url]

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        norm = normalize_url(url)
        if norm in visited:
            continue
        visited.add(norm)

        r = _get(client, url)
        if not r or "text/html" not in r.headers.get("content-type", ""):
            continue
        found.append((url, "html_link", r.status_code))

        soup = BeautifulSoup(r.text, "lxml")

        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            c_url = urljoin(url, canonical["href"])
            if same_domain(c_url, root_domain):
                found.append((c_url, "canonical", None))

        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"])
            if link.startswith(("mailto:", "tel:", "javascript:")):
                continue
            if same_domain(link, root_domain):
                n = normalize_url(link)
                if n not in visited:
                    queue.append(link)

    return found


def discover_all(domain: str, max_pages: int = 200):
    """
    Main entry point. `domain` can be given as 'example.com' or a full URL.
    Yields dicts: {original_url, normalized_url, source, http_status}
    """
    if not domain.startswith(("http://", "https://")):
        base_url = "https://" + domain
    else:
        base_url = domain
    root_domain = urlparse(base_url).netloc.replace("www.", "")

    results = {}  # normalized_url -> (original_url, source, http_status)

    with httpx.Client() as client:
        # 1. robots.txt -> sitemap hints
        sitemap_urls = discover_from_robots(client, base_url)
        if not sitemap_urls:
            sitemap_urls = [urljoin(base_url, "/sitemap.xml")]  # common default

        for sm in sitemap_urls:
            for loc, source in discover_from_sitemap(client, sm, root_domain):
                n = normalize_url(loc)
                results.setdefault(n, (loc, source, None))

        # 2. bounded HTML crawl from homepage (covers sites with no/partial sitemap)
        for item in discover_from_html_crawl(client, base_url, root_domain, max_pages=max_pages):
            url, source, *rest = item
            status = rest[0] if rest else None
            n = normalize_url(url)
            if n not in results:
                results[n] = (url, source, status)

    for norm, (original, source, status) in results.items():
        yield {
            "original_url": original,
            "normalized_url": norm,
            "source": source,
            "http_status": status,
        }
