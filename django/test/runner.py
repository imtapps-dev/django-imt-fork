import collections
import ctypes
import itertools
import logging
import multiprocessing
import os
import pickle
import textwrap

from django.db import DEFAULT_DB_ALIAS, connections
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.test.utils import setup_test_environment, teardown_test_environment
from django.utils import unittest
from django.utils.unittest import TestSuite, defaultTestLoader
from django.utils.datastructures import OrderedSet

try:
        import tblib.pickling_support
except ImportError:
        tblib = None


class RemoteTestResult(object):
    """
    Record information about which tests have succeeded and which have failed.
    The sole purpose of this class is to record events in the child processes
    so they can be replayed in the master process. As a consequence it doesn't
    inherit unittest.TestResult and doesn't attempt to implement all its API.
    The implementation matches the unpythonic coding style of unittest2.
    """

    def __init__(self):
        self.events = []
        self.failfast = False
        self.shouldStop = False
        self.testsRun = 0

    @property
    def test_index(self):
        return self.testsRun - 1

    def check_pickleable(self, test, err):
        # Ensure that sys.exc_info() tuples are picklable. This displays a
        # clear multiprocessing.pool.RemoteTraceback generated in the child
        # process instead of a multiprocessing.pool.MaybeEncodingError, making
        # the root cause easier to figure out for users who aren't familiar
        # with the multiprocessing module. Since we're in a forked process,
        # our best chance to communicate with them is to print to stdout.
        try:
            pickle.dumps(err)
        except Exception as exc:
            original_exc_txt = repr(err[1])
            original_exc_txt = textwrap.fill(original_exc_txt, 75)
            original_exc_txt = textwrap.indent(original_exc_txt, '    ')
            pickle_exc_txt = repr(exc)
            pickle_exc_txt = textwrap.fill(pickle_exc_txt, 75)
            pickle_exc_txt = textwrap.indent(pickle_exc_txt, '    ')
            if tblib is None:
                print("""
{} failed:
{}
Unfortunately, tracebacks cannot be pickled, making it impossible for the
parallel test runner to handle this exception cleanly.
In order to see the traceback, you should install tblib:
    pip install tblib
""".format(test, original_exc_txt))
            else:
                print("""
{} failed:
{}
Unfortunately, the exception it raised cannot be pickled, making it impossible
for the parallel test runner to handle it cleanly.
Here's the error encountered while trying to pickle the exception:
{}
You should re-run this test without the --parallel option to reproduce the
failure and get a correct traceback.
""".format(test, original_exc_txt, pickle_exc_txt))
            raise

    def stop_if_failfast(self):
        if self.failfast:
            self.stop()

    def stop(self):
        self.shouldStop = True

    def startTestRun(self):
        self.events.append(('startTestRun',))

    def stopTestRun(self):
        self.events.append(('stopTestRun',))

    def startTest(self, test):
        self.testsRun += 1
        self.events.append(('startTest', self.test_index))

    def stopTest(self, test):
        self.events.append(('stopTest', self.test_index))

    def addError(self, test, err):
        self.check_pickleable(test, err)
        self.events.append(('addError', self.test_index, err))
        self.stop_if_failfast()

    def addFailure(self, test, err):
        self.check_pickleable(test, err)
        self.events.append(('addFailure', self.test_index, err))
        self.stop_if_failfast()

    def addSubTest(self, test, subtest, err):
        raise NotImplementedError("subtests aren't supported at this time")

    def addSuccess(self, test):
        self.events.append(('addSuccess', self.test_index))

    def addSkip(self, test, reason):
        self.events.append(('addSkip', self.test_index, reason))

    def addExpectedFailure(self, test, err):
        self.check_pickleable(test, err)
        self.events.append(('addExpectedFailure', self.test_index, err))

    def addUnexpectedSuccess(self, test):
        self.events.append(('addUnexpectedSuccess', self.test_index))
        self.stop_if_failfast()


