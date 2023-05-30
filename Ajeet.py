import pendulum
import pandas as pd
from io import StringIO
from typing import List
from airflow.decorators import dag, task
from airflow.providers.snowflake.operators.snowflake import SnowflakeOperator
from data_transformations.lib import optimove
from gala.utils import args, config, callbacks
from gala.operators.http_to_s3 import HttpToS3Operator
doc_md = """

# Optimove S3 to Snowflake DAG
#### Runbook
If any of the tasks fail, you can retry them and if they are still failing open a support ticket with Optimove [here](https://optimoveww.zendesk.com/hc/en-us/requests).
#### Operation
1. Call Optimove login API to get the authentication token
2. Pass the token to the required Optimove APIs and store the data as CSV in S3 bucket
3. Load the data from S3 to snowflake stage & tables
"""
OPTIMOVE_TOKEN_TASK_ID = "get_api_token"

EXTRACT_URLS = {
    "unsubscribers_list": {
        "url": "https://api4.optimove.net/current/optimail/Getunsubscribers?BrandID=10"
    },
    "GetExecutedCampaignDetails":{
	"url":"https://apiX.optimove.net/current/actions/GetExecutedCampaignDetails"
    },
    "GetExecutionChannels":{
	"url":"https://apiX.optimove.net/current/actions/GetExecutionChannels"
}
}


Field_Data={
	"GetExecutedCampaignDetails":"CampaignID,TargetGroupID,CampaignType,Duration,LeadTime,Notes,IsMultiChannel,IsRecurrence,Status,string,Error"
	"GetExecutionChannels":"ChannelID,ChannelName"
}



@task(task_id=OPTIMOVE_TOKEN_TASK_ID)
def get_token_task():
    return optimove.get_token_for_airflow()

def json_to_csv_string_converter(fields: List[str]):
    def convert_response_to_csv_string(response):
        parsed = response.json()
        df = pd.DataFrame(parsed, dtype=object)
        df = df[fields]
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False, header=False)
        return csv_buffer.getvalue()
    return convert_response_to_csv_string

@dag(
    default_args=args.get_dsea_default_args(),
    schedule_interval=config.get_value_for_env(dev=None, test=None, prod="0 8 * * *"),
    start_date=pendulum.datetime(2023, 4, 10, tz="America/New_York"),
    on_failure_callback=callbacks.dag_failure_callback,
    on_success_callback=callbacks.dag_success_callback,
    catchup=False,
    tags=["optimove"],
    template_searchpath="dags/data_transformations/sql/optimove_to_snowflake/",
)

def optimove_extract():
    target_schema = "OPTIMOVE"
    target_db = config.get_value_for_env(
        dev=None, test="FBG_SOURCE_UAT", prod="FBG_SOURCE"
    )
    stage = config.get_value_for_env(
        dev=None,
        test="FBG_DATA_OPTIMOVE_TEST_STAGE",
        prod="FBG_DATA_OPTIMOVE_PROD_STAGE",
    )
    bucket_name = config.append_env("fbg-data-optimove-")

    for src in EXTRACT_URLS:
        s3_prefix = f"optimove/{src}/{src}_"
        s3_key = s3_prefix + "{{ ds }}.csv"
        get_data = HttpToS3Operator(
            task_id=f"get_{src}",
            http_conn_id=None,
            endpoint=EXTRACT_URLS[src]["url"],
            method="GET",
            headers={
                "Authorization-Token": "{{ task_instance.xcom_pull(task_ids='"
                + OPTIMOVE_TOKEN_TASK_ID
                + "', key='return_value') }}"
            },
            response_filter=json_to_csv_string_converter(
                fields=[Field_date[src],src]
            ),
            replace_flag=True,
            s3_conn_id="storage_aws_s3_audit",
            s3_bucket=bucket_name,
            s3_key=s3_key,
        )

        load_into_snowflake = SnowflakeOperator(
            task_id=f"load_{src}",
            sql=f"{src}.sql",
            snowflake_conn_id="snowflake_default",
            warehouse="ETL_XSM_WH",
            params={
                "target_db": target_db,
                "target_schema": target_schema,
                "stage": stage,
                "file_path": s3_prefix,
            },
        )
        get_token_task() >> get_data >> load_into_snowflake





dag = optimove_extract()
