import os
import asyncio
import datetime
import zmq
from .base import Status
from .core import bobsled
from .exceptions import AlreadyRunning


def parse_cron_segment(segment, star_equals):
    if segment == "*":
        return star_equals
    elif "," in segment:
        return sorted([int(n) for n in segment.split(",")])
    elif "-" in segment:
        start, end = segment.split("-")
        return list(range(int(start), int(end) + 1))
    elif segment.startswith("*/"):
        n = int(segment[2:])
        return list(range(0, 24, n))
    elif segment.isdigit():
        return [int(segment)]
    else:
        raise ValueError(segment)


def next_cron(cronstr, after=None):
    minute, hour, day, month, dow = cronstr.split()

    days = parse_cron_segment(day, list(range(1, 32)))
    minutes = parse_cron_segment(minute, list(range(60)))
    hours = parse_cron_segment(hour, list(range(24)))
    if dow != "?":
        dow = parse_cron_segment(dow, list(range(7)))

    # scheduling things that don't run every month not currently supported
    assert month == "*"

    if not after:
        after = datetime.datetime.utcnow()
    next_time = None

    for month in range(12):
        for day in days:
            for hour in hours:
                for minute in minutes:
                    try:
                        next_time = after.replace(
                            month=month,
                            day=day,
                            hour=hour,
                            minute=minute,
                            second=0,
                            microsecond=0,
                        )
                        # skip wrong days of the week
                        if dow != "?" and next_time.weekday() not in dow:
                            continue
                    except ValueError:
                        # if we made an invalid time due to month rollover, skip it
                        continue
                    if next_time > after:
                        return next_time

    # no next time this month, set to the first time but the next month
    if after.month == 12:
        month = 1
        year = after.year + 1
        next_time = next_time.replace(
            day=days[0], hour=hours[0], minute=minutes[0], month=month, year=year,
        )
        return next_time


def next_run_for_task(task):
    for trigger in task.triggers:
        return next_cron(trigger.cron)


# TODO: make these configurable
LOG_FILE = "/tmp/bobsled-beat.log"
UPDATE_CONFIG_MINS = 120


async def run_service():
    await bobsled.initialize()
    next_task_update = datetime.datetime.utcnow() + datetime.timedelta(
        minutes=UPDATE_CONFIG_MINS
    )

    port = os.environ.get("BOBSLED_BEAT_PORT", "1988")

    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://*:{port}")

    def _log(msg):
        socket.send_string(msg)
        print(msg)

    next_run_list = {}
    for task in await bobsled.storage.get_tasks():
        if not task.enabled:
            continue
        next_run = next_run_for_task(task)
        if next_run:
            next_run_list[task.name] = next_run
            _log(f"{task.name} next run at {next_run}")

    while True:
        pending = await bobsled.run.get_runs(status=Status.Pending)
        running = await bobsled.run.get_runs(status=Status.Running)
        utcnow = datetime.datetime.utcnow()

        _log(f"{utcnow}: pending={len(pending)} running={len(running)}")

        if utcnow > next_task_update:
            _log("updating config...")
            await bobsled.refresh_config()
            next_task_update = utcnow + datetime.timedelta(minutes=UPDATE_CONFIG_MINS)
            _log(f"updated tasks, will run again at {next_task_update}")

        # parallel updates from all running tasks
        await asyncio.gather(
            *[
                bobsled.run.update_status(run.uuid, update_logs=True)
                for run in running + pending
            ]
        )

        # TODO: could improve by basing next run time on last run instead of using utcnow
        for task_name, next_run in next_run_list.items():
            if next_run <= utcnow:
                # update next run time
                next_run_list[task_name] = next_run_for_task(task)
                try:
                    task = await bobsled.storage.get_task(task_name)
                    run = await bobsled.run.run_task(task)
                    msg = f"started {task_name}: {run}.  next run at {next_run_list[task_name]}"
                except AlreadyRunning:
                    msg = f"{task_name}: already running.  next run at {next_run_list[task_name]}"
                _log(msg)

        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(run_service())
