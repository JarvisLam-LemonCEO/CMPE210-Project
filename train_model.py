import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
import joblib

DATASET = "dataset.csv"
OUT = "model.joblib"

def main():
    df = pd.read_csv(DATASET)

    # Features must match controller’s feature vector order:
    # [tx_rate_bps, rx_rate_bps, drop_delta, active]
    X = df[["tx_rate_bps", "rx_rate_bps", "drop_delta", "active"]]
    y = df["latency_sec"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42)

    model = RandomForestRegressor(
        n_estimators=200,
        random_state=42,
        min_samples_leaf=2
    )
    model.fit(X_train, y_train)

    score = model.score(X_test, y_test)
    print("R^2 on test:", score)

    joblib.dump(model, OUT)
    print("Saved:", OUT)

if __name__ == "__main__":
    main()