class RemoteTestRunner(object):
    """
    Run tests and record everything but don't display anything.
    The implementation matches the unpythonic coding style of unittest2.
    """

    resultclass = RemoteTestResult

    def __init__(self, failfast=False, resultclass=None):
        self.failfast = failfast
        if resultclass is not None:
            self.resultclass = resultclass

    def run(self, test):
        result = self.resultclass()
        unittest.registerResult(result)
        result.failfast = self.failfast
        test(result)
        return result


def default_test_processes():
    """
    Default number of test processes when using the --parallel option.
    """
    try:
        return int(os.environ['DJANGO_TEST_PROCESSES'])
    except KeyError:
        return multiprocessing.cpu_count()


_worker_id = 0


def _init_worker(counter):
    """
    Switch to databases dedicated to this worker.
    This helper lives at module-level because of the multiprocessing module's
    requirements.
    """

    global _worker_id

    with counter.get_lock():
        counter.value += 1
        _worker_id = counter.value

    for alias in connections:
        connection = connections[alias]
        settings_dict = connection.creation.get_test_db_clone_settings(_worker_id)
        # connection.settings_dict must be updated in place for changes to be
        # reflected in django.db.connections. If the following line assigned
        # connection.settings_dict = settings_dict, new threads would connect
        # to the default database instead of the appropriate clone.
        connection.settings_dict.update(settings_dict)
        connection.close()


def _run_subsuite(args):
    """
    Run a suite of tests with a RemoteTestRunner and return a RemoteTestResult.
    This helper lives at module-level and its arguments are wrapped in a tuple
    because of the multiprocessing module's requirements.
    """
    subsuite_index, subsuite, failfast = args
    runner = RemoteTestRunner(failfast=failfast)
    result = runner.run(subsuite)
    return subsuite_index, result.events


class ParallelTestSuite(unittest.TestSuite):
    """
    Run a series of tests in parallel in several processes.
    While the unittest module's documentation implies that orchestrating the
    execution of tests is the responsibility of the test runner, in practice,
    it appears that TestRunner classes are more concerned with formatting and
    displaying test results.
    Since there are fewer use cases for customizing TestSuite than TestRunner,
    implementing parallelization at the level of the TestSuite improves
    interoperability with existing custom test runners. A single instance of a
    test runner can still collect results from all tests without being aware
    that they have been run in parallel.
    """

    # In case someone wants to modify these in a subclass.
    init_worker = _init_worker
    run_subsuite = _run_subsuite

    def __init__(self, suite, processes, failfast=False):
        self.subsuites = partition_suite_by_case(suite)
        self.processes = processes
        self.failfast = failfast
        super(ParallelTestSuite, self).__init__()

    def run(self, result):
        """
        Distribute test cases across workers.
        Return an identifier of each test case with its result in order to use
        imap_unordered to show results as soon as they're available.
        To minimize pickling errors when getting results from workers:
        - pass back numeric indexes in self.subsuites instead of tests
        - make tracebacks pickleable with tblib, if available
        Even with tblib, errors may still occur for dynamically created
        exception classes such Model.DoesNotExist which cannot be unpickled.
        """
        if tblib is not None:
            tblib.pickling_support.install()

        counter = multiprocessing.Value(ctypes.c_int, 0)
        pool = multiprocessing.Pool(
            processes=self.processes,
            initializer=self.init_worker.__func__,
            initargs=[counter])
        args = [
            (index, subsuite, self.failfast)
            for index, subsuite in enumerate(self.subsuites)
        ]
        test_results = pool.imap_unordered(self.run_subsuite.__func__, args)

        while True:
            if result.shouldStop:
                pool.terminate()
                break

            try:
                subsuite_index, events = test_results.next(timeout=0.1)
            except multiprocessing.TimeoutError:
                continue
            except StopIteration:
                pool.close()
                break

            tests = list(self.subsuites[subsuite_index])
            for event in events:
                event_name = event[0]
                handler = getattr(result, event_name, None)
                if handler is None:
                    continue
                test = tests[event[1]]
                args = event[2:]
                handler(test, *args)

        pool.join()

        return result

