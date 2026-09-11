from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BoardIndexMigrationTests(TransactionTestCase):
    def test_missing_legacy_indexes_are_created_without_changing_boards(self):
        self._check_migration(with_legacy_indexes=False)

    def test_existing_legacy_indexes_are_renamed_without_duplicates(self):
        self._check_migration(with_legacy_indexes=True)

    def _check_migration(self, with_legacy_indexes):
        executor = MigrationExecutor(connection)
        targets = [("core", "0017_alter_batchimportitem_retryable")]
        executor.migrate(targets)
        old_apps = executor.loader.project_state(targets).apps
        board_model = old_apps.get_model("core", "Board")
        owner = old_apps.get_model("users", "User").objects.create(
            username="index-migration-owner",
        )
        board = board_model.objects.create(
            submitter_id=owner.pk,
            name="보존 보드",
            display_order=7,
        )
        definitions = (
            (["submitter", "name"], ["submitter_id", "name"], "board_owner_name_idx"),
            (
                ["submitter", "display_order", "id"],
                ["submitter_id", "display_order", "id"],
                "board_owner_order_id_idx",
            ),
        )
        try:
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(
                    cursor,
                    board_model._meta.db_table,
                )
            with connection.schema_editor() as editor:
                for fields, columns, name in definitions:
                    for old_name, detail in constraints.items():
                        if (
                            detail["index"]
                            and not detail["unique"]
                            and detail["columns"] == columns
                        ):
                            editor.remove_index(
                                board_model, models.Index(fields=fields, name=old_name)
                            )
                    if with_legacy_indexes:
                        editor.add_index(
                            board_model, models.Index(fields=fields, name="old_" + name)
                        )

            executor = MigrationExecutor(connection)
            executor.migrate([("core", "0018_board_named_indexes")])
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(
                    cursor,
                    board_model._meta.db_table,
                )
            for fields, columns, name in definitions:
                matching = [
                    key
                    for key, detail in constraints.items()
                    if detail["index"]
                    and not detail["unique"]
                    and detail["columns"] == columns
                ]
                self.assertEqual(matching, [name])
            board.refresh_from_db()
            self.assertEqual(
                (board.submitter_id, board.name, board.display_order),
                (owner.pk, "보존 보드", 7),
            )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
