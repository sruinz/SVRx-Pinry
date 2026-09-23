from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils.translation import override

from taggit.models import Tag, TaggedItem

from core.models import Pin
from users.models import User


class TagAdminTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="tag-admin",
            email="tag-admin@example.com",
            password="password",
        )
        self.client.force_login(self.admin)
        self.url = reverse("admin:taggit_tag_changelist")
        self.pin_content_type = ContentType.objects.get_for_model(Pin)
        # Django 2.2의 override는 enable()/disable() 대신
        # __enter__/__exit__만 제공하므로 진입 시점과 정리를 직접 연결한다.
        language_override = override("ko")
        language_override.__enter__()
        self.addCleanup(language_override.__exit__, None, None, None)

    def create_tag(self, name):
        return Tag.objects.create(name=name, slug=name)

    def tag_pin(self, tag, pin_id):
        return TaggedItem.objects.create(
            tag=tag,
            content_type=self.pin_content_type,
            object_id=pin_id,
        )

    def test_changelist_shows_pin_usage_count(self):
        tag = self.create_tag("archive")
        self.tag_pin(tag, 101)
        self.tag_pin(tag, 102)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "핀 수")
        self.assertContains(
            response,
            'class="field-pin_usage_count">2</td>',
        )

    def test_merge_action_asks_for_a_target_from_selected_tags(self):
        first = self.create_tag("first")
        second = self.create_tag("second")

        response = self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(first.pk), str(second.pk)],
                "select_across": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "admin/taggit/tag/merge_confirmation.html",
        )
        self.assertContains(response, "대표 태그 선택")
        self.assertContains(response, first.name)
        self.assertContains(response, second.name)

    def test_merge_action_requires_at_least_two_tags(self):
        only_tag = self.create_tag("only")

        response = self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(only_tag.pk)],
                "select_across": "0",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "최소 두 개의 태그를 선택하세요.")

    def test_merge_action_moves_unique_links_and_removes_duplicates(self):
        target = self.create_tag("keep")
        first = self.create_tag("first")
        second = self.create_tag("second")
        self.tag_pin(target, 101)
        self.tag_pin(first, 101)
        self.tag_pin(first, 102)
        self.tag_pin(second, 102)
        self.tag_pin(second, 103)

        response = self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [
                    str(target.pk),
                    str(first.pk),
                    str(second.pk),
                ],
                "select_across": "0",
                "target_tag_id": str(target.pk),
                "apply": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Tag.objects.filter(pk=first.pk).exists())
        self.assertFalse(Tag.objects.filter(pk=second.pk).exists())
        self.assertEqual(
            list(
                TaggedItem.objects.filter(tag=target)
                .order_by("object_id")
                .values_list("object_id", flat=True)
            ),
            [101, 102, 103],
        )
        self.assertContains(
            response,
            "3개 태그를 keep 태그로 병합했습니다.",
        )

    def test_merge_action_rejects_a_target_outside_the_selection(self):
        first = self.create_tag("first")
        second = self.create_tag("second")
        outside = self.create_tag("outside")

        response = self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(first.pk), str(second.pk)],
                "select_across": "0",
                "target_tag_id": str(outside.pk),
                "apply": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tag.objects.count(), 3)
        self.assertContains(response, "대표 태그가 선택 범위에 없습니다.")

    def test_merge_action_requires_tag_delete_permission(self):
        editor = User.objects.create_user(
            username="tag-editor",
            email="tag-editor@example.com",
            password="password",
            is_staff=True,
        )
        editor.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="taggit",
                content_type__model="tag",
                codename="change_tag",
            )
        )
        self.create_tag("first")
        self.create_tag("second")
        self.client.force_login(editor)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "선택한 태그 병합")
