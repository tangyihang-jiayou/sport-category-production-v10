#!/usr/bin/env python3
"""Build platform-specific sports creator subscription lists.

The script turns a creator candidate pool into operational subscription outputs:

- YouTube creators that are ready to import.
- Instagram creators that are ready for canary validation.
- TikTok creators kept as watchlist only.
- Review/reject rows with explicit reasons.
- Stateful refresh outputs for ongoing operations.

It can run in two modes:

1. Re-screen an existing full-library audit CSV:
   python scripts/filter_creator_subscriptions.py --audit-csv audit.csv --output-dir out

2. Query the asset-center Postgres database directly:
   ASSET_CENTER_DSN=postgres://... python scripts/filter_creator_subscriptions.py \
     --candidates-json enum_v5.json --output-dir out

No secrets are stored in this file; pass DSNs through environment variables.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


MIN_VALID_RATE = 0.5
MIN_MEDIAN_VIEWS = 5_000
MAX_MEDIAN_VIEWS = 50_000_000

AUTHOR_NEGATIVE_RE = re.compile(
    r"(bookstagram|adult swim|sony pictures|filmisnow|movie scenes|moving pictures|"
    r"movie|cinema|choir|music|records|lyrics|k-?pop|anime|netflix|disney|"
    r"nickelodeon|cartoon|gaming|gameplay|asmr)",
    re.I,
)
BAD_CATEGORY_SET = {"Music", "Gaming", "Film & Animation"}

OUTPUT_FIELDS = [
    "platform",
    "decision",
    "issue",
    "uid",
    "name",
    "old_tier",
    "old_dom",
    "old_n",
    "full_n",
    "valid_n",
    "valid_rate",
    "recent_pub",
    "active30",
    "max_subs",
    "med_views",
    "med_eng",
    "url",
    "recent_titles",
    "category_samples",
]

STATE_FIELDS = OUTPUT_FIELDS + [
    "run_date",
    "lifecycle_state",
    "transition",
    "import_action",
    "first_seen_at",
    "last_seen_at",
    "previous_decision",
    "previous_issue",
    "consecutive_good_runs",
    "consecutive_bad_runs",
]


@dataclass
class ScreenedRow:
    row: dict[str, str]
    decision: str
    issues: list[str]


@dataclass(frozen=True)
class ScreeningConfig:
    run_date: str
    active30_cutoff: str
    active6m_cutoff: str
    min_valid_rate: float = MIN_VALID_RATE
    min_median_views: int = MIN_MEDIAN_VIEWS
    max_median_views: int = MAX_MEDIAN_VIEWS


def parse_ymd(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def subtract_months(value: date, months: int) -> date:
    month_index = value.month - months - 1
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def build_config(
    run_date: str,
    active_days: int,
    active_months: int,
    active_month_days: int | None = None,
) -> ScreeningConfig:
    if active_days <= 0:
        raise SystemExit("--active-days must be positive")
    if active_months <= 0:
        raise SystemExit("--active-months must be positive")
    if active_month_days is not None and active_month_days <= 0:
        raise SystemExit("--active-month-days must be positive when provided")

    today = parse_ymd(run_date)
    active6m_cutoff = (
        today - timedelta(days=active_month_days)
        if active_month_days is not None
        else subtract_months(today, active_months)
    )
    return ScreeningConfig(
        run_date=run_date,
        active30_cutoff=(today - timedelta(days=active_days)).isoformat(),
        active6m_cutoff=active6m_cutoff.isoformat(),
    )


def as_int(value: object, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def as_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value))
    except (TypeError, ValueError):
        return default


def parse_category_counts(raw: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for part in (raw or "").split("|"):
        if not part:
            continue
        if ":" in part:
            name, count = part.rsplit(":", 1)
            counts[name] += as_int(count, 1)
        else:
            counts[part] += 1
    return counts


def normalize_platform(value: object) -> str:
    platform = str(value or "").strip().lower()
    if platform == "youtube":
        return "yt"
    if platform == "instagram":
        return "ig"
    if platform == "tiktok":
        return "tk"
    return platform


def normalize_uid(value: object) -> str:
    return str(value or "").strip()


def normalize_pub_date(value: object) -> str:
    raw = str(value or "").strip()
    if len(raw) < 10:
        return ""
    candidate = raw[:10]
    try:
        return parse_ymd(candidate).isoformat()
    except SystemExit:
        return ""


def is_hard_negative(row: dict[str, str]) -> bool:
    """Detect obvious non-sports false positives without over-blocking sports media.

    Do not reject sports publishers just because they contain words such as
    "Films"; accounts like NFL Films and Courtside Films are sports sources.
    The negative list is intentionally specific to patterns seen in false
    positives from the 2026-06-29 audit.
    """

    name = row.get("name") or row.get("old_name") or ""
    categories = parse_category_counts(row.get("category_samples", ""))
    categories_only_bad = bool(categories) and set(categories).issubset(BAD_CATEGORY_SET)
    return bool(AUTHOR_NEGATIVE_RE.search(name) or categories_only_bad)


def screen_row(row: dict[str, str], config: ScreeningConfig) -> ScreenedRow:
    """Apply the actual subscription screening logic.

    S and A tiers are both eligible. A tier is not a rejection reason; it only
    means the original sample had weaker evidence. Full-library quality gates
    decide whether the creator can move forward.
    """

    platform = normalize_platform(row.get("platform") or row.get("plat"))
    uid = normalize_uid(row.get("uid") or row.get("aid"))
    full_n = as_int(row.get("full_n"))
    valid_n = as_int(row.get("valid_n"))
    valid_rate = as_float(row.get("valid_rate"))
    if valid_rate == 0 and full_n:
        valid_rate = valid_n / full_n

    recent_pub = normalize_pub_date(row.get("recent_pub", ""))
    med_views = as_int(row.get("med_views"))
    issues: list[str] = []

    if not uid:
        issues.append("profile uid缺失")
    if full_n <= 0:
        issues.append("全库未匹配author_id")
    if valid_rate < config.min_valid_rate:
        issues.append("有效播放率低")
    if recent_pub < config.active6m_cutoff:
        issues.append("近6月未见新作")
    if med_views < config.min_median_views:
        issues.append("中位播放低")
    if med_views > config.max_median_views:
        issues.append("中位播放异常高")
    if is_hard_negative(row):
        issues.append("疑似非运动/娱乐硬负例")

    if issues:
        decision = "review_or_reject"
    elif platform == "yt":
        decision = "subscription_ready"
    elif platform == "ig":
        decision = "canary_ready"
    elif platform in {"tk", "tiktok"}:
        decision = "watchlist_profile_unverified"
        issues.append("TK profile订阅能力待确认")
    else:
        decision = "review_or_reject"
        issues.append("未知平台")

    normalized = normalize_output_row(row)
    normalized["platform"] = platform
    normalized["uid"] = uid
    normalized["recent_pub"] = recent_pub
    normalized["decision"] = decision
    normalized["issue"] = ";".join(issues) or "OK"
    normalized["valid_rate"] = f"{valid_rate:.3f}".rstrip("0").rstrip(".")
    normalized["active30"] = "Y" if recent_pub >= config.active30_cutoff else ""
    return ScreenedRow(row=normalized, decision=decision, issues=issues)


def normalize_output_row(row: dict[str, str]) -> dict[str, str]:
    output = {field: str(row.get(field, "") or "") for field in OUTPUT_FIELDS}
    output["platform"] = normalize_platform(output["platform"] or row.get("plat", ""))
    output["uid"] = normalize_uid(output["uid"] or row.get("aid", ""))
    output["recent_pub"] = normalize_pub_date(output["recent_pub"])
    output["name"] = output["name"] or str(row.get("old_name", "") or row.get("db_author", "") or "")
    output["old_tier"] = output["old_tier"] or str(row.get("tier", "") or "")
    output["old_dom"] = output["old_dom"] or str(row.get("dom_l1", "") or "")
    output["old_n"] = output["old_n"] or str(row.get("n", "") or "")
    output["url"] = output["url"] or str(row.get("old_url", "") or row.get("aurl", "") or "")
    return output


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Iterable[dict[str, str]], fields: list[str] = OUTPUT_FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sort_ready(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: (
            row.get("active30") != "Y",
            -as_float(row.get("med_eng")),
            -as_int(row.get("med_views")),
            -as_int(row.get("full_n")),
        ),
    )


def sort_review(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: (
            "疑似非运动/娱乐硬负例" not in row.get("issue", ""),
            row.get("decision") == "review_or_reject",
            -as_int(row.get("med_views")),
        ),
    )


def is_forwardable(decision: str) -> bool:
    return decision in {
        "subscription_ready",
        "canary_ready",
        "watchlist_profile_unverified",
    }


def lifecycle_for(decision: str, previous_forwardable: bool) -> str:
    if decision == "subscription_ready":
        return "active_subscription"
    if decision == "canary_ready":
        return "canary"
    if decision == "watchlist_profile_unverified":
        return "watchlist"
    if previous_forwardable:
        return "paused_review"
    return "rejected_review"


def import_action_for(decision: str, transition: str) -> str:
    if decision == "subscription_ready":
        return "keep_subscription" if transition == "retained" else "upsert_subscription"
    if decision == "canary_ready":
        return "keep_canary" if transition == "retained" else "upsert_canary"
    if decision == "watchlist_profile_unverified":
        return "keep_watchlist" if transition == "retained" else "upsert_watchlist"
    if transition == "downgraded":
        return "pause_or_remove"
    return "hold_review"


def build_state_rows(
    screened: list[dict[str, str]],
    previous_state: dict[tuple[str, str], dict[str, str]],
    config: ScreeningConfig,
) -> list[dict[str, str]]:
    state_rows: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in screened:
        key = (row.get("platform", ""), row.get("uid", ""))
        seen_keys.add(key)
        previous = previous_state.get(key, {})
        previous_decision = previous.get("decision") or previous.get("previous_decision") or ""
        previous_issue = previous.get("issue") or previous.get("previous_issue") or ""
        was_forwardable = is_forwardable(previous_decision)
        is_now_forwardable = is_forwardable(row.get("decision", ""))

        if not previous and is_now_forwardable:
            transition = "new"
        elif not previous:
            transition = "new_review"
        elif was_forwardable and is_now_forwardable:
            transition = "retained"
        elif was_forwardable and not is_now_forwardable:
            transition = "downgraded"
        elif not was_forwardable and is_now_forwardable:
            transition = "reactivated"
        else:
            transition = "still_review"

        consecutive_good_runs = (
            as_int(previous.get("consecutive_good_runs")) + 1 if is_now_forwardable else 0
        )
        consecutive_bad_runs = (
            as_int(previous.get("consecutive_bad_runs")) + 1 if not is_now_forwardable else 0
        )

        state = {field: row.get(field, "") for field in OUTPUT_FIELDS}
        state.update(
            {
                "run_date": config.run_date,
                "lifecycle_state": lifecycle_for(row.get("decision", ""), was_forwardable),
                "transition": transition,
                "import_action": import_action_for(row.get("decision", ""), transition),
                "first_seen_at": previous.get("first_seen_at") or config.run_date,
                "last_seen_at": config.run_date,
                "previous_decision": previous_decision,
                "previous_issue": previous_issue,
                "consecutive_good_runs": str(consecutive_good_runs),
                "consecutive_bad_runs": str(consecutive_bad_runs),
            }
        )
        state_rows.append(state)

    for key, previous in previous_state.items():
        if key in seen_keys:
            continue
        platform, uid = key
        previous_decision = previous.get("decision") or previous.get("previous_decision") or ""
        previous_issue = previous.get("issue") or previous.get("previous_issue") or ""
        was_forwardable = is_forwardable(previous_decision)
        transition = "missing_downgraded" if was_forwardable else "missing_review"

        state = {field: previous.get(field, "") for field in OUTPUT_FIELDS}
        state.update(
            {
                "platform": platform,
                "uid": uid,
                "decision": "missing_from_current_pool",
                "issue": "本轮候选池未出现",
                "run_date": config.run_date,
                "lifecycle_state": "paused_review" if was_forwardable else "rejected_review",
                "transition": transition,
                "import_action": "pause_or_remove" if was_forwardable else "hold_review",
                "first_seen_at": previous.get("first_seen_at") or config.run_date,
                "last_seen_at": previous.get("last_seen_at") or previous.get("run_date") or "",
                "previous_decision": previous_decision,
                "previous_issue": previous_issue,
                "consecutive_good_runs": "0",
                "consecutive_bad_runs": str(as_int(previous.get("consecutive_bad_runs")) + 1),
            }
        )
        state_rows.append(state)
    return state_rows


def read_previous_state(path: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    if not path:
        return {}
    rows = read_csv(path)
    return {
        (normalize_platform(row.get("platform", "")), normalize_uid(row.get("uid", ""))): row
        for row in rows
        if row.get("platform") and row.get("uid")
    }


def write_outputs(
    rows: list[dict[str, str]],
    output_dir: Path,
    config: ScreeningConfig,
    previous_state_path: Path | None = None,
) -> None:
    screened = [screen_row(row, config).row for row in rows]
    previous_state = read_previous_state(previous_state_path)
    state_rows = build_state_rows(screened, previous_state, config)
    by_platform: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for row in screened:
        by_platform[row["platform"]].append(row)

    youtube_ready = sort_ready(
        [row for row in by_platform["yt"] if row["decision"] == "subscription_ready"]
    )
    instagram_canary = sort_ready(
        [row for row in by_platform["ig"] if row["decision"] == "canary_ready"]
    )
    tiktok_watchlist = sort_ready(
        [
            row
            for platform in ("tk", "tiktok")
            for row in by_platform.get(platform, [])
            if row["decision"] == "watchlist_profile_unverified"
        ]
    )
    review = sort_review(
        [row for row in screened if row["decision"] in {"review_or_reject"}]
    )

    write_csv(output_dir / "all_subscription_audit.csv", screened)
    write_csv(output_dir / "current_subscription_state.csv", state_rows, STATE_FIELDS)
    write_csv(
        output_dir / "import_actions.csv",
        [
            row
            for row in state_rows
            if row.get("import_action")
            in {
                "upsert_subscription",
                "upsert_canary",
                "upsert_watchlist",
                "pause_or_remove",
            }
        ],
        STATE_FIELDS,
    )
    write_csv(output_dir / "youtube_subscription_ready.csv", youtube_ready)
    write_csv(output_dir / "instagram_canary_ready.csv", instagram_canary)
    write_csv(output_dir / "tiktok_watchlist.csv", tiktok_watchlist)
    write_csv(output_dir / "review_or_reject.csv", review)

    summary_rows = []
    state_platforms = {row.get("platform", "") for row in state_rows if row.get("platform")}
    current_platforms = {key for key, value in by_platform.items() if value}
    for platform in sorted(current_platforms | state_platforms):
        platform_rows = by_platform.get(platform, [])
        issue_counts: Counter[str] = Counter()
        for row in platform_rows:
            for issue in row.get("issue", "").split(";"):
                if issue and issue != "OK":
                    issue_counts[issue] += 1
        summary_rows.append(
            {
                "platform": platform,
                "candidates": len(platform_rows),
                "subscription_ready": sum(row["decision"] == "subscription_ready" for row in platform_rows),
                "canary_ready": sum(row["decision"] == "canary_ready" for row in platform_rows),
                "watchlist": sum(
                    row["decision"] == "watchlist_profile_unverified" for row in platform_rows
                ),
                "review_or_reject": sum(row["decision"] == "review_or_reject" for row in platform_rows),
                "active30_forward": sum(
                    row.get("active30") == "Y" and row["decision"] != "review_or_reject"
                    for row in platform_rows
                ),
                "new": sum(
                    row.get("transition") == "new"
                    for row in state_rows
                    if row.get("platform") == platform
                ),
                "reactivated": sum(
                    row.get("transition") == "reactivated"
                    for row in state_rows
                    if row.get("platform") == platform
                ),
                "downgraded": sum(
                    row.get("transition") in {"downgraded", "missing_downgraded"}
                    for row in state_rows
                    if row.get("platform") == platform
                ),
                "top_issues": "|".join(
                    f"{issue}:{count}" for issue, count in issue_counts.most_common(8)
                ),
            }
        )
    write_csv(
        output_dir / "platform_summary.csv",
        summary_rows,
        [
            "platform",
            "candidates",
            "subscription_ready",
            "canary_ready",
            "watchlist",
            "review_or_reject",
            "active30_forward",
            "new",
            "reactivated",
            "downgraded",
            "top_issues",
        ],
    )


def build_db_audit_rows(candidates_json: Path, dsn: str) -> list[dict[str, str]]:
    """Return full-library audit rows for YouTube/Instagram candidates."""

    try:
        import psycopg2
    except ImportError as exc:
        raise SystemExit("psycopg2 is required for --candidates-json DB mode") from exc

    import json

    candidates = json.loads(candidates_json.read_text(encoding="utf-8")).get("qual", [])
    deduped = []
    seen = set()
    for item in candidates:
        platform = item.get("plat")
        uid = item.get("aid") or ""
        key = (platform, uid)
        if platform not in {"yt", "ig"} or not uid or key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    conn = psycopg2.connect(dsn)
    conn.set_session(autocommit=False)
    cursor = conn.cursor()
    cursor.execute(
        """
        create temp table cand(
            platform text,
            uid text,
            old_name text,
            old_tier text,
            old_dom text,
            old_n int,
            old_url text
        ) on commit drop
        """
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    for item in deduped:
        writer.writerow(
            [
                item.get("plat", ""),
                item.get("aid", ""),
                str(item.get("name") or "").replace("\t", " ")[:300],
                item.get("tier", ""),
                item.get("dom_l1", ""),
                as_int(item.get("n")),
                item.get("aurl", ""),
            ]
        )
    buffer.seek(0)
    cursor.copy_from(
        buffer,
        "cand",
        columns=("platform", "uid", "old_name", "old_tier", "old_dom", "old_n", "old_url"),
        null="",
    )
    cursor.execute(DB_AUDIT_QUERY)
    columns = [description[0] for description in cursor.description]
    rows = [normalize_db_row(dict(zip(columns, record))) for record in cursor.fetchall()]
    conn.rollback()
    conn.close()
    return rows


def normalize_db_row(row: dict[str, object]) -> dict[str, str]:
    titles = row.get("recent_titles") or []
    category_samples = row.get("category_samples") or []
    categories: Counter[str] = Counter()
    for sample in category_samples:
        if isinstance(sample, list):
            categories.update(str(item) for item in sample)

    return {
        "platform": normalize_platform(row.get("platform")),
        "uid": normalize_uid(row.get("uid")),
        "name": str(row.get("old_name") or row.get("db_author") or ""),
        "old_tier": str(row.get("old_tier") or ""),
        "old_dom": str(row.get("old_dom") or ""),
        "old_n": str(row.get("old_n") or ""),
        "full_n": str(row.get("full_n") or 0),
        "valid_n": str(row.get("valid_n") or 0),
        "valid_rate": (
            str(round((row.get("valid_n") or 0) / max((row.get("full_n") or 0), 1), 3))
        ),
        "recent_pub": str(row.get("recent_pub") or ""),
        "max_subs": str(row.get("max_subs") or 0),
        "med_views": str(int(row.get("med_views") or 0)),
        "med_eng": str(round(float(row.get("med_eng") or 0), 4)),
        "url": str(row.get("old_url") or ""),
        "recent_titles": " || ".join(str(title).replace("\n", " ")[:180] for title in titles if title),
        "category_samples": "|".join(f"{name}:{count}" for name, count in categories.most_common(5)),
    }


DB_AUDIT_QUERY = """
with src as (
  select case when source_info->>'source'='instagram' then 'ig' else source_info->>'source' end as platform,
         metadata->'properties'->>'author_id' as uid,
         metadata->>'author' as author,
         metadata->>'title' as title,
         left(coalesce(metadata->>'publish_time',''),10) as pub_date,
         metadata->'properties'->'categories' as categories,
         case when coalesce(metadata->'properties'->>'views','') ~ '^[0-9]+$' then (metadata->'properties'->>'views')::bigint
              when coalesce(metadata->'properties'->>'play_count','') ~ '^[0-9]+$' then (metadata->'properties'->>'play_count')::bigint else 0 end as views,
         case when coalesce(metadata->'properties'->>'likes','') ~ '^[0-9]+$' then (metadata->'properties'->>'likes')::bigint else 0 end as likes,
         case when coalesce(metadata->'properties'->>'comments','') ~ '^[0-9]+$' then (metadata->'properties'->>'comments')::bigint else 0 end as comments,
         case when coalesce(metadata->'properties'->>'shares','') ~ '^[0-9]+$' then (metadata->'properties'->>'shares')::bigint else 0 end as shares,
         case when coalesce(metadata->'properties'->>'subscribers','') ~ '^[0-9]+$' then (metadata->'properties'->>'subscribers')::bigint else 0 end as subscribers
  from assets
  where asset_type='video' and source_info->>'source' in ('yt','ig','instagram')
)
select c.platform, c.uid, c.old_name, c.old_tier, c.old_dom, c.old_n, c.old_url,
       count(s.*)::int as full_n,
       count(s.*) filter (where s.views>=100)::int as valid_n,
       max(s.pub_date) as recent_pub,
       max(s.subscribers)::bigint as max_subs,
       percentile_cont(0.5) within group (order by s.views) filter (where s.views>=100) as med_views,
       percentile_cont(0.5) within group (order by least((s.likes+s.comments+s.shares)::numeric/greatest(s.views,1),1)) filter (where s.views>=100) as med_eng,
       (array_agg(s.title order by s.pub_date desc) filter (where s.title is not null))[1:5] as recent_titles,
       (array_agg(s.categories order by s.pub_date desc) filter (where s.categories is not null))[1:5] as category_samples,
       max(s.author) as db_author
