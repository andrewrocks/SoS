#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import copy
import os
import fasteners
import pickle
import time
import lzma
import stat
import struct
from collections import namedtuple

from typing import Union, Dict
from collections.abc import Sequence

from .utils import (env, expand_time, linecount_of_file, sample_lines, log_to_file,
                    short_repr, tail_of_file, expand_size, format_HHMMSS,
                    DelayedAction)
from .targets import sos_targets

monitor_interval = 5
resource_monitor_interval = 60


class TaskParams(object):
    '''A parameter object that encaptulates parameters sending to
    task executors. This would makes the output of workers, especially
    in the web interface much cleaner (issue #259)'''

    def __init__(self, name, global_def, task, sos_dict):
        self.name = name
        self.global_def = global_def
        self.task = task
        self.sos_dict = sos_dict
        # remove builtins that could be saved in a dictionary
        if 'CONFIG' in self.sos_dict and '__builtins__' in self.sos_dict['CONFIG']:
            self.sos_dict['CONFIG'].pop('__builtins__')

    def __repr__(self):
        return self.name


class MasterTaskParams(TaskParams):
    def __init__(self, num_workers=0):
        self.ID = 'M_0'
        self.name = self.ID
        self.global_def = ''
        self.task = ''
        self.sos_dict = {'_runtime': {}, '_input': sos_targets(), '_output': sos_targets(), '_depends': sos_targets(),
                         'step_input': sos_targets(), 'step_output': sos_targets(),
                         'step_depends': sos_targets(), 'step_name': '',
                         '_index': 0}
        self.sos_dict['__task_vars__'] = copy.copy(self.sos_dict)
        self.num_workers = num_workers
        # a collection of tasks that will be executed by the master task
        self.task_stack = []

    def num_tasks(self):
        return len(self.task_stack)

    def push(self, task_id, params):
        # update walltime, cores, and mem
        # right now we require all tasks to have same resource requirment, which is
        # quite natural because they are from the same step
        #
        # update input, output, and depends
        #
        # walltime
        if not self.task_stack:
            for key in ('walltime', 'max_walltime', 'cores', 'max_cores', 'mem', 'max_mem', 'map_vars',
                        'name', 'cur_dir', 'home_dir', 'verbosity', 'sig_mode', 'run_mode'):
                if key in params.sos_dict['_runtime'] and params.sos_dict['_runtime'][key] is not None:
                    self.sos_dict['_runtime'][key] = params.sos_dict['_runtime'][key]
            self.sos_dict['step_name'] = params.sos_dict['step_name']
            self.tags = params.tags
        else:
            for key in ('walltime', 'max_walltime', 'cores', 'max_cores', 'mem', 'max_mem',
                        'name', 'cur_dir', 'home_dir'):
                val0 = self.task_stack[0][1].sos_dict['_runtime'].get(
                    key, None)
                val = params.sos_dict['_runtime'].get(key, None)
                if val0 != val:
                    raise ValueError(
                        f'All tasks should have the same resource {key}')
                #
                nrow = len(self.task_stack) if self.num_workers <= 1 else ((len(self.task_stack) + 1) //
                                                                           self.num_workers + (0 if (len(self.task_stack) + 1) % self.num_workers == 0 else 1))
                if self.num_workers == 0:
                    ncol = 1
                elif nrow > 1:
                    ncol = self.num_workers
                else:
                    ncol = len(self.task_stack) + 1

                if val0 is None:
                    continue
                elif key == 'walltime':
                    # if define walltime
                    self.sos_dict['_runtime']['walltime'] = format_HHMMSS(
                        nrow * expand_time(val0))
                elif key == 'mem':
                    # number of columns * mem for each + 100M for master
                    self.sos_dict['_runtime']['mem'] = ncol * \
                        expand_size(val0) + (expand_size('100M')
                                             if self.num_workers > 0 else 0)
                elif key == 'cores':
                    # number of columns * cores for each + 1 for the master
                    self.sos_dict['_runtime']['cores'] = ncol * \
                        val0 + (1 if self.num_workers > 0 else 0)
                elif key == 'name':
                    self.sos_dict['_runtime']['name'] = f'{val0}_{len(self.task_stack) + 1}'

            self.tags.extend(params.tags)
        #
        # input, output, preserved vars etc
        for key in ['_input', '_output', '_depends']:
            if key in params.sos_dict and isinstance(params.sos_dict[key], list):
                if key == '__builtins__':
                    continue
                # do not extend duplicated input etc
                self.sos_dict[key].extend(
                    list(set(params.sos_dict[key]) - set(self.sos_dict[key])))
        #
        self.task_stack.append([task_id, params])
        self.tags = sorted(list(set(self.tags)))
        #
        self.ID = f'M{len(self.task_stack)}_{self.task_stack[0][0]}'
        self.sos_dict.pop('__task_vars__')
        self.sos_dict['__task_vars__'] = copy.copy(self.sos_dict)
        self.name = self.ID


