# Catalog Verifier MCP server

The MCP server exposes read-only tools to Claude Code over stdio. The Catalog
Verifier app must also be running; MCP calls its local HTTP API rather than
opening the database itself.

## Install

From the repository root, install the project's normal dependencies, including
the MCP SDK:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the virtual environment has not been created yet:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Keep using `requirements.txt` and pip for this project; the MCP SDK is listed
there alongside the app's pinned dependencies.

## Run with Claude Code

1. Start Catalog Verifier in one terminal:

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

2. From the repository root, register the server with Claude Code (replace the
   path if the repository is elsewhere):

   ```powershell
   claude mcp add catalog-verifier -- "C:\Users\KateStolbun\catalog-verifier\.venv\Scripts\python.exe" -m mcp_server.server
   ```

3. In Claude Code, run `/mcp` and confirm `catalog-verifier` is connected.
   Ask a question such as:

   > Find Amazon listings for UPC `012345678905` using Catalog Verifier. Show
   > the best candidates and their verdicts.

   Claude should call `find_asins`. Replace the example UPC with a real product
   identifier from your catalog. The app needs working SP-API credentials for
   the search to return candidates.

The server reads `CV_API_BASE` and `CATALOG_VERIFIER_API_TOKEN` from the
repository's `.env`. `CV_API_BASE` defaults to `http://localhost:8000`. If
`CATALOG_VERIFIER_API_TOKEN` is configured, it must match the token used by the
app.

## Unit tests

The MCP tests mock the HTTP API; they do not require the app or SP-API access:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests\mcp -v
```
