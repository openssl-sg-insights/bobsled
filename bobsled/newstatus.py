from __future__ import print_function
import os
import re
import shutil
import datetime
from collections import defaultdict, OrderedDict
import boto3

from bobsled.dynamo import Run, Status
from bobsled.templates import render_jinja_template


def update_status():
    # update run records in database
    check_status()

    # update global view
    write_index_html()


def check_status():
    # check everything that's running
    runs = {r.task_arn: r for r in Run.status_index.query(Status.Running)}

    if not runs:
        return

    ecs = boto3.client('ecs', region_name='us-east-1')
    # we limit this to 100 for AWS, which is fine b/c 100 shouldn't be running at once
    # if somehow they are, a subsequent run will pick the rest up
    resp = ecs.describe_tasks(cluster=os.environ['BOBSLED_ECS_CLUSTER'],
                              tasks=list(runs.keys())[:100])

    # match status to runs
    for failure in resp['failures']:
        if failure['reason'] == 'MISSING':
            update_run_status(runs[failure['arn']])
        else:
            raise ValueError('unexpected status {}'.format(failure))

    for task in resp['tasks']:
        if task['lastStatus'] == 'STOPPED':
            update_run_status(runs[task['taskArn']])
        elif task['lastStatus'] in ('RUNNING', 'PENDING'):
            print('still running', runs[task['taskArn']])
        else:
            raise ValueError('unexpected status {}'.format(task))


def update_run_status(run):
    logs = get_log_for_run(run)
    if contains_error(logs):
        run.status = Status.Error
        run.save()
        print(run, '=> error')
    else:
        run.status = Status.Success
        run.save()
        print(run, '=> success')


def get_log_for_run(run):
    logs = boto3.client('logs', region_name='us-east-1')

    pieces = dict(
        task_name=os.environ['BOBSLED_TASK_NAME'],
        family=run.job.lower(),
        task_id=run.task_arn.split('/')[-1],
    )
    log_arn = '{family}/{task_name}/{task_id}'.format(**pieces)

    next = None

    while True:
        extra = {'nextToken': next} if next else {}
        events = logs.get_log_events(logGroupName=os.environ['BOBSLED_ECS_LOG_GROUP'],
                                     logStreamName=log_arn, **extra)
        next = events['nextForwardToken']

        if not events['events']:
            break

        for event in events['events']:
            yield event

        if not next:
            break


def contains_error(stream):
    ERROR_REGEX = re.compile(r'(CRITICAL)|(Exception)|(Traceback)')
    for line in stream:
        if ERROR_REGEX.findall(line['message']):
            return True


class RunList(object):

    def __init__(self):
        self.runs = []

    def add(self, run):
        self.runs.append(run)

    @property
    def status(self):
        has_success = False
        has_failure = False
        for r in self.runs:
            if r.status == Status.Error:
                has_failure = True
            elif r.status == Status.Success:
                has_success = True
        if has_success and has_failure:
            return 'other'
        elif has_success:
            return 'good'
        elif has_failure:
            return 'bad'
        else:
            return 'empty'


def write_index_html():
    chart_days = 14
    output_dir = '/tmp/bobsled-output'

    # get recent runs and group by day
    runs = Run.recent(chart_days)

    job_runs = defaultdict(lambda: defaultdict(RunList))

    for run in runs:
        rundate = run.start.date()
        job_runs[run.job][rundate].add(run)

    # render HTML
    today = datetime.date.today()
    days = [today - datetime.timedelta(days=n) for n in range(chart_days)]
    runs = OrderedDict(sorted(job_runs.items()))
    html = render_jinja_template('runs.html', runs=runs, days=days)

    try:
        os.makedirs(output_dir)
    except OSError:
        pass

    with open(os.path.join(output_dir, 'index.html'), 'w') as out:
        out.write(html)
    shutil.copy(os.path.join(os.path.dirname(__file__), '../css/main.css'), output_dir)
