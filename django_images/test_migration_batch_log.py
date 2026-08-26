import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from django_images.services.migration_batch_log import (
    BatchIntent,
    BatchLimits,
    CHECKPOINT_FILENAME,
    FileReceipt,
    JOURNAL_FILENAME,
    MigrationBatchJournal,
    MigrationBatchLogError,
    _durable_fsync,
    _durable_syncfs,
    _frame_for_event,
)


class MigrationBatchLogValueTests(SimpleTestCase):
    def receipt(self, **overrides):
        values = {
            "file_key": "original:1",
            "relative_path": "originals/a.png",
            "operation": "copy",
            "size": 10,
            "image_format": "PNG",
            "width": 2,
            "height": 3,
            "source_device": 1,
            "source_inode": 2,
            "destination_device": 3,
            "destination_inode": 4,
            "sha256": "a" * 64,
            "database_signature": "b" * 64,
        }
        values.update(overrides)
        return FileReceipt.for_values(**values)

    def intent(self, **overrides):
        values = {
            "batch_id": "paths:1-1",
            "batch_number": 1,
            "phase": "paths",
            "first_pk": 1,
            "last_pk": 1,
            "receipts": (self.receipt(),),
            "pre_signature": "c" * 64,
            "post_signature": "d" * 64,
            "images": 1,
            "files": 1,
            "total_bytes": 10,
            "total_pixels": 6,
        }
        values.update(overrides)
        return BatchIntent.for_values(**values)

    def test_default_limits_match_the_approved_contract(self):
        limits = BatchLimits()

        self.assertEqual(limits.max_images, 50)
        self.assertEqual(limits.max_bytes, 256 * 1024 * 1024)
        self.assertEqual(limits.max_pixels, 500000000)

    def test_receipt_rejects_absolute_and_parent_paths(self):
        for path in ("/data/static/media/a.png", "../a.png", "a/../../b"):
            with self.subTest(path=path):
                with self.assertRaisesMessage(
                    MigrationBatchLogError,
                    "linear_journal_receipt_invalid",
                ):
                    FileReceipt.for_values(
                        "original:1",
                        path,
                        "copy",
                        1,
                        "PNG",
                        1,
                        1,
                        1,
                        1,
                        2,
                        2,
                        "0" * 64,
                        "1" * 64,
                    )

    def test_limits_reject_non_positive_and_non_integer_values(self):
        for values in ((0, 1, 1), (1, -1, 1), (1, 1, True)):
            with self.subTest(values=values):
                with self.assertRaisesMessage(
                    MigrationBatchLogError,
                    "linear_journal_limits_invalid",
                ):
                    BatchLimits(*values)

    def test_limits_are_immutable_value_objects(self):
        limits = BatchLimits()

        with self.assertRaises(AttributeError):
            limits.max_images = 51

    def test_receipt_validates_all_fields_and_round_trips_immutably(self):
        receipt = self.receipt()

        self.assertEqual(FileReceipt.from_dict(receipt.as_dict()), receipt)
        with self.assertRaises(AttributeError):
            receipt.size = 11

        invalid_values = (
            {"file_key": ""},
            {"operation": "move"},
            {"size": -1},
            {"width": 0},
            {"height": True},
            {"source_device": -1},
            {"source_inode": 0},
            {"destination_device": -1},
            {"destination_inode": 0},
            {"sha256": "A" * 64},
            {"database_signature": "g" * 64},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaisesMessage(
                    MigrationBatchLogError,
                    "linear_journal_receipt_invalid",
                ):
                    self.receipt(**overrides)

    def test_batch_intent_validates_counters_and_round_trips_immutably(self):
        intent = self.intent()

        self.assertEqual(BatchIntent.from_dict(intent.as_dict()), intent)
        with self.assertRaises(AttributeError):
            intent.images = 2

        invalid_values = (
            {"batch_id": ""},
            {"batch_number": 0},
            {"phase": "other"},
            {"first_pk": 2},
            {"images": 0},
            {"files": 2},
            {"total_bytes": 9},
            {"total_pixels": 5},
            {"pre_signature": "C" * 64},
            {"post_signature": "x" * 64},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaisesMessage(
                    MigrationBatchLogError,
                    "linear_journal_batch_invalid",
                ):
                    self.intent(**overrides)


class VerifiedRunDirectory(object):
    def __init__(self, path):
        self.path = Path(path)
        self.descriptor = os.open(
            str(self.path),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


class MigrationBatchJournalTestCase(SimpleTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir="/private/tmp"
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.run_directory = VerifiedRunDirectory(
            self.temporary_directory.name
        )
        self.addCleanup(self.run_directory.close)
        self.path = Path(self.temporary_directory.name, JOURNAL_FILENAME)
        self.service_uid = os.geteuid()
        self.service_gid = os.getegid()

    def open_journal(self, **overrides):
        values = {
            "run_directory": self.run_directory,
            "filename": JOURNAL_FILENAME,
            "run_id": "run-1",
            "service_uid": self.service_uid,
            "service_gid": self.service_gid,
            "source_plan_sha256": "a" * 64,
            "source_manifest_sha256": "b" * 64,
        }
        values.update(overrides)
        with mock.patch(
            "django_images.services.migration_batch_log._utc_now",
            return_value="2026-08-27T00:00:00Z",
        ):
            return MigrationBatchJournal.open(**values)

    def events(self):
        events = []
        for raw_line in self.path.read_bytes().splitlines():
            frame = json.loads(raw_line.decode("utf-8"))
            payload = frame["payload"]
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self.assertEqual(
                frame["checksum"], hashlib.sha256(canonical).hexdigest()
            )
            events.append(payload)
        return events


class MigrationBatchJournalOpenTests(MigrationBatchJournalTestCase):
    def test_open_creates_one_fsynced_header_bound_to_v2_hashes(self):
        journal = self.open_journal()
        self.addCleanup(journal.close)

        self.assertEqual(journal.state.replay_count, 1)
        self.assertEqual(
            journal.state.source_bindings["paths"],
            {
                "plan_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
            },
        )
        self.assertEqual(stat.S_IMODE(os.stat(str(self.path)).st_mode), 0o600)
        self.assertEqual(
            self.events(),
            [
                {
                    "event": "header",
                    "format_version": 1,
                    "run_id": "run-1",
                    "started_at": "2026-08-27T00:00:00Z",
                    "source_manifest_sha256": "b" * 64,
                    "source_plan_sha256": "a" * 64,
                }
            ],
        )

    def test_open_fails_closed_when_bound_manifest_hash_changes(self):
        self.open_journal().close()

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_source_changed",
        ):
            self.open_journal(source_manifest_sha256="c" * 64)

    def test_bind_source_is_durable_idempotent_and_rejects_changes(self):
        journal = self.open_journal()
        self.addCleanup(journal.close)

        journal.bind_source("backfill", "c" * 64, "d" * 64)
        journal.bind_source("backfill", "c" * 64, "d" * 64)
        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_source_changed",
        ):
            journal.bind_source("backfill", "e" * 64, "d" * 64)

        self.assertEqual(len(self.events()), 2)
        self.assertEqual(
            journal.state.source_bindings["backfill"],
            {
                "plan_sha256": "c" * 64,
                "manifest_sha256": "d" * 64,
            },
        )

    def test_context_manager_closes_descriptor_once_on_error(self):
        with mock.patch(
            "django_images.services.migration_batch_log.os.close"
        ) as close:
            with self.assertRaises(RuntimeError):
                with self.open_journal():
                    raise RuntimeError("stop")

        close.assert_called_once()

    def test_open_rejects_symlink_without_modifying_target(self):
        target = Path(self.temporary_directory.name, "target")
        target.write_bytes(b"preserve")
        self.path.symlink_to(target)

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_invalid",
        ):
            self.open_journal()

        self.assertEqual(target.read_bytes(), b"preserve")


class MigrationBatchJournalAppendTests(MigrationBatchJournalTestCase):
    def receipt(self, **overrides):
        values = {
            "file_key": "original:1",
            "relative_path": "originals/a.png",
            "operation": "copy",
            "size": 10,
            "image_format": "PNG",
            "width": 2,
            "height": 3,
            "source_device": 1,
            "source_inode": 2,
            "destination_device": 3,
            "destination_inode": 4,
            "sha256": "a" * 64,
            "database_signature": "b" * 64,
        }
        values.update(overrides)
        return FileReceipt.for_values(**values)

    def intent(self, **overrides):
        receipts = overrides.pop("receipts", (self.receipt(),))
        values = {
            "batch_id": "paths:1-50",
            "batch_number": 1,
            "phase": "paths",
            "first_pk": 1,
            "last_pk": 50,
            "receipts": receipts,
            "pre_signature": "e" * 64,
            "post_signature": "f" * 64,
            "images": 1,
            "files": len(receipts),
            "total_bytes": sum(receipt.size for receipt in receipts),
            "total_pixels": sum(
                receipt.width * receipt.height for receipt in receipts
            ),
        }
        values.update(overrides)
        return BatchIntent.for_values(**values)

    def journal(self):
        journal = self.open_journal()
        self.addCleanup(journal.close)
        journal.freeze_work_totals(1000, 1000, 1000)
        return journal

    def committed_journal(self):
        journal = self.journal()
        journal.append_intent(self.intent())
        journal.append_commit("paths:1-50", "f" * 64)
        return journal

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_batch_uses_exactly_intent_and_commit_fsync(self, durable_fsync):
        journal = self.journal()
        durable_fsync.reset_mock()

        journal.append_intent(self.intent())
        journal.append_commit("paths:1-50", "f" * 64)

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["journal_intent", "journal_commit"],
        )
        self.assertEqual(journal.state.replay_count, 4)

    def test_append_never_reads_the_complete_journal_again(self):
        journal = self.journal()

        with mock.patch.object(journal, "_read_complete_file") as read_all:
            journal.append_intent(self.intent())
            journal.append_commit("paths:1-50", "f" * 64)

        read_all.assert_not_called()

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_repair_adds_one_event_without_incrementing_commit_ordinal(
        self, durable_fsync
    ):
        journal = self.committed_journal()
        before = journal.last_committed_batch()
        repaired = (
            self.receipt(destination_device=5, destination_inode=6),
        )
        durable_fsync.reset_mock()

        journal.append_repair("paths:1-50", repaired)

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["journal_repair"],
        )
        self.assertEqual(journal.last_committed_batch(), before)
        self.assertEqual(
            journal.effective_receipts("paths:1-50"), repaired
        )

    def test_repair_rejects_immutable_changes_and_file_key_mismatch(self):
        journal = self.committed_journal()

        for repaired in (
            (self.receipt(size=11),),
            (self.receipt(file_key="original:2"),),
        ):
            with self.subTest(repaired=repaired):
                with self.assertRaisesMessage(
                    MigrationBatchLogError,
                    "linear_journal_batch_conflict",
                ):
                    journal.append_repair("paths:1-50", repaired)

    @mock.patch("django_images.services.migration_batch_log.os.fsync")
    def test_durable_fsync_calls_os_fsync_exactly_once(self, fsync):
        _durable_fsync(17, "journal_intent")

        fsync.assert_called_once_with(17)

    @mock.patch("django_images.services.migration_batch_log._load_libc")
    def test_durable_syncfs_calls_libc_syncfs_exactly_once(self, load_libc):
        libc = mock.Mock()
        libc.syncfs.return_value = 0
        load_libc.return_value = libc

        _durable_syncfs(19, "batch_file_data")

        libc.syncfs.assert_called_once_with(19)

    @mock.patch("django_images.services.migration_batch_log._load_libc")
    def test_durable_syncfs_fails_closed_when_libc_cannot_load(
        self, load_libc
    ):
        load_libc.side_effect = OSError(2, "missing")

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_batch_file_sync_failed",
        ) as raised:
            _durable_syncfs(19, "batch_file_data")

        self.assertEqual(raised.exception.errno, 2)

    def test_append_retries_eintr_and_partial_writes_before_one_fsync(self):
        journal = self.journal()
        intent = self.intent()
        expected = _frame_for_event(
            {"event": "batch_intent", "intent": intent.as_dict()}
        )
        accepted = []
        results = [OSError(4, "interrupted"), 7, 11, "rest"]

        def short_write(_descriptor, remaining):
            result = results.pop(0)
            if isinstance(result, OSError):
                raise result
            count = len(remaining) if result == "rest" else result
            accepted.append(remaining[:count])
            return count

        with mock.patch(
            "django_images.services.migration_batch_log.os.write",
            side_effect=short_write,
        ), mock.patch(
            "django_images.services.migration_batch_log._durable_fsync"
        ) as durable_fsync:
            journal.append_intent(intent)

        self.assertEqual(b"".join(accepted), expected)
        durable_fsync.assert_called_once_with(
            journal.descriptor, "journal_intent"
        )

    def test_open_discards_only_unterminated_final_frame_and_fsyncs_repair(self):
        journal = self.committed_journal()
        journal.close()
        valid_prefix = self.path.read_bytes()
        partial = _frame_for_event(
            {
                "event": "batch_commit",
                "batch_id": "paths:other",
                "phase": "paths",
                "post_signature": "f" * 64,
            }
        )[:31]
        self.path.write_bytes(valid_prefix + partial)

        with mock.patch(
            "django_images.services.migration_batch_log._durable_fsync"
        ) as durable_fsync:
            reopened = self.open_journal()
        self.addCleanup(reopened.close)

        self.assertEqual(self.path.read_bytes(), valid_prefix)
        self.assertTrue(reopened.is_committed("paths:1-50"))
        durable_fsync.assert_called_once_with(
            reopened.descriptor, "journal_tail_repair"
        )

    def test_open_preserves_and_rejects_newline_terminated_bad_checksum(self):
        journal = self.committed_journal()
        journal.close()
        original = self.path.read_bytes() + _canonical_bad_checksum_frame()
        self.path.write_bytes(original)

        with self.assertRaisesMessage(
            MigrationBatchLogError, "linear_journal_invalid"
        ):
            self.open_journal()

        self.assertEqual(self.path.read_bytes(), original)

    def test_intent_without_commit_reexecutes_on_exact_pre_signature(self):
        journal = self.journal()
        journal.append_intent(self.intent())

        self.assertEqual(
            journal.recover_batch("paths:1-50", "e" * 64),
            "reexecute",
        )

    def test_intent_without_commit_appends_commit_on_exact_post_signature(self):
        journal = self.journal()
        journal.append_intent(self.intent())

        self.assertEqual(
            journal.recover_batch("paths:1-50", "f" * 64),
            "append_commit",
        )

    def test_intent_without_commit_fails_on_mixed_signature(self):
        journal = self.journal()
        journal.append_intent(self.intent())

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_database_conflict",
        ):
            journal.recover_batch("paths:1-50", "1" * 64)

    def test_committed_batch_is_already_committed(self):
        journal = self.committed_journal()

        self.assertEqual(
            journal.recover_batch("paths:1-50", "f" * 64),
            "committed",
        )

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_attempt_and_work_totals_are_durable_and_idempotent(
        self, durable_fsync
    ):
        journal = self.open_journal()
        self.addCleanup(journal.close)
        durable_fsync.reset_mock()

        first = journal.record_attempt("2026-08-27T00:00:00Z")
        journal.freeze_work_totals(100, 400, 100)
        journal.freeze_work_totals(100, 400, 100)

        self.assertEqual(
            first,
            {
                "attempt": 1,
                "resume_count": 0,
                "started_at": "2026-08-27T00:00:00Z",
            },
        )
        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["attempt", "work_totals"],
        )
        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_batch_conflict",
        ):
            journal.freeze_work_totals(101, 400, 100)

    def test_reopen_increments_attempt_but_preserves_first_started_at(self):
        journal = self.journal()
        journal.record_attempt("2026-08-27T00:00:00Z")
        journal.close()
        reopened = self.open_journal()
        self.addCleanup(reopened.close)

        second = reopened.record_attempt("2026-08-27T01:00:00Z")

        self.assertEqual(
            second,
            {
                "attempt": 2,
                "resume_count": 1,
                "started_at": "2026-08-27T00:00:00Z",
            },
        )

    def test_new_process_rebuilds_public_progress_from_journal_only(self):
        journal = self.open_journal()
        self.addCleanup(journal.close)
        journal.record_attempt("2026-08-27T00:00:00Z")
        journal.freeze_work_totals(100, 2, 100)
        path_receipts = (
            self.receipt(file_key="original:1"),
            self.receipt(
                file_key="thumbnail:1:2",
                relative_path="derivatives/a.png",
                source_inode=7,
                destination_inode=8,
            ),
        )
        path_intent = self.intent(
            receipts=path_receipts,
            images=50,
        )
        journal.append_intent(path_intent)
        journal.append_commit(path_intent.batch_id, path_intent.post_signature)
        journal.append_phase_complete(
            "paths",
            {
                "image_count": 50,
                "md5_legacy": 50,
                "fixed_slot": 0,
                "named_canonical": 0,
                "copy_required_bytes": 20,
            },
        )
        journal.bind_source("backfill", "c" * 64, "d" * 64)
        backfill_intent = self.intent(
            batch_id="backfill:1-10",
            batch_number=1,
            phase="backfill",
            first_pk=1,
            last_pk=10,
            images=10,
        )
        journal.append_intent(backfill_intent)
        journal.append_commit(
            backfill_intent.batch_id, backfill_intent.post_signature
        )
        journal.close()

        reopened = self.open_journal()
        self.addCleanup(reopened.close)
        snapshot = reopened.recovery_snapshot()

        self.assertEqual(
            snapshot,
            {
                "run_id": "run-1",
                "attempt": 1,
                "resume_count": 0,
                "started_at": "2026-08-27T00:00:00Z",
                "images_done": 50,
                "images_total": 100,
                "files_done": 2,
                "files_total": 2,
                "backfill_done": 10,
                "backfill_total": 100,
                "last_committed_batch": 2,
            },
        )
        self.assertEqual(
            reopened.receipts_by_image("paths"),
            {1: path_receipts},
        )

    def test_public_commit_ordinal_never_resets_across_phases_and_import(self):
        journal = self.open_journal()
        self.addCleanup(journal.close)
        journal.freeze_work_totals(3, 3, 1)
        imported = self.intent(
            batch_id="upgrade-paths:1-1",
            batch_number=4,
            first_pk=1,
            last_pk=1,
        )
        journal.import_v2_batch(imported)
        values = [journal.last_committed_batch()]
        next_paths = self.intent(
            batch_id="paths:2-2",
            batch_number=5,
            first_pk=2,
            last_pk=2,
            receipts=(
                self.receipt(
                    file_key="original:2",
                    source_inode=9,
                    destination_inode=10,
                ),
            ),
        )
        journal.append_intent(next_paths)
        journal.append_commit(next_paths.batch_id, next_paths.post_signature)
        values.append(journal.last_committed_batch())
        journal.append_phase_complete(
            "paths",
            {
                "image_count": 2,
                "md5_legacy": 2,
                "fixed_slot": 0,
                "named_canonical": 0,
                "copy_required_bytes": 20,
            },
        )
        journal.bind_source("backfill", "c" * 64, "d" * 64)
        backfill = self.intent(
            batch_id="backfill:1-1",
            batch_number=1,
            phase="backfill",
        )
        journal.append_intent(backfill)
        journal.append_commit(backfill.batch_id, backfill.post_signature)
        values.append(journal.last_committed_batch())

        self.assertEqual(values, [1, 2, 3])
        self.assertEqual(
            journal.last_committed_batch_for_phase("paths"), 5
        )
        self.assertEqual(
            journal.last_committed_batch_for_phase("backfill"), 1
        )
        self.assertEqual(
            journal.committed_ids("paths"),
            frozenset((imported.batch_id, next_paths.batch_id)),
        )
        self.assertEqual(journal.intent_for(imported.batch_id), imported)

    def test_phase_complete_requires_strict_summary(self):
        journal = self.committed_journal()
        summary = {
            "image_count": 1,
            "md5_legacy": 1,
            "fixed_slot": 0,
            "named_canonical": 0,
            "copy_required_bytes": 10,
        }

        journal.append_phase_complete("paths", summary)

        self.assertTrue(journal.is_phase_complete("paths"))
        self.assertEqual(journal.state.phase_summaries["paths"], summary)
        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_invalid",
        ):
            journal.append_phase_complete(
                "backfill",
                {
                    "scanned": 1,
                    "registered": 1,
                    "already_registered": 0,
                    "skipped": 0,
                    "reason_counts": {},
                    "extra": 0,
                },
            )

    def checkpoint_path(self):
        return Path(self.temporary_directory.name, CHECKPOINT_FILENAME)

    def completed_paths_journal(self):
        journal = self.committed_journal()
        journal.append_phase_complete(
            "paths",
            {
                "image_count": 1,
                "md5_legacy": 1,
                "fixed_slot": 0,
                "named_canonical": 0,
                "copy_required_bytes": 10,
            },
        )
        return journal

    def read_checkpoint(self):
        return json.loads(self.checkpoint_path().read_text(encoding="utf-8"))

    def write_raw_checkpoint_value(self, **updates):
        value = self.read_checkpoint()
        value.update(updates)
        self.checkpoint_path().write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_checkpoint_behind_is_rebuilt_and_ahead_fails_closed(self):
        journal = self.completed_paths_journal()
        journal.write_checkpoint()
        self.write_raw_checkpoint_value(last_committed_batch=0)

        journal.validate_or_rebuild_checkpoint()

        self.assertEqual(self.read_checkpoint()["last_committed_batch"], 1)
        self.write_raw_checkpoint_value(last_committed_batch=2)
        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_checkpoint_ahead",
        ):
            journal.validate_or_rebuild_checkpoint()

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_checkpoint_repeated_write_is_a_syscall_free_noop(
        self, durable_fsync
    ):
        journal = self.completed_paths_journal()
        durable_fsync.reset_mock()

        journal.write_checkpoint()
        journal.write_checkpoint()

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["phase_checkpoint", "phase_checkpoint"],
        )

    def test_replay_rejects_checksum_valid_intent_without_work_totals(self):
        journal = self.open_journal()
        journal.close()
        with self.path.open("ab") as journal_file:
            journal_file.write(
                _frame_for_event(
                    {
                        "event": "batch_intent",
                        "intent": self.intent().as_dict(),
                    }
                )
            )

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_invalid",
        ):
            self.open_journal()

    def test_backfill_intent_requires_paths_complete(self):
        journal = self.journal()
        journal.bind_source("backfill", "c" * 64, "d" * 64)
        backfill = self.intent(
            batch_id="backfill:1-1",
            phase="backfill",
        )

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_batch_conflict",
        ):
            journal.append_intent(backfill)

    def test_phase_complete_rejects_later_intents_for_the_same_phase(self):
        journal = self.completed_paths_journal()
        later = self.intent(
            batch_id="paths:51-51",
            batch_number=2,
            first_pk=51,
            last_pk=51,
            receipts=(
                self.receipt(
                    file_key="original:51",
                    source_inode=9,
                    destination_inode=10,
                ),
            ),
        )

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_batch_conflict",
        ):
            journal.append_intent(later)

    def test_phase_local_batch_number_cannot_be_reused(self):
        journal = self.committed_journal()
        duplicate_number = self.intent(
            batch_id="paths:51-51",
            batch_number=1,
            first_pk=51,
            last_pk=51,
            receipts=(
                self.receipt(
                    file_key="original:51",
                    source_inode=9,
                    destination_inode=10,
                ),
            ),
        )

        with self.assertRaisesMessage(
            MigrationBatchLogError,
            "linear_journal_batch_conflict",
        ):
            journal.append_intent(duplicate_number)

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_attempt_after_phase_boundary_does_not_rewrite_checkpoint(
        self, durable_fsync
    ):
        journal = self.completed_paths_journal()
        journal.write_checkpoint()
        original = self.checkpoint_path().read_bytes()
        journal.record_attempt("2026-08-27T01:00:00Z")
        durable_fsync.reset_mock()

        journal.write_checkpoint()

        durable_fsync.assert_not_called()
        self.assertEqual(self.checkpoint_path().read_bytes(), original)

    @mock.patch(
        "django_images.services.migration_batch_log._durable_fsync"
    )
    def test_repair_after_phase_completion_refreshes_checkpoint_projection(
        self, durable_fsync
    ):
        journal = self.completed_paths_journal()
        journal.write_checkpoint()
        original = self.checkpoint_path().read_bytes()
        repaired = (
            self.receipt(destination_device=5, destination_inode=6),
        )
        journal.append_repair("paths:1-50", repaired)
        durable_fsync.reset_mock()

        journal.write_checkpoint()

        self.assertEqual(
            [call.args[1] for call in durable_fsync.call_args_list],
            ["phase_checkpoint", "phase_checkpoint"],
        )
        self.assertNotEqual(self.checkpoint_path().read_bytes(), original)


def _canonical_bad_checksum_frame():
    return json.dumps(
        {
            "checksum": "0" * 64,
            "payload": {
                "event": "batch_commit",
                "batch_id": "paths:bad",
                "phase": "paths",
                "post_signature": "f" * 64,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
