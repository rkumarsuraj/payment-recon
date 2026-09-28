# Testing the Flow in VS Code

Three ways to test, from quickest to most thorough. Do them in this order.

---

## 0. One-time setup

**Open the project:**
```
cd setu-reconciliation
code .
```

**Install the Python extension** if you don't have it (Ctrl+Shift+X →
search "Python" → the Microsoft one). This also pulls in the debugger
(`debugpy`), which the `.vscode/launch.json` in this repo already uses.

**Create and select the virtual environment:**
- `Ctrl+Shift+P` → **"Python: Create Environment"** → Venv → select
  `requirements.txt` when it asks which dependencies to install.
- Or from the integrated terminal:
  ```bash
  python3 -m venv venv
  source venv/bin/activate      # Windows: venv\Scripts\activate
  pip install -r requirements.txt
  ```
- After creating it, `Ctrl+Shift+P` → **"Python: Select Interpreter"** →
  pick the `venv` one, if VS Code doesn't auto-select it.

**Get Postgres running.** Two options:

- **Docker (recommended):** if you have Docker Desktop, just run
  `docker compose up --build` in the integrated terminal — this starts
  both Postgres and the app together, and you can skip straight to
  §3 below.
- **Local Postgres:** if you have Postgres installed natively, create
  the database and set `DATABASE_URL`:
  ```bash
  createdb setu_db
  export DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/setu_db
  alembic upgrade head
  ```
  (Adjust the user/password to whatever your local Postgres is
  configured with. The `.vscode/launch.json` assumes `postgres:postgres`
  on `localhost:5432` — edit it if yours differs.)

---

## 1. Run and debug the server, with real breakpoints

This repo ships `.vscode/launch.json` with a config called
**"FastAPI: uvicorn (debug, breakpoints work)"**.

1. Open the **Run and Debug** panel (`Ctrl+Shift+D`).
2. Pick that config from the dropdown at the top.
3. Set a breakpoint — click in the gutter to the left of a line number.
   Good first ones to try:
   - `app/services/ingestion.py`, the line calling `db.execute(_INSERT_EVENTS_SQL, params)` — see exactly what gets sent to Postgres on ingestion.
   - `app/api/transactions.py`, inside `get_transaction` — step through how the detail response gets assembled.
4. Press `F5`. The server starts inside VS Code, and `--reload` is on, so
   editing a file restarts it automatically.
5. Hit an endpoint (see §2 or §3 below) that would pass through your
   breakpoint — execution pauses there, and you get the normal VS Code
   debugging experience: inspect variables, step line by line, view the
   call stack.

This is the single best way to actually understand the ingestion flow
before an interview — set a breakpoint at the start of `ingest_events`,
send a duplicate event, and watch `inserted_ids` come back empty.

---

## 2. Fire requests without leaving the editor (`requests.http`)

This repo ships `requests.http` at the root — a plain text file where
every line starting with `###` is a labeled request you can run
individually.

1. Install the **REST Client** extension (`humao.rest-client` — search
   for "REST Client" in the extensions panel).
2. Open `requests.http`.
3. Above any request block, a small **"Send Request"** link appears
   inline. Click it.
4. The response opens in a split pane — status code, headers, JSON body,
   all inside VS Code.

It's ordered to match a real walkthrough: health check → ingest a single
event → replay it (idempotency proof) → ingest a full lifecycle → list
transactions → get transaction detail → summary → discrepancies → a
couple of intentional error cases (400/404/422). Run them top to bottom
once, and you've exercised the whole system.

---

## 3. Run the actual test suite (the strongest form of "testing the flow")

**Set up the test database once:**
```bash
createdb setu_test
export DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/setu_test
alembic upgrade head
```

**Option A — VS Code's Test Explorer (visual):**
1. Open the **Testing** panel (flask icon in the left sidebar, or
   `Ctrl+Shift+P` → "Testing: Focus on Test Explorer View").
2. If it's the first time, VS Code will ask to configure a test
   framework — pick **pytest**, and point it at the `tests/` folder
   (already set in `.vscode/settings.json`, so this may happen
   automatically).
3. Click the ▶ next to any test, file, or the whole suite. Green
   checkmarks appear next to what passes; red X's next to what fails,
   with the failure shown inline.
4. Right-click any individual test → **"Debug Test"** to step through it
   with breakpoints — genuinely useful for understanding
   `test_projection_rebuild_matches_incremental` or the discrepancy
   boundary tests.

**Option B — the integrated terminal:**
```bash
pytest tests/ -v
```

Either way, you're looking for **48 passed**. If Postgres isn't reachable
or the test DB doesn't have the schema migrated, you'll see connection
errors — re-check the `createdb`/`alembic upgrade head` steps above.

---

## 4. Load the real sample data and watch the numbers line up

With the server running (§1) in one terminal, open a second **integrated
terminal** (`` Ctrl+` `` then the `+` icon) and run:

```bash
python scripts/load_sample_data.py --base-url http://localhost:8000 \
    --file sample_events_augmented.json --batch-size 500
```

You should see `inserted: 10290, duplicates: 190` on the first run, and
`inserted: 0, duplicates: 10480` if you run it a second time — that's the
idempotency proof, live.

Then, back in `requests.http`, run the discrepancies request (§2, block
5a) and confirm `counts_by_type` matches:
`PROCESSED_NOT_SETTLED: 380, SETTLED_AFTER_FAILURE: 95, DOUBLE_SETTLEMENT: 95,
AMOUNT_MISMATCH: 80, ORPHAN_SETTLEMENT: 25, STUCK_INITIATED: 190`.

---

## Quick troubleshooting

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError` on `import app...` | Wrong interpreter selected — `Ctrl+Shift+P` → "Python: Select Interpreter" → pick the `venv` one |
| `could not connect to server` / connection refused | Postgres isn't running, or `DATABASE_URL` points somewhere wrong — check `launch.json`'s `env` block matches your actual Postgres setup |
| `relation "transactions" does not exist` | Forgot `alembic upgrade head` against that database |
| REST Client requests all fail the same way | Server isn't actually running — check the Debug Console / integrated terminal for the uvicorn startup log |
| Breakpoints never hit | Make sure you launched via the **debug config** (`F5` with "FastAPI: uvicorn" selected), not by running `uvicorn` directly in a plain terminal — a plain run isn't attached to the debugger |
