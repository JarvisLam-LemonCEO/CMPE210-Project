from __future__ import annotations

import os

import pandas as pd

# Machine Learning models
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.tree import DecisionTreeRegressor
# utility functions for splitting dataset and evaluating models
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error
import joblib

# Get the directory where this Python file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Path to the training dataset
DATASET_CSV = os.path.join(BASE_DIR, "dataset.csv")
# Path where the trained model will be saved
MODEL_PATH = os.path.join(BASE_DIR, "model.joblib")


def main():
    # Load Dataset
    # Read dataset.csv into a pandas DataFrame
    df = pd.read_csv(DATASET_CSV)
    
    # Stop execution if dataset is empty
    if df.empty:
        raise RuntimeError("dataset.csv is empty. Run run_benchmark.py first.")
   
    # Define Features and Target
    # Input features used by the ML model
    # These values describe backend/server network conditions
    feature_cols = [
        # Server ID/index
        "backend_index",           # Server ID/index
        "tx_rate_bps",             # Current transmit rate
        "rx_rate_bps",             # Current receive rate
        "drop_delta",              # Packet drops
        "active_flows_assigned",   # Number of flows assigned to backend

        # Global traffic statistics
        "total_tx_rate_bps",
        "total_rx_rate_bps",
        "total_active_flows",

        # Relative backend utilization
        "tx_share",
        "rx_share",
        "active_flow_share",

        # Backend imbalance metrics
        "tx_imbalance_bps",
        "rx_imbalance_bps",
        "flow_imbalance",
    ]
    
    # Output label the model tries to predict
    # In this project we predict network latency
    target_col = "latency_sec"

    # Extract feature matrix (X) and target vector (y)
    X = df[feature_cols].to_numpy()
    y = df[target_col].to_numpy()

    # Split data into
    # 80% training data
    # 20% testing data
    #
    # random_state=42 ensures reproducible results
    X_train, X_test, y_train, y_test = train_test_split(
        X, 
        y, 
        test_size=0.2, 
        random_state=42
    )

    # Define Candidate ML Models
    # We test multiple regression models and select the best one automatically
    candidates = {
        # Decision Tree Regressor
        "decision_tree": DecisionTreeRegressor(
            random_state=42,
            max_depth=6,
            min_samples_leaf=3,
        ),
        # Random Forest Regressor
        # Ensemble of multiple decision trees
        "random_forest": RandomForestRegressor(
            n_estimators=60,
            max_depth=8,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=1,
        ),
        # Ridge Regression
        # Linear regression with L2 regularization
        "ridge": Ridge(alpha=1.0),
    }

    # Model Selection Variables
    best_name = None
    best_model = None
    best_preds = None
    
    # Start with infinitely large RMSE
    # so the first model automatically becomes the best initially
    best_rmse = float("inf")

    # Train and Evaluate Each Model
    for name, model in candidates.items():
        # Train the model using training data
        model.fit(X_train, y_train)
        # Predict latency using testing data
        preds = model.predict(X_test)
        # Compute Mean Squared Error (MSE)
        mse = mean_squared_error(y_test, preds)
        # Compute Root Mean Squared Error (RMSE)
        rmse = mse ** 0.5
        
        # Print model performance
        print(f"{name} RMSE: {rmse:.6f}")
        if rmse < best_rmse:
            best_name = name
            best_model = model
            best_preds = preds
            best_rmse = rmse
            
    # Final Evaluation of Best Model
    # Mean Absolute Error
    mae = mean_absolute_error(y_test, best_preds)
    # Mean Squared Error
    mse = mean_squared_error(y_test, best_preds)
    # Root Mean Squared Error
    rmse = mse ** 0.5

    # Print final results
    print("Training complete")
    print(f"Selected model: {best_name}")
    print(f"MAE:  {mae:.6f}")
    print(f"RMSE: {rmse:.6f}")
   
    # Save Trained Model
    # Save:
    # - trained ML model
    # - feature column names
    # - selected model name
    
    # This file will later be loaded by the SDN controller
    # for real-time backend prediction
    joblib.dump(
        {
            "model": best_model,
            "feature_cols": feature_cols,
            "model_name": best_name,
        },
        MODEL_PATH,
    )

    # Print save location
    print(f"Saved model to {MODEL_PATH}")

if __name__ == "__main__":
    main()
