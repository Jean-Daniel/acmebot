#!/usr/bin/env python3

# Certificate manager using ACME protocol
#
# To install on Debian:
# apt-get install build-essential libssl-dev libffi-dev python3-dev python3-pip
# pip3 install -r requirements.txt
import logging
import os

from acmebot import AcmeError
from acmebot.manager import AcmeManager


def verify_requirements():
    import os
    import pkg_resources
    import re
    requirements_file_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'requirements.txt')
    if os.path.exists(requirements_file_path):
        requirements_met = True
        with open(requirements_file_path, 'r') as requirements_file:
            requirements = requirements_file.read()
            for requirement in requirements.split('\n'):
                if requirement:
                    package, comparison, version = (re.split('\s?(<?>?==?)\s?', requirement) + ['', ''])[:3]
                    try:
                        installed_version = pkg_resources.get_distribution(package).version
                        if '<=' == comparison:
                            if installed_version > version:
                                print('Package', package, 'is more recent than', version)
                                requirements_met = False
                        elif '==' == comparison:
                            if installed_version != version:
                                print('Package', package, 'is not version', version)
                                requirements_met = False
                        elif '>=' == comparison:
                            if installed_version < version:
                                print('Package', package, 'is older than', version)
                                requirements_met = False
                    except Exception:
                        print('Package', package, 'is not installed')
                        requirements_met = False
        if not requirements_met:
            print('Run "pip3 install -r {path}" to complete installation'.format(path=requirements_file_path))
            exit()


verify_requirements()

if __name__ == '__main__':  # called from the command line
    try:
        script_dir = os.path.dirname(os.path.realpath(__file__))
        script_name = os.path.basename(__file__)
        AcmeManager(script_dir, 'acmebot').run()
    except AcmeError as e:
        logging.exception(e)
