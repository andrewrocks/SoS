#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import glob
import os
import sys
import shutil
import subprocess
import unittest
import getpass

from sos._version import __version__
from sos.parser import SoS_Script
from sos.targets import file_target
from sos.utils import env, load_config_files
# if the test is imported under sos/test, test interacive executor
from sos.workflow_executor import Base_Executor


class TestConfig(unittest.TestCase):

    def setUp(self):
        env.reset()
        subprocess.call('sos remove -s', shell=True)
        # self.resetDir('~/.sos')
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            if file_target(f).exists():
                file_target(f).unlink()

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

    def resetDir(self, dirname):
        if os.path.isdir(os.path.expanduser(dirname)):
            shutil.rmtree(os.path.expanduser(dirname))
        os.mkdir(os.path.expanduser(dirname))

    def testCommandLine(self):
        '''Test command line arguments'''
        self.assertEqual(
            subprocess.call(
                'sos config -h',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config -g --get',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config --get',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config -g --set a 5',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config --get a',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        self.assertEqual(
            subprocess.call(
                'sos config -g --unset a',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)

    def testGlobalVars(self):
        '''Test SoS defined variables'''
        script = SoS_Script(r"""
[0]
""")
        wf = script.workflow()
        Base_Executor(wf).run(mode='dryrun')
        self.assertEqual(env.sos_dict['SOS_VERSION'], __version__)
        self.assertTrue(isinstance(env.sos_dict['CONFIG'], dict))
        #
        with open('local_cfg.yml', 'w') as cfg:
            cfg.write('my_config: 5')
        #
        Base_Executor(wf, config={'config_file': 'local_cfg.yml'}).run()
        self.assertEqual(env.sos_dict['CONFIG']['my_config'], 5)

    def testConfigSet(self):
        '''Test interpolation of config'''
        self.assertEqual(
            subprocess.call(
                'sos config --set cut 0.5 -c myconfig.yml',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['cut'], 0.5)
        #
        self.assertEqual(
            subprocess.call(
                'sos config --set cut1 0.5 2 3 -c myconfig.yml',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['cut1'], [0.5, 2, 3])
        #
        self.assertEqual(
            subprocess.call(
                'sos config --set cut2 a3 -c myconfig.yml',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['cut2'], 'a3')
        #
        self.assertEqual(
            subprocess.call(
                'sos config --set cut3 a b c -c myconfig.yml',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['cut3'], ['a', 'b', 'c'])
        #
        self.assertEqual(
            subprocess.call(
                '''sos config --set cut4 "{'A': 123}" -c myconfig.yml''',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['cut4'], {'A': 123})

    def testInterpolate(self):
        '''Test interpolation of config'''
        self.assertEqual(
            subprocess.call(
                '''sos config --set me '{user_name}@my' -c myconfig.yml''',
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                shell=True), 0)
        load_config_files('myconfig.yml')
        self.assertEqual(env.sos_dict['CONFIG']['me'],
                         f'{getpass.getuser().lower()}@my')


if __name__ == '__main__':
    unittest.main()
