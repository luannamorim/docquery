# Entra ID — setting up a deployment

Three app registrations, in this order. Two of them are things the tenant
administrator does once; the third only matters if you ingest from SharePoint.

| # | Registration | What it is | Env var |
|---|---|---|---|
| 1 | `docquery-api` | The API. Validates tokens, never issues them. | `AZURE_CLIENT_ID` |
| 2 | `docquery-web` | The browser client. Obtains tokens for the API. | `FRONTEND_CLIENT_ID` |
| 3 | `docquery-ingest` | Reads documents from SharePoint. | `SHAREPOINT_CLIENT_*` |

Portal labels are given in English with the Portuguese in parentheses, because
the portal mixes the two depending on the account's language.

---

## 1. The API

**Microsoft Entra ID → App registrations (Registros de aplicativo) → New registration**

- **Name:** `docquery-api`
- **Supported account types:** single tenant
- **Redirect URI:** none — a resource server never receives one

Then, in the registration:

### Expose an API (Expor uma API)

Set the **Application ID URI** (the default `api://<client-id>` is fine) and add
a scope:

| Field | Value |
|---|---|
| Scope name | `access_as_user` |
| Who can consent | Admins and users |
| State | Enabled |

The name never appears in this codebase — the browser client asks for
`api://<client-id>/.default`, which means "everything already configured". It
just has to exist.

> **Without a scope the sign-in fails with `AADSTS650053`**, an error that does
> not say what is missing. This is the first thing to check when a new tenant
> will not sign in.

### App roles (Funções do aplicativo)

One per folder you want to compartmentalise, plus one for operators:

| Display name | Value | Allowed member types |
|---|---|---|
| Setor Contratos | `sector.contracts` | **Both** |
| Setor Políticas | `sector.policies` | **Both** |
| Operador de Ingestão | `docquery.admin` | **Both** |

The **Value** is what the code reads; the display name is a portal label.

A role named `sector.<folder>` grants that folder by convention — see
[RBAC](../README.md#rbac--sector-compartments). `docquery.admin` gates
`POST /ingest`, which rebuilds what everyone else reads.

> **"Both", never "Users/Groups only".** With Both, a scheduled job can hold the
> role and authenticate by client credentials — no shared API key anywhere. Pick
> Users/Groups and the only way to automate ingestion later is to invent one.

### Check the manifest

```json
"api": { "requestedAccessTokenVersion": 2 }
```

`validate_token` builds the issuer as `…/v2.0`. Version 1 tokens come from
`sts.windows.net` and **every** request would be rejected as invalid.

---

## 2. The browser client

**App registrations → New registration**

- **Name:** `docquery-web`
- **Supported account types:** single tenant
- **Redirect URI platform:** **Single-page application (SPA)**
- **Redirect URI:** the app's own origin, e.g. `http://localhost:8000`

Add a second redirect URI for the Vite dev server: `http://localhost:5173`. The
client uses `window.location.origin`, so each origin it is served from needs one.

> **The platform must be SPA, not Web.** Registered as Web, sign-in appears to
> work and then fails at the token exchange with
> `AADSTS9002326: Cross-origin token redemption is permitted only for the
> 'Single-Page Application' client-type`. Only the SPA platform enables PKCE
> with CORS on the token endpoint. Fixing it means *removing* the Web platform
> and adding the SPA one — editing the URI is not enough.

**Do not create a client secret.** This is a public client; anything shipped to
a browser is public by definition, and the code has no place to put one.

### API permissions (Permissões de API)

**Add a permission → My APIs → docquery-api → Delegated → `access_as_user`**

Then **Grant admin consent**. Optional — the scope allows user consent — but
without it every person sees a consent prompt on first sign-in.

> If **My APIs** is empty, the registration was created programmatically and you
> are not an Owner of it. Use the **APIs my organization uses** tab and search by
> name or client id, or add yourself under **Owners**.

---

## 3. Assigning roles to people

Creating a role only makes it exist. Granting it happens elsewhere:

**Enterprise applications (Aplicativos empresariais) → docquery-api → Users and
groups → Add user/group**

One entry per role — a single assignment carries one role, so a person who reads
two sectors has two entries.

> Group assignment needs Entra ID P1/P2. On the free tier you assign users
> individually, which changes how you roll this out, not whether it works.

**A role only reaches a token when that token is issued.** After assigning, the
user must sign out and back in; an existing token never gains a role. In this
app, signing out is local — it clears the MSAL cache without ending the
Microsoft session — and the next sign-in requests `prompt=select_account`, so a
fresh token is issued.

---

## 4. SharePoint ingestion (optional)

A third registration, and a **confidential** one — it reads documents rather
than identifying a caller.

- Application permission `Sites.Read.All`, or preferably `Sites.Selected` with a
  per-site grant
- A client secret, kept in `SHAREPOINT_CLIENT_SECRET`

```dotenv
INGEST_ALLOWED_SOURCE_PREFIXES='["sharepoint://<host>/sites/<site>/<drive>"]'
SHAREPOINT_TENANT_ID=
SHAREPOINT_CLIENT_ID=
SHAREPOINT_CLIENT_SECRET=
```

The allowlist is empty by default and that is deliberate: until an operator
names a location, `POST /ingest` pulls from nowhere remote, so holding
`docquery.admin` cannot be turned into "fetch any site in the tenant".

---

## Verifying

```dotenv
AUTH_ENABLED=true
AZURE_TENANT_ID=<tenant-guid>
AZURE_CLIENT_ID=<api-client-id>
FRONTEND_CLIENT_ID=<spa-client-id>
```

Sign in, then read the container log:

```
INFO  docquery.api.routes - Query authorized for sectors=['contracts', 'policies']
```

That line is the whole chain in one place: the token validated, the roles were
read, and the compartments were resolved. `sectors=none` means the token carried
no granting role — an Entra assignment problem, not a docquery one.

To check the ingestion role, call `POST /ingest`. A token without it is refused:

```
WARNING  docquery.api.auth - Ingestion refused: token lacks the docquery.admin role
```

### When it will not sign in

| Symptom | Cause |
|---|---|
| `AADSTS650053` | The API exposes no scope |
| `AADSTS9002326` | The client is registered as Web, not SPA |
| `AADSTS50011` | Redirect URI mismatch — check for a trailing slash |
| `401` from the API | Token audience or issuer wrong; check `requestedAccessTokenVersion: 2` |
| Signs in, reads nothing | No `sector.*` role assigned, or assigned but the token predates it |
