import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse

import joblib
import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("fastapi-risk-api")

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.abspath(os.path.join(ROOT_DIR, "..", "project ml", "models"))
DB_PATH = os.path.join(ROOT_DIR, "risk_cache.db")

FEATURE_COLUMNS = [
    "url_length",
    "num_digits",
    "num_special",
    "has_ip",
    "path_length",
    "domain_length",
    "num_subdomains",
    "has_suspicious_words",
    "entropy",
]

app = FastAPI(
    title="URL Risk ML API",
    description="A small FastAPI service that predicts URL risk with ML and caches results in SQLite.",
    version="0.1.0",
)

models = {}


class RiskRequest(BaseModel):
    url: str


class RiskResponse(BaseModel):
    url: str
    normalized_url: str
    risk_label: str
    risk_level: int
    details: dict
    cached: bool
    updated_at: str


def get_db_connection():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS url_risk_cache (
            url TEXT PRIMARY KEY,
            normalized_url TEXT NOT NULL,
            risk_label TEXT NOT NULL,
            risk_level INTEGER NOT NULL,
            details TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def init_db():
    os.makedirs(ROOT_DIR, exist_ok=True)
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS url_risk_cache (
            url TEXT PRIMARY KEY,
            normalized_url TEXT NOT NULL,
            risk_label TEXT NOT NULL,
            risk_level INTEGER NOT NULL,
            details TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def normalize_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        raise ValueError("URL cannot be empty")

    if "://" not in url:
        url = "http://" + url

    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL")

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query = parsed.query
    normalized = urlunparse((scheme, netloc, path, query, "", ""))
    return normalized


def load_models():
    global models
    logger.info("Loading ML models from %s", MODEL_DIR)
    if not os.path.isdir(MODEL_DIR):
        raise FileNotFoundError(f"Model directory not found: {MODEL_DIR}")

    models = {
        "logistic_regression": joblib.load(os.path.join(MODEL_DIR, "logistic_regression.pkl")),
        "naive_bayes": joblib.load(os.path.join(MODEL_DIR, "naive_bayes.pkl")),
        "random_forest": joblib.load(os.path.join(MODEL_DIR, "random_forest.pkl")),
        "isolation_forest": joblib.load(os.path.join(MODEL_DIR, "isolation_forest.pkl")),
    }
    logger.info("Models loaded successfully")


def is_url_alive(url: str) -> bool:
    try:
        response = requests.head(url, timeout=5, allow_redirects=True)
        return response.status_code < 400
    except requests.RequestException:
        try:
            response = requests.get(url, timeout=8, allow_redirects=True)
            return response.status_code < 400
        except requests.RequestException:
            return False


def extract_features(url: str) -> dict:
    parsed = urlparse(url)
    domain = parsed.netloc

    features = {
        "url_length": len(url),
        "num_digits": sum(c.isdigit() for c in url),
    }

    special_chars = [
        '@', '?', '-', '_', '.', '/', '=', '&', '%', '+', '$', '#', '!', '*', '(', ')',
        '[', ']', '{', '}', '|', '\\', ':', ';', '"', "'", '<', '>', ',',
    ]
    features["num_special"] = sum(url.count(char) for char in special_chars)

    hostname = domain.split(":")[0]
    try:
        import ipaddress

        ipaddress.ip_address(hostname)
        features["has_ip"] = 1
    except Exception:
        features["has_ip"] = 0

    features["path_length"] = len(parsed.path)
    features["domain_length"] = len(domain)
    features["num_subdomains"] = domain.count('.') - 1 if domain else 0

    suspicious_words = [
        "login", "verify", "secure", "account", "update", "bank", "paypal",
        "free", "win", "password",
    ]
    features["has_suspicious_words"] = int(any(word in url.lower() for word in suspicious_words))

    def entropy(s: str) -> float:
        counts = {}
        for char in s:
            counts[char] = counts.get(char, 0) + 1
        length = len(s)
        if length == 0:
            return 0.0
        return -sum((count / length) * np.log2(count / length) for count in counts.values())

    features["entropy"] = entropy(url)
    return features


def predict_risk_from_ml(url: str) -> dict:
    url = url.lower().strip()
    logger.info("Running ML prediction for %s", url)
    details = {"model_predictions": {}, "checks": {"valid": False, "anomaly": False}}

    if not is_url_alive(url):
        details["checks"]["valid"] = False
        logger.info("URL not reachable: %s", url)
        return {
            "risk_label": "SUSPICIOUS",
            "risk_level": 3,
            "details": {"reason": "URL not reachable", **details},
        }

    details["checks"]["valid"] = True
    features = extract_features(url)
    X = pd.DataFrame([features], columns=FEATURE_COLUMNS)

    pred_log = int(models["logistic_regression"].predict(X)[0])
    pred_nb = int(models["naive_bayes"].predict(X)[0])
    pred_rf = int(models["random_forest"].predict(X)[0])
    details["model_predictions"] = {
        "logistic_regression": pred_log,
        "naive_bayes": pred_nb,
        "random_forest": pred_rf,
    }

    votes = [pred_log, pred_nb, pred_rf]
    majority_vote = 1 if sum(votes) >= 2 else 0

    iso_pred = int(models["isolation_forest"].predict(X)[0])
    anomaly = 1 if iso_pred == -1 else 0
    details["checks"]["anomaly"] = anomaly

    if majority_vote == 1 or anomaly == 1:
        result = {
            "risk_label": "MALICIOUS",
            "risk_level": 2,
            "details": {
                "reason": f"ML majority {sum(votes)}/3 or anomaly {anomaly}",
                **details,
            },
        }
        logger.info("Prediction result for %s -> %s (votes=%s, anomaly=%s)", url, result["risk_label"], votes, anomaly)
        return result

    result = {
        "risk_label": "SAFE",
        "risk_level": 1,
        "details": {"reason": "No suspicious flags detected", **details},
    }
    logger.info("Prediction result for %s -> %s (votes=%s, anomaly=%s)", url, result["risk_label"], votes, anomaly)
    return result


def get_cached_result(normalized_url: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT url, normalized_url, risk_label, risk_level, details, updated_at FROM url_risk_cache WHERE normalized_url = ?",
        (normalized_url,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        logger.info("Cache miss for %s", normalized_url)
        return None
    logger.info("Cache hit for %s", normalized_url)
    return {
        "url": row[0],
        "normalized_url": row[1],
        "risk_label": row[2],
        "risk_level": int(row[3]),
        "details": json.loads(row[4]),
        "updated_at": row[5],
    }


def cache_result(url: str, normalized_url: str, risk_label: str, risk_level: int, details: dict):
    conn = get_db_connection()
    updated_at = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "INSERT OR REPLACE INTO url_risk_cache (url, normalized_url, risk_label, risk_level, details, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (url, normalized_url, risk_label, risk_level, json.dumps(details, ensure_ascii=False), updated_at),
    )
    conn.commit()
    conn.close()
    logger.info("Cached risk for %s as %s", normalized_url, risk_label)
    return updated_at


@app.on_event("startup")
def startup_event():
    init_db()
    load_models()


@app.get("/health")
def health_check():
    return {"status": "ok", "model_dir": MODEL_DIR, "database": DB_PATH}


@app.post("/predict", response_model=RiskResponse)
def predict(request: RiskRequest):
    try:
        normalized_url = normalize_url(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    cached = get_cached_result(normalized_url)
    if cached:
        logger.info("Returning cached response for %s", normalized_url)
        return {
            "url": request.url,
            "normalized_url": normalized_url,
            "risk_label": cached["risk_label"],
            "risk_level": cached["risk_level"],
            "details": cached["details"],
            "cached": True,
            "updated_at": cached["updated_at"],
        }

    prediction = predict_risk_from_ml(normalized_url)
    updated_at = cache_result(
        request.url,
        normalized_url,
        prediction["risk_label"],
        prediction["risk_level"],
        prediction["details"],
    )
    logger.info("Returning new prediction for %s", normalized_url)

    return {
        "url": request.url,
        "normalized_url": normalized_url,
        "risk_label": prediction["risk_label"],
        "risk_level": prediction["risk_level"],
        "details": prediction["details"],
        "cached": False,
        "updated_at": updated_at,
    }
