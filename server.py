"""
Expense tracking MCP server with KeyCloak authentication and Cosmos DB storage.

Run with:uvicorn server2:app --host 0.0.0.0 --port 8000
"""

import logging
import os
import uuid
import warnings
from datetime import date
from enum import Enum
from typing import Annotated
import datetime
from typing import Any

import logfire
import uvicorn
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from keycloak_provider import KeycloakAuthProvider
from rich.console import Console
from rich.logging import RichHandler
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

RUNNING_IN_PRODUCTION = os.getenv("RUNNING_IN_PRODUCTION", "false").lower() == "true"

if not RUNNING_IN_PRODUCTION:
    load_dotenv(override=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(name)s: %(message)s",
    handlers=[
        RichHandler(
            console=Console(stderr=True),
            show_path=False,
            show_level=False,
            rich_tracebacks=True,
        )
    ],
)
# Suppress OTEL 1.39 deprecation warnings and noisy logs
warnings.filterwarnings("ignore", category=DeprecationWarning, message=r".*Deprecated since version 1\.39\.0.*")
logger = logging.getLogger("TestMCP")
logger.setLevel(logging.INFO)


# Configure Keycloak authentication using KeycloakAuthProvider with DCR support
KEYCLOAK_REALM_URL = os.environ["KEYCLOAK_REALM_URL"]
if RUNNING_IN_PRODUCTION:
    keycloak_base_url = os.environ["KEYCLOAK_MCP_SERVER_BASE_URL"]
else:
    keycloak_base_url = "http://localhost:8000"

keycloak_audience = os.getenv("KEYCLOAK_MCP_SERVER_AUDIENCE") or "mcp-server"
keycloak_initial_access_token = os.getenv("KEYCLOAK_INITIAL_ACCESS_TOKEN")

auth = KeycloakAuthProvider(
    realm_url=KEYCLOAK_REALM_URL,
    base_url=keycloak_base_url,
    required_scopes=["openid", "mcp:access"],
    audience=keycloak_audience,
    initial_access_token=keycloak_initial_access_token,
)
logger.info(
    "Using Keycloak DCR auth for server %s and realm %s (audience=%s)",
    keycloak_base_url,
    KEYCLOAK_REALM_URL,
    keycloak_audience,
)


# Middleware to populate user_id in per-request context state
class UserAuthMiddleware(Middleware):
    def _get_user_id(self):
        token = get_access_token()
        if not (token and hasattr(token, "claims")):
            return None
        return token.claims.get("sub")

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        user_id = self._get_user_id()
        if context.fastmcp_context is not None:
            await context.fastmcp_context.set_state("user_id", user_id)
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        user_id = self._get_user_id()
        if context.fastmcp_context is not None:
            await context.fastmcp_context.set_state("user_id", user_id)
        return await call_next(context)


# Create the MCP server
mcp = FastMCP("Auth Calculator", auth=auth, middleware=[UserAuthMiddleware()])


@mcp.tool
async def add_numbers(a: float, b: float) -> dict[str, Any]:
    """Add two numbers together."""
    result = a + b
    return {
        "operation": "addition",
        "operand_a": a,
        "operand_b": b,
        "result": result,
        "timestamp": datetime.datetime.now().isoformat()
    }


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request):
    """Health check endpoint for service availability."""
    return JSONResponse({"status": "healthy", "service": "mcp-server"})


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def openid_configuration(_request):
    """OpenID Connect Discovery endpoint - redirects to Keycloak."""
    return JSONResponse({
        "issuer": KEYCLOAK_REALM_URL,
        "authorization_endpoint": f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/auth",
        "token_endpoint": f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/token",
        "userinfo_endpoint": f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/userinfo",
        "jwks_uri": f"{KEYCLOAK_REALM_URL}/protocol/openid-connect/certs",
        "registration_endpoint": f"{KEYCLOAK_REALM_URL}/clients-registrations/openid-connect",
        "scopes_supported": ["openid", "profile", "email", "mcp:access"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
    })


app = mcp.http_app()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    logger.info("Starting MCP server on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)



