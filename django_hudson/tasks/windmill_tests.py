# -*- coding: utf-8 -*-
import socket, threading, unittest
from windmill.bin import admin_lib
from windmill.authoring import WindmillTestClient
from django.db.models import get_app, get_apps
from django.conf import settings
from django.core import urlresolvers
from django.core.handlers.wsgi import WSGIHandler
from django.core.servers import basehttp
from django.test.simple import reorder_suite
from django.test import TestCase, TransactionTestCase
from django_hudson.tasks import BaseTask

WM_TEST_MODULE = 'wmtests'
TEST_SERVER_HOST = '127.0.0.2'
TEST_SERVER_PORT = 0

def get_tests(app_module):
    try:
        app_path = app_module.__name__.split('.')[:-1]
        test_module = __import__('.'.join(app_path + [WM_TEST_MODULE]), {}, {}, WM_TEST_MODULE)
    except ImportError:
        # Couldn't import tests.py. Was it due to a missing file, or
        # due to an import error in a tests.py that actually exists?
        import os.path
        from imp import find_module
        try:
            mod = find_module(WM_TEST_MODULE, [os.path.dirname(app_module.__file__)])
        except ImportError:
            # 'tests' module doesn't exist. Move on.
            test_module = None
        else:
            # The module exists, so there must be an import error in the
            # test module itself. We don't need the module; so if the
            # module was a single file module (i.e., tests.py), close the file
            # handle returned by find_module. Otherwise, the test module
            # is a directory, and there is nothing to close.
            if mod[0]:
                mod[0].close()
            raise
    return test_module


def build_suite(app_module):
    """
    Create a complete Django test suite for the provided application module
    """
    suite = unittest.TestSuite()

    # Load unit and doctests in the models.py module. If module has
    # a suite() method, use it. Otherwise build the test suite ourselves.
    if hasattr(app_module, 'suite'):
        suite.addTest(app_module.suite())
    else:
        suite.addTest(unittest.defaultTestLoader.loadTestsFromModule(app_module))

    # Check to see if a separate 'tests' module exists parallel to the
    # models module
    test_module = get_tests(app_module)
    if test_module:
        # Load unit and doctests in the tests.py module. If module has
        # a suite() method, use it. Otherwise build the test suite ourselves.
        if hasattr(test_module, 'suite'):
            suite.addTest(test_module.suite())
        else:
            suite.addTest(unittest.defaultTestLoader.loadTestsFromModule(test_module))

    return suite


def build_test(label):
    """
    Construct a test case with the specified label. Label should be of the
    form model.TestClass or model.TestClass.test_method. Returns an
    instantiated test or test suite corresponding to the label provided.

    """
    parts = label.split('.')
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError("Test label '%s' should be of the form app.TestCase or app.TestCase.test_method" % label)

    #
    # First, look for TestCase instances with a name that matches
    #
    app_module = get_app(parts[0])
    test_module = get_tests(app_module)
    TestClass = getattr(app_module, parts[1], None)

    # Couldn't find the test class in models.py; look in tests.py
    if TestClass is None:
        if test_module:
            TestClass = getattr(test_module, parts[1], None)

    try:
        if issubclass(TestClass, unittest.TestCase):
            if len(parts) == 2: # label is app.TestClass
                try:
                    return unittest.TestLoader().loadTestsFromTestCase(TestClass)
                except TypeError:
                    raise ValueError("Test label '%s' does not refer to a test class" % label)
            else: # label is app.TestClass.test_method
                return TestClass(parts[2])
    except TypeError:
        # TestClass isn't a TestClass - it must be a method or normal class
        pass


class StoppableWSGIServer(basehttp.WSGIServer):
    """WSGIServer with short timeout, so that server thread can stop this server."""

    def server_bind(self):
        """Sets timeout to 1 second."""
        basehttp.WSGIServer.server_bind(self)
        self.socket.settimeout(1)

        global TEST_SERVER_HOST, TEST_SERVER_PORT
        TEST_SERVER_HOST, TEST_SERVER_PORT = self.socket.getsockname()
        if TEST_SERVER_PORT == '0.0.0.0':
            TEST_SERVER_HOST = '127.0.0.2'


    def get_request(self):
        """Checks for timeout when getting request."""
        try:
            sock, address = self.socket.accept()
            sock.settimeout(None)
            return (sock, address)
        except socket.timeout:
            raise


class TestServerThread(threading.Thread):
    """Thread for running a http server while tests are running."""

    def __init__(self, server_addr):
        self.server_addr = server_addr
        self._stopevent = threading.Event()
        self.started = threading.Event()
        self.error = None
        super(TestServerThread, self).__init__()

    def run(self):
        """Sets up test server and loops over handling http requests."""
        try:
            handler = basehttp.AdminMediaHandler(WSGIHandler())
            httpd = StoppableWSGIServer(self.server_addr, basehttp.WSGIRequestHandler)
            httpd.application = handler
            self.started.set()
        except basehttp.WSGIServerException as err:
            self.error = err
            self.started.set()
            return

        # Loop until we get a stop event.
        while not self._stopevent.isSet():
            httpd.handle_request()

    def join(self, timeout=None):
        """Stop the thread and wait for it to finish."""
        self._stopevent.set()
        threading.Thread.join(self, timeout)


class Task(BaseTask):
    def __init__(self, test_labels, options):
        super(Task, self).__init__(test_labels, options)
        self.output_dir = options.get('output_dir', 'reports')
        self.verbosity = int(options.get('verbosity', 1))
        self.test_server_host = getattr(settings, 'WINDMILL_HOST', '127.0.0.2') # for 127.0.0.1 FF always ignore proxy for me
        self.test_server_port = getattr(settings, 'WINDMILL_PORT', 0) # select random available port

    def setup_test_environment(self, **kwargs):
                # configure windmill
        admin_lib.configure_global_settings(logging_on=False)
        self.windmill_dict = admin_lib.setup()
        self.windmill_dict['start_firefox']()

        #run wsgi server
        self.server_thread = TestServerThread((self.test_server_host, self.test_server_port))
        self.server_thread.start()
        self.server_thread.started.wait()
        if self.server_thread.error:
            raise self.server_thread.error

    def teardown_test_environment(self, **kwargs):
        # stop windmill browser
        admin_lib.teardown(self.windmill_dict)
        #self.windmill_dict['xmlrpc_client'].stop_runserver()

        # stop wsgi server
        if self.server_thread:
            self.server_thread.join()

    def build_suite(self, suite, **kwargs):
        if self.test_labels:
            for label in self.test_labels:
                if '.' in label:
                    suite.addTest(build_test(label))
                else:
                    app = get_app(label)
                    suite.addTest(build_suite(app))
        else:
            for app in get_apps():
                suite.addTest(build_suite(app))

        return reorder_suite(suite, (TestCase,))


class WindmillMixin(object):
    @property
    def base_url(self):
        return "http://%s:%s" % (TEST_SERVER_HOST, TEST_SERVER_PORT)

    def __call__(self, result=None):
        
        self.windmill = WindmillTestClient(__name__)
        old_opener = self.windmill.open
        def opener(url, *args, **kwargs):
            if url.startswith('http'):
                return old_opener(url=url)

            if not url.startswith('/'):
                path = urlresolvers.reverse(url, args=args, kwargs=kwargs)
            else:
                path = url

            return old_opener(url=self.base_url + path)
        self.windmill.open = opener

        return super(TestCase, self).__call__(result=result)


class WindmillTransactionalTestCase(WindmillMixin, TransactionTestCase):
    pass


class WindmillTestCase(WindmillMixin, TestCase):
    pass