class TaskFile(object):
    '''
    The task file has the following format:

    1. A binary header with the information of the structure of the file
    with field defined by TaskHeader
    2. compressed pickled param of task
    2. resource file appended by the worker
    3. compressed picked result
    4. compressed stdout
    5. compressed stderr
    '''
    TaskHeader = namedtuple('TaskHeader',
                            'version status tags '
                            'creation_time pending_time submitted_time failed_time completed_time last_modified '
                            'params_size pulse_size stdout_size stderr_size result_size'
                            )

    header_fmt = '!i 10s 128s 6d 5i'
    header_size = struct.calcsize(header_fmt)

    def __init__(self, task_id: str):
        self.task_file = os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', task_id + '.task'
        )

    def save(self, params, tags=[]):
        # updating job_file will not change timestamp because it will be Only
        # the update of runtime info
        now = time.time()
        params_block = lzma.compress(pickle.dumps(params))
        header = self.TaskHeader(
            version=1,
            status=b'new       ',
            tags=' '.join(sorted(tags)).ljust(128).encode(),
            creation_time=now,
            pending_time=0,
            submitted_time=0,
            failed_time=0,
            completed_time=0,
            last_modified=now,
            params_size=len(params_block),
            pulse_size=0,
            stdout_size=0,
            stderr_size=0,
            result_size=0
        )
        with open(self.task_file, 'wb+') as fh:
            self._write_header(fh, header)
            fh.write(params_block)

    def _read_header(self, fh):
        fh.seek(0, 0)
        return self.TaskHeader._make(struct.unpack(
            self.header_fmt,
            fh.read(self.header_size)))

    def _write_header(self, fh, header):
        fh.seek(0, 0)
        fh.write(struct.pack(self.header_fmt, *header))

    def _get_content(self, ext: str):
        filename = self.task_file[:-5] + ext
        if not os.path.isfile(filename):
            return b''
        with open(filename, 'rb') as fh:
            content = fh.read()
        return lzma.compress(content)

    def add_outputs(self):
        # get header
        pulse = self._get_content('.pulse')
        stdout = self._get_content('.out')
        stderr = self._get_content('.err')
        with open(self.task_file, 'r+b') as fh:
            header = self._read_header(fh)
            if header.result_size != 0:
                raise ValueError('Cannot output to task with result')
            header = header._replace(
                pulse_size=len(pulse),
                stdout_size=len(stdout),
                stderr_size=len(stderr)
            )
            self._write_header(fh, header)
            fh.seek(self.header_size + header.params_size, 0)
            if pulse:
                fh.write(pulse)
            if stdout:
                fh.write(stdout)
            if stderr:
                fh.write(stderr)

    def add_result(self, result: dict):
        result_block = lzma.compress(pickle.dumps(result))
        with open(self.task_file, 'r+b') as fh:
            header = self._read_header(fh)
            header = header._replace(
                result_size=len(result_block)
            )
            self._write_header(fh, header)
            fh.seek(self.header_size + header.params_size +
                    header.pulse_size + header.stdout_size + header.stderr_size)
            fh.write(result_block)

    def _get_info(self):
        with open(self.task_file, 'rb') as fh:
            return self._read_header(fh)

    info = property(_get_info)

    def _get_params(self):
        with open(self.task_file, 'rb') as fh:
            header = self._read_header(fh)
            if header.params_size == 0:
                return {}
            fh.seek(self.header_size, 0)
            return pickle.loads(lzma.decompress(fh.read(header.params_size)))

    params = property(_get_params)

    def _get_status(self):
        with open(self.task_file, 'rb') as fh:
            return self._read_header(fh).status.decode().strip()

    def _set_status(self, status):
        with open(self.task_file, 'r+b') as fh:
            header = self._read_header(fh)
            header = header._replace(status=status.ljust(
                10).encode(), last_modified=time.time())
            self._write_header(fh, header)

    status = property(_get_status, _set_status)

    def _get_tags(self):
        with open(self.task_file, 'rb') as fh:
            return self._read_header(fh).tags.decode().strip().split()

    def _set_tags(self, tags:list):
        with open(self.task_file, 'r+b') as fh:
            header = self._read_header(fh)
            header = header._replace(tags=' '.join(sorted(tags)).ljust(128).encode())
            self._write_header(fh, header)

    def add_tags(self, tags:list):
        with open(self.task_file, 'r+b') as fh:
            header = self._read_header(fh)
            header = header._replace(tags=' '.join(
                sorted(tags + header.tags.decode().strip().split())).ljust(128).encode())
            self._write_header(fh, header)

    tags = property(_get_tags, _set_tags)

    def _get_pulse(self):
        with open(self.task_file, 'rb') as fh:
            header = self._read_header(fh)
            if header.pulse_size == 0:
                return ''
            fh.seek(self.header_size + header.params_size, 0)
            try:
                return lzma.decompress(fh.read(header.pulse_size)).decode()
            except Exception as e:
                env.logger.warning(f'Failed to decode pulse: {e}')
                return ''

    pulse = property(_get_pulse)

    def _get_stdout(self):
        with open(self.task_file, 'rb') as fh:
            header = self._read_header(fh)
            if header.stdout_size == 0:
                return ''
            fh.seek(self.header_size + header.params_size + header.pulse_size, 0)
            try:
                return lzma.decompress(fh.read(header.stdout_size)).decode()
            except Exception as e:
                env.logger.warning(f'Failed to decode stdout: {e}')
                return ''
    stdout = property(_get_stdout)

    def _get_stderr(self):
        with open(self.task_file, 'rb') as fh:
            header = self._read_header(fh)
            if header.stderr_size == 0:
                return ''
            fh.seek(self.header_size + header.params_size + header.pulse_size
                    + header.stdout_size, 0)
            try:
                return lzma.decompress(fh.read(header.stderr_size)).decode()
            except Exception as e:
                env.logger.warning(f'Failed to decode stderr: {e}')
                return ''

    stderr = property(_get_stderr)

    def _get_result(self):
        with open(self.task_file, 'rb') as fh:
            header = self._read_header(fh)
            if header.result_size == 0:
                return {}
            fh.seek(self.header_size + header.params_size +
                    header.pulse_size + header.stdout_size + header.stderr_size, 0)
            try:
                return pickle.loads(lzma.decompress(fh.read(header.result_size)))
            except Exception as e:
                env.logger.warning(f'Failed to decode result: {e}')
                return {'ret_code': 1}

    result = property(_get_result)


