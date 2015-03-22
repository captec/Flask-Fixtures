"""
    flask.ext.fixtures
    ~~~~~~~~~~~~~~~~~~

    Flask-Fixtures is a `Flask <http://flask.pocoo.org>`_ extension that aids
    in the creation of fixtures data from serialized data files. It is
    compatible with the `SQLAlchemy <http://sqlalchemy.org>`_ library.

    :copyright: (c) 2015 Christopher Roach <ask.croach@gmail.com>.
    :license: MIT, see LICENSE for more details.
"""

from __future__ import print_function

import importlib
import inspect
import os
import sys

#from flask import current_app
from sqlalchemy import Table

try:
    import simplejson as json
except ImportError:
    import json

try:
    import yaml
    YAML_INSTALLED = True
except ImportError:
    YAML_INSTALLED = False


def setup(obj):
    # Setup the database
    obj.db.create_all()
    # TODO why do we call this?
    obj.db.session.rollback()

    # Load all of the fixtures
    fixtures_dirs = obj.app.config['FIXTURES_DIRS']
    for filename in obj._fixtures:
        for directory in fixtures_dirs:
            filepath = os.path.join(directory, filename)
            if os.path.exists(filepath):
                # TODO load the data into the database
                load_fixtures(obj.db, load_file(filepath))
                break
        else:
            # TODO should we raise an error here instead?
            print("Error loading '%s'. File could not be found." % filename, file=sys.stderr)


def teardown(obj):
    obj.db.session.expunge_all()
    obj.db.drop_all()


def load_file(filename):
    """Returns list of fixtures from the given file.
    """
    name, extension = os.path.splitext(filename)
    if extension.lower() in ('.yaml', '.yml'):
        if not YAML_INSTALLED:
            raise Exception("Could not load fixture '%s'; PyYAML must first be installed")
        loader = yaml.load
    elif extension.lower() in ('.json', '.js'):
        loader = json.load
    else:
        # Try both supported formats
        def loader(f):
            try:
                return yaml.load(f)
            except Exception:
                pass
            try:
                return json.load(f)
            except Exception:
                pass
            raise Exception("Could not load fixture '%s'; unsupported format")
    with open(filename, 'r') as fin:
        return loader(fin)


def load_fixtures(db, fixtures):
    """Loads the given fixtures into the database.
    """
    conn = db.engine.connect()
    metadata = db.metadata

    for fixture in fixtures:
        if 'model' in fixture:
            module_name, class_name = fixture['model'].rsplit('.', 1)
            module = importlib.import_module(module_name)
            model = getattr(module, class_name)
            for fields in fixture['records']:
                obj = model(**fields)
                db.session.add(obj)
            db.session.commit()
        elif 'table' in fixture:
            table = Table(fixture['table'], metadata)
            conn.execute(table.insert(), fixture['records'])
        else:
            raise ValueError("Fixture missing a 'model' or 'table' field: %s" % json.dumps(fixture))

CLASS_SETUP_NAMES = ('setUpClass', 'setup_class', 'setup_all', 'setupClass', 'setupAll', 'setUpAll')
CLASS_TEARDOWN_NAMES = ('tearDownClass', 'teardown_class', 'teardown_all', 'teardownClass', 'teardownAll', 'tearDownAll')
TEST_SETUP_NAMES = ('setUp',)
TEST_TEARDOWN_NAMES = ('tearDown',)