from cand c
left join src s on s.platform=c.platform and s.uid=c.uid
group by c.platform, c.uid, c.old_name, c.old_tier, c.old_dom, c.old_n, c.old_url
order by c.platform, c.old_tier desc, full_n desc
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-csv", type=Path, help="Existing full-library audit CSV to screen")
    parser.add_argument("--candidates-json", type=Path, help="enum_v5.json candidate pool")
    parser.add_argument("--dsn", default=os.getenv("ASSET_CENTER_DSN"), help="Postgres DSN")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for output CSVs")
    parser.add_argument(
        "--previous-state",
        type=Path,
        help="Previous current_subscription_state.csv for transition/import-action output",
    )
    parser.add_argument(
        "--run-date",
        default=date.today().isoformat(),
        help="Refresh date in YYYY-MM-DD; defaults to today",
    )
    parser.add_argument(
        "--active-days",
        type=int,
        default=30,
        help="Days for the active30 priority flag",
    )
    parser.add_argument(
        "--active-months",
        type=int,
        default=6,
        help="Calendar months for the hard activity gate",
    )
    parser.add_argument(
        "--active-month-days",
        type=int,
        help="Optional day-based override for the hard activity gate",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config(args.run_date, args.active_days, args.active_months, args.active_month_days)
    if args.audit_csv:
        rows = read_csv(args.audit_csv)
    elif args.candidates_json:
        if not args.dsn:
            raise SystemExit("Pass --dsn or ASSET_CENTER_DSN for --candidates-json mode")
        rows = build_db_audit_rows(args.candidates_json, args.dsn)
    else:
        raise SystemExit("Pass either --audit-csv or --candidates-json")

    write_outputs(rows, args.output_dir, config, args.previous_state)
    print(f"wrote subscription screening outputs to {args.output_dir}")


if __name__ == "__main__":
    main()