def taskDuration(task):
    filename = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', f'{task}.task')
    return os.path.getatime(filename) - os.path.getmtime(filename)


def remove_task_files(task: str, exts: list):
    for ext in exts:
        filename = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + ext)
        if os.path.isfile(filename):
            if ext == '.pulse' and not os.access(filename, os.W_OK):
                os.chmod(filename, stat.S_IREAD | stat.S_IWRITE)
            try:
                os.remove(filename)
            except Exception as e:
                # if the file cannot be removed now, we use a thread to wait a
                # bit and try to remove it later. The function should not
                # wiat for the thread thoug
                try:
                    s = DelayedAction(os.remove, filename)
                except:
                    pass


def check_task(task, hint={}) -> Dict[str, Union[str, Dict[str, float]]]:

    # when testing. if the timestamp is 0, the file does not exist originally, it should
    # still does not exist. Otherwise the file should exist and has the same timestamp
    if hint and hint['status'] not in ('pending', 'running') and \
            all((os.path.isfile(f) and os.stat(f).st_mtime == v) if v else (not os.path.isfile(f)) for f, v in hint['files'].items()):
        return {}
    # status of the job, please refer to https://github.com/vatlab/SOS/issues/529
    # for details.
    #
    task_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.task')
    if not os.path.isfile(task_file):
        return dict(status='missing', files={task_file: 0})

    mtime = os.stat(task_file).st_mtime

    def task_changed():
        return os.stat(task_file).st_mtime != mtime

    pulse_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.pulse')

    try:
        params = loadTask(task_file)
    except Exception as e:
        # it is possible that the file is being changed ...
        time.sleep(0.5)
        return check_task(task)

    res = params.result
    # if the task has result section
    if res:
        try:
            from .targets import file_target
            # remove other files if exist
            remove_task_files(
                task, ['.pulse', '.sh', '.job_id', '.out', '.err'])
            status_files = {task_file: os.stat(task_file).st_mtime,
                            pulse_file: 0
                            }
            if ('ret_code' in res and res['ret_code'] == 0) or ('succ' in res and res['succ'] == 0):
                return dict(status='completed', files=status_files)
            else:
                return dict(status='failed', files=status_files)
        except Exception as e:
            # sometimes the task is changed while we are reading it
            # so we wait a bit and try again.
            env.logger.warning(e)
            time.sleep(.5)
            return check_task(task)

    # check the existence and validity of .pulse file
    # for whatever reason, sometimes the pulse file might appear to be slightly
    # before the task file, and if the task is very short so the pulse file is
    # not updated, the task will appear to be in pending mode forever
    if os.path.isfile(pulse_file) and os.stat(pulse_file).st_mtime >= os.stat(task_file).st_mtime - 1:
        try:
            status_files = {task_file: os.stat(task_file).st_mtime,
                            pulse_file: os.stat(pulse_file).st_mtime}
            # dead?
            # if the status file is readonly
            if not os.access(pulse_file, os.W_OK):
                return dict(status='aborted', files={task_file: os.stat(task_file).st_mtime,
                                                     pulse_file: os.stat(pulse_file).st_mtime})
            start_stamp = os.stat(pulse_file).st_mtime
            elapsed = time.time() - start_stamp
            if elapsed < 0:
                env.logger.debug(
                    f'{pulse_file} is created in the future. Your system time might be problematic')
            # if the file is within 5 seconds
            if elapsed < monitor_interval:
                # if running, we return old hint files even if the timestamp has been changed
                # because we will check the status of running jobs anyway.
                if hint and hint['status'] == 'running':
                    return {}
                else:
                    return dict(status='running', files=status_files)
            elif elapsed > 2 * monitor_interval:
                if task_changed():
                    # result file appears during sos tatus run
                    return check_task(task)
                else:
                    remove_task_files(task, ['.sh', '.job_id', '.out', '.err'])
                    return dict(status='aborted', files=status_files)
            # otherwise, let us be patient ... perhaps there is some problem with the filesystem etc
            time.sleep(2 * monitor_interval)
            end_stamp = os.stat(pulse_file).st_mtime
            # the process is still alive
            if task_changed():
                return check_task(task)
            elif start_stamp != end_stamp:
                if hint and hint['status'] == 'running':
                    return {}
                else:
                    return dict(status='running', files=status_files)
            else:
                remove_task_files(task, ['.sh', '.job_id', '.out', '.err'])
                return dict(status='aborted', files=status_files)
        except:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            else:
                raise
    # if there is no pulse file
    job_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.sh')

    def has_job():
        job_id_file = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + '.job_id')
        return os.path.isfile(job_file) and os.stat(job_file).st_mtime >= os.stat(task_file).st_mtime \
            and os.path.isfile(job_id_file) and os.stat(job_id_file).st_mtime >= os.stat(job_file).st_mtime

    if has_job():
        try:
            return dict(status='submitted', files={task_file: os.stat(task_file).st_mtime,
                                                   job_file: os.stat(job_file).st_mtime,
                                                   pulse_file: 0})
        except:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            else:
                raise
    else:
        # status not changed
        try:
            if hint and hint['status'] == 'pending' and hint['files'][task_file] == os.stat(task_file).st_mtime:
                return {}
            else:
                stat = os.stat(task_file)
                if stat.st_ctime == stat.st_mtime:
                    return dict(status='new', files={task_file: os.stat(task_file).st_mtime,
                                                     job_file: 0})
                else:
                    return dict(status='pending', files={task_file: os.stat(task_file).st_mtime,
                                                         job_file: 0})
        except:
            # the pulse file could disappear when the job is completed.
            if task_changed():
                return check_task(task)
            else:
                raise


