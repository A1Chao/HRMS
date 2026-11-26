"""独立建表脚本的回归测试；这里只使用模拟连接，不触碰实际业务库。

运行：python -m unittest app01.test_database_bootstrap -v
真实 MySQL 建库/迁移/重复执行另外在独立随机命名测试库验收。
"""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import init_database as bootstrap


CONFIG = {
    "ENGINE": "django.db.backends.mysql", "NAME": "test_hrms_bootstrap",
    "HOST": "127.0.0.1", "PORT": "3306", "USER": "tester",
    "PASSWORD": "only-a-test-placeholder",
}


def mock_server(rows, tables=()):
    driver = Mock()
    server = MagicMock()
    cursor = MagicMock()
    driver.connect.return_value = server
    server.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = rows
    cursor.fetchall.return_value = [(table,) for table in tables]
    return driver, server, cursor


class ConnectionOptionsTests(unittest.TestCase):
    def test_options_come_from_settings_and_do_not_require_an_existing_database(self):
        name, options = bootstrap.mysql_options(CONFIG)
        self.assertEqual(name, CONFIG["NAME"])
        self.assertNotIn("db", options)
        self.assertEqual(options["passwd"], CONFIG["PASSWORD"])
        self.assertEqual(options["charset"], "utf8mb4")
        self.assertEqual(options["connect_timeout"], 10)
        self.assertEqual(options["port"], 3306)

    def test_invalid_ports_are_rejected(self):
        for value in ("oops", "0", "65536", "-1"):
            with self.subTest(value=value), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.mysql_options({**CONFIG, "PORT": value})

    def test_non_mysql_database_is_rejected(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.mysql_options({**CONFIG, "ENGINE": "django.db.backends.sqlite3"})

    def test_invalid_names_are_rejected(self):
        for name in (None, "", "a" * 65, "bad\x00name"):
            with self.subTest(name=name), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.mysql_options({**CONFIG, "NAME": name})

    def test_socket_is_supported(self):
        _, options = bootstrap.mysql_options({**CONFIG, "HOST": "/tmp/mysql.sock"})
        self.assertEqual(options["unix_socket"], "/tmp/mysql.sock")
        self.assertNotIn("host", options)


class DatabaseSessionTests(unittest.TestCase):
    def test_new_database_is_utf8mb4_and_connection_is_closed(self):
        driver, server, cursor = mock_server([(1,), None, ("utf8mb4",)])
        with bootstrap.database_session(CONFIG, driver) as name:
            self.assertEqual(name, CONFIG["NAME"])
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE DATABASE IF NOT EXISTS" in item for item in sql))
        self.assertTrue(any("COLLATE utf8mb4_unicode_ci" in item for item in sql))
        self.assertTrue(any("RELEASE_LOCK" in item for item in sql))
        self.assertTrue(all(CONFIG["PASSWORD"] not in item for item in sql))
        self.assertFalse(any("DROP " in item or "ALTER " in item for item in sql))
        server.close.assert_called_once()

    def test_existing_migrated_database_does_not_need_create_statement(self):
        driver, server, cursor = mock_server([(1,), ("utf8mb4",)], ["django_migrations", "app01_user"])
        with bootstrap.database_session(CONFIG, driver):
            pass
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE DATABASE" in item for item in sql))
        server.close.assert_called_once()

    def test_database_without_migration_history_is_not_overwritten(self):
        driver, server, cursor = mock_server([(1,), ("utf8mb4",)], ["app01_user"])
        with self.assertRaisesRegex(bootstrap.BootstrapError, "没有 Django 迁移记录"):
            with bootstrap.database_session(CONFIG, driver):
                self.fail("Must not reach migrations")
        server.close.assert_called_once()

    def test_existing_non_utf8mb4_database_is_not_altered(self):
        driver, server, cursor = mock_server([(1,), ("latin1",)])
        with self.assertRaisesRegex(bootstrap.BootstrapError, "不是 utf8mb4"):
            with bootstrap.database_session(CONFIG, driver):
                self.fail("Must not reach migrations")
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER " in item for item in sql))
        server.close.assert_called_once()

    def test_busy_database_lock_stops_without_creating_tables(self):
        driver, server, cursor = mock_server([(0,)])
        with self.assertRaises(bootstrap.BootstrapError):
            with bootstrap.database_session(CONFIG, driver):
                self.fail("Must not reach migrations")
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(len(sql), 1)
        server.close.assert_called_once()

    def test_lock_is_released_when_migrations_fail(self):
        driver, server, cursor = mock_server([(1,), ("utf8mb4",)])
        with self.assertRaisesRegex(RuntimeError, "mock-failure"):
            with bootstrap.database_session(CONFIG, driver):
                raise RuntimeError("mock-failure")
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("RELEASE_LOCK" in item for item in sql))
        server.close.assert_called_once()

    def test_backticks_in_identifier_are_escaped(self):
        driver, server, cursor = mock_server([(1,), None, ("utf8mb4",)])
        with bootstrap.database_session({**CONFIG, "NAME": "a`b"}, driver):
            pass
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("`a``b`" in item for item in sql))


