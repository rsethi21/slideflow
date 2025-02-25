'''Test script for running both unit tests and functional tests.'''

import os
import click
import multiprocessing
import logging
import tabulate  # type: ignore
import slideflow as sf
from slideflow.test import TestSuite
from slideflow.util import colors as col


@click.command()
@click.option('--slides', help='Path to directory containing slides',
              required=False, metavar='DIR')
@click.option('--out', help='Directory in which to store test project files.',
              required=False, metavar='DIR')
@click.option('--all', help='Perform all tests.',
              required=False, type=bool)
@click.option('--extract', help='Test tile extraction.',
              required=False, type=bool)
@click.option('--reader', help='Test TFRecord readers.',
              required=False, type=bool)
@click.option('--train', help='Test training.',
              required=False, type=bool)
@click.option('--norm', 'normalizer', help='Test real-time normalization.',
              required=False, type=bool)
@click.option('--eval', 'evaluate', help='Test evaluation.',
              required=False, type=bool)
@click.option('--predict', help='Test prediction/inference.',
              required=False, type=bool)
@click.option('--heatmap', help='Test heatmaps.',
              required=False, type=bool)
@click.option('--act', 'activations', help='Test activations & mosaic maps.',
              required=False, type=bool)
@click.option('--wsi', 'predict_wsi', help='Test WSI prediction.',
              required=False, type=bool)
@click.option('--clam', help='Test CLAM.',
              required=False, type=bool)
def main(slides, out, all, **kwargs):
    '''Test script for running both unit tests and functional tests.

    Unit tests are included in `slideflow.test` and use the builtin `unittest`
    framework. These tests can be run by executing this script with no arguments:

        python3 test.py

    Most functions are difficult to test without sample slides. To this end, an
    additional set of functional tests are provided in `slideflow.test.TestSuite`,
    which require a set of sample slides. These tests can be enabled by providing
    a path to a directory with sample slides to the argument `--slides`:

        python3 test.py --slides=/path/to/slides

    To run all functional tests, set the `--all` flag to True:

        python3 test.py --slides=/path/to/slides --all=True

    To run only certain tests, set the individual flag to True:

        python3 test.py --slides=/path/to/slides --extract=True

    To run all tests while omitting some, set `--all` to True and other
    flags to False:

        python3 test.py --slides=/path/to/slides --all=True --clam=False
    '''
    if not out:
        out = 'slideflow_test'
    if 'SF_LOGGING_LEVEL' in os.environ:
        verbosity = logging.getLogger('slideflow').getEffectiveLevel()
    else:
        verbosity = logging.WARNING
    if all is not None:
        kwargs = {k: all if kwargs[k] is None else kwargs[k] for k in kwargs}

    # Show active backend
    if sf.backend() == 'tensorflow':
        print(col.bold("\nActive backend:"), col.yellow('tensorflow'))
    elif sf.backend() == 'torch':
        print(col.bold("\nActive backend:"), col.purple('torch'))
    else:
        print(col.bold("\nActive backend: <Unknown>"))

    # Show tests to run
    print(col.bold("\nTests to run:"))
    tests = kwargs.values()
    print(tabulate.tabulate({
        'Test': kwargs.keys(),
        'Run': [col.green('True') if v else col.red('False') for v in tests]
    }))
    TS = TestSuite(out, slides, verbosity=verbosity)
    TS.test(**kwargs)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()  # pylint: disable=no-value-for-parameter