class MetaFixturesMixin(type):
    def __new__(meta, name, bases, attrs):

        fixtures = attrs.pop('fixtures', None)
        class_fixtures = attrs.pop('class_fixtures', None)

        # TODO: In the future we may want to support class and test fixtures simultaneously.
        # This is tough to do since the test fixtures need to be wiped out of the database
        # after each test and re-inserted back in before each test without affecting the
        # the class fixtures or any changes to them that the tests have made.
        if fixtures is not None and class_fixtures is not None:
            raise RuntimeError("Flask-Fixtures does not currently support the use of both class and test fixtures.")

        if fixtures is not None:
            child_setup_fn = meta.get_child_fn(attrs, TEST_SETUP_NAMES, bases)
            child_teardown_fn = meta.get_child_fn(attrs, TEST_TEARDOWN_NAMES, bases)
            attrs[child_setup_fn.__name__] = meta.setup_handler(setup, child_setup_fn)
            attrs[child_teardown_fn.__name__] = meta.teardown_handler(teardown, child_teardown_fn)
            attrs['_fixtures'] = fixtures
        elif class_fixtures is not None:
            child_setup_fn = meta.get_child_fn(attrs, CLASS_SETUP_NAMES, bases)
            child_teardown_fn = meta.get_child_fn(attrs, CLASS_TEARDOWN_NAMES, bases)
            attrs[child_setup_fn.__name__] = classmethod(meta.setup_handler(setup, child_setup_fn))
            attrs[child_teardown_fn.__name__] = classmethod(meta.teardown_handler(teardown, child_teardown_fn))
            attrs['_fixtures'] = class_fixtures

        return super(MetaFixturesMixin, meta).__new__(meta, name, bases, attrs)

    @staticmethod
    def setup_handler(setup_fixtures_fn, setup_fn):
        """Returns a function that adds fixtures handling to the setup method.

        Makes sure that fixtures are setup before calling the given setup method.
        """
        def handler(obj):
            setup_fixtures_fn(obj)
            setup_fn(obj)
        return handler

    @staticmethod
    def teardown_handler(teardown_fixtures_fn, teardown_fn):
        """Returns a function that adds fixtures handling to the teardown method.

        Calls the given teardown method first before calling the fixtures teardown.
        """
        def handler(obj):
            teardown_fn(obj)
            teardown_fixtures_fn(obj)
        return handler

    @staticmethod
    def get_child_fn(attrs, names, bases):
        """Returns a function from the child class that matches one of the names.

        Searches the child class's set of methods (i.e., the attrs dict) for all
        the functions matching the given list of names. If more than one is found,
        an exception is raised, if one is found, it is returned, and if none are
        found, a function that calls the default method on each parent class is
        returned.

        """
        def call_method(obj, method):
            """Calls a method as either a class method or an instance method.
            """
            # The __get__ method takes an instance and an owner which changes
            # depending on the calling object. If the calling object is a class,
            # the instance is None and the owner will be the object itself. If the
            # calling object is an instance, the instance will be the calling object
            # and the owner will be its class. For more info on the __get__ method,
            # see http://docs.python.org/2/reference/datamodel.html#object.__get__.
            if isinstance(obj, type):
                instance = None
                owner = obj
            else:
                instance = obj
                owner = obj.__class__
            method.__get__(instance, owner)()

        # Create a default function that calls the default method on each parent
        default_name = names[0]
        def default_fn(obj):
            for cls in bases:
                if hasattr(cls, default_name):
                    call_method(obj, getattr(cls, default_name))
        default_fn.__name__ = default_name

        # Get all of the functions in the child class that match the list of names
        fns = [(name, attrs[name]) for name in names if name in attrs]

        # Raise an error if more than one setup/teardown method is found
        if len(fns) > 1:
            raise RuntimeError("Cannot have more than one setup or teardown method per context (class or test).")
        # If one setup/teardown function was found, return it
        elif len(fns) == 1:
            name, fn = fns[0]
            def child_fn(obj):
                call_method(obj, fn)
            child_fn.__name__ = name
            return child_fn
        # Otherwise, return the default function
        else:
            return default_fn


class FixturesMixin(object):

    __metaclass__ = MetaFixturesMixin

    fixtures = None

    @classmethod
    def init_app(cls, app, db=None):
        default_fixtures_dir = os.path.join(app.root_path, 'fixtures')

        # All relative paths should be relative to the app's root directory.
        fixtures_dirs = [default_fixtures_dir]
        for directory in app.config.get('FIXTURES_DIRS', []):
            if not os.path.isabs(directory):
                directory = os.path.abspath(os.path.join(app.root_path, directory))
            fixtures_dirs.append(directory)
        app.config['FIXTURES_DIRS'] = fixtures_dirs
        cls.app = app
        cls.db = db

