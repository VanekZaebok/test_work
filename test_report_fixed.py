# -*- coding: utf-8 -*-
"""
Независимая проверка report_fixed.csv против сырых отгрузок.

Не вызывает reconstruct() из audit.py: правила закодированы здесь заново.
Если строка в report_fixed врёт — эти тесты падают.
"""
from __future__ import annotations

import csv
import sys
import unittest
from collections import defaultdict
from datetime import date
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEP = ";"

ALLOWED_STATUS = {
    "пролонгировано",
    "непролонгировано",
    "отвал",
    "прерван",
    "смена услуги",
    "активен",
    "неизвестно",
    "завершился (разовые работы)",
}
ALLOWED_FLAG = {"ok", "fixed", "спорно"}


def parse_month(s: str) -> date:
    y, m, *_ = s.split("-")
    return date(int(y), int(m), 1)


def add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def months_between(start: date, end: date) -> list[date]:
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def read_csv(name: str) -> list[dict]:
    with open(ROOT / name, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=SEP))


class Data:
    def __init__(self) -> None:
        self.fixed = read_csv("report_fixed.csv")
        self.works = read_csv("works.csv")
        self.projects = {int(r["project_id"]): r for r in read_csv("projects.csv")}
        self.history = read_csv("projects_history.csv")
        self.svc_changes = read_csv("service_changes.csv")
        self.svc_terms = {r["service_type"]: int(r["term_months"]) for r in read_csv("service_terms.csv")}
        self.as_of = max(parse_month(r["month"]) for r in self.works)
        self.by_pid: dict[int, dict[date, dict]] = defaultdict(dict)
        for r in self.works:
            pid = int(r["project_id"])
            m = parse_month(r["month"])
            rec = self.by_pid[pid].setdefault(m, {"amount": 0.0, "labels": []})
            rec["amount"] += float(r["amount"])
            if (r.get("label") or "").strip():
                rec["labels"].append(r["label"].strip())

    def service_at(self, pid: int, month: date) -> str:
        type_ = self.projects[pid]["service_type"]
        changes = [c for c in self.svc_changes if int(c["project_id"]) == pid]
        for c in sorted(changes, key=lambda x: parse_month(x["month"]), reverse=True):
            if month < parse_month(c["month"]):
                type_ = c["old_service_type"]
            else:
                break
        return type_

    def pids(self, row: dict) -> list[int]:
        return [int(x) for x in row["project_ids"].split("|")]

    def amount(self, pids: list[int], month: date) -> float:
        return sum(self.by_pid[p].get(month, {}).get("amount", 0.0) for p in pids)

    def labels(self, pids: list[int], month: date) -> list[str]:
        out: list[str] = []
        for p in pids:
            out.extend(self.by_pid[p].get(month, {}).get("labels", []))
        return out

    def last_paid(self, pids: list[int], start: date, end: date) -> date | None:
        paid = [m for m in months_between(start, end) if self.amount(pids, m) > 0]
        return max(paid) if paid else None

    def has_stop(self, pids: list[int], start: date, end: date) -> date | None:
        for m in months_between(start, end):
            if any(x.lower() == "стоп" for x in self.labels(pids, m)):
                return m
        return None


D = Data()


def row_key(r: dict) -> tuple:
    return (int(r["client_id"]), int(r["flight_no"]), r["flight_start"])


