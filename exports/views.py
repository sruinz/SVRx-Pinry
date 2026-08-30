from django.utils import timezone
from rest_framework.exceptions import (
    ParseError,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from exports.serializers import ExportJSONParser, ExportRequestSerializer
from exports.services.jobs import JobService
from exports.services.targeting import ExportRequestError, TargetingService


def _validated_request(request):
    serializer = ExportRequestSerializer(data=request.data)
    if not serializer.is_valid():
        raise ExportRequestError("invalid_target", 400)
    return serializer.validated_data


class ExportAPIView(APIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (ExportJSONParser,)

    def handle_exception(self, error):
        if isinstance(
            error,
            (ParseError, UnsupportedMediaType, ValidationError),
        ):
            return Response({"code": "invalid_target"}, status=400)
        return super(ExportAPIView, self).handle_exception(error)


class ExportPreviewView(ExportAPIView):
    def post(self, request):
        try:
            request_data = _validated_request(request)
            preview = TargetingService().preview(
                request.user,
                request_data,
                timezone.now(),
            )
        except ExportRequestError as error:
            return Response(
                {"code": error.code},
                status=error.status_code,
            )
        return Response(preview.as_dict(), status=200)


class ExportCreateView(ExportAPIView):
    def post(self, request):
        try:
            request_data = _validated_request(request)
            job = JobService().create(
                request.user,
                request_data,
                timezone.now(),
            )
        except ExportRequestError as error:
            return Response(
                {"code": error.code},
                status=error.status_code,
            )
        return Response({
            "schema_version": 1,
            "id": str(job.pk),
            "state": job.state,
            "scope": job.scope,
            "requested_total": job.requested_total,
            "target_total": job.target_total,
            "excluded_total": job.excluded_total,
            "status_url": "/api/v2/exports/{}/".format(job.pk),
        }, status=202)
