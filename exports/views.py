from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import (
    ParseError,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from exports.contracts import (
    ExportExpired,
    ExportNotFound,
    ExportNotReady,
    ExportTemporarilyUnavailable,
)
from exports.serializers import ExportJSONParser, ExportRequestSerializer
from exports.models import ExportJob
from exports.services.download import authorize_download, content_disposition
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


class ExportLatestView(ExportAPIView):
    def get(self, request):
        return Response(
            JobService.latest_for(request.user, timezone.now()),
            status=200,
        )


class ExportStatusView(ExportAPIView):
    def get(self, request, job_uuid):
        job = get_object_or_404(
            ExportJob.objects.filter(owner_id=request.user.pk),
            pk=job_uuid,
        )
        return Response(
            JobService.status_for(job, timezone.now()),
            status=200,
        )


class ExportDownloadView(ExportAPIView):
    def get(self, request, job_uuid):
        try:
            job = authorize_download(
                job_uuid,
                request.user.pk,
                timezone.now,
            )
        except ExportNotFound:
            raise Http404()
        except ExportNotReady:
            return Response({"code": "export_not_ready"}, status=409)
        except ExportExpired:
            return Response({"code": "export_expired"}, status=410)
        except ExportTemporarilyUnavailable:
            return Response(
                {"code": "export_temporarily_unavailable"},
                status=503,
            )
        response = HttpResponse(content_type="application/zip")
        response["X-Accel-Redirect"] = (
            "/__protected_exports/{}.zip".format(job.pk)
        )
        response["Cache-Control"] = "private, no-store"
        response["Content-Disposition"] = content_disposition(job)
        return response
