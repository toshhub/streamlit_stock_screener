"""Google OIDC helpers for optional Streamlit user accounts."""

import html
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
    """Render compact sidebar account context without duplicating header actions."""
    with st.sidebar:
        st.markdown("### Account")
        if user:
            st.write(f"Signed in as **{user.name}**")
            if user.email:
                st.caption(user.email)
            if not cloud_enabled:
                st.warning("Cloud storage is not configured. Personal saves are temporarily unavailable.")
        elif auth_configured(st):
            st.caption("Guest access")
            st.caption("Use the Log in with Google button in the top banner to save favorites and alerts.")
        else:
            st.caption("Guest mode")
            st.caption("Google login becomes available after the deployment secrets are configured.")


def render_header_account_controls(st, user, cloud_enabled):
    """Render the primary login or account action inside the application banner."""
    with st.container(key="hero_account_panel"):
        if user:
            st.markdown(
                '<div class="hero-account__label">Signed in</div>'
                f'<div class="hero-account__name">{html.escape(user.name)}</div>',
                unsafe_allow_html=True,
            )
            if user.email:
                st.caption(user.email)
            if not cloud_enabled:
                st.warning("Personal cloud saves are unavailable.")
            st.button(
                "Sign out",
                key="hero_sign_out",
                on_click=st.logout,
                use_container_width=True,
            )
        elif auth_configured(st):
            st.markdown(
                '<div class="hero-account__label">Guest access</div>'
                '<div class="hero-account__name">Browsing as Guest</div>',
                unsafe_allow_html=True,
            )
            st.caption("Log in to save favorites and alerts.")
            st.button(
                "Log in with Google",
                key="hero_google_login",
                type="primary",
                on_click=st.login,
                use_container_width=True,
            )
        else:
            st.markdown(
                '<div class="hero-account__label">Guest access</div>'
                '<div class="hero-account__name">Browsing as Guest</div>',
                unsafe_allow_html=True,
            )
            st.caption("Google login is not configured.")
