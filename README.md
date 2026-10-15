# Acme Appliances Ledger Q&A service

Deploy as a Python web service on Render.

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- POST JSON to `/answer` (or `/`) like `{"question":"What was total revenue in the North region in March 2026?"}`
- Returns `{"answer": ...}`

Set `QUESTION_DATA_URL` to the exact personalized questionData URL if needed. The service attempts to discover JSON endpoints linked by the entrypoint and extract ledger records. Before submitting, check `/debug/source` to verify the private API was reachable and inspect discovered endpoints; adapt field mappings in `main.py` to the schema exposed by your personalized API if needed.