class TestSchema(unittest.TestCase):
    def test_required_columns(self):
        need = {
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
        }
        self.assertEqual(need, set(D.fixed[0].keys()))

    def test_row_count(self):
        self.assertEqual(len(D.fixed), 18)

    def test_status_and_flag_vocab(self):
        for r in D.fixed:
            self.assertIn(r["status"], ALLOWED_STATUS, r)
            self.assertIn(r["flag"], ALLOWED_FLAG, r)

    def test_dates_are_first_of_month(self):
        for r in D.fixed:
            for col in ("flight_start", "flight_end", "last_active_month", "report_generated_at"):
                d = parse_month(r[col])
                self.assertEqual(d.day, 1, f"{col} {r[col]}")
                self.assertEqual(d.isoformat(), r[col])

    def test_as_of_matches_works(self):
        for r in D.fixed:
            self.assertEqual(r["report_generated_at"], D.as_of.isoformat())
        self.assertEqual(D.as_of, date(2026, 1, 1))

    def test_flight_no_sequential_per_client(self):
        by_c: dict[int, list[int]] = defaultdict(list)
        for r in D.fixed:
            by_c[int(r["client_id"])].append(int(r["flight_no"]))
        for cid, nums in by_c.items():
            self.assertEqual(nums, list(range(1, len(nums) + 1)), f"client {cid}")


class TestTermAndService(unittest.TestCase):
    def test_term_matches_catalog_of_stated_service(self):
        for r in D.fixed:
            self.assertEqual(
                int(r["term_months"]),
                D.svc_terms[r["service_type"]],
                f"{r['project_name']} {r['service_type']}",
            )

    def test_service_type_is_historical_at_flight_start(self):
        for r in D.fixed:
            start = parse_month(r["flight_start"])
            pids = D.pids(r)
            # услуга на старте — у того project_id, который тогда отгружался
            live = [p for p in pids if D.amount([p], start) > 0] or pids
            actual = D.service_at(live[0], start)
            self.assertEqual(r["service_type"], actual, f"{r['client_id']} f{r['flight_no']}")

    def test_project_name_exists_in_directory(self):
        names = {p["project_name"] for p in D.projects.values()}
        for r in D.fixed:
            self.assertIn(r["project_name"], names)


class TestBillingWindows(unittest.TestCase):
    def test_start_before_end_and_last_inside(self):
        for r in D.fixed:
            a, b, last = (parse_month(r[c]) for c in ("flight_start", "flight_end", "last_active_month"))
            self.assertLessEqual(a, b, r)
            self.assertGreaterEqual(last, a, r)
            self.assertLessEqual(last, b, r)

    def test_last_active_is_last_nonzero_month_in_window(self):
        for r in D.fixed:
            pids = D.pids(r)
            start, end = parse_month(r["flight_start"]), parse_month(r["flight_end"])
            last = D.last_paid(pids, start, end)
            self.assertIsNotNone(last, r)
            self.assertEqual(last, parse_month(r["last_active_month"]), r)

    def test_flight_start_has_paid_activity(self):
        for r in D.fixed:
            pids = D.pids(r)
            start = parse_month(r["flight_start"])
            self.assertGreater(D.amount(pids, start), 0, r)

    def test_every_paid_month_belongs_to_exactly_one_flight_of_its_project(self):
        """Каждый ненулевой месяц каждого project_id покрыт ровно одним флайтом."""
        covered: dict[tuple[int, date], int] = defaultdict(int)
        for r in D.fixed:
            start, end = parse_month(r["flight_start"]), parse_month(r["flight_end"])
            for pid in D.pids(r):
                for m in months_between(start, end):
                    if D.amount([pid], m) > 0:
                        covered[(pid, m)] += 1
        expected = {
            (int(w["project_id"]), parse_month(w["month"]))
            for w in D.works
            if float(w["amount"]) > 0
        }
        self.assertEqual(set(covered), expected)
        for key, n in covered.items():
            self.assertEqual(n, 1, f"месяц попал в {n} флайтов: {key}")

    def test_flights_of_same_client_do_not_overlap(self):
        by_c: dict[int, list] = defaultdict(list)
        for r in D.fixed:
            by_c[int(r["client_id"])].append(r)
        for cid, rows in by_c.items():
            windows = sorted(
                (parse_month(r["flight_start"]), parse_month(r["flight_end"])) for r in rows
            )
            for i in range(len(windows) - 1):
                self.assertLess(
                    windows[i][1],
                    windows[i + 1][0],
                    f"пересечение окон клиента {cid}: {windows[i]} и {windows[i + 1]}",
                )