def check_tasks(tasks, is_all: bool):
    if not tasks:
        return {}
    cache_file: str = os.path.join(
        os.path.expanduser('~'), '.sos', 'tasks', 'status_cache.pickle')
    #
    status_cache = {}
    if os.path.isfile(cache_file):
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'rb') as cache:
                status_cache = pickle.load(cache)
    # at most 20 threads
    from multiprocessing.pool import ThreadPool as Pool
    p = Pool(min(20, len(tasks)))
    # the result can be {} for unchanged, or real results
    raw_status = p.starmap(
        check_task, [(x, status_cache.get(x, {})) for x in tasks])

    # if check all, we clear the cache and record all existing tasks
    has_changes: bool = any(x for x in raw_status)
    if has_changes:
        if is_all:
            status_cache = {k: v if v else status_cache[k]
                            for k, v in zip(tasks, raw_status)}
        else:
            status_cache.update(
                {k: v for k, v in zip(tasks, raw_status) if v})
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'wb') as cache:
                pickle.dump(status_cache, cache)
    return status_cache


def print_task_status(tasks, verbosity: int=1, html: bool=False, start_time=False, age=None, tags=None, status=None):
    # verbose is ignored for now
    import glob
    from multiprocessing.pool import ThreadPool as Pool
    check_all: bool = not tasks
    if check_all:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                     for x in tasks]
        if not all_tasks:
            return
    else:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                       for x in matched]
            if not matched:
                all_tasks.append((t, None))
            else:
                all_tasks.extend(matched)
    if age is not None:
        age = expand_time(age, default_unit='d')
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    all_tasks = sorted(list(set(all_tasks)),
                       key=lambda x: 0 if x[1] is None else x[1])

    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x[0]).split(' '))]

    if not all_tasks:
        env.logger.info('No matching tasks')
        return

    raw_status = check_tasks([x[0] for x in all_tasks], check_all)
    obtained_status = [raw_status[x[0]]['status'] for x in all_tasks]
    if status:
        all_tasks = [x for x, s in zip(
            all_tasks, obtained_status) if s in status]
        obtained_status = [x for x in obtained_status if x in status]
    #
    # automatically remove non-running tasks that are more than 30 days old
    to_be_removed = []
    #
    if html:
        verbosity = -1
    if verbosity == 0:
        print('\n'.join(obtained_status))
    elif verbosity == 1:
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            print(f'{t}\t{s}')
    elif verbosity == 2:
        from .utils import PrettyRelativeTime
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            if start_time:
                if d is None:
                    print(f'{t}\t{taskTags(t)}\t{time.time()}\t{s}')
                else:
                    print(f'{t}\t{taskTags(t)}\t{d}\t{s}')
            else:
                if d is None:
                    print(f'{t}\t{taskTags(t)}\t{"":>15}\t{s}')
                elif s in ('pending', 'submitted', 'running'):
                    print(
                        f'{t}\t{taskTags(t)}\t{PrettyRelativeTime(time.time() - d):>15}\t{s}')
                else:
                    # completed or failed
                    print(
                        f'{t}\t{taskTags(t)}\t{PrettyRelativeTime(taskDuration(t)):>15}\t{s}')
    elif verbosity > 2:
        from .utils import PrettyRelativeTime
        import pprint
        from .monitor import summarizeExecution

        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            print(f'{t}\t{s}\n')
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                continue
            if d is not None:
                print(f'Started {PrettyRelativeTime(time.time() - d)} ago')
            if s not in ('pending', 'submitted', 'running'):
                print(f'Duration {PrettyRelativeTime(taskDuration(t))}')
            params = loadTask(task_file)
            print('TASK:\n=====')
            print(params.task)
            print('TAGS:\n=====')
            print(' '.join(params.tags))
            print()
            if params.global_def:
                print('GLOBAL:\n=======')
                print(params.global_def)
                print()
            print('ENVIRONMENT:\n============')
            job_vars = params.sos_dict
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                print(
                    f'{k:22}{short_repr(v) if verbosity == 3 else pprint.pformat(v)}')
            print()

            res = params.result
            if res:
                with open(res_file, 'rb') as result:
                    res = pickle.load(result)
                if 'pulse' in res:
                    print('EXECUTION STATS:\n================')
                    print(summarizeExecution(t, res['pulse'], status=s))
                if verbosity == 4:
                    # if there are other files such as job file, print them.
                    if 'stdout' in res:
                        print('standout output:\n================\n' +
                              res['stdout'])
                    if 'stderr' in res:
                        print('standout output:\n================\n' +
                              res['stderr'])
            else:
                # we have separate pulse, out and err files
                print('EXECUTION STATS:\n================')
                pulse_file = os.path.join(
                    os.path.expanduser('~'), '.sos', 'tasks', t + '.pulse')
                if os.path.isfile(pulse_file):
                    with open(pulse_file) as pulse:
                        print(summarizeExecution(t, pulse.read(), status=s))
                if verbosity == 4:
                    # if there are other files such as job file, print them.
                    for ext in ('.out', '.err'):
                        f = os.path.join(
                            os.path.expanduser('~'), '.sos', 'tasks', t + ext)
                        if not os.path.isfile(f):
                            continue
                        print(
                            f'{os.path.basename(f)}:\n{"="*(len(os.path.basename(f))+1)}')
                        try:
                            with open(f) as fc:
                                print(fc.read())
                        except Exception:
                            print('Binary file')
    else:
        # HTML output
        from .utils import PrettyRelativeTime, isPrimitive
        from .monitor import summarizeExecution
        import pprint
        print('<table width="100%" class="resource_table">')

        def row(th=None, td=None):
            if td is None:
                print(
                    f'<tr><th align="right" width="30%">{th}</th><td></td></tr>')
            elif th is None:
                print(
                    f'<tr><td colspan="2" align="left"  width="30%">{td}</td></tr>')
            else:
                print(
                    f'<tr><th align="right"  width="30%">{th}</th><td align="left"><div class="one_liner">{td}</div></td></tr>')
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60:
                to_be_removed.append(t)
                continue
            row('ID', t)
            row('Status', s)
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                print('</table>')
                continue
            if d is not None:
                row('Start', f'{PrettyRelativeTime(time.time() - d):>15} ago')
            if s not in ('pending', 'submitted', 'running'):
                row('Duration', f'{PrettyRelativeTime(taskDuration(t)):>15}')
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                continue
            params = loadTask(task_file)
            row('Task')
            row(td=f'<pre style="text-align:left">{params.task}</pre>')
            row('Tags')
            row(td=f'<pre style="text-align:left">{" ".join(params.tags)}</pre>')
            if params.global_def:
                row('Global')
                row(
                    td=f'<pre style="text-align:left">{params.global_def}</pre>')
            # row('Environment')
            job_vars = params.sos_dict
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                if not k.startswith('__') and not k == 'CONFIG':
                    if k == '_runtime':
                        for _k, _v in v.items():
                            if isPrimitive(_v) and _v not in (None, '', [], (), {}):
                                row(_k, _v)
                    elif isPrimitive(v) and v not in (None, '', [], (), {}):
                        row(k,
                            f'<pre style="text-align:left">{pprint.pformat(v)}</pre>')
            res = params.result
            pulse_content = ''
            if res:
                if 'pulse' in res:
                    pulse_content = res['pulse']
                    summary = summarizeExecution(t, res['pulse'], status=s)
                    # row('Execution')
                    for line in summary.split('\n'):
                        fields = line.split(None, 1)
                        if fields[0] == 'task':
                            continue
                        row(fields[0], '' if fields[1] is None else fields[1])
                # this is a placeholder for the frontend to draw figure
                row(td=f'<div id="res_{t}"></div>')
                #
                if 'stdout' in res:
                    numLines = res['stdout'].count('\n')
                    row('standard output', '(empty)' if numLines ==
                        0 else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}')
                    row(
                        td=f'<small><pre style="text-align:left">{res["stdout"].splitlines()[-200:]}</pre></small>')
                if 'stderr' in res:
                    numLines = res['stderr'].count('\n')
                    row('standard error', '(empty)' if numLines ==
                        0 else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}')
                    row(
                        td=f'<small><pre style="text-align:left">{res["stderr"].splitlines()[-200:]}</pre></small>')
            else:
                pulse_file = os.path.join(
                    os.path.expanduser('~'), '.sos', 'tasks', t + '.pulse')
                if os.path.isfile(pulse_file):
                    with open(pulse_file) as pulse:
                        pulse_content = pulse.read()
                        summary = summarizeExecution(
                            t, pulse_content, status=s)
                        if summary:
                            # row('Execution')
                            for line in summary.split('\n'):
                                fields = line.split(None, 1)
                                if fields[0] == 'task':
                                    continue
                                row(fields[0], '' if fields[1]
                                    is None else fields[1])
                            # this is a placeholder for the frontend to draw figure
                            row(td=f'<div id="res_{t}"></div>')
                #
                files = glob.glob(os.path.join(
                    os.path.expanduser('~'), '.sos', 'tasks', t + '.*'))
                for f in sorted([x for x in files if os.path.splitext(x)[-1] not in ('.task', '.pulse')]):
                    numLines = linecount_of_file(f)
                    row(os.path.splitext(f)[-1], '(empty)' if numLines ==
                        0 else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}')
                    try:
                        row(
                            td=f'<small><pre style="text-align:left">{tail_of_file(f, 200, ansi2html=True)}</pre></small>')
                    except Exception:
                        row(td='<small><pre style="text-align:left">ignored.</pre><small>')
            print('</table>')
            #
            if not pulse_content:
                return
            # A sample of 400 point should be enough to show the change of resources
            lines = sample_lines(pulse_content, 400).splitlines()
            if len(lines) <= 2:
                return
            # read the pulse file and plot it
            # time   proc_cpu        proc_mem        children        children_cpu    children_mem
            try:
                etime = []
                cpu = []
                mem = []
                for line in lines:
                    if line.startswith('#') or not line.strip():
                        continue
                    fields = line.split()
                    etime.append(float(fields[0]))
                    cpu.append(float(fields[1]) + float(fields[4]))
                    mem.append(float(fields[2]) / 1e6 + float(fields[5]) / 1e6)
                if not etime:
                    return
            except Exception:
                return
            #
            print('''
<script>
    function loadFiles(files, fn) {
        if (!files.length) {
            files = [];
        }
        var head = document.head || document.getElementsByTagName('head')[0];

        function loadFile(index) {
            if (files.length > index) {
                if (files[index].endsWith('.css')) {
                    var fileref = document.createElement('link');
                    fileref.setAttribute("rel", "stylesheet");
                    fileref.setAttribute("type", "text/css");
                    fileref.setAttribute("href", files[index]);
                } else {
                    var fileref = document.createElement('script');
                    fileref.setAttribute("type", "text/javascript");
                    fileref.setAttribute("src", files[index]);
                }
                console.log('Load ' + files[index]);
                head.appendChild(fileref);
                index = index + 1;
                // Used to call a callback function
                fileref.onload = function() {
                    loadFile(index);
                }
            } else if (fn) {
                fn();
            }
        }
        loadFile(0);
    }

function plotResourcePlot_''' + t + '''() {
    // get the item
    // parent element is a table cell, needs enlarge
    document.getElementById(
        "res_''' + t + '''").parentElement.setAttribute("height", "300px;");
    $("#res_''' + t + '''").css("height", "300px");
    $("#res_''' + t + '''").css("width", "100%");
    $("#res_''' + t + '''").css("min-height", "300px");

    var cpu = [''' + ','.join([f'[{x*1000},{y}]' for x, y in zip(etime, cpu)]) + '''];
    var mem = [''' + ','.join([f'[{x*1000},{y}]' for x, y in zip(etime, mem)]) + '''];

    $.plot('#res_''' + t + '''', [{
                data: cpu,
                label: "CPU (%)"
            },
            {
                data: mem,
                label: "mem (M)",
                yaxis: 2
            }
        ], {
            xaxes: [{
                mode: "time"
            }],
            yaxes: [{
                min: 0
            }, {
                position: "right",
                tickFormatter: function(v, axis) {
                    return v.toFixed(1) + 'M';
                }
            }],
            legend: {
                position: "nw"
            }
        });
}

var dt = 100;
// the frontend might be notified before the table is inserted as results.
function showResourceFigure_''' + t + '''() {
    if ( $("#res_''' + t + '''").length === 0) {
          dt = dt * 1.5; // slow-down checks for datatable as time goes on;
          setTimeout(showResourceFigure_''' + t + ''', dt);
          return;
    } else {
        $("#res_''' + t + '''").css('width', "100%").css('height', "300px");
        loadFiles(["http://www.flotcharts.org/flot/jquery.flot.js",
             "http://www.flotcharts.org/flot/jquery.flot.time.js"
            ], plotResourcePlot_''' + t + ''');
    }
}
showResourceFigure_''' + t + '''()
</script>
''')
    # remove jobs that are older than 1 month
    if to_be_removed:
        purge_tasks(to_be_removed, verbosity=0)


