from io import BytesIO
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import uuid

from django.test import SimpleTestCase, override_settings
import mock
from PIL import Image as PILImage

from core.services.media_storage import MediaStorage, MediaStorageError
from core.services.safe_url_fetch import FetchedImage
from django_images import file_ops
from django_images.file_ops import (
    MediaPathError,
    PublishFailure,
    create_owned_staging_file,
    open_media_root,
    open_or_create_media_directory_from,
    publish_owned_noreplace,
    sha256_file_descriptor,
    unlink_published_name_if_current,
    verify_published_name,
)
from django_images.test_helpers import TemporaryMediaMixin


ASSET_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def make_fetched_image(image_format="PNG", size=(640, 480)):
    output = BytesIO()
    PILImage.new("RGB", size, "red").save(output, format=image_format)
    return FetchedImage(
        content=output.getvalue(),
        image_format=image_format,
        width=size[0],
        height=size[1],
        final_url="https://images.example/photo.jpg",
    )


def file_snapshot(media_root):
    root = Path(media_root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class ManualClock(object):
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LinuxStrongPublishTestMixin(object):
    def setUp(self):
        super(LinuxStrongPublishTestMixin, self).setUp()
        if file_ops.sys.platform.startswith("linux"):
            return
        platform_patch = mock.patch(
            "django_images.file_ops.sys.platform",
            "linux",
        )
        publish_patch = mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=self._link_staging_descriptor_for_test,
        )
        platform_patch.start()
        publish_patch.start()
        self.addCleanup(publish_patch.stop)
        self.addCleanup(platform_patch.stop)

    def _link_staging_descriptor_for_test(
        self,
        descriptor,
        destination_directory_descriptor,
        destination_name,
    ):
        expected = os.fstat(descriptor)
        staging_root = Path(self.temporary_media.name, ".staging")
        for candidate in staging_root.rglob("*.part"):
            current = os.stat(str(candidate), follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                continue
            os.link(
                str(candidate),
                destination_name,
                dst_dir_fd=destination_directory_descriptor,
                follow_symlinks=False,
            )
            return
        raise OSError("test staging descriptor path was not found")


class MediaStorageTests(
    LinuxStrongPublishTestMixin,
    TemporaryMediaMixin,
    SimpleTestCase,
):
    def make_storage(self, **kwargs):
        return MediaStorage(media_root=self.temporary_media.name, **kwargs)

    def test_prepare_and_publish_preserves_original_and_derivatives(self):
        fetched = make_fetched_image()
        prepared = self.make_storage().prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="folder/page-image.jpg",
        )

        self.assertEqual(prepared.asset_uuid, ASSET_UUID)
        self.assertEqual(prepared.original_filename, "page-image.jpg")
        self.assertEqual(
            [entry.kind for entry in prepared.files],
            ["original", "thumbnail", "standard", "square"],
        )
        prepared_snapshot = file_snapshot(self.temporary_media.name)
        self.assertEqual(len(prepared_snapshot), 4)
        self.assertTrue(
            all(path.startswith(".staging/") for path in prepared_snapshot)
        )

        published = self.make_storage().publish(prepared)
        expected_paths = {
            "originals/{}/page-image.png".format(ASSET_UUID),
            "derivatives/{}/thumbnail.png".format(ASSET_UUID),
            "derivatives/{}/standard.png".format(ASSET_UUID),
            "derivatives/{}/square.png".format(ASSET_UUID),
        }
        snapshot = file_snapshot(self.temporary_media.name)
        self.assertEqual(set(snapshot), expected_paths)
        self.assertEqual(
            snapshot["originals/{}/page-image.png".format(ASSET_UUID)],
            fetched.content,
        )
        self.assertTrue(published.verify_current())

        expected_sizes = {
            "original": (640, 480),
            "thumbnail": (240, 180),
            "standard": (600, 450),
            "square": (125, 125),
        }
        for entry in published.files:
            with self.subTest(kind=entry.kind):
                self.assertEqual(
                    (entry.width, entry.height),
                    expected_sizes[entry.kind],
                )
                self.assertEqual(entry.image_format, "PNG")
                self.assertEqual(
                    entry.sha256,
                    hashlib.sha256(snapshot[entry.final_relative_path])
                    .hexdigest(),
                )
                self.assertEqual(
                    entry.size,
                    len(snapshot[entry.final_relative_path]),
                )
                with PILImage.open(Path(
                    self.temporary_media.name,
                    entry.final_relative_path,
                )) as stored_image:
                    stored_image.load()
                    self.assertEqual(stored_image.format, entry.image_format)
                    self.assertEqual(
                        stored_image.size,
                        (entry.width, entry.height),
                    )

        published.release()
        published.release()
        self.assertEqual(set(file_snapshot(self.temporary_media.name)), expected_paths)

    def test_normal_publish_and_release_emit_no_cleanup_warning(self):
        storage = self.make_storage()

        with mock.patch(
            "core.services.media_storage.logger.warning",
        ) as warning:
            prepared = storage.prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )
            published = storage.publish(prepared)
            published.release()

        warning.assert_not_called()

    def test_service_rechecks_staging_namespace_after_validation(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        asset_directory = Path(
            self.temporary_media.name,
            ".staging",
            str(prepared.run_uuid),
            str(ASSET_UUID),
        )
        retained = asset_directory.with_name("retained-after-validation")
        real_validate = storage._validate_prepared

        def validate_then_replace_namespace(asset, deadline=None):
            real_validate(asset, deadline)
            os.rename(str(asset_directory), str(retained))
            for staged_file in retained.iterdir():
                Path(outside.name, staged_file.name).write_bytes(
                    staged_file.read_bytes()
                )
            asset_directory.symlink_to(
                outside.name,
                target_is_directory=True,
            )

        with mock.patch.object(
            storage,
            "_validate_prepared",
            side_effect=validate_then_replace_namespace,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(
            sorted(path.name for path in Path(outside.name).iterdir()),
            [
                "original.part",
                "square.part",
                "standard.part",
                "thumbnail.part",
            ],
        )
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_supported_formats_use_actual_extension_and_exact_original(self):
        for image_format, extension in (
            ("JPEG", ".jpg"),
            ("PNG", ".png"),
            ("GIF", ".gif"),
            ("WEBP", ".webp"),
            ("BMP", ".bmp"),
            ("TIFF", ".tif"),
        ):
            with self.subTest(image_format=image_format):
                asset_uuid = uuid.uuid4()
                fetched = make_fetched_image(image_format, size=(32, 24))
                storage = self.make_storage()
                prepared = storage.prepare(
                    fetched,
                    asset_uuid=asset_uuid,
                    original_filename="misleading.jpeg",
                )
                published = storage.publish(prepared)
                original = next(
                    entry for entry in published.files
                    if entry.kind == "original"
                )
                self.assertEqual(
                    original.final_relative_path,
                    "originals/{}/misleading{}".format(
                        asset_uuid,
                        extension,
                    ),
                )
                self.assertEqual(
                    Path(
                        self.temporary_media.name,
                        original.final_relative_path,
                    ).read_bytes(),
                    fetched.content,
                )
                for entry in published.files:
                    self.assertEqual(entry.image_format, image_format)
                    self.assertEqual(
                        (entry.width, entry.height),
                        (32, 24),
                    )
                    self.assertTrue(
                        entry.final_relative_path.endswith(extension)
                    )
                    with PILImage.open(Path(
                        self.temporary_media.name,
                        entry.final_relative_path,
                    )) as stored_image:
                        stored_image.load()
                        self.assertEqual(stored_image.format, image_format)
                        self.assertEqual(
                            stored_image.size,
                            (entry.width, entry.height),
                        )
                published.compensate()

    def test_publish_conflict_compensates_only_files_created_by_attempt(self):
        fetched = make_fetched_image()
        storage = self.make_storage()
        prepared = storage.prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        conflict_path = Path(
            self.temporary_media.name,
            "derivatives",
            str(ASSET_UUID),
            "square.png",
        )
        conflict_path.parent.mkdir(parents=True)
        conflict_path.write_bytes(b"foreign")

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(file_snapshot(self.temporary_media.name), {
            "derivatives/{}/square.png".format(ASSET_UUID): b"foreign",
        })

    def test_compensation_preserves_reused_file(self):
        fetched = make_fetched_image()
        storage = self.make_storage()
        prepared = storage.prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        original = next(
            entry for entry in prepared.files if entry.kind == "original"
        )
        original_path = Path(
            self.temporary_media.name,
            original.final_relative_path,
        )
        original_path.parent.mkdir(parents=True)
        original_path.write_bytes(fetched.content)

        published = storage.publish(prepared)
        original_receipt = next(
            entry for entry in published.files if entry.kind == "original"
        )
        self.assertTrue(original_receipt.reused)
        self.assertFalse(original_receipt.created)

        published.compensate()
        published.compensate()
        self.assertEqual(file_snapshot(self.temporary_media.name), {
            original.final_relative_path: fetched.content,
        })

    def test_verify_and_compensate_preserve_replacement_inode(self):
        fetched = make_fetched_image()
        storage = self.make_storage()
        published = storage.publish(storage.prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        ))
        original = next(
            entry for entry in published.files if entry.kind == "original"
        )
        original_path = Path(
            self.temporary_media.name,
            original.final_relative_path,
        )
        replacement = original_path.with_name("replacement")
        replacement.write_bytes(b"foreign")
        os.replace(str(replacement), str(original_path))

        with self.assertRaises(MediaStorageError) as caught:
            published.verify_current()
        self.assertEqual(caught.exception.code, "media_publish_changed")

        with mock.patch(
            "core.services.media_storage.logger.warning",
        ) as warning:
            published.compensate()

        warning.assert_any_call(
            "media_storage_cleanup_incomplete event=%s asset_uuid=%s "
            "run_uuid=%s kind=%s error_type=%s",
            "compensate_file_preserved",
            str(ASSET_UUID),
            str(published.prepared.run_uuid),
            "original",
            "IdentityMismatch",
        )
        self.assertEqual(original_path.read_bytes(), b"foreign")

    def test_verify_rechecks_all_names_after_content_hash_pass(self):
        storage = self.make_storage()
        published = storage.publish(storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        ))
        original = next(
            entry for entry in published.files if entry.kind == "original"
        )
        original_path = Path(
            self.temporary_media.name,
            original.final_relative_path,
        )
        real_verify = verify_published_name
        calls = {"count": 0}

        def replace_original_during_later_hash(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 2:
                replacement = original_path.with_name("replacement")
                replacement.write_bytes(b"foreign")
                os.replace(str(replacement), str(original_path))
            return result

        with mock.patch(
            "core.services.media_storage.verify_published_name",
            side_effect=replace_original_during_later_hash,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                published.verify_current()

        self.assertEqual(caught.exception.code, "media_publish_changed")
        published.compensate()
        self.assertEqual(original_path.read_bytes(), b"foreign")

    def test_publish_revalidates_receipts_before_return(self):
        original_path = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "photo.png",
        )

        def replace_after_last_publish(event):
            if event != "after_square_publish":
                return
            replacement = original_path.with_name("replacement")
            replacement.write_bytes(b"foreign")
            os.replace(str(replacement), str(original_path))

        storage = self.make_storage(
            fault_injector=replace_after_last_publish,
        )
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_publish_changed")
        self.assertEqual(original_path.read_bytes(), b"foreign")
        self.assertEqual(file_snapshot(self.temporary_media.name), {
            "originals/{}/photo.png".format(ASSET_UUID): b"foreign",
        })

    def test_publish_rejects_replaced_destination_top_component(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        originals = Path(self.temporary_media.name, "originals")
        retained = Path(self.temporary_media.name, "retained-originals")

        def replace_originals_after_publish(event):
            if event != "after_original_publish":
                return
            os.rename(str(originals), str(retained))
            originals.symlink_to(outside.name, target_is_directory=True)

        storage = self.make_storage(
            fault_injector=replace_originals_after_publish,
        )
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_publish_changed")
        self.assertEqual(file_snapshot(outside.name), {})
        self.assertTrue(Path(
            retained,
            str(ASSET_UUID),
            "photo.png",
        ).is_file())

    def test_reused_receipt_replacement_is_never_compensated(self):
        fetched = make_fetched_image()
        original_path = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "photo.png",
        )
        original_path.parent.mkdir(parents=True)
        original_path.write_bytes(fetched.content)

        def replace_after_last_publish(event):
            if event != "after_square_publish":
                return
            replacement = original_path.with_name("replacement")
            replacement.write_bytes(b"foreign")
            os.replace(str(replacement), str(original_path))

        storage = self.make_storage(
            fault_injector=replace_after_last_publish,
        )
        prepared = storage.prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_publish_changed")
        self.assertEqual(file_snapshot(self.temporary_media.name), {
            "originals/{}/photo.png".format(ASSET_UUID): b"foreign",
        })

    def test_release_closes_all_owned_descriptors_without_deleting_final(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        root_directory = prepared.root_directory
        staging_directory = prepared.staging_directory
        handles = [
            entry.owned_staging_handle for entry in prepared.files
        ]
        published = storage.publish(prepared)
        destination_directories = published.destination_directories
        expected = file_snapshot(self.temporary_media.name)

        published.release()

        self.assertEqual(root_directory.descriptors, [])
        self.assertEqual(staging_directory.descriptors, [])
        self.assertTrue(all(handle._closed for handle in handles))
        self.assertTrue(all(
            not directory.descriptors
            for directory in destination_directories
        ))
        self.assertEqual(file_snapshot(self.temporary_media.name), expected)

    def test_release_attempts_every_directory_close_after_one_error(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        published = storage.publish(prepared)
        staging_descriptors = tuple(prepared.staging_directory.descriptors)
        failed_descriptor = staging_descriptors[-1]
        real_close = file_ops._close_descriptor
        attempted = []
        failed = {"value": False}

        def fail_one_close(descriptor):
            attempted.append(descriptor)
            if descriptor == failed_descriptor and not failed["value"]:
                failed["value"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "injected pre-close failure"
                )
            return real_close(descriptor)

        with mock.patch(
            "django_images.file_ops._close_descriptor",
            side_effect=fail_one_close,
        ):
            published.release()
            self.assertFalse(published._released)
            self.assertIn(
                failed_descriptor,
                prepared.staging_directory.descriptors,
            )
            published.release()

        self.assertTrue(set(staging_descriptors).issubset(set(attempted)))
        self.assertEqual(prepared.staging_directory.descriptors, [])
        self.assertTrue(all(
            not directory.descriptors
            for directory in published.destination_directories
        ))
        self.assertTrue(published._released)

    def test_symlinked_destination_directory_is_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        fetched = make_fetched_image()
        storage = self.make_storage()
        prepared = storage.prepare(
            fetched,
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        originals = Path(self.temporary_media.name, "originals")
        originals.mkdir()
        Path(originals, str(ASSET_UUID)).symlink_to(
            outside.name,
            target_is_directory=True,
        )

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_replaced_staging_namespace_is_rejected_before_publish(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        asset_directory = Path(
            self.temporary_media.name,
            ".staging",
            str(prepared.run_uuid),
            str(ASSET_UUID),
        )
        retained = asset_directory.with_name("retained")
        os.rename(str(asset_directory), str(retained))
        for staged_file in retained.iterdir():
            Path(outside.name, staged_file.name).write_bytes(
                staged_file.read_bytes()
            )
        outside_before = file_snapshot(outside.name)
        asset_directory.symlink_to(outside.name, target_is_directory=True)

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(file_snapshot(outside.name), outside_before)
        self.assertFalse(Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "photo.png",
        ).exists())

    def test_replaced_staging_run_component_is_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        run_directory = Path(
            self.temporary_media.name,
            ".staging",
            str(prepared.run_uuid),
        )
        retained = run_directory.with_name("retained-run")
        os.rename(str(run_directory), str(retained))
        outside_asset = Path(outside.name, str(ASSET_UUID))
        outside_asset.mkdir()
        for staged_file in Path(retained, str(ASSET_UUID)).iterdir():
            Path(outside_asset, staged_file.name).write_bytes(
                staged_file.read_bytes()
            )
        outside_before = file_snapshot(outside.name)
        run_directory.symlink_to(outside.name, target_is_directory=True)

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(file_snapshot(outside.name), outside_before)
        self.assertFalse(Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "photo.png",
        ).exists())

    def test_internal_destination_fsync_failure_is_compensated(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        real_publish = self._link_staging_descriptor_for_test
        real_fsync = file_ops.os.fsync
        state = {"published": False, "failed": False}

        def record_publish(*args, **kwargs):
            real_publish(*args, **kwargs)
            state["published"] = True

        def fail_destination_file_fsync(descriptor):
            if state["published"] and not state["failed"]:
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    state["failed"] = True
                    raise OSError("/private/secret/fsync failure")
            return real_fsync(descriptor)

        with mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=record_publish,
        ), mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=fail_destination_file_fsync,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared)

        self.assertEqual(
            caught.exception.code,
            "media_storage_failed",
        )
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_deadline_failure_cleans_staging_and_creates_no_final(self):
        clock = ManualClock()

        def advance_after_original(event):
            if event == "after_original_write":
                clock.advance(2)

        storage = self.make_storage(
            clock=clock,
            fault_injector=advance_after_original,
        )
        with self.assertRaises(MediaStorageError) as caught:
            storage.prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
                deadline=1,
            )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_decode_deadline_stops_before_staging_hash(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_inspect = storage._inspect

        def inspect_then_expire(descriptor):
            result = real_inspect(descriptor)
            clock.advance(2)
            return result

        with mock.patch.object(
            storage,
            "_inspect",
            side_effect=inspect_then_expire,
        ), mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            wraps=sha256_file_descriptor,
        ) as staged_hash:
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        staged_hash.assert_not_called()
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_staging_hash_deadline_stops_before_outer_fault_hook(self):
        clock = ManualClock()

        def unexpected_fault(_event):
            raise AssertionError("expired stage reached outer fault hook")

        storage = self.make_storage(
            clock=clock,
            fault_injector=unexpected_fault,
        )
        real_sha256 = sha256_file_descriptor

        def hash_then_expire(descriptor):
            digest = real_sha256(descriptor)
            clock.advance(2)
            return digest

        with mock.patch(
            "core.services.media_storage.sha256_file_descriptor",
            side_effect=hash_then_expire,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_resize_deadline_stops_before_staging_derivative(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        from django_images.utils import scale_and_crop_iter as real_scale

        class ExpiringIterator(object):
            def __init__(self, source, options):
                self.inner = real_scale(source, options)

            def __iter__(self):
                return self

            def __next__(self):
                result = next(self.inner)
                clock.advance(2)
                return result

            def close(self):
                self.inner.close()

        with mock.patch(
            "core.services.media_storage.scale_and_crop_iter",
            side_effect=ExpiringIterator,
        ), mock.patch.object(
            storage,
            "_stage_image",
            wraps=storage._stage_image,
        ) as stage_image:
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        stage_image.assert_not_called()
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_original_write_deadline_stops_before_file_fsync(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_write = storage._write_all
        real_fsync = os.fsync
        regular_fsyncs = []

        def write_then_expire(descriptor, content):
            real_write(descriptor, content)
            clock.advance(2)

        def record_fsync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                regular_fsyncs.append(descriptor)
            return real_fsync(descriptor)

        with mock.patch.object(
            storage,
            "_write_all",
            side_effect=write_then_expire,
        ), mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=record_fsync,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(regular_fsyncs, [])
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_original_file_fsync_deadline_stops_before_directory_fsync(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_create = create_owned_staging_file
        real_fsync = os.fsync
        state = {
            "expired": False,
            "cleaning": False,
            "directory_fsyncs_after_expiry": 0,
        }

        def create_with_cleanup_marker(*args, **kwargs):
            handle = real_create(*args, **kwargs)
            real_cleanup = handle.cleanup

            def marked_cleanup():
                state["cleaning"] = True
                return real_cleanup()

            handle.cleanup = marked_cleanup
            return handle

        def expire_after_file_fsync(descriptor):
            result = real_fsync(descriptor)
            mode = os.fstat(descriptor).st_mode
            if stat.S_ISREG(mode) and not state["expired"]:
                state["expired"] = True
                clock.advance(2)
            elif (
                state["expired"]
                and not state["cleaning"]
                and stat.S_ISDIR(mode)
            ):
                state["directory_fsyncs_after_expiry"] += 1
            return result

        with mock.patch(
            "core.services.media_storage.create_owned_staging_file",
            side_effect=create_with_cleanup_marker,
        ), mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=expire_after_file_fsync,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(state["directory_fsyncs_after_expiry"], 0)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_original_directory_fsync_deadline_stops_before_inspection(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_fsync = os.fsync
        real_inspect = storage._inspect
        state = {"file_synced": False, "expired": False}
        inspections_after_expiry = []

        def expire_after_directory_fsync(descriptor):
            result = real_fsync(descriptor)
            mode = os.fstat(descriptor).st_mode
            if stat.S_ISREG(mode):
                state["file_synced"] = True
            elif state["file_synced"] and not state["expired"]:
                state["expired"] = True
                clock.advance(2)
            return result

        def record_inspection(descriptor):
            if state["expired"]:
                inspections_after_expiry.append(descriptor)
            return real_inspect(descriptor)

        with mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=expire_after_directory_fsync,
        ), mock.patch.object(
            storage,
            "_inspect",
            side_effect=record_inspection,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(inspections_after_expiry, [])
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_derivative_save_deadline_stops_before_file_fsync(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_fsync = os.fsync
        regular_fsyncs = []
        state = {"derivative_saved": False}

        def save_then_expire(image, file_obj):
            from django_images.utils import write_image_to_file

            write_image_to_file(image, file_obj)
            state["derivative_saved"] = True
            clock.advance(2)

        def record_fsync(descriptor):
            if (
                state["derivative_saved"]
                and stat.S_ISREG(os.fstat(descriptor).st_mode)
            ):
                regular_fsyncs.append(descriptor)
            return real_fsync(descriptor)

        with mock.patch(
            "core.services.media_storage.write_image_to_file",
            side_effect=save_then_expire,
        ), mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=record_fsync,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(regular_fsyncs, [])
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_derivative_file_fsync_deadline_stops_before_directory_fsync(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_create = create_owned_staging_file
        real_fsync = os.fsync
        from django_images.utils import write_image_to_file as real_write
        state = {
            "derivative_saved": False,
            "expired": False,
            "cleaning": False,
            "directory_fsyncs_after_expiry": 0,
        }

        def mark_derivative_save(image, file_obj):
            real_write(image, file_obj)
            state["derivative_saved"] = True

        def create_with_cleanup_marker(*args, **kwargs):
            handle = real_create(*args, **kwargs)
            if handle.name == "original.part":
                return handle
            real_cleanup = handle.cleanup

            def marked_cleanup():
                state["cleaning"] = True
                return real_cleanup()

            handle.cleanup = marked_cleanup
            return handle

        def expire_after_derivative_file_fsync(descriptor):
            result = real_fsync(descriptor)
            mode = os.fstat(descriptor).st_mode
            if (
                state["derivative_saved"]
                and stat.S_ISREG(mode)
                and not state["expired"]
            ):
                state["expired"] = True
                clock.advance(2)
            elif (
                state["expired"]
                and not state["cleaning"]
                and stat.S_ISDIR(mode)
            ):
                state["directory_fsyncs_after_expiry"] += 1
            return result

        with mock.patch(
            "core.services.media_storage.write_image_to_file",
            side_effect=mark_derivative_save,
        ), mock.patch(
            "core.services.media_storage.create_owned_staging_file",
            side_effect=create_with_cleanup_marker,
        ), mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=expire_after_derivative_file_fsync,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(state["directory_fsyncs_after_expiry"], 0)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_derivative_directory_fsync_deadline_stops_before_inspection(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        real_fsync = os.fsync
        real_inspect = storage._inspect
        from django_images.utils import write_image_to_file as real_write
        state = {
            "derivative_saved": False,
            "file_synced": False,
            "expired": False,
        }
        inspections_after_expiry = []

        def mark_derivative_save(image, file_obj):
            real_write(image, file_obj)
            state["derivative_saved"] = True

        def expire_after_derivative_directory_fsync(descriptor):
            result = real_fsync(descriptor)
            mode = os.fstat(descriptor).st_mode
            if state["derivative_saved"] and stat.S_ISREG(mode):
                state["file_synced"] = True
            elif state["file_synced"] and not state["expired"]:
                state["expired"] = True
                clock.advance(2)
            return result

        def record_inspection(descriptor):
            if state["expired"]:
                inspections_after_expiry.append(descriptor)
            return real_inspect(descriptor)

        with mock.patch(
            "core.services.media_storage.write_image_to_file",
            side_effect=mark_derivative_save,
        ), mock.patch(
            "core.services.media_storage.os.fsync",
            side_effect=expire_after_derivative_directory_fsync,
        ), mock.patch.object(
            storage,
            "_inspect",
            side_effect=record_inspection,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                    deadline=1,
                )

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(inspections_after_expiry, [])
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    @override_settings(
        IMAGE_SIZES={
            "thumbnail": {"size": [240, 0]},
            "standard": {"size": [600, 0]},
        }
    )
    def test_invalid_required_sizes_fail_before_staging_creation(self):
        with self.assertRaises(MediaStorageError) as caught:
            self.make_storage().prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )

        self.assertEqual(caught.exception.code, "media_configuration_error")
        self.assertFalse(Path(self.temporary_media.name, ".staging").exists())

    def test_invalid_size_configurations_fail_before_staging_creation(self):
        invalid_configurations = (
            [],
            {
                "thumbnail": {"size": [True, 0]},
                "standard": {"size": [600, 0]},
                "square": {"size": [125, 125], "crop": True},
            },
            {
                "thumbnail": {"size": [240, 0]},
                "standard": {"size": [0, 0]},
                "square": {"size": [125, 125], "crop": True},
            },
            {
                "thumbnail": {"size": [240, 0]},
                "standard": {"size": [600, 0]},
                "square": {"size": [125, 125], "crop": "yes"},
            },
            {
                "thumbnail": {"size": [240, 0], "typo": True},
                "standard": {"size": [600, 0]},
                "square": {"size": [125, 125], "crop": True},
            },
        )
        for image_sizes in invalid_configurations:
            with self.subTest(image_sizes=image_sizes):
                with override_settings(IMAGE_SIZES=image_sizes):
                    with self.assertRaises(MediaStorageError) as caught:
                        self.make_storage().prepare(
                            make_fetched_image(),
                            asset_uuid=ASSET_UUID,
                            original_filename="photo.png",
                        )
                self.assertEqual(
                    caught.exception.code,
                    "media_configuration_error",
                )
                self.assertFalse(
                    Path(self.temporary_media.name, ".staging").exists()
                )

    def test_original_dimension_mismatch_cleans_owned_staging(self):
        fetched = make_fetched_image(size=(8, 8))._replace(width=9)

        with self.assertRaises(MediaStorageError) as caught:
            self.make_storage().prepare(
                fetched,
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )

        self.assertEqual(caught.exception.code, "image_processing_failed")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})
        staging = Path(self.temporary_media.name, ".staging")
        self.assertTrue(staging.is_dir())
        self.assertEqual(list(staging.iterdir()), [])

    def test_identity_unknown_staging_is_preserved_and_logged_safely(self):
        real_fstat = file_ops.os.fstat
        state = {"failed": False}

        def fail_first_regular_fstat(descriptor):
            result = real_fstat(descriptor)
            if stat.S_ISREG(result.st_mode) and not state["failed"]:
                state["failed"] = True
                raise OSError("/private/secret/token")
            return result

        with mock.patch(
            "django_images.file_ops.os.fstat",
            side_effect=fail_first_regular_fstat,
        ), mock.patch(
            "core.services.media_storage.logger.warning",
        ) as warning:
            with self.assertRaises(MediaStorageError) as caught:
                self.make_storage().prepare(
                    make_fetched_image(),
                    asset_uuid=ASSET_UUID,
                    original_filename="photo.png",
                )

        self.assertEqual(caught.exception.code, "media_storage_failed")
        snapshot = file_snapshot(self.temporary_media.name)
        self.assertEqual(len(snapshot), 1)
        relative_path, content = next(iter(snapshot.items()))
        self.assertTrue(relative_path.endswith("/original.part"))
        self.assertEqual(content, b"")
        warning.assert_any_call(
            "media_storage_cleanup_incomplete event=%s asset_uuid=%s "
            "run_uuid=%s kind=%s error_type=%s",
            "prepare_failed",
            str(ASSET_UUID),
            mock.ANY,
            "original",
            "OSError",
        )
        self.assertNotIn("secret", repr(warning.call_args_list))
        self.assertNotIn(self.temporary_media.name, repr(warning.call_args_list))

    def test_invalid_metadata_fails_safely_before_staging_creation(self):
        fetched = make_fetched_image()
        invalid_inputs = (
            (fetched._replace(width=True), "photo.png"),
            (fetched, None),
        )
        for invalid_fetched, filename in invalid_inputs:
            with self.subTest(filename=filename):
                with self.assertRaises(MediaStorageError) as caught:
                    self.make_storage().prepare(
                        invalid_fetched,
                        asset_uuid=ASSET_UUID,
                        original_filename=filename,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "image_processing_failed",
                )
                self.assertFalse(
                    Path(self.temporary_media.name, ".staging").exists()
                )

    def test_noncanonical_uuid_fails_before_staging_creation(self):
        with self.assertRaises(MediaStorageError) as caught:
            self.make_storage().prepare(
                make_fetched_image(),
                asset_uuid="ABCDEFAB-1234-5678-9ABC-ABCDEFABCDEF",
                original_filename="photo.png",
            )

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertFalse(Path(self.temporary_media.name, ".staging").exists())

    def test_prepare_faults_clean_every_completed_staging_file(self):
        events = (
            "after_original_write",
            "before_thumbnail_resize",
            "after_thumbnail_save",
            "before_standard_resize",
            "after_standard_save",
            "before_square_resize",
            "after_square_save",
        )
        for event_to_fail in events:
            with self.subTest(event=event_to_fail):
                def fail_at_event(event):
                    if event == event_to_fail:
                        raise RuntimeError("secret prepare failure")

                with self.assertRaises(MediaStorageError) as caught:
                    self.make_storage(
                        fault_injector=fail_at_event,
                    ).prepare(
                        make_fetched_image(),
                        asset_uuid=uuid.uuid4(),
                        original_filename="photo.png",
                    )
                self.assertEqual(
                    caught.exception.code,
                    "image_processing_failed",
                )
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_prepare_cleans_up_and_preserves_keyboard_interrupt(self):
        def interrupt_after_original(event):
            if event == "after_original_write":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.make_storage(
                fault_injector=interrupt_after_original,
            ).prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )

        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_publish_faults_compensate_all_completed_receipts(self):
        for event_to_fail in (
            "after_original_publish",
            "after_thumbnail_publish",
            "after_standard_publish",
            "after_square_publish",
        ):
            with self.subTest(event=event_to_fail):
                def fail_at_event(event):
                    if event == event_to_fail:
                        raise OSError("/private/secret/media failure")

                storage = self.make_storage(
                    fault_injector=fail_at_event,
                )
                prepared = storage.prepare(
                    make_fetched_image(),
                    asset_uuid=uuid.uuid4(),
                    original_filename="photo.png",
                )
                with self.assertRaises(MediaStorageError) as caught:
                    storage.publish(prepared)
                self.assertEqual(
                    caught.exception.code,
                    "media_storage_failed",
                )
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_publish_compensates_and_preserves_keyboard_interrupt(self):
        def interrupt_after_original(event):
            if event == "after_original_publish":
                raise KeyboardInterrupt()

        storage = self.make_storage(
            fault_injector=interrupt_after_original,
        )
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )

        with self.assertRaises(KeyboardInterrupt):
            storage.publish(prepared)

        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_unsupported_atomic_publish_has_distinct_safe_error(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        with mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=MediaPathError("atomic_publish_unsupported"),
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared)

        self.assertEqual(
            caught.exception.code,
            "media_storage_unsupported",
        )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_publish_deadline_compensates_partial_receipts(self):
        clock = ManualClock()

        def advance_after_thumbnail(event):
            if event == "after_thumbnail_publish":
                clock.advance(2)

        storage = self.make_storage(
            clock=clock,
            fault_injector=advance_after_thumbnail,
        )
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
            deadline=1,
        )

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared, deadline=1)

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_final_receipt_verification_observes_publish_deadline(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
            deadline=1,
        )
        real_verify = verify_published_name
        calls = {"count": 0}

        def expire_after_first_verify(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 1:
                clock.advance(2)
            return result

        with mock.patch(
            "core.services.media_storage.verify_published_name",
            side_effect=expire_after_first_verify,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared, deadline=1)

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_tampered_manifest_path_is_rejected_before_publish(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        original = prepared.files[0]
        prepared.files = (
            replace(original, final_relative_path="../../foreign.png"),
        ) + prepared.files[1:]

        with self.assertRaises(MediaStorageError) as caught:
            storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_prepared_original_leaf_tamper_is_rejected_before_publish(self):
        prepared = self.make_storage().prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="page-image.jpg",
        )
        original = prepared.files[0]
        object.__setattr__(
            original,
            "final_relative_path",
            "originals/{}/forged.png".format(ASSET_UUID),
        )

        with self.assertRaises(MediaStorageError) as caught:
            self.make_storage().publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_prepared_format_and_path_pair_tamper_is_rejected_before_publish(
        self,
    ):
        prepared = self.make_storage().prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="page-image.png",
        )
        original = prepared.files[0]
        object.__setattr__(original, "image_format", "JPEG")
        object.__setattr__(
            original,
            "final_relative_path",
            "originals/{}/page-image.jpg".format(ASSET_UUID),
        )

        with self.assertRaises(MediaStorageError) as caught:
            self.make_storage().publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_publish_validation_inspection_observes_deadline(self):
        clock = ManualClock()
        storage = self.make_storage(clock=clock)
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
            deadline=1,
        )
        real_inspect = storage._inspect
        calls = {"count": 0}

        def inspect_then_expire(descriptor):
            result = real_inspect(descriptor)
            calls["count"] += 1
            clock.advance(2)
            return result

        with mock.patch.object(
            storage,
            "_inspect",
            side_effect=inspect_then_expire,
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared, deadline=1)

        self.assertEqual(caught.exception.code, "image_processing_timeout")
        self.assertEqual(calls["count"], 1)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_publish_validation_inspection_error_fails_closed(self):
        storage = self.make_storage()
        prepared = storage.prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )

        with mock.patch.object(
            storage,
            "_inspect",
            side_effect=OSError("/private/secret/token"),
        ):
            with self.assertRaises(MediaStorageError) as caught:
                storage.publish(prepared)

        self.assertEqual(caught.exception.code, "media_path_conflict")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(file_snapshot(self.temporary_media.name), {})

    def test_cleanup_failure_does_not_stop_remaining_file_cleanup(self):
        prepared = self.make_storage().prepare(
            make_fetched_image(),
            asset_uuid=ASSET_UUID,
            original_filename="photo.png",
        )
        failed_handle = prepared.files[-1].owned_staging_handle
        with mock.patch.object(
            failed_handle,
            "cleanup",
            side_effect=OSError("injected cleanup failure"),
        ):
            prepared.cleanup()

        self.assertTrue(all(
            entry.owned_staging_handle._closed
            for entry in prepared.files
        ))
        remaining = file_snapshot(self.temporary_media.name)
        self.assertEqual(len(remaining), 1)
        self.assertTrue(next(iter(remaining)).endswith("square.part"))
        prepared.cleanup()

    def test_compensation_continues_and_removes_only_empty_owned_dirs(self):
        published = self.make_storage().publish(
            self.make_storage().prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )
        )
        real_unlink = unlink_published_name_if_current
        attempted = []
        originals_directory, derivatives_directory = (
            published.destination_directories
        )
        thumbnail_path = Path(
            self.temporary_media.name,
            "derivatives",
            str(ASSET_UUID),
            "thumbnail.png",
        )
        expected_thumbnail = thumbnail_path.read_bytes()

        def fail_thumbnail_once(
            directory,
            name,
            expected_stat,
            **kwargs
        ):
            attempted.append(name)
            if name == "thumbnail.png":
                raise OSError("injected compensation failure")
            return real_unlink(
                directory,
                name,
                expected_stat,
                **kwargs
            )

        with mock.patch(
            "core.services.media_storage.unlink_published_name_if_current",
            side_effect=fail_thumbnail_once,
        ), mock.patch.object(
            originals_directory,
            "fsync_publish",
            wraps=originals_directory.fsync_publish,
        ) as originals_fsync, mock.patch.object(
            derivatives_directory,
            "fsync_publish",
            wraps=derivatives_directory.fsync_publish,
        ) as derivatives_fsync:
            published.compensate()

        self.assertEqual(attempted, [
            "square.png",
            "standard.png",
            "thumbnail.png",
            "photo.png",
        ])
        self.assertEqual(originals_fsync.call_count, 1)
        self.assertEqual(derivatives_fsync.call_count, 2)
        self.assertEqual(file_snapshot(self.temporary_media.name), {
            "derivatives/{}/thumbnail.png".format(ASSET_UUID):
                expected_thumbnail,
        })
        self.assertFalse(Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
        ).exists())
        self.assertTrue(Path(
            self.temporary_media.name,
            "derivatives",
            str(ASSET_UUID),
        ).is_dir())
        self.assertEqual(
            list(Path(
                self.temporary_media.name,
                ".staging",
            ).iterdir()),
            [],
        )

    def test_compensation_fsyncs_each_successful_unlink_chain(self):
        published = self.make_storage().publish(
            self.make_storage().prepare(
                make_fetched_image(),
                asset_uuid=ASSET_UUID,
                original_filename="photo.png",
            )
        )
        originals_directory, derivatives_directory = (
            published.destination_directories
        )

        with mock.patch.object(
            originals_directory,
            "fsync_publish",
            wraps=originals_directory.fsync_publish,
        ) as originals_fsync, mock.patch.object(
            derivatives_directory,
            "fsync_publish",
            wraps=derivatives_directory.fsync_publish,
        ) as derivatives_fsync:
            published.compensate()

        self.assertEqual(originals_fsync.call_count, 1)
        self.assertEqual(derivatives_fsync.call_count, 3)
        self.assertEqual(file_snapshot(self.temporary_media.name), {})


