"""Common media-session recovery configuration for local and cloud entry points."""
from functools import partial


def create_media_client(vpn, *, access_mode='webvpn'):
    """Use fresh authenticated sessions with bounded, progress-aware recovery."""
    from src.api.auth_recovery import fresh_media_session
    from src.api.icourse import ICourseClient
    if access_mode not in ('direct', 'webvpn'):
        raise ValueError('Invalid media access mode')
    client = ICourseClient(vpn, media_reauth_factory=partial(fresh_media_session,
        read_timeout=60, max_attempts=2, access_mode=access_mode))
    client._media_reauth_timeout = 180
    client._media_max_fresh_sessions = 3
    client._media_max_total_fresh_sessions = 8
    return client
