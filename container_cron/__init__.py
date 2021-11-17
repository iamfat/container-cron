import os
import sys
import logging
import hashlib
import grpc
import json
import threading
import time
import getopt

from tempfile import gettempdir
from crontab import CronTab

from containerd.services.containers.v1 import containers_pb2_grpc, containers_pb2
from containerd.services.events.v1 import unwrap, events_pb2, events_pb2_grpc
from containerd.services.tasks.v1 import tasks_pb2, tasks_pb2_grpc

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger

SPECIALS = {"reboot":   '@reboot',
            "hourly":   '0 * * * *',
            "daily":    '0 0 * * *',
            "weekly":   '0 0 * * 0',
            "monthly":  '0 0 1 * *',
            "yearly":   '0 0 1 1 *',
            "annually": '0 0 1 1 *',
            "midnight": '0 0 * * *'}

METADATA = (('containerd-namespace', 'k8s.io'),)


def hashsum(str):
    md5 = hashlib.md5()
    md5.update(str.encode('utf-8'))
    return md5.hexdigest()

class ReadTask(threading.Thread):
    abort_event: threading.Event
    output: bytearray
    path: str

    @staticmethod
    def target(self):
        with open(self.path, 'rb') as f:
            while not self.abort_event.is_set():
                self.output += f.read()
                time.sleep(0.1)

    def __init__(self, path: str) -> None:
        self.abort_event = threading.Event()
        self.open_event = threading.Event()
        self.path = path
        self.output = bytearray()
        super().__init__(target=ReadTask.target, args=(self,))

    def result(self) -> bytearray:
        self.abort_event.set()
        if self.is_alive():
            # make sure fifo was operated to avoid blocking behavior
            try:
                open(self.path, 'wb').close()
            except:
                pass
            self.join()
        return self.output


def mkfifo(exec_id: str):
    fifo = gettempdir() + '/' + exec_id
    try:
        os.unlink(fifo)
    except:
        pass
    os.mkfifo(fifo)
    return fifo


def run_command(channel, container_id, args, timeout=5):
    exec_id = "exec-" + hashsum(container_id + ': ' + str(args))
    stdout = mkfifo(exec_id)
    read_task = ReadTask(stdout)

    containers_stub = containers_pb2_grpc.ContainersStub(channel)
    container = containers_stub.Get(containers_pb2.GetContainerRequest(
        id=container_id), metadata=METADATA).container
    container_process = json.loads(container.spec.value)['process']

    process = {
        'args': args,
        'cwd': container_process['cwd'],
        'terminal': False,
        'env': container_process['env'],
        'user': container_process['user']
    }

    spec = {
        'type_url': 'types.containerd.io/opencontainers/runtime-spec/1/Spec',
        'value': json.dumps(process).encode('utf-8')
    }

    tasks_stub = tasks_pb2_grpc.TasksStub(channel)

    # remove previous conflict process
    try:
        tasks_stub.DeleteProcess(tasks_pb2.DeleteProcessRequest(
            container_id=container_id,
            exec_id=exec_id
        ), metadata=METADATA)
    except:
        pass

    try:
        tasks_stub.Exec(tasks_pb2.ExecProcessRequest(
            container_id=container_id,
            exec_id=exec_id,
            stdin=os.devnull, stdout=stdout, stderr=os.devnull,
            terminal=False,
            spec=spec
        ), metadata=METADATA)
        read_task.start()
        tasks_stub.Start(tasks_pb2.StartRequest(
            container_id=container_id, exec_id=exec_id), metadata=METADATA)
        exit_status = tasks_stub.Wait(tasks_pb2.WaitRequest(
            container_id=container_id, exec_id=exec_id), timeout=timeout, metadata=METADATA).exit_status
    except:
        exit_status = 1

    return exit_status, read_task.result()


def run_schedule(channel, container_id, args):
    exit_code = run_command(channel, container_id, args)
    logging.getLogger('cron').info(
        "{code} <- schedule {schedule}".format(code=exit_code, schedule=' '.join(args)))


def load_container_schedules(scheduler, container_id, channel):
    logging.getLogger('cron').info(
        'load schedules from {container_id}'.format(container_id=container_id))

    args = ["/bin/sh", "-c",
            '[ -d /etc/cron.d ] && find /etc/cron.d ! -name \".*\" -type f -exec cat \{\} \;']
    exit_code, output = run_command(channel, container_id, args)
    if exit_code != 0:
        return

    tab = output.decode('utf8').replace('\t', ' ')
    if tab == '':
        return

    cron_jobs = CronTab(tab=tab, user=False)
    for job in cron_jobs:
        if not job.is_enabled():
            continue
        slices = str(job.slices)
        if slices.startswith('@'):
            slices = SPECIALS[slices.lstrip('@')]
        scheduler.add_job(run_schedule,
                          CronTrigger.from_crontab(slices),
                          args=[channel, container_id, job.command],
                          name=job.command)


def unload_container_schedules(scheduler, container_id):
    logging.getLogger('cron').info(
        'unload schedules from {container_id}'.format(container_id=container_id))

    for job in scheduler.get_jobs():
        # 若存储器中的任务所属容器当前不存在，则在存储请中删除此任务
        if job.args[0] == container_id:
            scheduler.remove_job(job_id=job.id)


def main():
    try:
        opts, args = getopt.getopt(sys.argv[1:], 's:n:', ['cri-socket=', 'namespace='])
    except getopt.GetoptError as e:
        print('Usage: --cri-socket|-s <SOCKET> --namespace|-n <NAMESPACE>')
        exit(1)

    cri_socket = 'unix:///run/containerd/containerd.sock'
    namespace = 'k8s.io' # moby for docker

    for k,v in opts:
        if k == '--cri-socket' or k == '-s':
            cri_socket = 'unix://' + v if v.startswith('/') else v
        elif k == '--namespace' or k == '-n':
            namespace = v

    METADATA = (('containerd-namespace', namespace),)

    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')

    logging.basicConfig(stream=sys.stdout)
    logging.getLogger('apscheduler').setLevel(logging.INFO)
    logging.getLogger('cron').setLevel(logging.INFO)

    scheduler = BackgroundScheduler(
        executors={'default': ThreadPoolExecutor(40)}, timezone=TIMEZONE)
    try:
        scheduler.start()
    except:
        pass

    with grpc.insecure_channel(cri_socket) as channel:
        containers_stub = containers_pb2_grpc.ContainersStub(channel)
        containers = containers_stub.List(
            containers_pb2.ListContainersRequest(), metadata=METADATA).containers
        for container in containers:
            load_container_schedules(scheduler, container.id, channel)

        events_stub = events_pb2_grpc.EventsStub(channel)
        for ev in events_stub.Subscribe(events_pb2.SubscribeRequest()):
            v = unwrap(ev)
            if ev.event.type_url == 'containerd.events.TaskCreate':
                load_container_schedules(scheduler, v.container_id, channel)
            elif ev.event.type_url == 'containerd.events.TaskDelete':
                unload_container_schedules(scheduler, v.container_id)


if __name__ == "__main__":
    main()
