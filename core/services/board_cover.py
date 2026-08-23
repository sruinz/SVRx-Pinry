from django.db.models import Q


class BoardCoverService(object):
    @staticmethod
    def eligible_pins(board, request):
        query = board.pins.select_related("image", "submitter")
        if not board.private:
            return query.filter(private=False)
        user = request.user
        if user.is_authenticated:
            return query.filter(Q(private=False) | Q(submitter=user))
        return query.filter(private=False)

    def resolve(self, board, request):
        query = self.eligible_pins(board, request)
        if board.cover_pin_id is not None:
            manual = query.filter(pk=board.cover_pin_id).first()
            if manual is not None:
                return manual, manual.pk
        return query.order_by("pk").first(), None
