import requests
from requests.auth import HTTPBasicAuth


def verify_ado_connection(org: str, project: str, auth: HTTPBasicAuth) -> None:
    """Verify PAT, org, project, and required scopes before any data download.

    Raises RuntimeError with a descriptive message on failure.
    """
    # Step 1: Check PAT + org by hitting the org-level projects list
    org_url = f"https://dev.azure.com/{org}/_apis/projects?$top=1&api-version=7.0"
    try:
        resp = requests.get(org_url, auth=auth, timeout=15)
    except requests.ConnectionError:
        raise RuntimeError(
            f"Org/project URL is wrong or inaccessible: cannot reach dev.azure.com/{org}"
        )

    if resp.status_code == 401:
        raise RuntimeError(
            "Authentication failed: bad / expired / revoked / malformed PAT, or bad auth formatting"
        )
    if resp.status_code == 403:
        raise RuntimeError(
            "Authentication succeeded. PAT lacks required project permissions (need Work Items Read)"
        )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Org '{org}' or project '{project}' is wrong or inaccessible"
        )
    resp.raise_for_status()

    # Step 2: Check that the project exists within the org
    project_url = f"https://dev.azure.com/{org}/_apis/projects/{project}?api-version=7.0"
    resp = requests.get(project_url, auth=auth, timeout=15)

    if resp.status_code == 404:
        raise RuntimeError(
            f"Org '{org}' or project '{project}' not found"
        )
    resp.raise_for_status()

    # Step 3: Check work-item read scope by running a minimal WIQL query
    wiql_url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0"
    resp = requests.post(
        wiql_url,
        json={"query": f"SELECT [System.Id] FROM workitems WHERE [System.TeamProject] = '{project}' AND [System.Id] = 0"},
        auth=auth,
        timeout=15,
    )

    if resp.status_code == 401:
        raise RuntimeError(
            "Authentication failed: bad / expired / revoked / malformed PAT, or bad auth formatting"
        )
    if resp.status_code == 403:
        raise RuntimeError(
            "Authentication succeeded. PAT lacks required project permissions (need at Work Items Read)"
        )
    resp.raise_for_status()

    # Step 4: Check Analytics OData access
    analytics_url = (
        f"https://analytics.dev.azure.com/{org}/{project}"
        f"/_odata/v3.0-preview/Projects?$top=1&$select=ProjectName"
    )
    resp = requests.get(analytics_url, auth=auth, timeout=15)

    if resp.status_code == 401:
        raise RuntimeError(
            "Authentication failed: bad / expired / revoked / malformed PAT, or bad auth formatting"
        )
    if resp.status_code == 403:
        raise RuntimeError(
            "PAT is valid but lacks required scope (need Analytics Read)"
        )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Analytics endpoint not available for '{org}/{project}'"
        )
    resp.raise_for_status()

    print(f"PAT is valid and works. Connected to {org}/{project}.")
