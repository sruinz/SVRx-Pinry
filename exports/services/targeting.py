from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from core.models import Board, MediaAsset, Pin
from django_images.file_ops import (
    open_verified_media_file,
    open_verified_media_root,
)
from exports.services.file_ops import SpaceBudget


QUERY_CHUNK_SIZE = 400
_METADATA_PER_PIN_BYTES = 32 * 1024
_MANIFEST_OVERHEAD_BYTES = 64 * 1024
_METADATA_ESCAPE_AND_DOCUMENT_FACTOR = 16


class ExportRequestError(Exception):
    def __init__(self, code, status_code):
        self.code = code
        self.status_code = status_code
        super(ExportRequestError, self).__init__(code)
        self.__suppress_context__ = True


class TargetIdentityChanged(Exception):
    pass


def iter_chunks(values, size=QUERY_CHUNK_SIZE):
    if type(size) is not int or size <= 0:
        raise ValueError("chunk_size_must_be_positive")
    values = tuple(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


@dataclass(frozen=True)
class TargetIdentity(object):
    pin_id: int
    image_id: int
    media_asset_id: object
    storage_name: str
    content_sha256: object
    owner_id: int
    private: bool

    @property
    def source_key(self):
        return (
            self.image_id,
            self.media_asset_id,
            self.storage_name,
            self.content_sha256,
        )


@dataclass(frozen=True)
class TargetSnapshot(object):
    scope: str
    identities: tuple
    requested_total: int
    excluded_total: int
    owned_private_total: int
    metadata_overhead: int
    board_id: object = None
    board_name: object = None
    board_private: object = None
    board_owner_username: object = None

    @property
    def pin_ids(self):
        return tuple(identity.pin_id for identity in self.identities)


@dataclass(frozen=True)
class ObservedTargets(object):
    snapshot: TargetSnapshot
    sizes: dict
    distinct_original_bytes: int
    archive_pin_bytes: int
    metadata_overhead: int

    @property
    def space_budget(self):
        return SpaceBudget.for_export(
            self.distinct_original_bytes,
            self.archive_pin_bytes,
            self.metadata_overhead,
        )


@dataclass(frozen=True)
class ExportPreview(object):
    schema_version: int
    as_of: object
    scope: str
    requested_total: int
    eligible_total: int
    excluded_total: int
    owned_private_total: int
    estimated_original_files: int
    estimated_original_bytes: int
    estimated_zip_bytes: int
    pin_ids: tuple

    def as_dict(self):
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "scope": self.scope,
            "requested_total": self.requested_total,
            "eligible_total": self.eligible_total,
            "excluded_total": self.excluded_total,
            "owned_private_total": self.owned_private_total,
            "estimated_original_files": self.estimated_original_files,
            "estimated_original_bytes": self.estimated_original_bytes,
            "estimated_zip_bytes": self.estimated_zip_bytes,
        }


@dataclass(frozen=True)
class CapturedTargets(object):
    pin_ids: tuple
    identities: tuple
    job_fields: dict
    space_budget: SpaceBudget


def _media_asset(image):
    try:
        return image.media_asset
    except MediaAsset.DoesNotExist:
        return None


def _identity(pin):
    asset = _media_asset(pin.image)
    return TargetIdentity(
        pin_id=pin.pk,
        image_id=pin.image_id,
        media_asset_id=asset.pk if asset is not None else None,
        storage_name=pin.image.image.name,
        content_sha256=(
            asset.content_sha256 if asset is not None else None
        ),
        owner_id=pin.submitter_id,
        private=pin.private,
    )


def _encoded_size(value):
    if value is None:
        return 0
    return len(str(value).encode("utf-8", errors="replace"))


def _metadata_overhead(pins):
    payload_bytes = 0
    for pin in pins:
        payload_bytes += sum(_encoded_size(value) for value in (
            pin.submitter.username,
            pin.description,
            pin.url,
            pin.referer,
            pin.image.original_filename,
            pin.image.image.name,
        ))
        payload_bytes += sum(
            _encoded_size(tag.name) for tag in pin.tags.all()
        )
    return (
        _MANIFEST_OVERHEAD_BYTES
        + len(pins) * _METADATA_PER_PIN_BYTES
        + payload_bytes * _METADATA_ESCAPE_AND_DOCUMENT_FACTOR
    )


def _default_size_observer(root, identity):
    receipt = open_verified_media_file(root, identity.storage_name)
    try:
        return receipt.file_stat.st_size
    finally:
        receipt.close()


