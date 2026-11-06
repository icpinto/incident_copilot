"""SQL-backed issue triage and analytics over immutable uploaded observations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from .issue_capture import activity, new_issue
from .issue_models import IssueFilters, IssueUpdate
from .store import utcnow


def window(filters: IssueFilters) -> tuple[str, str, str]:
    end = filters.end_at or datetime.now(UTC)
    start = filters.start_at or end - timedelta(days=14)
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Use timezone-aware start_at and end_at timestamps.")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end or end - start > timedelta(days=366):
        raise ValueError("Choose a positive time window of at most 366 days.")
    return start.isoformat(), end.isoformat(), (start - (end - start)).isoformat()


def percent(current: int, previous: int) -> float | None:
    return round((current - previous) * 100 / previous, 1) if previous else None


def predicates(filters: IssueFilters, *, issue_id: str = "") -> tuple[str, list]:
    clauses, args = ["i.merged_into IS NULL"], []
    for column, value in (
        ("i.repository_id", filters.repository_id),
        ("i.owner", filters.owner),
        ("i.status", filters.status),
        ("i.id", issue_id),
    ):
        if value:
            clauses.append(f"{column}=?")
            args.append(value)
    if filters.q:
        clauses.append("instr(lower(i.title || ' ' || i.id),lower(?))>0")
        args.append(filters.q)
    for kind, value in (
        ("service", filters.service_name),
        ("error_class", filters.error_class),
        ("file", filters.file_path),
        ("function", filters.function_name),
    ):
        if value:
            clauses.append(
                "EXISTS (SELECT 1 FROM error_dimensions d WHERE d.occurrence_id=o.id AND d.kind=? AND d.value=?)"
            )
            args.extend([kind, value])
    if filters.incident_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM incident_error_batches ib WHERE ib.batch_id=o.batch_id AND ib.incident_id=?)"
        )
        args.append(filters.incident_id)
    return " AND ".join(clauses), args


def aggregated(
    filters: IssueFilters, bounds: tuple[str, str, str], *, issue_id: str = ""
) -> tuple[str, list]:
    start, end, previous = bounds
    where, args = predicates(filters, issue_id=issue_id)
    return (
        f"""SELECT i.*, COALESCE(json_extract(r.payload,'$.name'),i.repository_id) repository_name,
        MIN(o.effective_at) first_seen, MAX(o.effective_at) last_seen, COUNT(o.id) total_count,
        SUM(o.effective_at>=? AND o.effective_at<?) occurrence_count,
        SUM(o.effective_at>=? AND o.effective_at<?) previous_count,
        SUM(o.effective_at>=? AND o.effective_at<? AND o.source_timestamp=1) source_timestamp_count,
        SUM(o.effective_at>=? AND o.effective_at<? AND o.source_timestamp=0) fallback_timestamp_count
        FROM error_issues i JOIN error_occurrences o ON o.issue_id=i.id
        LEFT JOIN repositories r ON r.id=i.repository_id WHERE {where} GROUP BY i.id""",
        [start, end, previous, start, start, end, start, end, *args],
    )


def summary(row: sqlite3.Row | dict) -> dict:
    result = dict(row)
    result.pop("merged_into", None)
    result.update(
        change=result["occurrence_count"] - result["previous_count"],
        change_percent=percent(result["occurrence_count"], result["previous_count"]),
    )
    return result


class IssueService:
    def __init__(self, store, configuration):
        self.store = store
        self.configuration = configuration

    def public(self, value):
        return self.configuration.redact(value)

    @staticmethod
    def canonical(db, issue_id: str) -> sqlite3.Row:
        visited = set()
        while issue_id not in visited:
            visited.add(issue_id)
            row = db.execute("SELECT * FROM error_issues WHERE id=?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(issue_id)
            if not row["merged_into"]:
                return row
            issue_id = row["merged_into"]
        raise ValueError("Invalid issue redirect.")

    def list(
        self, filters: IssueFilters, sort: str = "occurrences", page: int = 1, page_size: int = 20
    ) -> dict:
        bounds = window(filters)
        sql, args = aggregated(filters, bounds)
        # Keep older entities discoverable; explicit time filters constrain membership.
        visible = "occurrence_count>0" if filters.start_at or filters.end_at else "1=1"
        order = {
            "occurrences": "occurrence_count DESC",
            "growth": "(occurrence_count-previous_count) DESC",
            "last_seen": "last_seen DESC",
        }[sort]
        with self.store.connection() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT COUNT(*) FROM ({sql}) WHERE {visible}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM ({sql}) WHERE {visible} ORDER BY {order},id LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
        return self.public(
            {
                "items": [summary(row) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "start_at": bounds[0],
                "end_at": bounds[1],
                "tracking_since": self.store.config("error_tracking")["tracking_since"],
            }
        )

    def detail(self, issue_id: str) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN")
            row = self.canonical(db, issue_id)
            sql, args = aggregated(IssueFilters(), window(IssueFilters()), issue_id=row["id"])
            result = summary(db.execute(sql, args).fetchone())
            result["redirected_from"] = issue_id if issue_id != row["id"] else None
            result["tracking_since"] = json.loads(
                db.execute(
                    "SELECT payload FROM configuration WHERE key='error_tracking'"
                ).fetchone()[0]
            )["tracking_since"]
        return self.public(result)

    def update(self, issue_id: str, update: IssueUpdate) -> dict:
        values = update.model_dump(exclude_none=True)
        if "title" in values:
            values["title"] = values["title"].strip()
            if not values["title"]:
                raise ValueError("Issue title cannot be blank.")
        if "owner" in values:
            values["owner"] = values["owner"].strip()
        values = self.public(values)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.canonical(db, issue_id)
            changes = {key: value for key, value in values.items() if value != row[key]}
            if changes:
                now = utcnow()
                if "status" in changes:
                    changes["resolved_at"] = now if changes["status"] == "resolved" else None
                db.execute(
                    "UPDATE error_issues SET "
                    + ",".join(f"{key}=?" for key in changes)
                    + ",updated_at=? WHERE id=?",
                    [*changes.values(), now, row["id"]],
                )
                activity(
                    db,
                    row["id"],
                    "updated",
                    now,
                    {
                        "changes": {
                            key: {"from": row[key], "to": value} for key, value in changes.items()
                        }
                    },
                )
            canonical_id = row["id"]
        return self.detail(canonical_id)

    def merge(self, target_id: str, source_ids: list[str]) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self.canonical(db, target_id)
            sources = [self.canonical(db, source_id) for source_id in source_ids]
            ids = [row["id"] for row in sources]
            if len(set(ids)) != len(ids) or target["id"] in ids:
                raise ValueError("Select distinct source issues other than the target.")
            if any(row["repository_id"] != target["repository_id"] for row in sources):
                raise ValueError("Only issues in the same repository can be merged.")
            now = utcnow()
            for source_id in ids:
                db.execute(
                    "UPDATE error_occurrences SET issue_id=? WHERE issue_id=?",
                    (target["id"], source_id),
                )
                db.execute(
                    "UPDATE error_fingerprints SET issue_id=? WHERE issue_id=?",
                    (target["id"], source_id),
                )
                db.execute(
                    "UPDATE error_issues SET merged_into=?,updated_at=? WHERE id=? OR merged_into=?",
                    (target["id"], now, source_id, source_id),
                )
                activity(db, source_id, "merged", now, {"target_issue_id": target["id"]})
            db.execute("UPDATE error_issues SET updated_at=? WHERE id=?", (now, target["id"]))
            activity(db, target["id"], "merged", now, {"source_issue_ids": ids})
            canonical_id = target["id"]
        return self.detail(canonical_id)

    def split(self, issue_id: str, occurrence_ids: list[str], title: str | None = None) -> dict:
        if len(set(occurrence_ids)) != len(occurrence_ids):
            raise ValueError("Select distinct occurrences.")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = self.canonical(db, issue_id)
            placeholders = ",".join("?" for _ in occurrence_ids)
            count = db.execute(
                f"SELECT COUNT(*) FROM error_occurrences WHERE issue_id=? AND id IN ({placeholders})",
                [source["id"], *occurrence_ids],
            ).fetchone()[0]
            total = db.execute(
                "SELECT COUNT(*) FROM error_occurrences WHERE issue_id=?", (source["id"],)
            ).fetchone()[0]
            if not occurrence_ids or count != len(occurrence_ids) or count >= total:
                raise ValueError("Select a nonempty proper subset of this issue's occurrences.")
            title = title.strip() if title is not None else f"Split from {source['title']}"[:200]
            if not title:
                raise ValueError("Issue title cannot be blank.")
            now = utcnow()
            new_id = new_issue(
                db, source["repository_id"], self.public(title), now, source["owner"]
            )
            db.execute(
                f"UPDATE error_occurrences SET issue_id=? WHERE id IN ({placeholders})",
                [new_id, *occurrence_ids],
            )
            db.execute("UPDATE error_issues SET updated_at=? WHERE id=?", (now, source["id"]))
            activity(
                db,
                source["id"],
                "split",
                now,
                {"target_issue_id": new_id, "occurrence_ids": occurrence_ids},
            )
            activity(
                db,
                new_id,
                "split",
                now,
                {"source_issue_id": source["id"], "occurrence_ids": occurrence_ids},
            )
        return self.detail(new_id)

    def occurrences(self, issue_id: str, filters: IssueFilters, page: int, page_size: int) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN")
            canonical = self.canonical(db, issue_id)
            where, args = predicates(filters, issue_id=canonical["id"])
            if filters.start_at or filters.end_at:
                start, end, _ = window(filters)
                where += " AND o.effective_at>=? AND o.effective_at<?"
                args.extend([start, end])
            base = f"FROM error_occurrences o JOIN error_issues i ON i.id=o.issue_id WHERE {where}"
            total = db.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT o.* {base} ORDER BY o.effective_at DESC,o.id LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for row in rows:
                links = db.execute(
                    """SELECT i.id,json_extract(i.payload,'$.title') title FROM incidents i
                    JOIN incident_error_batches ib ON ib.incident_id=i.id WHERE ib.batch_id=?
                    ORDER BY i.created_at,i.id""",
                    (row["batch_id"],),
                ).fetchall()
                items.append(
                    {
                        "id": row["id"],
                        "issue_id": row["issue_id"],
                        "batch_id": row["batch_id"],
                        "event_id": row["event_id"],
                        "effective_at": row["effective_at"],
                        "timestamp_source": "event" if row["source_timestamp"] else "submission",
                        "event": json.loads(row["payload"]),
                        "investigations": [dict(link) for link in links],
                    }
                )
        return self.public({"items": items, "total": total, "page": page, "page_size": page_size})

    def history(self, issue_id: str, page: int, page_size: int) -> dict:
        with self.store.connection() as db:
            db.execute("BEGIN")
            canonical = self.canonical(db, issue_id)
            # Include activity from aliases so merged history remains reviewable.
            where = "issue_id IN (SELECT id FROM error_issues WHERE id=? OR merged_into=?)"
            args = [canonical["id"], canonical["id"]]
            total = db.execute(
                f"SELECT COUNT(*) FROM error_issue_activity WHERE {where}", args
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM error_issue_activity WHERE {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
        return self.public(
            {
                "items": [{**dict(row), "details": json.loads(row["payload"])} for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    def intelligence(self, filters: IssueFilters, dimension: str) -> dict:
        bounds = window(filters)
        start, end, previous = bounds
        sql, args = aggregated(filters, bounds)
        where, occurrence_args = predicates(filters)
        base = f"FROM error_occurrences o JOIN error_issues i ON i.id=o.issue_id WHERE {where} AND o.effective_at>=? AND o.effective_at<?"
        current_args = [*occurrence_args, start, end]
        with self.store.connection() as db:
            db.execute("BEGIN")
            metrics = db.execute(
                f"""SELECT COALESCE(SUM(occurrence_count),0) occurrences,
                COALESCE(SUM(previous_count),0) previous_occurrences,
                SUM(occurrence_count>0) issues,
                SUM(occurrence_count>0 AND status IN ('open','acknowledged','investigating')) active_issues,
                SUM(occurrence_count>0 AND first_seen>=? AND first_seen<?) new_issues,
                COALESCE(SUM(source_timestamp_count),0) source_timestamp_count,
                COALESCE(SUM(fallback_timestamp_count),0) fallback_timestamp_count FROM ({sql})""",
                [start, end, *args],
            ).fetchone()
            metrics = {key: value or 0 for key, value in dict(metrics).items()}
            metrics.update(
                change=metrics["occurrences"] - metrics["previous_occurrences"],
                change_percent=percent(metrics["occurrences"], metrics["previous_occurrences"]),
            )
            metrics["reopened_issues"] = db.execute(
                f"""SELECT COUNT(DISTINCT o.issue_id) {base}
                AND EXISTS (SELECT 1 FROM error_issue_activity a WHERE a.issue_id IN
                (SELECT ai.id FROM error_issues ai WHERE ai.id=o.issue_id OR ai.merged_into=o.issue_id)
                AND a.kind='reopened' AND a.effective_at>=? AND a.effective_at<?)""",
                [*current_args, start, end],
            ).fetchone()[0]
            trend = {
                row["date"]: row["occurrences"]
                for row in db.execute(
                    f"SELECT substr(o.effective_at,1,10) date,COUNT(*) occurrences {base} GROUP BY date",
                    current_args,
                )
            }
            hotspots = db.execute(
                f"""SELECT d.value,COUNT(*) occurrences FROM error_dimensions d
                JOIN error_occurrences o ON o.id=d.occurrence_id JOIN error_issues i ON i.id=o.issue_id
                WHERE {where} AND o.effective_at>=? AND o.effective_at<? AND d.kind=?
                GROUP BY d.value ORDER BY occurrences DESC,d.value LIMIT 20""",
                [*current_args, dimension],
            ).fetchall()
            frequent = db.execute(
                f"SELECT * FROM ({sql}) WHERE occurrence_count>0 ORDER BY occurrence_count DESC,id LIMIT 5",
                args,
            ).fetchall()
            growing = db.execute(
                f"SELECT * FROM ({sql}) WHERE occurrence_count>previous_count ORDER BY (occurrence_count-previous_count) DESC,id LIMIT 5",
                args,
            ).fetchall()
            names = (
                {
                    row["id"]: json.loads(row["payload"])["name"]
                    for row in db.execute("SELECT id,payload FROM repositories")
                }
                if dimension == "repository"
                else {}
            )
            tracking = json.loads(
                db.execute(
                    "SELECT payload FROM configuration WHERE key='error_tracking'"
                ).fetchone()[0]
            )
        cursor = datetime.fromisoformat(start).date()
        last = (datetime.fromisoformat(end) - timedelta(microseconds=1)).date()
        trends = []
        while cursor <= last:
            trends.append(
                {"date": cursor.isoformat(), "occurrences": trend.get(cursor.isoformat(), 0)}
            )
            cursor += timedelta(days=1)
        return self.public(
            {
                "tracking_since": tracking["tracking_since"],
                "start_at": start,
                "end_at": end,
                "previous_start_at": previous,
                "dimension": dimension,
                "summary": metrics,
                "trends": trends,
                "hotspots": [
                    {**dict(row), "label": names.get(row["value"], row["value"])}
                    for row in hotspots
                ],
                "frequent_issues": [summary(row) for row in frequent],
                "growing_issues": [summary(row) for row in growing],
            }
        )
