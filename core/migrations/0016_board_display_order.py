from django.db import migrations, models


def populate_display_order(apps, schema_editor):
    Board = apps.get_model("core", "Board")
    database = schema_editor.connection.alias
    owner_ids = (
        Board.objects.using(database)
        .order_by()
        .values_list("submitter_id", flat=True)
        .distinct()
    )
    for owner_id in owner_ids:
        boards = Board.objects.using(database).filter(
            submitter_id=owner_id
        ).order_by("-id")
        for display_order, board in enumerate(boards, start=1):
            board.display_order = display_order
            board.save(using=database, update_fields=["display_order"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_board_cover_pin"),
    ]

    operations = [
        migrations.AddField(
            model_name="board",
            name="display_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(populate_display_order, noop),
        migrations.AlterIndexTogether(
            name="board",
            index_together={
                ("submitter", "name"),
                ("submitter", "display_order", "id"),
            },
        ),
    ]