class DescriptorPublishReceiptTests(
    LinuxStrongPublishTestMixin,
    TemporaryMediaMixin,
    SimpleTestCase,
):
    def setUp(self):
        super(DescriptorPublishReceiptTests, self).setUp()
        self.root_directory = open_media_root(self.temporary_media.name)
        self.addCleanup(self.root_directory.close)
        self.staging_directory = open_or_create_media_directory_from(
            self.root_directory,
            ".staging/{}/{}".format(uuid.uuid4(), ASSET_UUID),
        )
        self.addCleanup(self.staging_directory.close)
        self.destination_directory = open_or_create_media_directory_from(
            self.root_directory,
            "originals/{}".format(ASSET_UUID),
        )
        self.addCleanup(self.destination_directory.close)
        self.content = make_fetched_image().content
        self.staging_file = create_owned_staging_file(
            self.staging_directory,
            "original.part",
        )
        self.addCleanup(self.staging_file.close)
        os.write(self.staging_file.descriptor, self.content)
        os.fsync(self.staging_file.descriptor)
        self.staging_file.file_stat = os.fstat(
            self.staging_file.descriptor
        )
        self.digest = sha256_file_descriptor(
            self.staging_file.descriptor
        )

    def test_owned_staging_close_attempts_directory_after_file_error(self):
        path = Path(self.temporary_media.name, "legacy.part")
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        directory = mock.Mock()
        handle = file_ops.OwnedStagingFile(
            directory,
            "legacy.part",
            descriptor,
            os.fstat(descriptor),
            str(path),
            owns_directory=True,
        )
        real_close = file_ops._close_descriptor
        failed = {"value": False}

        def fail_file_close(candidate):
            if candidate == descriptor and not failed["value"]:
                failed["value"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "injected pre-close failure"
                )
            return real_close(candidate)

        with mock.patch(
            "django_images.file_ops._close_descriptor",
            side_effect=fail_file_close,
        ):
            with self.assertRaisesRegex(
                file_ops.DescriptorCloseNotAttempted,
                "pre-close",
            ):
                handle.close()
            self.assertFalse(handle._closed)
            handle.close()

        self.assertTrue(handle._closed)
        self.assertEqual(directory.close.call_count, 2)

    def test_owned_staging_close_retries_retained_directory_only(self):
        path = Path(self.temporary_media.name, "legacy-directory.part")
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        directory = mock.Mock()
        directory.descriptors = [12]
        state = {"failed": False}

        def close_directory():
            if not state["failed"]:
                state["failed"] = True
                raise file_ops.DescriptorCloseNotAttempted(
                    "injected directory pre-close failure"
                )
            directory.descriptors = []

        directory.close.side_effect = close_directory
        handle = file_ops.OwnedStagingFile(
            directory,
            "legacy-directory.part",
            descriptor,
            os.fstat(descriptor),
            str(path),
            owns_directory=True,
        )

        with self.assertRaisesRegex(
            file_ops.DescriptorCloseNotAttempted,
            "directory pre-close",
        ):
            handle.close()
        self.assertTrue(handle._closed)
        self.assertEqual(directory.descriptors, [12])

        handle.close()

        self.assertEqual(directory.descriptors, [])
        self.assertEqual(directory.close.call_count, 2)

    def test_path_wrapper_closes_child_when_root_close_fails(self):
        root_directory = mock.Mock()
        child_directory = mock.Mock()
        root_directory.close.side_effect = OSError(
            "injected root close failure"
        )

        with mock.patch(
            "django_images.file_ops.open_media_root",
            return_value=root_directory,
        ), mock.patch(
            "django_images.file_ops.open_or_create_media_directory_from",
            return_value=child_directory,
        ):
            with self.assertRaisesRegex(OSError, "root close"):
                file_ops.open_or_create_media_directory(
                    self.temporary_media.name,
                    "originals/asset",
                )

        child_directory.close.assert_called_once_with()

    def test_partial_directory_creation_removes_owned_nested_suffix(self):
        real_fsync = file_ops.os.fsync
        for relative_directory, top_name in (
            (
                ".partial-staging/{}/{}".format(
                    uuid.uuid4(), uuid.uuid4()
                ),
                ".partial-staging",
            ),
            (
                ".partial-derivatives/{}".format(uuid.uuid4()),
                ".partial-derivatives",
            ),
        ):
            with self.subTest(relative_directory=relative_directory):
                calls = {"count": 0}

                def fail_third_fsync(descriptor):
                    calls["count"] += 1
                    if calls["count"] == 3:
                        raise OSError("injected directory fsync failure")
                    return real_fsync(descriptor)

                with mock.patch(
                    "django_images.file_ops.os.fsync",
                    side_effect=fail_third_fsync,
                ):
                    with self.assertRaises(OSError):
                        open_or_create_media_directory_from(
                            self.root_directory,
                            relative_directory,
                        )

                top_directory = Path(
                    self.temporary_media.name,
                    top_name,
                )
                self.assertTrue(top_directory.is_dir())
                self.assertEqual(list(top_directory.iterdir()), [])

    def test_publish_directory_fsync_runs_leaf_to_root(self):
        calls = []
        with mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=lambda descriptor: calls.append(descriptor),
        ):
            self.destination_directory.fsync_publish()

        self.assertEqual(
            calls,
            list(reversed(self.destination_directory.descriptors)),
        )

    def test_destination_file_fsync_failure_carries_created_receipt(self):
        real_publish = self._link_staging_descriptor_for_test
        real_fsync = file_ops.os.fsync
        state = {"published": False, "failed": False}

        def record_publish(*args, **kwargs):
            real_publish(*args, **kwargs)
            state["published"] = True

        def fail_destination_file_fsync(descriptor):
            if state["published"] and not state["failed"]:
                mode = os.fstat(descriptor).st_mode
                if stat.S_ISREG(mode):
                    state["failed"] = True
                    raise OSError("injected destination fsync failure")
            return real_fsync(descriptor)

        with mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=record_publish,
        ), mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=fail_destination_file_fsync,
        ):
            with self.assertRaises(PublishFailure) as caught:
                publish_owned_noreplace(
                    self.staging_file,
                    self.destination_directory,
                    "original.png",
                    self.digest,
                )

        receipt = caught.exception.result
        self.assertTrue(receipt.created)
        self.assertFalse(receipt.reused)
        self.assertIsNotNone(receipt.destination_stat)
        self.assertTrue(Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "original.png",
        ).exists())
        self.assertTrue(unlink_published_name_if_current(
            self.destination_directory,
            "original.png",
            receipt.destination_stat,
        ))

    def test_owned_publish_rejects_darwin_before_clone(self):
        with mock.patch(
            "django_images.file_ops.sys.platform",
            "darwin",
        ), mock.patch(
            "django_images.file_ops._clone_descriptor_noreplace",
            wraps=file_ops._clone_descriptor_noreplace,
        ) as clone:
            with self.assertRaises(MediaPathError) as caught:
                publish_owned_noreplace(
                    self.staging_file,
                    self.destination_directory,
                    "darwin.png",
                    self.digest,
                )

        self.assertEqual(str(caught.exception), "atomic_publish_unsupported")
        clone.assert_not_called()
        self.assertFalse(Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "darwin.png",
        ).exists())

    def test_owned_publish_never_reopens_display_path_or_legacy_api(self):
        self.staging_file.path = "/outside/missing/original.part"
        with mock.patch(
            "django_images.file_ops.publish_noreplace",
            side_effect=AssertionError("legacy publish must not be used"),
        ) as legacy_publish:
            result = publish_owned_noreplace(
                self.staging_file,
                self.destination_directory,
                "descriptor-only.png",
                self.digest,
            )

        self.assertTrue(result.created)
        legacy_publish.assert_not_called()

    def test_created_publish_does_not_reopen_destination_name(self):
        with mock.patch(
            "django_images.file_ops._open_regular_nofollow",
            side_effect=AssertionError(
                "created hard link must use its held descriptor"
            ),
        ) as destination_open:
            result = publish_owned_noreplace(
                self.staging_file,
                self.destination_directory,
                "no-reopen.png",
                self.digest,
            )

        self.assertTrue(result.created)
        destination_open.assert_not_called()
        destination = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "no-reopen.png",
        )
        self.assertEqual(destination.read_bytes(), self.content)

    def test_created_publish_never_claims_foreign_replacement(self):
        destination = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "replaced.png",
        )

        def link_then_replace(*args, **kwargs):
            self._link_staging_descriptor_for_test(*args, **kwargs)
            replacement = destination.with_name("foreign")
            replacement.write_bytes(self.content)
            os.replace(str(replacement), str(destination))

        with mock.patch(
            "django_images.file_ops._publish_linux_descriptor",
            side_effect=link_then_replace,
        ):
            with self.assertRaises(PublishFailure) as caught:
                publish_owned_noreplace(
                    self.staging_file,
                    self.destination_directory,
                    "replaced.png",
                    self.digest,
                )

        receipt = caught.exception.result
        self.assertTrue(receipt.created)
        self.assertEqual(
            (receipt.destination_stat.st_dev,
             receipt.destination_stat.st_ino),
            (self.staging_file.file_stat.st_dev,
             self.staging_file.file_stat.st_ino),
        )
        self.assertFalse(unlink_published_name_if_current(
            self.destination_directory,
            "replaced.png",
            receipt.destination_stat,
        ))
        self.assertEqual(destination.read_bytes(), self.content)

    def test_post_publish_failures_carry_receipt(self):
        for failure_point in ("directory_fsync", "staging_unlink"):
            with self.subTest(failure_point=failure_point):
                if failure_point == "directory_fsync":
                    patcher = mock.patch.object(
                        self.destination_directory,
                        "fsync_publish",
                        side_effect=OSError("injected directory fsync"),
                    )
                else:
                    patcher = mock.patch(
                        "django_images.file_ops._unlink_owned_name",
                        side_effect=OSError("injected staging unlink"),
                    )
                with patcher:
                    with self.assertRaises(PublishFailure) as caught:
                        publish_owned_noreplace(
                            self.staging_file,
                            self.destination_directory,
                            "{}.png".format(failure_point),
                            self.digest,
                        )
                receipt = caught.exception.result
                self.assertTrue(receipt.created)
                self.assertTrue(unlink_published_name_if_current(
                    self.destination_directory,
                    "{}.png".format(failure_point),
                    receipt.destination_stat,
                ))

    def test_staging_directory_fsync_failure_carries_receipt(self):
        real_unlink = file_ops._unlink_owned_name
        real_fsync = file_ops.os.fsync
        state = {"unlinked": False, "failed": False}

        def record_unlink(*args, **kwargs):
            result = real_unlink(*args, **kwargs)
            state["unlinked"] = True
            return result

        def fail_staging_directory_fsync(descriptor):
            if (
                state["unlinked"]
                and not state["failed"]
                and descriptor == self.staging_directory.descriptor
            ):
                state["failed"] = True
                raise OSError("injected staging directory fsync")
            return real_fsync(descriptor)

        with mock.patch(
            "django_images.file_ops._unlink_owned_name",
            side_effect=record_unlink,
        ), mock.patch(
            "django_images.file_ops.os.fsync",
            side_effect=fail_staging_directory_fsync,
        ):
            with self.assertRaises(PublishFailure) as caught:
                publish_owned_noreplace(
                    self.staging_file,
                    self.destination_directory,
                    "staging-fsync.png",
                    self.digest,
                )

        receipt = caught.exception.result
        self.assertTrue(receipt.created)
        self.assertTrue(state["failed"])
        self.assertTrue(unlink_published_name_if_current(
            self.destination_directory,
            "staging-fsync.png",
            receipt.destination_stat,
        ))

    def test_retained_directory_detects_name_replacement(self):
        result = publish_owned_noreplace(
            self.staging_file,
            self.destination_directory,
            "original.png",
            self.digest,
        )
        asset_directory = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
        )
        retained = asset_directory.with_name("retained")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.rename(str(asset_directory), str(retained))
        asset_directory.symlink_to(outside.name, target_is_directory=True)

        with self.assertRaises(MediaPathError):
            verify_published_name(
                self.destination_directory,
                "original.png",
                result.destination_stat,
                self.digest,
            )
        self.assertFalse(unlink_published_name_if_current(
            self.destination_directory,
            "original.png",
            result.destination_stat,
        ))
        self.assertEqual(list(Path(outside.name).iterdir()), [])

    def test_verify_rejects_name_replaced_after_descriptor_hash(self):
        result = publish_owned_noreplace(
            self.staging_file,
            self.destination_directory,
            "original.png",
            self.digest,
        )
        destination = Path(
            self.temporary_media.name,
            "originals",
            str(ASSET_UUID),
            "original.png",
        )
        real_sha256 = file_ops.sha256_file_descriptor

        def replace_name_after_hash(descriptor):
            digest = real_sha256(descriptor)
            replacement = destination.with_name("replacement")
            replacement.write_bytes(b"foreign")
            os.replace(str(replacement), str(destination))
            return digest

        with mock.patch(
            "django_images.file_ops.sha256_file_descriptor",
            side_effect=replace_name_after_hash,
        ):
            with self.assertRaises(MediaPathError):
                verify_published_name(
                    self.destination_directory,
                    "original.png",
                    result.destination_stat,
                    self.digest,
                )

        self.assertEqual(destination.read_bytes(), b"foreign")


