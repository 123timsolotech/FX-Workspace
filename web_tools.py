"""
web_tools.py
============
Browser and network-facing tools: opening URLs, opening a new tab in an
ALREADY-OPEN browser window, downloading files, and a lightweight
reachability check. No headless browser or new dependency is introduced:

  - open_url() hands the URL to the OS's registered default-browser
    handler (os.startfile on Windows) - the SAME mechanism as double-
    clicking a link. Most browsers are single-instance and reuse an
    already-running window for this as a new tab automatically; that is
    the OS/browser's own behaviour, not anything this function has to
    orchestrate.
  - new_browser_tab() is for the narrower case of "keep THIS window
    focused and add a tab to it" - it drives the already-open window with
    real keystrokes (Ctrl+T) through desktop_input, the same primitives
    main.py's keyboard_action/type_text tools already use.
"""
from __future__ import annotations

import os
import re
import time

import httpx

import desktop_input as di
import netlayer
import privacy

IS_WINDOWS = hasattr(os, "startfile")


def _safe_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


_OTHER_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:(?!\d)", re.I)    # "ms-settings:x", "file:", not "host:8080"


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url and not _OTHER_SCHEME_RE.match(url) and not re.match(r"^[a-z]:\\", url, re.I):
        url = "https://" + url
    return url


def is_web_url(url: str) -> bool:
    return (url or "").lower().startswith(("http://", "https://"))


def open_url(url: str) -> dict:
    target = _normalize_url(url)
    if not target:
        return {"ok": False, "message": "No URL given."}
    # open_url runs without approval, and os.startfile would happily launch a
    # file:// program or a custom-protocol handler. Only web pages here.
    if not is_web_url(target):
        return {"ok": False, "message": (f"Refused to open '{target}': open_url only opens http(s) web pages. "
                                         "Use open_application or open_file_in_os for local programs and files.")}
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    try:
        os.startfile(target)
        return {"ok": True, "message": (
            f"Opened '{target}' with the default browser. If a browser was already running, most "
            "browsers reuse that window and open this as a new tab automatically.")}
    except Exception as e:
        return {"ok": False, "message": f"Could not open '{target}': {e}"}


def new_browser_tab(window_title: str, url: str = "") -> dict:
    """Opens a new tab in an ALREADY-OPEN browser window via real
    keystrokes (Ctrl+T), then optionally types a URL and presses Enter.
    Use open_url instead when no particular browser window needs to stay
    the one focused."""
    target = _normalize_url(url)
    if target and not is_web_url(target):
        return {"ok": False, "message": (f"Refused to open '{target}': only http(s) web pages can be typed "
                                         "into a browser tab.")}
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    found = di.find_window(window_title)
    if not found:
        return {"ok": False, "message": (
            f"No open window matched '{window_title}'. Use open_url or open_application to start "
            "a browser first.")}
    hwnd, real_title = found
    if not di.focus_window(hwnd):
        return {"ok": False, "message": f"Found '{real_title}' but could not focus it."}
    time.sleep(0.15)
    di.send_key_combo("ctrl+t")
    time.sleep(0.25)
    if not target:
        return {"ok": True, "message": f"Opened a new blank tab in '{real_title}'."}
    di.send_unicode_text(target)
    time.sleep(0.05)
    di.send_key_combo("enter")
    return {"ok": True, "message": f"Opened a new tab in '{real_title}' and navigated to '{target}'."}


def download_file(url: str, filename: str, timeout: float = 30.0, max_bytes: int = 200 * 1024 * 1024,
                  should_cancel=None, total_timeout: float = 300.0) -> dict:
    """Stop (should_cancel) and the overall time limit are checked between
    chunks; the file is closed before returning, so a rollback of the partial
    download never races a writer."""
    target = _normalize_url(url)
    if not target:
        return {"ok": False, "message": "No URL given."}
    if not is_web_url(target):
        return {"ok": False, "message": f"Refused: '{target}' is not an http(s) URL."}
    should_cancel = should_cancel or (lambda: False)
    deadline = time.monotonic() + total_timeout
    dest = _safe_path(filename)
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        written = 0
        with _other_client(timeout) as client, client.stream("GET", target) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    if should_cancel():
                        return {"ok": False, "status": "cancelled",
                                "message": f"Download stopped by you after {written:,} bytes."}
                    if time.monotonic() > deadline:
                        return {"ok": False, "status": "permanent_failure",
                                "message": f"Download took longer than {total_timeout:.0f}s and was stopped."}
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        f.close()
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                        return {"ok": False, "message": (
                            f"Download exceeded the {max_bytes // (1024 * 1024)} MB limit; aborted "
                            "and removed the partial file.")}
                    f.write(chunk)
        return {"ok": True, "message": f"Downloaded {written:,} bytes from '{target}' to '{dest}'."}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"Download failed: {e}"}
    except Exception as e:
        return {"ok": False, "message": f"Download failed: {e}"}


def _other_client(timeout: float):
    """Downloads and reachability checks: the network layer's "other" route (netlayer.py)."""
    return netlayer.client("other", timeout=timeout, follow_redirects=True)


def get_url_status(url: str, timeout: float = 10.0) -> dict:
    target = _normalize_url(url)
    if not target:
        return {"ok": False, "message": "No URL given."}
    try:
        with _other_client(timeout) as client:
            resp = client.head(target)
            if resp.status_code >= 405:   # some servers reject HEAD outright
                with client.stream("GET", target) as got:
                    resp = got
        ok = resp.status_code < 400
        return {"ok": ok, "message": f"'{target}' responded {resp.status_code} {resp.reason_phrase} (final URL: {resp.url})."}
    except httpx.HTTPError as e:
        return {"ok": False, "message": f"'{target}' is not reachable: {e}"}


