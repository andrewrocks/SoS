#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil
import subprocess
import sys
import time
import unittest
import sqlite3
from contextlib import contextmanager

from sos.hosts import Host
from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env
from sos.tasks import TaskParams, TaskFile

from sos.workflow_executor import Base_Executor

has_docker = sys.platform != 'win32'
try:
    if sys.platform != 'win32':
        subprocess.check_output(
            'docker ps | grep test_sos', shell=True).decode()
except subprocess.CalledProcessError:
    subprocess.call('sh build_test_docker.sh', shell=True)
    try:
        subprocess.check_output(
            'docker ps | grep test_sos', shell=True).decode()
    except subprocess.CalledProcessError:
        print('Failed to set up a docker machine with sos')
        has_docker = False


@contextmanager
def cd_new(path):
    old_dir = os.getcwd()
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_dir)


def get_tasks():
    conn = sqlite3.connect('.sos/workflow_signatures.db')
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT id FROM workflows WHERE entry_type = "task"')
    return [x[0] for x in cur.fetchall()]


class TestTask(unittest.TestCase):

    def setUp(self):
        env.reset()
        subprocess.call('sos remove -s', shell=True)
        # self.resetDir('~/.sos')
        self.temp_files = []
        Host.reset()

    def touch(self, files):
        '''create temporary files'''
        if isinstance(files, str):
            files = [files]
        #
        for f in files:
            with open(f, 'w') as tmp:
                tmp.write('test')
        #
        self.temp_files.extend(files)

    def testTaskFile(self):
        '''Test task file handling'''
        for ext in ('.pulse', '.out', '.err', '.task', '.sh'):
            filename = os.path.join(
                os.path.expanduser('~'), '.sos', 'tasks',
                'ffffffffffffffff' + ext)
            if os.path.isfile(filename):
                os.remove(filename)
        params = TaskParams(
            name='ffffffffffffffff',
            global_def={},
            task='b=a',
            sos_dict={'a': 1},
            tags=['b', 'a'])
        a = TaskFile('ffffffffffffffff')
        a.save(params)
        self.assertEqual(a.tags, 'a b')
        for ext in ('.pulse', '.out', '.err', '.sh'):
            with open(
                    os.path.join(
                        os.path.expanduser('~'), '.sos', 'tasks',
                        'ffffffffffffffff' + ext), 'w') as fh:
                fh.write(ext)
        self.assertFalse(a.has_stdout())
        self.assertFalse(a.has_stderr())
        a.add_outputs()
        #
        self.assertEqual(a.params.sos_dict['a'], 1)
        self.assertEqual(a.status, 'new')
        a.status = 'completed'
        self.assertLess(time.time() - a.last_updated, 2)
        self.assertEqual(a.status, 'completed')
        #
        # get and reset info
        info = a.info
        a.status = 'running'
        self.assertEqual(a.status, 'running')
        a.info = info
        self.assertEqual(a.status, 'completed')
        self.assertTrue(a.has_stdout())
        #
        a.add_result({'ret_code': 5})
        #
        a.tags = ['ee', 'd']
        self.assertEqual(a.tags, 'd ee')
        #a.add_tags(['kk'])
        #self.assertEqual(a.tags.split(), ['d', 'ee', 'kk'])
        #
        self.assertEqual(a.params.sos_dict['a'], 1)
        self.assertEqual(a.params.task, 'b=a')
        #

        self.assertEqual(a.stdout, '.out')
        self.assertEqual(a.stderr, '.err')
        self.assertEqual(a.pulse, '.pulse')
        self.assertEqual(a.shell, '.sh')
        self.assertTrue(a.has_stdout())
        self.assertTrue(a.has_stderr())
        self.assertTrue(a.has_pulse())
        self.assertTrue(a.has_shell())
        #
        self.assertFalse(a.has_signature())
        a.add_signature({'file': 'md5'})
        self.assertTrue(a.has_signature())
        self.assertEqual(a.signature['file'], 'md5')
        #
        #self.assertRaises(Exception, a.add_outputs)
        #
        a.reset()
        self.assertEqual(a.status, 'new')
        self.assertEqual(a.stdout, '')
        self.assertEqual(a.stderr, '')
        self.assertEqual(a.signature, {})
        a.add_outputs()
        a.add_result({'ret_code': 5})
        self.assertEqual(a.result['ret_code'], 5)

    def testWorkdir(self):
        '''Test workdir option for runtime environment'''
        import tempfile
        tdir = tempfile.mkdtemp()
        with open(os.path.join(tdir, 'aaa.pp'), 'w') as aaa:
            aaa.write('something')
        script = r"""
import os
[0]
task: workdir={0!r}

with open(os.path.join({1!r}, 'result.txt'), 'w') as res:
   for file in os.listdir({1!r}):
       res.write(file + '\n')
""".format(os.path.split(tdir)[0],
           os.path.split(tdir)[1])
        wf = SoS_Script(script).workflow()
        env.config['sig_mode'] = 'force'
        Base_Executor(wf).run()
        with open(os.path.join(tdir, 'result.txt')) as res:
            content = [x.strip() for x in res.readlines()]
            self.assertTrue('aaa.pp' in content)

    def testSequential(self):
        '''Test concurrency option for runtime environment'''
        env.max_jobs = 5
        env.config['sig_mode'] = 'force'
        script = SoS_Script(r"""
import time
[0]

repeat = range(4)
input: for_each='repeat'

task: concurrent=False

print('I am {}, waited {} seconds'.format(_index, _repeat + 1))
time.sleep(_repeat + 1)
print('I am {}, done'.format(_index))
""")
        wf = script.workflow()
        start = time.time()
        Base_Executor(wf).run()
        self.assertGreater(time.time() - start, 11)

    def testConcurrency(self):
        '''Test concurrency option for runtime environment'''
        env.max_jobs = 5
        env.config['sig_mode'] = 'force'
        script = SoS_Script(r"""
[0]

repeat = range(4)
input: for_each='repeat'

task:

import time
print('I am {}, waited {} seconds'.format(_index, _repeat + 1))
time.sleep(_repeat + 1)
print('I am {}, done'.format(_index))
""")
        wf = script.workflow()
        Base_Executor(wf).run()

    def testPrependPath(self):
        '''Test prepend path'''
        import stat
        if not os.path.isdir('temp'):
            os.mkdir('temp')
        if sys.platform == 'win32':
            with open(r'temp\temp_cmd.bat', 'w') as tc:
                tc.write('echo "a"')
        else:
            with open('temp/temp_cmd', 'w') as tc:
                tc.write('echo "a"')
            os.chmod('temp/temp_cmd',
                     stat.S_IXUSR | stat.S_IWUSR | stat.S_IRUSR)
        #
        script = SoS_Script(r"""
[1]
task:
run:
    temp_cmd
""")
        wf = script.workflow()
        env.config['sig_mode'] = 'force'
        #self.assertRaises(Exception, Base_Executor(wf).run)
        #
        # the following is supposed to create its own task file but
        # for some reason it uses the same task file
        #
        # use option env
        script = SoS_Script(r"""
import os
[1]
task: env={'PATH': 'temp' + os.pathsep + os.environ['PATH']}
run:
    temp_cmd
""")
        wf = script.workflow()
        env.config['sig_mode'] = 'force'
        Base_Executor(wf).run()
        #
        #
        script = SoS_Script(r"""
[1]
task: prepend_path='temp'
run:
    temp_cmd
""")
        wf = script.workflow()
        Base_Executor(wf).run()

    def testNoTask(self):
        env.config['sig_mode'] = 'force'
        script = SoS_Script(r'''

[10]
task:
run:
   sleep 0
''')
        wf = script.workflow()
        # this will always work and not through task
        Base_Executor(wf, config={'default_queue': 'None'}).run()
        #
        env.config['sig_mode'] = 'force'
        script = SoS_Script(r'''

[10]
task: queue=None
run:
   sleep 0
''')
        wf = script.workflow()
        # this will always work and not through task
        Base_Executor(wf).run()

    def testPassingVarToTask(self):
        '''Test passing used variable to tasks'''
        for i in range(10, 13):
            if file_target(f'myfile_{i}.txt').exists():
                file_target(f'myfile_{i}.txt').unlink()
        #
        env.config['sig_mode'] = 'force'
        script = SoS_Script(r'''
parameter: gvar = 10

[10]
# generate a file
tt = range(gvar, gvar + 3)
input: for_each='tt'
output: f"myfile_{_tt}.txt"
# additional comment

# _tt should be used in task
task:
run: expand=True
    echo {_tt}_{_index} > {_output:q}

''')
        wf = script.workflow()
        env.max_jobs = 4
        Base_Executor(wf).run()
        for t in range(10, 13):
            with open(f'myfile_{t}.txt') as tmp:
                self.assertEqual(tmp.read().strip(), str(t) + '_' + str(t - 10))
            if file_target(f'myfile_{t}.txt').exists():
                file_target(f'myfile_{t}.txt').unlink()

    def testMaxJobs(self):
        '''Test default max number of jobs'''
        script = SoS_Script(r'''

[10]
input: for_each=[{'a': range(2)}, {'b': range(3)}]

task:
run: expand=True
    echo "a = {a}, b = {b}"
''')
        env.config['max_running_jobs'] = 2
        wf = script.workflow()
        Base_Executor(wf).run()

    def testKillAndPurge(self):
        '''Test no wait'''
        subprocess.call(['sos', 'purge'])
        with open('test_purge.sos', 'w') as script:
            script.write(r'''
[10]
task:
run:
    echo Try to kill
    sleep 20
''')
        subprocess.Popen('sos run test_purge.sos -s force', shell=True)
        time.sleep(5)
        subprocess.call(['sos', 'kill', '--all'])
        for i in range(20):
            output = subprocess.check_output(['sos', 'status', '-v',
                                              '1']).decode()
            if 'killed' in output or 'aborted' in output or 'completed' in output:
                break
            self.assertFalse(
                i > 10,
                'Task should be killed within 10 seconds, got {}'.format(
                    output))
            time.sleep(1)
        # test purge by status
        subprocess.call(['sos', 'purge', '--status', 'aborted'])
        self.assertFalse('killed' in subprocess.check_output(
            ['sos', 'status', '-v', '3']).decode())
        # purge by all is not tested because it is dangerous

    def testConcurrentTask(self):
        '''Test submitting tasks from concurrent substeps'''
        for f in [f'con_{x}.txt' for x in range(5)]:
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
[10]
input: for_each={'i': range(5)}
output: f'con_{i}.txt'

