from django_images.paths import asset_upload_to


def upload_path(instance, filename, **kwargs):
    return asset_upload_to(instance, filename)