# ==============================================================================
# Web search and page reading (read-only; results are untrusted content)
# ==============================================================================
# No new dependency: httpx (already used for streaming) fetches, html.parser
# (stdlib) extracts the readable text. Everything a page says reaches the
# model framed as untrusted data (agent/loop.py), and the model is asked to
# cite the sources it used.
import base64
import html as _html
import ipaddress
import json
import math
import socket
import threading
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, quote, quote_plus, urlencode, urljoin, urlparse

import httpcore
import httpx

import netlayer

READ_WINDOW = 10_000            # characters of page text per web_read call
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024  # a PDF is read whole (its text is extracted, pdf_text.py)
MAX_REDIRECTS = 5
# A current desktop Chrome and the headers it sends for a page load. The old
# User-Agent ended in "AgenticWorkspace/1.0", and search engines and CDNs
# treat an unknown token there as a bot.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/150.0.0.0 Safari/537.36")
BROWSER_HEADERS = {"User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
                   "Upgrade-Insecure-Requests": "1"}
# The second try at a page that refused the first. Some sites (Wikipedia)
# refuse browser look-alikes from scripts and ask automated clients to say
# what they are, with an address.
HONEST_HEADERS = {**BROWSER_HEADERS, "User-Agent": (
    "AgenticWorkspace/1.0 (personal desktop assistant reading a page for its user; +http://127.0.0.1/)")}
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json", "text/xml",
              "application/xml")
# Injectable for tests: (method, url, **kw) -> httpx.Response
HTTP_CLIENT = None
# Injectable for tests: (url, extract=None, wait_for=None) -> {"url", "title", "html" | "value"}.
# Otherwise the assistant's browser (browser.AgentBrowser.load).
BROWSER_LOADER = None


class BlockedAddress(Exception):
    pass


def _public_addresses(host: str, port: int, resolve=None) -> list:
    """The host's addresses, or BlockedAddress if any is on this PC or the
    local network."""
    resolve = resolve or socket.getaddrinfo
    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedAddress(f"Could not resolve {host}: {e}")
    out = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise BlockedAddress(f"{host} is on this computer or the local network ({ip}); web tools only read public pages.")
        out.append(str(ip))
    if not out:
        raise BlockedAddress(f"Could not resolve {host}.")
    return out


_LOCAL_NAME_RE = re.compile(r"^(?:localhost|.+\.(?:localhost|local|lan|home|internal|intranet|corp|home\.arpa))$|^[^.]+$", re.I)


def _check_name(host: str):
    """The SSRF check without a DNS lookup - for a routed connection (proxy/Tor),
    where resolving the name here would leak it around the route: literal
    private/loopback addresses and names that only mean something on a local
    network are refused; the proxy resolves the rest."""
    h = host.strip("[]")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        if _LOCAL_NAME_RE.match(h):
            raise BlockedAddress(f"{host} is a local-network name; web tools only read public pages.")
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise BlockedAddress(f"{host} is on this computer or the local network ({ip}); web tools only read public pages.")


def _check_public(url: str, resolve=None) -> str:
    """http(s) only, and never this PC or the local network: a page (or an
    injected instruction) must not make the assistant read local services
    such as this app's own API or a router's admin page."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress(f"'{url}' is not an http(s) web address.")
    if resolve is None and netlayer.route_for("search").routed:
        _check_name(parts.hostname)                  # no local DNS lookup around the proxy
        return url
    _public_addresses(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), resolve)
    return url


class _PublicOnlyBackend(getattr(httpcore, "SyncBackend", object)):
    """Resolves the host itself at connect time and connects only to a
    checked public address. Checking a name and then letting the connection
    resolve it again would let a hostile DNS server answer "public" to the
    check and "127.0.0.1" to the connect (DNS rebinding). The URL, Host
    header and TLS name stay the original host."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = _public_addresses(host, port)[0]
        return super().connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                   socket_options=socket_options)


