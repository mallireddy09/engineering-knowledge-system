# Microsoft account setup

EKM uses delegated public-client OAuth. It never needs a client secret. Mock storage
is the default development path; ordinary tests neither sign in nor write to Microsoft.
Live Microsoft behavior has not been validated by mocked tests. Use a dedicated test
account/site before trusting a deployment with valuable records.

## Application registration

1. Register an application in the [Microsoft Entra admin center](https://entra.microsoft.com/).
   For personal and company use, select accounts in any organizational directory and
   personal Microsoft accounts. A tenant may require an administrator to register apps.
2. Under Authentication, enable public-client/device-code flows. Conditional Access
   can prohibit device-code login; do not weaken company policy to bypass it.
3. Record the Application (client) ID. Company profiles also require the directory's
   specific tenant UUID. `common` and `organizations` are intentionally rejected for
   company profile configuration.
4. Add delegated Microsoft Graph permissions as described below. MSAL handles the
   standard OpenID scopes and token renewal; no application credentials are needed.

## Personal OneDrive

The default scope is `Files.ReadWrite.AppFolder`; authentication uses the `consumers`
authority. Storage initialization explicitly approves resolving `special/approot`,
because Microsoft can create the application folder even on that GET request.
Persist the returned root item ID in the profile. Subsequent reads use this ID and
never resolve the special folder implicitly.

The actual path is Microsoft's application-owned folder under `Apps`, containing:

```text
database/tasks/               database/decisions/
database/questions/           database/debugging/
database/technical-debt/      database/projects/
database/reviews/
journals/                    reports/
dashboards/                  manifests/
```

Each record is a complete versioned JSON object named by UUID. Rebuild a local cache
by enumerating those folders; no manifest is required to recover the data. Microsoft
drive versions provide additional recovery where available. EKM never automatically
restores a historical version or overwrites a conflicting current version.

Creating records and documents uses an upload session with
`@microsoft.graph.conflictBehavior=fail`, so a concurrent creator cannot be silently
overwritten. Record updates and deletion require an exact ETag. A network failure
after a write is ambiguous: fetch the UUID again before retrying. Documents are
uploaded with unique names; existing names produce a conflict.

See [application folders](https://learn.microsoft.com/en-us/graph/onedrive-sharepoint-appfolder)
and [upload session concurrency](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0).

## Company SharePoint

Company profiles authenticate against one tenant UUID. The default delegated scope
is `Sites.Selected`. An administrator must separately grant the application suitable
access to the selected test site; consenting to the scope alone grants no site access.
The signed-in user's own site access also applies. EKM does not create site permission
grants or broaden its scopes automatically.

List provisioning may need elevated site rights and an administrator-approved
`Sites.Manage.All` delegated scope. An administrator can provision the seven lists
in advance instead. `Sites.ReadWrite.All` is a broader optional scope for tenant-approved
record/document access; it does not by itself promise list provisioning. Configure
custom delegated scopes only after approval from your tenant administrator.

Set the authorized Graph site ID, obtainable from `GET /sites/{hostname}:/{site-path}`.
The company provider creates only these lists when storage initialization is approved:

| Record kind | List |
|---|---|
| Task | EKM_Tasks |
| Decision | EKM_Decisions |
| Question | EKM_Questions |
| Investigation | EKM_Debugging |
| Technical debt | EKM_TechnicalDebt |
| Project | EKM_Projects |
| Review history | EKM_ReviewHistory |

Each list has an indexed, unique `EKMId`, multiline plain-text `EKMPayload`, indexed
`EKMProject`, and `EKMHash`, plus the standard Title. Records retain the same complete
schema and relationship UUIDs as OneDrive. The application rejects payloads exceeding
60,000 UTF-16 code units. Native SharePoint views can use the explicit metadata;
full-content search uses EKM's local index rather than assuming JSON is indexed by
SharePoint. Initialization checks existing list schemas and refuses to overwrite
unrelated/incompatible lists. A dedicated `EngineeringKnowledge` folder in the selected
document library holds exports. An optional drive ID selects a non-default library.

See [Selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview),
[create a list](https://learn.microsoft.com/en-us/graph/api/list-create?view=graph-rest-1.0),
and [conditional item updates](https://learn.microsoft.com/en-us/graph/api/listitem-update?view=graph-rest-1.0).

## Token security and account isolation

MSAL Extensions encrypts the token cache through macOS Keychain, Windows DPAPI, or
Linux libsecret. Linux needs a functioning Secret Service session and libsecret.
Headless environments without encrypted persistence fail closed; there is no plaintext
fallback. Normal tests replace the persistence implementation with a fake and never
touch the real keychain.

Every profile has a separate directory, token cache, identity binding, and local data
cache. The first successful login binds that profile to the authenticated tenant and
MSAL home-account ID. Subsequent token retrieval and login verify the binding. Logout
removes cached accounts while preserving the binding, so an existing profile cannot
be silently reassigned to another person. Create a new profile for a different account.
Switching active destinations requires explicit approval. No synchronization operation
copies records between profiles.

Bearer tokens go only to `https://graph.microsoft.com/v1.0/`. Pagination URLs are
checked against that origin. Upload/download capability URLs are sent without bearer
or client-default authorization, require HTTPS, and must use recognized Microsoft
file-transfer host suffixes. Redirects are handled explicitly. Unsupported sovereign
cloud domains fail closed and require a separately reviewed transport configuration.
Never paste capability URLs or tokens into an issue or engineering record.

## Troubleshooting and recovery

| Symptom | Action |
|---|---|
| Encrypted persistence unavailable | Unlock/configure the OS keychain or libsecret; do not save tokens as plaintext. |
| Device flow fails/expires | Verify public-client configuration, supported account types, tenant policy, and consent. |
| HTTP 401 | Sign in again to the same bound profile. |
| HTTP 403 | Ask the administrator to verify delegated consent, selected-site grants, and the user's rights. |
| HTTP 409/412 | Fetch the current record and compare versions; explicitly merge and retry with its current ETag. |
| HTTP 429/503/504 on reads | Bounded exponential backoff honors numeric Retry-After; retry later if exhausted. |
| Interrupted write | Fetch the stable UUID; compare the stored payload before retrying. Mutations are never blindly retried. |
| Partial provisioning | Run initialization again with approval; existing compatible folders/lists are reused. |
| Cache loss | Rebuild from remote JSON or complete SharePoint list payloads. |

Export JSON backups separately from the Microsoft account and protect them according
to your organization's policy. Remote deletion and local cache removal are distinct
operations; verify backups before approving either. Never run destructive integration
tests against production accounts.
