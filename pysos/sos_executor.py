#!/usr/bin/env python3
#
# This file is part of Script of Scripts (sos), a workflow system
# for the execution of commands and scripts in different languages.
# Please visit https://github.com/bpeng2000/SOS
#
# Copyright (C) 2016 Bo Peng (bpeng@mdanderson.org)
##
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
import os
import sys
import yaml
import glob
import shlex
import shutil
import argparse

import multiprocessing as mp

from . import __version__
from .sos_step import Inspect_Step_Executor, Prepare_Step_Executor, Run_Step_Executor, Interactive_Step_Executor
from .utils import env, Error, WorkflowDict,  get_traceback, ProgressBar, \
    frozendict, natural_keys, dict_merge, ArgumentError
from .sos_eval import Undetermined, SoS_eval, SoS_exec
from .sos_script import SoS_Script, SoS_Step, SoS_ScriptContent
from .sos_syntax import SOS_SECTION_HEADER
from .dag import SoS_DAG

__all__ = []


def __null_func__(*args, **kwargs):
    '''This function will be passed to SoS's namespace and be executed
    to evaluate functions of input, output, and depends directives.'''
    return args, kwargs

class ExecuteError(Error):
    """Raised when there are errors in inspect mode. Such errors are not raised
    immediately, but will be collected and raised at the end """

    def __init__(self, workflow):
        Error.__init__(self, 'SoS workflow contains errors: %s' % workflow)
        self.workflow = workflow
        self.errors = []
        self.traces = []
        self.args = (workflow, )

    def append(self, line, error):
        lines = [x for x in line.split('\n') if x.strip()]
        if not lines:
            short_line = '<empty>'
        else:
            short_line = lines[0][:40] if len(lines[0]) > 40 else lines[0]
        self.errors.append(short_line)
        self.traces.append(get_traceback())
        if isinstance(error, Exception):
            self.message += '\n[%s] %s:\n\t%s' % (short_line, error.__class__.__name__, error)
        else:
            self.message += '\n[%s]:\n\t%s' % (short_line, error)