class _PublicOnlyTransport(httpx.HTTPTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        ssl_context = getattr(self._pool, "_ssl_context", None) or httpx.create_ssl_context()
        self._pool = httpcore.ConnectionPool(ssl_context=ssl_context, network_backend=_PublicOnlyBackend())


def _public_only_transport():
    """None where this httpcore has no pluggable network backend (then the
    name is checked before the request only - see ARCHITECTURE Limitations)."""
    if not hasattr(httpcore, "SyncBackend"):
        return None
    try:
        return _PublicOnlyTransport()
    except (AttributeError, TypeError):
        return None


def _client(public_only: bool = True, component: str = "search"):
    """Through the network layer (netlayer.py): its route (direct, a proxy or
    Tor - the proxy resolves the name), kill switch and log. On the direct
    route the connection itself checks the address it connects to."""
    return HTTP_CLIENT or netlayer.client(component, direct=_public_only_transport if public_only else None,
                                          timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False,
                                          headers=BROWSER_HEADERS)


def strip_tracking(url: str) -> str:
    """url without tracking parameters (utm_*, fbclid, gclid ... - sources._TRACKING) and fragment."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not sources_tracking(k)]
    return p._replace(query=urlencode(kept), fragment="").geturl()


def sources_tracking(key: str) -> bool:
    import sources
    return bool(sources._TRACKING.match(key))


def fetch(url: str, *, method: str = "GET", data=None, check=_check_public,
          headers: dict | None = None, component: str = "search") -> tuple[httpx.Response, bytes, str]:
    """(response, body, final_url) - redirects followed by hand so every hop is
    checked; at most MAX_PAGE_BYTES read. headers replace the browser ones.
    In Private search (netlayer.private_requests): no cookies, no Referer, a
    generic User-Agent and no tracking parameters."""
    private = netlayer.is_private()
    headers = netlayer.private_headers(headers or BROWSER_HEADERS) if private else headers
    client = _client(public_only=check is _check_public, component=component)
    try:
        current = strip_tracking(url) if private else url
        for _ in range(MAX_REDIRECTS + 1):
            check(current)
            with client.stream(method, current, data=data, headers=headers or BROWSER_HEADERS) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    current = strip_tracking(current) if private else current
                    method, data = ("GET", None) if resp.status_code == 303 or method == "POST" else (method, data)
                    continue
                cap = MAX_PDF_BYTES if "pdf" in (resp.headers.get("content-type") or "").lower() else MAX_PAGE_BYTES
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > cap:
                        body = body[:cap]
                        break
                return resp, body, current
        raise BlockedAddress(f"Too many redirects from {url}.")
    finally:
        if client is not HTTP_CLIENT:
            client.close()


def _decode(resp, body: bytes) -> str:
    charset = resp.charset_encoding if hasattr(resp, "charset_encoding") else None
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', body[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _Readable(HTMLParser):
    """HTML -> readable text: headings, paragraphs, lists and table rows on
    their own lines, links numbered [n] with their targets collected.
    only_main: just the page's <main> (or role=main) - readable() falls back
    to the whole page when that holds little text."""
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head", "nav", "footer", "aside", "form"}
    BLOCK = {"p", "div", "section", "article", "main", "br", "tr", "table", "pre", "blockquote", "ul", "ol", "dl", "dt",
             "dd", "figure", "figcaption", "hr", "header"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    # Page furniture marked by role or class: menus, dropdowns (Wikipedia's
    # 86-language list), navigation boxes, cookie banners, "[edit]" links.
    FURNITURE_ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "dialog", "complementary"}
    FURNITURE_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:dropdown|navbox|sidebar|breadcrumbs?|cookie|skip-link|"
                                    r"mw-editsection)(?:$|[\s_-])", re.I)
    MAX_LINKS = 60
    # Text a reader can't see is where prompt injections hide (2026-10-27): display:none, visibility:hidden, zero
    # size or opacity, clipped away, or pushed far off-screen. The model reads what the user would see.
    HIDDEN_STYLE_RE = re.compile(
        r"(?<![\w-])(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|opacity\s*:\s*0(?:\.0+)?\s*(?:;|!|$)"
        r"|font-size\s*:\s*0(?:\.0+)?(?:px|em|rem|pt|%)?\s*(?:;|!|$)|(?:max-)?(?:height|width)\s*:\s*0(?:px)?\s*(?:;|!|$)"
        r"|clip(?:-path)?\s*:\s*(?:rect\(\s*0|inset\(\s*(?:50|100)%)|(?:left|top|text-indent)\s*:\s*-\d{4,})", re.I)

    def __init__(self, base: str, only_main: bool = False):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.links, self.skip, self.title, self._in_title = base, [], [], 0, "", False
        self._href, self._link_text, self.description = None, [], ""
        self.only_main = only_main
        self._furniture = None                 # [tag, open count] of the furniture being left out
        self._main = None                      # [tag, open count] while inside the main content
        self._main_seen = False

    def _hidden(self) -> bool:
        return bool(self.skip or self._furniture or (self.only_main and self._main is None))

    def _nest(self, state, tag, step):
        if state is not None and state[0] == tag:
            state[1] += step
            if state[1] <= 0:
                return None
        return state

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or "").lower() == "description":
            self.description = (a.get("content") or "")[:300]
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag not in self.VOID:
            self._furniture = self._nest(self._furniture, tag, 1)
            self._main = self._nest(self._main, tag, 1)
            if self._furniture is None and not self.skip and (
                    (a.get("role") or "").lower() in self.FURNITURE_ROLES or "hidden" in a
                    or (a.get("aria-hidden") or "").lower() == "true"
                    or self.FURNITURE_CLASS_RE.search(a.get("class") or "")
                    or self.HIDDEN_STYLE_RE.search(a.get("style") or "")):
                self._furniture = [tag, 1]
            if self.only_main and self._main is None and not self._main_seen and (
                    tag == "main" or (a.get("role") or "").lower() == "main"):
                self._main, self._main_seen = [tag, 1], True
        if self._hidden():
            return
        if tag in self.BLOCK:
            self.out.append("\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a" and a.get("href") and not a["href"].startswith(("javascript:", "#", "mailto:")):
            self._href, self._link_text = urljoin(self.base, a["href"]), []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        hidden = self._hidden()
        if tag not in self.VOID:
            self._furniture = self._nest(self._furniture, tag, -1)
            self._main = self._nest(self._main, tag, -1)
        if hidden:
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in ("p", "tr", "pre", "blockquote"):
            self.out.append("\n")
        if tag == "a" and self._href:
            text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            if text and len(self.links) < self.MAX_LINKS:
                self.links.append((text[:100], self._href))
                self.out.append(f" [{len(self.links)}]")
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._hidden():
            return
        self.out.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        raw = "".join(self.out)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", l).strip() for l in raw.split("\n")]
        text = "\n".join(l for l in lines if l)
        return re.sub(r"\n{3,}", "\n\n", text)


def readable(html_text: str, base: str) -> dict:
    """The page's main content when it marks one (and it holds real text),
    otherwise the whole page - without the furniture either way."""
    def parse(only_main: bool) -> _Readable:
        parser = _Readable(base, only_main)
        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            pass
        return parser
    parser = parse(True) if re.search(r"<main[\s>]|role=[\"']main[\"']", html_text, re.I) else None
    if parser is None or len(parser.text()) < 500:
        parser = parse(False)
    return {"title": re.sub(r"\s+", " ", parser.title).strip(), "text": parser.text(), "links": parser.links,
            "description": parser.description, "date": published_date(html_text)}


# Where pages say when they were published or last changed: article metadata,
# schema.org JSON-LD, a <time datetime>, or a plain meta date.
_DATE_META_RE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:article:published_time|og:published_time|datePublished|"
    r"article:modified_time|og:updated_time|dateModified|date|dc\.date|DC\.date\.issued|pubdate|publish-date|"
    r"sailthru\.date|parsely-pub-date)[\"'][^>]*content=[\"']([^\"']+)|"
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name|itemprop)=[\"'](?:article:published_time|"
    r"datePublished|date|pubdate)[\"']|"
    r"\"datePublished\"\s*:\s*\"([^\"]+)\"|<time[^>]+datetime=[\"']([^\"']+)", re.I)


def published_date(html_text: str) -> str:
    """The page's own date as YYYY-MM-DD, or ''."""
    for m in _DATE_META_RE.finditer(html_text[:300_000]):
        raw = next((g for g in m.groups() if g), "")
        d = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if d and 1990 <= int(d.group(1)) <= 2100:
            return d.group(0)
    return ""


def _page_of(text: str, ctype: str, final: str) -> dict:
    if "html" in ctype or (not ctype and "<html" in text[:2000].lower()):
        return readable(text, final)
    return {"title": final.rsplit("/", 1)[-1], "text": text, "links": [], "description": "", "date": ""}


def _pdf_page(body: bytes, final: str) -> dict:
    """A PDF's text as a page (pdf_text.py), or {"error": why}."""
    import pdf_text
    try:
        got = pdf_text.extract(body)
    except pdf_text.PDFError as e:
        return {"error": f"{final} is a PDF that can't be read: {e}"}
    except Exception as e:                      # a malformed file must never break a run
        return {"error": f"{final} is a PDF whose text could not be extracted ({str(e)[:120]})"}
    if len(got["text"].strip()) < 40:
        return {"error": f"{final} is a PDF with no extractable text ({got['pages']} pages) - probably scanned images, "
                         "which need OCR."}
    name = urlparse(final).path.rsplit("/", 1)[-1] or final
    return {"title": got["title"] or name, "text": got["text"], "links": [], "description": f"PDF, {got['pages']} pages",
            "date": ""}


# A refusal dressed as a page: a bot check, a CAPTCHA or an "access denied"
# from the site's CDN. The title is checked on any page, the body only on a
# short one (a long article may well mention a CAPTCHA).
_REFUSAL_TITLE_RE = re.compile(r"just a moment|attention required|access denied|forbidden|are you a robot|robot check|"
                               r"security check|verify(?:ing)? you are (?:a )?human|captcha|too many requests|"
                               r"one more step|you(?:'ve| have) been blocked|prove your humanity|human verification", re.I)
_REFUSAL_BODY_RE = re.compile(r"enable javascript and cookies to continue|verify (?:that )?you(?:'re| are) "
                              r"(?:not a robot|a human|human)|checking (?:if the site connection is secure|your browser)|"
                              r"cf-chl|challenge-platform|captcha|unusual traffic|automated (?:queries|requests|access)|"
                              r"request unsuccessful|you(?:'ve| have) been blocked|access (?:to this page )?(?:has been )?"
                              r"denied|prove your humanity|complete the challenge below|blocked by network security", re.I)
# A redirect to a sign-in page: the page is for signed-in readers only (for
# example Reddit, 2026-09: logged-out readers go to /login).
_SIGN_IN_RE = re.compile(r"/(?:log-?in|sign-?in|accounts/login|auth/login|signup)(?:/|$|\?)", re.I)


def _refusal(status: int, headers, title: str, html_text: str, text: str) -> str:
    """Why this response is the site refusing an automated reader rather than
    the page, or ''."""
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return "a Cloudflare bot check"
    if status == 429:
        return "too many requests (429)"
    if status in (401, 403, 406):
        return f"access refused ({status})"
    if _REFUSAL_TITLE_RE.search(title or "") or (len(html_text) < 60_000 and _REFUSAL_BODY_RE.search(html_text)):
        if status >= 400 or status == 202 or len(text) < 1500:
            return "a bot check or CAPTCHA page"
    if status == 202 and len(text) < 200:
        return "an empty 'accepted' page (a bot check)"
    return ""


def _needs_script(html_text: str, text: str) -> bool:
    """The page builds its text with JavaScript: nothing readable arrives."""
    return len(text) < 200 and (len(html_text) > 5000 or bool(re.search(
        r"<noscript|enable javascript|requires javascript|javascript is (?:disabled|required)", html_text, re.I)))


def _browser_load(url: str, extract: str | None = None, wait_for: str | None = None) -> dict:
    if BROWSER_LOADER is not None:
        return BROWSER_LOADER(url, extract, wait_for)
    import browser
    return browser.current().load(url, extract=extract, wait_for=wait_for)


def _archived(url: str):
    """(response, body, snapshot page, date) of the Wayback Machine's newest
    copy of url, or None."""
    try:
        resp, body, _ = fetch("https://archive.org/wayback/available?url=" + quote(url, safe=""), headers=HONEST_HEADERS)
        snap = ((json.loads(body or b"{}").get("archived_snapshots") or {}).get("closest") or {})
        stamp = str(snap.get("timestamp") or "")
        if resp.status_code >= 400 or not snap.get("available") or not re.fullmatch(r"\d{8,14}", stamp):
            return None
        # id_ is the page as archived, without the archive's toolbar or rewritten links
        resp, body, _ = fetch(f"https://web.archive.org/web/{stamp}id_/{url}", headers=HONEST_HEADERS)
    except (BlockedAddress, httpx.HTTPError, ValueError):
        return None
    if resp.status_code >= 400:
        return None
    return resp, body, f"https://web.archive.org/web/{stamp}/{url}", f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _load_page(target: str, browser_fallback: bool, deadline: float) -> dict:
    """{"page", "final", "note"} for a readable page, else a failed tool result.
    Tries, in order: a normal browser-like request; the same with an honest
    User-Agent (after a refusal); the assistant's own browser (a real browser
    passes the checks sites put in front of scripts), when allowed; the
    Wayback Machine's newest copy. Stops early when little time is left."""
    host = urlparse(target).hostname or target
    reason = ""
    for headers in (BROWSER_HEADERS, HONEST_HEADERS):
        try:
            resp, body, final = fetch(target, headers=headers)
        except httpx.HTTPError as e:
            return {"ok": False, "retryable": True, "message": f"Could not read {target}: {e}"}
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if resp.status_code < 400 and (ctype == "application/pdf" or body[:5] == b"%PDF-"):
            page = _pdf_page(body, final)
            if "error" in page:
                return {"ok": False, "message": page["error"]}
            return {"page": page, "final": final, "note": ""}
        if resp.status_code < 400 and ctype and not ctype.startswith(TEXT_TYPES):
            return {"ok": False, "message": f"{final} is a {ctype} file ({len(body):,} bytes), not a page. "
                                            "Use download_file to save it."}
        text = _decode(resp, body)
        page = _page_of(text, ctype, final)
        reason = _refusal(resp.status_code, resp.headers, page["title"], text, page["text"])
        if not reason and _SIGN_IN_RE.search(urlparse(final).path) and not _SIGN_IN_RE.search(urlparse(target).path):
            reason = "sign-in"               # the same for any User-Agent
            break
        if not reason and resp.status_code >= 400:
            return {"ok": False, "retryable": resp.status_code >= 500, "message": f"{final} answered {resp.status_code}."}
        if not reason:
            if _needs_script(text, page["text"]):
                reason = "script"            # a second User-Agent won't change that
                break
            return {"page": page, "final": final, "note": ""}
    why = {"script": "builds its text with JavaScript, so a direct read gets an empty page",
           "sign-in": "sends readers who aren't signed in to a sign-in page"}.get(
        reason, f"refused automated reading ({reason})")
    tried = []
    if browser_fallback and time.monotonic() < deadline - 12:
        try:
            got = _browser_load(target)
            where = got.get("url") or target
            _check_public(where)
            html_text = got.get("html") or ""
            page = readable(html_text, where)
            signed_out = bool(_SIGN_IN_RE.search(urlparse(where).path)) and not _SIGN_IN_RE.search(urlparse(target).path)
            if len(page["text"]) >= 50 and not signed_out and not _refusal(200, {}, page["title"], html_text, page["text"]):
                return {"page": page, "final": where, "note": f"(Read through the assistant's browser: {host} {why}.)"}
            tried.append("the assistant's browser got a bot check or sign-in page too (the user can complete it "
                         "there: browser_open, then browser_read)")
        except BlockedAddress:
            raise
        except Exception as e:                 # BrowserError, a closed window, a lost connection
            tried.append(f"the assistant's browser could not load it ({str(e)[:160]})")
    if reason != "script" and time.monotonic() < deadline - 8:
        arch = _archived(target)
        if arch:
            resp, body, snapshot, when = arch
            text = _decode(resp, body)
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            page = _page_of(text, ctype, target)
            if len(page["text"]) >= 50:
                return {"page": page, "final": target, "note": (
                    f"(Archived copy from {when}, via the Wayback Machine: {host} {why}. The live page may have "
                    f"changed since - archived at {snapshot})")}
        tried.append("no archived copy was found")
    hint = ("Use another result, or open it with browser_open and read it with browser_read." if not browser_fallback
            else "Use another result from the search.")
    return {"ok": False, "status": "permanent_failure", "refused": True,
            "message": f"{host} {why}" + (f"; {'; '.join(tried)}" if tried else "") + f". {hint}"}


# Reading on (start > 0) reuses the page read a moment ago instead of fetching
# it - or opening the assistant's browser - again for every window.
PAGE_CACHE_TTL = 600
_page_cache: dict = {}


def _saved_page(url: str) -> dict | None:
    """A page from the saved search history (search_history.py), shaped like a read."""
    import search_history
    p = search_history.load()["pages"].get(url)
    if not p:
        return None
    return {"page": {"title": p.get("title") or url, "text": p.get("text") or "", "links": []}, "final": url,
            "note": "(A saved copy - Local Only doesn't go online.)"}


def web_read(url: str, start: int = 0, browser_fallback: bool = False) -> dict:
    """A public page's readable text, a window at a time, with its links.
    The result names the characters it did not show. browser_fallback: when
    the site refuses a direct request, read it in the assistant's browser."""
    target = _normalize_url(url)
    start = max(0, int(start or 0))
    cached = None if privacy.active() else _page_cache.get(target)     # a temporary chat bypasses the cache
    offline = netlayer.offline_reason()
    if offline:                                   # Local Only: a copy read before, or nothing
        got = cached[1] if cached else _saved_page(target)
        if not got:
            return {"ok": False, "status": "permanent_failure",
                    "message": f"Not read: {offline} This page wasn't read before, so there's no copy on this PC."}
    elif start and cached and time.monotonic() - cached[0] < PAGE_CACHE_TTL:
        got = cached[1]
    else:
        try:
            got = _load_page(target, browser_fallback, time.monotonic() + 60)
        except BlockedAddress as e:
            return {"ok": False, "message": str(e)}
        if "page" not in got:
            return got
        if not privacy.active():
            _page_cache[target] = (time.monotonic(), got)
        for key in [k for k, v in _page_cache.items() if time.monotonic() - v[0] > PAGE_CACHE_TTL]:
            _page_cache.pop(key, None)
    page, final = got["page"], got["final"]
    full = page["text"]
    window = full[start:start + READ_WINDOW]
    end = start + len(window)
    lines = [f"Page: {page['title'] or final}", f"Source: {final}"]
    if got["note"] and not start:
        lines.append(got["note"])
    if start:
        lines.append(f"(characters {start:,}-{end:,} of {len(full):,})")
    lines += ["", window]
    if end < len(full):
        lines.append(f"\n[NOT SHOWN: characters {end:,}-{len(full):,} of this page. Call web_read with start={end} to read on.]")
    if page["links"] and not start:
        lines.append("\nLinks on the page:")
        lines += [f"[{i}] {t} - {u}" for i, (t, u) in enumerate(page["links"], 1)]
    return {"ok": True, "message": "\n".join(lines),
            "data": {"url": final, "title": page["title"], "length": len(full), "start": start, "end": end}}


# ---- reading several pages for a question (fetch_page, Deep Research) -----------
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.'-]{1,}", re.I)
_STOPWORDS = set("""the a an and or of to in on for with by from at as is are was were be been this that these those it its
what which who whom how why when where does do did can could should would will about into than then there their
your you we our they them his her not no yes vs versus""".split())


def _terms(text: str) -> list:
    return [w for w in (m.group(0).lower().strip(".'-") for m in _WORD_RE.finditer(text or "")) if w and w not in _STOPWORDS]


def select_passages(text: str, query: str = "", budget: int = 6000) -> dict:
    """The parts of a page that answer the query, within budget characters, in
    page order: the opening (what the page is), then the passages that use the
    query's words the most (rarer words count more), each block marked where
    text was left out. Without a query, or when the page fits, the start.
    -> {"text", "shown", "total", "excerpt" (the best passage, for a citation card)}"""
    text = text or ""
    total = len(text)
    if total <= budget or not _terms(query):
        cut = text[:budget]
        return {"text": cut, "shown": len(cut), "total": total, "excerpt": _best_line(cut, query)}
    # Blocks: paragraphs and headings merged to ~300-900 characters.
    raw = [b.strip() for b in re.split(r"\n\s*\n|\n(?=#{1,6} )", text) if b.strip()]
    blocks, cur = [], ""
    for b in raw:
        if cur and (len(cur) + len(b) > 900 or b.startswith("#")):
            blocks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        blocks.append(cur)
    q = set(_terms(query))
    df = {t: sum(1 for b in blocks if t in b.lower()) for t in q}
    n = len(blocks)

    def score(b: str) -> float:
        words = _terms(b)
        if not words:
            return 0.0
        s = 0.0
        for t in q:
            tf = words.count(t) + (b.lower().count(t) if " " in t else 0)
            if tf:
                s += (1 + math.log(tf)) * math.log(1 + n / (1 + df[t]))
        heading = b.split("\n", 1)[0]
        if heading.startswith("#") and q & set(_terms(heading)):
            s *= 1.4
        return s / (1 + len(b) / 2500)
    keep = {0}
    used = len(blocks[0])
    for i in sorted(range(1, n), key=lambda i: score(blocks[i]), reverse=True):
        if score(blocks[i]) <= 0 or used >= budget:
            break
        if used + len(blocks[i]) <= budget * 1.05:
            keep.add(i)
            used += len(blocks[i])
    out, last = [], -1
    for i in sorted(keep):
        if last >= 0 and i != last + 1:
            out.append("[…]")
        out.append(blocks[i][:budget])
        last = i
    if last < n - 1:
        out.append("[…]")
    best = max(range(1, n), key=lambda i: score(blocks[i])) if n > 1 else 0
    body = "\n\n".join(out)
    return {"text": body, "shown": len(body), "total": total, "excerpt": _best_line(blocks[best], query)}


def _best_line(text: str, query: str) -> str:
    """One quotable sentence or two: the one that uses the query's words most."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if 40 <= len(s.strip()) <= 400
                 and not s.strip().startswith(("#", "|", "[…]"))]
    if not sentences:
        return re.sub(r"\s+", " ", (text or "")[:280]).strip()
    q = set(_terms(query))
    best = max(sentences, key=lambda s: len(q & set(_terms(s)))) if q else sentences[0]
    return best[:300]


def read_pages(urls: list, browser_fallback: bool = False, should_cancel=None, timeout: float = 45.0,
               workers: int = 4, on_done=None) -> list:
    """Several pages at once (a few at a time), each through the same path as
    web_read (refusal checks, browser and archive fallbacks, the page cache).
    Returns one entry per url, in order: {"url", "page", "final", "note"} or
    {"url", "error", "refused"?}. Stop ends the wait; pages not read by then
    say so. on_done(entry) is called as each page finishes. Never raises."""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    should_cancel = should_cancel or (lambda: False)
    deadline = time.monotonic() + timeout
    out: dict = {}

    temporary = privacy.active()                 # a temporary chat neither reads nor fills the page cache

    def one(url: str) -> dict:
        target = _normalize_url(url)
        cached = None if temporary else _page_cache.get(target)
        if cached and time.monotonic() - cached[0] < PAGE_CACHE_TTL:
            return {"url": url, **cached[1]}
        try:
            got = _load_page(target, browser_fallback, deadline)
        except BlockedAddress as e:
            return {"url": url, "error": str(e)}
        except Exception as e:                   # a broken page never fails the others
            return {"url": url, "error": f"Could not read {target}: {str(e)[:200]}"}
        if "page" not in got:
            return {"url": url, "error": got.get("message") or f"Could not read {target}.", "refused": got.get("refused")}
        if not temporary:
            _page_cache[target] = (time.monotonic(), got)
        return {"url": url, **got}

    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1)), thread_name_prefix="read-page")
    try:
        # privacy.wrap: the workers carry the turn's temporary-chat marker (and the network layer's component)
        pending = {pool.submit(privacy.wrap(one), u): u for u in urls}
        while pending:
            done, _ = wait(list(pending), timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                out[pending.pop(f)] = f.result()
                if on_done:
                    try:
                        on_done(f.result())
                    except Exception:
                        pass
            if should_cancel() or time.monotonic() > deadline:
                why = "stopped by you" if should_cancel() else f"no answer within {timeout:.0f} s"
                for f, u in pending.items():
                    f.cancel()
                    out[u] = {"url": u, "error": f"Not read: {why}.", "cancelled": should_cancel()}
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [out.get(u) or {"url": u, "error": "Not read."} for u in urls]


# ---- search -------------------------------------------------------------------
class _DuckResults(HTMLParser):
    """Results of html.duckduckgo.com: <a class="result__a" href=...>title</a>,
    <a class="result__snippet">...</a> and, after the result's address, the
    page's date as an ISO timestamp in a <span> (a keyless endpoint)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self._field, self._extras = [], None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls.split():
            self.results.append({"title": "", "url": _unwrap_ddg(a.get("href") or ""), "snippet": "", "date": ""})
            self._field = "title"
        elif "result__snippet" in cls.split() and self.results:
            self._field = "snippet"
        elif "result__extras__url" in cls.split():
            self._extras = True

    def handle_endtag(self, tag):
        if tag in ("a", "div", "td"):
            self._field = None
        if tag == "div":
            self._extras = False

    def handle_data(self, data):
        if self._field and self.results:
            self.results[-1][self._field] += data
        elif self._extras and self.results and not self.results[-1]["date"]:
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})T", data)
            if m:
                self.results[-1]["date"] = m.group(1)