def kill_tasks(tasks, tags=None):
    #
    import glob
    from multiprocessing.pool import ThreadPool as Pool
    if not tasks:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [os.path.basename(x)[:-5] for x in tasks]
    else:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [os.path.basename(x)[:-5] for x in matched]
            if not matched:
                env.logger.warning(f'{t} does not match any existing task')
            else:
                all_tasks.extend(matched)
    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x).split(' '))]

    if not all_tasks:
        env.logger.warning('No task to kill')
        return
    all_tasks = sorted(list(set(all_tasks)))
    # at most 20 threads
    p = Pool(min(20, len(all_tasks)))
    killed = p.map(kill_task, all_tasks)
    for s, t in zip(killed, all_tasks):
        print(f'{t}\t{s}')


def kill_task(task):
    status = check_task(task)['status']
    if status == 'pending':
        # the task engine will remove it from lists if killed through a task engine
        # but it can also be killed through command line externally, so we will need
        # to mark the task as killed
        pulse_file = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + '.pulse')
        os.open(pulse_file, flags=os.O_CREAT | os.O_RDONLY)
        return 'aborted'
    # remove job file as well
    job_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.sh')
    if os.path.isfile(job_file):
        try:
            os.remove(job_file)
        except Exception:
            pass
    if status != 'running':
        return status
    # job is running
    pulse_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.pulse')
    os.chmod(pulse_file, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return 'killed'


def purge_tasks(tasks, purge_all=False, age=None, status=None, tags=None, verbosity=2):
    # verbose is ignored for now
    import glob
    if tasks:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                       for x in matched]
            all_tasks.extend(matched)
        is_all = False
    else:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                     for x in tasks]
        is_all = True
    #
    if age is not None:
        age = expand_time(age, default_unit='d')
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    if status:
        # at most 20 threads
        task_status = check_tasks([x[0] for x in all_tasks], is_all)
        all_tasks = [x for x in all_tasks if task_status[x[0]]['status']
                     in status]

    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x[0]).split(' '))]
    #
    # remoe all task files
    all_tasks = set([x[0] for x in all_tasks])
    if all_tasks:
        #
        # find all related files, including those in nested directories
        from collections import defaultdict
        to_be_removed = defaultdict(list)
        for dirname, _, filelist in os.walk(os.path.join(os.path.expanduser('~'), '.sos', 'tasks')):
            for f in filelist:
                ID = os.path.basename(f).split('.', 1)[0]
                if ID in all_tasks:
                    to_be_removed[ID].append(os.path.join(dirname, f))
        #
        cache_file: str = os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', 'status_cache.pickle')

        if os.path.isfile(cache_file):
            with fasteners.InterProcessLock(cache_file + '_'):
                with open(cache_file, 'rb') as cache:
                    status_cache = pickle.load(cache)
        else:
            status_cache = {}
        for task in all_tasks:
            removed = True
            for f in to_be_removed[task]:
                try:
                    if verbosity > 3:
                        env.logger.trace(f'Remove {f}')
                    os.remove(f)
                except Exception as e:
                    removed = False
                    if verbosity > 0:
                        env.logger.warning(f'Failed to purge task {task[0]}')
            status_cache.pop(task, None)
            if removed and verbosity > 1:
                env.logger.info(f'Task ``{task}`` removed.')
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'wb') as cache:
                pickle.dump(status_cache, cache)
    elif verbosity > 1:
        env.logger.info('No matching tasks')
    if purge_all:
        matched = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*'))
        count = 0
        for f in matched:
            if os.path.isdir(f):
                import shutil
                try:
                    shutil.rmtree(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f'Failed to remove {f}')
            else:
                try:
                    os.remove(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f'Failed to remove {e}')
        if count > 0 and verbosity > 1:
            env.logger.info(
                f'{count} other files and directories are removed.')
    return ''
