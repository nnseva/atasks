"""Application properties."""

from django.apps import AppConfig
from django.utils.translation import ugettext_lazy as _


class Config(AppConfig):
    """Application config."""

    name = 'django_atasks'
    verbose_name = _('AIO Tasks')