task:
run: expand=True
  echo {i} > {_output}
''')
        wf = script.workflow()
        Base_Executor(wf, config={'sig_mode': 'force'}).run()
        for f in [f'con_{x}.txt' for x in range(5)]:
            self.assertTrue(file_target(f).exists())

    def testSharedOption(self):
        '''Test shared option of task'''
        for f in ('a.txt', 'a100.txt'):
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
[10: shared = 'a']
output: 'a.txt'
task: shared={'a': 'int(open("a.txt").read())'}
run:
  echo 100 > a.txt

[20]
run: expand=True
    touch a{a}.txt
''')
        wf = script.workflow()
        Base_Executor(wf, config={'sig_mode': 'force'}).run()
        self.assertTrue(os.path.isfile("a100.txt"))
        # sequence of var or mapping
        for f in ('a.txt', 'a100.txt'):
            if file_target(f).exists():
                file_target(f).unlink()
        script = SoS_Script('''
[10: shared = ['a', 'b']]
output: 'a.txt'
task: shared=[{'a': 'int(open("a.txt").read())'}, 'b']
b = 20
run:
  echo 100 > a.txt

[20]
run: expand=True
    touch a{a}_{b}.txt
''')
        wf = script.workflow()
        Base_Executor(wf, config={'sig_mode': 'force'}).run()
        self.assertTrue(os.path.isfile("a100_20.txt"))

        script = SoS_Script('''
[10 (simulate): shared=['rng', 'step_rng']]
input: for_each={'i': range(5)}
task: shared='rng'
print(f"{i}")
import random
rng = random.randint(1, 1000)
''')
        wf = script.workflow()
        Base_Executor(wf).run()
        var = env.sos_dict['rng']
        self.assertTrue(isinstance(var, int))
        self.assertTrue(isinstance(env.sos_dict['step_rng'], list))
        self.assertEqual(env.sos_dict['step_rng'][-1], var)
        # run it again, should get from signature
        #
        #Base_Executor(wf).run()
        #self.assertEqual(var, env.sos_dict['rng'])

    def testTrunkSizeOption(self):
        '''Test option trunk_size'''
        with open('test_trunksize.sos', 'w') as tt:
            tt.write('''
[10]
input: for_each={'I': range(10)}
task: trunk_size=5, cores=1, mem='1M', walltime='10m'
run: expand=True
    echo {I} > {I}.txt
    sleep 2
''')
        wf = SoS_Script(filename='test_trunksize.sos').workflow()
        for i in range(10):
            if os.path.isfile(f'{i}.txt'):
                file_target(f'{i}.txt').unlink()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'script': 'test_trunksize.sos',
                'max_running_jobs': 10,
                'bin_dirs': [],
                'workflow_args': [],
                'output_dag': '',
                'output_report': None,
                'targets': [],
                'max_procs': 4,
                'default_queue': None,
                'workflow': 'default',
                'workdir': '.',
            }).run()

        for i in range(10):
            self.assertTrue(os.path.isfile(f'{i}.txt'))

    def testTrunkWorkersOption(self):
        '''Test option trunk_workers'''
        with open('test_trunkworker.sos', 'w') as tt:
            tt.write('''
[10]
input: for_each={'I': range(12)}
task: trunk_size=6, trunk_workers=3, mem='1M', walltime='10m'
run: expand=True
    echo {I} > {I}.txt
    sleep 2
''')
        wf = SoS_Script(filename='test_trunkworker.sos').workflow()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'script': 'test_trunkworker.sos',
                'max_running_jobs': 10,
                'bin_dirs': [],
                'workflow_args': [],
                'output_dag': '',
                'output_report': None,
                'targets': [],
                'max_procs': 4,
                'default_queue': None,
                'workflow': 'default',
                'workdir': '.',
            }).run()
        for i in range(10):
            self.assertTrue(os.path.isfile('{}.txt'.format(i)))

    def testTaskTags(self):
        '''Test option tags of tasks'''
        import random
        tag = "tag{}".format(random.randint(1, 100000))
        with open('test_tags.sos', 'w') as tt:
            tt.write('''
[10]
input: for_each={{'i': range(10)}}
task: tags='{}', trunk_size=2
sh: expand=True
  echo {} {{i}}
'''.format(tag, tag))
        wf = SoS_Script(filename='test_tags.sos').workflow()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'script': 'test_trunkworker.sos',
                'max_running_jobs': 10,
                'bin_dirs': [],
                'workflow_args': [],
                'output_dag': '',
                'output_report': None,
                'targets': [],
                'max_procs': 4,
                'workflow': 'default',
                'workdir': '.',
            }).run()
        ret = subprocess.check_output(
            'sos status -t {}'.format(tag), shell=True).decode()
        self.assertEqual(len(ret.splitlines()), 5, "Obtained {}".format(ret))
        # test multiple tags
        tag1 = "tag{}".format(random.randint(1, 100000))
        tag2 = "tag{}".format(random.randint(1, 100000))
        with open('test_tags.sos', 'w') as tt:
            tt.write('''
[10]
input: for_each={{'i': range(2)}}
task: tags=['{}', '{}']
sh: expand=True
  echo {} {{i}}
'''.format(tag1, tag2, tag1))
        wf = SoS_Script(filename='test_tags.sos').workflow()
        Base_Executor(
            wf,
            config={
                'sig_mode': 'force',
                'script': 'test_trunkworker.sos',
                'max_running_jobs': 10,
                'bin_dirs': [],
                'workflow_args': [],
                'output_dag': '',
                'output_report': None,
                'targets': [],
                'max_procs': 4,
                'workflow': 'default',
                'workdir': '.',
            }).run()
        ret = subprocess.check_output(
            'sos status -t {}'.format(tag2), shell=True).decode()
        self.assertEqual(len(ret.splitlines()), 2, "Obtained {}".format(ret))

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testMaxMem(self):
        '''Test server restriction max_mem'''
        script = SoS_Script('''
[10]
task: mem='2G'
print('a')
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'docker_limited',
                    'sig_mode': 'force',
                }).run)

    def testLocalMaxMem(self):
        '''Test server restriction max_mem'''
        script = SoS_Script('''
[10]
task: mem='2G'
print('a')
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'local_limited',
                    'sig_mode': 'force',
                }).run)

    def testRuntimeMaxWalltime(self):
        '''Test server max_walltime option'''
        script = SoS_Script('''
[10]
task:
import time
time.sleep(25)
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'docker_limited',
                    'sig_mode': 'force',
                }).run)

    def testLocalRuntimeMaxWalltime(self):
        '''Test server max_walltime option'''
        script = SoS_Script('''
[10]
task:
import time
time.sleep(15)
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'local_limited',
                    'sig_mode': 'force',
                }).run)

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testMaxCores(self):
        '''Test server restriction max_cores'''
        script = SoS_Script('''
[10]
task: cores=8
print('a')
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'docker_limited',
                    'sig_mode': 'force',
                }).run)

    def testLocalMaxCores(self):
        '''Test server restriction max_cores'''
        script = SoS_Script('''
[10]
task: cores=8
print('a')
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'local_limited',
                    'sig_mode': 'force',
                }).run)

    def testListHosts(self):
        '''test list hosts using sos status -q'''
        for v in ['0', '1', '3', '4']:
            output = subprocess.check_output(
                ['sos', 'remote', 'list', '-c', '~/docker.yml', '-q', '-v',
                 v]).decode()
            self.assertTrue('local_limited' in output)

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testMaxWalltime(self):
        '''Test server restriction max_walltime'''
        script = SoS_Script('''
[10]
task: walltime='1:00:00'
print('a')
''')
        wf = script.workflow()
        self.assertRaises(
            Exception,
            Base_Executor(
                wf,
                config={
                    'config_file': '~/docker.yml',
                    'default_queue': 'docker_limited',
                    'sig_mode': 'force',
                }).run)

    def testPurgeWithoutOption(self):
        '''Test sos purge to remove only tasks related to current directory'''
        with cd_new('temp_a'):
            with open('test.sos', 'w') as tst:
                tst.write('''
input: for_each={'i': range(2)}
output: f'a{i}.txt'
task:
run: expand=True
echo temp_a
touch {_output}
''')
            subprocess.call('sos run test -s force', shell=True)
            tasks_a = get_tasks()
        #
        with cd_new('temp_b'):
            with open('test.sos', 'w') as tst:
                tst.write('''
input: for_each={'i': range(2)}
output: f'a{i}.txt'
task:
run: expand=True
echo temp_b
touch {_output}
''')
            subprocess.call('sos run test -s force', shell=True)
            tasks_b = get_tasks()
            # only tasks related to this directory will be listed.
            self.assertEqual(
                len(tasks_b),
                len(
                    subprocess.check_output('sos status -v1',
                                            shell=True).decode().splitlines()))
            subprocess.call('sos purge', shell=True)
        # check tasks
        tasks = [
            x.split()[0] for x in subprocess.check_output(
                'sos status -v1 --all', shell=True).decode().splitlines()
        ]
        self.assertTrue(all(x in tasks for x in tasks_a))
        self.assertFalse(any(x in tasks for x in tasks_b))

    def testPurgeAllWithOption(self):
        '''Test sos purge all with options such as -s completed'''
        with cd_new('temp_c'):
            with open('test.sos', 'w') as tst:
                tst.write('''
input: for_each={'i': range(2)}
output: f'a{i}.txt'
task:
run: expand=True
echo temp_a
touch {_output}
''')
            subprocess.call('sos run test -s force', shell=True)
            tasks = get_tasks()
            subprocess.call('sos purge --all -s failed', shell=True)
        # check tasks
        taskstatus = [
            x.split()[0] for x in subprocess.check_output(
                'sos status --all -v1', shell=True).decode().splitlines()
        ]
        self.assertTrue(all(x in taskstatus for x in tasks))
        # purge one of them
        subprocess.call(f'sos purge {tasks[0]}', shell=True)
        taskstatus = [
            x.split()[0] for x in subprocess.check_output(
                'sos status --all -v1', shell=True).decode().splitlines()
        ]
        self.assertTrue(tasks[0] not in taskstatus)
        self.assertTrue(tasks[1] in taskstatus)
        #
        subprocess.call(f'sos purge --all', shell=True)
        taskstatus = [
            x.split()[0] for x in subprocess.check_output(
                'sos status --all -v1', shell=True).decode().splitlines()
        ]
        self.assertTrue(tasks[1] not in taskstatus)

    def testResubmitTaskWithDifferentWalltime(self):
        '''Test resubmission of tasks with different walltime #1019'''
        with cd_new('temp_walltime'):
            with open('test.sos', 'w') as tst:
                tst.write('''
task: walltime='1m'
sh:
echo 0.1
''')
            subprocess.call('sos run test -s force', shell=True)
            tasks = get_tasks()
            out = subprocess.check_output(
                f'sos status {tasks[0]} -v4', shell=True)
            self.assertTrue('00:01:00' in out.decode())
            with open('test1.sos', 'w') as tst:
                tst.write('''
task: walltime='2m'
sh:
echo 0.1
''')
            subprocess.call('sos run test1 -s force', shell=True)
            new_tasks = get_tasks()
            self.assertEqual(tasks, new_tasks)
            #
            out = subprocess.check_output(
                f'sos status {tasks[0]} -v4', shell=True)
            self.assertTrue('00:02:00' in out.decode())

    def testTaskSignature(self):
        '''Test re-execution of tasks'''
        with cd_new('temp_signature'):
            with open('test.sos', 'w') as tst:
                tst.write('''
task:
sh:
sleep 2
''')
            subprocess.call('sos run test -s force', shell=True)
            tasks = get_tasks()
            tf = TaskFile(tasks[0])
            self.assertTrue(tf.has_signature())
            self.assertEqual(tf.status, 'completed')
            tf.tags_created_start_and_duration()
            #
            subprocess.call('sos run test', shell=True)
            self.assertLess(tf.tags_created_start_and_duration()[3], 1)

    def testWrongHost(self):
        script = SoS_Script('''
[10]
task: walltime='1:00:00', queue='undefined'
print('a')
''')
        wf = script.workflow()
        self.assertRaises(Exception, Base_Executor(wf).run)

    def testOutputInTask(self):
        '''Test passing _output to task #1136'''
        script = SoS_Script('''
chunks  = [1,2]
[1]
input: for_each = 'chunks'
output: f'{_chunks}.txt'
_output.touch()

[2]
input: group_with = 'chunks'
output: summary_stats = f'{_input}.summary', ld_matrix = f'{_input}.result'
task:

python3: expand="${ }"
       open("${_output['summary_stats']}", 'w').close()
       open("${_output['ld_matrix']}", 'w').close()
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    def testRepeatedTasks(self):
        '''Test statement before task #1142 '''
        script = SoS_Script('''
[1]
input: for_each=dict(i=range(5))

print(f'This is for {i}')
task:  walltime='10m'
print(f'this is task {i}')

''')
        for i in range(5):
            wf = script.workflow()
            Base_Executor(wf, config={'sig_mode': 'force'}).run()

    def testPassingParametersToTask(self):
        '''Test passing of parameters in global section to tasks #1155'''
        script = SoS_Script('''\
[global]
parameter: par=5
def a():
  print(par)

[default]
task:
a()
''')
        wf = script.workflow()
        Base_Executor(wf, config={'sig_mode': 'force'}).run()

    def testTrunkSizeWithStopIf(self):
        '''Test a case when some tasks are not submitted due to holes in slots #1159'''
        for i in range(5):
            f = f'{i+1}.txt'
            if os.path.isfile(f):
                os.remove(f)
        script = SoS_Script('''\
[1]
output: [f'{x+1}.txt' for x in range(5)]
for i in range(5):
  name = f'{i+1}.txt'
  if i not in [0,1,2]:
    path(name).touch()
  else:
    with open(name, 'w') as f:
      f.write('test it')

[2]
input: group_by = 1
output: f'{_input:n}.out'
stop_if(_input.stat().st_size==0, no_output=True)

task: trunk_size = 80
_output.touch()
''')
        wf = script.workflow()
        Base_Executor(wf, config={'sig_mode': 'force'}).run()

    def testOutputFromMasterTask(self):
        '''Test splitting the output from master task #1203'''
        script = SoS_Script('''\
l=[x for x in range(1,13)]

[2]
input: for_each = 'l'
output: f'{_l}.out'

task: trunk_size = 4
_output.touch()

[3]
assert _input == f'{_index+1}.out'
''')
        wf = script.workflow()
        Base_Executor(wf).run()

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testSyncInputOutputAndRerun(self):
        '''Test sync input and output with remote host'''
        for i in range(4):
            if os.path.isfile(f'test_{i}.txt'):
                os.remove(f'test_{i}.txt')
            if os.path.isfile(f'test_{i}.bak'):
                os.remove(f'test_{i}.bak')
        import random
        script = SoS_Script('''
parameter: g = 100

[10]
input: for_each=dict(i=range(4))
output: f'test_{i}.txt'

with open(f'test_{i}.txt', 'w') as tst:
    tst.write(f'test_{i}_{g}')

[20]
output: _input.with_suffix('.bak')

task:

with open(_input, 'r') as inf, open(_output, 'w') as outf:
	outf.write(inf.read() + '.bak')
''')
        wf = script.workflow()
        val = random.randint(1, 10000)
        Base_Executor(
            wf,
            args=['--g', str(val)],
            config={
                'config_file': '~/docker.yml',
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        # now check if
        for i in range(4):
            self.assertTrue(os.path.isfile(f'test_{i}.txt'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')
            self.assertTrue(os.path.isfile(f'test_{i}.bak'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')
        #
        # test rerun the task file on local host
        for i in range(4):
            if os.path.isfile(f'test_{i}.txt'):
                os.remove(f'test_{i}.txt')
            if os.path.isfile(f'test_{i}.bak'):
                os.remove(f'test_{i}.bak')
        Base_Executor(
            wf, args=['--g', str(val)], config={
                'sig_mode': 'force'
            }).run()
        for i in range(4):
            self.assertTrue(os.path.isfile(f'test_{i}.txt'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')
            self.assertTrue(os.path.isfile(f'test_{i}.bak'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testSyncMasterTask(self):
        '''Test sync input and output with remote host with trunksize'''
        for i in range(4):
            if os.path.isfile(f'test_{i}.txt'):
                os.remove(f'test_{i}.txt')
            if os.path.isfile(f'test_{i}.bak'):
                os.remove(f'test_{i}.bak')
        import random
        script = SoS_Script('''
parameter: g = 100

[10]
input: for_each=dict(i=range(4))
output: f'test_{i}.txt'

with open(f'test_{i}.txt', 'w') as tst:
    tst.write(f'test_{i}_{g}')

[20]
output: _input.with_suffix('.bak')

task: trunk_size=2

with open(_input, 'r') as inf, open(_output, 'w') as outf:
	outf.write(inf.read() + '.bak')
''')
        wf = script.workflow()
        val = random.randint(1, 10000)
        Base_Executor(
            wf,
            args=['--g', str(val)],
            config={
                'config_file': '~/docker.yml',
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        # now check if
        for i in range(4):
            self.assertTrue(os.path.isfile(f'test_{i}.txt'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')
            self.assertTrue(os.path.isfile(f'test_{i}.bak'))
            with open(f'test_{i}.bak') as outf:
                self.assertEqual(outf.read(), f'test_{i}_{val}.bak')

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testRemoteInputTarget(self):
        '''Test the use of remote target'''
        if os.path.isfile(f'vars.sh'):
            os.remove(f'vars.sh')
        if os.path.isfile(f'vars1.sh'):
            os.remove(f'vars1.sh')
        script = SoS_Script('''

[10]
input: remote('/lib/init/vars.sh')
output: f'vars1.sh'

task:

with open(_input, 'r') as inf, open(_output, 'w') as outf:
	outf.write(inf.read())
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        self.assertFalse(os.path.isfile('vars.sh'))
        self.assertTrue(os.path.isfile('vars1.sh'))

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testRemoteOutputTarget(self):
        '''Test the use of remote target'''
        if os.path.isfile(f'vars.sh'):
            os.remove(f'vars.sh')
        if os.path.isfile(f'vars1.sh'):
            os.remove(f'vars1.sh')
        script = SoS_Script('''
[10]
input: remote('/lib/init/vars.sh')
output: remote(f'vars1.sh')

task:

with open(_input, 'r') as inf, open(_output, 'w') as outf:
	outf.write(inf.read())
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        self.assertFalse(os.path.isfile('vars.sh'))
        self.assertFalse(os.path.isfile('vars1.sh'))
        #
        # case with trunksize
        if os.path.isfile(f'vars.sh'):
            os.remove(f'vars.sh')
        if os.path.isfile(f'vars1.sh'):
            os.remove(f'vars1.sh')
        script = SoS_Script('''
[10]
import os
input: remote('/lib/init/vars.sh'), remote('/lib/init/init-d-script'), group_by=1
output: remote(os.path.basename(str(_input)))

task: trunk_size=2

with open(_input, 'r') as inf, open(_output, 'w') as outf:
	outf.write(inf.read())
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        self.assertFalse(os.path.isfile('vars.sh'))
        self.assertFalse(os.path.isfile('init-d-script'))

    @unittest.skipIf(not has_docker, "Docker container not usable")
    def testDelayedInterpolation(self):
        '''Test delayed interpolation with expression involving remote objects'''
        # purge all previous tasks
        if file_target('test.py').exists():
            file_target('test.py').unlink()
        if file_target('test.py.bak').exists():
            file_target('test.py.bak').unlink()
        script = SoS_Script('''
[10]
output: remote('test.py')
task:
run:
    touch test.py

[20]
output: remote(f"{_input:R}.bak")
task:
run: expand=True
    cp {_input} {_output}
''')
        wf = script.workflow()
        Base_Executor(
            wf,
            config={
                'config_file': '~/docker.yml',
                # do not wait for jobs
                'wait_for_task': True,
                'default_queue': 'docker',
                'sig_mode': 'force',
            }).run()
        # this file is remote only
        self.assertFalse(os.path.isfile('test.py'))
        self.assertFalse(os.path.isfile('test.py.bak'))


if __name__ == '__main__':
    unittest.main()