class TestStatusRules(unittest.TestCase):
    def test_prolonged_has_paid_month_right_after_end(self):
        for r in D.fixed:
            if r["status"] != "пролонгировано":
                continue
            pids = D.pids(r)
            end = parse_month(r["flight_end"])
            self.assertEqual(parse_month(r["last_active_month"]), end, r)
            nxt = add_months(end, 1)
            # следующий месяц может сидеть на другом project_id того же клиента
            client_rows = [x for x in D.fixed if x["client_id"] == r["client_id"]]
            all_pids = sorted({p for x in client_rows for p in D.pids(x)})
            self.assertGreater(D.amount(all_pids, nxt), 0, f"нет оплаты после {end}: {r}")

    def test_not_prolonged_completed_window_and_silence_after(self):
        for r in D.fixed:
            if r["status"] != "непролонгировано":
                continue
            pids = D.pids(r)
            start, end, last = (parse_month(r[c]) for c in ("flight_start", "flight_end", "last_active_month"))
            self.assertEqual(last, end, "непролонгировано только если флайт доигран")
            nxt = add_months(end, 1)
            self.assertLessEqual(nxt, D.as_of, "слишком рано для непролонгировано")
            self.assertEqual(D.amount(pids, nxt), 0, r)

    def test_churn_stop_has_label_and_no_resume(self):
        for r in D.fixed:
            if r["status"] != "отвал":
                continue
            pids = D.pids(r)
            start, end, last = (parse_month(r[c]) for c in ("flight_start", "flight_end", "last_active_month"))
            stop = D.has_stop(pids, start, end)
            self.assertIsNotNone(stop, r)
            self.assertLess(last, end, r)
            after = [m for m in months_between(add_months(stop, 1), end) if D.amount(pids, m) > 0]
            self.assertEqual(after, [], f"после стопа снова платили: {r}")

    def test_broken_off_before_planned_end_without_terminal_stop(self):
        for r in D.fixed:
            if r["status"] != "прерван":
                continue
            pids = D.pids(r)
            start, end, last = (parse_month(r[c]) for c in ("flight_start", "flight_end", "last_active_month"))
            self.assertLess(last, end, r)
            self.assertLess(end, D.as_of)  # окно уже в прошлом относительно выгрузки
            # либо стопа нет, либо после стопа была оплата (стоп не терминальный)
            stop = D.has_stop(pids, start, end)
            if stop is not None:
                resumed = any(D.amount(pids, m) > 0 for m in months_between(add_months(stop, 1), last))
                self.assertTrue(resumed, "стоп без возврата должен быть отвалом, не прерван")

    def test_active_window_not_finished(self):
        for r in D.fixed:
            if r["status"] != "активен":
                continue
            end, last = parse_month(r["flight_end"]), parse_month(r["last_active_month"])
            self.assertGreater(end, D.as_of, r)
            self.assertEqual(last, D.as_of, r)

    def test_service_change_next_month_is_other_service_and_paid(self):
        for r in D.fixed:
            if r["status"] != "смена услуги":
                continue
            pids = D.pids(r)
            end = parse_month(r["flight_end"])
            nxt = add_months(end, 1)
            self.assertGreater(D.amount(pids, nxt), 0, r)
            start_svc = r["service_type"]
            live = [p for p in pids if D.amount([p], nxt) > 0][0]
            self.assertNotEqual(D.service_at(live, nxt), start_svc, r)

    def test_one_shot_matches_directory(self):
        for r in D.fixed:
            if r["status"] != "завершился (разовые работы)":
                continue
            pid = D.pids(r)[0]
            self.assertEqual(D.projects[pid]["project_type"], "Разовый")
            self.assertEqual(r["flight_start"], r["flight_end"])
            self.assertEqual(r["flight_start"], r["last_active_month"])


