from .runtime_state import *

# ── Cron Scheduler ──

# Cron jobs are stored separately from conversation history. When a job fires,
# it becomes a scheduled prompt that is injected back into the same agent loop.
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
ONCE_DURABLE_PATH = WORKDIR / ".scheduled_once_tasks.json"


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    kind: str = "cron"


@dataclass
class OnceJob:
    id: str
    run_at: float
    prompt: str
    durable: bool
    kind: str = "once"


scheduled_jobs: dict[str, CronJob] = {}
scheduled_once_jobs: dict[str, OnceJob] = {}
cron_queue: list = []
cron_lock = threading.Lock()
_last_fired: dict[str, str] = {}


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)
    if not (m and h and month_ok):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def _looks_like_one_time_request(text: str) -> bool:
    lowered = str(text).lower()
    patterns = [
        r"\d+\s*(秒|second|seconds)\s*(后|later)?",
        r"\d+\s*(分钟|minute|minutes)\s*(后|later)",
        r"\d+\s*(小时|hour|hours)\s*(后|later)",
        r"\b(in|after)\s+\d+\s*(seconds|minutes|hours)\b",
        r"明天",
        r"\btomorrow\b",
        r"只执行一次",
        r"\bone[- ]?time\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def save_durable_jobs():
    durable = [asdict(job) for job in scheduled_jobs.values() if job.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def save_durable_once_jobs():
    durable = [asdict(job) for job in scheduled_once_jobs.values() if job.durable]
    ONCE_DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    if not DURABLE_PATH.exists():
        return
    try:
        for item in json.loads(DURABLE_PATH.read_text()):
            job = CronJob(**item)
            if not validate_cron(job.cron):
                scheduled_jobs[job.id] = job
    except Exception:
        pass


def load_durable_once_jobs():
    if not ONCE_DURABLE_PATH.exists():
        return
    try:
        now = time.time()
        changed = False
        for item in json.loads(ONCE_DURABLE_PATH.read_text()):
            job = OnceJob(**item)
            if job.run_at > now:
                scheduled_once_jobs[job.id] = job
            else:
                changed = True
        if changed:
            save_durable_once_jobs()
    except Exception:
        pass


def schedule_job(cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True) -> CronJob | str:
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable)
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    return job


def _parse_run_at(run_at: str) -> float | str:
    text = str(run_at).strip()
    if not text:
        return "run_at is empty"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return ("Invalid run_at. Use ISO/local format like "
                "2026-07-06 16:30 or 2026-07-06T16:30:00")
    return value.timestamp()


def schedule_once(prompt: str, delay_seconds=None,
                  run_at: str | None = None,
                  durable: bool = True) -> OnceJob | str:
    if delay_seconds is None and not run_at:
        return "Provide delay_seconds or run_at"
    if delay_seconds is not None and run_at:
        return "Provide only one of delay_seconds or run_at"
    if delay_seconds is not None:
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError):
            return "delay_seconds must be a number"
        if delay < 0:
            return "delay_seconds must be >= 0"
        target = time.time() + delay
    else:
        parsed = _parse_run_at(run_at)
        if isinstance(parsed, str):
            return parsed
        target = parsed
    if target <= time.time():
        return "Target time is in the past"
    job = OnceJob(
        id=f"once_{random.randint(0, 999999):06d}",
        run_at=target, prompt=prompt, durable=durable)
    with cron_lock:
        scheduled_once_jobs[job.id] = job
    if durable:
        save_durable_once_jobs()
    return job


def cancel_job(job_id: str) -> str:
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    return f"Cancelled {job_id}"


def cancel_once_job(job_id: str) -> str:
    with cron_lock:
        job = scheduled_once_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_once_jobs()
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    while True:
        time.sleep(1)
        now = datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now) and _last_fired.get(job.id) != marker:
                        cron_queue.append(job)
                        _last_fired[job.id] = marker
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")
            for job in list(scheduled_once_jobs.values()):
                try:
                    if job.run_at <= time.time():
                        scheduled_once_jobs.pop(job.id, None)
                        cron_queue.append(job)
                        if job.durable:
                            save_durable_once_jobs()
                except Exception as e:
                    print(f"  \033[31m[once error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    if recurring is False:
        return ("Error: schedule_cron is only for repeating 5-field cron tasks. "
                "Use schedule_once for one-time or seconds-level tasks.")
    if _looks_like_one_time_request(prompt):
        return ("Error: schedule_cron is only for repeating 5-field cron tasks. "
                "Use schedule_once for one-time or seconds-level tasks.")
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' -> {prompt}"


def run_schedule_once(prompt: str, delay_seconds=None,
                      run_at: str | None = None,
                      durable: bool = True) -> str:
    result = schedule_once(prompt, delay_seconds, run_at, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    when = datetime.fromtimestamp(result.run_at).isoformat(timespec="seconds")
    return f"Scheduled {result.id}: once at {when} -> {prompt}"


def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."
    return "\n".join(
        f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs)


def run_cancel_cron(job_id: str) -> str:
    if job_id.startswith("once_"):
        return cancel_once_job(job_id)
    result = cancel_job(job_id)
    if result.endswith("not found"):
        return cancel_once_job(job_id)
    return result


load_durable_jobs()
load_durable_once_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()



import sys as _sys
from . import runtime_state as _runtime_state
_runtime_state.register_module(_sys.modules[__name__])
_runtime_state.export_public(globals())
