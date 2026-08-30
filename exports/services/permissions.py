from core.models import Pin
from exports.contracts import StopRequested


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


def _chunk_size(value):
    if type(value) is not int or value <= 0:
        raise ValueError("chunk_size_must_be_positive")
    return min(value, QUERY_CHUNK_SIZE)


def _live_pins(items, using):
    pin_ids = [item.pin_id for item in items]
    return {
        pin_id: (submitter_id, private, published)
        for pin_id, submitter_id, private, published in
        Pin.objects.using(using).filter(pk__in=pin_ids).values_list(
            "pk", "submitter_id", "private", "published"
        )
    }


def iter_visible_foreign_pin_ids(items, user_id, chunk_size=400):
    foreign_items = (
        item for item in items if item.pin_owner_id != user_id
    )
    for item_chunk in _chunks(foreign_items, _chunk_size(chunk_size)):
        live = _live_pins(item_chunk, "default")
        yield {
            item.pin_id for item in item_chunk
            if live.get(item.pin_id) == (
                item.pin_owner_id,
                False,
                item.published_at,
            )
        }


def _heartbeat(heartbeat):
    if heartbeat is None:
        return
    if callable(heartbeat):
        heartbeat()
        return
    pulse = getattr(heartbeat, "pulse", None)
    if callable(pulse):
        pulse()


def revoked_item_ids(
    job,
    inclusion_state="included",
    using="default",
    heartbeat=None,
    stop_requested=None,
    checkpoint=None,
    lease=None,
):
    items = job.items.using(using).filter(
        inclusion_state=inclusion_state,
    ).order_by("target_position", "pk")
    revoked = set()
    for item_chunk in _chunks(items.iterator(), QUERY_CHUNK_SIZE):
        live = _live_pins(item_chunk, using)
        for item in item_chunk:
            current = live.get(item.pin_id)
            if item.pin_owner_id != job.owner_id:
                if current != (
                    item.pin_owner_id,
                    False,
                    item.published_at,
                ):
                    revoked.add(item.pk)
                continue
            if (
                current is not None
                and current[2] == item.published_at
                and current[0] != job.owner_id
            ):
                revoked.add(item.pk)
        if stop_requested is not None and stop_requested():
            raise StopRequested(lease)
        _heartbeat(heartbeat)
        if checkpoint is not None:
            checkpoint()
    return revoked


__all__ = (
    "iter_visible_foreign_pin_ids",
    "revoked_item_ids",
)
