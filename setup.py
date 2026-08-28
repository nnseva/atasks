"""
An atasks setuptools based setup module.
"""

# To use a consistent encoding
from codecs import open
from os import path

# Always prefer setuptools over distutils
from setuptools import find_packages, setup


here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

# Get the requirements from the pip-requirements.txt file
with open(path.join(here, 'requirements.txt'), encoding='utf-8') as f:
    requirements = [l for l in f.read().split() if l]

setup(
    name='atasks',
    use_scm_version=True,
    packages=[
        'atasks',
        'atasks.transport',
        'atasks.transport.backends',
    ],
    platforms='any',
    install_requires=requirements,
    extras_require={
        'test': [
            'flake8',
            'wrapt',
            'tox',
            'isort',
            'pydocstyle',
            'backoff',
        ],
    },
    setup_requires=['setuptools-scm>=8'],
    include_package_data=True,
    data_files=[('.', ['requirements.txt'])],
    license='LGPL',
    description='ATasks executes async Tasks in separate processes distributed among a network.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='',
    zip_safe=False,
    author='Vsevolod Novikov',
    author_email='nnseva@gmail.com',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
        'Programming Language :: Python :: 3.15',
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)',
        'Topic :: Software Development :: Libraries',
    ],
)