def _unwrap_ddg(href: str) -> str:
    href = _html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def _unwrap_bing(href: str) -> str:
    """Bing's click-tracking link (bing.com/ck/a?...&u=a1<base64>) -> the result's own address."""
    parsed = urlparse(href or "")
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        u = (parse_qs(parsed.query).get("u") or [""])[0]
        if u.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
    return href or ""


DDG_URL = "https://html.duckduckgo.com/html/"
DDG_GAP = 3.0                  # seconds between two DuckDuckGo requests: it refuses bursts
DDG_PAUSE = (120.0, 1200.0)    # after a refusal: the first pause and the longest (it doubles each time)
SEARCH_CACHE_TTL = 15 * 60
_ddg_lock = threading.Lock()
_ddg = {"last": 0.0, "until": 0.0, "pause": 0.0}
_search_cache: dict = {}       # (backend, query) -> (time, results, source)

# The same search in the assistant's browser (when DuckDuckGo refuses direct
# requests): (name, address, element that means results arrived, a script in
# the page that returns them as [{title, url, snippet}]).
BROWSER_ENGINES = (
    ("DuckDuckGo", "https://duckduckgo.com/?q={q}&ia=web", 'article[data-testid="result"]',
     "[...document.querySelectorAll('article[data-testid=\"result\"]')].map(a => {"
     " const l = a.querySelector('a[data-testid=\"result-title-a\"]'); const s = a.querySelector('[data-result=\"snippet\"]');"
     " return l ? {title: l.innerText, url: l.href, snippet: s ? s.innerText : ''} : null; }).filter(Boolean)"),
    ("Bing", "https://www.bing.com/search?q={q}", "li.b_algo",
     "[...document.querySelectorAll('li.b_algo')].map(li => { const l = li.querySelector('h2 a');"
     " const s = li.querySelector('.b_caption p, p'); return l ? {title: l.innerText, url: l.href,"
     " snippet: s ? s.innerText : ''} : null; }).filter(Boolean)"),
)


