from django import template
from ..masking import mask_code as _mask_code, mask_email as _mask_email

register = template.Library()

@register.filter
def mask_code(value):
    return _mask_code(value)

@register.filter
def mask_email(value):
    return _mask_email(value)