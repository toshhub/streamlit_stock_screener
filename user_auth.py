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


def render_workspace_account_controls(st, user, cloud_enabled, workspace_key):
    """Render compact, uniquely keyed account controls in a workspace banner."""
    clean_key = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in str(workspace_key)
    )
    with st.container(key=f"workspace_account_{clean_key}"):
        if user:
            st.markdown(
                '<div class="workspace-account__label">Signed in</div>'
                f'<div class="workspace-account__name">{html.escape(user.name)}</div>'
                + (
                    f'<div class="workspace-account__email">{html.escape(user.email)}</div>'
                    if user.email
                    else ""
                ),
                unsafe_allow_html=True,
            )
            if not cloud_enabled:
                st.caption("Cloud saves unavailable")
            st.button(
                "Log out",
                key=f"workspace_sign_out_{clean_key}",
                on_click=st.logout,
                use_container_width=True,
            )
        elif auth_configured(st):
            st.markdown(
                '<div class="workspace-account__label">Guest access</div>'
                '<div class="workspace-account__name">Browsing as Guest</div>',
                unsafe_allow_html=True,
            )
            st.button(
                "Log in with Google",
                key=f"workspace_google_login_{clean_key}",
                type="primary",
                on_click=st.login,
                use_container_width=True,
            )
        else:
            st.markdown(
                '<div class="workspace-account__label">Guest access</div>'
                '<div class="workspace-account__name">Browsing as Guest</div>',
                unsafe_allow_html=True,
            )
            st.caption("Login unavailable")
