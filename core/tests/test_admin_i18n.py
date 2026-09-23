from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from taggit.models import Tag

User = get_user_model()


class AdminLanguageSelectionTest(TestCase):
    """관리자 페이지 언어가 쿠키→Accept-Language→한국어 순으로 결정되는지 검증한다."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="i18n-admin",
            email="i18n-admin@example.com",
            password="password",
        )
        self.client.force_login(self.admin)
        self.url = reverse("admin:index")

    def test_index_falls_back_to_korean_without_language_hints(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "사이트 관리")

    def test_index_uses_django_language_cookie(self):
        self.client.cookies["django_language"] = "fr"

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Site d’administration")

    def test_index_prefers_cookie_over_accept_language_header(self):
        self.client.cookies["django_language"] = "ko"

        response = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="fr")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "사이트 관리")

    def test_index_falls_back_to_korean_for_unsupported_cookie(self):
        self.client.cookies["django_language"] = "xx"

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "사이트 관리")

    def test_index_normalizes_chinese_accept_language(self):
        response = self.client.get(
            self.url, HTTP_ACCEPT_LANGUAGE="zh-CN,zh;q=0.9"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "站点管理")


class TagMergeScreenKoreanDefaultTest(TestCase):
    """래핑 후에도 기본 언어(ko) 화면 문구가 그대로인지 검증한다."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="i18n-merge-admin",
            email="i18n-merge-admin@example.com",
            password="password",
        )
        self.client.force_login(self.admin)
        self.first = Tag.objects.create(name="first", slug="first")
        self.second = Tag.objects.create(name="second", slug="second")

    def _open_merge_screen(self):
        return self.client.post(
            reverse("admin:taggit_tag_changelist"),
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(self.first.pk), str(self.second.pk)],
                "select_across": "0",
            },
            follow=True,
        )

    def test_merge_screen_renders_korean_by_default(self):
        response = self._open_merge_screen()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "대표 태그 선택")
        self.assertContains(response, "병합")
        self.assertContains(response, "취소")


class AdminCustomStringsTranslationTest(TestCase):
    """커스텀 문구가 4개 언어로 렌더링되는지 검증한다."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="i18n-catalog-admin",
            email="i18n-catalog-admin@example.com",
            password="password",
        )
        self.client.force_login(self.admin)
        self.first = Tag.objects.create(name="first", slug="first")
        self.second = Tag.objects.create(name="second", slug="second")
        self.url = reverse("admin:taggit_tag_changelist")

    def _open_merge_screen(self, language=None):
        if language is not None:
            self.client.cookies["django_language"] = language
        return self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(self.first.pk), str(self.second.pk)],
                "select_across": "0",
            },
            follow=True,
        )

    def test_merge_screen_renders_each_language(self):
        cases = [
            ("ko", "대표 태그 선택", "취소"),
            ("en", "Select primary tag", "Cancel"),
            ("zh-hans", "选择主要标签", "取消"),
            ("fr", "Sélectionner le tag principal", "Annuler"),
        ]
        for language, heading, cancel in cases:
            with self.subTest(language=language):
                response = self._open_merge_screen(language)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, cancel)

    def test_merge_success_message_translated(self):
        for language, expected in [
            ("en", "Merged 2 tags into keep."),
            ("fr", "2 tags fusionnés dans keep."),
        ]:
            # subTest 간 DB 롤백이 없어 대표 태그 이름/slug가 unique 제약에
            # 걸리므로 각 subTest를 savepoint로 격리한다.
            with self.subTest(language=language), transaction.atomic():
                target = Tag.objects.create(name="keep", slug="keep")
                extra = Tag.objects.create(
                    name="extra-%s" % language, slug="extra-%s" % language
                )

                self.client.cookies["django_language"] = language
                response = self.client.post(
                    self.url,
                    {
                        "action": "merge_selected_tags",
                        "_selected_action": [str(target.pk), str(extra.pk)],
                        "select_across": "0",
                        "target_tag_id": str(target.pk),
                        "apply": "1",
                    },
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)
                transaction.set_rollback(True)

    def test_chinese_accept_language_uses_own_strings(self):
        response = self.client.post(
            self.url,
            {
                "action": "merge_selected_tags",
                "_selected_action": [str(self.first.pk), str(self.second.pk)],
                "select_across": "0",
            },
            follow=True,
            HTTP_ACCEPT_LANGUAGE="zh",
        )

        self.assertContains(response, "选择主要标签")

    def test_catalogs_contain_core_translations(self):
        for language, expected in [
            ("en", "Merge tags"),
            ("zh-hans", "合并标签"),
            ("fr", "Fusionner les tags"),
        ]:
            with self.subTest(language=language), translation.override(language):
                self.assertEqual(translation.gettext("태그 병합"), expected)
                self.assertNotEqual(
                    translation.gettext("병합"),
                    translation.gettext("취소"),
                )


class AdminModelLabelTranslationTest(TestCase):
    """관리자 인덱스의 앱·모델 표시명이 언어별로 렌더링되는지 검증한다."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="i18n-label-admin",
            email="i18n-label-admin@example.com",
            password="password",
        )
        self.client.force_login(self.admin)
        self.url = reverse("admin:index")

    def test_admin_index_shows_translated_app_and_model_labels(self):
        cases = [
            ("ko", ["코어", "이미지", "Pin", "보드", "썸네일"]),
            ("en", ["Core", "Image", "Pin", "Board", "Thumbnail"]),
            ("zh-hans", ["核心", "图片", "Pin", "画板", "缩略图"]),
            ("fr", ["Noyau", "Image", "Fiche", "Tableau", "Miniature"]),
        ]
        for language, expected in cases:
            with self.subTest(language=language):
                self.client.cookies["django_language"] = language

                response = self.client.get(self.url)

                self.assertEqual(response.status_code, 200)
                for text in expected:
                    self.assertContains(response, text)

    def test_model_options_have_migrations(self):
        from django.core.management import call_command

        call_command("makemigrations", "core", "django_images",
                     check_changes=True, dry_run=True)


class ApiLanguageInvarianceTest(TestCase):
    """언어 쿠키가 있어도 JSON API 응답 본문이 동일한지 검증한다."""

    def test_version_api_identical_regardless_of_language(self):
        plain = self.client.get("/api/v2/version/")

        self.client.cookies["django_language"] = "fr"
        french = self.client.get("/api/v2/version/")

        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.content, french.content)