def reset_search_state():
    """Forget the pacing, pauses and cached results and pages (tests)."""
    with _ddg_lock:
        _ddg.update(last=0.0, until=0.0, pause=0.0)
    _search_cache.clear()
    _page_cache.clear()


def _duckduckgo(query: str, df: str = "", kl: str = "") -> tuple[list, str]:
    """(results, why there are none). Paced: one request per DDG_GAP seconds
    at most, and none for a while after a refusal - asking again during a
    block only extends it. Its refusal is a 202 "anomaly" page, which used to
    read as "No results". df: d/w/m/y (how recent); kl: a region like us-en."""
    with _ddg_lock:
        now = time.monotonic()
        if now < _ddg["until"]:
            return [], f"DuckDuckGo refused automated searches a moment ago (paused for {int(_ddg['until'] - now)} s more)"
        if _ddg["last"] + DDG_GAP > now:
            time.sleep(_ddg["last"] + DDG_GAP - now)
        form = {"q": query, **({"df": df} if df else {}), **({"kl": kl} if kl else {})}
        try:
            resp, body, _ = fetch(DDG_URL, method="POST", data=form, headers={
                **BROWSER_HEADERS, "Origin": "https://html.duckduckgo.com", "Referer": "https://html.duckduckgo.com/"})
        finally:
            _ddg["last"] = time.monotonic()
        text = _decode(resp, body)
        parser = _DuckResults()
        parser.feed(text)
        if parser.results:
            _ddg["pause"] = 0.0
            return parser.results, ""
        if resp.status_code == 200 and re.search(r'class="no-results"|>\s*No\s+results\.?\s*<', text):
            return [], ""                                  # a real "nothing found"
        _ddg["pause"] = min(max(_ddg["pause"] * 2, DDG_PAUSE[0]), DDG_PAUSE[1])
        _ddg["until"] = time.monotonic() + _ddg["pause"]
        return [], (f"DuckDuckGo refused the search (it answered {resp.status_code} with a bot check, as it does "
                    "to bursts of automated searches)")


