import os


# Please don't change following settings unless you know what you are doing
STATIC_ROOT = '/data/static'

MEDIA_ROOT = os.path.join(STATIC_ROOT, 'media')

# SECURITY WARNING: keep the secret key used in production secret!
# Or just write your own secret-key here instead of using a env-variable
SECRET_KEY = "secret_key_place_holder"

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False
TEMPLATE_DEBUG = DEBUG

# SECURITY WARNING: use your actual domain name in production!
ALLOWED_HOSTS = ['*']

# Database
# https://docs.djangoproject.com/en/1.10/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': '/data/production.db',
    }
}

# Allow users to register by themselves
ALLOW_NEW_REGISTRATIONS = True

# Delete image files once you remove your pin
IMAGE_AUTO_DELETE = True

# thumbnail size control
IMAGE_SIZES = {
    'thumbnail': {'size': [240, 0]},
    'standard': {'size': [600, 0]},
    'square': {'crop': True, 'size': [125, 125]},
}

# Safe server-side image fetching and batch import limits
PINRY_FETCH_MAX_BYTES = 25 * 1024 * 1024
PINRY_FETCH_CONNECT_TIMEOUT = 3
PINRY_FETCH_READ_TIMEOUT = 8
PINRY_FETCH_TOTAL_TIMEOUT = 12
PINRY_FETCH_MAX_REDIRECTS = 3
PINRY_FETCH_MAX_PIXELS = 100000000
PINRY_FETCH_PRIVATE_ALLOWLIST = []
PINRY_BATCH_DEADLINE_SECONDS = 45
PINRY_BATCH_RESULT_RESERVE_SECONDS = 3
PINRY_BATCH_MAX_ITEMS = 10
PINRY_BATCH_MAX_BODY_BYTES = 1024 * 1024
PINRY_IMPORT_LEASE_SECONDS = 300

# Whether people can view pins without login
PUBLIC = True

ENABLED_PLUGINS = [
    'pinry_plugins.batteries.plugin_example.Plugin',
]
