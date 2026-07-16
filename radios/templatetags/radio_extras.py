from django import template

register = template.Library()


@register.filter
def dictget(dictionary, key):
    """Template filter to safely access dict keys.

    Usage: ``{{ my_dict|dictget:key_name }}``
    Returns the value for *key* from *dictionary*, or ``None`` if missing.
    """
    return dictionary.get(key)
