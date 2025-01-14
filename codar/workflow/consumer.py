"""Classes for 'consuming' pipelines - running groups of MPI tasks based on a
specified total process limit."""

import threading
import os
import json
import logging

from codar.cheetah.helpers import get_file_size
from codar.workflow import status
from codar.workflow.scheduler import JobList


_log = logging.getLogger('codar.workflow.consumer')


class PipelineRunner(object):
    """Runner that assumes a homogonous set of nodes. Now only support only
    node based limiting (although process limiting can be emulated by setting
    process_per_node=1 and max_nodes=max_procs).

    Threading model: assumes there could be multiple producer threads calling
    add_pipeline, e.g. if using a dynamic job submission model based on
    results of previous jobs. Pipelines and each Run in a pipeline are all
    executed in separate threads, so their notification callbacks execute in
    separate threads, and their threads must be joined before exiting. The
    stop and kill_all methods could be called from any of the producer,
    Pipeline or Run threads."""

    def __init__(self, runner, max_nodes, processes_per_node,
                 status_file=None):
        self.max_nodes = max_nodes
        self.ppn = processes_per_node
        self.runner = runner

        if status_file is not None:
            self._status = status.WorkflowStatus(status_file)
        else:
            self._status = None

        self.job_list_cv = threading.Condition()
        costfn = lambda pipe_or_run: pipe_or_run.get_nodes_used()
        self.job_list = JobList(costfn)

        self.free_cv = threading.Condition()
        self.free_nodes = max_nodes

        self.pipelines_lock = threading.Lock()
        self.pipelines = []
        self._pipeline_ids = set()

        self._running_pipelines = set()
        self._process_pipelines = True
        self._allow_new_pipelines = True
        self._killed = False

    def add_pipeline(self, p):
        with self.pipelines_lock:
            if not self._allow_new_pipelines:
                raise ValueError(
                    "new pipelines are not allowed after stop or kill")
            if p.id in self._pipeline_ids:
                raise ValueError("duplicate pipeline id: %s" % p.id)
            self._pipeline_ids.add(p.id)
            p.set_ppn(self.ppn)
            if p.get_nodes_used() > self.max_nodes:
                _log.error(
                    "pipeline '%s' requires %d nodes > max %d, skipping",
                    p.id, p.get_nodes_used(), self.max_nodes)
                if self._status is not None:
                    state = p.get_state()
                    state.reason = status.REASON_NOFIT
                    self._status.set_state(state)
                return
            elif self._status is not None:
                self._status.set_state(p.get_state())

        with self.job_list_cv:
            self.job_list.add_job(p)
            self.job_list_cv.notify()

    def stop(self):
        """Signal to stop when all pipelines are finished. Don't allow adding
        new pipelines."""
        self._allow_new_pipelines = False

        # signal main thread to wake up and check state
        with self.job_list_cv:
            self.job_list_cv.notify()

    def kill_all(self):
        """Kill all running processes spawned by this consumer and don't
        start any new processes."""

        _log.warn("killing all pipelines and exiting consumer")

        with self.pipelines_lock:
            self._killed = True
            self._allow_new_pipelines = False
            self._process_pipelines = False
            still_running = list(self._running_pipelines)

        # signal both cvs to stop waiting in main thread
        with self.free_cv:
            self.free_cv.notify()

        with self.job_list_cv:
            self.job_list_cv.notify()

        for pipe in still_running:
            pipe.force_kill_all()
        # NB: the run_pipelines methods will block waiting for the
        # pipelines, so we don't need to do that here. Callers that want
        # to block can call join on the consumer thread.

    def run_finished(self, run):
        """Monitor thread(s) should call this as runs complete."""
        with self.free_cv:
            _log.debug("finished run, free nodes %d -> %d",
                       self.free_nodes, self.free_nodes + run.get_nodes_used())
            self.free_nodes += run.get_nodes_used()
            self.free_cv.notify()

    def pipeline_finished(self, pipeline):
        """Monitor thread(s) should call this as pipelines complete."""

        self._get_adios_file_sizes(pipeline)
        with self.pipelines_lock:
            self._running_pipelines.remove(pipeline)
            if self._status is not None:
                self._status.set_state(pipeline.get_state())

    def pipeline_fatal(self, pipeline):
        _log.error("fatal error in pipeline '%s'" % pipeline.id)
        self.kill_all()

    def run_pipelines(self):
        """Main loop of consumer thread. Does not return until all child
        threads are complete."""
        while True:
            # wait until a job is available or end has been signaled
            no_more_pipelines = False
            with self.job_list_cv:
                while len(self.job_list) == 0:
                    if not self._allow_new_pipelines:
                        no_more_pipelines = True
                        break
                    self.job_list_cv.wait()

            if no_more_pipelines:
                self._join_running_pipelines()
                return

            # wait until nodes are available or quit has been signaled
            with self.free_cv:
                pipeline = self.job_list.pop_job(self.free_nodes)
                while pipeline is None:
                    if not self._process_pipelines:
                        break
                    self.free_cv.wait()
                    pipeline = self.job_list.pop_job(self.free_nodes)

                if self._process_pipelines:
                    _log.debug("starting pipeline %s, free nodes %d -> %d",
                               pipeline.id, self.free_nodes,
                               self.free_nodes - pipeline.get_nodes_used())
                    self.free_nodes -= pipeline.get_nodes_used()

            if not self._process_pipelines:
                self._join_running_pipelines()
                return

            with self.pipelines_lock:
                pipeline.start(self, self.runner)
                self._running_pipelines.add(pipeline)
                if self._status is not None:
                    self._status.set_state(pipeline.get_state())

        self._join_running_pipelines()

    def _join_running_pipelines(self):
        """Wait for any pipelines that are still running to complete. Use
        a copy since the monitor threads may be removing pipelines as
        they complete (and joining an already complete pipeline is
        harmless).

        This must be called without any locks held, since the Pipeline and
        Run threads may need to acquire them in the callback functions set
        by the consumer."""
        still_running = list(self._running_pipelines)
        for pipeline in still_running:
            pipeline.join_all()

    def _get_adios_file_sizes(self, pipeline):
        """
        Record the size of all adios files in the run dir.
        """

        def _adios_file_sizes_recursive(path):
            fname_size = {}
            for entry in os.scandir(path):
                if entry.name.endswith(".bp") or entry.name.endswith(".bp.dir"):
                    size = get_file_size(entry)
                    relative_path = entry.path.split(path+"/", 1).pop()
                    fname_size[relative_path] = size
                elif entry.is_dir():
                    _adios_file_sizes_recursive(entry.path)
            return fname_size

        d_fname_size =_adios_file_sizes_recursive(pipeline.working_dir)
        # Write dict to file
        out_fname = os.path.join(pipeline.working_dir,
                                 ".codar.adios_file_sizes.out.json")
        with open(out_fname, 'w') as f:
            f.write(json.dumps(d_fname_size))
