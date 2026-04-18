from __future__ import annotations

import os

import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.tree import DecisionTreeRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error
import joblib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_CSV = os.path.join(BASE_DIR, "dataset.csv")
MODEL_PATH = os.path.join(BASE_DIR, "model.joblib")


def main():
    df = pd.read_csv(DATASET_CSV)

    if df.empty:
        raise RuntimeError("dataset.csv is empty. Run run_benchmark.py first.")

    feature_cols = [
        "backend_index",
        "tx_rate_bps",
        "rx_rate_bps",
        "drop_delta",
        "active_flows_assigned",
        "total_tx_rate_bps",
        "total_rx_rate_bps",
        "total_active_flows",
        "tx_share",
        "rx_share",
        "active_flow_share",
        "tx_imbalance_bps",
        "rx_imbalance_bps",
        "flow_imbalance",
    ]
    target_col = "latency_sec"

    X = df[feature_cols].to_numpy()
    y = df[target_col].to_numpy()

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    candidates = {
        "decision_tree": DecisionTreeRegressor(
            random_state=42,
            max_depth=6,
            min_samples_leaf=3,
        ),
        "random_forest": RandomForestRegressor(
            n_estimators=60,
            max_depth=8,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=1,
        ),
        "ridge": Ridge(alpha=1.0),
    }

    best_name = None
    best_model = None
    best_preds = None
    best_rmse = float("inf")

    for name, model in candidates.items():
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        mse = mean_squared_error(y_test, preds)
        rmse = mse ** 0.5
        print(f"{name} RMSE: {rmse:.6f}")
        if rmse < best_rmse:
            best_name = name
            best_model = model
            best_preds = preds
            best_rmse = rmse

    mae = mean_absolute_error(y_test, best_preds)
    mse = mean_squared_error(y_test, best_preds)
    rmse = mse ** 0.5

    print("Training complete")
    print(f"Selected model: {best_name}")
    print(f"MAE:  {mae:.6f}")
    print(f"RMSE: {rmse:.6f}")

    joblib.dump(
        {
            "model": best_model,
            "feature_cols": feature_cols,
            "model_name": best_name,
        },
        MODEL_PATH,
    )
    print(f"Saved model to {MODEL_PATH}")


if __name__ == "__main__":
    main()
