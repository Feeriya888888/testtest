import pandas as pd
import numpy as np

def clean_and_transform_batch(df_prices, df_cities, df_rates, execution_date):
    if df_prices.empty: 
        return pd.DataFrame(), pd.DataFrame()
    df = df_prices.copy()
    
    invalid_metrics = (df['price'] <= 0) | (df['distance'] <= 0) | (df['total_weight'] < 0) | (df['total_volume'] < 0)
    invalid_rub = (df['currency_code'] == 643) & (df['price'] <= 1000)
    valid_cities = set(df_cities['id'].unique())
    invalid_cities = (~df['loading_city_gcid'].isin(valid_cities)) | (~df['unloading_city_gcid'].isin(valid_cities))
    
    q_mask = invalid_metrics | invalid_rub | invalid_cities
    df_q = df[q_mask].copy()
    
    reasons = []
    for _, row in df_q.iterrows():
        r = []
        if row['price'] <= 0: r.append("PRICE_LE_0")
        if row['currency_code'] == 643 and row['price'] <= 1000: r.append("RUB_PRICE_LE_1000")
        if row['distance'] <= 0: r.append("DISTANCE_LE_0")
        if row['total_weight'] < 0: r.append("WEIGHT_NEG")
        if row['total_volume'] < 0: r.append("VOLUME_NEG")
        if row['loading_city_gcid'] not in valid_cities or row['unloading_city_gcid'] not in valid_cities: r.append("CITY_NOT_FOUND")
        reasons.append(",".join(r))
        
    df_q['quarantine_reason'] = reasons
    df_q['quarantine_at'] = execution_date
    
    df_valid = df[~q_mask].copy()
    if df_valid.empty: 
        return pd.DataFrame(), df_q
    
    df_rates = df_rates.sort_values('rate_date')
    df_valid = df_valid.sort_values('created_at')
    
    df_rub = df_valid[df_valid['currency_code'] == 643].copy()
    df_rub['price_rub'] = df_rub['price']
    
    df_fx = df_valid[df_valid['currency_code'] != 643].copy()
    if not df_fx.empty:
        df_fx = pd.merge_asof(df_fx, df_rates, on='created_at', by='currency_code', direction='nearest')
        df_fx['price_rub'] = df_fx['price'] * df_fx['exchange_rate']
        
        no_rate = df_fx['price_rub'].isna()
        if no_rate.any():
            df_no_rate = df_fx[no_rate].copy()
            df_no_rate['quarantine_reason'] = "NO_EXCHANGE_RATE"
            df_no_rate['quarantine_at'] = execution_date
            df_q = pd.concat([df_q, df_no_rate.drop(columns=['exchange_rate', 'rate_date'], errors='ignore')])
            df_fx = df_fx[~no_rate]
            
        df_fx = df_fx.drop(columns=['exchange_rate', 'rate_date'], errors='ignore')
        
    df_core = pd.concat([df_rub, df_fx], ignore_index=True)
    
    w_t = (df_core['total_weight'] / 1000.0).round(1)
    df_core['weight_category'] = np.select(
        [w_t >= 30, (w_t >= 10) & (w_t < 30), (w_t >= 5) & (w_t < 10), (w_t >= 2.5) & (w_t < 5), (w_t >= 0.5) & (w_t < 2.5)],
        ['G20T', 'FTL', '10T', '5T', 'L5T'], default='OTH'
    )
    df_core['volume_category'] = np.select([(df_core['total_volume'] > 0) & (df_core['total_volume'] <= 120)], ['FTL'], default='OTH')
    
    return df_core, df_q