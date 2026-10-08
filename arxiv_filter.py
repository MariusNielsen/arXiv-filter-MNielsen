#!/usr/bin/env python3
"""Daily arXiv / Hexagon filter.

Modes:
  python arxiv_filter.py                  daily run: fetch, match, write docs/
  python arxiv_filter.py --backtest 90    classify the last N days -> docs/backtest.html
  python arxiv_filter.py --refresh-citers suggest authors citing my_papers -> citers_suggested.yaml
  python arxiv_filter.py --fixtures DIR   daily run on saved XML instead of the network (testing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
STATE = ROOT / "state"
UA = "arxiv-filter-MNielsen/1.0 (+https://github.com/MariusNielsen/arXiv-filter-MNielsen)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
    "mathlake": "https://mathlake.org/schemas/atom",
}

# --------------------------------------------------------------------------- text


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


TEX_ACCENT = re.compile(r"\\[`'\"^~=.uvHck]\s*\{?\s*([A-Za-z])\s*\}?")
TEX_CMD = re.compile(r"\\[A-Za-z]+")


def norm_text(s: str) -> str:
    s = TEX_ACCENT.sub(r"\1", s)
    s = s.replace("\\infty", "infinity").replace("∞", "infinity")
    s = TEX_CMD.sub("", s)
    s = re.sub(r"[{}$\\]", "", s)
    s = strip_accents(s).lower()
    s = re.sub(r"\s*(--|—|–|‐|‑)\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_name(s: str) -> str:
    s = strip_accents(TEX_ACCENT.sub(r"\1", s)).lower()
    s = s.replace("’", "'").replace("'", "").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- data


@dataclass
class Paper:
    pid: str                 # arXiv id without version, or "hexagon:..."
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    link: str
    pdf: str
    source: str              # "arxiv" | "hexagon"
    announce: str = "new"    # new | cross | replace | replace-cross | author-search
    date: str = ""
    ai_mode: str = ""        # hexagon authorshipMode
    other_links: dict = field(default_factory=dict)
    # filled by classify()
    tier: int = 9
    reasons: list[str] = field(default_factory=list)
    watched: set = field(default_factory=set)

    def title_key(self) -> str:
        return re.sub(r"[^a-z0-9]", "", norm_text(self.title))[:80]

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        d["watched"] = sorted(self.watched)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Paper":
        d = dict(d)
        d["watched"] = set(d.get("watched", []))
        return cls(**d)


# --------------------------------------------------------------------------- config


def load_yaml(name: str):
    with open(ROOT / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Authors:
    """Full-name matching: (first given name, last surname word), plus explicit variants.

    A listed name whose first token is a single letter ("J. D. Quigley") matches on initial.
    """

    def __init__(self, cfg: dict):
        self.tier: dict[str, str] = {}        # display name -> always/watch
        self.keys: dict[tuple, str] = {}      # (first, last) -> display name
        self.initial_keys: dict[tuple, str] = {}
        for tier in ("always", "watch"):
            for entry in cfg.get(tier) or []:
                if isinstance(entry, str):
                    name, variants = entry, []
                else:
                    if entry.get("disabled"):
                        continue
                    name, variants = entry["name"], entry.get("variants") or []
                self.tier[name] = tier
                for v in [name, *variants]:
                    toks = norm_name(v).split()
                    if len(toks) < 2:
                        continue
                    if len(toks[0]) == 1:
                        self.initial_keys[(toks[0], toks[-1])] = name
                    else:
                        self.keys[(toks[0], toks[-1])] = name

    def match(self, author: str) -> str | None:
        toks = norm_name(author).split()
        if len(toks) < 2:
            return None
        hit = self.keys.get((toks[0], toks[-1]))
        if hit:
            return hit
        return self.initial_keys.get((toks[0][0], toks[-1]))

    def all_names(self) -> list[str]:
        return list(self.tier)


class Keywords:
    def __init__(self, cfg: dict):
        self.strong = [(p, re.compile(p, re.I)) for p in cfg.get("strong") or []]
        self.title_only = [(p, re.compile(p, re.I)) for p in cfg.get("title_only") or []]

    def hits(self, title: str, abstract: str):
        """Return matched text (not the pattern), deduplicated, for display."""
        t, a = norm_text(title), norm_text(abstract)
        in_title, in_abs = [], []
        for _, rx in self.strong:
            m = rx.search(t)
            if m:
                in_title.append(m.group(0).strip())
            else:
                m = rx.search(a)
                if m:
                    in_abs.append(m.group(0).strip())
        title_only = [m.group(0).strip() for _, rx in self.title_only if (m := rx.search(t))]
        dedup = lambda xs: list(dict.fromkeys(xs))
        return dedup(in_title), dedup(title_only), dedup(in_abs)


# --------------------------------------------------------------------------- fetching


def http_get(url: str, retries: int = 4, pause: float = 3.0) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(pause * (2 ** i))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last}")


def strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def parse_arxiv_rss(xml: str) -> list[Paper]:
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):
        desc = it.findtext("description") or ""
        m = re.match(r"\s*arXiv:(\S+)\s+Announce Type:\s*(\S+)\s*Abstract:\s*(.*)", desc, re.S)
        if not m:
            continue
        aid, ann, abstract = strip_version(m.group(1)), m.group(2), m.group(3).strip()
        creators = it.findtext("dc:creator", default="", namespaces=NS)
        authors = [a.strip() for a in re.split(r",\s*|\s+and\s+", creators) if a.strip()]
        out.append(Paper(
            pid=aid,
            title=re.sub(r"\s+", " ", it.findtext("title") or "").strip(),
            abstract=abstract,
            authors=authors,
            categories=[c.text for c in it.findall("category") if c.text],
            link=f"https://arxiv.org/abs/{aid}",
            pdf=f"https://arxiv.org/pdf/{aid}",
            source="arxiv",
            announce=ann,
            date=_rss_date(it.findtext("pubDate") or ""),
        ))
    return out


def _rss_date(s: str) -> str:
    try:
        return dt.datetime.strptime(s[:16], "%a, %d %b %Y").date().isoformat()
    except ValueError:
        return dt.date.today().isoformat()


def parse_atom(xml: str, source: str) -> tuple[list[Paper], int]:
    root = ET.fromstring(xml)
    total = int(root.findtext("os:totalResults", default="0", namespaces=NS) or 0)
    out = []
    for e in root.findall("atom:entry", NS):
        eid = e.findtext("atom:id", default="", namespaces=NS)
        if not eid or "api/errors" in eid:
            continue
        raw_id = eid.rsplit("/abs/", 1)[-1]
        links = {(l.get("title") or l.get("rel")): l.get("href") for l in e.findall("atom:link", NS)}
        if source == "hexagon":
            pid = "hexagon:" + strip_version(raw_id)
            link = links.get("alternate") or f"https://hexagonmath.org/abs/{raw_id}"
            pdf = links.get("pdf") or f"https://hexagonmath.org/pdf/{raw_id}"
        else:
            pid = strip_version(raw_id)
            link, pdf = f"https://arxiv.org/abs/{pid}", f"https://arxiv.org/pdf/{pid}"
        out.append(Paper(
            pid=pid,
            title=re.sub(r"\s+", " ", e.findtext("atom:title", default="", namespaces=NS)).strip(),
            abstract=re.sub(r"\s+", " ", e.findtext("atom:summary", default="", namespaces=NS)).strip(),
            authors=[a.findtext("atom:name", default="", namespaces=NS) for a in e.findall("atom:author", NS)],
            categories=[c.get("term") for c in e.findall("atom:category", NS) if c.get("term")],
            link=link,
            pdf=pdf,
            source=source,
            announce="author-search" if source == "arxiv" else "new",
            date=(e.findtext("atom:published", default="", namespaces=NS) or "")[:10],
            ai_mode=e.findtext("mathlake:authorshipMode", default="", namespaces=NS) or "",
        ))
    return out, total


def fetch_arxiv_rss(cfg) -> list[Paper]:
    cats = "+".join(cfg["categories"])
    return parse_arxiv_rss(http_get(f"https://rss.arxiv.org/rss/{cats}"))


def api_query(base: str, search: str, start=0, n=200, sort="submittedDate") -> str:
    qs = urllib.parse.urlencode({
        "search_query": search, "start": start, "max_results": n,
        "sortBy": sort, "sortOrder": "descending",
    })
    return http_get(f"{base}?{qs}")


def fetch_author_search(authors: Authors, days: int = 4) -> list[Paper]:
    """Recent arXiv submissions by watched authors, in any category."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y%m%d%H%M")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M")
    terms = []
    for name in authors.all_names():
        toks = strip_accents(name).replace(".", " ").split()
        terms.append(f'au:"{toks[-1]}_{toks[0][0]}"')
    out = []
    for i in range(0, len(terms), 15):
        q = f"({' OR '.join(terms[i:i + 15])}) AND submittedDate:[{since} TO {now}]"
        papers, _ = parse_atom(api_query("https://export.arxiv.org/api/query", q, n=100), "arxiv")
        out += papers
        time.sleep(3.1)
    return out


