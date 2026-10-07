"""openIMIS settings component that adds the privacy middleware.

openIMIS assembles its settings from components selected by the MODE environment variable
(``openIMIS/settings/<MODE>.py``). To validate the module, this file is installed as
``openIMIS/settings/pbdvalidation.py`` and the assembly is started with ``MODE=pbdvalidation``,
which is also how a deployment adds the middleware: one settings component, no code changes.
"""

GRAPHENE["MIDDLEWARE"] = [*GRAPHENE["MIDDLEWARE"], "pbd.middleware.PrivacyMiddleware"]  # noqa: F821

# Validation runs without the frontend, OpenSearch, RabbitMQ or Redis.
MIDDLEWARE = [m for m in MIDDLEWARE if "csrf" not in m]  # noqa: F821
OPENSEARCH_DSL_AUTOSYNC = False
AXES_ENABLED = False
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# The Django test client sends Host: localhost.
ALLOWED_HOSTS = ["*"]