class DiscoverRunner(object):
    """
    A Django test runner that uses unittest2 test discovery.
    """

    test_suite = unittest.TestSuite
    parallel_test_suite = ParallelTestSuite
    test_runner = unittest.TextTestRunner
    test_loader = unittest.defaultTestLoader
    reorder_by = (TestCase, )

    def __init__(self, pattern=None, top_level=None, verbosity=1,
                 interactive=True, failfast=False, keepdb=False,
                 reverse=False, debug_sql=False, parallel=0,
                 **kwargs):

        self.pattern = pattern
        self.top_level = top_level

        self.verbosity = verbosity
        self.interactive = interactive
        self.failfast = failfast
        self.keepdb = keepdb
        self.reverse = reverse
        self.debug_sql = debug_sql
        self.parallel = parallel

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument('-t', '--top-level-directory',
            action='store', dest='top_level', default=None,
            help='Top level of project for unittest discovery.')
        parser.add_argument('-p', '--pattern', action='store', dest='pattern',
            default="test*.py",
            help='The test matching pattern. Defaults to test*.py.')
        parser.add_argument('-k', '--keepdb', action='store_true', dest='keepdb',
            default=False,
            help='Preserves the test DB between runs.')
        parser.add_argument('-r', '--reverse', action='store_true', dest='reverse',
            default=False,
            help='Reverses test cases order.')
        parser.add_argument('-d', '--debug-sql', action='store_true', dest='debug_sql',
            default=False,
            help='Prints logged SQL queries on failure.')
        parser.add_argument(
            '--parallel', dest='parallel', nargs='?', default=1, type=int,
            const=default_test_processes(),
            help='Run tests in parallel processes.')

    def setup_test_environment(self, **kwargs):
        setup_test_environment()
        settings.DEBUG = False
        unittest.installHandler()

    def build_suite(self, test_labels=None, extra_tests=None, **kwargs):
        suite = TestSuite()
        test_labels = test_labels or ['.']
        extra_tests = extra_tests or []

        discover_kwargs = {}
        if self.pattern is not None:
            discover_kwargs['pattern'] = self.pattern
        if self.top_level is not None:
            discover_kwargs['top_level_dir'] = self.top_level

        for label in test_labels:
            kwargs = discover_kwargs.copy()
            tests = None

            label_as_path = os.path.abspath(label)

            # if a module, or "module.ClassName[.method_name]", just run those
            if not os.path.exists(label_as_path):
                tests = self.test_loader.loadTestsFromName(label)
            elif os.path.isdir(label_as_path) and not self.top_level:
                # Try to be a bit smarter than unittest about finding the
                # default top-level for a given directory path, to avoid
                # breaking relative imports. (Unittest's default is to set
                # top-level equal to the path, which means relative imports
                # will result in "Attempted relative import in non-package.").

                # We'd be happy to skip this and require dotted module paths
                # (which don't cause this problem) instead of file paths (which
                # do), but in the case of a directory in the cwd, which would
                # be equally valid if considered as a top-level module or as a
                # directory path, unittest unfortunately prefers the latter.

                top_level = label_as_path
                while True:
                    init_py = os.path.join(top_level, '__init__.py')
                    if os.path.exists(init_py):
                        try_next = os.path.dirname(top_level)
                        if try_next == top_level:
                            # __init__.py all the way down? give up.
                            break
                        top_level = try_next
                        continue
                    break
                kwargs['top_level_dir'] = top_level

            if not (tests and tests.countTestCases()):
                # if no tests found, it's probably a package; try discovery
                tests = self.test_loader.discover(start_dir=label, **kwargs)

                # make unittest forget the top-level dir it calculated from this
                # run, to support running tests from two different top-levels.
                self.test_loader._top_level_dir = None

            suite.addTests(tests)

        for test in extra_tests:
            suite.addTest(test)

        suite = reorder_suite(suite, self.reorder_by, self.reverse)

        if self.parallel > 1:
            parallel_suite = self.parallel_test_suite(suite, self.parallel, self.failfast)

            # Since tests are distributed across processes on a per-TestCase
            # basis, there's no need for more processes than TestCases.
            parallel_units = len(parallel_suite.subsuites)
            if self.parallel > parallel_units:
                self.parallel = parallel_units

            # If there's only one TestCase, parallelization isn't needed.
            if self.parallel > 1:
                suite = parallel_suite

        return suite

    def setup_databases(self, **kwargs):
        return setup_databases(self.verbosity, self.interactive, self.parallel, **kwargs)

    def run_suite(self, suite, **kwargs):
        return unittest.TextTestRunner(
            verbosity=self.verbosity,
            failfast=self.failfast,
        ).run(suite)

    def teardown_databases(self, old_config, **kwargs):
        """
        Destroys all the non-mirror databases.
        """
        old_names, mirrors = old_config
        for connection, old_name, destroy in old_names:
            if destroy:
                if self.parallel > 1:
                    for index in range(self.parallel):
                        connection.creation.destroy_test_db(
                            number=index + 1,
                            verbosity=self.verbosity,
                        )
                connection.creation.destroy_test_db(old_name, self.verbosity)

    def teardown_test_environment(self, **kwargs):
        unittest.removeHandler()
        teardown_test_environment()

    def suite_result(self, suite, result, **kwargs):
        return len(result.failures) + len(result.errors)

    def run_tests(self, test_labels, extra_tests=None, **kwargs):
        """
        Run the unit tests for all the test labels in the provided list.

        Test labels should be dotted Python paths to test modules, test
        classes, or test methods.

        A list of 'extra' tests may also be provided; these tests
        will be added to the test suite.

        Returns the number of tests that failed.
        """
        self.setup_test_environment()
        suite = self.build_suite(test_labels, extra_tests)
        old_config = self.setup_databases()
        result = self.run_suite(suite)
        self.teardown_databases(old_config)
        self.teardown_test_environment()
        return self.suite_result(suite, result)