class MediaStorageOwnedRootTests(TemporaryMediaMixin, SimpleTestCase):
    def setUp(self):
        super(MediaStorageOwnedRootTests, self).setUp()
        self.strict_media = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.strict_media.cleanup)
        self.temporary_media = self.strict_media
        self.strict_media_override = override_settings(
            MEDIA_ROOT=self.temporary_media.name,
        )
        self.strict_media_override.enable()
        self.addCleanup(self.strict_media_override.disable)

    def test_verified_root_duplicate_is_independently_owned(self):
        root = file_ops.open_verified_media_root(self.temporary_media.name)
        duplicate = root.duplicate_owned()
        try:
            root_identity = os.fstat(root.descriptor)
            duplicate_identity = os.fstat(duplicate.descriptor)
            self.assertIsNot(root, duplicate)
            self.assertNotEqual(root.descriptor, duplicate.descriptor)
            self.assertEqual(
                (root_identity.st_dev, root_identity.st_ino),
                (duplicate_identity.st_dev, duplicate_identity.st_ino),
            )

            duplicate.close()

            self.assertFalse(duplicate.descriptors)
            self.assertTrue(root.verify_current())
        finally:
            duplicate.close()
            root.close()

    def test_prepare_from_root_uses_duplicate_and_keeps_shared_root_open(self):
        root = file_ops.open_verified_media_root(self.temporary_media.name)
        prepared = None
        storage = MediaStorage(media_root=self.temporary_media.name)
        try:
            with mock.patch(
                "core.services.media_storage.open_media_root",
                side_effect=AssertionError("pathname root reopen"),
            ) as legacy_open:
                prepared = storage.prepare_from_root(
                    root,
                    make_fetched_image(),
                    ASSET_UUID,
                    "owned-root.png",
                )

            legacy_open.assert_not_called()
            self.assertIsNot(prepared.root_directory, root)
            root_stat = os.fstat(root.descriptor)
            prepared_stat = os.fstat(prepared.root_directory.descriptor)
            self.assertEqual(
                (root_stat.st_dev, root_stat.st_ino),
                (prepared_stat.st_dev, prepared_stat.st_ino),
            )

            prepared.cleanup()

            self.assertFalse(prepared.root_directory.descriptors)
            self.assertTrue(root.verify_current())
        finally:
            if prepared is not None:
                prepared.cleanup()
            root.close()

    def test_two_preparations_cleanup_only_their_owned_root(self):
        root = file_ops.open_verified_media_root(self.temporary_media.name)
        storage = MediaStorage(media_root=self.temporary_media.name)
        first = None
        second = None
        try:
            first = storage.prepare_from_root(
                root,
                make_fetched_image(),
                ASSET_UUID,
                "first.png",
            )
            second = storage.prepare_from_root(
                root,
                make_fetched_image(size=(320, 240)),
                uuid.uuid4(),
                "second.png",
            )

            first.cleanup()

            self.assertFalse(first.root_directory.descriptors)
            self.assertTrue(root.verify_current())
            self.assertTrue(second.is_open)
            self.assertTrue(second.root_directory.verify_current())
        finally:
            if first is not None:
                first.cleanup()
            if second is not None:
                second.cleanup()
            root.close()

    def test_prepare_from_root_rejects_path_swap_without_external_writes(self):
        root = file_ops.open_verified_media_root(self.temporary_media.name)
        media_root = Path(self.temporary_media.name)
        retained = media_root.with_name(media_root.name + "-retained")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        media_root.rename(retained)
        media_root.symlink_to(outside.name, target_is_directory=True)
        try:
            with self.assertRaises(MediaStorageError) as caught:
                MediaStorage(
                    media_root=self.temporary_media.name,
                ).prepare_from_root(
                    root,
                    make_fetched_image(),
                    ASSET_UUID,
                    "swapped.png",
                )

            self.assertEqual(caught.exception.code, "media_path_conflict")
            self.assertEqual(list(Path(outside.name).rglob("*")), [])
        finally:
            root.close()
            media_root.unlink()
            retained.rename(media_root)
