# FastAPI URL Risk Service

This service exposes a simple ML-backed URL risk prediction API with SQLite caching.

## Structure
- `app.py` - FastAPI application
- `risk_cache.db` - SQLite cache file created automatically
- `requirements.txt` - dependencies

## How it works
1. `POST /predict` receives a URL.
2. service normalizes the URL.
3. if the URL already exists in cache, it returns the stored result.
4. otherwise it loads the ML models from `../project ml/models`, predicts risk, stores the result, and returns it.

## Run
```bash
cd "fastapi-risk-api"
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

## Example request
```bash
curl -X POST "http://localhost:8000/predict" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```
