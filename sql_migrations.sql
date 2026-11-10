-- DDL миграции аналитического хранилища (Core DWH & Mart Layer)
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

-- 1. Таблица фактов Core DWH (Очищенные и нормализованные данные)
CREATE TABLE IF NOT EXISTS core.fact_delivery_prices (
    id INT,
    price_rub NUMERIC(15, 2),
    distance NUMERIC(10, 2),
    total_weight NUMERIC(12, 2),
    total_volume NUMERIC(10, 2),
    weight_category VARCHAR(10),
    volume_category VARCHAR(10),
    loading_city_gcid INT,
    unloading_city_gcid INT,
    is_fact BOOLEAN,
    price_type INT,
    complex BOOLEAN,
    outlier_mark BOOLEAN,
    organization_id INT,
    created_at TIMESTAMP,
    loading_date DATE
);

-- 2. Таблица карантина (Data Quality Quarantine Log)
CREATE TABLE IF NOT EXISTS core.dq_quarantine_prices (
    id INT,
    price NUMERIC(15, 2),
    currency_code INT,
    loading_city_gcid INT,
    unloading_city_gcid INT,
    quarantine_reason TEXT,
    quarantine_at DATE
);

-- 3. Аналитическая витрина: Карточка маршрута и рыночный коридор ставок
CREATE TABLE IF NOT EXISTS mart.mart_market_corridor (
    snapshot_month DATE,
    loading_city_gcid INT,
    unloading_city_gcid INT,
    body_group_std_id INT,
    weight_category VARCHAR(10),
    volume_category VARCHAR(10),
    p25_trip NUMERIC(15, 2),
    median_trip NUMERIC(15, 2),
    p75_trip NUMERIC(15, 2),
    avg_trip NUMERIC(15, 2),
    p25_km NUMERIC(15, 4),
    median_km NUMERIC(15, 4),
    p75_km NUMERIC(15, 4),
    trips_count INT,
    carriers_count INT,
    cv_rate NUMERIC(8, 4),
    mom_pct NUMERIC(8, 2),
    yoy_pct NUMERIC(8, 2),
    reliability_level VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category)
);

-- 4. Таблица результатов ML тарификации (Batch Inference Predictions)
CREATE TABLE IF NOT EXISTS mart.ml_tariff_predictions (
    price_id INT PRIMARY KEY,
    predicted_price_rub NUMERIC(15, 2) NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    scored_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 5. Представление дедуплицированного справочника городов с парсингом регионов
CREATE OR REPLACE VIEW mart.dim_cities_deduplicated AS
SELECT DISTINCT ON (gc_id)
    id AS city_id,
    gc_id,
    name,
    TRIM(SUBSTRING(full_name_local FROM ', (.*)$')) AS region_name,
    full_name_local
FROM stage.cities
ORDER BY gc_id, id ASC;

-- 6. Хранимая процедура инкрементального пересчета и наполнения витрины
CREATE OR REPLACE PROCEDURE mart.refresh_market_corridor()
LANGUAGE plpgsql AS $$
BEGIN
    -- Очистка и расчет базовых метрик перцентилей рыночного коридора
    INSERT INTO mart.mart_market_corridor (
        snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category, volume_category,
        p25_trip, median_trip, p75_trip, avg_trip, p25_km, median_km, p75_km, trips_count, carriers_count, cv_rate, reliability_level
    )
    WITH filtered_facts AS (
        SELECT 
            DATE_TRUNC('month', pd.price_date)::DATE AS snapshot_month, 
            f.loading_city_gcid, 
            f.unloading_city_gcid,
            CASE 
                WHEN pbt.body_type_id IN (56,57,58,59,60,61) THEN 56 
                WHEN pbt.body_type_id IN (4,46,47,48) THEN 4 
                WHEN pbt.body_type_id = 2 THEN 2 
                WHEN pbt.body_type_id IN (1,15) THEN 15 
                ELSE 999 
            END AS body_group_std_id,
            f.weight_category, 
            f.volume_category, 
            f.price_rub, 
            (f.price_rub / NULLIF(f.distance, 0)) AS price_km, 
            f.organization_id, 
            f.price_type
        FROM core.fact_delivery_prices f
        INNER JOIN stage.price_dates pd ON f.id = pd.price_id
        LEFT JOIN stage.price_body_types pbt ON f.id = pbt.price_id
        WHERE f.is_fact = TRUE 
          AND f.price_type IN (1,2,3,4,5,9) 
          AND f.complex = FALSE 
          AND f.distance > 0 
          AND f.outlier_mark = FALSE 
          AND pd.price_date <= '2026-10-26'::DATE
    ),
    aggregated_metrics AS (
        SELECT 
            snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category, 
            MAX(volume_category) AS volume_category,
            PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY price_rub) AS p25_trip, 
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY price_rub) AS median_trip, 
            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY price_rub) AS p75_trip, 
            AVG(price_rub) AS avg_trip,
            PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY price_km) AS p25_km, 
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY price_km) AS median_km, 
            PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY price_km) AS p75_km,
            COUNT(*) AS trips_count, 
            COUNT(DISTINCT CASE WHEN price_type IN (3,9) THEN organization_id END) AS carriers_count,
            CASE WHEN COUNT(*) >= 2 AND AVG(price_rub) > 0 THEN STDDEV_SAMP(price_rub) / AVG(price_rub) ELSE NULL END AS cv_rate
        FROM filtered_facts
        GROUP BY snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category
    )
    SELECT *, 
        CASE 
            WHEN trips_count >= 30 AND carriers_count >= 5 THEN 'HIGH' 
            WHEN trips_count >= 10 THEN 'MEDIUM' 
            ELSE 'LOW_DATA' 
        END AS reliability_level 
    FROM aggregated_metrics
    ON CONFLICT (snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category) DO UPDATE SET
        p25_trip = EXCLUDED.p25_trip, 
        median_trip = EXCLUDED.median_trip, 
        p75_trip = EXCLUDED.p75_trip, 
        avg_trip = EXCLUDED.avg_trip,
        p25_km = EXCLUDED.p25_km, 
        median_km = EXCLUDED.median_km, 
        p75_km = EXCLUDED.p75_km, 
        trips_count = EXCLUDED.trips_count,
        carriers_count = EXCLUDED.carriers_count, 
        cv_rate = EXCLUDED.cv_rate, 
        reliability_level = EXCLUDED.reliability_level;

    -- Вторым шагом рассчитываем скользящие окна смещений цен MoM% и YoY%
    WITH dynamic_calculated AS (
        SELECT 
            snapshot_month, loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category,
            LAG(median_trip, 1) OVER(PARTITION BY loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category ORDER BY snapshot_month) as prev_m,
            LAG(median_trip, 12) OVER(PARTITION BY loading_city_gcid, unloading_city_gcid, body_group_std_id, weight_category ORDER BY snapshot_month) as prev_y
        FROM mart.mart_market_corridor
    )
    UPDATE mart.mart_market_corridor m
    SET 
        mom_pct = CASE WHEN d.prev_m > 0 THEN ((m.median_trip - d.prev_m) / d.prev_m) * 100.0 ELSE NULL END,
        yoy_pct = CASE WHEN d.prev_y > 0 THEN ((m.median_trip - d.prev_y) / d.prev_y) * 100.0 ELSE NULL END
    FROM dynamic_calculated d
    WHERE m.snapshot_month = d.snapshot_month 
      AND m.loading_city_gcid = d.loading_city_gcid 
      AND m.unloading_city_gcid = d.unloading_city_gcid 
      AND m.body_group_std_id = d.body_group_std_id 
      AND m.weight_category = d.weight_category;
END;
$$;