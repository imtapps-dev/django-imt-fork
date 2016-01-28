import os
import shutil
import sys
from django.db.backends.creation import BaseDatabaseCreation
from django.utils.six.moves import input

class DatabaseCreation(BaseDatabaseCreation):
    # SQLite doesn't actually support most of these types, but it "does the right
    # thing" given more verbose field definitions, so leave them as is so that
    # schema inspection is more useful.
    data_types = {
        'AutoField':                    'integer',
        'BooleanField':                 'bool',
        'CharField':                    'varchar(%(max_length)s)',
        'CommaSeparatedIntegerField':   'varchar(%(max_length)s)',
        'DateField':                    'date',
        'DateTimeField':                'datetime',
        'DecimalField':                 'decimal',
        'FileField':                    'varchar(%(max_length)s)',
        'FilePathField':                'varchar(%(max_length)s)',
        'FloatField':                   'real',
        'IntegerField':                 'integer',
        'BigIntegerField':              'bigint',
        'IPAddressField':               'char(15)',
        'GenericIPAddressField':        'char(39)',
        'NullBooleanField':             'bool',
        'OneToOneField':                'integer',
        'PositiveIntegerField':         'integer unsigned',
        'PositiveSmallIntegerField':    'smallint unsigned',
        'SlugField':                    'varchar(%(max_length)s)',
        'SmallIntegerField':            'smallint',
        'TextField':                    'text',
        'TimeField':                    'time',
    }

    def sql_for_pending_references(self, model, style, pending_references):
        "SQLite3 doesn't support constraints"
        return []

    def sql_remove_table_constraints(self, model, references_to_delete, style):
        "SQLite3 doesn't support constraints"
        return []

    def _get_test_db_name(self):
        test_database_name = self.connection.settings_dict['TEST_NAME']
        if test_database_name and test_database_name != ':memory:':
            return test_database_name
        return ':memory:'

    def _create_test_db(self, verbosity, autoclobber):
        test_database_name = self._get_test_db_name()
        if test_database_name != ':memory:':
            # Erase the old test database
            if verbosity >= 1:
                print("Destroying old test database '%s'..." % self.connection.alias)
            if os.access(test_database_name, os.F_OK):
                if not autoclobber:
                    confirm = input("Type 'yes' if you would like to try deleting the test database '%s', or 'no' to cancel: " % test_database_name)
                if autoclobber or confirm == 'yes':
                    try:
                        os.remove(test_database_name)
                    except Exception as e:
                        sys.stderr.write("Got an error deleting the old test database: %s\n" % e)
                        sys.exit(2)
                else:
                    print("Tests cancelled.")
                    sys.exit(1)
        return test_database_name

    def get_test_db_clone_settings(self, number):
        orig_settings_dict = self.connection.settings_dict
        source_database_name = orig_settings_dict['NAME']
        if self.connection.is_in_memory_db(source_database_name):
            return orig_settings_dict
        else:
            new_settings_dict = orig_settings_dict.copy()
            root, ext = os.path.splitext(orig_settings_dict['NAME'])
            new_settings_dict['NAME'] = '{}_{}.{}'.format(root, number, ext)
            return new_settings_dict

    def _clone_test_db(self, number, verbosity, keepdb=False):
        source_database_name = self.connection.settings_dict['NAME']
        target_database_name = self.get_test_db_clone_settings(number)['NAME']
        # Forking automatically makes a copy of an in-memory database.
        if not self.connection.is_in_memory_db(source_database_name):
            # Erase the old test database
            if os.access(target_database_name, os.F_OK):
                if keepdb:
                    return
                if verbosity >= 1:
                    print("Destroying old test database '%s'..." % target_database_name)
                try:
                    os.remove(target_database_name)
                except Exception as e:
                    sys.stderr.write("Got an error deleting the old test database: %s\n" % e)
                    sys.exit(2)
            try:
                shutil.copy(source_database_name, target_database_name)
            except Exception as e:
                sys.stderr.write("Got an error cloning the test database: %s\n" % e)
                sys.exit(2)

    def _destroy_test_db(self, test_database_name, verbosity):
        if test_database_name and test_database_name != ":memory:":
            # Remove the SQLite database file
            os.remove(test_database_name)

    def set_autocommit(self):
        self.connection.connection.isolation_level = None

    def test_db_signature(self):
        """
        Returns a tuple that uniquely identifies a test database.

        This takes into account the special cases of ":memory:" and "" for
        SQLite since the databases will be distinct despite having the same
        TEST_NAME. See http://www.sqlite.org/inmemorydb.html
        """
        settings_dict = self.connection.settings_dict
        test_dbname = self._get_test_db_name()
        sig = [self.connection.settings_dict['NAME']]
        if test_dbname == ':memory:':
            sig.append(self.connection.alias)
        return tuple(sig)
