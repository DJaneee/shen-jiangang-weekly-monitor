#!/usr/bin/env python3
"""Local public-information monitor for Professor Jiangang Shen.

The MVP uses only the Python standard library. It collects public metadata,
applies conservative identity rules, stores results in SQLite, and renders a
local HTML digest. It never sends email.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config" / "monitor.json"
DB_PATH = APP_DIR / "data" / "monitor.db"
RAW_DIR = APP_DIR / "archive" / "raw"
REPORT_DIR = APP_DIR / "reports"
LOG_DIR = APP_DIR / "logs"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def apply_subject_secret_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Load optional identity values from private environment variables."""
    subject = config.setdefault("subject", {})
    for field, env_name in {
        "orcid": "SHEN_ORCID",
        "scopus_author_id": "SHEN_SCOPUS_AUTHOR_ID",
        "hku_person_id": "SHEN_HKU_PERSON_ID",
        "email": "SHEN_EMAIL",
    }.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            subject[field] = value
    return config


def ensure_dirs() -> None:
    for path in (DB_PATH.parent, RAW_DIR, REPORT_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    ensure_dirs()
    log_path = LOG_DIR / f"monitor_{dt.date.today().isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(source, external_id, content_hash)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at TEXT,
            discovered_at TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence TEXT NOT NULL,
            identity_reasons_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_state (
            source_id TEXT PRIMARY KEY,
            checked_at TEXT NOT NULL,
            content_hash TEXT,
            url TEXT,
            changed INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    return connection


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value or None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_payload(candidate: dict[str, Any]) -> str:
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dedupe_key(candidate: dict[str, Any]) -> str:
    doi = normalize_doi(candidate.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmid = normalize_text(candidate.get("pmid"))
    if pmid:
        return f"pmid:{pmid}"
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalize_text(candidate.get("title")).lower())
    date = normalize_text(candidate.get("published_at"))
    return f"title:{sha256_text(title + '|' + date)}"


def classify_identity(candidate: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, list[str]]:
    subject = config["subject"]
    text_parts: list[str] = []
    for key in ("authors", "affiliations", "emails"):
        value = candidate.get(key, [])
        text_parts.extend(value if isinstance(value, list) else [str(value)])
    combined = " | ".join(text_parts).lower()
    authors = [normalize_text(name).lower() for name in candidate.get("authors", [])]
    reasons: list[str] = []

    candidate_orcid = normalize_text(candidate.get("orcid"))
    if candidate_orcid and candidate_orcid == subject["orcid"]:
        reasons.append("ORCID exact match")
    if subject["email"].lower() in combined:
        reasons.append("HKU email exact match")
    if any(term.lower() in combined for term in subject["affiliation_terms"]):
        reasons.append("HKU affiliation match")
    strong_name = any(name.lower() in authors for name in subject["names"] if not re.search(r"[\u4e00-\u9fff]", name))
    if strong_name:
        reasons.append("strong name match")

    if "ORCID exact match" in reasons or "HKU email exact match" in reasons:
        return "accepted", "high", reasons
    if "strong name match" in reasons and "HKU affiliation match" in reasons:
        return "accepted", "high", reasons
    if "strong name match" in reasons:
        return "review", "medium", reasons + ["name alone is insufficient"]
    return "rejected", "low", reasons + ["no qualifying identity evidence"]


def http_get(url: str, config: dict[str, Any]) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": config["runtime"]["user_agent"]})
    context = ssl.create_default_context()
    try:
        import certifi  # optional; fixes incomplete local CA stores on some Windows Python installations
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(
        request,
        timeout=config["runtime"]["request_timeout_seconds"],
        context=context,
    ) as response:
        return response.read()


def save_raw_bytes(source: str, suffix: str, content: bytes) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    target_dir = RAW_DIR / source
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stamp}_{hashlib.sha256(content).hexdigest()[:12]}.{suffix}"
    if not target.exists():
        target.write_bytes(content)
    return target


def pubmed_query() -> str:
    return (
        '("Jiangang Shen"[Author] OR "Shen Jiangang"[Author] '
        'OR "Jian-Gang Shen"[Author] OR "Shen JG"[Author] '
        'OR "0000-0002-4199-8095"[Author Identifier])'
    )


def collect_pubmed(config: dict[str, Any], lookback_days: int) -> list[dict[str, Any]]:
    params = {
        "db": "pubmed",
        "term": pubmed_query(),
        "reldate": str(lookback_days),
        "datetype": "edat",
        "retmode": "json",
        "retmax": "100",
        "sort": "pub date",
    }
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    search_bytes = http_get(search_url, config)
    save_raw_bytes("pubmed", "json", search_bytes)
    ids = json.loads(search_bytes.decode("utf-8"))["esearchresult"].get("idlist", [])
    if not ids:
        return []
    fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(fetch_params)
    fetch_bytes = http_get(fetch_url, config)
    save_raw_bytes("pubmed", "xml", fetch_bytes)
    return parse_pubmed_xml(fetch_bytes)


def element_text(element: ET.Element | None) -> str:
    return normalize_text("".join(element.itertext()) if element is not None else "")


def parse_pubmed_date(article: ET.Element) -> str | None:
    date_node = article.find(".//ArticleDate")
    if date_node is None:
        date_node = article.find(".//PubDate")
    if date_node is None:
        return None
    year = element_text(date_node.find("Year"))
    month = element_text(date_node.find("Month")) or "01"
    day = element_text(date_node.find("Day")) or "01"
    month_map = {name: f"{idx:02d}" for idx, name in enumerate(
        ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )}
    month = month_map.get(month[:3].title(), month.zfill(2) if month.isdigit() else "01")
    if year.isdigit():
        return f"{year}-{month}-{day.zfill(2)}"
    return None


def parse_pubmed_xml(content: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    candidates: list[dict[str, Any]] = []
    for record in root.findall(".//PubmedArticle"):
        article = record.find(".//Article")
        if article is None:
            continue
        pmid = element_text(record.find(".//PMID"))
        authors: list[str] = []
        affiliations: list[str] = []
        emails: list[str] = []
        for author in article.findall(".//Author"):
            name = normalize_text(f"{element_text(author.find('ForeName'))} {element_text(author.find('LastName'))}")
            if name:
                authors.append(name)
            for aff in author.findall(".//Affiliation"):
                aff_text = element_text(aff)
                if aff_text:
                    affiliations.append(aff_text)
                    emails.extend(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", aff_text))
        article_ids = {node.attrib.get("IdType"): element_text(node) for node in record.findall(".//ArticleId")}
        abstract = " ".join(element_text(node) for node in article.findall(".//AbstractText"))
        candidates.append(
            {
                "source": "pubmed",
                "external_id": f"pmid:{pmid}",
                "event_type": "publication",
                "title": element_text(article.find("ArticleTitle")),
                "published_at": parse_pubmed_date(article),
                "authors": authors,
                "affiliations": sorted(set(affiliations)),
                "emails": sorted(set(emails)),
                "doi": article_ids.get("doi"),
                "pmid": pmid,
                "journal": element_text(article.find(".//Journal/Title")),
                "abstract": abstract,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )
    return candidates


def collect_crossref(config: dict[str, Any], lookback_days: int) -> list[dict[str, Any]]:
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    params = {
        "filter": f"orcid:{config['subject']['orcid']},from-index-date:{since}",
        "rows": "100",
        "select": "DOI,title,author,published-online,published-print,created,indexed,container-title,URL,type",
        "mailto": os.environ.get("SHEN_MONITOR_CONTACT", "monitor@example.invalid"),
    }
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    content = http_get(url, config)
    save_raw_bytes("crossref", "json", content)
    items = json.loads(content.decode("utf-8"))["message"].get("items", [])
    candidates: list[dict[str, Any]] = []
    for item in items:
        authors: list[str] = []
        subject_affiliations: list[str] = []
        all_affiliations: list[str] = []
        orcid = None
        for author in item.get("author", []):
            author_name = normalize_text(f"{author.get('given', '')} {author.get('family', '')}")
            authors.append(author_name)
            author_affiliations = [
                normalize_text(aff.get("name")) for aff in author.get("affiliation", []) if aff.get("name")
            ]
            all_affiliations.extend(author_affiliations)
            author_orcid = normalize_text(author.get("ORCID")).rsplit("/", 1)[-1] or None
            if author_orcid == config["subject"]["orcid"]:
                orcid = author_orcid
                subject_affiliations.extend(author_affiliations)
            elif author_name.lower() in {"jiangang shen", "jian-gang shen"}:
                subject_affiliations.extend(author_affiliations)
                orcid = author_orcid
        published_at = crossref_date(item.get("published-online")) or crossref_date(item.get("published-print"))
        candidates.append(
            {
                "source": "crossref",
                "external_id": f"doi:{normalize_doi(item.get('DOI'))}",
                "event_type": "publication",
                "title": normalize_text(" ".join(item.get("title", []))),
                "published_at": published_at,
                "authors": authors,
                "affiliations": sorted(set(filter(None, subject_affiliations))),
                "all_affiliations": sorted(set(filter(None, all_affiliations))),
                "emails": [],
                "orcid": orcid,
                "doi": item.get("DOI"),
                "journal": normalize_text(" ".join(item.get("container-title", []))),
                "abstract": "",
                "url": item.get("URL") or (f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else None),
            }
        )
    return candidates


def crossref_date(node: dict[str, Any] | None) -> str | None:
    parts = ((node or {}).get("date-parts") or [[]])[0]
    if not parts:
        return None
    values = list(parts) + [1, 1]
    return f"{values[0]:04d}-{values[1]:02d}-{values[2]:02d}"


def check_official_pages(connection: sqlite3.Connection, config: dict[str, Any]) -> None:
    for source in config.get("official_pages", []):
        try:
            content = http_get(source["url"], config)
            save_raw_bytes(source["id"], "html", content)
            digest = hashlib.sha256(content).hexdigest()
            previous = connection.execute(
                "SELECT content_hash FROM source_state WHERE source_id = ?", (source["id"],)
            ).fetchone()
            changed = int(previous is not None and previous["content_hash"] != digest)
            connection.execute(
                """INSERT INTO source_state(source_id, checked_at, content_hash, url, changed, error)
                   VALUES (?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(source_id) DO UPDATE SET checked_at=excluded.checked_at,
                   content_hash=excluded.content_hash, url=excluded.url, changed=excluded.changed, error=NULL""",
                (source["id"], iso_now(), digest, source["url"], changed),
            )
        except Exception as exc:  # one source must not stop the run
            logging.warning("Official page failed: %s: %s", source["id"], exc)
            connection.execute(
                """INSERT INTO source_state(source_id, checked_at, url, changed, error)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(source_id) DO UPDATE SET checked_at=excluded.checked_at,
                   url=excluded.url, changed=0, error=excluded.error""",
                (source["id"], iso_now(), source["url"], str(exc)),
            )


def ingest_candidates(
    connection: sqlite3.Connection, candidates: Iterable[dict[str, Any]], config: dict[str, Any]
) -> dict[str, int]:
    counts = {"accepted": 0, "review": 0, "rejected": 0}
    now = iso_now()
    for candidate in candidates:
        candidate["doi"] = normalize_doi(candidate.get("doi"))
        payload = canonical_payload(candidate)
        content_hash = sha256_text(payload)
        connection.execute(
            "INSERT OR IGNORE INTO raw_items(source, external_id, discovered_at, content_hash, payload_json) VALUES (?, ?, ?, ?, ?)",
            (candidate["source"], candidate["external_id"], now, content_hash, payload),
        )
        status, confidence, reasons = classify_identity(candidate, config)
        counts[status] += 1
        key = dedupe_key(candidate)
        connection.execute(
            """INSERT INTO events(
                   dedupe_key, source, external_id, event_type, title, published_at, discovered_at,
                   status, confidence, identity_reasons_json, payload_json, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(dedupe_key) DO UPDATE SET
                   last_seen_at=excluded.last_seen_at,
                   status=CASE WHEN events.status='accepted' THEN events.status ELSE excluded.status END,
                   confidence=CASE WHEN events.confidence='high' THEN events.confidence ELSE excluded.confidence END,
                   source=CASE WHEN excluded.status='accepted' THEN excluded.source ELSE events.source END,
                   external_id=CASE WHEN excluded.status='accepted' THEN excluded.external_id ELSE events.external_id END,
                   payload_json=CASE WHEN excluded.status='accepted' THEN excluded.payload_json ELSE events.payload_json END""",
            (
                key,
                candidate["source"],
                candidate["external_id"],
                candidate.get("event_type", "other"),
                normalize_text(candidate.get("title")) or "（无标题）",
                candidate.get("published_at"),
                now,
                status,
                confidence,
                json.dumps(reasons, ensure_ascii=False),
                payload,
                now,
                now,
            ),
        )
    connection.commit()
    return counts


def create_run(connection: sqlite3.Connection, mode: str) -> int:
    cursor = connection.execute(
        "INSERT INTO runs(started_at, mode, status) VALUES (?, ?, 'running')", (iso_now(), mode)
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(connection: sqlite3.Connection, run_id: int, status: str, counts: dict[str, int], error: str | None = None) -> None:
    connection.execute(
        """UPDATE runs SET finished_at=?, status=?, accepted_count=?, review_count=?, rejected_count=?, error=? WHERE id=?""",
        (iso_now(), status, counts.get("accepted", 0), counts.get("review", 0), counts.get("rejected", 0), error, run_id),
    )
    connection.commit()


def collect(connection: sqlite3.Connection, config: dict[str, Any], lookback_days: int, fixture: Path | None) -> dict[str, int]:
    candidates: list[dict[str, Any]] = []
    if fixture:
        logging.info("Using offline fixture: %s", fixture)
        candidates.extend(load_json(fixture))
    else:
        for name, collector in (("PubMed", collect_pubmed), ("Crossref", collect_crossref)):
            try:
                found = collector(config, lookback_days)
                logging.info("%s returned %d candidates", name, len(found))
                candidates.extend(found)
            except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
                logging.error("%s collection failed: %s", name, exc)
        check_official_pages(connection, config)
    counts = ingest_candidates(connection, candidates, config)
    logging.info("Ingested candidates: %s", counts)
    return counts


def summarize_payload(payload: dict[str, Any]) -> str:
    abstract = normalize_text(payload.get("abstract"))
    if abstract:
        sentences = re.split(r"(?<=[.!?。！？])\s+", abstract)
        return " ".join(sentences[:2])[:600]
    return "已核实作者身份和来源元数据；当前公开记录未提供摘要。"


def render_digest(connection: sqlite3.Connection, digest_days: int, include_all: bool = False) -> Path:
    cutoff = (dt.date.today() - dt.timedelta(days=digest_days)).isoformat()
    if include_all:
        rows = connection.execute(
            "SELECT * FROM events WHERE status='accepted' ORDER BY COALESCE(published_at, discovered_at) DESC"
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT * FROM events WHERE status='accepted'
               AND (substr(first_seen_at,1,10) >= ? OR published_at >= ?)
               ORDER BY COALESCE(published_at, discovered_at) DESC""",
            (cutoff, cutoff),
        ).fetchall()
    review_count = connection.execute("SELECT COUNT(*) AS n FROM events WHERE status='review'").fetchone()["n"]
    source_rows = connection.execute("SELECT * FROM source_state ORDER BY source_id").fetchall()
    items_html: list[str] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        url = payload.get("url") or ""
        doi = payload.get("doi") or "—"
        meta = " · ".join(filter(None, [row["published_at"] or "日期待核验", payload.get("journal"), f"DOI: {doi}"]))
        link_html = f'<p><a href="{html.escape(url, quote=True)}">查看原始来源</a></p>' if url else ""
        historical = bool(row["published_at"] and row["published_at"] < cutoff)
        discovery_tag = '<span class="tag history">历史成果本周补录</span>' if historical else ''
        items_html.append(
            f"""
            <article class="item">
              <div><span class="tag">{html.escape(row['event_type'])}</span><span class="tag high">高置信度</span>{discovery_tag}</div>
              <h3>{html.escape(row['title'])}</h3>
              <p class="meta">{html.escape(meta)}</p>
              <p>{html.escape(summarize_payload(payload))}</p>
              {link_html}
            </article>"""
        )
    if not items_html:
        items_html.append('<div class="empty">本报告周期未发现通过身份核验的新增公开动态。</div>')
    source_status = "".join(
        f"<li>{html.escape(row['source_id'])}：{'异常：' + html.escape(row['error']) if row['error'] else '正常'}</li>"
        for row in source_rows
    ) or "<li>本次为离线测试，未检查在线来源。</li>"
    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>沈剑刚教授公开动态周报</title><style>
body{{margin:0;background:#f3f6f8;color:#1d2935;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
.shell{{max-width:760px;margin:auto;padding:24px 12px}}.card{{background:#fff;border:1px solid #dfe7ec;border-radius:14px;overflow:hidden}}
.hero{{padding:28px 30px;background:linear-gradient(135deg,#073b4c,#0d6474);color:white}}h1{{margin:0;font-size:26px}}.hero p{{margin:8px 0 0;opacity:.85}}
.content{{padding:26px 30px}}.metrics{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:24px}}.metric{{padding:14px;border:1px solid #e3eaee;border-radius:10px}}.metric strong{{display:block;color:#0d6474;font-size:24px}}
.item{{border:1px solid #dfe7ec;border-radius:12px;padding:20px;margin:12px 0}}.tag{{display:inline-block;margin-right:6px;padding:4px 8px;border-radius:999px;background:#e6f5f3;color:#07675e;font-size:12px}}.high{{background:#eaf5ea;color:#24702c}}.history{{background:#fff2d8;color:#80520a}}
h2{{color:#153c49;margin-top:28px}}h3{{line-height:1.5}}p,li{{line-height:1.7}}.meta{{color:#60717d;font-size:13px}}a{{color:#08778a}}.empty{{padding:18px;border:1px dashed #c9d5dc;border-radius:10px;color:#687985;text-align:center}}
footer{{padding:18px 30px;border-top:1px solid #e5ecef;background:#fafcfd;color:#75848e;font-size:12px}}@media(max-width:560px){{.hero,.content,footer{{padding-left:20px;padding-right:20px}}.metrics{{grid-template-columns:1fr}}}}
</style></head><body><div class="shell"><main class="card"><header class="hero"><h1>沈剑刚教授公开动态周报</h1><p>本地MVP · 生成于 {html.escape(generated)}</p></header><section class="content">
<div class="metrics"><div class="metric"><strong>{len(rows)}</strong><span>已核实新增</span></div><div class="metric"><strong>{review_count}</strong><span>待人工核验</span></div></div>
<h2>本期动态</h2>{''.join(items_html)}<h2>数据源状态</h2><ul>{source_status}</ul>
</section><footer>本报告仅整合公开信息；大模型未参与本地MVP的事实发现。未发现新增不等于全网不存在其他公开消息。</footer></main></div></body></html>"""
    target = REPORT_DIR / f"shen_weekly_digest_{dt.date.today().isoformat()}.html"
    target.write_text(document, encoding="utf-8")
    logging.info("Digest written: %s", target)
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local public-information monitor; never sends email")
    parser.add_argument("mode", choices=("collect", "digest", "all"), nargs="?", default="all")
    parser.add_argument("--fixture", type=Path, help="Use a local JSON fixture instead of network collection")
    parser.add_argument("--lookback-days", type=int, help="Override collection lookback")
    parser.add_argument("--digest-days", type=int, help="Override digest window")
    parser.add_argument("--include-all", action="store_true", help="Include all accepted events in the digest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    config = apply_subject_secret_overrides(load_json(CONFIG_PATH))
    connection = connect_db()
    run_id = create_run(connection, args.mode)
    counts = {"accepted": 0, "review": 0, "rejected": 0}
    try:
        if args.mode in ("collect", "all"):
            lookback = args.lookback_days or int(config["runtime"]["collection_lookback_days"])
            counts = collect(connection, config, lookback, args.fixture)
        if args.mode in ("digest", "all"):
            digest_days = args.digest_days or int(config["runtime"]["digest_days"])
            render_digest(connection, digest_days, args.include_all)
        finish_run(connection, run_id, "success", counts)
        return 0
    except Exception as exc:
        logging.exception("Run failed")
        finish_run(connection, run_id, "failed", counts, str(exc))
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