def fetch_hexagon(cfg, since_iso: str | None) -> list[Paper]:
    """Newest Hexagon works (all subjects; volume is small). Category filtering is done locally."""
    papers, _ = parse_atom(api_query("https://hexagonmath.org/api/query", "all:*", n=100), "hexagon")
    if not papers:  # fall back to an explicit category query if the wildcard is rejected
        q = " OR ".join(f"cat:{c}" for c in cfg["categories"])
        papers, _ = parse_atom(api_query("https://hexagonmath.org/api/query", q, n=100), "hexagon")
    if since_iso:
        papers = [p for p in papers if p.date > since_iso[:10] or p.date == since_iso[:10]]
    return papers


# --------------------------------------------------------------------------- classification

TIER_NAMES = {
    1: "Followed authors",
    2: "Keyword in title",
    3: "Keyword in abstract",
    4: "Rest of today",
}


def classify(p: Paper, cfg, authors: Authors, kws: Keywords) -> Paper:
    subs = set(cfg["categories"])
    in_subs = [c for c in p.categories if c in subs]
    for a in p.authors:
        hit = authors.match(a)
        if hit:
            p.watched.add(a)
            p.reasons.append(("author: " if authors.tier[hit] == "always" else "author (watch): ") + hit)
    kt, kto, ka = kws.hits(p.title, p.abstract)
    # broad title-only keywords (motivic, picard, nilpoten, ...) only count in these categories
    broad_cats = cfg.get("title_only_categories")
    if broad_cats and not any(c in broad_cats for c in p.categories):
        kto = []
    p.reasons += [f"title: {k}" for k in kt + kto] + [f"abstract: {k}" for k in ka]

    always_author = any(authors.tier[authors.match(a)] == "always" for a in p.watched)
    watch_author = bool(p.watched) and not always_author
    any_kw = bool(kt or kto or ka)

    # watched-but-low-signal authors count only inside your categories or with a keyword
    author_counts = always_author or (watch_author and (in_subs or any_kw))

    if p.announce.startswith("replace") and not (author_counts and cfg.get("show_replacements_for_authors", True)):
        p.tier = 9
        return p
    if p.source == "hexagon" and p.ai_mode == "no-human-author-claimed" and cfg.get("hexagon", {}).get("drop_ai_generated"):
        p.tier = 9
        return p

    if author_counts:
        p.tier = 1
    elif kt or kto:
        p.tier = 2
    elif ka:
        p.tier = 3
    elif in_subs and any(c in cfg.get("rest_categories", []) for c in in_subs):
        p.tier = 4
    else:
        p.tier = 9

    # restricted categories (e.g. math.RT): keep only on the listed conditions
    rules = cfg.get("category_rules") or {}
    if in_subs and all(c in rules for c in in_subs) and p.tier < 9:
        allowed = set().union(*(rules[c] for c in in_subs))
        ok = ("author" in allowed and author_counts) or \
             ("strong_title" in allowed and bool(kt)) or \
             ("strong" in allowed and bool(kt or ka))
        if not ok:
            p.tier = 9
    # outside your categories entirely: only authors or strong keywords
    if not in_subs and p.tier < 9 and not (author_counts or kt or ka):
        p.tier = 9
    return p