class Base_Executor:
    '''This is the base class of all executor that provides common
    set up and tear functions for all executors.'''
    def __init__(self, workflow=None, args=[], config_file=None, new_dict=True):
        self.workflow = workflow

        # if creating a new dictionary, set it up with some basic varibles
        # and functions
        if not new_dict:
            SoS_exec('import os, sys, glob')
            SoS_exec('from pysos import *')
            return

        env.sos_dict = WorkflowDict()

        # inject a few things
        env.sos_dict.set('__null_func__', __null_func__)
        env.sos_dict.set('__args__', args)
        env.sos_dict.set('__unknown_args__', args)
        # initial values
        env.sos_dict.set('SOS_VERSION', __version__)
        env.sos_dict.set('__step_output__', [])
        env.sos_dict.set('__execute_errors__', ExecuteError('' if self.workflow is None else self.workflow.name))

        # load configuration files
        cfg = {}
        sos_config_file = os.path.join(os.path.expanduser('~'), '.sos', 'config.yaml')
        if os.path.isfile(sos_config_file):
            try:
                with open(sos_config_file) as config:
                    cfg = yaml.safe_load(config)
            except Exception as e:
                raise RuntimeError('Failed to parse global sos config file {}, is it in YAML/JSON format? ({})'.format(sos_config_file, e))
        # local config file
        sos_config_file = os.path.join('.sos', 'config.yaml')
        if os.path.isfile(sos_config_file):
            try:
                with open(sos_config_file) as config:
                    dict_merge(cfg, yaml.safe_load(config))
            except Exception as e:
                raise RuntimeError('Failed to parse local sos config file {}, is it in YAML/JSON format? ({})'.format(sos_config_file, e))
        # user-specified configuration file.
        if config_file is not None:
            if not os.path.isfile(config_file):
                raise RuntimeError('Config file {} not found'.format(config_file))
            try:
                with open(config_file) as config:
                    dict_merge(cfg, yaml.safe_load(config))
            except Exception as e:
                raise RuntimeError('Failed to parse config file {}, is it in YAML/JSON format? ({})'.format(config_file, e))
        # set config to CONFIG 
        env.sos_dict.set('CONFIG', frozendict(cfg))

        SoS_exec('import os, sys, glob')
        SoS_exec('from pysos import *')

    def skip(self, section):
        if section.global_def:
            try:
                SoS_exec(section.global_def)
            except Exception as e:
                if env.verbosity > 2:
                    sys.stderr.write(get_traceback())
                raise RuntimeError('Failed to execute statements\n"{}"\n{}'.format(
                    section.global_def, e))
        #
        if 'skip' in section.options:
            if isinstance(section.options['skip'], Undetermined):
                try:
                    val_skip = section.options['skip'].value(section.sigil)
                    if val_skip is None:
                        val_skip = False
                except Exception as e:
                    raise RuntimeError('Failed to evaluate value of section option skip={}: {}'.format(section.options['skip'], e))
            else:
                val_skip = section.options['skip']
            if val_skip is None or val_skip is True:
                return True
            elif val_skip is not False:
                raise RuntimeError('The value of section option skip can only be None, True or False, {} provided'.format(val_skip))
        return False

    def inspect(self):
        '''Run the script in inspect mode to check for errors.'''
        env.run_mode = 'inspect'
        # passing run_mode to SoS dict so that users can execute blocks of
        # python statements in different run modes.
        env.sos_dict.set('run_mode', env.run_mode)

        # process steps of the pipeline
        for idx, section in enumerate(self.workflow.sections):
            if self.skip(section):
                continue
            #
            # execute section with specified input
            queue = mp.Queue()
            executor = Inspect_Step_Executor(section, queue)
            proc = mp.Process(target=executor.run)
            proc.start()
            res = queue.get()
            proc.join()
            # if the job is failed
            if isinstance(res, Exception):
                raise RuntimeError(res)
            #
            for k, v in res.items():
                env.sos_dict.set(k, v)
        # at the end
        exception = env.sos_dict['__execute_errors__']
        if exception.errors:
            # if there is any error, raise it
            raise exception

    def prepare(self):
        '''Run the script in prepare mode to prepare resources.'''
        env.run_mode = 'prepare'
        # passing run_mode to SoS dict so that users can execute blocks of
        # python statements in different run modes.
        env.sos_dict.set('run_mode', env.run_mode)
        
        # process step of the pipelinp
        #
        # the steps can be executed in the pool (Not implemented)
        # if nested = true, start a new progress bar
        dag = SoS_DAG()
        for idx, section in enumerate(self.workflow.sections):
            if self.skip(section):
                continue
            #
            # execute section with specified input
            queue = mp.Queue()
            executor = Prepare_Step_Executor(section, queue)
            proc = mp.Process(target=executor.run)
            proc.start()
            res = queue.get()
            proc.join()
            # if the job is failed
            if isinstance(res, Exception):
                raise RuntimeError(res)
            #
            #
            for k, v in res.items():
                env.sos_dict.set(k, v)
            #
            # build DAG with input and output files of step
            dag.add_step(res['__step_name__'], idx, res['__step_input__'], res['__step_depends__'], res['__step_output__'])
        #            
        # at the end
        exception = env.sos_dict['__execute_errors__']
        if exception.errors:
            # if there is any error, raise it
            raise exception
        #
        dag.connect()
        return dag


