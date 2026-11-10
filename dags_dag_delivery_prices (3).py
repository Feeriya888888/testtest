from datetime import datetime
from airflow.decorators import dag, task
from airflow.providers.postgres.hooks.postgres import PostgresHook
import pandas as pd
from etl.etl_processor import clean_and_transform_batch

@dag(
    dag_id='etl_core_dwh_delivery_prices',
    schedule_interval='@daily',
    start_date=datetime(2026, 10, 1),
    catchup=True,
    max_active_runs=1
)
def delivery_prices_pipeline():
    @task
    def process_day(ds=None):
        pg_hook = PostgresHook(postgres_conn_id='postgres_dwh')
        engine = pg_hook.get_sqlalchemy_engine()
        
        df_prices = pd.read_sql(f"SELECT * FROM stage.delivery_prices WHERE created_at::date = '{ds}'::date", con=engine)
        df_cities = pd.read_sql("SELECT id, gc_id FROM stage.cities", con=engine)
        df_rates = pd.read_sql("SELECT currency_code, rate_date, exchange_rate FROM stage.currency_rates", con=engine)
        
        df_core, df_q = clean_and_transform_batch(df_prices, df_cities, df_rates, ds)
        
        with engine.begin() as conn:
            conn.execute(f"DELETE FROM core.fact_delivery_prices WHERE created_at::date = '{ds}'::date")
            if not df_core.empty:
                df_core.to_sql('fact_delivery_prices', con=conn, schema='core', if_exists='append', index=False)
                
            conn.execute(f"DELETE FROM core.dq_quarantine_prices WHERE quarantine_at = '{ds}'::date")
            if not df_q.empty:
                df_q.to_sql('dq_quarantine_prices', con=conn, schema='core', if_exists='append', index=False)
                
    process_day()

delivery_prices_pipeline_dag = delivery_prices_pipeline()