def _browser_search(query: str) -> tuple[list, str, str]:
    """(results, engine, why none) - the search run in the assistant's browser."""
    problems = []
    for name, url, wait_for, script in BROWSER_ENGINES:
        try:
            got = _browser_load(url.format(q=quote_plus(query)), extract=script, wait_for=wait_for)
        except Exception as e:                 # the browser itself: no point trying the next engine
            problems.append(f"the assistant's browser could not search ({str(e)[:160]})")
            break
        results = [{"title": str(r.get("title") or ""), "url": _unwrap_bing(str(r.get("url") or "")),
                    "snippet": str(r.get("snippet") or "")} for r in (got.get("value") or []) if isinstance(r, dict)]
        if results:
            return results, name, ""
        problems.append(f"{name} in the assistant's browser showed no results (or a check)")
    return [], "", "; ".join(problems)


def _clean_results(results: list) -> list:
    clean = []
    for r in results:
        url = r["url"].strip()
        if not is_web_url(url) or any(c["url"] == url for c in clean):
            continue
        clean.append({"title": re.sub(r"\s+", " ", r["title"]).strip()[:160] or url,
                      "url": url, "snippet": re.sub(r"\s+", " ", r["snippet"]).strip()[:300],
                      "date": str(r.get("date") or "")[:10]})
        if len(clean) >= 15:
            break
    return clean


