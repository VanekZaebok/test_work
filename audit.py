# -*- coding: utf-8 -*-
"""
Аудит report.csv против сырых отгрузок works.csv.

Пересобирает флаиты из биллинга с учётом:
- splitted payments (несколько строк на месяц),
- меток стоп,
- переименований проектов (projects_history),
- смены услуги (service_changes) и исторических сроков (service_terms),
- даты актуальности выгрузки (макс. месяц в works).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEP = ";"
AS_OF: date | None = None  # заполняется из works


def parse_month(s: str) -> date:
    y, m, *_ = s.split("-")
    return date(int(y), int(m), 1)


def add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def iso(d: date) -> str:
    return d.isoformat()


def read_csv(name: str) -> list[dict]:
    with open(ROOT / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=SEP))


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
works_raw = read_csv("works.csv")
projects = {int(r["project_id"]): r for r in read_csv("projects.csv")}
history = read_csv("projects_history.csv")
svc_changes = read_csv("service_changes.csv")
svc_terms = {r["service_type"]: int(r["term_months"]) for r in read_csv("service_terms.csv")}
report = read_csv("report.csv")


def service_at(project_id: int, month: date) -> str:
    """Услуга, действовавшая в месяц (не текущая из справочника)."""
    type_ = projects[project_id]["service_type"]
    changes = [c for c in svc_changes if int(c["project_id"]) == project_id]
    for c in sorted(changes, key=lambda x: parse_month(x["month"]), reverse=True):
        if month < parse_month(c["month"]):
            type_ = c["old_service_type"]
        else:
            break
    return type_


# month → {amount, labels, parts}
def aggregate_project(pid: int) -> dict[date, dict]:
    months: dict[date, dict] = {}
    for r in works_raw:
        if int(r["project_id"]) != pid:
            continue
        m = parse_month(r["month"])
        rec = months.setdefault(m, {"amount": 0.0, "labels": [], "parts": []})
        rec["amount"] += float(r["amount"])
        if (r.get("label") or "").strip():
            rec["labels"].append(r["label"].strip())
        if (r.get("part") or "").strip():
            rec["parts"].append(r["part"].strip())
    return months


def is_stop(labels: list[str]) -> bool:
    return any(x.lower() == "стоп" for x in labels)


# ---------------------------------------------------------------------------
# Client identity from history
# ---------------------------------------------------------------------------
paid_months = {pid: {m for m, rec in aggregate_project(pid).items() if rec["amount"] > 0} for pid in projects}

merge_groups: list[tuple[int, int, date, str]] = []
rejected_merges: list[dict] = []
for h in history:
    old, new = int(h["project_id"]), int(h["new_project_id"])
    when = parse_month(h["month"])
    overlap = paid_months.get(old, set()) & paid_months.get(new, set())
    if overlap:
        rejected_merges.append(
            {
                "old": old,
                "new": new,
                "month": when,
                "overlap": sorted(overlap),
                "reason": "параллельные оплаченные месяцы — не склеиваем в одного клиента",
            }
        )
    else:
        merge_groups.append((old, new, when, f"{h['project_name']} → {h['new_project_name']}"))

# client_id = surviving (new) id for merged; else project_id
parent: dict[int, int] = {pid: pid for pid in projects}
for old, new, *_ in merge_groups:
    parent[old] = new
    parent[new] = new

clients: dict[int, list[int]] = defaultdict(list)
for pid in sorted(projects):
    clients[parent[pid]].append(pid)


# ---------------------------------------------------------------------------
# Combined billing timeline per client
# ---------------------------------------------------------------------------
@dataclass
class MonthRow:
    month: date
    amount: float
    labels: list[str]
    parts: list[str]
    project_ids: list[int]
    service: str
    term: int


def client_timeline(cids: list[int]) -> dict[date, MonthRow]:
    by_month: dict[date, MonthRow] = {}
    for pid in cids:
        for m, rec in aggregate_project(pid).items():
            svc = service_at(pid, m)
            if m not in by_month:
                by_month[m] = MonthRow(
                    month=m,
                    amount=rec["amount"],
                    labels=list(rec["labels"]),
                    parts=list(rec["parts"]),
                    project_ids=[pid],
                    service=svc,
                    term=svc_terms[svc],
                )
            else:
                row = by_month[m]
                row.amount += rec["amount"]
                row.labels.extend(rec["labels"])
                row.parts.extend(rec["parts"])
                if pid not in row.project_ids:
                    row.project_ids.append(pid)
                # if parallel projects disagree on service — keep first, flag later
    return dict(sorted(by_month.items()))


# ---------------------------------------------------------------------------
# Flight reconstruction
# ---------------------------------------------------------------------------
@dataclass
class Flight:
    client_id: int
    project_ids: str
    project_name: str
    service_type: str
    term_months: int
    flight_no: int
    flight_start: date
    flight_end: date
    last_active_month: date
    status: str
    report_generated_at: str
    comment: str
    flag: str  # ok | fixed | спорно
    extra: dict = field(default_factory=dict)


def last_paid(timeline: dict[date, MonthRow], start: date, end: date) -> date | None:
    paid = [m for m, r in timeline.items() if start <= m <= end and r.amount > 0]
    return max(paid) if paid else None


def reconstruct(as_of: date) -> list[Flight]:
    flights: list[Flight] = []
    as_of_s = iso(as_of)

    for client_id, pids in sorted(clients.items()):
        timeline = client_timeline(pids)
        if not timeline:
            continue

        project_type = projects[pids[-1]]["project_type"]
        ids_str = "|".join(str(p) for p in pids)

        # one-shot
        if project_type == "Разовый":
            m = min(timeline)
            row = timeline[m]
            flights.append(
                Flight(
                    client_id=client_id,
                    project_ids=ids_str,
                    project_name=projects[pids[-1]]["project_name"],
                    service_type=row.service,
                    term_months=row.term,
                    flight_no=1,
                    flight_start=m,
                    flight_end=m,
                    last_active_month=m,
                    status="завершился (разовые работы)",
                    report_generated_at=as_of_s,
                    comment="Разовый проект, в отчёт попал верно.",
                    flag="ok",
                )
            )
            continue

        used: set[date] = set()
        flight_no = 0
        cursor = min(m for m, r in timeline.items() if r.amount > 0)

        while cursor is not None:
            row0 = timeline[cursor]
            svc = row0.service
            term = row0.term
            planned_end = add_months(cursor, term - 1)
            flight_no += 1

            # Walk months inside the planned window. Close early on service change
            # if the new service actually has a paid month.
            actual_end = planned_end
            stop_month = None
            resumed_after_stop = False
            service_change_close = False

            m = cursor
            while m <= planned_end:
                rec = timeline.get(m)
                if rec and rec.amount > 0 and rec.service != svc:
                    actual_end = add_months(m, -1)
                    service_change_close = True
                    break
                if rec and is_stop(rec.labels):
                    stop_month = m
                    # resume = any paid month after the stop, still in/after this flight
                    after = [
                        mm
                        for mm, rr in timeline.items()
                        if mm > m and rr.amount > 0
                    ]
                    if after and after[0] <= planned_end:
                        resumed_after_stop = True
                    elif after and after[0] == add_months(m, 1):
                        resumed_after_stop = True
                m = add_months(m, 1)

            last = last_paid(timeline, cursor, actual_end)
            assert last is not None

            comments: list[str] = []
            flag = "ok"
            status = None

            if stop_month and not resumed_after_stop:
                status = "отвал"
                comments.append(
                    f"Метка стоп {iso(stop_month)[:7]}, последняя ненулевая отгрузка {iso(last)[:7]}."
                )
            elif stop_month and resumed_after_stop:
                flag = "спорно"
                first_back = min(
                    mm for mm, rr in timeline.items() if mm > stop_month and rr.amount > 0
                )
                comments.append(
                    f"Метка стоп {iso(stop_month)[:7]}, но уже в {iso(first_back)[:7]} "
                    f"снова ненулевая отгрузка — как отвал не считаем (похоже на сбой или отмену стопа)."
                )

            next_paid = min(
                (mm for mm, rr in timeline.items() if mm > actual_end and rr.amount > 0),
                default=None,
            )
            next_month = add_months(actual_end, 1)
            in_progress = actual_end > as_of or (last == as_of and actual_end >= as_of)

            paid_in_flight = [
                mm for mm, rr in timeline.items() if cursor <= mm <= actual_end and rr.amount > 0
            ]
            expected_months = term if not service_change_close else (
                (actual_end.year - cursor.year) * 12 + (actual_end.month - cursor.month) + 1
            )
            silent_gap = False
            # дыра внутри окна, только по уже наступившим месяцам выгрузки
            mm = cursor
            while mm <= min(actual_end, as_of):
                rec = timeline.get(mm)
                if rec is None or rec.amount <= 0:
                    if not (rec and is_stop(rec.labels)):
                        silent_gap = True
                mm = add_months(mm, 1)

            if status == "отвал":
                pass
            elif service_change_close:
                status = "смена услуги"
                flag = "fixed"
                comments.append(
                    f"С {iso(add_months(actual_end, 1))[:7]} услуга сменилась, отгрузки непрерывны. "
                    f"Исходный срок услуги «{svc}» — {term} мес, фактически закрыли раньше. "
                    f"Это не отток: клиент остался."
                )
            elif in_progress and last >= add_months(as_of, -1):
                status = "активен"
                flag = "fixed"
                comments.append(
                    f"Флайт ещё не закончился относительно выгрузки (as of {iso(as_of)[:7]})."
                )
            elif last < actual_end and not (stop_month and not resumed_after_stop):
                # не дотянули до планового конца
                status = "прерван"
                flag = "fixed"
                n_paid = len(paid_in_flight)
                comments.append(
                    f"Отгрузки оборвались на {iso(last)[:7]} при плановом конце {iso(actual_end)[:7]} "
                    f"({n_paid} из {term} мес), метки стоп нет. В исходном отчёте такие случаи "
                    f"часто записаны как непролонгировано — это занижает «отвал/прерывание»."
                )
            elif next_paid == next_month:
                nxt = timeline[next_paid]
                if nxt.service != svc:
                    status = "смена услуги"
                    flag = "fixed"
                    comments.append("Следующий месяц есть, но уже другая услуга.")
                else:
                    status = "пролонгировано"
            elif next_paid is None:
                # completed the window, nothing after
                if add_months(actual_end, 1) > as_of:
                    status = "неизвестно"
                    flag = "fixed"
                    comments.append("Флайт только что закончился, следующего месяца в выгрузке нет.")
                else:
                    status = "непролонгировано"
            else:
                # next paid exists but with a gap — not a prolongation of this flight
                status = "непролонгировано"
                flag = "fixed"
                comments.append(
                    f"Следующая отгрузка только в {iso(next_paid)[:7]}, это новый заход, не продление."
                )

            if silent_gap and status not in ("отвал", "прерван"):
                comments.append("Внутри окна флайта есть месяц без отгрузки.")
                flag = "спорно" if flag == "ok" else flag

            # name: project that carried the last paid month of this flight
            last_pids = timeline[last].project_ids
            name = projects[last_pids[-1]]["project_name"]
            # if flight started on another project, prefer that name for f1 of a rename
            start_pids = timeline[cursor].project_ids
            if start_pids[0] != last_pids[-1]:
                name = projects[last_pids[-1]]["project_name"]

            extra_comment = ""
            if len(pids) > 1:
                extra_comment = (
                    f" Склеены project_id {ids_str}: в history переименование без "
                    f"перекрытия оплаченных месяцев — это один клиент."
                )
            comments_s = " ".join(comments) + extra_comment

            # mark known-good simple cases
            if status in ("пролонгировано", "непролонгировано", "отвал", "завершился (разовые работы)") and flag == "ok":
                pass

            flights.append(
                Flight(
                    client_id=client_id,
                    project_ids=ids_str,
                    project_name=name,
                    service_type=svc,
                    term_months=term,
                    flight_no=flight_no,
                    flight_start=cursor,
                    flight_end=actual_end,
                    last_active_month=last,
                    status=status,
                    report_generated_at=as_of_s,
                    comment=comments_s.strip(),
                    flag=flag,
                    extra={
                        "stop_month": iso(stop_month) if stop_month else "",
                        "resumed_after_stop": resumed_after_stop,
                        "service_change_close": service_change_close,
                        "paid_months": len(paid_in_flight),
                    },
                )
            )

            for mm in paid_in_flight:
                used.add(mm)
            if stop_month:
                used.add(stop_month)

            if status == "отвал":
                cursor = next_paid  # may start a later flight if they return much later
            elif service_change_close or status in ("пролонгировано", "смена услуги"):
                cursor = next_month if next_month in timeline or (next_paid == next_month) else next_paid
            elif status == "прерван":
                cursor = next_paid  # only if they return after a hole, as a new flight
            elif status == "активен":
                cursor = None
            else:
                cursor = next_paid

            if cursor is not None and cursor in used:
                # advance to first unused paid month
                later = [mm for mm, rr in timeline.items() if mm > cursor and rr.amount > 0 and mm not in used]
                cursor = min(later) if later else None

    return flights


def annotate(flights: list[Flight]) -> None:
    """Дописываем сверку с исходным отчётом и отклонённые склейки."""
    rejected_by_id = {}
    for r in rejected_merges:
        rejected_by_id[r["old"]] = r
        rejected_by_id[r["new"]] = r

    original_by_client: dict[int, list[dict]] = defaultdict(list)
    for row in report:
        original_by_client[int(row["client_id"])].append(row)

    for fl in flights:
        bits = [fl.comment] if fl.comment else []

        rej = rejected_by_id.get(fl.client_id)
        if rej:
            ov = ", ".join(iso(m)[:7] for m in rej["overlap"])
            bits.append(
                f"В projects_history указано переименование {rej['old']}→{rej['new']} "
                f"с {iso(rej['month'])[:7]}, но оба проекта отгружались параллельно ({ov}). "
                f"Как одного клиента не склеиваем: это два договора, не ренейм. "
                f"В исходном отчёте склеены в client_id 321, из-за этого первый флайт "
                f"ложно помечен как пролонгировано."
            )
            fl.flag = "спорно"

        orig_rows = original_by_client.get(fl.client_id, [])
        # Gamma lived under 311 in the original too; 320 did not exist as client_id
        if not orig_rows:
            for cid, rows in original_by_client.items():
                pids = {int(x) for row in rows for x in row["project_ids"].split("|")}
                if fl.client_id in pids:
                    orig_rows = rows
                    break

        matched = None
        for row in orig_rows:
            if parse_month(row["flight_start"]) == fl.flight_start:
                matched = row
                break

        if matched:
            diffs = []
            if matched["status"] != fl.status:
                diffs.append(f"статус «{matched['status']}» → «{fl.status}»")
            if matched["service_type"] != fl.service_type:
                diffs.append(f"услуга «{matched['service_type']}» → «{fl.service_type}»")
            if int(matched["term_months"]) != fl.term_months:
                diffs.append(f"срок {matched['term_months']} → {fl.term_months}")
            if parse_month(matched["flight_end"]) != fl.flight_end:
                diffs.append(f"конец {matched['flight_end'][:7]} → {iso(fl.flight_end)[:7]}")
            if parse_month(matched["last_active_month"]) != fl.last_active_month:
                diffs.append(f"last_active {matched['last_active_month'][:7]} → {iso(fl.last_active_month)[:7]}")
            if diffs:
                bits.append("В отчёте было: " + "; ".join(diffs) + ".")
                if fl.flag == "ok":
                    fl.flag = "fixed"
            elif fl.flag == "ok":
                bits.append("Строка отчёта по границам и статусу верна.")
        else:
            bits.append("Этой строки в исходном отчёте не было (или флайт нарезан иначе).")
            if fl.flag == "ok":
                fl.flag = "fixed"

        fl.comment = " ".join(b for b in bits if b).strip()


def main() -> None:
    global AS_OF
    AS_OF = max(parse_month(r["month"]) for r in works_raw)
    flights = reconstruct(AS_OF)
    annotate(flights)

    out_fields = [
        "client_id",
        "project_ids",
        "project_name",
        "service_type",
        "term_months",
        "flight_no",
        "flight_start",
        "flight_end",
        "last_active_month",
        "status",
        "report_generated_at",
        "comment",
        "flag",
    ]
    out_path = ROOT / "report_fixed.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, delimiter=SEP)
        w.writeheader()
        for fl in flights:
            w.writerow(
                {
                    "client_id": fl.client_id,
                    "project_ids": fl.project_ids,
                    "project_name": fl.project_name,
                    "service_type": fl.service_type,
                    "term_months": fl.term_months,
                    "flight_no": fl.flight_no,
                    "flight_start": iso(fl.flight_start),
                    "flight_end": iso(fl.flight_end),
                    "last_active_month": iso(fl.last_active_month),
                    "status": fl.status,
                    "report_generated_at": fl.report_generated_at,
                    "comment": fl.comment,
                    "flag": fl.flag,
                }
            )

    print(f"as_of (max month in works) = {iso(AS_OF)}")
    print(f"unique project_id = {len(projects)}")
    print(f"merged pairs accepted = {merge_groups}")
    print(f"rejected merges = {rejected_merges}")
    print(f"unique clients in reconstruction = {len(clients)}")
    print(f"report unique clients = {len({r['client_id'] for r in report})}")
    print(f"flights written = {len(flights)} → {out_path.name}")
    print()
    print(f"{'cid':>4} {'ids':8} {'name':20} {'svc':22} {'t':>2} f{'n'} {'start':7} {'end':7} {'last':7} { 'status':28} { 'flag'}")
    for fl in flights:
        print(
            f"{fl.client_id:4} {fl.project_ids:8} {fl.project_name:20} {fl.service_type:22} "
            f"{fl.term_months:2} f{fl.flight_no} {iso(fl.flight_start)[:7]} {iso(fl.flight_end)[:7]} "
            f"{iso(fl.last_active_month)[:7]} {fl.status:28} {fl.flag}"
        )
        if fl.comment:
            print(f"       {fl.comment}")

    from collections import Counter
    print("\nSTATUS COUNTS original vs fixed")
    orig = Counter(r["status"] for r in report)
    fix = Counter(fl.status for fl in flights)
    keys = sorted(set(orig) | set(fix))
    for k in keys:
        print(f"  {k:32} report={orig[k]:2}  fixed={fix[k]:2}")
    print("\nflags", Counter(fl.flag for fl in flights))


if __name__ == "__main__":
    main()
