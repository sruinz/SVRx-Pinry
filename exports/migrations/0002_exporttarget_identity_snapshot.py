from django.db import migrations, models


QUERY_CHUNK_SIZE = 400


def _chunks(values, size):
    chunk = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def backfill_target_identity(apps, schema_editor):
    pin_model = apps.get_model("core", "Pin")
    target_model = apps.get_model("exports", "ExportTarget")
    using = schema_editor.connection.alias
    targets = target_model.objects.using(using).order_by("pk").values_list(
        "pk", "pin_id"
    )
    for target_chunk in _chunks(targets.iterator(), QUERY_CHUNK_SIZE):
        pin_ids = [pin_id for _target_id, pin_id in target_chunk]
        identities = {
            pin_id: (owner_id, published)
            for pin_id, owner_id, published in
            pin_model.objects.using(using).filter(pk__in=pin_ids).values_list(
                "pk", "submitter_id", "published"
            )
        }
        for target_id, pin_id in target_chunk:
            identity = identities.get(pin_id)
            if identity is None:
                continue
            target_model.objects.using(using).filter(pk=target_id).update(
                pin_owner_id_snapshot=identity[0],
                pin_published_at_snapshot=identity[1],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_board_display_order"),
        ("exports", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="exporttarget",
            name="pin_owner_id_snapshot",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exporttarget",
            name="pin_published_at_snapshot",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_target_identity,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="exporttarget",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        pin_owner_id_snapshot__isnull=False,
                        pin_published_at_snapshot__isnull=False,
                    )
                    | models.Q(
                        pin_owner_id_snapshot__isnull=True,
                        pin_published_at_snapshot__isnull=True,
                    )
                ),
                name="export_target_identity_full",
            ),
        ),
    ]