class TestClientIdentity(unittest.TestCase):
    def test_unique_clients(self):
        self.assertEqual({int(r["client_id"]) for r in D.fixed}, {301, 302, 303, 304, 311, 320, 321, 330, 331, 340, 350, 351})
        self.assertEqual(len({r["client_id"] for r in D.fixed}), 12)

    def test_gamma_merged_without_overlap(self):
        gamma = [r for r in D.fixed if r["client_id"] == "311"]
        self.assertEqual(len(gamma), 2)
        self.assertEqual(gamma[0]["project_ids"], "310|311")
        paid_310 = {m for m, rec in D.by_pid[310].items() if rec["amount"] > 0}
        paid_311 = {m for m, rec in D.by_pid[311].items() if rec["amount"] > 0}
        self.assertFalse(paid_310 & paid_311)
        self.assertEqual(max(paid_310), date(2024, 6, 1))
        self.assertEqual(min(paid_311), date(2024, 7, 1))

    def test_delta_not_merged_because_of_overlap(self):
        paid_320 = {m for m, rec in D.by_pid[320].items() if rec["amount"] > 0}
        paid_321 = {m for m, rec in D.by_pid[321].items() if rec["amount"] > 0}
        overlap = paid_320 & paid_321
        self.assertEqual(overlap, {date(2024, 4, 1), date(2024, 5, 1), date(2024, 6, 1)})
        ids = {r["client_id"] for r in D.fixed if "320" in r["project_ids"] or "321" in r["project_ids"]}
        self.assertEqual(ids, {"320", "321"})
        self.assertTrue(all(r["project_ids"] in ("320", "321") for r in D.fixed if r["client_id"] in ("320", "321")))

    def test_all_work_projects_appear(self):
        in_fixed = {p for r in D.fixed for p in D.pids(r)}
        in_works = {int(r["project_id"]) for r in D.works}
        self.assertEqual(in_fixed, in_works)


class TestSplitPaymentsAndStops(unittest.TestCase):
    def test_aurora_february_is_one_month_not_two(self):
        rows = [w for w in D.works if w["project_id"] == "301" and w["month"].startswith("2024-02")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(float(x["amount"]) for x in rows), 150000)
        self.assertEqual(D.amount([301], date(2024, 2, 1)), 150000)

    def test_everest_stop_is_zero_july(self):
        rec = D.by_pid[303][date(2024, 7, 1)]
        self.assertEqual(rec["amount"], 0)
        self.assertTrue(any(x.lower() == "стоп" for x in rec["labels"]))

    def test_sigma_stop_is_followed_by_june_payment(self):
        self.assertEqual(D.by_pid[340][date(2024, 5, 1)]["amount"], 0)
        self.assertGreater(D.amount([340], date(2024, 6, 1)), 0)
        sigma_f1 = [r for r in D.fixed if r["client_id"] == "340" and r["flight_no"] == "1"][0]
        self.assertEqual(sigma_f1["status"], "пролонгировано")
        self.assertNotEqual(sigma_f1["status"], "отвал")


