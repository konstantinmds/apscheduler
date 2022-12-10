import os
import pickle
from datetime import datetime

from pytz import utc
from kazoo.exceptions import NoNodeError, NodeExistsError

from apscheduler.jobstores.base import BaseJobStore, JobLookupError, ConflictingIdError
from apscheduler.util import maybe_ref, datetime_to_utc_timestamp, utc_timestamp_to_datetime
from apscheduler.job import Job

try:
    from kazoo.client import KazooClient
except ImportError:  # pragma: nocover
    raise ImportError('ZooKeeperJobStore requires Kazoo installed')


class ZooKeeperJobStore(BaseJobStore):
    """
    Stores jobs in a ZooKeeper tree. Any leftover keyword arguments are directly passed to
    kazoo's `KazooClient
    <http://kazoo.readthedocs.io/en/latest/api/client.html>`_.

    Plugin alias: ``zookeeper``

    :param str path: path to store jobs in
    :param client: a :class:`~kazoo.client.KazooClient` instance to use instead of
        providing connection arguments
    :param int pickle_protocol: pickle protocol level to use (for serialization), defaults to the
        highest available
    """

    def __init__(self, path='/apscheduler', client=None, close_connection_on_exit=False,
                 pickle_protocol=pickle.HIGHEST_PROTOCOL, **connect_args):
        super().__init__()
        self.pickle_protocol = pickle_protocol
        self.close_connection_on_exit = close_connection_on_exit

        if not path:
            raise ValueError('The "path" parameter must not be empty')

        self.path = path

        self.client = maybe_ref(client) if client else KazooClient(**connect_args)
        self._ensured_path = False

    def _ensure_paths(self):
        if not self._ensured_path:
            self.client.ensure_path(self.path)
        self._ensured_path = True

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        if not self.client.connected:
            self.client.start()

    def lookup_job(self, job_id):
        self._ensure_paths()
        node_path = os.path.join(self.path, job_id)
        try:
            content, _ = self.client.get(node_path)
            doc = pickle.loads(content)
            return self._reconstitute_job(doc['job_state'])
        except BaseException:
            return None

    def get_due_jobs(self, now):
        timestamp = datetime_to_utc_timestamp(now)
        return [
            job_def['job']
            for job_def in self._get_jobs()
            if job_def['next_run_time'] is not None
            and job_def['next_run_time'] <= timestamp
        ]

    def get_next_run_time(self):
        next_runs = [job_def['next_run_time'] for job_def in self._get_jobs()
                     if job_def['next_run_time'] is not None]
        return utc_timestamp_to_datetime(min(next_runs)) if next_runs else None

    def get_all_jobs(self):
        jobs = [job_def['job'] for job_def in self._get_jobs()]
        self._fix_paused_jobs_sorting(jobs)
        return jobs

    def add_job(self, job):
        self._ensure_paths()
        node_path = os.path.join(self.path,  str(job.id))
        value = {
            'next_run_time': datetime_to_utc_timestamp(job.next_run_time),
            'job_state': job.__getstate__()
        }
        data = pickle.dumps(value, self.pickle_protocol)
        try:
            self.client.create(node_path, value=data)
        except NodeExistsError:
            raise ConflictingIdError(job.id)

    def update_job(self, job):
        self._ensure_paths()
        node_path = os.path.join(self.path,  str(job.id))
        changes = {
            'next_run_time': datetime_to_utc_timestamp(job.next_run_time),
            'job_state': job.__getstate__()
        }
        data = pickle.dumps(changes, self.pickle_protocol)
        try:
            self.client.set(node_path, value=data)
        except NoNodeError:
            raise JobLookupError(job.id)

    def remove_job(self, job_id):
        self._ensure_paths()
        node_path = os.path.join(self.path,  str(job_id))
        try:
            self.client.delete(node_path)
        except NoNodeError:
            raise JobLookupError(job_id)

    def remove_all_jobs(self):
        try:
            self.client.delete(self.path, recursive=True)
        except NoNodeError:
            pass
        self._ensured_path = False

    def shutdown(self):
        if self.close_connection_on_exit:
            self.client.stop()
            self.client.close()

    def _reconstitute_job(self, job_state):
        job_state = job_state
        job = Job.__new__(Job)
        job.__setstate__(job_state)
        job._scheduler = self._scheduler
        job._jobstore_alias = self._alias
        return job

    def _get_jobs(self):
        self._ensure_paths()
        jobs = []
        failed_job_ids = []
        all_ids = self.client.get_children(self.path)
        for node_name in all_ids:
            try:
                node_path = os.path.join(self.path, node_name)
                content, _ = self.client.get(node_path)
                doc = pickle.loads(content)
                job_def = {
                    'job_id': node_name,
                    'next_run_time': doc['next_run_time'] or None,
                    'job_state': doc['job_state'],
                    'job': self._reconstitute_job(doc['job_state']),
                    'creation_time': _.ctime,
                }
                jobs.append(job_def)
            except BaseException:
                self._logger.exception(f'Unable to restore job "{node_name}" -- removing it')
                failed_job_ids.append(node_name)

        # Remove all the jobs we failed to restore
        if failed_job_ids:
            for failed_id in failed_job_ids:
                self.remove_job(failed_id)
        paused_sort_key = datetime(9999, 12, 31, tzinfo=utc)
        return sorted(jobs, key=lambda job_def: (job_def['job'].next_run_time or paused_sort_key,
                                                 job_def['creation_time']))

    def __repr__(self):
        self._logger.exception(f'<{self.__class__.__name__} (client={self.client})>')
        return f'<{self.__class__.__name__} (client={self.client})>'
