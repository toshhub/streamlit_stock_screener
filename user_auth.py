"""Google OIDC helpers for optional Streamlit user accounts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppUser:
    """Stable identity values supplied by Google's signed OIDC identity token."""

    id: str
    email: str
    name: str
    picture: str = ""


def auth_configured(st):
    """Return True when the required Streamlit OIDC secrets are present."""
    try:
        auth = st.secrets.get("auth", {})
    except Exception:
        return False
    required = {"redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url"}
    return required.issubset(auth) and all(str(auth.get(key, "")).strip() for key in required)


def current_user(st):
    """Return the authenticated Google user, or None for a guest/unconfigured app."""
    if not auth_configured(st) or not hasattr(st, "user"):
        return None
    try:
        if not st.user.is_logged_in:
            return None
        claims = st.user.to_dict()
    except Exception:
        return None
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        return None
    return AppUser(
        id=subject,
        email=str(claims.get("email") or "").strip(),
        name=str(claims.get("name") or claims.get("email") or "User").strip(),
        picture=str(claims.get("picture") or "").strip(),
    )


def render_account_controls(st, user, cloud_enabled):
    """Render the login/account area without blocking guest use."""
    with st.sidebar:
        st.markdown("### Account")
        if user:
            st.write(f"Signed in as **{user.name}**")
            if user.email:
                st.caption(user.email)
            if not cloud_enabled:
                st.warning("Cloud storage is not configured. Personal saves are temporarily unavailable.")
            st.button("Sign out", on_click=st.logout, use_container_width=True)
        elif auth_configured(st):
            st.caption("Sign in to save personal filters and create price alerts.")
            st.button("Continue with Google", on_click=st.login, use_container_width=True)
        else:
            st.caption("Guest mode")
            st.caption("Google login becomes available after the deployment secrets are configured.")