def _search_result(query: str, clean: list, source: str, limit: int) -> dict:
    shown = clean[:limit]
    if not shown:
        return {"ok": True, "message": f"No results for '{query}' ({source}).", "data": {"results": []}}
    lines = [f"Web results for '{query}' ({source}). Read a page with web_read before relying on it, and cite "
             "the pages you used as [title](url) links in your answer:"]
    for i, r in enumerate(shown, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}" + (f"\n   {r['snippet']}" if r["snippet"] else ""))
    return {"ok": True, "message": "\n".join(lines), "data": {"results": shown, "source": source}}


def web_search(query: str, limit: int = 8, provider: str = "duckduckgo", searxng_url: str = "",
               browser_fallback: bool = False) -> dict:
    """Web search without an API key: DuckDuckGo's HTML endpoint (paced, and
    results kept for SEARCH_CACHE_TTL), then - when DuckDuckGo refuses and
    browser_fallback allows it - the same search in the assistant's browser.
    Or a SearXNG instance the user configured (Settings > Agent)."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "message": "No search query given."}
    limit = max(1, min(int(limit or 8), 15))
    searxng = provider == "searxng" and bool(searxng_url)
    key = ("searxng" if searxng else "web", query.lower())
    hit = None if privacy.active() else _search_cache.get(key)          # a temporary chat bypasses the cache
    if hit and time.monotonic() - hit[0] < SEARCH_CACHE_TTL:
        return _search_result(query, hit[1], hit[2] + ", from a search a few minutes ago", limit)
    why = ""
    try:
        if searxng:
            base = searxng_url.rstrip("/")
            resp, body, _final = fetch(f"{base}/search?q={quote_plus(query)}&format=json",
                                       check=lambda u: u)          # the user's own instance may be local
            if resp.status_code >= 400:
                why = (f"SearXNG answered {resp.status_code}" + (" - allow the json format in its settings.yml "
                       "(search: formats: [html, json])" if resp.status_code == 403 else ""))
            data = httpx.Response(200, content=body).json() if resp.status_code < 400 else {}
            results = [{"title": r.get("title") or "", "url": r.get("url") or "", "snippet": r.get("content") or ""}
                       for r in (data.get("results") or [])]
            source = "SearXNG"
        else:
            try:
                results, why = _duckduckgo(query)
            except httpx.HTTPError as e:
                results, why = [], f"DuckDuckGo could not be reached ({e})"
            source = "DuckDuckGo"
            if why and browser_fallback:
                found, engine, trouble = _browser_search(query)
                if found:
                    results, source, why = found, f"{engine}, in the assistant's browser", ""
                else:
                    why += "; " + trouble
    except BlockedAddress as e:
        return {"ok": False, "message": str(e)}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "retryable": True, "message": f"Web search failed: {e}"}
    clean = _clean_results(results)
    if not clean and why:
        hint = ("" if searxng else " It usually lifts within minutes. Meanwhile read a page you know with web_read"
                + ("" if browser_fallback else ", or search in the assistant's browser (browser_open "
                   f"https://duckduckgo.com/?q={quote_plus(query)} then browser_read)")
                + ", or answer from what you know and say it is unverified.")
        return {"ok": False, "status": "permanent_failure", "refused": True,
                "message": f"No search results: {why}.{hint}"}
    if clean and not privacy.active():
        _search_cache[key] = (time.monotonic(), clean, source)
        for k in [k for k, v in _search_cache.items() if time.monotonic() - v[0] > SEARCH_CACHE_TTL]:
            _search_cache.pop(k, None)
    return _search_result(query, clean, source, limit)