def merge(papers: list[Paper]) -> list[Paper]:
    """Dedupe by arXiv id, then by normalized title across sources."""
    by_id: dict[str, Paper] = {}
    for p in papers:
        q = by_id.get(p.pid)
        if q is None or (q.announce == "author-search" and p.announce != "author-search"):
            by_id[p.pid] = p
    by_title: dict[str, Paper] = {}
    for p in by_id.values():
        k = p.title_key()
        q = by_title.get(k)
        if q is None:
            by_title[k] = p
        else:
            keep, other = (q, p) if q.source == "arxiv" else (p, q)
            keep.other_links[other.source] = other.link
            if other.ai_mode and not keep.ai_mode:
                keep.ai_mode = other.ai_mode
            by_title[k] = keep
    return list(by_title.values())


# --------------------------------------------------------------------------- output

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e4e2dc;--accent:#8a2c1f;--chip:#efece4;--hit:#fff1c2}
@media (prefers-color-scheme:dark){:root{--bg:#151517;--fg:#ececec;--muted:#9a9aa2;--line:#2c2c30;--accent:#f0907e;--chip:#25252a;--hit:#4a3f12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1rem 16px 4rem}
header{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:.5rem}
h1{font-size:1.25rem;margin:0}header nav a{color:var(--muted);font-size:.9rem;margin-left:.75rem}
h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:2rem 0 .5rem}
article{padding:.75rem 0;border-bottom:1px solid var(--line)}
article h3{font-size:1.02rem;margin:0 0 .2rem;font-weight:600;line-height:1.35}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.meta{font-size:.88rem;color:var(--muted)}.meta b{color:var(--fg);background:var(--hit);padding:0 .2em;border-radius:3px;font-weight:600}
.chips{margin:.3rem 0 0;display:flex;flex-wrap:wrap;gap:.3rem}.chip{font-size:.75rem;background:var(--chip);border-radius:999px;padding:.05rem .55rem;color:var(--muted)}
.chip.why{color:var(--fg)}.chip.warn{background:var(--hit);color:var(--fg)}
details{margin-top:.35rem}summary{cursor:pointer;color:var(--muted);font-size:.85rem}details p{font-size:.92rem;margin:.4rem 0 0}
.links a{font-size:.85rem;margin-right:.8rem}
.rest li{margin:.25rem 0;font-size:.92rem}.empty{color:var(--muted)}
"""

AI_LABEL = {"no-human-author-claimed": "AI-generated (no human author claimed)", "mixed": "AI-assisted (mixed)"}


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def render_paper(p: Paper) -> str:
    authors = ", ".join(f"<b>{esc(a)}</b>" if a in p.watched else esc(a) for a in p.authors)
    chips = [f'<span class="chip why">{esc(r)}</span>' for r in p.reasons]
    chips += [f'<span class="chip">{esc(c)}</span>' for c in p.categories[:4]]
    if p.announce not in ("new", "author-search"):
        chips.append(f'<span class="chip">{esc(p.announce)}</span>')
    if p.source == "hexagon" or "hexagon" in p.other_links:
        chips.append('<span class="chip">Hexagon</span>')
    if p.ai_mode in AI_LABEL:
        chips.append(f'<span class="chip warn">{AI_LABEL[p.ai_mode]}</span>')
    links = [f'<a href="{esc(p.link)}">abstract</a>', f'<a href="{esc(p.pdf)}">pdf</a>']
    links += [f'<a href="{esc(u)}">{esc(s)}</a>' for s, u in p.other_links.items()]
    pid = p.pid.replace("hexagon:", "")
    return f"""<article><h3><a href="{esc(p.link)}">{esc(p.title)}</a></h3>
<div class="meta">{authors} · <span>{esc(pid)}</span></div>
<div class="chips">{''.join(chips)}</div>
<details><summary>Abstract</summary><p>{esc(p.abstract)}</p></details>
<div class="links">{''.join(links)}</div></article>"""


def render_page(title: str, papers: list[Paper], cfg, subtitle: str = "", nav: str = "") -> str:
    tiers = {t: [p for p in papers if p.tier == t] for t in TIER_NAMES}
    body = []
    for t in (1, 2, 3):
        if tiers[t]:
            body.append(f"<h2>{TIER_NAMES[t]} · {len(tiers[t])}</h2>")
            body += [render_paper(p) for p in tiers[t]]
    if not any(tiers[t] for t in (1, 2, 3)):
        body.append('<p class="empty">No matches.</p>')
    if tiers[4]:
        rest = "".join(f'<li><a href="{esc(p.link)}">{esc(p.title)}</a> <span class="meta">— {esc(", ".join(p.authors[:4]))}'
                       f'{" et al." if len(p.authors) > 4 else ""}</span></li>' for p in tiers[4])
        body.append(f'<details class="rest"><summary>{TIER_NAMES[4]} · {len(tiers[4])} unmatched in '
                    f'{esc(", ".join(cfg.get("rest_categories", [])))}</summary><ul>{rest}</ul></details>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><link rel="alternate" type="application/atom+xml" href="{esc(cfg['output']['base_url'])}feed.xml">
<style>{CSS}</style></head><body><main>
<header><h1>{esc(title)}</h1><nav>{nav}</nav></header>
<p class="meta">{esc(subtitle)}</p>
{''.join(body)}
</main></body></html>"""


def nav_html(prefix: str = "") -> str:
    return f'<a href="{prefix}index.html">Today</a><a href="{prefix}archive/index.html">Archive</a><a href="{prefix}feed.xml">Feed</a>'


def render_atom(items: list[dict], cfg) -> str:
    base = cfg["output"]["base_url"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for d in items:
        p = Paper.from_json(d["paper"])
        why = "; ".join(p.reasons)
        content = (f"<p><b>{esc(why)}</b></p><p>{esc(', '.join(p.authors))}</p><p>{esc(p.abstract)}</p>"
                   f'<p><a href="{esc(p.link)}">abstract</a> · <a href="{esc(p.pdf)}">pdf</a></p>')
        entries.append(f"""<entry><title>{esc(p.title)}</title><id>{esc(p.link)}</id>
<link href="{esc(p.link)}"/><updated>{d['day']}T06:00:00Z</updated>
{''.join(f'<author><name>{esc(a)}</name></author>' for a in p.authors)}
<summary>{esc(why)}</summary><content type="html">{esc(content)}</content></entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>{esc(cfg['output']['site_title'])}</title>
<id>{esc(base)}feed.xml</id><link rel="self" href="{esc(base)}feed.xml"/><link href="{esc(base)}"/>
<updated>{now}</updated>
{''.join(entries)}
</feed>"""


def write_archive_index():
    days = sorted((p.stem for p in (DOCS / "archive").glob("20*.html")), reverse=True)
    items = "".join(f'<li><a href="{d}.html">{d}</a></li>' for d in days)
    (DOCS / "archive" / "index.html").write_text(
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Archive</title><style>{CSS}</style></head><body><main><header><h1>Archive</h1>'
        f'<nav>{nav_html("../")}</nav></header><ul>{items}</ul></main></body></html>', encoding="utf-8")


# --------------------------------------------------------------------------- state


def load_state() -> dict:
    f = STATE / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"seen": {}, "hexagon_since": None, "feed": []}


def save_state(st: dict):
    STATE.mkdir(exist_ok=True)
    # keep "seen" bounded: drop ids older than 60 days
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    (STATE / "state.json").write_text(json.dumps(st, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- modes


def run_daily(cfg, authors, kws, fixtures: Path | None = None):
    st = load_state()
    today = dt.date.today().isoformat()
    papers: list[Paper] = []
    errors = []

    def attempt(label, fn):
        try:
            return fn()
        except Exception as e:  # one failing source must not kill the run
            errors.append(f"{label}: {e}")
            print(f"WARNING {label}: {e}", file=sys.stderr)
            return []

    src = cfg.get("sources", {})
    if fixtures:
        papers += parse_arxiv_rss((fixtures / "rss.xml").read_text())
        if (fixtures / "hexagon.xml").exists():
            papers += parse_atom((fixtures / "hexagon.xml").read_text(), "hexagon")[0]
        if (fixtures / "author_search.xml").exists():
            papers += parse_atom((fixtures / "author_search.xml").read_text(), "arxiv")[0]
    else:
        if src.get("arxiv", True):
            papers += attempt("arXiv RSS", lambda: fetch_arxiv_rss(cfg))
        if src.get("arxiv_author_search", True):
            papers += attempt("arXiv author search", lambda: fetch_author_search(authors))
        if src.get("hexagon", True):
            papers += attempt("Hexagon", lambda: fetch_hexagon(cfg, st.get("hexagon_since")))

    papers = [p for p in merge(papers) if p.pid not in st["seen"]]
    for p in papers:
        classify(p, cfg, authors, kws)
    shown = sorted([p for p in papers if p.tier < 9], key=lambda p: (p.tier, p.title.lower()))

    for p in papers:
        st["seen"][p.pid] = today
    hex_dates = [p.date for p in papers if p.source == "hexagon" and p.date]
    if hex_dates:
        st["hexagon_since"] = max(hex_dates)

    if not shown:
        print("Nothing new; leaving the page unchanged.")
        save_state(st)
        return 0

    matches = [p for p in shown if p.tier <= 3]
    sub = f"{today} · {len(matches)} matches, {len(shown) - len(matches)} others"
    if errors:
        sub += " · source errors: " + "; ".join(errors)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "archive").mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render_page(cfg["output"]["site_title"], shown, cfg, sub, nav_html()), encoding="utf-8")
    (DOCS / "archive" / f"{today}.html").write_text(
        render_page(f"{cfg['output']['site_title']} · {today}", shown, cfg, sub, nav_html("../")), encoding="utf-8")
    write_archive_index()

    cutoff = (dt.date.today() - dt.timedelta(days=cfg["output"].get("feed_days", 21))).isoformat()
    st["feed"] = [d for d in st.get("feed", []) if d["day"] >= cutoff]
    st["feed"] = [{"day": today, "paper": p.to_json()} for p in matches] + st["feed"]
    (DOCS / "feed.xml").write_text(render_atom(st["feed"], cfg), encoding="utf-8")
    save_state(st)
    print(sub)
    for p in matches:
        print(f"  [{p.tier}] {p.title}  <- {'; '.join(p.reasons)}")
    return 0


def run_backtest(cfg, authors, kws, days: int):
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)
    cats = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    q = f"({cats}) AND submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    papers, offset, total, empty = [], 0, None, 0
    while total is None or offset < min(total, 30000):
        batch, t = parse_atom(api_query("https://export.arxiv.org/api/query", q, start=offset, n=500), "arxiv")
        total = max(total or 0, t)
        if not batch:
            # the arXiv API intermittently returns an empty page mid-pagination: retry, don't stop
            empty += 1
            print(f"empty page at {offset}/{total} (retry {empty})", file=sys.stderr)
            if empty > 6:
                break
            time.sleep(10)
            continue
        empty = 0
        papers += batch
        offset += len(batch)
        print(f"fetched {offset}/{total}", file=sys.stderr)
        time.sleep(3.1)
    for p in papers:
        p.announce = "new"  # treat API results like announcements
    papers = merge(papers)
    for p in papers:
        classify(p, cfg, authors, kws)
    shown = sorted([p for p in papers if p.tier < 9], key=lambda p: (p.tier, p.title.lower()))
    counts = {t: sum(p.tier == t for p in shown) for t in (1, 2, 3, 4)}
    sub = (f"Backtest {start:%Y-%m-%d} → {end:%Y-%m-%d}: {len(papers)} papers scanned (API reported {total}); "
           f"{counts[1]} author, {counts[2]} title, {counts[3]} abstract matches; {counts[4]} unmatched in rest categories")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "backtest.html").write_text(render_page("Backtest", shown, cfg, sub, nav_html()), encoding="utf-8")
    print(sub)
    return 0