class MigrationTests(unittest.TestCase):
    @patch.object(bootstrap, "verify_schema")
    def test_default_runs_project_migrations_without_seed(self, verify):
        command, connection, apps = Mock(), Mock(), Mock()
        bootstrap.apply_migrations(connection, apps, command)
        self.assertEqual([call.args[0] for call in command.call_args_list],
                         ["check", "makemigrations", "migrate", "migrate"])
        self.assertEqual(command.call_args_list[1].kwargs,
                         {"check": True, "dry_run": True, "interactive": False})
        self.assertTrue(command.call_args_list[3].kwargs["check_unapplied"])
        verify.assert_called_once_with(connection, apps)

    @patch.object(bootstrap, "verify_schema")
    def test_demo_is_explicit_and_runs_after_migrations(self, verify):
        command = Mock()
        bootstrap.apply_migrations(Mock(), Mock(), command, with_demo=True)
        self.assertEqual(command.call_args_list[-1].args, ("init_data",))

    @patch.object(bootstrap, "verify_schema")
    def test_full_demo_is_explicit_and_completes_missing_salaries(self, verify):
        command = Mock()
        with patch.dict(bootstrap.os.environ, {}, clear=True):
            bootstrap.apply_migrations(Mock(), Mock(), command, with_demo=True, with_demo_managers=True)
            self.assertNotIn("HRMS_DEMO_MANAGER_INITIAL_PASSWORD", bootstrap.os.environ)
        self.assertEqual([call.args[0] for call in command.call_args_list][-3:],
                         ["init_data", "provision_department_managers", "fill_demo_salaries"])
        self.assertTrue(command.call_args_list[-1].kwargs["allow_demo"])

    @patch.object(bootstrap, "verify_schema")
    def test_existing_demo_password_environment_is_preserved(self, verify):
        command = Mock()
        with patch.dict(bootstrap.os.environ, {"HRMS_DEMO_MANAGER_INITIAL_PASSWORD": "test-placeholder"}, clear=True):
            bootstrap.apply_migrations(Mock(), Mock(), command, with_demo=True, with_demo_managers=True)
            self.assertEqual(bootstrap.os.environ["HRMS_DEMO_MANAGER_INITIAL_PASSWORD"], "test-placeholder")

    @patch.object(bootstrap, "verify_schema")
    def test_missing_migrations_stop_before_migrate(self, verify):
        command = Mock(side_effect=[None, SystemExit(1)])
        with self.assertRaises(SystemExit):
            bootstrap.apply_migrations(Mock(), Mock(), command, with_demo=True)
        self.assertEqual(command.call_count, 2)
        verify.assert_not_called()

    @patch.object(bootstrap, "verify_schema", side_effect=bootstrap.BootstrapError("broken table"))
    def test_schema_failure_does_not_seed(self, verify):
        command = Mock()
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.apply_migrations(Mock(), Mock(), command, with_demo=True)
        self.assertNotIn("init_data", [call.args[0] for call in command.call_args_list])


class SchemaTests(unittest.TestCase):
    def schema(self, tables, columns, managed=True):
        connection = MagicMock()
        connection.introspection.table_names.return_value = tables
        connection.introspection.get_table_description.return_value = [
            SimpleNamespace(name=name) for name in columns
        ]
        apps = Mock()
        apps.get_models.return_value = [SimpleNamespace(_meta=SimpleNamespace(
            managed=managed, proxy=False, swapped=False, db_table="app01_user",
            local_fields=[SimpleNamespace(column="id"), SimpleNamespace(column="password")],
        ))]
        return connection, apps

    def test_valid_schema_includes_many_to_many_models(self):
        connection, apps = self.schema(["app01_user"], ["id", "password"])
        bootstrap.verify_schema(connection, apps)
        apps.get_models.assert_called_once_with(include_auto_created=True)

    def test_missing_table_is_reported(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError, "缺少表 app01_user"):
            bootstrap.verify_schema(*self.schema([], []))

    def test_missing_column_is_reported(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError, "缺少列 app01_user.password"):
            bootstrap.verify_schema(*self.schema(["app01_user"], ["id"]))

    def test_unmanaged_tables_are_skipped(self):
        bootstrap.verify_schema(*self.schema([], [], managed=False))


if __name__ == "__main__":
    unittest.main()
