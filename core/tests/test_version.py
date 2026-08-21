from django.test import SimpleTestCase, override_settings

from core.version import normalize_source_commit


VALID_SOURCE_COMMIT = "9b54cf1b5a5a209b9aa8f000b6db53238b626001"
DEVELOPMENT_VERSION = {
    "source_commit": "development",
    "display_version": "development",
}


class SourceCommitNormalizationTests(SimpleTestCase):
    def test_accepts_exact_lowercase_forty_character_hex(self):
        self.assertEqual(
            normalize_source_commit(VALID_SOURCE_COMMIT),
            {
                "source_commit": VALID_SOURCE_COMMIT,
                "display_version": "9b54cf1b5a5a",
            },
        )

    def test_rejects_missing_or_non_string_values(self):
        for value in (None, b"a" * 40, True, 123, object()):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_source_commit(value),
                    DEVELOPMENT_VERSION,
                )

    def test_rejects_wrong_length_uppercase_and_non_hex_values(self):
        for value in (
            "a" * 39,
            "a" * 41,
            "A" * 40,
            "g" * 40,
            " a" * 20,
            " " + "a" * 40,
            "a" * 40 + " ",
            "a" * 40 + "\n",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_source_commit(value),
                    DEVELOPMENT_VERSION,
                )


class VersionEndpointTests(SimpleTestCase):
    @override_settings(PINRY_SOURCE_COMMIT=VALID_SOURCE_COMMIT)
    def test_anonymous_get_exposes_only_normalized_version_fields(self):
        response = self.client.get("/api/v2/version/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "source_commit": VALID_SOURCE_COMMIT,
                "display_version": "9b54cf1b5a5a",
            },
        )
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(PINRY_SOURCE_COMMIT="/srv/build/NOT-A-COMMIT")
    def test_invalid_runtime_value_fails_closed_without_echoing_it(self):
        response = self.client.get("/api/v2/version/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DEVELOPMENT_VERSION)
        self.assertNotContains(response, "/srv/build/NOT-A-COMMIT")

    def test_endpoint_is_read_only(self):
        response = self.client.post("/api/v2/version/", {})

        self.assertEqual(response.status_code, 405)

    @override_settings(
        PINRY_SOURCE_COMMIT=VALID_SOURCE_COMMIT,
        PUBLIC=False,
    )
    def test_private_site_still_allows_the_exact_anonymous_version_path(self):
        response = self.client.get("/api/v2/version/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source_commit"], VALID_SOURCE_COMMIT)

    @override_settings(PUBLIC=False)
    def test_private_site_keeps_other_and_similar_paths_closed(self):
        self.assertEqual(self.client.get("/api/v2/pins/").status_code, 403)
        self.assertEqual(
            self.client.get("/api/v2/version/evil").status_code,
            403,
        )

    @override_settings(PUBLIC=False)
    def test_private_site_preserves_the_existing_profile_prefix(self):
        response = self.client.get("/api/v2/profile/not-a-route")

        self.assertEqual(response.status_code, 404)