def run_refresh_citers(cfg, authors):
    known = {norm_name(n) for n in authors.all_names()}
    found: dict[str, list[str]] = {}
    for aid in cfg.get("my_papers") or []:
        url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{aid}/citations?fields=title,authors&limit=500"
        data = json.loads(http_get(url, retries=6, pause=5))
        for c in data.get("data", []):
            cp = c.get("citingPaper") or {}
            for a in cp.get("authors") or []:
                name = a.get("name") or ""
                if name and authors.match(name) is None and norm_name(name) not in known:
                    found.setdefault(name, []).append(cp.get("title") or "?")
        time.sleep(3)
    out = {"generated": dt.date.today().isoformat(),
           "note": "Authors citing your papers who are not in authors.yaml yet. Move the ones you want into authors.yaml.",
           "suggested": [{"name": n, "citing": sorted(set(t))} for n, t in sorted(found.items())]}
    (ROOT / "citers_suggested.yaml").write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"{len(found)} new citing authors suggested")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", type=int, metavar="DAYS")
    ap.add_argument("--refresh-citers", action="store_true")
    ap.add_argument("--fixtures", type=Path)
    args = ap.parse_args(argv)
    cfg = load_yaml("config.yaml")
    authors = Authors(load_yaml("authors.yaml"))
    kws = Keywords(load_yaml("keywords.yaml"))
    if args.backtest:
        return run_backtest(cfg, authors, kws, args.backtest)
    if args.refresh_citers:
        return run_refresh_citers(cfg, authors)
    return run_daily(cfg, authors, kws, args.fixtures)


if __name__ == "__main__":
    sys.exit(main())