def dependency_ordered(test_databases, dependencies):
    """
    Reorder test_databases into an order that honors the dependencies
    described in TEST_DEPENDENCIES.
    """
    ordered_test_databases = []
    resolved_databases = set()

    # Maps db signature to dependencies of all it's aliases
    dependencies_map = {}

    # sanity check - no DB can depend on it's own alias
    for sig, (_, aliases) in test_databases:
        all_deps = set()
        for alias in aliases:
            all_deps.update(dependencies.get(alias, []))
        if not all_deps.isdisjoint(aliases):
            raise ImproperlyConfigured(
                "Circular dependency: databases %r depend on each other, "
                "but are aliases." % aliases)
        dependencies_map[sig] = all_deps

    while test_databases:
        changed = False
        deferred = []

        # Try to find a DB that has all it's dependencies met
        for signature, (db_name, aliases) in test_databases:
            if dependencies_map[signature].issubset(resolved_databases):
                resolved_databases.update(aliases)
                ordered_test_databases.append((signature, (db_name, aliases)))
                changed = True
            else:
                deferred.append((signature, (db_name, aliases)))

        if not changed:
            raise ImproperlyConfigured(
                "Circular dependency in TEST_DEPENDENCIES")
        test_databases = deferred
    return ordered_test_databases


def reorder_suite(suite, classes, reverse=False):
    """
    Reorders a test suite by test type.

    `classes` is a sequence of types

    All tests of type classes[0] are placed first, then tests of type
    classes[1], etc. Tests with no match in classes are placed last.
    """
    class_count = len(classes)
    suite_class = type(suite)
    bins = [OrderedSet() for i in range(class_count+1)]
    partition_suite_by_type(suite, classes, bins, reverse=reverse)
    reordered_suite = suite_class()
    for i in range(class_count + 1):
        reordered_suite.addTests(bins[i])
    return reordered_suite
    # for i in range(class_count):
    #     bins[0].addTests(bins[i+1])
    # return bins[0]