class TestExpectedRows(unittest.TestCase):
    """Золотой набор: каждая строка report_fixed должна совпасть поле в поле."""

    GOLD = [
        (301, "301", "Аврора Клиник", "Управление репутацией", 6, 1, "2024-01-01", "2024-06-01", "2024-06-01", "пролонгировано", "ok"),
        (301, "301", "Аврора Клиник", "Управление репутацией", 6, 2, "2024-07-01", "2024-12-01", "2024-12-01", "непролонгировано", "ok"),
        (302, "302", "БетаСтрой", "Крауд-маркетинг", 6, 1, "2024-02-01", "2024-07-01", "2024-07-01", "непролонгировано", "ok"),
        (303, "303", "Эверест Тур", "Управление репутацией", 6, 1, "2024-03-01", "2024-08-01", "2024-06-01", "отвал", "ok"),
        (304, "304", "Нова Медиа", "Разовый аудит", 1, 1, "2024-05-01", "2024-05-01", "2024-05-01", "завершился (разовые работы)", "ok"),
        (311, "310|311", "Гамма Ритейл", "Управление репутацией", 6, 1, "2024-01-01", "2024-06-01", "2024-06-01", "пролонгировано", "ok"),
        (311, "310|311", "Гамма Ритейл Про", "Управление репутацией", 6, 2, "2024-07-01", "2024-12-01", "2024-12-01", "непролонгировано", "ok"),
        (320, "320", "Дельта Пиар", "Управление репутацией", 6, 1, "2024-01-01", "2024-06-01", "2024-06-01", "непролонгировано", "спорно"),
        (321, "321", "Дельта Реклама", "Управление репутацией", 6, 1, "2024-04-01", "2024-09-01", "2024-09-01", "непролонгировано", "спорно"),
        (330, "330", "Кварц Медиа", "Крауд-маркетинг", 6, 1, "2024-01-01", "2024-06-01", "2024-06-01", "непролонгировано", "fixed"),
        (330, "330", "Кварц Медиа", "Мониторинг СМИ", 12, 2, "2024-11-01", "2025-10-01", "2025-02-01", "прерван", "fixed"),
        (331, "331", "Титан Строй", "Мониторинг СМИ", 12, 1, "2024-01-01", "2024-06-01", "2024-06-01", "смена услуги", "fixed"),
        (331, "331", "Титан Строй", "Управление репутацией", 6, 2, "2024-07-01", "2024-12-01", "2024-12-01", "непролонгировано", "ok"),
        (340, "340", "Сигма Групп", "Управление репутацией", 6, 1, "2024-01-01", "2024-06-01", "2024-06-01", "пролонгировано", "спорно"),
        (340, "340", "Сигма Групп", "Управление репутацией", 6, 2, "2024-07-01", "2024-12-01", "2024-09-01", "прерван", "fixed"),
        (350, "350", "Орион Диджитал", "Управление репутацией", 6, 1, "2025-03-01", "2025-08-01", "2025-08-01", "пролонгировано", "fixed"),
        (350, "350", "Орион Диджитал", "Управление репутацией", 6, 2, "2025-09-01", "2026-02-01", "2026-01-01", "активен", "fixed"),
        (351, "351", "Пульс Медиа", "Управление репутацией", 6, 1, "2025-03-01", "2025-08-01", "2025-08-01", "непролонгировано", "fixed"),
    ]

    def test_every_row_matches_gold(self):
        got = [
            (
                int(r["client_id"]),
                r["project_ids"],
                r["project_name"],
                r["service_type"],
                int(r["term_months"]),
                int(r["flight_no"]),
                r["flight_start"],
                r["flight_end"],
                r["last_active_month"],
                r["status"],
                r["flag"],
            )
            for r in D.fixed
        ]
        self.assertEqual(got, self.GOLD)

    def test_comments_nonempty_on_non_ok_rows(self):
        for r in D.fixed:
            if r["flag"] != "ok":
                self.assertTrue(r["comment"].strip(), r)


class TestOriginalReportErrorsAreGone(unittest.TestCase):
    def test_no_merged_delta_client_321_with_both_ids(self):
        for r in D.fixed:
            self.assertNotEqual(r["project_ids"], "320|321")

    def test_quartz_first_flight_is_not_12mo_monitoring(self):
        f1 = [r for r in D.fixed if r["client_id"] == "330" and r["flight_no"] == "1"][0]
        self.assertEqual(f1["service_type"], "Крауд-маркетинг")
        self.assertEqual(int(f1["term_months"]), 6)
        self.assertEqual(f1["status"], "непролонгировано")

    def test_titan_first_flight_not_reputation(self):
        f1 = [r for r in D.fixed if r["client_id"] == "331" and r["flight_no"] == "1"][0]
        self.assertEqual(f1["service_type"], "Мониторинг СМИ")
        self.assertEqual(f1["status"], "смена услуги")

    def test_sigma_flight_numbers_unique(self):
        nums = [r["flight_no"] for r in D.fixed if r["client_id"] == "340"]
        self.assertEqual(nums, ["1", "2"])

    def test_orion_and_pulse_are_no_longer_unknown(self):
        for r in D.fixed:
            if r["client_id"] in ("350", "351"):
                self.assertNotEqual(r["status"], "неизвестно")


