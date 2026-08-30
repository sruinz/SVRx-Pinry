from contextlib import contextmanager
import re

from django.db import OperationalError, connections, transaction


_SQLITE_BUSY_WORD = re.compile(r"\b(?:busy|locked)\b")
_POSTGRESQL_BUSY_SQLSTATES = frozenset(("40001", "40P01", "55P03"))


class DatabaseFenceError(Exception):
    def __init__(self, code="database_fence_failed", retryable=False):
        self.code = code
        self.retryable = retryable
        super(DatabaseFenceError, self).__init__(code)
        self.__suppress_context__ = True


class DatabaseFenceBusy(DatabaseFenceError):
    def __init__(self):
        super(DatabaseFenceBusy, self).__init__(
            "database_busy",
            retryable=True,
        )


class DatabaseFenceDeadline(DatabaseFenceBusy):
    def __init__(self, monotonic, budget_seconds=5.0):
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if (
            not isinstance(budget_seconds, (int, float))
            or isinstance(budget_seconds, bool)
            or budget_seconds <= 0
        ):
            raise ValueError("budget_seconds must be positive")
        self.monotonic = monotonic
        self.deadline = monotonic() + budget_seconds
        super(DatabaseFenceDeadline, self).__init__()

    def checkpoint(self):
        if self.monotonic() >= self.deadline:
            raise self


def _database_error_is_busy(error, vendor):
    if (
        vendor == "sqlite"
        and isinstance(error, OperationalError)
        and _SQLITE_BUSY_WORD.search(str(error).lower())
    ):
        return True
    if vendor != "postgresql":
        return False
    for candidate in (
        error,
        getattr(error, "__cause__", None),
        getattr(error, "__context__", None),
    ):
        sqlstate = (
            getattr(candidate, "pgcode", None)
            or getattr(candidate, "sqlstate", None)
        )
        if sqlstate in _POSTGRESQL_BUSY_SQLSTATES:
            return True
    return False


def _close_connection_safely(database_connection):
    try:
        database_connection.close()
    except BaseException:
        pass


def _restore_sqlite_busy_timeout(database_connection, busy_timeout):
    with database_connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout = {}".format(busy_timeout))


def _raise_normalized(error, vendor):
    if isinstance(error, DatabaseFenceError):
        raise error
    if isinstance(error, Exception) and _database_error_is_busy(error, vendor):
        raise DatabaseFenceBusy() from None
    if isinstance(error, Exception):
        raise DatabaseFenceError() from None
    raise error


def _raise_busy_or_original(error, vendor):
    if isinstance(error, DatabaseFenceError):
        raise error
    if isinstance(error, Exception) and _database_error_is_busy(error, vendor):
        raise DatabaseFenceBusy() from None
    raise error


@contextmanager
def _database_write_fence_window(database_connection):
    if database_connection.vendor != "sqlite":
        try:
            yield
        except BaseException as error:
            _raise_busy_or_original(error, database_connection.vendor)
        return

    try:
        with database_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            row = cursor.fetchone()
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 1
            or type(row[0]) is not int
            or row[0] < 0
        ):
            raise ValueError("invalid_sqlite_busy_timeout")
        busy_timeout = row[0]
        with database_connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 0")
    except BaseException as error:
        _close_connection_safely(database_connection)
        _raise_normalized(error, "sqlite")

    try:
        yield
    except BaseException as error:
        try:
            _restore_sqlite_busy_timeout(
                database_connection,
                busy_timeout,
            )
        except BaseException:
            _close_connection_safely(database_connection)
        _raise_busy_or_original(error, "sqlite")
    else:
        try:
            _restore_sqlite_busy_timeout(
                database_connection,
                busy_timeout,
            )
        except BaseException as error:
            _close_connection_safely(database_connection)
            _raise_normalized(error, "sqlite")


def _acquire_database_write_fence(database_connection, models):
    models = tuple(models)
    if not models:
        raise ValueError("database_write_fence_requires_models")
    try:
        with database_connection.cursor() as cursor:
            if database_connection.vendor == "sqlite":
                model = models[0]
                table_name = database_connection.ops.quote_name(
                    model._meta.db_table
                )
                primary_key = database_connection.ops.quote_name(
                    model._meta.pk.column
                )
                cursor.execute(
                    "UPDATE {table} SET {pk} = {pk} WHERE 0 = 1".format(
                        table=table_name,
                        pk=primary_key,
                    )
                )
                return
            if database_connection.vendor == "postgresql":
                table_names = sorted({
                    model._meta.db_table for model in models
                })
                for raw_table_name in table_names:
                    table_name = database_connection.ops.quote_name(
                        raw_table_name
                    )
                    cursor.execute(
                        "LOCK TABLE {} IN EXCLUSIVE MODE NOWAIT".format(
                            table_name
                        )
                    )
                return
    except BaseException as error:
        _raise_normalized(error, database_connection.vendor)
    raise DatabaseFenceError("unsupported_database_fence_backend")


@contextmanager
def database_write_fence(using, models):
    database_connection = connections[using]
    if database_connection.in_atomic_block:
        raise RuntimeError(
            "database_write_fence_requires_top_level_transaction"
        )
    with _database_write_fence_window(database_connection):
        with transaction.atomic(using=using):
            _acquire_database_write_fence(database_connection, models)
            yield database_connection