def partition_suite_by_type(suite, classes, bins, reverse=False):
    """
    Partitions a test suite by test type. Also prevents duplicated tests.
    classes is a sequence of types
    bins is a sequence of TestSuites, one more than classes
    reverse changes the ordering of tests within bins
    Tests of type classes[i] are added to bins[i],
    tests with no match found in classes are place in bins[-1]
    """
    suite_class = type(suite)
    if reverse:
        suite = reversed(tuple(suite))
    for test in suite:
        if isinstance(test, suite_class):
            partition_suite_by_type(test, classes, bins, reverse=reverse)
        else:
            for i in range(len(classes)):
                if isinstance(test, classes[i]):
                    bins[i].add(test)
                    break
            else:
                bins[-1].add(test)


def partition_suite_by_case(suite):
    """
    Partitions a test suite by test case, preserving the order of tests.
    """
    groups = []
    suite_class = type(suite)
    for test_type, test_group in itertools.groupby(suite, type):
        if issubclass(test_type, unittest.TestCase):
            groups.append(suite_class(test_group))
        else:
            for item in test_group:
                groups.extend(partition_suite_by_case(item))
    return groups


def setup_databases(verbosity, interactive, parallel=0, **kwargs):
    from django.db import connections, DEFAULT_DB_ALIAS

    # First pass -- work out which databases actually need to be created,
    # and which ones are test mirrors or duplicate entries in DATABASES
    mirrored_aliases = {}
    test_databases = {}
    dependencies = {}
    default_sig = connections[DEFAULT_DB_ALIAS].creation.test_db_signature()
    for alias in connections:
        connection = connections[alias]
        if connection.settings_dict['TEST_MIRROR']:
            # If the database is marked as a test mirror, save
            # the alias.
            mirrored_aliases[alias] = (
                connection.settings_dict['TEST_MIRROR'])
        else:
            # Store a tuple with DB parameters that uniquely identify it.
            # If we have two aliases with the same values for that tuple,
            # we only need to create the test database once.
            item = test_databases.setdefault(
                connection.creation.test_db_signature(),
                (connection.settings_dict['NAME'], set())
            )
            item[1].add(alias)

            if 'TEST_DEPENDENCIES' in connection.settings_dict:
                dependencies[alias] = (
                    connection.settings_dict['TEST_DEPENDENCIES'])
            else:
                if alias != DEFAULT_DB_ALIAS and connection.creation.test_db_signature() != default_sig:
                    dependencies[alias] = connection.settings_dict.get(
                        'TEST_DEPENDENCIES', [DEFAULT_DB_ALIAS])

    # Second pass -- actually create the databases.
    old_names = []
    mirrors = []

    for signature, (db_name, aliases) in dependency_ordered(
        test_databases.items(), dependencies):
        test_db_name = None
        # Actually create the database for the first connection
        for alias in aliases:
            connection = connections[alias]
            if test_db_name is None:
                test_db_name = connection.creation.create_test_db(
                        verbosity=verbosity, autoclobber=not interactive)
                if parallel > 1:
                    for index in range(parallel):
                        connection.creation.clone_test_db(
                            number=index + 1,
                            verbosity=verbosity,
                        )
                destroy = True
            else:
                connection.settings_dict['NAME'] = test_db_name
                destroy = False
            old_names.append((connection, db_name, destroy))

    for alias, mirror_alias in mirrored_aliases.items():
        mirrors.append((alias, connections[alias].settings_dict['NAME']))
        connections[alias].settings_dict['NAME'] = (
            connections[mirror_alias].settings_dict['NAME'])

    return old_names, mirrors