def _write_audit(result: unittest.TestResult, tests_run: int) -> Path:
    lines = [
        "# Аудит тестов report_fixed.csv",
        "",
        "Прогон независимых тестов: каждая строка `report_fixed.csv` сверена с `works.csv`,",
        "`projects.csv`, `projects_history.csv`, `service_changes.csv`, `service_terms.csv`.",
        "Генератор `audit.py` в проверке **не участвует** — тесты кодируют правила заново.",
        "",
        f"- Запущено тестов: **{tests_run}**",
        f"- Успешно: **{tests_run - len(result.failures) - len(result.errors)}**",
        f"- Провалено: **{len(result.failures)}**",
        f"- Ошибок выполнения: **{len(result.errors)}**",
        "",
    ]
    if result.wasSuccessful():
        lines += [
            "## Вердикт",
            "",
            "**Все данные в `report_fixed.csv` верны** относительно сырых отгрузок и принятых правил.",
            "",
            "Покрыто:",
            "",
            "- схема и словари статусов/флагов;",
            "- 18 строк золотого набора (клиент, услуга, срок, границы, last_active, статус, флаг);",
            "- last_active = последний ненулевой месяц в окне флайта;",
            "- каждый ненулевой месяц каждого `project_id` входит ровно в один флайт;",
            "- окна одного клиента не пересекаются;",
            "- правила статусов: пролонгация, непролонгация, отвал, прерван, смена услуги, активен, разовые;",
            "- 12 клиентов; Гамма склеена без перекрытия; Дельта не склеена из-за параллельных отгрузок;",
            "- февраль Авроры (две части) считается одним месяцем;",
            "- ошибки исходного `report.csv` в фиксе отсутствуют.",
            "",
        ]
    else:
        lines += ["## Вердикт", "", "**Есть расхождения — report_fixed нельзя считать подтверждённым.**", ""]
        for kind, bag in (("FAIL", result.failures), ("ERROR", result.errors)):
            for test, tb in bag:
                lines += [f"### {kind}: {test}", "", "```", tb, "```", ""]

    lines += [
        "## Список проверок",
        "",
        "| Класс | Что проверяет |",
        "|---|---|",
        "| TestSchema | колонки, 18 строк, даты, порядковые номера флайтов |",
        "| TestTermAndService | срок из каталога, услуга на старте флайта, имя из справочника |",
        "| TestBillingWindows | last_active, покрытие всех оплаченных месяцев, нет пересечений |",
        "| TestStatusRules | семантика каждого статуса против отгрузок |",
        "| TestClientIdentity | 12 клиентов, Гамма/Дельта, все project_id из works |",
        "| TestSplitPaymentsAndStops | разбитая оплата 301, стоп 303, ложный отвал 340 |",
        "| TestExpectedRows | построчный золотой эталон |",
        "| TestOriginalReportErrorsAreGone | исходные ошибки отчёта не вернулись |",
        "",
        f"Команда: `python test_report_fixed.py`. as of выгрузки: {D.as_of.isoformat()}.",
        "",
    ]
    path = ROOT / "TEST_AUDIT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    buf = StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.stdout.write(buf.getvalue())
    audit_path = _write_audit(result, result.testsRun)
    print(f"\nАудит тестов записан: {audit_path.name}")
    sys.exit(0 if result.wasSuccessful() else 1)
