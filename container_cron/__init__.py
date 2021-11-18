import os
import sys
import logging
import hashlib
import grpc
import json
import threading
import time
import getopt
import shlex

from typing import Tuple, Union

from tempfile import gettempdir
from crontab import CronTab
from dotenv import dotenv_values
from io import StringIO

from containerd.services.containers.v1 import containers_pb2_grpc, containers_pb2
from containerd.services.events.v1 import unwrap, events_pb2, events_pb2_grpc
from containerd.services.tasks.v1 import tasks_pb2, tasks_pb2_grpc

from apscheduler.schedulers.base import BaseScheduler
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


def get_container_spec(channel: grpc.Channel, container_id: str):
    containers_stub = containers_pb2_grpc.ContainersStub(channel)
    container = containers_stub.Get(containers_pb2.GetContainerRequest(
        id=container_id), metadata=METADATA).container
    return json.loads(container.spec.value)


def run_command(channel: grpc.Channel, container_id: str, args, timeout=5) -> Tuple[int, str]:
    exec_id = "exec-" + hashsum(container_id + ': ' + str(args))
    stdout = mkfifo(exec_id)
    read_task = ReadTask(stdout)

    container_spec = get_container_spec(channel, container_id)
    container_process = container_spec['process']

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


def run_schedule(channel: grpc.Channel, container_id: str, args):
    exit_code, _ = run_command(channel, container_id, args)
    logging.getLogger('cron').info(
        "{code} <- schedule {schedule}".format(code=exit_code, schedule=' '.join(shlex.quote(arg) for arg in args)))
    return exit_code


def get_os_id(channel: grpc.Channel, container_id: str):
    exit_code, output = run_command(
        channel, container_id, ["cat", "/etc/os-release"])
    if exit_code != 0:
        return
    release = dotenv_values(stream=StringIO(output.decode('utf8')))
    return release['ID']


def get_alpine_crontab(channel: grpc.Channel, container_id: str) -> Tuple[str, Union[str, bool]]:
    exit_code, output = run_command(
        channel, container_id, ['whoami'])
    if exit_code != 0:
        return '', None
    user = output.decode('utf8').replace('\n', '')
    exit_code, output = run_command(
        channel, container_id, ['cat', '/etc/crontabs/{user}'.format(user=user)])
    if exit_code != 0:
        return '', None
    return output.decode('utf8').replace('\t', ' '), user


def get_debian_crontab(channel: grpc.Channel, container_id: str) -> Tuple[str, Union[str, bool]]:
    exit_code, output = run_command(channel, container_id, ["/bin/sh", "-c",
                                                            '[ -d /etc/cron.d ] && find /etc/cron.d ! -name \".*\" -type f -exec cat \{\} \;'])
    if exit_code != 0:
        return '', None
    return output.decode('utf8').replace('\t', ' '), False


def get_container_crontab(channel: grpc.Channel, container_id: str) -> Tuple[str, Union[str, bool]]:
    os_id = get_os_id(channel, container_id)
    if os_id == 'alpine':
        return get_alpine_crontab(channel, container_id)
    elif os_id == 'debian' or os_id == 'ubuntu':
        return get_debian_crontab(channel, container_id)
    return '', None


def parse_args(command: str):
    return shlex.split(command)


def get_container_name(spec):
    if 'annotations' in spec:
        annotations = spec['annotations']
        if 'io.kubernetes.cri.container-name' in annotations:
            return annotations['io.kubernetes.cri.container-name']
    if 'hostname' in spec:
        return spec['hostname']
    if 'id' in spec:
        return spec['id']


def load_container_schedules(scheduler: BaseScheduler, container_id, channel):
    tab, user = get_container_crontab(channel, container_id)
    if tab == '':
        return

    logger = logging.getLogger('cron')
    container_spec = get_container_spec(channel, container_id)
    container_name = get_container_name(
        container_spec) if not None else container_id
    logger.info(
        'load schedules from [{container_name}]...'.format(container_name=container_name))

    cron_jobs = CronTab(tab=tab, user=user)
    for job in cron_jobs:
        if not job.is_enabled():
            continue
        slices = str(job.slices)
        if slices.startswith('@'):
            slices = SPECIALS[slices.lstrip('@')]
        scheduler.add_job(run_schedule,
                          CronTrigger.from_crontab(slices),
                          args=[channel, container_id,
                                parse_args(job.command)],
                          name=job.command)
        logger.debug(
            'found [{container_name}]: {job}.'.format(job=job.command, container_name=container_name))

    logger.info(
        'got {job_count} schedules now.'.format(job_count=len(scheduler.get_jobs())))


def unload_container_schedules(scheduler: BaseScheduler, container_id):
    jobs = scheduler.get_jobs()
    job_count = len(jobs)
    for job in jobs:
        # 若存储器中的任务所属容器当前不存在，则在存储请中删除此任务
        if job.args[1] == container_id:
            scheduler.remove_job(job_id=job.id)
            job_count -= 1
    logging.getLogger('cron').info(
        'some schedules removed, {job_count} left.'.format(job_count=job_count))


def main():
    try:
        opts, _ = getopt.getopt(sys.argv[1:], 's:n:', [
            'cri-socket=', 'namespace='])
    except getopt.GetoptError as e:
        print('Usage: --cri-socket|-s <SOCKET> --namespace|-n <NAMESPACE>')
        exit(1)

    cri_socket = 'unix:///run/containerd/containerd.sock'
    namespace = 'k8s.io'  # moby for docker

    for k, v in opts:
        if k == '--cri-socket' or k == '-s':
            cri_socket = 'unix://' + v if v.startswith('/') else v
        elif k == '--namespace' or k == '-n':
            namespace = v

    global METADATA
    METADATA = (('containerd-namespace', namespace),)

    TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')

    logging.basicConfig(stream=sys.stdout)
    logging.getLogger('apscheduler').setLevel(logging.ERROR)
    logging.getLogger('cron').setLevel(logging.INFO)

    scheduler = BackgroundScheduler(
        executors={'default': ThreadPoolExecutor(40)}, timezone=TIMEZONE)
    scheduler.start()

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
