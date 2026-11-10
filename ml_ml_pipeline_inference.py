import os
import pandas as pd
import numpy as np
from category_encoders import TargetEncoder
import lightgbm as lgb
from sqlalchemy import create_engine

def run_batch_inference(db_uri, execution_date):
    engine = create_engine(db_uri)
    
    query_train = """
        SELECT 
            f.price_rub AS target, f.distance, f.total_weight, f.total_volume, f.weight_category, f.volume_category,
            c_load.region_name AS source_region, c_unload.region_name AS target_region, COALESCE(pbt.body_type_id, 0) AS body_type
        FROM core.fact_delivery_prices f
        INNER JOIN mart.dim_cities_deduplicated c_load ON f.loading_city_gcid = c_load.gc_id
        INNER JOIN mart.dim_cities_deduplicated c_unload ON f.unloading_city_gcid = c_unload.gc_id
        LEFT JOIN stage.price_body_types pbt ON f.id = pbt.price_id
        WHERE f.loading_date < '2026-10-26' AND f.price_rub IS NOT NULL
    """
    
    query_score = f"""
        SELECT 
            f.id AS price_id, f.distance, f.total_weight, f.total_volume, f.weight_category, f.volume_category,
            c_load.region_name AS source_region, c_unload.region_name AS target_region, COALESCE(pbt.body_type_id, 0) AS body_type
        FROM core.fact_delivery_prices f
        INNER JOIN mart.dim_cities_deduplicated c_load ON f.loading_city_gcid = c_load.gc_id
        INNER JOIN mart.dim_cities_deduplicated c_unload ON f.unloading_city_gcid = c_unload.gc_id
        LEFT JOIN stage.price_body_types pbt ON f.id = pbt.price_id
        WHERE f.loading_date = '{execution_date}'::date
    """
    
    df_train = pd.read_sql(query_train, con=engine)
    df_score = pd.read_sql(query_score, con=engine)
    
    if df_score.empty:
        print(f"No transactions found for date: {execution_date}")
        return
        
    features = ['distance', 'total_weight', 'total_volume', 'weight_category', 'volume_category', 'source_region', 'target_region', 'body_type']
    cat_cols = ['weight_category', 'volume_category', 'body_type']
    
    te = TargetEncoder(cols=['source_region', 'target_region'])
    df_train[['source_region', 'target_region']] = te.fit_transform(df_train[['source_region', 'target_region']], df_train['target'])
    df_score[['source_region', 'target_region']] = te.transform(df_score[['source_region', 'target_region']])
    
    for c in cat_cols:
        df_train[c] = df_train[c].astype('category')
        df_score[c] = df_score[c].astype('category')
        
    X_train, y_train = df_train[features], df_train['target']
    X_score = df_score[features]
    
    model = lgb.LGBMRegressor(objective='regression', n_estimators=200, learning_rate=0.05, random_state=42, verbose=-1)
    model.fit(X_train, y_train)
    
    preds = model.predict(X_score)
    
    df_results = pd.DataFrame({
        'price_id': df_score['price_id'],
        'predicted_price_rub': np.round(preds, 2),
        'model_version': 'lgb_regressor_v1.0',
        'scored_at': pd.Timestamp.now()
    })
    
    with engine.begin() as conn:
        conn.execute("CREATE TEMP TABLE temp_scoring AS SELECT * FROM mart.ml_tariff_predictions WHERE 1=0")
        df_results.to_sql('temp_scoring', con=conn, if_exists='append', index=False)
        conn.execute("""
            INSERT INTO mart.ml_tariff_predictions (price_id, predicted_price_rub, model_version, scored_at)
            SELECT price_id, predicted_price_rub, model_version, scored_at FROM temp_scoring
            ON CONFLICT (price_id) DO UPDATE SET 
                predicted_price_rub = EXCLUDED.predicted_price_rub, 
                scored_at = EXCLUDED.scored_at;
        """)
    print(f"Successfully scored {len(df_results)} rows.")

if __name__ == "__main__":
    DB_URI = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/dwh")
    run_batch_inference(DB_URI, "2026-10-26")
