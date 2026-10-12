"""AT21 scheduled research and cron semantics (FR20, FR21)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omca.contracts import TaskStatus
from omca.research.search import StaticSearchAdapter
from omca.scheduler import Cron, CronError
from omca.util import iso


def test_cron_evaluates_in_the_schedule_timezone():
    nxt = Cron("0 9 * * *").next_after(datetime(2026, 10, 12, 3, 0, tzinfo=UTC), "Asia/Kolkata")
    assert nxt == datetime(2026, 10, 12, 3, 30, tzinfo=UTC)  # 09:00 IST
    assert Cron("*/15 * * * 1-5").next_after(datetime(2026, 10, 17, 10, 0, tzinfo=UTC), "UTC") == \
        datetime(2026, 10, 19, 0, 0, tzinfo=UTC)  # Saturday -> Monday 00:00
    assert Cron("30 8 1 jan,jul *").next_after(datetime(2026, 2, 1, tzinfo=UTC), "UTC") == datetime(2026, 7, 1, 8, 30, tzinfo=UTC)
    # day-of-month OR day-of-week when both are restricted (standard cron)
    assert Cron("0 0 13 * fri").next_after(datetime(2026, 10, 12, tzinfo=UTC), "UTC") == datetime(2026, 10, 13, tzinfo=UTC)


def test_cron_skips_nonexistent_dst_times():
    # 02:30 does not exist in New York on 2026-03-08
    nxt = Cron("30 2 * * *").next_after(datetime(2026, 3, 8, 5, 0, tzinfo=UTC), "America/New_York")
    assert nxt == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_cron_rejects_invalid_expressions():
    for expr in ("* * *", "61 * * * *", "* 24 * * *", "*/0 * * * *"):
        with pytest.raises(CronError):
            Cron(expr)


SCHEDULE = {"id": "daily", "name": "Daily solar brief", "cron": "0 9 * * *", "timezone": "Asia/Kolkata",
            "workflow": "research_report", "goal_template": "Brief on rooftop solar capacity in India for {date}",
            "spending_limit": 20, "concurrency": "forbid", "missed_run_policy": "skip"}


@pytest.mark.acceptance("AT21")
async def test_scheduled_research_timezone_dedup_overlap_and_restart(make_system, site, brain):
    s = await make_system(worker=False, schedules=[SCHEDULE])
    s.coord.search.search_adapter = StaticSearchAdapter("fx", lambda q, n: [{"title": "Solar", "url": site.url("/research/solar.html")}])
    sched = s.coord.scheduler
    s.coord.db.execute("UPDATE schedules SET last_planned_at=? WHERE id='daily'", ("2026-10-12T04:00:00.000Z",))

    # 09:00 IST on 13 Oct = 03:30 UTC
    fired = sched.tick(datetime(2026, 10, 13, 3, 30, 20, tzinfo=UTC))
    assert len(fired) == 1
    run = sched.runs("daily")[0]
    assert run["planned_at"] == "2026-10-13T03:30:00.000Z" and run["dedup_key"] == "daily:2026-10-13T03:30:00.000Z"
    task = s.coord.tasks.require(run["task_id"])
    assert "2026-10-13" in task.goal and task.origin == "schedule:daily"
    assert task.request.budget.max_model_spend == 20

    # duplicates: the same tick, an external trigger for the same planned time, and a "restart" replay
    assert sched.tick(datetime(2026, 10, 13, 3, 31, tzinfo=UTC)) == []
    assert sched.fire_external("daily", "2026-10-13T03:30:00.000Z") is None
    s.coord.db.execute("UPDATE schedules SET last_planned_at=? WHERE id='daily'", ("2026-10-12T04:00:00.000Z",))
    assert sched.tick(datetime(2026, 10, 13, 3, 32, tzinfo=UTC)) == []
    assert len([t for t in s.coord.tasks.list(limit=50) if t.origin == "schedule:daily"]) == 1

    # the result reaches the owner
    t = await s.wait(task.id, timeout=90)
    assert t.status == TaskStatus.COMPLETED
    await s.flush()
    assert s.channel.documents()

    # overlap: while a run is paused (still active), the next firing is skipped, not stacked
    second = sched.tick(datetime(2026, 10, 14, 3, 30, 5, tzinfo=UTC))
    t2 = s.coord.tasks.require(sched.runs("daily")[-1]["task_id"])
    s.coord.pause_task(t2.id)
    sched.tick(datetime(2026, 10, 15, 3, 30, 5, tzinfo=UTC))
    runs = sched.runs("daily")
    assert second and runs[-1]["status"] == "SKIPPED_OVERLAP"
    s.coord.cancel_task(t2.id)

    # missed runs while down: policy "skip" records them and does not fire stale work
    sched.tick(datetime(2026, 10, 19, 5, 0, tzinfo=UTC))
    statuses = [r["status"] for r in sched.runs("daily")][-4:]
    assert statuses == ["MISSED"] * 4

    # pause all also stops schedules
    s.coord.scheduler.pause_all()
    assert sched.tick(datetime(2026, 10, 20, 3, 30, 5, tzinfo=UTC)) == []


@pytest.mark.acceptance("AT21")
async def test_run_latest_policy_fires_only_the_latest_missed_run(make_system, site, brain):
    s = await make_system(worker=False, schedules=[{**SCHEDULE, "id": "latest", "missed_run_policy": "run_latest"}])
    s.coord.search.search_adapter = StaticSearchAdapter("fx", lambda q, n: [])
    s.coord.db.execute("UPDATE schedules SET last_planned_at=? WHERE id='latest'", (iso(datetime(2026, 10, 12, 4, tzinfo=UTC)),))
    fired = s.coord.scheduler.tick(datetime(2026, 10, 16, 6, 0, tzinfo=UTC))
    runs = s.coord.scheduler.runs("latest")
    assert len(fired) == 1 and [r["status"] for r in runs] == ["MISSED", "MISSED", "MISSED", "CREATED"]
    assert runs[-1]["planned_at"] == "2026-10-16T03:30:00.000Z"