class Sequential_Executor(Base_Executor):
    #
    # Execute a workflow sequentially in batch mode
    def __init__(self, workflow, args=[], config_file=None, nested=False):
        Base_Executor.__init__(self, workflow, args, config_file, new_dict=not nested)

    def run(self):
        '''Execute a workflow with specified command line args. If sub is True, this
        workflow is a nested workflow and be treated slightly differently.
        '''
        env.run_mode = 'run'
        # passing run_mode to SoS dict so that users can execute blocks of
        # python statements in different run modes.
        env.sos_dict.set('run_mode', env.run_mode)
        # process step of the pipelinp
        #
        # the steps can be executed in the pool (Not implemented)
        # if nested = true, start a new progress bar
        prog = ProgressBar(self.workflow.name, len(self.workflow.sections),
            disp=len(self.workflow.sections) > 1 and env.verbosity == 1)
        for idx, section in enumerate(self.workflow.sections):
            if self.skip(section):
                continue
            # execute section with specified input
            queue = mp.Queue()
            executor = Run_Step_Executor(section, queue)
            proc = mp.Process(target=executor.run)
            proc.start()
            res = queue.get()
            proc.join()
            # if the job is failed
            if isinstance(res, Exception):
                raise RuntimeError(res)
            #
            for k, v in res.items():
                env.sos_dict.set(k, v)
            prog.progress(1)
        prog.done()
        # at the end
        exception = env.sos_dict['__execute_errors__']
        if exception.errors:
            # if there is any error, raise it
            raise exception


class Interactive_Executor(Base_Executor):
    '''Interactive executor called from by iPython Jupyter or Spyder'''
    def __init__(self):
        # we actually do not have our own workflow, everything is passed from ipython
        Base_Executor.__init__(self, new_dict=False)

    def parse_command_line(self, command_line):
        parser = argparse.ArgumentParser()
        # no default workflow so it will execute any workflow if the code piece
        # defines only one workflow
        parser.add_argument('workflow', metavar='WORKFLOW', nargs='?')
        # parser.add_argument('-j', type=int, metavar='JOBS', default=1, dest='__max_jobs__')
        parser.add_argument('-c', dest='__config__', metavar='CONFIG_FILE')
        #parser.add_argument('-r', dest='__report__', metavar='REPORT_FILE', 
        #    default=os.path.join('.sos', '__step_report.md'))
        runmode = parser.add_argument_group(title='Run mode options')
        runmode.add_argument('-f', action='store_true', dest='__rerun__')
        runmode.add_argument('-F', action='store_true', dest='__construct__')
        # default to 1 to avoid output env.logger.info to notebook
        parser.add_argument('-v', '--verbosity', type=int, choices=range(5), default=1)
        #
        args, workflow_args = parser.parse_known_args(shlex.split(command_line))
        return args, workflow_args

    def parse_script(self, code):
        '''Used by the kernel to judge if the code is complete'''
        return SoS_Script(content=code)

    def run(self, block, command_line=''):
        '''Execute a block of SoS script that is sent by iPython/Jupyer/Spyer
        The code can be simple SoS/Python statements, one SoS step, or more
        or more SoS workflows with multiple steps. This executor,
        1. adds a section header to the script if there is no section head
        2. execute the workflow in interactive mode, which is different from
           batch mode in a number of ways, which most notably without support
           for nested workflow.
        3. Optionally execute the workflow in inspection or preparation mode
           for debugging purposes.
        '''
        # if there is no valid code do nothing
        if not block.strip():
            return
        # if there is no section header, add a header so that the block
        # appears to be a SoS script with one section
        if not any([SOS_SECTION_HEADER.match(line) for line in block.split()]):
            block = '[interactive_0]\n' + block

        script = SoS_Script(content=block)
        env.run_mode = 'interactive'
        try:
            args, workflow_args = self.parse_command_line(command_line)
            self.workflow = script.workflow(args.workflow)

            if args.__rerun__:
                sig_mode = 'ignore'
            elif args.__construct__:
                sig_mode = 'construct'
            else:
                sig_mode = 'default'

            #if os.path.isfile(args.__report__):
            #    os.remove(args.__report__)

            # this is the result returned by the workflow, if the
            # last stement is an expression.
            last_res = None
            #
            for idx, section in enumerate(self.workflow.sections):
                if 'skip' in section.options:
                    val_skip = section.options['skip']
                    if val_skip is None or val_skip is True:
                        continue
                    elif val_skip is not False:
                        raise RuntimeError('The value of section option skip can only be None, True or False, {} provided'.format(val_skip))
                #
                last_res = Interactive_Step_Executor(section).run()
                # if the step is failed
                if isinstance(last_res, Exception):
                    raise RuntimeError(last_res)
            return last_res
        finally:
            env.verbosity = 1
            env.sig_mode = 'default'



