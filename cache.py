# Shared cache for user permissions
_user_cache: dict[str, tuple[bool, float]] = {}

def invalidate_user_cache(user_id: str):
    """Invalidate cache for a specific user."""
    _user_cache.pop(user_id, None)

def get_user_cache():
    """Get the cache dict."""
    return _user_cache
