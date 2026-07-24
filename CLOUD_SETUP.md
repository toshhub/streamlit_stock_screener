# Google login and Supabase setup

The application supports guest screening plus optional Google accounts.
Authenticated users get private cloud-backed favorite filters, UI settings,
and price alerts. Existing stock JSON and `favourite_filters.json` remain
shared application data.

## 1. Create the Supabase tables

Create a free Supabase project, open **SQL Editor**, and run
`supabase_schema.sql`.

The service-role key is used only by the Streamlit Python server. Do not put it
in browser code, expose it in logs, or commit it to Git. The SQL schema denies
the Supabase `anon` and `authenticated` roles direct access.

## 2. Configure Google OAuth

In Google Cloud Console:

1. Configure the OAuth consent screen.
2. Create an OAuth 2.0 **Web application** client.
3. Add `http://localhost:8501/oauth2callback` as an authorized redirect URI
   for local development.
4. For deployment, also add
   `https://YOUR-APP-HOST/oauth2callback`.

The user authenticates on Google's site. This application never receives or
stores the user's Gmail password.

## 3. Configure local secrets

Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml` and replace
the placeholders. Generate `cookie_secret` with a cryptographically secure
random value.

For Streamlit Community Cloud, enter the same TOML in the app's **Secrets**
settings and change `auth.redirect_uri` to the deployed HTTPS callback URL.

You can alternatively provide `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` as server environment variables. Google OIDC
configuration must be provided in Streamlit secrets.

## 4. Install and run

```powershell
py -m pip install -r requirements.txt
py -m streamlit run app.py
```

If auth or Supabase secrets are absent, the app continues in guest mode.
Guests can screen stocks and use shared favorites, but cannot save personal
favorites or create alerts.

## Data ownership

- Shared: stock JSON, fundamentals/cache files, existing
  `data/metadata/favourite_filters.json`.
- Personal: `user_filter_sets`, `user_settings`, and `user_alerts` in
  Supabase.
- Alert evaluation remains central: successful daily stock downloads query
  active cloud alerts for that symbol and update any triggers.
