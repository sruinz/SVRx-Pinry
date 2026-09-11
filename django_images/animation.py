import warnings

from PIL import Image as PILImage


def inspect_animation(source):
    """전체 프레임 수를 세지 않고 두 번째 프레임의 존재만 확인한다."""
    position = source.tell()
    try:
        source.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(source) as image:
                if image.format not in ("GIF", "WEBP"):
                    return "static"
                try:
                    image.seek(1)
                except EOFError:
                    return "static"
                return image.format.lower()
    except (
        OSError, ValueError, SyntaxError,
        PILImage.DecompressionBombError, PILImage.DecompressionBombWarning,
    ):
        return "unreadable"
    finally:
        source.seek(position)
