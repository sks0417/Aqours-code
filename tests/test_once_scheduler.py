import time

from codepilot_s20 import agent_loop, cron, tool_defs


def reset_scheduler(tmp_path):
    with cron.cron_lock:
        cron.scheduled_jobs.clear()
        cron.scheduled_once_jobs.clear()
        cron.cron_queue.clear()
        cron._last_fired.clear()
        cron.DURABLE_PATH = tmp_path / ".scheduled_tasks.json"
        cron.ONCE_DURABLE_PATH = tmp_path / ".scheduled_once_tasks.json"


def test_schedule_cron_is_for_repeating_tasks_only(tmp_path):
    reset_scheduler(tmp_path)

    result = cron.run_schedule_cron("30 9 * * *", "review")
    assert result.startswith("Scheduled cron_")
    assert len(cron.scheduled_jobs) == 1

    one_shot = cron.run_schedule_cron("30 9 * * *", "once", recurring=False)
    assert one_shot.startswith("Error:")
    assert "schedule_once" in one_shot

    seconds_prompt = cron.run_schedule_cron("30 9 * * *", "5 秒后提醒我")
    assert seconds_prompt.startswith("Error:")
    assert "schedule_once" in seconds_prompt


def test_schedule_once_seconds_fires_once_and_disappears(tmp_path):
    reset_scheduler(tmp_path)

    result = cron.schedule_once("answer trace once test", delay_seconds=0.01)
    assert not isinstance(result, str)
    assert result.id in cron.scheduled_once_jobs

    time.sleep(0.02)
    now = time.time()
    with cron.cron_lock:
        for job in list(cron.scheduled_once_jobs.values()):
            if job.run_at <= now:
                cron.scheduled_once_jobs.pop(job.id, None)
                cron.cron_queue.append(job)
                if job.durable:
                    cron.save_durable_once_jobs()

    fired = cron.consume_cron_queue()
    assert len(fired) == 1
    assert fired[0].id == result.id
    assert fired[0].kind == "once"
    assert result.id not in cron.scheduled_once_jobs
    assert cron.consume_cron_queue() == []


def test_schedule_once_is_consumed_by_scheduler_loop(tmp_path):
    reset_scheduler(tmp_path)
    cron.start_scheduler(load_durable=False)

    result = cron.schedule_once("answer trace once test", delay_seconds=1)
    assert not isinstance(result, str)

    time.sleep(2.2)
    fired = cron.consume_cron_queue()
    assert [job.id for job in fired] == [result.id]
    assert result.id not in cron.scheduled_once_jobs
    assert cron.consume_cron_queue() == []


def test_start_scheduler_is_idempotent(monkeypatch):
    old_thread = cron._scheduler_thread
    cron._scheduler_thread = None
    starts = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False

        def start(self):
            self.started = True
            starts.append(self)

        def is_alive(self):
            return self.started

    monkeypatch.setattr(cron.threading, "Thread", FakeThread)
    try:
        first = cron.start_scheduler(load_durable=False)
        second = cron.start_scheduler(load_durable=False)
    finally:
        cron._scheduler_thread = old_thread

    assert first is second
    assert len(starts) == 1


def test_schedule_once_minute_delay(tmp_path):
    reset_scheduler(tmp_path)

    result = cron.schedule_once("drink water", delay_seconds=60)
    assert not isinstance(result, str)
    assert 55 <= result.run_at - time.time() <= 65


def test_tool_descriptions_draw_clear_boundary():
    tools = {tool["name"]: tool for tool in tool_defs.BUILTIN_TOOLS}

    assert "schedule_once" in tools
    cron_desc = tools["schedule_cron"]["description"].lower()
    once_desc = tools["schedule_once"]["description"].lower()

    assert "5-field" in cron_desc
    assert "does not support seconds" in cron_desc
    assert "one-time" in once_desc
    assert "delay_seconds" in once_desc


def test_scheduled_once_prompt_prefix():
    class Job:
        kind = "once"
        prompt = "answer trace cron test"

    assert agent_loop.scheduled_prompt_text(Job()) == (
        "[Scheduled Once] answer trace cron test")
