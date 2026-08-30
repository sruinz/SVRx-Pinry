from collections import namedtuple

from django.db import DEFAULT_DB_ALIAS, transaction

from taggit.models import Tag, TaggedItem


TagMergeResult = namedtuple(
    "TagMergeResult",
    (
        "target_name",
        "selected_count",
        "moved_link_count",
        "duplicate_link_count",
    ),
)


class TagMergeError(Exception):
    pass


def merge_tags(target_tag_id, selected_tag_ids, using=DEFAULT_DB_ALIAS):
    selected_ids = sorted(set(selected_tag_ids))
    if len(selected_ids) < 2:
        raise TagMergeError("최소 두 개의 태그를 선택하세요.")
    if target_tag_id not in selected_ids:
        raise TagMergeError("대표 태그가 선택 범위에 없습니다.")

    with transaction.atomic(using=using):
        tags = list(
            Tag.objects.using(using)
            .select_for_update()
            .filter(pk__in=selected_ids)
            .order_by("pk")
        )
        if [tag.pk for tag in tags] != selected_ids:
            raise TagMergeError(
                "선택한 태그가 변경되었습니다. 다시 시도하세요."
            )

        target = next(tag for tag in tags if tag.pk == target_tag_id)
        source_ids = [tag.pk for tag in tags if tag.pk != target.pk]
        occupied_links = set(
            TaggedItem.objects.using(using)
            .filter(tag_id=target.pk)
            .values_list("content_type_id", "object_id")
        )
        source_links = list(
            TaggedItem.objects.using(using)
            .filter(tag_id__in=source_ids)
            .order_by("pk")
        )
        move_ids = []
        duplicate_ids = []
        for link in source_links:
            identity = (link.content_type_id, link.object_id)
            if identity in occupied_links:
                duplicate_ids.append(link.pk)
            else:
                occupied_links.add(identity)
                move_ids.append(link.pk)

        if duplicate_ids:
            TaggedItem.objects.using(using).filter(
                pk__in=duplicate_ids,
            ).delete()
        if move_ids:
            TaggedItem.objects.using(using).filter(pk__in=move_ids).update(
                tag_id=target.pk,
            )
        Tag.objects.using(using).filter(pk__in=source_ids).delete()

        return TagMergeResult(
            target_name=target.name,
            selected_count=len(selected_ids),
            moved_link_count=len(move_ids),
            duplicate_link_count=len(duplicate_ids),
        )
