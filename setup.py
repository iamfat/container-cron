import os
import sys
import codecs
from setuptools import setup, find_packages
from shutil import rmtree

here = os.path.abspath(os.path.dirname(__file__))
package = 'container_cron'
version = '0.1.1'

with codecs.open(os.path.join(here, 'README.md')) as f:
    long_description = '\n' + f.read()

def print_run(cmd, err_exit=False):
    print('RUNNING: {}'.format(cmd))
    r = os.system(cmd)
    if err_exit and r != 0:
        sys.exit(r)


if sys.argv[-1] == 'publish':
    try:
        rmtree(os.path.join(here, 'dist'))
    except FileNotFoundError:
        pass

    print_run('{0} setup.py sdist'.format(sys.executable), True)
    print_run('twine upload dist/*', True)
    print_run('git tag v{}'.format(version))
    print_run('git push --tags')
    sys.exit()

required = [
    'containerd>=1.5',
    'apscheduler>=3',
    'python-crontab>=2'
]

setup(
    name='container-cron',
    version=version,
    description='An automated service importing scheduled jobs from /etc/cron.d in running containers.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Jia Huang',
    author_email='iamfat@gmail.com',
    url='https://github.com/iamfat/container-cron',
    packages=find_packages(exclude=['tests']),
    install_requires=required,
    entry_points={
        'console_scripts': [
            'container-cron={}:main'.format(package)
        ]
    },
    package_data={
        '': ['LICENSE']
    },
    license='MIT',
    python_requires='>=3.5',
    classifiers=[
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy'
    ]
)