class TargetingService(object):
    def __init__(self, size_observer=None):
        self.size_observer = size_observer

    def _load_pins(self, pin_ids, user, checkpoint):
        visible = Q(submitter_id=user.pk) | Q(private=False)
        by_id = {}
        for pin_id_chunk in iter_chunks(pin_ids):
            pins = (
                Pin.objects.filter(pk__in=pin_id_chunk)
                .filter(visible)
                .select_related("submitter", "image", "image__media_asset")
                .prefetch_related("tags")
            )
            by_id.update((pin.pk, pin) for pin in pins)
            checkpoint()
        return by_id

    def _snapshot(self, user, request_data, checkpoint):
        scope = request_data["scope"]
        if scope == "pins":
            pin_ids = tuple(request_data["pin_ids"])
            by_id = self._load_pins(pin_ids, user, checkpoint)
            if any(pin_id not in by_id for pin_id in pin_ids):
                raise ExportRequestError("invalid_target", 404)
            pins = tuple(by_id[pin_id] for pin_id in pin_ids)
            return TargetSnapshot(
                scope="pins",
                identities=tuple(_identity(pin) for pin in pins),
                requested_total=len(pin_ids),
                excluded_total=0,
                owned_private_total=sum(
                    pin.submitter_id == user.pk and pin.private
                    for pin in pins
                ),
                metadata_overhead=_metadata_overhead(pins),
            )

        board = (
            Board.objects.select_related("submitter")
            .filter(pk=request_data["board_id"], submitter_id=user.pk)
            .first()
        )
        if board is None:
            raise ExportRequestError("invalid_target", 404)
        membership_ids = tuple(
            Board.pins.through.objects
            .filter(board_id=board.pk)
            .order_by("pin_id")
            .values_list("pin_id", flat=True)
        )
        checkpoint()
        by_id = self._load_pins(membership_ids, user, checkpoint)
        visible_ids = tuple(
            pin_id for pin_id in membership_ids if pin_id in by_id
        )
        pins = tuple(by_id[pin_id] for pin_id in visible_ids)
        return TargetSnapshot(
            scope="board",
            identities=tuple(_identity(pin) for pin in pins),
            requested_total=len(membership_ids),
            excluded_total=len(membership_ids) - len(visible_ids),
            owned_private_total=sum(
                pin.submitter_id == user.pk and pin.private for pin in pins
            ),
            metadata_overhead=_metadata_overhead(pins),
            board_id=board.pk,
            board_name=board.name,
            board_private=board.private,
            board_owner_username=board.submitter.username,
        )

    def snapshot_for_observation(self, user, request_data):
        with transaction.atomic():
            return self._snapshot(user, request_data, lambda: None)

    def observe_snapshot(self, snapshot):
        sizes = {}
        fallback = settings.PINRY_FETCH_MAX_BYTES
        root = None
        if self.size_observer is None:
            try:
                root = open_verified_media_root(settings.MEDIA_ROOT)
            except Exception:
                root = None
        try:
            for identity in snapshot.identities:
                if identity.source_key in sizes:
                    continue
                try:
                    if self.size_observer is None:
                        if root is None:
                            raise ValueError("media_root_unavailable")
                        size = _default_size_observer(root, identity)
                    else:
                        size = self.size_observer(identity)
                    if type(size) is not int or size < 0:
                        raise ValueError("invalid_observed_size")
                except Exception:
                    size = fallback
                sizes[identity.source_key] = size
        finally:
            if root is not None:
                try:
                    root.close()
                except Exception:
                    pass
        distinct_original_bytes = sum(sizes.values())
        archive_pin_bytes = sum(
            sizes[identity.source_key] for identity in snapshot.identities
        )
        metadata_overhead = snapshot.metadata_overhead
        return ObservedTargets(
            snapshot=snapshot,
            sizes=sizes,
            distinct_original_bytes=distinct_original_bytes,
            archive_pin_bytes=archive_pin_bytes,
            metadata_overhead=metadata_overhead,
        )

    def preview(self, user, request_data, now):
        snapshot = self.snapshot_for_observation(user, request_data)
        observed = self.observe_snapshot(snapshot)
        return ExportPreview(
            schema_version=1,
            as_of=now,
            scope=snapshot.scope,
            requested_total=snapshot.requested_total,
            eligible_total=len(snapshot.identities),
            excluded_total=snapshot.excluded_total,
            owned_private_total=snapshot.owned_private_total,
            estimated_original_files=len(observed.sizes),
            estimated_original_bytes=observed.distinct_original_bytes,
            estimated_zip_bytes=(
                observed.archive_pin_bytes + observed.metadata_overhead
            ),
            pin_ids=snapshot.pin_ids,
        )

    def capture_locked(
        self,
        user,
        request_data,
        now,
        checkpoint,
        observed_sizes=None,
        expected_snapshot=None,
    ):
        del now
        snapshot = self._snapshot(user, request_data, checkpoint)
        if expected_snapshot is not None and snapshot != expected_snapshot:
            raise TargetIdentityChanged()
        if observed_sizes is None:
            observed_sizes = {
                identity.source_key: settings.PINRY_FETCH_MAX_BYTES
                for identity in snapshot.identities
            }
        if any(
            identity.source_key not in observed_sizes
            for identity in snapshot.identities
        ):
            raise TargetIdentityChanged()
        distinct = {
            identity.source_key for identity in snapshot.identities
        }
        distinct_original_bytes = sum(
            observed_sizes[source_key] for source_key in distinct
        )
        archive_pin_bytes = sum(
            observed_sizes[identity.source_key]
            for identity in snapshot.identities
        )
        metadata_overhead = snapshot.metadata_overhead
        job_fields = {
            "scope": snapshot.scope,
            "requested_total": snapshot.requested_total,
            "target_total": len(snapshot.identities),
            "included_total": len(snapshot.identities),
            "excluded_total": snapshot.excluded_total,
            "excluded_not_visible_total": snapshot.excluded_total,
            "bytes_total": archive_pin_bytes,
        }
        if snapshot.scope == "board":
            job_fields.update({
                "board_id_snapshot": snapshot.board_id,
                "board_name_snapshot": snapshot.board_name,
                "board_private_snapshot": snapshot.board_private,
                "board_owner_username_snapshot": (
                    snapshot.board_owner_username
                ),
            })
        return CapturedTargets(
            pin_ids=snapshot.pin_ids,
            identities=snapshot.identities,
            job_fields=job_fields,
            space_budget=SpaceBudget.for_export(
                distinct_original_bytes,
                archive_pin_bytes,
                metadata_overhead,
            ),
        )
